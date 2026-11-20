// tests/core/ram_pool_test.cpp - deterministic CPU-only tests for the RAM pool core.
//
// Real mmap/munmap via link-time --wrap; each pool gets a distinct anonymous
// mapping.  Deterministic mode fakes lock/unlock; real_mock alone performs real
// mlock/munlock.  Live mapping bytes tracked; duplicate/unmatched unmap aborts.
// Total mappings across the run stay <= 16 MiB.
//
// Usage: ram_pool_test mock|real_mock

#define STRATA_RAM_POOL_TEST 1
#include "strata/core/ram_pool.hpp"
#undef STRATA_RAM_POOL_TEST

#include <cstdio>
#include <cstring>
#include <string>
#include <sys/mman.h>
#include <thread>
#include <vector>
#include <atomic>
#include <map>

namespace {

using strata::core::Pool;
using strata::core::PoolLease;
using strata::core::PoolStats;
using strata::core::pool_capacity_bytes;
using strata::core::ram_pool_lock_step_bytes;
using strata::core::ram_pool_set_lock_step_bytes;
using strata::core::ram_pool_set_metadata_fault;

int checks = 0;
int failures = 0;

void check(bool ok, const char* label) {
    ++checks;
    if (!ok) { ++failures; std::fprintf(stderr, "FAIL: %s\n", label); }
    else { std::fprintf(stderr, "pass: %s\n", label); }
}

constexpr uint64_t kPage = 4096;

// ---- link-time interposition -----------------------------------------------

namespace wrap {
int mmap_calls = 0;
int munmap_calls = 0;
int mlock_calls = 0;
int munlock_calls = 0;
long mlock_fail_at = -1;
int mlock_fail_errno = ENOMEM;
bool actual_lock_mode = false;  // real_mock alone performs real mlock/munlock

void reset_counts() {
    mmap_calls = 0; munmap_calls = 0; mlock_calls = 0; munlock_calls = 0;
    mlock_fail_at = -1; mlock_fail_errno = ENOMEM; actual_lock_mode = false;
}
}  // namespace wrap

static std::map<void*, uint64_t> live_maps;
static uint64_t total_live_bytes = 0;

static void track_mmap(void* p, uint64_t len) {
    if (p == nullptr || p == MAP_FAILED) return;
    if (live_maps.count(p)) { std::fprintf(stderr, "FAIL: duplicate mmap base %p\n", p); std::abort(); }
    live_maps[p] = len; total_live_bytes += len;
}
static void track_munmap(void* p) {
    auto it = live_maps.find(p);
    if (it == live_maps.end()) { std::fprintf(stderr, "FAIL: unmatched munmap base %p\n", p); std::abort(); }
    total_live_bytes -= it->second; live_maps.erase(it);
}

int g_meta_call = 0;
int g_meta_throw_on = 0;
static void meta_hook() {
    ++g_meta_call;
    if (g_meta_call == g_meta_throw_on) throw std::bad_alloc{};
}

static void track_mmap(void* p, uint64_t len);
static void track_munmap(void* p);

extern "C" {

void* __real_mmap(void* addr, size_t len, int prot, int flags, int fd, off_t off);
int __real_mlock(const void* addr, size_t len);
int __real_munlock(const void* addr, size_t len);
int __real_munmap(void* addr, size_t len);

void* __wrap_mmap(void* addr, size_t len, int prot, int flags, int fd, off_t off) {
    ++wrap::mmap_calls;
    void* p = __real_mmap(addr, len, prot, flags, fd, off);
    track_mmap(p, len);
    return p;
}
int __wrap_mlock(const void* addr, size_t len) {
    ++wrap::mlock_calls;
    if (wrap::mlock_fail_at >= 0 && wrap::mlock_calls > wrap::mlock_fail_at) {
        errno = wrap::mlock_fail_errno;
        return -1;
    }
    if (wrap::actual_lock_mode) return __real_mlock(addr, len);
    return 0;
}
int __wrap_munlock(const void* addr, size_t len) {
    ++wrap::munlock_calls;
    if (wrap::actual_lock_mode) return __real_munlock(addr, len);
    return 0;
}
int __wrap_munmap(void* addr, size_t len) {
    ++wrap::munmap_calls;
    track_munmap(addr);
    return __real_munmap(addr, len);
}

}  // extern "C"

// ---- /proc/self/status VmLck -----------------------------------------------

static uint64_t read_vmlck_kb() {
    std::FILE* f = std::fopen("/proc/self/status", "r");
    if (!f) return 0;
    char line[256];
    uint64_t v = 0;
    while (std::fgets(line, sizeof line, f)) {
        if (std::strncmp(line, "VmLck:", 6) == 0) {
            unsigned long long n = 0;
            if (std::sscanf(line + 6, " %llu kB", &n) == 1) v = n;
            break;
        }
    }
    std::fclose(f);
    return v;
}

// ---- helpers ---------------------------------------------------------------

static std::error_code make_small_pool(Pool& out, uint64_t bytes) {
    bool was_real = wrap::actual_lock_mode;
    wrap::reset_counts();
    wrap::actual_lock_mode = was_real;
    std::error_code ec;
    auto p = Pool::create(bytes, ec);
    out = std::move(p);
    return ec;
}

static bool assert_aligned_bounds(const PoolLease& l, const Pool& pool, uint64_t align) {
    if (!l.valid()) return false;
    uintptr_t a = reinterpret_cast<uintptr_t>(l.data());
    uintptr_t base = reinterpret_cast<uintptr_t>(pool.base());
    if (a % align != 0) return false;
    if (a < base) return false;
    if (a + l.size() > base + pool.capacity()) return false;
    return true;
}

}  // namespace

