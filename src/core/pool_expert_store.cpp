// src/core/pool_expert_store.cpp - dynamic-layout-backed primary expert store.
//
// Consumes the actual ExpertLayout, snapshots per-layer offset+byte geometry
// out of pool, serves (layer, expert) payloads from experts.bin via
// PoolExpertCache + BoundedDirectReader, all payload+scratch out of ONE Pool.

#include "strata/core/pool_expert_store.hpp"

#include "strata/kernels/cpu/expert_layout.hpp"

#include <cstdint>
#include <system_error>
#include <utility>

namespace strata::core {

namespace {

using std::errc;

// Checked multiply: *out = a * b, false on overflow.
bool checked_mul(uint64_t a, uint64_t b, uint64_t& out) noexcept {
    if (a == 0 || b == 0) { out = 0; return true; }
    if (a > UINT64_MAX / b) return false;
    out = a * b;
    return true;
}

// Checked add: *out = a + b, false on overflow.
bool checked_add(uint64_t a, uint64_t b, uint64_t& out) noexcept {
    if (a > UINT64_MAX - b) return false;
    out = a + b;
    return true;
}

}  // namespace

// ---- lifecycle -------------------------------------------------------------

PoolExpertStore::~PoolExpertStore() {
    close();
}

std::error_code PoolExpertStore::open(const char* experts_bin,
                                      const strata::kernels::cpu::ExpertLayout& layout) noexcept {
    if (experts_bin == nullptr)
        return std::make_error_code(errc::invalid_argument);

    // The whole open (preparation + commit) is serialized against acquire()/
    // close(): the commit mutates reader_/scratch_/layers_, which a concurrent
    // acquire() reads under the same mutex.
    std::lock_guard<std::mutex> lk(mu_);

    // ---- transactional preparation: nothing mutates shared state yet ----
    strata::platform::BoundedDirectReader new_reader;
    std::error_code ec = new_reader.open(experts_bin);
    if (ec) return ec;  // old reader + cache preserved

    const uint64_t file_size = new_reader.size();
    if (file_size == 0)
        return std::make_error_code(errc::invalid_argument);

    Prepared prep;
    ec = prepare_geometry(layout, file_size, prep);
    if (ec) return ec;

    // ---- admit one page of same-pool scratch (reader alignment) ----
    // May transiently need an extra page; never evicts cache entries.
    prep.scratch_bytes = new_reader.alignment();
    PoolLease scratch;
    ec = pool_->pool_alloc(prep.scratch_bytes, prep.scratch_bytes, scratch);
    if (ec) return ec;

    // ---- commit: clear old cache, then swap/move prepared state (no alloc) ----
    cache_.clear();
    reader_ = std::move(new_reader);
    scratch_ = std::move(scratch);
    layers_ = std::move(prep.layers);
    n_layers_ = prep.n_layers;
    n_expert_ = prep.n_expert;
    total_ = prep.total;
    open_ = true;
    return {};
}

std::error_code PoolExpertStore::prepare_geometry(const strata::kernels::cpu::ExpertLayout& layout,
                                                  uint64_t file_size,
                                                  Prepared& prepared) const noexcept {
    using std::errc;

    // ---- validate dimensions ----
    if (layout.n_layers <= 0 || layout.n_expert <= 0)
        return std::make_error_code(errc::invalid_argument);
    const int64_t n_layers = layout.n_layers;
    const int64_t n_expert = layout.n_expert;

    // native vectors must cover every layer.
    if (layout.native) {
        if ((int64_t)layout.offset.size() < n_layers ||
            (int64_t)layout.bytes.size() < n_layers)
            return std::make_error_code(errc::invalid_argument);
    }

    prepared.layers.clear();
    prepared.layers.reserve((size_t)n_layers);

    uint64_t prev_end = 0;
    bool first = true;

    for (int64_t l = 0; l < n_layers; ++l) {
        // per-expert bytes: native exact, canonical constant.
        uint64_t bytes = 0;
        if (layout.native) {
            bytes = layout.bytes[(size_t)l];
        } else {
            bytes = (uint64_t)strata::kernels::cpu::BLOB;  // canonical: one blob size for every layer
        }
        if (bytes == 0) return std::make_error_code(errc::invalid_argument);

        // layer start offset: native exact, canonical derived with checked math.
        uint64_t offset = 0;
        if (layout.native) {
            offset = layout.offset[(size_t)l];
        } else {
            uint64_t per_layer = 0;
            if (!checked_mul((uint64_t)n_expert, (uint64_t)strata::kernels::cpu::BLOB, per_layer))
                return std::make_error_code(errc::result_out_of_range);
            if (!checked_mul((uint64_t)l, per_layer, offset))
                return std::make_error_code(errc::result_out_of_range);
        }

        // extent: offset + n_expert * bytes, checked.
        uint64_t layer_bytes = 0;
        if (!checked_mul((uint64_t)n_expert, bytes, layer_bytes))
            return std::make_error_code(errc::result_out_of_range);
        uint64_t end = 0;
        if (!checked_add(offset, layer_bytes, end))
            return std::make_error_code(errc::result_out_of_range);

        // ordered non-overlapping layer ranges (gaps allowed), within total.
        if (!first && offset < prev_end) return std::make_error_code(errc::invalid_argument);  // overlap
        prev_end = end;
        first = false;

        LayerGeom g;
        g.layer = l;
        g.offset = offset;
        g.bytes = bytes;
        prepared.layers.push_back(std::move(g));
    }

    // total extent must equal layout.total and the file size.
    if (prev_end > layout.total) return std::make_error_code(errc::invalid_argument);
    // The file either holds exactly the layout's expert bytes, or those bytes plus the ONE spare blob the engine's own
    // STRATA_ARENA_MMAP writer appends (expert_file_bytes_ok, expert_layout.hpp) - the pack on this box is that form:
    // 48 layers x 512 blobs = 50292326400 B of data, then one 2662400 B max blob of padding.  Both sizes are the same
    // expert geometry (the offsets and blob sizes above are unchanged); any other size is still refused.
    if (!strata::kernels::cpu::expert_file_bytes_ok(layout, file_size))
        return std::make_error_code(errc::invalid_argument);

    prepared.n_layers = n_layers;
    prepared.n_expert = n_expert;
    prepared.total = layout.total;
    prepared.scratch_bytes = 0;
    return {};
}

// ---- acquire ---------------------------------------------------------------

std::error_code PoolExpertStore::acquire(int64_t layer, int64_t expert,
                                         PoolLease& out) noexcept {
    std::lock_guard<std::mutex> lk(mu_);

    if (!open_) return std::make_error_code(errc::not_connected);
    if (layer < 0 || layer >= n_layers_) return std::make_error_code(errc::invalid_argument);
    if (expert < 0 || expert >= n_expert_) return std::make_error_code(errc::invalid_argument);

    const LayerGeom& g = layers_[(size_t)layer];
    const uint64_t bytes = g.bytes;

    // source offset: layer start + expert * per-expert bytes, checked.
    uint64_t expert_off = 0;
    if (!checked_mul((uint64_t)expert, bytes, expert_off))
        return std::make_error_code(errc::result_out_of_range);
    uint64_t src_off = 0;
    if (!checked_add(g.offset, expert_off, src_off))
        return std::make_error_code(errc::result_out_of_range);

    // Reader callback: resolved checked offset, real O_DIRECT read into same-
    // pool scratch.  Runs under the cache mutex (store mutex held here too).
    auto reader = [this, src_off, bytes](int64_t, int64_t,
                                         uint8_t* dst, uint64_t n)
        -> std::error_code {
        if (n != bytes) return std::make_error_code(errc::invalid_argument);
        return reader_.read_exact(src_off, bytes, dst,
                                  static_cast<uint8_t*>(scratch_.data()),
                                  scratch_.size());
    };

    return cache_.acquire(layer, expert, bytes, reader, out);
}

// ---- release / clear / close ----------------------------------------------

void PoolExpertStore::release(int64_t layer, int64_t expert) noexcept {
    cache_.release(layer, expert);
}

void PoolExpertStore::clear() noexcept {
    cache_.clear();
}

void PoolExpertStore::close() noexcept {
    // Serialized against acquire()/open(): acquire() reads reader_/scratch_
    // under this mutex, so the state teardown must hold it too.
    std::lock_guard<std::mutex> lk(mu_);
    open_ = false;
    cache_.clear();
    reader_.close();
    scratch_ = PoolLease{};  // release scratch; borrowers keep their leases
    n_layers_ = 0;
    n_expert_ = 0;
    total_ = 0;
    layers_.clear();
}

}  // namespace strata::core
