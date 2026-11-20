// tests/core/pool_expert_store_test.cpp - deterministic CPU-only tests for the
// dynamic-layout-backed primary expert store (PoolExpertStore) over the RAM
// pool.
//
// Uses a REAL ExpertLayout (native, 2-3 layers, nonuniform per-expert bytes
// including >scratch page, nonzero/gapped offsets, page-distinct bytes) and a
// REAL experts.bin fixture written once and frozen thereafter.  All payload +
// scratch out of ONE REAL pool (mmap+mlock via link-time --wrap).  Fake ONLY
// lock calls; real mmap and direct file I/O.  Total live mappings <= 1 MiB.
//
// Usage: pool_expert_store_test <build_dir>

#define STRATA_RAM_POOL_TEST 1
#include "strata/core/pool_expert_store.hpp"
#include "strata/core/ram_pool.hpp"
#include "strata/kernels/cpu/expert_layout.hpp"
#undef STRATA_RAM_POOL_TEST

#include <algorithm>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>

namespace {

using strata::core::Pool;
using strata::core::PoolLease;
using strata::core::PoolExpertStore;
using strata::core::pool_capacity_bytes;
using strata::kernels::cpu::ExpertLayout;

int checks = 0;
int failures = 0;

void check(bool ok, const char* label) {
    ++checks;
    if (!ok) { ++failures; std::fprintf(stderr, "FAIL: %s\n", label); }
    else { std::fprintf(stderr, "pass: %s\n", label); }
}

constexpr uint64_t kPage = 4096;

// ---- deterministic fixture data, keyed by global file offset ----------------

static uint8_t byte_at(uint64_t p) {
    return static_cast<uint8_t>((p * 31u) ^ (p >> 8) ^ (p >> 16) ^ (p >> 24) ^ 7u);
}

std::string g_full_data;     // byte_at(p) for p in [0, total)
std::string g_fixture_path;  // experts.bin fixture (written once, frozen)
std::string g_missing_path;  // path that does not exist

static bool write_file(const std::string& path, const std::string& data) {
    std::FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) return false;
    if (std::fwrite(data.data(), 1, data.size(), f) != data.size()) {
        std::fclose(f);
        return false;
    }
    return std::fclose(f) == 0;
}

// Native layout: 3 layers, nonuniform per-expert bytes (64, 128, 5000>scratch),
// nonzero gapped offsets (0, 4096, 8192), 4 experts per layer.
//   layer 0: [0, 256)       bytes/expert=64
//   layer 1: [4096, 4608)   bytes/expert=128  (gap 256..4096)
//   layer 2: [8192, 28192)  bytes/expert=5000 (gap 4608..8192, > page scratch)
// total = 8192 + 4*5000 = 28192 = file size.
static bool build_layout(ExpertLayout& layout) {
    layout = ExpertLayout{};
    layout.native = true;
    layout.n_layers = 3;
    layout.n_expert = 4;
    layout.offset = {0, 4096, 8192};
    layout.bytes = {64, 128, 5000};
    layout.max_blob = 5000;
    layout.total = 28192;
    return true;
}

static bool build_fixture(const std::string& dir) {
    ExpertLayout layout;
    if (!build_layout(layout)) return false;
    g_full_data.resize(layout.total);
    for (uint64_t p = 0; p < layout.total; ++p) g_full_data[p] = byte_at(p);

    g_fixture_path = dir + "/expert_store_fixture_20261120.bin";
    if (!write_file(g_fixture_path, g_full_data)) return false;

    g_missing_path = dir + "/expert_store_missing_20261120.bin";
    return true;
}

static bool region_matches(const void* data, uint64_t file_offset, uint64_t size) {
    if (size == 0) return true;
    if (file_offset + size > g_full_data.size()) return false;
    return std::memcmp(data, g_full_data.data() + file_offset, size) == 0;
}

static bool file_contents_equal(const std::string& path,
                                const std::string& expected) {
    std::FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) return false;
    std::string got;
    char buf[4096];
    size_t n;
    while ((n = std::fread(buf, 1, sizeof buf, f)) > 0) got.append(buf, n);
    std::fclose(f);
    return got == expected;
}

// ---- link-time interposition: mmap/munmap real+counted, mlock/munlock fake,--
// ---- pread wrapped to count calls issued by the store's reader --------------

