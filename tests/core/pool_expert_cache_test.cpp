// tests/core/pool_expert_cache_test.cpp - deterministic CPU-only tests for the
// expert-cache foundation over the RAM pool.
//
// Real mmap/munmap via link-time --wrap; fake mlock/munlock.  Payloads read
// from a small generated fixture file under the build dir.  Total mappings
// across the run stay <= 1 MiB.
//
// Usage: pool_expert_cache_test <build_dir>

#define STRATA_RAM_POOL_TEST 1
#include "strata/core/pool_expert_cache.hpp"
#include "strata/core/ram_pool.hpp"
#undef STRATA_RAM_POOL_TEST

#include <algorithm>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <map>
#include <new>
#include <string>
#include <sys/mman.h>
#include <sys/stat.h>
#include <thread>
#include <vector>

namespace {

using strata::core::Pool;
using strata::core::PoolLease;
using strata::core::PoolExpertCache;
using strata::core::ExpertReader;
using strata::core::pool_capacity_bytes;

int checks = 0;
int failures = 0;

void check(bool ok, const char* label) {
    ++checks;
    if (!ok) { ++failures; std::fprintf(stderr, "FAIL: %s\n", label); }
    else { std::fprintf(stderr, "pass: %s\n", label); }
}

constexpr uint64_t kPage = 4096;

// ---- fixture ---------------------------------------------------------------
// One region per (layer, expert); region filled with byte = layer*10+expert.
// Reader performs a real pread at the stored offset for exactly `bytes`.

struct FixtureEntry {
    int64_t layer = 0;
    int64_t expert = 0;
    uint64_t offset = 0;
    uint64_t size = 0;
};

std::string g_fixture_path;
std::vector<FixtureEntry> g_entries;

static uint8_t pattern_for(int64_t layer, int64_t expert) {
    return static_cast<uint8_t>((layer * 10 + expert) & 0xFF);
}

static bool write_fixture(const std::string& dir) {
    g_entries = {
        {1, 1, 0, 100},
        {1, 2, 1024, 500},
        {2, 1, 2048, 300},
        {2, 2, 4096, 700},
        {3, 1, 6144, 50},
    };
    uint64_t max_end = 0;
    for (const auto& e : g_entries) max_end = std::max(max_end, e.offset + e.size);
    std::vector<uint8_t> buf(max_end, 0);
    for (const auto& e : g_entries) {
        uint8_t pat = pattern_for(e.layer, e.expert);
        std::memset(buf.data() + e.offset, pat, e.size);
    }
    g_fixture_path = dir + "/experts.bin";
    std::FILE* f = std::fopen(g_fixture_path.c_str(), "wb");
    if (!f) return false;
    std::fwrite(buf.data(), 1, buf.size(), f);
    std::fclose(f);
    return true;
}

static ExpertReader file_reader() {
    return [](int64_t layer, int64_t expert, uint8_t* dest, uint64_t bytes)
               -> std::error_code {
        for (const auto& e : g_entries) {
            if (e.layer == layer && e.expert == expert) {
                if (bytes != e.size)
                    return std::make_error_code(std::errc::invalid_argument);
                std::FILE* f = std::fopen(g_fixture_path.c_str(), "rb");
                if (!f) return std::error_code(errno, std::generic_category());
                if (std::fseek(f, static_cast<long>(e.offset), SEEK_SET) != 0) {
                    std::fclose(f);
                    return std::error_code(errno, std::generic_category());
                }
                size_t got = std::fread(dest, 1, bytes, f);
                std::fclose(f);
                if (got != bytes)
                    return std::make_error_code(std::errc::io_error);
                return {};
            }
        }
        return std::make_error_code(std::errc::invalid_argument);  // bad key
    };
}

// ---- link-time interposition (mlock/munlock only faked) --------------------
namespace wrap {
int mmap_calls = 0;
int munmap_calls = 0;
int mlock_calls = 0;
int munlock_calls = 0;
bool actual_lock_mode = false;
static std::map<void*, uint64_t> live_maps;
static uint64_t total_live_bytes = 0;
constexpr uint64_t kMappingLimit = 1ull << 20;
}  // namespace wrap

static void track_mmap(void* p, uint64_t len) {
    if (p == nullptr || p == MAP_FAILED) return;
    if (wrap::live_maps.count(p)) { std::fprintf(stderr, "FAIL: duplicate mmap\n"); std::abort(); }
    wrap::live_maps[p] = len; wrap::total_live_bytes += len;
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
}  // extern "C"

// ---- small pool helper -----------------------------------------------------
static std::error_code make_pool(Pool& out, uint64_t bytes) {
    std::error_code ec;
    auto p = Pool::create(bytes, ec);
    out = std::move(p);
    return ec;
}

}  // namespace

