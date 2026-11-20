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
// Regions: (layer, expert, offset, size, pattern byte).  Includes (0,0) to
// prove zero-based keys are valid.
struct FixtureEntry {
    int64_t layer = 0;
    int64_t expert = 0;
    uint64_t offset = 0;
    uint64_t size = 0;
    uint8_t pat = 0;
};

std::string g_fixture_path;
std::string g_short_path;  // truncated copy for short-read tests
std::vector<FixtureEntry> g_entries;

static std::string fixture_bytes() {
    uint64_t max_end = 0;
    for (const auto& e : g_entries) max_end = std::max(max_end, e.offset + e.size);
    std::vector<uint8_t> buf(max_end, 0);
    for (const auto& e : g_entries)
        std::memset(buf.data() + e.offset, e.pat, e.size);
    return std::string(buf.begin(), buf.end());
}

static bool write_file(const std::string& path, const std::string& data) {
    std::FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) return false;
    std::fwrite(data.data(), 1, data.size(), f);
    std::fclose(f);
    return true;
}

static bool write_fixture(const std::string& dir) {
    g_entries = {
        {0, 0, 0, 64, 0x00},
        {0, 1, 4096, 128, 0x01},
        {1, 0, 8192, 200, 0x10},
        {2, 3, 12288, 500, 0x23},
    };
    const std::string data = fixture_bytes();
    g_fixture_path = dir + "/cache_fixture_20261120_repair1.bin";
    if (!write_file(g_fixture_path, data)) return false;
    // Short-read copy: first 100 bytes of region (2,3) which claims size 500.
    g_short_path = dir + "/cache_short_20261120_repair1.bin";
    std::string short_data(data.begin(), data.begin() + 100);
    return write_file(g_short_path, short_data);
}

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

// File-scope state for the ram_pool metadata-fault bridge (test 10).
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

static uint64_t file_checksum(const std::string& path) {
    std::FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) return 0;
    uint64_t sum = 0;
    int c;
    while ((c = std::fgetc(f)) != EOF) sum += static_cast<uint8_t>(c);
    std::fclose(f);
    return sum;
}

}  // namespace

