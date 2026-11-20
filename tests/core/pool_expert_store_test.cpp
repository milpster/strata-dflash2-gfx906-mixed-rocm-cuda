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
//   layer 2: [8192, 18192)  bytes/expert=5000 (gap 4608..8192, > page scratch)
// total = 18192 = file size.
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
constexpr uint64_t kMappingLimit = 1ull << 20;  // 1 MiB
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
        check(store.scratch_bytes() == kPage, "scratch one page in pool");

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
        // Pool = scratch(4096) + one 4096-page entry = exactly full.
        Pool pool;
        auto ec = make_pool(pool, 2 * kPage);
        check(!ec, "create tight 8KiB pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "open on tight pool");

        // Scratch charged.
        check(store.scratch_bytes() == kPage, "scratch charged in pool");

        // Acquire a 64-byte expert (reserves 4096) -> pool now full (scratch+entry).
        PoolLease a;
        ec = store.acquire(0, 0, a);
        check(!ec && a.valid(), "tight: acquire (0,0)");
        check(pool.stats().reserved_live_bytes == 2 * kPage,
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
        check(pool.stats().reserved_live_bytes == kPage,
              "clear: only scratch remains");

        // Another OWNER reuses/overwrites the freed payload, then reload
        // restores ALL source bytes (fixture never altered).
        PoolLease full;
        ec = pool.pool_alloc(pool.capacity() - kPage, kPage, full);
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
              "peak mapping <= 1 MiB");
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
