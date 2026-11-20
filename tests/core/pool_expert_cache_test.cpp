// tests/core/pool_expert_cache_test.cpp - deterministic CPU-only tests for the
// expert-cache foundation over the RAM pool.
//
// Real mmap/munmap via link-time --wrap; fake mlock/munlock.  Payloads read
// from a small generated fixture file under the build dir (unique name, never a
// production model).  Total mappings across the run stay <= 1 MiB.
//
// Usage: pool_expert_cache_test <build_dir>

#define STRATA_RAM_POOL_TEST 1
#include "strata/core/pool_expert_cache.hpp"
#include "strata/core/ram_pool.hpp"
#undef STRATA_RAM_POOL_TEST

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
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
using strata::core::ram_pool_set_metadata_fault;
using strata::core::cache_set_pre_emplace_fault;

int checks = 0;
int failures = 0;

void check(bool ok, const char* label) {
    ++checks;
    if (!ok) { ++failures; std::fprintf(stderr, "FAIL: %s\n", label); }
    else { std::fprintf(stderr, "pass: %s\n", label); }
}

constexpr uint64_t kPage = 4096;

// ---- fixture ---------------------------------------------------------------
// Deterministic nonuniform per-byte data keyed by global offset, so offset or
// order corruption cannot hide behind a uniform pattern.
// byte(p) = (p * 31 + 7) & 0xFF
static uint8_t byte_at(uint64_t p) { return static_cast<uint8_t>((p * 31u + 7u) & 0xFFu); }

struct FixtureEntry {
    int64_t layer = 0;
    int64_t expert = 0;
    uint64_t offset = 0;
    uint64_t size = 0;
};

std::string g_full_data;          // byte_at(p) for p in [0, max_end)
std::string g_fixture_path;       // complete fixture
std::string g_short_path;         // prefix ending 100 bytes into region (2,3)
std::vector<FixtureEntry> g_entries;

static bool write_file(const std::string& path, const std::string& data);

static bool build_fixture(const std::string& dir) {
    g_entries = {
        {0, 0, 0, 64},
        {0, 1, 4096, 128},
        {1, 0, 8192, 200},
        {2, 3, 12288, 500},
    };
    uint64_t max_end = 0;
    for (const auto& e : g_entries) max_end = std::max(max_end, e.offset + e.size);
    g_full_data.resize(max_end);
    for (uint64_t p = 0; p < max_end; ++p) g_full_data[p] = byte_at(p);

    g_fixture_path = dir + "/cache_fixture_20261120_testfinal.bin";
    if (!write_file(g_fixture_path, g_full_data)) return false;

    // Short file: region prefix ending 100 bytes INTO region (2,3) (offset
    // 12288 + 100), not 100 bytes total before the region.  Reading region
    // (2,3) (500 bytes at 12288) yields only 100 bytes -> short read.
    g_short_path = dir + "/cache_short_20261120_testfinal.bin";
    std::string short_data = g_full_data.substr(0, 12288 + 100);
    return write_file(g_short_path, short_data);
}

static bool write_file(const std::string& path, const std::string& data) {
    std::FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) return false;
    if (std::fwrite(data.data(), 1, data.size(), f) != data.size()) {
        std::fclose(f);
        return false;
    }
    if (std::fclose(f) != 0) return false;
    return true;
}

static bool region_matches(const void* data, uint64_t offset, uint64_t size) {
    if (size == 0) return true;
    if (offset + size > g_full_data.size()) return false;
    return std::memcmp(data, g_full_data.data() + offset, size) == 0;
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
    return got == expected;  // complete bytes + length
}