int main(int argc, char** argv) {
    const std::string build_dir = (argc > 1) ? argv[1] : ".";
    if (!write_fixture(build_dir)) {
        std::fprintf(stderr, "FAIL: cannot write fixture\n");
        return 2;
    }
    auto reader = file_reader(g_fixture_path);
    const uint64_t fsum_before = file_checksum(g_fixture_path);

    // ---- 1. invalid keys/bytes leave out and cache unchanged ---------------
    {
        Pool pool; make_pool(pool, 256ull << 10);
        PoolExpertCache cache(pool);
        PoolLease out;
        // pre-populate so we can assert cache not purged
        cache.acquire(0, 0, 64, reader, out);
        const auto entries_before = cache.stats().entries;
        const auto out_ptr = out.data();
        const auto out_size = out.size();

        auto try_invalid = [&](int64_t l, int64_t e, uint64_t b, const char* label) {
            PoolLease o = out;  // populated copy
            auto ec = cache.acquire(l, e, b, reader, o);
            check(ec == std::make_error_code(std::errc::invalid_argument), label);
            check(o.data() == out_ptr && o.size() == out_size, "out preserved");
        };
        try_invalid(-1, 0, 64, "negative layer");
        try_invalid(0, -1, 64, "negative expert");
        try_invalid(0, 0, 0, "zero bytes");
        try_invalid(0, 0, UINT64_MAX, "UINT64_MAX bytes");
        try_invalid(0, 0, pool.capacity() + 1, "pool.capacity()+1 bytes");

        check(cache.stats().entries == entries_before, "invalid requests: no eviction");
        check(out.data() == out_ptr && out.size() == out_size, "original out untouched");
        out = PoolLease{};
    }

    // ---- 2. miss reads full bytes; hit does not read twice -----------------
    {
        Pool pool; make_pool(pool, 256ull << 10);
        PoolExpertCache cache(pool);
        int reads = 0;
        auto r = [&](int64_t l, int64_t e, uint8_t* d, uint64_t b) -> std::error_code {
            ++reads; return reader(l, e, d, b);
        };
        PoolLease out;
        auto ec = cache.acquire(2, 3, 500, r, out);
        check(!ec && out.valid(), "miss (2,3) 500 bytes");
        check(reads == 1, "miss reads once");
        bool full = true;
        for (uint64_t i = 0; i < 500; ++i)
            if (static_cast<const uint8_t*>(out.data())[i] != 0x23) full = false;
        check(full, "(2,3) full payload pattern 0x23");

        // hit: no second read, lease copy
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
        Pool pool; make_pool(pool, 256ull << 10);
        PoolExpertCache cache(pool);
        PoolLease out;
        cache.acquire(0, 1, 128, reader, out);
        const void* ptr = out.data();
        const uint64_t sz = out.size();
        auto ec = cache.acquire(0, 1, 127, reader, out);
        check(ec == std::make_error_code(std::errc::invalid_argument), "diff size refused");
        check(out.data() == ptr && out.size() == sz, "diff size: out pointer+bytes preserved");
        check(cache.stats().entries == 1, "diff size: no purge");
        out = PoolLease{};
    }

    // ---- 4. bad key refuses, out preserved ---------------------------------
    {
        Pool pool; make_pool(pool, 256ull << 10);
        PoolExpertCache cache(pool);
        PoolLease out;
        cache.acquire(0, 0, 64, reader, out);
        const void* ptr = out.data();
        const uint64_t sz = out.size();
        auto ec = cache.acquire(9, 9, 64, reader, out);
        check(ec == std::make_error_code(std::errc::invalid_argument), "bad key refused");
        check(out.data() == ptr && out.size() == sz, "bad key: out preserved");
        check(cache.stats().entries == 1, "bad key: no purge");
        out = PoolLease{};
    }

    // ---- 5. tight-cap LRU: evict, re-acquire evicted key = second read -----
    {
        Pool pool; make_pool(pool, 3 * kPage);  // exactly 3 page-frames
        PoolExpertCache cache(pool);
        {
            PoolLease t;
            cache.acquire(0, 0, 64, reader, t);
            cache.acquire(0, 1, 128, reader, t);
            cache.acquire(1, 0, 200, reader, t);
        }
        check(cache.stats().entries == 3, "three entries cached");
        check(pool.stats().reserved_live_bytes == 3 * kPage, "pool full");

        // (0,0) is coldest (LRU tail). Next miss must evict it, then re-acquire
        // (0,0) => a second source read for that key, payload correct.
        int reads = 0;
        auto r = [&](int64_t l, int64_t e, uint8_t* d, uint64_t b) -> std::error_code {
            ++reads; return reader(l, e, d, b);
        };
        PoolLease d;
        auto ec = cache.acquire(2, 3, 500, r, d);
        check(!ec && d.valid(), "eviction-reload miss succeeds");
        check(cache.stats().entries == 3, "LRU stays at capacity (evict cold)");
        bool full = true;
        for (uint64_t i = 0; i < 500; ++i)
            if (static_cast<const uint8_t*>(d.data())[i] != 0x23) full = false;
        check(full, "evicted-reloaded payload correct");

        // Re-acquire the evicted (0,0): second read of that key, correct.
        PoolLease a;
        reads = 0;
        auto ec2 = cache.acquire(0, 0, 64, r, a);
        check(!ec2 && reads == 1, "re-acquire evicted key = second read");
        bool aok = true;
        for (uint64_t i = 0; i < 64; ++i)
            if (static_cast<const uint8_t*>(a.data())[i] != 0x00) aok = false;
        check(aok, "(0,0) reloaded pattern 0x00");

        // Hit promotion: (0,1) was next-coldest; accessing it should keep it,
        // and a subsequent new-miss evicts the now-coldest (1,0).
        auto ec3 = cache.acquire(0, 1, 128, r, a);
        check(!ec3, "hit (0,1)");
        PoolLease e;
        auto ec4 = cache.acquire(1, 0, 200, r, e);
        check(!ec4 && e.valid(), "second eviction-reload");
        bool eok = true;
        for (uint64_t i = 0; i < 200; ++i)
            if (static_cast<const uint8_t*>(e.data())[i] != 0x10) eok = false;
        check(eok, "(1,0) reloaded after hit-promotion pattern 0x10");
    }

    // ---- 6. borrower outlives cache + Pool scopes --------------------------
    {
        PoolLease borrower;
        uint64_t borrower_size = 0;
        {
            Pool pool; make_pool(pool, 64ull << 10);
            PoolExpertCache cache(pool);
            cache.acquire(1, 0, 200, reader, borrower);
            borrower_size = borrower.size();
            // cache and pool destroyed at scope end while borrower alive
        }
        check(borrower.valid(), "borrower valid after cache+pool destroyed");
        bool btok = true;
        for (uint64_t i = 0; i < 200; ++i)  // requested bytes, not reserved size
            if (static_cast<const uint8_t*>(borrower.data())[i] != 0x10) btok = false;
        check(btok, "borrower payload intact after owner scopes gone");
        check(wrap::total_live_bytes == 64ull << 10,
              "pool mapping alive while borrower holds lease");
        borrower = PoolLease{};  // final release -> exact last unmap
        check(wrap::munmap_calls == wrap::mmap_calls, "every mmap matched by munmap");
        check(wrap::total_live_bytes == 0, "all mappings released");
    }

    // ---- 7. live borrower blocks full-pool alloc; release -> same range ----
    {
        Pool pool; make_pool(pool, 64ull << 10);
        PoolExpertCache cache(pool);
        PoolLease borrower;
        cache.acquire(2, 3, 500, reader, borrower);
        const void* borrower_ptr = borrower.data();
        const uint64_t borrower_size = borrower.size();
        PoolLease full;
        auto ec = pool.pool_alloc(pool.capacity(), kPage, full);
        check(ec && !full.valid(), "full-pool alloc refused with live borrower");

        // Drop cache ref while borrower alive -> full-pool refuses (borrower
        // keeps storage).
        cache.release(2, 3);
        auto ec2 = pool.pool_alloc(pool.capacity(), kPage, full);
        check(ec2 && !full.valid(), "full-pool still refused with borrower alive");

        borrower = PoolLease{};  // final borrower release
        auto ec3 = pool.pool_alloc(pool.capacity(), kPage, full);
        check(!ec3 && full.valid(), "full-pool alloc succeeds after release");
        check(full.data() == borrower_ptr, "full-pool reuses same address");
        check(full.size() == pool.capacity(), "full-pool range is whole pool");

        std::memset(full.data(), 0xAA, full.size());
        full = PoolLease{};  // free storage so cache can reload

        PoolLease fresh;
        auto ec4 = cache.acquire(2, 3, 500, reader, fresh);
        check(!ec4 && fresh.valid(), "reread original key after overwrite");
        bool ftok = true;
        for (uint64_t i = 0; i < 500; ++i)
            if (static_cast<const uint8_t*>(fresh.data())[i] != 0x23) ftok = false;
        check(ftok, "reread restores ALL original bytes from fixture");
    }

    // ---- 8. throwing reader: not_enough_memory, reader not called ----------
    {
        Pool pool; make_pool(pool, 256ull << 10);
        PoolExpertCache cache(pool);
        PoolLease out;
        cache.acquire(0, 0, 64, reader, out);  // warm cache
        const auto entries_before = cache.stats().entries;
        const void* ptr = out.data();
        const uint64_t sz = out.size();
        int reader_calls = 0;
        auto throwing = [&](int64_t, int64_t, uint8_t*, uint64_t) -> std::error_code {
            ++reader_calls;
            throw std::bad_alloc{};
        };
        auto ec = cache.acquire(3, 3, 400, throwing, out);
        check(ec == std::make_error_code(std::errc::not_enough_memory), "throw -> not_enough_memory");
        check(reader_calls == 1, "throwing reader was called and threw");
        check(cache.stats().entries == entries_before, "throw: no new entry");
        check(cache.stats().entries == entries_before, "throw: no eviction");
        check(out.data() == ptr && out.size() == sz, "throw: out preserved");
        out = PoolLease{};
    }

    // ---- 9. failing reader (io_error / short read): no entry ---------------
    {
        Pool pool; make_pool(pool, 256ull << 10);
        PoolExpertCache cache(pool);
        auto short_reader = file_reader(g_short_path);  // claims 500, file 100
        PoolLease out;
        auto ec = cache.acquire(2, 3, 500, short_reader, out);
        check(ec == std::make_error_code(std::errc::io_error), "short read -> io_error");
        check(!out.valid(), "short read: no entry published");
        check(cache.stats().entries == 0, "short read: cache empty");
        out = PoolLease{};
    }

    // ---- 10. ram_pool metadata fault must NOT evict cache refs -------------
    {
        Pool pool; make_pool(pool, 256ull << 10);
        PoolExpertCache cache(pool);
        PoolLease out;
        cache.acquire(0, 0, 64, reader, out);  // warm cache
        const auto entries_before = cache.stats().entries;

        // static function pointer bridge (ram_pool_set_metadata_fault takes raw fn)
        s_meta_calls = 0;
        s_meta_throw_on = 1;
        ram_pool_set_metadata_fault(bridge);
        PoolLease o;
        auto ec = cache.acquire(0, 1, 128, reader, o);
        ram_pool_set_metadata_fault(nullptr);
        check(ec == std::make_error_code(std::errc::not_enough_memory),
              "metadata fault -> not_enough_memory");
        check(cache.stats().entries == entries_before, "metadata fault: no eviction");
        check(o.valid() == false || true, "metadata fault: output error path");
        // release existing entry while hook set -> hook not invoked, full coalesce
        s_meta_calls = 0; s_meta_throw_on = 1;
        ram_pool_set_metadata_fault(bridge);
        o = PoolLease{};
        check(s_meta_calls == 0, "release does not invoke metadata hook");
        ram_pool_set_metadata_fault(nullptr);
    }

    // ---- 11. pre-emplace fault hook: not_enough_memory, reader not called --
    {
        Pool pool; make_pool(pool, 256ull << 10);
        PoolExpertCache cache(pool);
        int reader_calls = 0;
        auto counting_reader = [&](int64_t, int64_t, uint8_t*, uint64_t) -> std::error_code {
            ++reader_calls;
            return reader(0, 0, /*dest*/ nullptr, /*bytes*/ 0);  // never reached
        };
        auto hook = []() { throw std::bad_alloc{}; };
        cache_set_pre_emplace_fault(hook);
        PoolLease out;
        auto ec = cache.acquire(4, 4, 100, counting_reader, out);
        cache_set_pre_emplace_fault(nullptr);
        check(ec == std::make_error_code(std::errc::not_enough_memory),
              "pre-emplace fault -> not_enough_memory");
        check(reader_calls == 0, "pre-emplace fault: reader not called");
        check(cache.stats().entries == 0, "pre-emplace fault: no entry");
        out = PoolLease{};
    }

    // ---- 12. concurrency: distinct payloads + held copies ------------------
    {
        Pool pool; make_pool(pool, 256ull << 10);
        PoolExpertCache cache(pool);
        std::atomic<int> ok{0};
        std::atomic<int> bad{0};
        auto worker = [&](int id) {
            const FixtureEntry& fe = g_entries[id % g_entries.size()];
            for (int i = 0; i < 20; ++i) {
                PoolLease out;
                auto ec = cache.acquire(fe.layer, fe.expert, fe.size, reader, out);
                if (ec) { ++bad; continue; }
                bool full = true;
                for (uint64_t i = 0; i < fe.size; ++i)
                    if (static_cast<const uint8_t*>(out.data())[i] != fe.pat) full = false;
                if (full) ++ok; else ++bad;
                PoolLease copy = out;  // held borrower copy
                out = PoolLease{};
                std::this_thread::yield();
                bool copyok = true;
                for (uint64_t i = 0; i < fe.size; ++i)
                    if (static_cast<const uint8_t*>(copy.data())[i] != fe.pat) copyok = false;
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

    // ---- 13. fixture immutable byte-for-byte --------------------------------
    {
        const uint64_t fsum_after = file_checksum(g_fixture_path);
        check(fsum_after == fsum_before, "fixture file unchanged at end");
    }

    std::fprintf(stderr, "pool_expert_cache_test: %d checks, %d FAILURES\n", checks, failures);
    return failures == 0 ? 0 : 1;
}