namespace wrap {
int mmap_calls = 0;
int munmap_calls = 0;
int mlock_calls = 0;
int munlock_calls = 0;
int pread_calls = 0;
static std::map<void*, uint64_t> live_maps;
static uint64_t total_live_bytes = 0;
static uint64_t peak_live_bytes = 0;
constexpr uint64_t kMappingLimit = 32ull << 20;  // 32 MiB
// The store's scratch is now a WHOLE blob (plus one alignment page), not one
// page, so a pool for a multi-megabyte blob is genuinely multi-megabyte: the
// over-cap scenario (9 MB blob, 8 MiB scratch) reserves 16.6 MiB.  Still far
// from a real run's tens of GiB, and every pool is released at its scope end.
}  // namespace wrap

static void track_mmap(void* p, uint64_t len) {
    if (p == nullptr || p == MAP_FAILED) return;
    if (wrap::live_maps.count(p)) { std::fprintf(stderr, "FAIL: duplicate mmap\n"); std::abort(); }
    wrap::live_maps[p] = len; wrap::total_live_bytes += len;
    wrap::peak_live_bytes = std::max(wrap::peak_live_bytes, wrap::total_live_bytes);
    if (wrap::total_live_bytes > wrap::kMappingLimit) std::abort();
}
static void track_munmap(void* p, uint64_t len) {
    auto it = wrap::live_maps.find(p);
    if (it == wrap::live_maps.end() || it->second != len) {
        std::fprintf(stderr, "FAIL: unmatched munmap\n"); std::abort();
    }
    wrap::total_live_bytes -= it->second; wrap::live_maps.erase(it);
}

extern "C" {
void* __real_mmap(void*, size_t, int, int, int, off_t);
int __real_munmap(void*, size_t);
int __real_mlock(const void*, size_t);
int __real_munlock(const void*, size_t);
ssize_t __real_pread(int, void*, size_t, off_t);

void* __wrap_mmap(void* a, size_t l, int p, int f, int d, off_t o) {
    ++wrap::mmap_calls;
    void* p2 = __real_mmap(a, l, p, f, d, o);
    if (p2 == MAP_FAILED) return p2;
    track_mmap(p2, l);
    return p2;
}
int __wrap_munmap(void* a, size_t l) {
    ++wrap::munmap_calls;
    track_munmap(a, l);
    return __real_munmap(a, l);
}
int __wrap_mlock(const void*, size_t) { ++wrap::mlock_calls; return 0; }
int __wrap_munlock(const void*, size_t) { ++wrap::munlock_calls; return 0; }
ssize_t __wrap_pread(int fd, void* buf, size_t count, off_t offset) {
    ++wrap::pread_calls;
    return __real_pread(fd, buf, count, offset);
}
}  // extern "C"

// ---- helpers ----------------------------------------------------------------

static std::error_code make_pool(Pool& out, uint64_t bytes) {
    std::error_code ec;
    auto p = Pool::create(bytes, ec);
    out = std::move(p);
    return ec;
}

// Expert (layer, expert) -> file offset for the native fixture layout.
static uint64_t expert_file_offset(int64_t layer, int64_t expert) {
    static const uint64_t off[3] = {0, 4096, 8192};
    static const uint64_t b[3] = {64, 128, 5000};
    return off[(size_t)layer] + (uint64_t)expert * b[(size_t)layer];
}
static uint64_t expert_bytes(int64_t layer) {
    static const uint64_t b[3] = {64, 128, 5000};
    return b[(size_t)layer];
}

}  // namespace