// ---- reader ----------------------------------------------------------------
static ExpertReader file_reader(const std::string& path) {
    return [path](int64_t layer, int64_t expert, uint8_t* dest, uint64_t bytes)
               -> std::error_code {
        for (const auto& e : g_entries) {
            if (e.layer == layer && e.expert == expert) {
                if (bytes != e.size)
                    return std::make_error_code(std::errc::invalid_argument);
                std::FILE* f = std::fopen(path.c_str(), "rb");
                if (!f) return std::error_code(errno, std::generic_category());
                if (std::fseek(f, static_cast<long>(e.offset), SEEK_SET) != 0) {
                    std::fclose(f);
                    return std::error_code(errno, std::generic_category());
                }
                size_t got = std::fread(dest, 1, bytes, f);
                std::fclose(f);
                if (got != bytes)
                    return std::make_error_code(std::errc::io_error);  // short read
                return {};
            }
        }
        return std::make_error_code(std::errc::invalid_argument);  // bad key
    };
}

// ---- link-time interposition (mlock/munlock faked; mmap/munmap real+counted)--
namespace wrap {
int mmap_calls = 0;
int munmap_calls = 0;
int mlock_calls = 0;
int munlock_calls = 0;
static std::map<void*, uint64_t> live_maps;
static uint64_t total_live_bytes = 0;
static uint64_t peak_live_bytes = 0;
constexpr uint64_t kMappingLimit = 1ull << 20;
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

// File-scope state for the ram_pool metadata-fault bridge.
static int s_meta_calls = 0;
static int s_meta_throw_on = 0;
static void bridge() {
    ++s_meta_calls;
    if (s_meta_calls == s_meta_throw_on) throw std::bad_alloc{};
}

// ---- helpers ----------------------------------------------------------------
static std::error_code make_pool(Pool& out, uint64_t bytes) {
    std::error_code ec;
    auto p = Pool::create(bytes, ec);
    out = std::move(p);
    return ec;
}

}  // namespace