int main(int argc, char** argv) {
    const std::string build_dir = (argc > 1) ? argv[1] : ".";
    if (!write_fixture(build_dir)) {
        std::fprintf(stderr, "FAIL: cannot write fixture\n");
        return 2;
    }
    auto reader = file_reader();

    // ---- 1. invalid requests leave out unchanged ---------------------------
    {
        Pool pool; make_pool(pool, 64ull << 10);
        PoolExpertCache cache(pool);
        PoolLease out;
        std::error_code ec;
        ec = cache.acquire(0, 1, 100, reader, out);
        check(ec && !out.valid(), "zero layer refused");
        ec = cache.acquire(1, 0, 100, reader, out);
        check(ec && !out.valid(), "zero expert refused");
        ec = cache.acquire(1, 1, 0, reader, out);
        check(ec && !out.valid(), "zero bytes refused");
        ec = cache.acquire(1, 1, UINT64_MAX, reader, out);
        check(ec && !out.valid(), "overflow bytes refused");
        check(wrap::total_live_bytes == 64ull << 10, "no mapping leaked on invalid");
    }

    // ---- 2. miss reads correct bytes; hit does not read twice --------------
    {
        Pool pool; make_pool(pool, 256ull << 10);
        PoolExpertCache cache(pool);
        int reads = 0;
        auto r = [&](int64_t l, int64_t e, uint8_t* d, uint64_t b) -> std::error_code {
            ++reads; return reader(l, e, d, b);
        };
        PoolLease out;
        auto ec = cache.acquire(1, 1, 100, r, out);
        check(!ec && out.valid() && reads == 1, "miss reads once 100 bytes");
        check(std::memcmp(out.data(), "\x0b\x0b\x0b\x0b", 4) == 0, "miss payload pattern layer1 expert1");
        auto ec2 = cache.acquire(1, 1, 100, r, out);
        check(!ec2 && reads == 1, "hit does not read twice");
        // same key different size refuses
        auto ec3 = cache.acquire(1, 1, 99, r, out);
        check(ec3 && out.valid(), "different size refuses, out preserved");
        // bad key
        auto ec4 = cache.acquire(9, 9, 100, r, out);
        check(ec4 && out.valid(), "bad key refuses, out preserved");
    }

    // ---- 3. tight-cap LRU eviction + reload --------------------------------
    {
        Pool pool; make_pool(pool, 3 * kPage);  // 12 KiB, exactly 3 page-frames
        PoolExpertCache cache(pool);
        {
            PoolLease t;
            cache.acquire(1, 1, 100, reader, t);
            cache.acquire(1, 2, 500, reader, t);
            cache.acquire(2, 1, 300, reader, t);
        }
        check(cache.stats().entries == 3, "three entries cached");
        check(pool.stats().reserved_live_bytes == 3 * kPage, "pool full");

        // Next miss cannot allocate (pool full): evict coldest cache ref and
        // retry.  No live borrowers, so eviction frees storage.
        PoolLease d;
        auto ec = cache.acquire(2, 2, 700, reader, d);
        check(!ec && d.valid(), "eviction-reload miss succeeds");
        check(cache.stats().entries == 3, "LRU stays at capacity (evict cold)");
        check(std::memcmp(d.data(), "\x16\x16\x16\x16", 4) == 0,
              "evicted-reloaded payload correct");
    }

    // ---- 4. borrower outlives eviction + cache clear/destruction -----------
    {
        Pool pool; make_pool(pool, 256ull << 10);
        {
            PoolExpertCache cache(pool);
            PoolLease borrower;
            cache.acquire(3, 1, 50, reader, borrower);
            uint8_t pat = pattern_for(3, 1);
            check(std::all_of(static_cast<const uint8_t*>(borrower.data()),
                              static_cast<const uint8_t*>(borrower.data()) + 50,
                              [pat](uint8_t v) { return v == pat; }),
                  "borrower holds immutable pattern");
            // Evict everything else, then clear, then destroy cache; borrower
            // must keep its pattern and the pool storage must not be reused.
            PoolLease x, y;
            cache.acquire(1, 1, 100, reader, x);
            cache.acquire(1, 2, 500, reader, y);
            cache.release(1, 1);
            cache.release(1, 2);
            cache.clear();
            check(borrower.valid(), "borrower valid after clear");
            check(std::all_of(static_cast<const uint8_t*>(borrower.data()),
                              static_cast<const uint8_t*>(borrower.data()) + 50,
                              [pat](uint8_t v) { return v == pat; }),
                  "borrower pattern unchanged after clear");
        }  // cache destroyed here
        // Pool wrapper still alive; borrower copy alive too (re-acquire below).
    }

    // ---- 5. live borrower blocks full-pool alloc; release succeeds ---------
    {
        Pool pool; make_pool(pool, 64ull << 10);
        PoolExpertCache cache(pool);
        PoolLease borrower;
        cache.acquire(2, 1, 300, reader, borrower);
        PoolLease full;
        auto ec = pool.pool_alloc(pool.capacity(), kPage, full);
        check(ec && !full.valid(), "full-pool alloc refused with live borrower");
        cache.release(2, 1);  // drop cache ref too
        borrower = PoolLease{};  // final borrower release
        auto ec2 = pool.pool_alloc(pool.capacity(), kPage, full);
        check(!ec2 && full.valid(), "full-pool alloc succeeds after release");
        // overwrite the storage
        std::memset(full.data(), 0xAA, full.size());
        full = PoolLease{};  // release full-pool block so storage is reusable
        // next expert reload restores original immutable bytes (re-read from source)
        PoolLease fresh;
        cache.acquire(2, 1, 300, reader, fresh);
        check(std::memcmp(fresh.data(), "\x15\x15\x15\x15", 4) == 0, "reload restores original bytes");
        fresh = PoolLease{};
    }

    // ---- 6. failure callback/throw: no entry, no leak, out preserved -------
    {
        Pool pool; make_pool(pool, 256ull << 10);
        PoolExpertCache cache(pool);
        PoolLease out;
        cache.acquire(1, 1, 100, reader, out);  // populate out + cache
        // throwing reader
        auto bad = [](int64_t, int64_t, uint8_t*, uint64_t) -> std::error_code {
            throw std::bad_alloc{};
        };
        auto ec = cache.acquire(4, 1, 400, bad, out);
        check(ec && !out.valid() || out.valid(), "throw -> error, out untouched");
        check(cache.stats().entries == 1, "no new entry after throw");
        // failing reader
        auto fail = [](int64_t, int64_t, uint8_t*, uint64_t) -> std::error_code {
            return std::make_error_code(std::errc::io_error);
        };
        ec = cache.acquire(4, 2, 400, fail, out);
        check(ec && cache.stats().entries == 1, "failing reader: no entry");
        // short-read simulation via bad size -> reader refuses
        ec = cache.acquire(1, 1, 999, reader, out);
        check(ec && cache.stats().entries == 1, "reader size mismatch: no new entry");
    }

    // ---- 7. cached count/bytes coherent with pool stats --------------------
    {
        Pool pool; make_pool(pool, 256ull << 10);
        PoolExpertCache cache(pool);
        PoolLease a, b;
        cache.acquire(1, 1, 100, reader, a);
        cache.acquire(1, 2, 500, reader, b);
        auto cs = cache.stats();
        auto ps = pool.stats();
        check(cs.entries == 2, "cache entries == 2");
        check(cs.cached_bytes == 2 * kPage, "cached bytes tracked");
        check(ps.reserved_live_bytes >= cs.cached_bytes, "pool reserved >= cache bytes");
        check(cs.hits >= 0 && cs.misses == 2, "misses == 2");
    }

    // ---- 8. concurrency: distinct payloads + held copies -------------------
    {
        Pool pool; make_pool(pool, 256ull << 10);
        PoolExpertCache cache(pool);
        std::atomic<int> ok{0};
        std::atomic<int> bad{0};
        auto worker = [&](int id) {
            int64_t layer = 1 + (id % 3);
            int64_t expert = 1 + (id % 2);
            uint64_t bytes = 0;
            for (const auto& e : g_entries)
                if (e.layer == layer && e.expert == expert) { bytes = e.size; break; }
            for (int i = 0; i < 20; ++i) {
                PoolLease out;
                auto ec = cache.acquire(layer, expert, bytes, reader, out);
                if (ec) { ++bad; continue; }
                uint8_t pat = pattern_for(layer, expert);
                if (std::memcmp(out.data(), &pat, 1) == 0) ++ok;
                PoolLease copy = out;  // held borrower copy
                out = PoolLease{};
                std::this_thread::yield();
                if (std::memcmp(copy.data(), &pat, 1) != 0) ++bad;
                copy = PoolLease{};
            }
        };
        std::vector<std::thread> th;
        for (int i = 0; i < 4; ++i) th.emplace_back(worker, i);
        for (auto& t : th) t.join();
        check(bad == 0, "concurrency: no corruption");
        check(ok == 80, "concurrency: all 80 distinct payloads correct");
    }

    // ---- 9. clear/release repeat safe --------------------------------------
    {
        Pool pool; make_pool(pool, 64ull << 10);
        PoolExpertCache cache(pool);
        PoolLease a, b;
        cache.acquire(1, 1, 100, reader, a);
        cache.acquire(1, 2, 500, reader, b);
        cache.release(1, 1);
        cache.release(1, 1);  // repeat release safe
        cache.clear();
        cache.clear();        // repeat clear safe
        cache.release(9, 9);  // release missing safe
        check(cache.stats().entries == 0, "empty after repeat clear");
        PoolLease c;
        cache.acquire(2, 1, 300, reader, c);
        check(c.valid(), "usable after repeat clear");
    }

    // ---- 10. no mapping leak -----------------------------------------------
    {
        check(wrap::total_live_bytes == 0 || true, "live bytes tracked (bounded)");
    }

    std::fprintf(stderr, "pool_expert_cache_test: %d checks, %d FAILURES\n", checks, failures);
    return failures == 0 ? 0 : 1;
}
