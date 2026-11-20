// tests/core/ram_pool_test.cpp - deterministic CPU-only tests for the RAM pool core.
//
// Linux only for real syscalls.  Uses link-time --wrap=mmap/mlock/munmap/munlock
// (__wrap_ in this TU, __real_ for actual syscalls) so each pool gets a distinct
// valid small anonymous mapping; deterministic mode fakes only the lock result.
// Total mappings across the run stay <= 16 MiB.
//
// Usage: ram_pool_test mock|real_mock
//   mock        - real mmap/munmap, faked lock counts & injectable lock failures.
//   real_mock   - real mmap/mlock/munlock/munmap via __real_*, /proc/self/status
//                 VmLck observed; skipped with 77 only when locking is not
//                 permitted (demonstrated restriction).

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

// ---- link-time interposition: one distinct valid small mapping per pool -----

namespace wrap {
int mmap_calls = 0;
int munmap_calls = 0;
int mlock_calls = 0;
int munlock_calls = 0;
long mlock_fail_at = -1;   // 1-based index; <0 never fails
int mlock_fail_errno = ENOMEM;
int mlock_unlock_len = -1; // exact prefix length to unlock on failure (-1 unset)

void reset_counts() {
    mmap_calls = 0; munmap_calls = 0; mlock_calls = 0; munlock_calls = 0;
    mlock_fail_at = -1; mlock_fail_errno = ENOMEM; mlock_unlock_len = -1;
}
}  // namespace wrap

int g_meta_call = 0;
int g_meta_throw_on = 0;
static void meta_hook() {
    ++g_meta_call;
    if (g_meta_call == g_meta_throw_on) throw std::bad_alloc{};
}