int main(int argc, char** argv) {
    const std::string build_dir = (argc > 1) ? argv[1] : ".";
    if (!build_fixture(build_dir)) {
        std::fprintf(stderr, "FAIL: cannot write fixture\n");
        return 2;
    }
    auto reader = file_reader(g_fixture_path);

    // ---- 1. invalid keys/bytes leave out and cache unchanged ---------------
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "create 256KiB pool");
        PoolExpertCache cache(pool);
        PoolLease out;
        ec = cache.acquire(0, 0, 64, reader, out);
        check(!ec && out.valid(), "warm (0,0)");
        const auto entries_before = cache.stats().entries;
        const auto out_ptr = out.data();
        const auto out_size = out.size();

        auto try_invalid = [&](int64_t l, int64_t e, uint64_t b,
                               const char* label) {
            PoolLease o = out;  // populated copy
            auto ec2 = cache.acquire(l, e, b, reader, o);
            check(ec2 == std::make_error_code(std::errc::invalid_argument), label);
            check(o.data() == out_ptr && o.size() == out_size, "out preserved");
            check(cache.stats().entries == entries_before, label);
        };
        // NEW keys for oversize so cached-size mismatch cannot bypass preflight.
        try_invalid(-1, 0, 64, "negative layer");
        try_invalid(0, -1, 64, "negative expert");
        try_invalid(0, 0, 0, "zero bytes");
        try_invalid(9, 9, UINT64_MAX, "UINT64_MAX bytes new key");
        try_invalid(9, 9, pool.capacity() + 1, "pool.capacity()+1 new key");

        check(cache.stats().entries == entries_before, "invalid requests: no eviction");
        check(out.data() == out_ptr && out.size() == out_size, "original out untouched");
        out = PoolLease{};
    }

    // ---- 2. miss reads full nonuniform bytes; hit does not read twice ------
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "create 256KiB pool");
        PoolExpertCache cache(pool);
        int reads = 0;
        auto r = [&](int64_t l, int64_t e, uint8_t* d, uint64_t b) -> std::error_code {
            ++reads; return reader(l, e, d, b);
        };
        PoolLease out;
        ec = cache.acquire(2, 3, 500, r, out);
        check(!ec && out.valid(), "miss (2,3) 500 bytes");
        check(reads == 1, "miss reads once");
        check(region_matches(out.data(), 12288, 500), "(2,3) full nonuniform payload");

        const void* hit_ptr = out.data();
        const uint64_t hit_size = out.size();
        auto ec2 = cache.acquire(2, 3, 500, r, out);
        check(!ec2 && reads == 1, "hit does not read twice");
        check(out.data() == hit_ptr && out.size() == hit_size, "hit returns same lease bytes");
        check(cache.stats().hits == 1, "hit counted");
        check(cache.stats().misses == 1, "miss counted once");
        out = PoolLease{};
    }

    // ---- 3. same key different size refuses, out preserved -----------------
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "create pool");
        PoolExpertCache cache(pool);
        PoolLease out;
        ec = cache.acquire(0, 1, 128, reader, out);
        check(!ec && out.valid(), "warm (0,1)");
        const void* ptr = out.data();
        const uint64_t sz = out.size();
        ec = cache.acquire(0, 1, 127, reader, out);
        check(ec == std::make_error_code(std::errc::invalid_argument), "diff size refused");
        check(out.data() == ptr && out.size() == sz, "diff size: out pointer+bytes preserved");
        check(cache.stats().entries == 1, "diff size: no purge");
        out = PoolLease{};
    }

    // ---- 4. bad key refuses, out preserved ---------------------------------
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "create pool");
        PoolExpertCache cache(pool);
        PoolLease out;
        ec = cache.acquire(0, 0, 64, reader, out);
        check(!ec && out.valid(), "warm (0,0)");
        const void* ptr = out.data();
        const uint64_t sz = out.size();
        ec = cache.acquire(9, 9, 64, reader, out);
        check(ec == std::make_error_code(std::errc::invalid_argument), "bad key refused");
        check(out.data() == ptr && out.size() == sz, "bad key: out preserved");
        check(cache.stats().entries == 1, "bad key: no purge");
        out = PoolLease{};
    }

    // ---- 5. tight-cap LRU: hit A, missD evicts B, reload B = 2nd read ------
    // Sequence: warm A,B,C (cache owns, no borrowers). LRU tail=A.
    // HIT A -> [A,C,B], tail=B. missD evicts B -> [D,A,C].
    // reacquire A -> hit. reload B -> evict C -> [B,A,D], readsB==2.
    {
        Pool pool;
        auto ec = make_pool(pool, 3 * kPage);
        check(!ec, "create 12KiB pool");
        PoolExpertCache cache(pool);
        std::map<int64_t, int> reads;
        auto key_of = [](int64_t l, int64_t e) { return l * 10000000LL + e; };
        auto rc = [&](int64_t l, int64_t e, uint8_t* d, uint64_t b)
                      -> std::error_code {
            ++reads[key_of(l, e)];
            return reader(l, e, d, b);
        };
        PoolLease t;
        ec = cache.acquire(0, 0, 64, rc, t);  // A
        check(!ec, "warm A"); t = PoolLease{};
        ec = cache.acquire(0, 1, 128, rc, t); // B
        check(!ec, "warm B"); t = PoolLease{};
        ec = cache.acquire(1, 0, 200, rc, t); // C
        check(!ec, "warm C"); t = PoolLease{};
        check(cache.stats().entries == 3, "three entries cached");
        check(pool.stats().reserved_live_bytes == 3 * kPage, "pool full");

        // HIT A: readsA stays 1, no read.
        PoolLease a;
        ec = cache.acquire(0, 0, 64, rc, a);
        check(!ec, "hit A");
        check(reads[key_of(0, 0)] == 1, "hit A: readsA stays 1");
        check(cache.stats().hits == 1, "hit A counted");

        // missD evicts B (tail after hit A).
        PoolLease d;
        ec = cache.acquire(2, 3, 500, rc, d);
        check(!ec && d.valid(), "miss D evicts B");
        check(reads[key_of(2, 3)] == 1, "D read once");
        check(cache.stats().entries == 3, "LRU stays at capacity");
        check(cache.stats().evictions == 1, "one eviction (B)");
        check(region_matches(d.data(), 12288, 500), "D full payload");

        // reacquire A: still a hit (A retained).
        ec = cache.acquire(0, 0, 64, rc, a);
        check(!ec, "reacquire A hit");
        check(reads[key_of(0, 0)] == 1, "reacquire A: still hit, no new read");
        check(cache.stats().hits == 2, "second hit counted");

        // reload B: evicts C (now tail), B read a second time, payload correct.
        PoolLease b;
        ec = cache.acquire(0, 1, 128, rc, b);
        check(!ec && b.valid(), "reload B evicts C");
        check(reads[key_of(0, 1)] == 2, "readsB becomes 2");
        check(cache.stats().evictions == 2, "second eviction (C)");
        check(region_matches(b.data(), 4096, 128), "B full nonuniform payload");
    }

    // ---- 6. borrower outlives cache + Pool scopes --------------------------
    {
        PoolLease borrower;
        {
            Pool pool;
            auto ec = make_pool(pool, 64ull << 10);
            check(!ec, "create 64KiB pool");
            PoolExpertCache cache(pool);
            ec = cache.acquire(1, 0, 200, reader, borrower);
            check(!ec && borrower.valid(), "acquire borrower (1,0)");
            // cache and pool destroyed at scope end while borrower alive
        }
        check(borrower.valid(), "borrower valid after cache+pool destroyed");
        check(region_matches(borrower.data(), 8192, 200),
              "borrower payload intact after owner scopes gone");
        check(wrap::total_live_bytes == 64ull << 10,
              "pool mapping alive while borrower holds lease");
        borrower = PoolLease{};  // final release -> exact last unmap
        check(wrap::munmap_calls == wrap::mmap_calls, "every mmap matched by munmap");
        check(wrap::total_live_bytes == 0, "all mappings released");
    }

    // ---- 7. live borrower blocks full-pool alloc; release -> same range -----
    {
        Pool pool;
        auto ec = make_pool(pool, 64ull << 10);
        check(!ec, "create 64KiB pool");
        PoolExpertCache cache(pool);
        PoolLease borrower;
        ec = cache.acquire(2, 3, 500, reader, borrower);
        check(!ec && borrower.valid(), "acquire borrower");
        const void* borrower_ptr = borrower.data();
        PoolLease full;
        ec = pool.pool_alloc(pool.capacity(), kPage, full);
        check(ec && !full.valid(), "full-pool alloc refused with live borrower");

        cache.release(2, 3);  // drop cache ref, borrower alive
        ec = pool.pool_alloc(pool.capacity(), kPage, full);
        check(ec && !full.valid(), "full-pool still refused with borrower alive");

        borrower = PoolLease{};  // final borrower release
        ec = pool.pool_alloc(pool.capacity(), kPage, full);
        check(!ec && full.valid(), "full-pool alloc succeeds after release");
        check(full.data() == borrower_ptr, "full-pool reuses same address");

        std::memset(full.data(), 0xAA, full.size());
        full = PoolLease{};  // free storage so cache can reload

        PoolLease fresh;
        ec = cache.acquire(2, 3, 500, reader, fresh);
        check(!ec && fresh.valid(), "reread original key after overwrite");
        check(region_matches(fresh.data(), 12288, 500),
              "reread restores ALL original bytes from fixture");
    }

    // ---- 8. throwing reader preserves exact output + no leaked allocation --
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "create pool");
        PoolExpertCache cache(pool);
        PoolLease out;
        ec = cache.acquire(0, 0, 64, reader, out);
        check(!ec, "warm (0,0)");
        const auto entries_before = cache.stats().entries;
        const auto reserved_before = pool.stats().reserved_live_bytes;
        const void* ptr = out.data();
        const uint64_t sz = out.size();
        int reader_calls = 0;
        auto throwing = [&](int64_t, int64_t, uint8_t*, uint64_t) -> std::error_code {
            ++reader_calls;
            throw std::bad_alloc{};
        };
        ec = cache.acquire(3, 3, 400, throwing, out);
        check(ec == std::make_error_code(std::errc::not_enough_memory),
              "throw -> not_enough_memory");
        check(reader_calls == 1, "throwing reader was called and threw");
        check(cache.stats().entries == entries_before, "throw: no new entry");
        check(cache.stats().entries == entries_before, "throw: no eviction");
        check(pool.stats().reserved_live_bytes == reserved_before,
              "throw: no leaked reserved allocation");
        check(out.data() == ptr && out.size() == sz, "throw: out preserved");
        out = PoolLease{};
    }

    // ---- 9. failing reader (short read): no entry published ----------------
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "create pool");
        PoolExpertCache cache(pool);
        auto short_reader = file_reader(g_short_path);  // ends 100 bytes into (2,3)
        PoolLease out;
        ec = cache.acquire(2, 3, 500, short_reader, out);
        check(ec == std::make_error_code(std::errc::io_error), "short read -> io_error");
        check(!out.valid(), "short read: no entry published");
        check(cache.stats().entries == 0, "short read: cache empty");
        out = PoolLease{};
    }

    // ---- 10. ram_pool metadata fault must NOT evict cache refs ------------
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "create pool");
        PoolExpertCache cache(pool);
        PoolLease out;
        ec = cache.acquire(0, 0, 64, reader, out);  // warm + populated out
        check(!ec && out.valid(), "warm (0,0) populated");
        const auto entries_before = cache.stats().entries;
        const auto reserved_before = pool.stats().reserved_live_bytes;
        const void* ptr = out.data();
        const uint64_t sz = out.size();

        s_meta_calls = 0;
        s_meta_throw_on = 1;
        ram_pool_set_metadata_fault(bridge);
        PoolLease o;
        ec = cache.acquire(0, 1, 128, reader, o);  // new key, pool_alloc metadata fault
        ram_pool_set_metadata_fault(nullptr);
        check(ec == std::make_error_code(std::errc::not_enough_memory),
              "metadata fault -> not_enough_memory");
        check(!o.valid(), "metadata fault: new-key output not populated");
        check(cache.stats().entries == entries_before, "metadata fault: no eviction");
        check(pool.stats().reserved_live_bytes == reserved_before,
              "metadata fault: pool reserved unchanged");
        check(out.data() == ptr && out.size() == sz, "metadata fault: populated out preserved");

        // release existing entry + clear with hook set: hook never invoked,
        // full coalesce once borrower released.
        s_meta_calls = 0;
        s_meta_throw_on = 1;
        ram_pool_set_metadata_fault(bridge);
        cache.release(0, 0);
        check(s_meta_calls == 0, "release does not invoke metadata hook");
        cache.clear();
        check(s_meta_calls == 0, "clear does not invoke metadata hook");
        ram_pool_set_metadata_fault(nullptr);
        out = PoolLease{};
        check(cache.stats().entries == 0, "cache empty after clear");
        check(pool.stats().free_bytes == pool.stats().capacity_bytes,
              "full coalesce: free == capacity");
    }

    // ---- 11. pre-emplace fault on TIGHT cache: before eviction/read --------
    {
        Pool pool;
        auto ec = make_pool(pool, 3 * kPage);
        check(!ec, "create 12KiB pool");
        PoolExpertCache cache(pool);
        PoolLease t;
        ec = cache.acquire(0, 0, 64, reader, t); check(!ec, "warm A"); t = PoolLease{};
        ec = cache.acquire(0, 1, 128, reader, t); check(!ec, "warm B"); t = PoolLease{};
        ec = cache.acquire(1, 0, 200, reader, t); check(!ec, "warm C"); t = PoolLease{};
        check(cache.stats().entries == 3, "tight cache full");
        check(pool.stats().reserved_live_bytes == 3 * kPage, "pool full");

        PoolLease aout;
        ec = cache.acquire(0, 0, 64, reader, aout);
        check(!ec && aout.valid(), "capture populated output on A");
        const auto entries_before = cache.stats().entries;
        const auto reserved_before = pool.stats().reserved_live_bytes;
        const void* ptr = aout.data();
        const uint64_t sz = aout.size();

        int reader_calls = 0;
        auto counting = [&](int64_t, int64_t, uint8_t*, uint64_t) -> std::error_code {
            ++reader_calls;
            return std::make_error_code(std::errc::io_error);  // never reached
        };
        auto hook = []() { throw std::bad_alloc{}; };
        cache_set_pre_emplace_fault(hook);
        PoolLease o;
        ec = cache.acquire(2, 3, 500, counting, o);  // NEW key -> pending fault
        cache_set_pre_emplace_fault(nullptr);
        check(ec == std::make_error_code(std::errc::not_enough_memory),
              "pre-emplace fault -> not_enough_memory");
        check(reader_calls == 0, "pre-emplace fault: reader not called");
        check(!o.valid(), "pre-emplace fault: no new entry");
        check(cache.stats().entries == entries_before, "pre-emplace: no eviction");
        check(pool.stats().reserved_live_bytes == reserved_before,
              "pre-emplace: no pool allocation");
        check(aout.data() == ptr && aout.size() == sz, "pre-emplace: captured output preserved");
        aout = PoolLease{};
    }

    // ---- 12. concurrency: 80/80 full nonuniform borrower data --------------
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "create pool");
        PoolExpertCache cache(pool);
        std::atomic<int> ok{0};
        std::atomic<int> bad{0};
        auto worker = [&](int id) {
            const FixtureEntry& fe = g_entries[id % g_entries.size()];
            for (int i = 0; i < 20; ++i) {
                PoolLease out;
                auto ec2 = cache.acquire(fe.layer, fe.expert, fe.size, reader, out);
                if (ec2) { ++bad; continue; }
                bool full = region_matches(out.data(), fe.offset, fe.size);
                if (full) ++ok; else ++bad;
                PoolLease copy = out;  // held borrower copy
                out = PoolLease{};
                std::this_thread::yield();
                bool copyok = region_matches(copy.data(), fe.offset, fe.size);
                if (!copyok) ++bad;
                copy = PoolLease{};
            }
        };
        std::vector<std::thread> th;
        for (int i = 0; i < 4; ++i) th.emplace_back(worker, i);
        for (auto& t : th) t.join();
        check(bad == 0, "concurrency: no corruption / partial reads");
        check(ok == 80, "concurrency: all 80 full payloads correct");

        cache.clear();
        auto sc = pool.stats();
        check(sc.alloc_count == 0, "concurrency: zero live allocations after clear");
        check(sc.free_bytes == sc.capacity_bytes, "concurrency: full free restored");
    }

    // ---- 13. clear/release repeat safe -------------------------------------
    {
        Pool pool;
        auto ec = make_pool(pool, 64ull << 10);
        check(!ec, "create pool");
        PoolExpertCache cache(pool);
        PoolLease a, b;
        ec = cache.acquire(1, 0, 200, reader, a);
        check(!ec, "acquire a");
        ec = cache.acquire(0, 1, 128, reader, b);
        check(!ec, "acquire b");
        cache.release(1, 0);
        cache.release(1, 0);  // repeat release safe
        cache.clear();
        cache.clear();        // repeat clear safe
        cache.release(9, 9);  // release missing safe
        check(cache.stats().entries == 0, "empty after repeat clear");
        PoolLease c;
        ec = cache.acquire(2, 3, 500, reader, c);
        check(!ec && c.valid(), "usable after repeat clear");
    }

    // ---- 14. fixture immutable byte-for-byte + mapping limits --------------
    {
        check(file_contents_equal(g_fixture_path, g_full_data),
              "fixture file unchanged at end (bytes + length)");
        std::fprintf(stderr, "peak_mapping_bytes=%llu limit=%llu\n",
                     (unsigned long long)wrap::peak_live_bytes,
                     (unsigned long long)wrap::kMappingLimit);
        check(wrap::peak_live_bytes <= wrap::kMappingLimit, "peak <= 1 MiB");
    }

    // ---- final: every mmap matched, no live mappings ------------------------
    check(wrap::live_maps.empty(), "final: no live mappings");
    check(wrap::total_live_bytes == 0, "final: zero live bytes");
    check(wrap::mmap_calls == wrap::munmap_calls,
          "final: mmap_calls == munmap_calls");

    std::fprintf(stderr, "pool_expert_cache_test: %d checks, %d FAILURES\n",
                 checks, failures);
    return failures == 0 ? 0 : 1;
}