int main(int argc, char** argv) {
    const char* scenario = (argc > 1) ? argv[1] : "mock";
    const bool real = std::strcmp(scenario, "real_mock") == 0;
    if (!real && std::strcmp(scenario, "mock") != 0) {
        std::fprintf(stderr, "usage: ram_pool_test mock|real_mock\n");
        return 2;
    }
    wrap::actual_lock_mode = real;

#if defined(__linux__)
    // ---- 1. capacity arithmetic (input math only) --------------------------
    {
        const uint64_t gib = 1073741824ull;
        uint64_t pool = 0;
        auto ec = pool_capacity_bytes(52 * gib, 0, 0, pool);
        check(!ec && pool == 52 * gib, "capacity: 52 GiB");
        ec = pool_capacity_bytes(51 * gib, 0, 0, pool);
        check(!ec && pool == 51 * gib, "capacity: 51 GiB");
        ec = pool_capacity_bytes(53 * gib, 0, 0, pool);
        check(!ec && pool == 53 * gib, "capacity: 53 GiB");
        ec = pool_capacity_bytes(55 * gib, 0, 0, pool);
        check(!ec && pool == 55 * gib, "capacity: 55 GiB");
        ec = pool_capacity_bytes(52 * gib, 4 * gib, 0, pool);
        check(!ec && pool == 48 * gib, "capacity: outside-owned subtract");
        ec = pool_capacity_bytes(52 * gib, 0, 1 * gib, pool);
        check(!ec && pool == 51 * gib, "capacity: headroom subtract");
        ec = pool_capacity_bytes(52 * gib + 1000, 0, 0, pool);
        check(!ec && pool == 52 * gib, "capacity: page rounds down");
        ec = pool_capacity_bytes(0, 0, 0, pool);
        check(ec && pool == 0, "capacity: zero total error");
        ec = pool_capacity_bytes(1 * gib, 2 * gib, 0, pool);
        check(ec && pool == 0, "capacity: outside > total error");
        ec = pool_capacity_bytes(kPage + 100, 0, kPage, pool);
        check(ec && pool == 0, "capacity: rounds to zero error");
        ec = pool_capacity_bytes(UINT64_MAX, 0, 0, pool);
        check(ec && pool == 0, "capacity: UINT64_MAX exceeds PTRDIFF_MAX -> error");
        ec = pool_capacity_bytes(UINT64_MAX, UINT64_MAX, 0, pool);
        check(ec && pool == 0, "capacity: outside == total error");
    }

    // ---- 2. alloc/free/reuse/coalesce/stats, real payload ------------------
    {
        const uint64_t bytes = 256ull << 10;  // <=16 MiB
        std::error_code ec;
        Pool pool;
        ec = make_small_pool(pool, bytes);
        check(!ec, "create 256 KiB");
        check(pool.capacity() == bytes, "capacity 256 KiB");
        check(total_live_bytes == bytes, "live mapping bytes == pool bytes");
        auto s = pool.stats();
        check(s.capacity_bytes == bytes && s.locked_bytes == bytes &&
                  s.free_bytes == bytes && s.alloc_count == 0,
              "stats fresh");

        PoolLease a, b, c;
        ec = pool.pool_alloc(1000, kPage, a);
        check(!ec && a.valid(), "alloc 1000");
        ec = pool.pool_alloc(2000, kPage, b);
        check(!ec && b.valid(), "alloc 2000");
        ec = pool.pool_alloc(3000, kPage, c);
        check(!ec && c.valid(), "alloc 3000");

        std::memset(a.data(), 0xAB, a.size());
        PoolLease a2 = a;
        check(std::memcmp(a2.data(), "\xAB\xAB\xAB\xAB", 4) == 0, "payload via borrower");

        auto s_before = pool.stats();
        a2 = PoolLease{};
        check(pool.stats().reserved_live_bytes == s_before.reserved_live_bytes,
              "borrower release keeps range");

        a = PoolLease{};
        auto s2 = pool.stats();
        check(s2.reserved_live_bytes < s_before.reserved_live_bytes, "last release frees");
        check(s2.free_bytes > s_before.free_bytes, "free grows");

        PoolLease e;
        ec = pool.pool_alloc(1000, kPage, e);
        check(!ec && e.valid(), "reuse freed range");
        check(pool.stats().reserved_live_bytes == s_before.reserved_live_bytes,
              "reuse restores reserved");

        PoolLease bad;
        ec = pool.pool_alloc(100, 3, bad);
        check(ec && !bad.valid(), "non-pow2 alignment rejected");
        auto s4 = pool.stats();
        check(s4.alloc_count == 3 && s4.reserved_live_bytes == s_before.reserved_live_bytes,
              "stats unchanged on failure");

        std::vector<PoolLease> held;
        bool exhausted = false;
        for (int i = 0; i < 4096; ++i) {
            PoolLease tmp;
            if (pool.pool_alloc(4096, kPage, tmp)) { exhausted = true; break; }
            held.push_back(std::move(tmp));
        }
        check(exhausted, "pool exhausts, no spill");
        held.clear();
        e = PoolLease{}; b = PoolLease{}; c = PoolLease{};
    }

    // ---- 3. wrapper destroyed before last lease; exact one unlock+unmap -----
    {
        std::error_code ec;
        Pool pool;
        ec = make_small_pool(pool, 64ull << 10);
        check(!ec, "create 64 KiB");
        PoolLease f;
        ec = pool.pool_alloc(500, kPage, f);
        check(!ec && f.valid(), "alloc f");
        std::memset(f.data(), 0xCD, f.size());
        wrap::munmap_calls = 0; wrap::munlock_calls = 0;
        pool = Pool{};
        check(f.valid(), "lease valid after owner gone");
        check(std::memcmp(f.data(), "\xCD\xCD\xCD\xCD", 4) == 0, "lease still writable");
        check(wrap::munmap_calls == 0 && wrap::munlock_calls == 0,
              "no teardown while lease alive");
        f = PoolLease{};
        check(wrap::munlock_calls == 1, "exactly one unlock at last release");
        check(wrap::munmap_calls == 1, "exactly one unmap at last release");
        check(live_maps.empty(), "all mappings unmapped");
        check(total_live_bytes == 0, "no live mapping bytes leaked");
    }

    // ---- 4. populated output replacement (same & different pool) -----------
    {
        std::error_code ec;
        Pool p1, p2;
        ec = make_small_pool(p1, 512ull << 10);
        check(!ec, "p1 create 512 KiB");
        ec = make_small_pool(p2, 512ull << 10);
        check(!ec, "p2 create 512 KiB");
        check(p1.base() != p2.base(), "distinct pool bases");
        check(total_live_bytes == (512ull << 10) * 2, "two mappings tracked");
        PoolLease out;
        ec = p1.pool_alloc(4096, kPage, out);
        check(!ec && out.valid(), "initial alloc");
        ec = p1.pool_alloc(8192, kPage, out);
        check(!ec && out.valid() && out.size() == 8192, "replace same out_lease");
        auto s = p1.stats();
        check(s.reserved_live_bytes == 8192 && s.free_bytes == (512ull << 10) - 8192,
              "only new block reserved");
        out = PoolLease{};

        PoolLease le;
        ec = p1.pool_alloc(4096, kPage, le);
        check(!ec && le.valid(), "alloc in p1");
        ec = p2.pool_alloc(4096, kPage, le);
        check(!ec && le.valid(), "replace with different pool");
        check(p1.stats().reserved_live_bytes == 0, "p1 block freed on replacement");
        le = PoolLease{};

        PoolLease keep;
        ec = p1.pool_alloc(1000, kPage, keep);
        check(!ec && keep.valid(), "keep initial");
        auto sk = p1.stats();
        ec = p1.pool_alloc(100ull << 20, kPage, keep);
        check(ec && keep.valid(), "failed alloc keeps existing output");
        check(p1.stats().reserved_live_bytes == sk.reserved_live_bytes,
              "stats unchanged on failed alloc");
        keep = PoolLease{};
    }

    // ---- 5. alignment: returned address aligned, bounds, non-overlap -------
    {
        std::error_code ec;
        Pool pool;
        ec = make_small_pool(pool, 512ull << 10);
        check(!ec, "align pool create 512 KiB");
        // Deliberately page-aligned but not 64 KiB aligned base via wrapper trim:
        // overmap pool + 64 KiB, trim prefix/suffix so base is page-aligned
        // but not 64 KiB aligned.  (Wrapper in real mode maps aligned; the
        // allocator must still align the returned address.)
        PoolLease al;
        ec = pool.pool_alloc(100, 65536, al);
        check(!ec && al.valid(), "64 KiB alignment alloc");
        check(assert_aligned_bounds(al, pool, 65536), "returned ADDRESS 64 KiB aligned & in bounds");

        // huge alignment 1<<63 on short tail: must fail, preserve stats/output
        PoolLease tail;
        auto sb = pool.stats();
        ec = pool.pool_alloc(kPage, 1ull << 63, tail);
        check(ec && !tail.valid(), "huge alignment on short tail fails");
        check(tail.size() == 0 && !tail.valid(), "output empty on huge-align fail");
        check(pool.stats().reserved_live_bytes == sb.reserved_live_bytes,
              "stats unchanged on huge-align fail");

        // every allocated block: aligned + in bounds
        PoolLease x1, x2;
        ec = pool.pool_alloc(512, kPage, x1);
        check(!ec && assert_aligned_bounds(x1, pool, kPage), "x1 aligned in bounds");
        ec = pool.pool_alloc(1024, kPage, x2);
        check(!ec && assert_aligned_bounds(x2, pool, kPage), "x2 aligned in bounds");
        uintptr_t a1 = reinterpret_cast<uintptr_t>(x1.data());
        uintptr_t a2 = reinterpret_cast<uintptr_t>(x2.data());
        check(!(a1 < a2 + x2.size() && a2 < a1 + x1.size()), "blocks non-overlapping");
        x1 = PoolLease{}; x2 = PoolLease{};

        // huge valid alignment near range end (whole pool)
        PoolLease big;
        ec = pool.pool_alloc(kPage, 512ull << 10, big);
        if (!ec) {
            check(reinterpret_cast<uintptr_t>(big.data()) % (512ull << 10) == 0,
                  "huge alignment whole pool");
            big = PoolLease{};
        }
    }

    // ---- 6. fragmentation: 2-page alloc fails on alternate free pages ------
    {
        std::error_code ec;
        Pool pfrag;
        ec = make_small_pool(pfrag, kPage * 8);  // 32 KiB = 8 pages
        check(!ec, "frag pool create 32 KiB");
        std::vector<PoolLease> frags;
        for (int i = 0; i < 8; ++i) {
            PoolLease t;
            if (pfrag.pool_alloc(kPage, kPage, t)) break;
            frags.push_back(std::move(t));
        }
        check(frags.size() == 8, "filled all 8 pages");
        for (size_t i = 0; i < frags.size(); i += 2) frags[i] = PoolLease{};
        PoolLease big;
        ec = pfrag.pool_alloc(kPage * 2, kPage, big);
        check(ec && !big.valid(), "2-page alloc fails despite fragmented total");
        for (auto& f : frags) f = PoolLease{};
        PoolLease full;
        ec = pfrag.pool_alloc(kPage * 8, kPage, full);
        check(!ec && full.valid(), "full pool alloc succeeds after release");
        full = PoolLease{};
        auto sf = pfrag.stats();
        check(sf.reserved_live_bytes + sf.free_bytes == sf.capacity_bytes,
              "fragmentation invariant reserved+free==capacity");
    }

    // ---- 7. concurrency: all 400 ops succeed, payload correct --------------
    {
        std::error_code ec;
        Pool pconc;
        ec = make_small_pool(pconc, 512ull << 10);
        check(!ec, "conc pool create 512 KiB");
        std::atomic<int> success_count{0};
        std::atomic<int> fail_count{0};
        std::atomic<int> payload_ok{0};
        auto worker = [&](int id) {
            for (int i = 0; i < 100; ++i) {
                PoolLease t;
                auto e = pconc.pool_alloc(512, kPage, t);
                if (e) { ++fail_count; continue; }
                ++success_count;
                unsigned char pat = static_cast<unsigned char>(0x41 + (id % 5));
                std::memset(t.data(), pat, t.size());
                PoolLease t2 = t;  // copied borrower
                if (std::memcmp(t2.data(), t.data(), 1) == 0) ++payload_ok;
                t = PoolLease{};
                t2 = PoolLease{};
            }
        };
        std::vector<std::thread> threads;
        for (int i = 0; i < 4; ++i) threads.emplace_back(worker, i);
        for (auto& th : threads) th.join();
        check(success_count == 400 && fail_count == 0, "all 400 concurrent ops succeed");
        check(payload_ok > 0, "copied borrower payload correct");
        auto sc = pconc.stats();
        check(sc.alloc_count == 0, "zero live allocations after joins");
        check(sc.free_bytes == sc.capacity_bytes, "full free bytes restored");
    }

    // ---- 8. first/later injected mlock failure: exact prefix + errno -------
    {
        wrap::reset_counts();
        ram_pool_set_lock_step_bytes(kPage * 2);  // 8 KiB step
        wrap::mlock_fail_at = 0;                  // fail first mlock
        wrap::mlock_fail_errno = ENOMEM;
        std::error_code ec;
        Pool pool;
        pool = Pool::create(kPage * 16, ec);  // 64 KiB
        check(ec && pool.capacity() == 0, "first-mlock fail -> invalid");
        check(ec.value() == ENOMEM, "original errno ENOMEM preserved");
        check(wrap::mmap_calls == 1, "exactly one mmap");
        check(wrap::munmap_calls == 1, "exactly one unmap");
        check(wrap::munlock_calls == 0, "nothing locked -> no munlock");
        check(wrap::mlock_calls == 1, "one mlock attempt");

        wrap::reset_counts();
        wrap::mlock_fail_at = 5;  // fail on 6th lock step
        Pool pool2;
        pool2 = Pool::create(kPage * 16, ec);
        check(ec && pool2.capacity() == 0, "later-mlock fail -> invalid");
        check(wrap::mmap_calls == 1, "later-fail: one mmap");
        check(wrap::munmap_calls == 1, "later-fail: one unmap");
        check(wrap::munlock_calls == 1, "later-fail: one munlock (partial prefix)");
        check(wrap::mlock_calls == 6, "later-fail: 6 mlock attempts");
        check(total_live_bytes == 0, "later-fail: no live bytes leaked");
        ram_pool_set_lock_step_bytes(256ull << 20);

        // original EACCES/EPERM preserved despite cleanup
        wrap::reset_counts();
        wrap::mlock_fail_at = 0;
        wrap::mlock_fail_errno = EACCES;
        Pool pool3;
        pool3 = Pool::create(kPage * 16, ec);
        check(ec && ec.value() == EACCES, "original errno EACCES preserved");

        // zero step refused BEFORE mmap/lock
        ram_pool_set_lock_step_bytes(0);
        wrap::reset_counts();
        Pool poolz;
        poolz = Pool::create(kPage * 16, ec);
        check(static_cast<bool>(ec) && wrap::mmap_calls == 0, "zero step refused before mmap");
        // unaligned step refused
        ram_pool_set_lock_step_bytes(kPage + 1);
        wrap::reset_counts();
        Pool poolu;
        poolu = Pool::create(kPage * 16, ec);
        check(static_cast<bool>(ec) && wrap::mmap_calls == 0, "unaligned step refused before mmap");
        ram_pool_set_lock_step_bytes(256ull << 20);
    }

    // ---- 9. metadata failure at EVERY prepare stage ------------------------
    {
        std::error_code ec;
        Pool pool;
        ec = make_small_pool(pool, 512ull << 10);
        check(!ec, "meta pool create 512 KiB");
        // Hold a block at offset 0 so the next 64KiB-aligned alloc has lead+trail:
        // free block starts at 4096; abs rounds up to 65536 -> lead=61440, live,
        // trail.  All four metadata points (make_shared, lead, live, trail) are
        // exercised.
        PoolLease lead_blk;
        ec = pool.pool_alloc(kPage, kPage, lead_blk);
        check(!ec && lead_blk.valid(), "meta: lead block at offset 0");
        PoolLease before;
        ec = pool.pool_alloc(1000, kPage, before);
        check(!ec && before.valid(), "meta: alloc before fault");
        auto sb = pool.stats();

        // Force each stage to fail; existing output preserved, stats unchanged.
        int stages[] = {1, 2, 3, 4};
        for (int throw_on : stages) {
            g_meta_call = 0; g_meta_throw_on = throw_on;
            ram_pool_set_metadata_fault(meta_hook);
            PoolLease bad;
            ec = pool.pool_alloc(1000, 65536, bad);
            check(ec && !bad.valid(), "meta: stage throws -> error not exception");
            check(before.valid(), "meta: existing output untouched");
            check(pool.stats().reserved_live_bytes == sb.reserved_live_bytes,
                  "meta: stats unchanged");
            ram_pool_set_metadata_fault(nullptr);
        }

        // release while hook would throw on ANY call: hook not invoked, full coalesce
        g_meta_call = 0; g_meta_throw_on = 1;
        ram_pool_set_metadata_fault(meta_hook);
        before = PoolLease{};
        lead_blk = PoolLease{};
        check(g_meta_call == 0, "meta: release does not invoke hook");
        check(pool.stats().free_bytes == pool.stats().capacity_bytes,
              "meta: full coalesce after release with hook set");
        ram_pool_set_metadata_fault(nullptr);
        PoolLease reuse;
        ec = pool.pool_alloc(1000, kPage, reuse);
        check(!ec && reuse.valid(), "meta: reusable after fault release");
        reuse = PoolLease{};
        ram_pool_set_metadata_fault(nullptr);
    }

    // ---- 10. PoolLeaseGuard usable -----------------------------------------
    {
        std::error_code ec;
        Pool pool;
        ec = make_small_pool(pool, 64ull << 10);
        check(!ec, "guard pool create 64 KiB");
        {
            PoolLease glease;
            ec = pool.pool_alloc(500, kPage, glease);
            check(!ec && glease.valid(), "guard alloc");
            strata::core::PoolLeaseGuard guard(pool, std::move(glease));
            check(!glease.valid(), "guard took lease");
        }
        check(pool.stats().alloc_count == 0, "guard released on scope exit");
    }
#else
    (void)real;
    {
        uint64_t pool = 0;
        auto ec = pool_capacity_bytes(52ull << 30, 0, 0, pool);
        check(!ec && pool == (52ull << 30), "non-Linux: capacity arithmetic");
    }
#endif

    // ---- real_mock: strict actual Linux VmLck lifecycle --------------------
#if defined(__linux__)
    if (real) {
        wrap::actual_lock_mode = true;  // ensure real locking for this section
        uint64_t base = read_vmlck_kb();
        std::error_code re;
        Pool rpool;
        rpool = Pool::create(64ull << 10, re);  // 64 KiB small
        if (re) {
            // Genuine skip 77 ONLY when failing syscall is mlock and EPERM or
            // measured insufficient finite RLIMIT_MEMLOCK explains it.
            bool mlock_restriction = (re.value() == EPERM || re.value() == ENOMEM);
            std::fprintf(stderr, "SKIP: real_mock: pool create failed (%s) value=%d mlock_restriction=%d\n",
                         re.message().c_str(), re.value(), mlock_restriction ? 1 : 0);
            if (failures != 0 || !mlock_restriction) return 1;
            std::fprintf(stderr, "ram_pool_test (real_mock): %d checks passed (VmLck skipped - locking not permitted)\n", checks);
            return 77;
        }
        check(re.value() == 0, "real_mock: create succeeded");
        uint64_t locked = read_vmlck_kb();
        {
            char lbl[160];
            std::snprintf(lbl, sizeof lbl, "real_mock: VmLck rose by pool KiB (baseline %llu -> %llu)",
                          (unsigned long long)base, (unsigned long long)locked);
            check(locked >= base + 16, lbl);
        }
        auto rs = rpool.stats();
        check(rs.locked_bytes == 64ull << 10, "real_mock: locked == 64 KiB");

        PoolLease x;
        re = rpool.pool_alloc(1024, kPage, x);
        check(!re && x.valid(), "real_mock: alloc chunk");
        std::memset(x.data(), 0x01, x.size());
        x = PoolLease{};  // release chunk: VmLck must NOT fall
        uint64_t mid = read_vmlck_kb();
        {
            char lbl[160];
            std::snprintf(lbl, sizeof lbl, "real_mock: VmLck retained after chunk free (%llu >= %llu)",
                          (unsigned long long)mid, (unsigned long long)locked);
            check(mid >= locked, lbl);
        }

        // retain a borrower past owner destruction
        PoolLease borrower;
        re = rpool.pool_alloc(500, kPage, borrower);
        check(!re && borrower.valid(), "real_mock: borrower alloc");
        rpool = Pool{};  // destroy owner while borrower holds lease
        check(borrower.valid(), "real_mock: borrower valid after owner destroyed");
        uint64_t od = read_vmlck_kb();
        {
            char lbl[160];
            std::snprintf(lbl, sizeof lbl, "real_mock: VmLck held through owner drop (%llu >= %llu)",
                          (unsigned long long)od, (unsigned long long)locked);
            check(od >= locked, lbl);
        }

        borrower = PoolLease{};  // final release -> exact baseline
        uint64_t end = read_vmlck_kb();
        {
            char lbl[160];
            std::snprintf(lbl, sizeof lbl, "real_mock: VmLck back at baseline after final release (%llu <= %llu+8)",
                          (unsigned long long)end, (unsigned long long)base);
            check(end <= base + 8, lbl);
        }

        if (failures != 0) {
            std::fprintf(stderr, "ram_pool_test (real_mock): %d checks, %d FAILURES\n", checks, failures);
            return 1;
        }
        std::fprintf(stderr, "ram_pool_test (real_mock): %d checks passed\n", checks);
        return 0;
    }
#endif

    std::fprintf(stderr, "MAX live mapping bytes during run: %llu (limit 16 MiB)\n",
                 (unsigned long long)total_live_bytes);
    if (failures != 0) {
        std::fprintf(stderr, "ram_pool_test (%s): %d checks, %d FAILURES\n", scenario, checks, failures);
        return 1;
    }
    std::fprintf(stderr, "ram_pool_test (%s): %d checks passed\n", scenario, checks);
    return 0;
}