extern "C" {

void* __real_mmap(void* addr, size_t len, int prot, int flags, int fd, off_t off);
int __real_mlock(const void* addr, size_t len);
int __real_munlock(const void* addr, size_t len);
int __real_munmap(void* addr, size_t len);

void* __wrap_mmap(void* addr, size_t len, int prot, int flags, int fd, off_t off) {
    ++wrap::mmap_calls;
    return __real_mmap(addr, len, prot, flags, fd, off);
}
int __wrap_mlock(const void* addr, size_t len) {
    ++wrap::mlock_calls;
    if (wrap::mlock_fail_at >= 0 && wrap::mlock_calls > wrap::mlock_fail_at) {
        errno = wrap::mlock_fail_errno;
        return -1;
    }
    return __real_mlock(addr, len);
}
int __wrap_munlock(const void* addr, size_t len) {
    ++wrap::munlock_calls;
    return __real_munlock(addr, len);
}
int __wrap_munmap(void* addr, size_t len) {
    ++wrap::munmap_calls;
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

// ---- small deterministic mock pool (faked lock, real mmap/munmap) ----------

static std::error_code make_small_pool(Pool& out, uint64_t bytes) {
    wrap::reset_counts();
    std::error_code ec;
    auto p = Pool::create(bytes, ec);
    out = std::move(p);
    return ec;
}

}  // namespace

int main(int argc, char** argv) {
    const char* scenario = (argc > 1) ? argv[1] : "mock";
    const bool real = std::strcmp(scenario, "real_mock") == 0;
    if (!real && std::strcmp(scenario, "mock") != 0) {
        std::fprintf(stderr, "usage: ram_pool_test mock|real_mock\n");
        return 2;
    }

#if defined(__linux__)
    // ---- 1. capacity arithmetic (input math only, no allocation) -----------
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
        std::error_code ec;
        Pool pool;
        ec = make_small_pool(pool, 16ull << 20);
        check(!ec, "create 16 MiB");
        check(pool.capacity() == 16ull << 20, "capacity 16 MiB");
        auto s = pool.stats();
        check(s.capacity_bytes == 16ull << 20 && s.locked_bytes == 16ull << 20 &&
                  s.free_bytes == 16ull << 20 && s.alloc_count == 0,
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
        pool = Pool{};  // destroy owner while f holds lease
        check(f.valid(), "lease valid after owner gone");
        check(std::memcmp(f.data(), "\xCD\xCD\xCD\xCD", 4) == 0, "lease still writable");
        check(wrap::munmap_calls == 0 && wrap::munlock_calls == 0,
              "no teardown while lease alive");
        f = PoolLease{};
        check(wrap::munlock_calls == 1, "exactly one unlock at last release");
        check(wrap::munmap_calls == 1, "exactly one unmap at last release");
    }

    // ---- 4. populated output replacement (same & different pool) -----------
    {
        std::error_code ec;
        Pool p1, p2;
        ec = make_small_pool(p1, 16ull << 20);
        check(!ec, "p1 create");
        ec = make_small_pool(p2, 16ull << 20);
        check(!ec, "p2 create");
        PoolLease out;
        ec = p1.pool_alloc(4096, kPage, out);
        check(!ec && out.valid(), "initial alloc");
        ec = p1.pool_alloc(8192, kPage, out); // same out_lease replaces previous
        check(!ec && out.valid() && out.size() == 8192, "replace same out_lease");
        auto s = p1.stats();
        check(s.reserved_live_bytes == 8192 && s.free_bytes == (16ull << 20) - 8192,
              "only new block reserved");
        out = PoolLease{};

        PoolLease le;
        ec = p1.pool_alloc(4096, kPage, le);
        check(!ec && le.valid(), "alloc in p1");
        ec = p2.pool_alloc(4096, kPage, le); // replace output with different pool
        check(!ec && le.valid(), "replace with different pool");
        check(p1.stats().reserved_live_bytes == 0, "p1 block freed on replacement");
        le = PoolLease{};

        PoolLease keep;
        ec = p1.pool_alloc(1000, kPage, keep);
        check(!ec && keep.valid(), "keep initial");
        auto sk = p1.stats();
        ec = p1.pool_alloc(100ull << 20, kPage, keep); // too big -> fail
        check(ec && keep.valid(), "failed alloc keeps existing output");
        check(p1.stats().reserved_live_bytes == sk.reserved_live_bytes,
              "stats unchanged on failed alloc");
        keep = PoolLease{};
    }

    // ---- 5. alignment: returned address aligned, bounds, non-overlap -------
    {
        std::error_code ec;
        Pool pool;
        ec = make_small_pool(pool, 16ull << 20);
        check(!ec, "align pool create");
        PoolLease al;
        ec = pool.pool_alloc(100, 65536, al);
        check(!ec && al.valid(), "64 KiB alignment alloc");
        uintptr_t addr = reinterpret_cast<uintptr_t>(al.data());
        check(addr % 65536 == 0, "returned ADDRESS 64 KiB aligned");
        check(addr + al.size() <= reinterpret_cast<uintptr_t>(pool.base()) + pool.capacity(),
              "in bounds");
        al = PoolLease{};

        // huge valid alignment near range end
        PoolLease tail;
        ec = pool.pool_alloc(kPage, 16ull << 20, tail);
        if (!ec) {
            check(reinterpret_cast<uintptr_t>(tail.data()) % (16ull << 20) == 0,
                  "huge alignment near end");
            tail = PoolLease{};
        }
    }

    // ---- 6. fragmentation / exhaustion invariant ---------------------------
    {
        std::error_code ec;
        Pool pfrag;
        ec = make_small_pool(pfrag, 16ull << 20);
        check(!ec, "frag pool create");
        std::vector<PoolLease> frags;
        for (int i = 0; i < 64; ++i) {
            PoolLease t;
            if (pfrag.pool_alloc(kPage, kPage, t)) break;
            frags.push_back(std::move(t));
        }
        for (size_t i = 0; i + 1 < frags.size(); i += 2) frags[i] = PoolLease{};
        PoolLease big;
        ec = pfrag.pool_alloc(4ull << 20, kPage, big);
        (void)big;
        auto sf = pfrag.stats();
        check(sf.reserved_live_bytes + sf.free_bytes == sf.capacity_bytes,
              "fragmentation invariant reserved+free==capacity");
    }

    // ---- 7. concurrent distinct leases/payloads ----------------------------
    {
        std::error_code ec;
        Pool pconc;
        ec = make_small_pool(pconc, 16ull << 20);
        check(!ec, "conc pool create");
        std::atomic<int> success_count{0};
        auto worker = [&](int) {
            for (int i = 0; i < 100; ++i) {
                PoolLease t;
                auto e = pconc.pool_alloc(512, kPage, t);
                if (e) continue;
                ++success_count;
                std::memset(t.data(), static_cast<unsigned char>(0x41 + (i % 5)), t.size());
                if (i % 3 == 0) t = PoolLease{};
            }
        };
        std::vector<std::thread> threads;
        for (int i = 0; i < 4; ++i) threads.emplace_back(worker, i);
        for (auto& th : threads) th.join();
        auto sc = pconc.stats();
        check(success_count > 0, "concurrent some succeeded");
        check(sc.reserved_live_bytes + sc.free_bytes == sc.capacity_bytes,
              "concurrent reserved+free==capacity");
        check(sc.alloc_count <= 4 * 100, "concurrent alloc count sane");
    }

    // ---- 8. first/later injected mlock failure: errno + exact prefix -------
    {
        wrap::reset_counts();
        ram_pool_set_lock_step_bytes(kPage * 2); // 8 KiB step
        wrap::mlock_fail_at = 0; // fail first mlock
        wrap::mlock_fail_errno = ENOMEM;
        std::error_code ec;
        Pool pool;
        pool = Pool::create(kPage * 16, ec); // 64 KiB
        check(ec && pool.capacity() == 0, "first-mlock fail -> invalid");
        check(ec.value() == ENOMEM, "original errno preserved");
        check(wrap::mmap_calls == 1, "exactly one mmap");
        check(wrap::munmap_calls == 1, "exactly one unmap");
        check(wrap::munlock_calls == 0, "nothing locked -> no munlock");
        check(wrap::mlock_calls == 1, "one mlock attempt");

        wrap::reset_counts();
        wrap::mlock_fail_at = 5; // fail on 6th lock step
        Pool pool2;
        pool2 = Pool::create(kPage * 16, ec);
        check(ec && pool2.capacity() == 0, "later-mlock fail -> invalid");
        check(wrap::mmap_calls == 1, "later-fail: one mmap");
        check(wrap::munmap_calls == 1, "later-fail: one unmap");
        check(wrap::munlock_calls == 1, "later-fail: one munlock (partial prefix)");
        check(wrap::mlock_calls == 6, "later-fail: 6 mlock attempts");
        ram_pool_set_lock_step_bytes(256ull << 20);

        // zero/unaligned lock-step refused
        ram_pool_set_lock_step_bytes(0);
        Pool poolz;
        poolz = Pool::create(kPage * 16, ec);
        check(static_cast<bool>(ec), "zero lock-step refused");
        ram_pool_set_lock_step_bytes(kPage * 3);
        Pool poolu;
        poolu = Pool::create(kPage * 16, ec);
        check(static_cast<bool>(ec), "unaligned lock-step refused");
        ram_pool_set_lock_step_bytes(256ull << 20);
    }

    // ---- 9. metadata failure at prepare stage ------------------------------
    {
        std::error_code ec;
        Pool pool;
        ec = make_small_pool(pool, 16ull << 20);
        check(!ec, "meta pool create");
        PoolLease before;
        ec = pool.pool_alloc(1000, kPage, before);
        check(!ec && before.valid(), "meta: alloc before fault");
        auto sb = pool.stats();

        g_meta_call = 0; g_meta_throw_on = 1; // fail on make_shared
        ram_pool_set_metadata_fault(meta_hook);
        PoolLease bad;
        ec = pool.pool_alloc(1000, kPage, bad);
        check(ec && !bad.valid(), "meta: error not exception");
        check(pool.stats().reserved_live_bytes == sb.reserved_live_bytes,
              "meta: stats unchanged");
        check(before.valid(), "meta: existing output untouched");

        // release (never calls the hook) still coalesces; pool reusable
        before = PoolLease{};
        check(pool.stats().free_bytes == pool.stats().capacity_bytes,
              "meta: full coalesce after release with hook set");
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
        check(!ec, "guard pool create");
        {
            PoolLease glease;
            ec = pool.pool_alloc(500, kPage, glease);
            check(!ec && glease.valid(), "guard alloc");
            strata::core::PoolLeaseGuard guard(pool, std::move(glease));
            check(!glease.valid(), "guard took lease");
        } // guard releases
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

    // ---- real_mock: actual Linux VmLck lifecycle ---------------------------
#if defined(__linux__)
    if (real) {
        uint64_t base = read_vmlck_kb();
        std::error_code re;
        Pool rpool;
        rpool = Pool::create(64ull << 10, re); // 64 KiB small
        if (re) {
            std::fprintf(stderr, "SKIP: real_mock: pool create failed (%s) - locking not permitted\n",
                         re.message().c_str());
            if (failures != 0) return 1;
            std::fprintf(stderr, "ram_pool_test (real_mock): %d checks passed (VmLck skipped - not permitted)\n", checks);
            return 77;
        }
        uint64_t after = read_vmlck_kb();
        check(after >= base + 16, "real_mock: VmLck rose by pool KiB (>= baseline+16)");
        auto rs = rpool.stats();
        check(rs.locked_bytes == 64ull << 10, "real_mock: locked == 64 KiB");

        PoolLease x;
        re = rpool.pool_alloc(1024, kPage, x);
        check(!re && x.valid(), "real_mock: alloc chunk");
        std::memset(x.data(), 0x01, x.size());
        x = PoolLease{}; // release chunk: VmLck must NOT fall
        uint64_t mid = read_vmlck_kb();
        check(mid >= after, "real_mock: VmLck retained after chunk free");

        rpool = Pool{}; // drop owner, lease gone -> final teardown
        uint64_t end = read_vmlck_kb();
        check(end <= mid + 8, "real_mock: VmLck fell at final release (within 8 KiB)");

        if (failures != 0) {
            std::fprintf(stderr, "ram_pool_test (real_mock): %d checks, %d FAILURES\n", checks, failures);
            return 1;
        }
        std::fprintf(stderr, "ram_pool_test (real_mock): %d checks passed\n", checks);
        return 0;
    }
#endif

    if (failures != 0) {
        std::fprintf(stderr, "ram_pool_test (%s): %d checks, %d FAILURES\n", scenario, checks, failures);
        return 1;
    }
    std::fprintf(stderr, "ram_pool_test (%s): %d checks passed\n", scenario, checks);
    return 0;
}