int main(int argc, char** argv) {
    const std::string build_dir = (argc > 1) ? argv[1] : ".";
    if (!build_fixture(build_dir)) {
        std::fprintf(stderr, "FAIL: cannot write fixture\n");
        return 2;
    }

    // ---- 1. open + every valid key resolves, full byte equality, (0,0) -----
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);  // 256 KiB
        check(!ec, "create 256KiB pool");

        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "open real fixture + native layout");
        check(store.valid(), "store valid after open");
        check(store.n_layers() == 3 && store.n_expert() == 4, "geometry snapshot");
        check(store.total() == 28192, "total matches layout.total");
        // Scratch = round_up(max blob 5000 + one alignment page, page) = 12288:
        // one whole blob per pread, not one per 4 KiB.
        check(store.scratch_bytes() == 3 * kPage, "scratch holds a whole blob (3 pages)");
        check(store.stats().blob_bytes_max == 5000, "stats: largest blob reported");
        check(store.stats().io_window_bytes == store.scratch_bytes(),
              "stats: io window is the whole scratch");
        check(store.stats().preads_per_blob == 1, "stats: one pread per blob");

        // Every valid key, including (0,0), resolves with full byte equality.
        for (int64_t l = 0; l < 3; ++l) {
            for (int64_t e = 0; e < 4; ++e) {
                PoolLease out;
                auto ec2 = store.acquire(l, e, out);
                check(!ec2 && out.valid(), "acquire valid key");
                check(region_matches(out.data(), expert_file_offset(l, e), expert_bytes(l)),
                      "acquire: full byte equality vs fixture");
            }
        }

        // Same key hit does not reread: pread count stable on re-acquire.
        const int before = wrap::pread_calls;
        PoolLease again;
        auto ec2 = store.acquire(0, 0, again);
        check(!ec2 && again.valid(), "re-acquire (0,0) hit");
        check(wrap::pread_calls == before, "same-key hit: no reread");
        check(region_matches(again.data(), 0, 64), "(0,0) hit bytes correct");

        // ---- invalid key preserves actual populated out, no eviction/read --
        {
            const auto entries_before = store.cache_stats().entries;
            const auto ptr = again.data();
            const auto sz = again.size();
            const int pread_before = wrap::pread_calls;
            auto ec3 = store.acquire(9, 9, again);  // bad layer
            check(ec3 == std::make_error_code(std::errc::invalid_argument),
                  "invalid key: error returned");
            check(again.data() == ptr && again.size() == sz,
                  "invalid key: populated out preserved");
            check(store.cache_stats().entries == entries_before,
                  "invalid key: no cache change");
            check(wrap::pread_calls == pread_before, "invalid key: no pread");
        }
        again = PoolLease{};
    }

    // ---- 2. same-key hit returns same lease bytes, no second read -----------
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "create pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "open");

        PoolLease out;
        ec = store.acquire(2, 0, out);  // 5000-byte expert (> scratch page)
        check(!ec && out.valid(), "acquire (2,0) 5000 bytes");
        check(region_matches(out.data(), 8192, 5000), "(2,0) full nonuniform bytes");

        const void* hit_ptr = out.data();
        const uint64_t hit_size = out.size();
        const int pread_before = wrap::pread_calls;
        auto ec2 = store.acquire(2, 0, out);
        check(!ec2, "re-acquire (2,0)");
        check(wrap::pread_calls == pread_before, "hit: no pread");
        check(out.data() == hit_ptr && out.size() == hit_size,
              "hit: same lease bytes");
        check(store.cache_stats().hits == 1, "hit counted");
        out = PoolLease{};
    }

    // ---- 3. caller layout mutation/destruction after open does not alter ----
    // ----    the store snapshot ---------------------------------------------
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "create pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "open");

        // Mutate/destroy the caller's layout after open.
        layout.offset[0] = 999999;
        layout.bytes[0] = 0;
        layout.n_layers = 0;
        layout.n_expert = 0;
        layout.total = 0;

        // Store snapshot unchanged: every key still resolves correctly.
        for (int64_t l = 0; l < 3; ++l) {
            for (int64_t e = 0; e < 4; ++e) {
                PoolLease out;
                auto ec2 = store.acquire(l, e, out);
                check(!ec2 && out.valid(), "post-mutation acquire valid key");
                check(region_matches(out.data(), expert_file_offset(l, e), expert_bytes(l)),
                      "post-mutation byte equality (snapshot preserved)");
            }
        }
    }

    // ---- 4. failure cases preserve old store + exact old borrower bytes -----
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "create pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "open valid store");

        // Warm a borrower whose bytes must be preserved on every failure.
        PoolLease borrower;
        ec = store.acquire(0, 0, borrower);
        check(!ec && borrower.valid(), "warm borrower (0,0)");
        const void* borrower_ptr = borrower.data();
        const uint64_t borrower_sz = borrower.size();
        const auto entries_before = store.cache_stats().entries;

        // Truncated copy (first 100 bytes only), reused by 4c and 4d.
        std::string trunc_path;
        {
            std::string truncated = g_full_data.substr(0, 100);
            trunc_path = build_dir + "/expert_store_trunc_20261120.bin";
            if (!write_file(trunc_path, truncated)) { std::fprintf(stderr, "FAIL: write trunc\n"); std::abort(); }
        }

        // 4a. malformed layout: zero per-expert bytes.
        {
            ExpertLayout bad = layout;
            bad.bytes[0] = 0;
            PoolExpertStore fresh(pool);
            auto ec2 = fresh.open(g_fixture_path.c_str(), bad);
            check(ec2 == std::make_error_code(std::errc::invalid_argument),
                  "malformed layout zero bytes: rejected");
            check(!fresh.valid(), "malformed layout: fresh store not open");
        }
        // 4b. open missing file.
        {
            ExpertLayout good = layout;
            PoolExpertStore fresh(pool);
            auto ec2 = fresh.open(g_missing_path.c_str(), good);
            check(ec2 == std::make_error_code(std::errc::no_such_file_or_directory) ||
                  ec2 == std::make_error_code(std::errc::invalid_argument),
                  "missing file: rejected");
            check(!fresh.valid(), "missing file: fresh store not open");
        }
        // 4c. size mismatch: file smaller than layout.total (truncated).
        {
            // Truncated copy: first 100 bytes only.
            std::string truncated = g_full_data.substr(0, 100);
            if (!write_file(trunc_path, truncated)) { std::fprintf(stderr, "FAIL: write trunc\n"); std::abort(); }
            ExpertLayout good = layout;
            PoolExpertStore fresh(pool);
            auto ec2 = fresh.open(trunc_path.c_str(), good);
            check(ec2 == std::make_error_code(std::errc::invalid_argument),
                  "truncated file (size mismatch): rejected");
            check(!fresh.valid(), "truncated: fresh store not open");
        }
        // 4d. overlap: layer ranges overlap.
        {
            ExpertLayout bad = layout;
            bad.offset[1] = 100;  // overlaps layer 0 [0,256)
            PoolExpertStore fresh(pool);
            auto ec2 = fresh.open(trunc_path.c_str(), bad);
            // Note: file is truncated too, but overlap check may come first.
            check(static_cast<bool>(ec2), "overlap layout: rejected");
            check(!fresh.valid(), "overlap: fresh store not open");
        }

        // Old store still open and borrower bytes intact.
        check(store.valid(), "old store still valid after failures");
        PoolLease recheck;
        auto ec2 = store.acquire(0, 0, recheck);
        check(!ec2 && recheck.valid(), "old store still serves (0,0)");
        check(region_matches(recheck.data(), 0, 64), "old borrower bytes intact");
        check(store.cache_stats().entries == entries_before,
              "old store cache unchanged by failed opens");

        borrower = PoolLease{};
        recheck = PoolLease{};
    }

    // ---- 5. scratch reserved in SAME pool: tight capacity, eviction, reload --
    {
        // The store's scratch for this fixture is round_up(5000 + 4096, 4096) =
        // 12288 = 3 pages (a whole blob per pread).  Pool = scratch + one
        // 4096-page entry = exactly full.
        constexpr uint64_t kFixtureScratch = 3 * kPage;
        Pool pool;
        auto ec = make_pool(pool, kFixtureScratch + kPage);
        check(!ec, "create tight 16KiB pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "open on tight pool");

        // Scratch charged, and it is the whole-blob size.
        check(store.scratch_bytes() == kFixtureScratch, "scratch charged in pool (whole blob)");
        check(store.stats().scratch_bytes == kFixtureScratch, "stats: scratch is real pool bytes");
        check(store.stats().pool_capacity_bytes == pool.capacity(), "stats: pool capacity");
        check(store.stats().usable_bytes == pool.capacity() - kFixtureScratch,
              "stats: usable = pool minus scratch");

        // Acquire a 64-byte expert (reserves 4096) -> pool now full (scratch+entry).
        PoolLease a;
        ec = store.acquire(0, 0, a);
        check(!ec && a.valid(), "tight: acquire (0,0)");
        check(pool.stats().reserved_live_bytes == kFixtureScratch + kPage,
              "tight: pool full (scratch + one entry)");

        // Live borrower blocks a second admission.
        PoolLease b;
        auto ec2 = store.acquire(0, 1, b);
        check(ec2 == std::make_error_code(std::errc::no_buffer_space),
              "tight: live borrower blocks second admission");
        check(!b.valid(), "tight: no lease on refusal");

        // Release borrower -> admission succeeds.
        a = PoolLease{};
        auto ec3 = store.acquire(0, 1, b);
        check(!ec3 && b.valid(), "tight: admission after release");
        check(region_matches(b.data(), 64, 64), "tight: (0,1) bytes correct");
        b = PoolLease{};  // release borrower so clear leaves only scratch

        // Clear leaves only scratch.
        store.clear();
        check(store.cache_stats().entries == 0, "clear: no entries");
        check(pool.stats().reserved_live_bytes == kFixtureScratch,
              "clear: only scratch remains");

        // Another OWNER reuses/overwrites the freed payload, then reload
        // restores ALL source bytes (fixture never altered).
        PoolLease full;
        ec = pool.pool_alloc(pool.capacity() - kFixtureScratch, kPage, full);
        check(!ec && full.valid(), "owner reuses freed range");
        std::memset(full.data(), 0xBB, full.size());
        full = PoolLease{};

        PoolLease reloaded;
        ec = store.acquire(0, 0, reloaded);
        check(!ec && reloaded.valid(), "reload restores (0,0)");
        check(region_matches(reloaded.data(), 0, 64), "reload: ALL source bytes restored");

        // Fixture file never altered (bytes + length unchanged).
        check(file_contents_equal(g_fixture_path, g_full_data),
              "fixture immutable at end (bytes + length)");

        reloaded = PoolLease{};
    }

    // ---- 6. close/destruction while borrower held preserves bytes + mapping -
    {
        PoolLease borrower;
        {
            Pool pool;
            auto ec = make_pool(pool, 256ull << 10);
            check(!ec, "create pool");
            ExpertLayout layout;
            build_layout(layout);
            PoolExpertStore store(pool);
            ec = store.open(g_fixture_path.c_str(), layout);
            check(!ec, "open");

            ec = store.acquire(2, 0, borrower);  // 5000-byte expert
            check(!ec && borrower.valid(), "acquire borrower (2,0)");
            // store + pool destroyed at scope end while borrower alive
        }
        check(borrower.valid(), "borrower valid after store+pool destroyed");
        check(region_matches(borrower.data(), 8192, 5000),
              "borrower payload intact after owner scopes gone");
        check(wrap::total_live_bytes == 256ull << 10,
              "pool mapping alive while borrower holds lease");

        borrower = PoolLease{};  // final release -> exact last unmap
        check(wrap::live_maps.empty(), "final: no live mappings");
        check(wrap::total_live_bytes == 0, "final: zero live bytes");
        check(wrap::mmap_calls == wrap::munmap_calls,
              "final: mmap_calls == munmap_calls");
        check(wrap::peak_live_bytes <= wrap::kMappingLimit,
              "peak mapping <= 2 MiB");
    }

    // ---- 6b. canonical Q2_0 layout path (native=false, derived offsets) -----
    {
        // One layer, one expert: total = BLOB (1,382,400).  The canonical path
        // derives the offset/bytes with checked arithmetic instead of vectors.
        ExpertLayout layout;
        layout = ExpertLayout{};
        layout.native = false;
        layout.n_layers = 1;
        layout.n_expert = 1;
        layout.total = strata::kernels::cpu::BLOB;

        const std::string canon_path = build_dir + "/expert_store_canon_20261120.bin";
        std::string canon_data;
        canon_data.resize(layout.total);
        for (uint64_t p = 0; p < layout.total; ++p) canon_data[p] = byte_at(p + 7);
        if (!write_file(canon_path, canon_data)) { std::fprintf(stderr, "FAIL: write canon\n"); std::abort(); }

        // Pool fits scratch (whole BLOB + one alignment page) + the page-rounded
        // BLOB payload.
        const uint64_t payload = ((layout.total + kPage - 1) / kPage) * kPage;
        const uint64_t scratch = ((layout.total + kPage + kPage - 1) / kPage) * kPage;
        Pool pool;
        auto ec = make_pool(pool, payload + scratch);
        check(!ec, "create canonical pool");
        PoolExpertStore store(pool);
        ec = store.open(canon_path.c_str(), layout);
        check(!ec, "open canonical layout");
        check(store.valid() && store.total() == strata::kernels::cpu::BLOB,
              "canonical geometry snapshot");
        check(store.scratch_bytes() == scratch, "canonical: scratch is a whole BLOB + a page");
        check(store.stats().preads_per_blob == 1, "canonical: one pread for a whole BLOB");

        PoolLease out;
        ec = store.acquire(0, 0, out);
        check(!ec && out.valid(), "canonical acquire (0,0)");
        check(out.size() == payload, "canonical payload page-rounded size");
        check(std::memcmp(out.data(), canon_data.data(), layout.total) == 0,
              "canonical: ALL BLOB bytes correct");
        out = PoolLease{};

        // Canonical file mismatch rejected transactionally.
        ExpertLayout wrong = layout;
        wrong.n_expert = 2;   // total no longer matches the file
        PoolExpertStore fresh(pool);
        auto ec2 = fresh.open(canon_path.c_str(), wrong);
        check(static_cast<bool>(ec2) && !fresh.valid(),
              "canonical size mismatch rejected");
        check(store.valid(), "canonical old store preserved");
    }

    // ---- 6c. vector-short + overflow layouts rejected -----------------------
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "create pool");
        ExpertLayout layout;
        build_layout(layout);

        // Native vectors shorter than n_layers.
        ExpertLayout shortv = layout;
        shortv.offset.resize(1);
        PoolExpertStore fresh1(pool);
        auto ec1 = fresh1.open(g_fixture_path.c_str(), shortv);
        check(ec1 == std::make_error_code(std::errc::invalid_argument),
              "vector-short layout rejected");
        check(!fresh1.valid(), "vector-short: store not open");

        // Per-expert bytes so large that n_expert * bytes overflows uint64.
        ExpertLayout huge = layout;
        huge.bytes[0] = 0x4000000000000000ull;   // 4 * 2^62 overflows
        PoolExpertStore fresh2(pool);
        auto ec2 = fresh2.open(g_fixture_path.c_str(), huge);
        check(ec2 == std::make_error_code(std::errc::result_out_of_range),
              "overflow layout rejected");
        check(!fresh2.valid(), "overflow: store not open");
    }

    // ---- 6c2. the padded experts.bin the engine's own arena writer leaves --
    // expert_source.cpp's STRATA_ARENA_MMAP writer appends ONE spare blob (lay.max_blob) to the layout's expert
    // bytes so that a whole-slot copy may start at any expert; the pack on this box (iq3xxs-ud) is that form:
    // 50,292,326,400 B of experts + one 2,662,400 B pad = 50,294,988,800 B.  The store must describe that file's
    // real geometry (same offsets and blob sizes, the pad never read) and still refuse any other size.
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "create pool for padded fixture");

        ExpertLayout layout;
        build_layout(layout);   // total 28192, max_blob 5000
        std::string padded = g_full_data;
        padded.append((size_t) layout.max_blob, '\0');   // the spare blob

        const std::string padded_path = build_dir + "/expert_store_padded_20261120.bin";
        if (!write_file(padded_path, padded)) { std::fprintf(stderr, "FAIL: write padded\n"); std::abort(); }

        PoolExpertStore store(pool);
        ec = store.open(padded_path.c_str(), layout);
        check(!ec, "open padded (+one max blob) fixture");
        check(store.valid(), "padded: store valid");
        check(store.total() == layout.total, "padded: total is the expert bytes, not the file size");

        // The pad is never read: every payload is the same bytes as in the unpadded fixture.
        for (int64_t l = 0; l < 3; ++l) {
            PoolLease out;
            auto ec2 = store.acquire(l, 3, out);
            check(!ec2 && region_matches(out.data(), expert_file_offset(l, 3), expert_bytes(l)),
                  "padded: last expert of every layer byte-exact");
        }

        // Any other size stays refused: one byte short of the pad, and two pad blobs.
        std::string short_pad = g_full_data;
        short_pad.append((size_t) layout.max_blob - 1, '\0');
        const std::string short_path = build_dir + "/expert_store_pad_short_20261120.bin";
        if (!write_file(short_path, short_pad)) { std::fprintf(stderr, "FAIL: write\n"); std::abort(); }
        PoolExpertStore fresh(pool);
        auto ec_short = fresh.open(short_path.c_str(), layout);
        check(ec_short == std::make_error_code(std::errc::invalid_argument),
              "one byte short of the pad is rejected");
        check(!fresh.valid(), "short pad: store not open");
        check(store.valid(), "short pad: existing store preserved");

        std::string two_pads = padded;
        two_pads.append((size_t) layout.max_blob, '\0');
        const std::string two_path = build_dir + "/expert_store_pad_two_20261120.bin";
        if (!write_file(two_path, two_pads)) { std::fprintf(stderr, "FAIL: write\n"); std::abort(); }
        PoolExpertStore fresh2(pool);
        auto ec_two = fresh2.open(two_path.c_str(), layout);
        check(ec_two == std::make_error_code(std::errc::invalid_argument),
              "two pad blobs are rejected");
        check(!fresh2.valid(), "two pads: store not open");
    }

    // ---- 6d. never-opened + closed stores refuse acquire --------------------
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "create pool");

        PoolLease out;
        PoolExpertStore unopened(pool);
        auto ec1 = unopened.acquire(0, 0, out);
        check(ec1 == std::make_error_code(std::errc::not_connected),
              "never-opened store refuses acquire");
        check(!out.valid(), "never-opened: no lease");

        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "open for close test");
        ec = store.acquire(0, 0, out);
        check(!ec && out.valid(), "warm before close");
        const auto ptr = out.data();
        const auto sz = out.size();
        const int pread_before = wrap::pread_calls;
        store.close();
        check(!store.valid(), "closed store invalid");
        auto ec2 = store.acquire(0, 0, out);
        check(ec2 == std::make_error_code(std::errc::not_connected),
              "closed store refuses acquire");
        check(out.data() == ptr && out.size() == sz,
              "closed refusal preserves populated out");
        check(wrap::pread_calls == pread_before, "closed refusal: no pread");
        check(pool.stats().reserved_live_bytes == kPage,
              "close released scratch; borrower payload remains reserved");
        out = PoolLease{};
    }

    // ---- 8. one read per blob: multi-megabyte blobs, coalesced path ----------
    {
        // Two layers, 1.5 MB and 3 MB blobs (both far above a page); the big
        // layer starts at 1,500,000 - NOT page aligned, so the covering window
        // needs alignment slack before the blob and must still take ONE call.
        ExpertLayout layout{};
        layout.native = true;
        layout.n_layers = 2;
        layout.n_expert = 2;
        layout.offset = {0, 3000000};
        layout.bytes = {1500000, 3000000};
        layout.max_blob = 3000000;
        layout.total = 3000000 + 2 * 3000000;

        std::string big_data((size_t) layout.total, '\0');
        for (uint64_t p = 0; p < layout.total; ++p) big_data[(size_t) p] = byte_at(p);
        const std::string big_path = build_dir + "/expert_store_bigblob_20261120.bin";
        if (!write_file(big_path, big_data)) { std::fprintf(stderr, "FAIL: write bigblob\n"); std::abort(); }

        const uint64_t scratch = ((3000000ull + kPage + kPage - 1) / kPage) * kPage;
        const uint64_t payload = ((3000000ull + kPage - 1) / kPage) * kPage;
        Pool pool;
        auto ec = make_pool(pool, scratch + payload);
        check(!ec, "8: create pool for a 3MB blob");
        PoolExpertStore store(pool);
        ec = store.open(big_path.c_str(), layout);
        check(!ec, "8: open big-blob layout");
        check(store.scratch_bytes() == scratch, "8: scratch = whole 3MB blob + one page");
        check(store.stats().blob_bytes_max == 3000000, "8: biggest blob is the 3MB one");
        check(store.stats().preads_per_blob == 1, "8: one pread per 3MB blob declared");

        PoolLease out;
        const int before = wrap::pread_calls;
        ec = store.acquire(1, 0, out);   // 3 MB blob at unaligned offset 3,000,000
        check(!ec && out.valid(), "8: acquire 3MB blob");
        // Page-granular reads needed 733 calls for these 3 MB.
        check(wrap::pread_calls - before == 1, "8: exactly ONE pread for a 3MB blob");
        check(out.data() && std::memcmp(out.data(), big_data.data() + 3000000, 3000000) == 0,
              "8: all 3,000,000 bytes correct");
        check(store.stats().resident_pairs == 1 && store.stats().resident_bytes == out.size(),
              "8: stats count the resident pair");
        check(store.stats().misses == 1 && store.stats().hits == 0, "8: miss counted");
        out = PoolLease{};
        store.clear();

        // Second expert of the same layer: offset 6,000,000, also unaligned.
        const int before2 = wrap::pread_calls;
        ec = store.acquire(1, 1, out);
        check(!ec && out.valid(), "8: acquire second 3MB blob");
        check(wrap::pread_calls - before2 == 1, "8: one pread for the second 3MB blob");
        check(std::memcmp(out.data(), big_data.data() + 6000000, 3000000) == 0,
              "8: second blob bytes correct");
        out = PoolLease{};
        store.clear();

        // A 1.5 MB blob (unaligned start too) is one read as well.
        const int before3 = wrap::pread_calls;
        ec = store.acquire(0, 1, out);
        check(!ec && out.valid(), "8: acquire 1.5MB blob");
        check(wrap::pread_calls - before3 == 1, "8: one pread for a 1.5MB blob");
        check(std::memcmp(out.data(), big_data.data() + 1500000, 1500000) == 0,
              "8: 1.5MB blob bytes correct");
        out = PoolLease{};
    }

    // ---- 8b. blob above the 8 MiB cap: cap-sized windows, not pages --------
    {
        ExpertLayout layout{};
        layout.native = true;
        layout.n_layers = 1;
        layout.n_expert = 1;
        layout.offset = {0};
        layout.bytes = {9000000};
        layout.max_blob = 9000000;
        layout.total = 9000000;

        std::string big_data((size_t) layout.total, '\0');
        for (uint64_t p = 0; p < layout.total; ++p) big_data[(size_t) p] = byte_at(p);
        const std::string big_path = build_dir + "/expert_store_overcap_20261120.bin";
        if (!write_file(big_path, big_data)) { std::fprintf(stderr, "FAIL: write overcap\n"); std::abort(); }

        const uint64_t cap = 8ull << 20;
        const uint64_t payload = ((9000000ull + kPage - 1) / kPage) * kPage;
        Pool pool;
        auto ec = make_pool(pool, cap + payload);
        check(!ec, "8b: create pool for a 9MB blob");
        PoolExpertStore store(pool);
        ec = store.open(big_path.c_str(), layout);
        check(!ec, "8b: open over-cap layout");
        check(store.scratch_bytes() == cap, "8b: scratch clamped to the 8 MiB cap");
        check(store.stats().preads_per_blob == 2, "8b: cap window costs 2 preads, not 2198");

        PoolLease out;
        const int before = wrap::pread_calls;
        ec = store.acquire(0, 0, out);
        check(!ec && out.valid(), "8b: acquire 9MB blob");
        check(wrap::pread_calls - before == 2, "8b: two cap-sized preads for 9MB");
        check(std::memcmp(out.data(), big_data.data(), 9000000) == 0,
              "8b: all 9,000,000 bytes correct through the clamped path");
        out = PoolLease{};
    }

    // ---- 8c. the reader's strict checks still fail cleanly ------------------
    {
        strata::platform::BoundedDirectReader reader;
        auto ec = reader.open(g_fixture_path.c_str());
        check(!ec && reader.is_open(), "8c: reader opens the fixture");
        const uint64_t align = reader.alignment();
        check(align >= kPage, "8c: reader alignment");

        alignas(4096) static uint8_t scratch[16384];
        alignas(4096) static uint8_t dst[5000];

        // A valid read: unaligned logical offset, exact bytes.
        ec = reader.read_exact(100, 64, dst, scratch, sizeof scratch);
        check(!ec, "8c: unaligned-offset read succeeds");
        check(std::memcmp(dst, g_full_data.data() + 100, 64) == 0, "8c: unaligned read bytes exact");

        // Misaligned scratch address.
        ec = reader.read_exact(0, 64, dst, scratch + 1, sizeof scratch);
        check(ec == std::make_error_code(std::errc::invalid_argument),
              "8c: misaligned scratch rejected");
        ec = reader.read_exact(0, 64, dst, scratch + align, align - 1);
        check(ec == std::make_error_code(std::errc::invalid_argument),
              "8c: scratch smaller than alignment rejected");
        ec = reader.read_exact(0, 64, dst, scratch, align + 1);
        check(ec == std::make_error_code(std::errc::invalid_argument),
              "8c: scratch not a multiple of alignment rejected");

        // Overlapping dest/scratch spans.
        ec = reader.read_exact(0, 64, scratch + align, scratch, sizeof scratch);
        check(ec == std::make_error_code(std::errc::invalid_argument),
              "8c: overlapping dest/scratch rejected");

        // Zero bytes, and a request beyond the end of the file.
        ec = reader.read_exact(0, 0, dst, scratch, sizeof scratch);
        check(ec == std::make_error_code(std::errc::invalid_argument), "8c: zero bytes rejected");
        ec = reader.read_exact((uint64_t) reader.size() - 100, 200, dst, scratch, sizeof scratch);
        check(ec == std::make_error_code(std::errc::invalid_argument),
              "8c: read past EOF rejected");

        // A file that shrinks under the reader: the aligned window comes up
        // short, which is a clean I/O error (never a silent short payload).
        const std::string shrink_path = build_dir + "/expert_store_shrink_20261120.bin";
        if (!write_file(shrink_path, g_full_data)) { std::fprintf(stderr, "FAIL: write shrink\n"); std::abort(); }
        strata::platform::BoundedDirectReader shrink;
        ec = shrink.open(shrink_path.c_str());
        check(!ec, "8c: shrink fixture opens");
        check(::truncate(shrink_path.c_str(), 100) == 0, "8c: fixture truncated after open");
        ec = shrink.read_exact(0, 200, dst, scratch, sizeof scratch);
        check(ec == std::make_error_code(std::errc::io_error),
              "8c: short read (EOF under the window) is a clean io_error");
        shrink.close();

        // And the store itself still refuses a truncated file at open.
        ExpertLayout layout;
        build_layout(layout);
        {
            Pool pool;
            auto ec2 = make_pool(pool, 256ull << 10);
            check(!ec2, "8c: pool for truncated open");
            PoolExpertStore fresh(pool);
            auto ec3 = fresh.open(shrink_path.c_str(), layout);
            check(ec3 == std::make_error_code(std::errc::invalid_argument) && !fresh.valid(),
                  "8c: truncated experts.bin still refused at open");
        }
    }

    // ---- 7. fixture immutable byte-for-byte over the whole run --------------
    {
        check(file_contents_equal(g_fixture_path, g_full_data),
              "fixture file unchanged over whole run (bytes + length)");
        std::fprintf(stderr, "pread_calls=%d peak_mapping_bytes=%llu limit=%llu\n",
                     wrap::pread_calls,
                     (unsigned long long)wrap::peak_live_bytes,
                     (unsigned long long)wrap::kMappingLimit);
    }

    std::fprintf(stderr, "pool_expert_store_test: %d checks, %d FAILURES\n",
                 checks, failures);
    return failures == 0 ? 0 : 1;
}
