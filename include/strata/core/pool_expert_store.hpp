// include/strata/core/pool_expert_store.hpp - dynamic-layout-backed primary
// expert store over the managed RAM pool.
//
// Consumes the *actual* ExpertLayout the engine uses (native per-layer or
// canonical Q2_0), snapshots only per-layer offset+byte geometry in small
// out-of-pool metadata, and serves (layer, expert) expert payloads from an
// experts.bin backing file through PoolExpertCache + BoundedDirectReader, all
// payload+scratch out of ONE Pool.  No GPU headers, no GGUF shard/role claim,
// no fixed geometry, no second arena/mmap, no payload malloc/spill.
//
// Contract highlights:
//   * open() is transactional: reader+validated snapshot+one page of same-pool
//     scratch are prepared first; the old store/cache survive on any failure.
//   * acquire() validates open/layer/expert before any cache admission or
//     eviction; exact bytes derive from the snapshot, never from the caller.
//   * release()/clear() drop only cache refs; scratch stays charged.  close()
//     clears cache, closes reader, releases scratch; existing borrowers stay
//     valid (lease state outlives the Pool wrapper).
//
// Default path untouched: not wired into the engine yet.  Opt-in test only.

#pragma once

#include <cstdint>
#include <mutex>
#include <system_error>
#include <vector>

#include "strata/core/pool_expert_cache.hpp"
#include "strata/platform/bounded_direct_reader.hpp"

namespace strata::kernels::cpu {
struct ExpertLayout;  // forward-declared; real header included by the cpp
}

namespace strata::core {

/// Per-layer snapshot geometry: where the layer's experts start in the file,
/// and each expert's byte size.  Outside pool metadata only; never part of
/// Pool stats.
struct LayerGeom {
    int64_t layer = 0;
    uint64_t offset = 0;   // layer start offset in experts.bin (checked)
    uint64_t bytes = 0;    // per-expert bytes (nonzero, checked)
};

/// Primary expert store backed by dynamic layout geometry over experts.bin.
class PoolExpertStore {
public:
    PoolExpertStore() = delete;
    explicit PoolExpertStore(Pool& pool) noexcept : pool_(&pool), cache_(pool) {}
    PoolExpertStore(const PoolExpertStore&) = delete;
    PoolExpertStore& operator=(const PoolExpertStore&) = delete;
    PoolExpertStore(PoolExpertStore&&) = delete;
    PoolExpertStore& operator=(PoolExpertStore&&) = delete;
    ~PoolExpertStore();

    /// Open `experts_bin` read-only and snapshot validated geometry from
    /// `layout`.  Transactional: a failed open leaves this store and its cache
    /// exactly as before, and `layout` may be destroyed/mutated after success.
    /// The caller keeps `experts_bin` immutable externally.
    std::error_code open(const char* experts_bin,
                         const strata::kernels::cpu::ExpertLayout& layout) noexcept;

    /// Acquire the cached payload for (layer, expert) into `out`.  Exact bytes
    /// derive from the snapshot.  Validates open/layer/expert before any cache
    /// admission or eviction; preserves populated `out` on every failure.
    std::error_code acquire(int64_t layer, int64_t expert, PoolLease& out) noexcept;

    /// Drop this store's cache reference to (layer, expert) if present. Never
    /// allocates.  Borrowers holding copies are unaffected.
    void release(int64_t layer, int64_t expert) noexcept;

    /// Drop every cache-owned reference.  Never allocates.  Scratch stays
    /// charged; borrowers may outlive the store.
    void clear() noexcept;

    /// Clear cache, close the reader, release scratch.  Existing borrowers
    /// remain valid (their leases keep the mapping alive).
    void close() noexcept;

    bool valid() const noexcept { return open_; }
    int64_t n_layers() const noexcept { return n_layers_; }
    int64_t n_expert() const noexcept { return n_expert_; }
    uint64_t total() const noexcept { return total_; }
    /// Per-expert bytes for `layer` (from the snapshot); 0 when invalid.
    uint64_t blob_bytes(int64_t layer) const noexcept {
        if (layer < 0 || layer >= n_layers_) return 0;
        return layers_[(size_t)layer].bytes;
    }
    /// Bytes reserved for scratch (page-aligned, same pool).  Zero until open.
    uint64_t scratch_bytes() const noexcept { return scratch_.valid() ? scratch_.size() : 0; }

    /// Cache-only stats (metadata/payload geometry not in Pool stats).
    PoolExpertCache::Stats cache_stats() const noexcept { return cache_.stats(); }

private:
    struct Prepared {
        std::vector<LayerGeom> layers;
        int64_t n_layers = 0;
        int64_t n_expert = 0;
        uint64_t total = 0;
        uint64_t scratch_bytes = 0;
    };

    /// Validate layout + file size, fill `prepared`.  Does not touch the pool.
    std::error_code prepare_geometry(const strata::kernels::cpu::ExpertLayout& layout,
                                     uint64_t file_size,
                                     Prepared& prepared) const noexcept;

    Pool* pool_ = nullptr;            // non-owning; must outlive the store
    mutable std::mutex mu_;
    bool open_ = false;
    int64_t n_layers_ = 0;
    int64_t n_expert_ = 0;
    uint64_t total_ = 0;
    std::vector<LayerGeom> layers_;   // out-of-pool metadata snapshot
    strata::platform::BoundedDirectReader reader_;      // readonly fd over experts.bin
    PoolExpertCache cache_;           // payload cache over the same pool
    PoolLease scratch_;               // one page, same pool, reader's alignment
};

}  // namespace strata::core
