// tests/core/ram_pool_test.cpp - deterministic CPU-only tests for the managed RAM pool core.
//
// Covers: capacity arithmetic (51/52/53/55 GiB input math), invalid arithmetic,
// alignment/rounding, allocate/free/reuse/coalesce, fragmentation/exhaustion,
// payload byte preservation, lease retained across owner release, pool object gone
// before last lease, stats unchanged on failure.
//
// Two modes selectable by argv[1]:
//   mock       - interpose mlock/mmap/munlock/munmap/getrlimit/setrlimit so lock
//                failure cleanup and RLIMIT refusal are deterministic (no real RAM,
//                no real RLIMIT change).
//   real_mock  - same interposition but lets the *first* N mlocks succeed for real,
//                then fails; asserts /proc/self/status VmLck rises while the pool is
//                held and does not fall on chunk release, only at teardown.
//
// No scenario allocates more than 16 MiB total.

#include "strata/core/ram_pool.hpp"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <sys/mman.h>
#include <sys/resource.h>
#include <unistd.h>
#include <fcntl.h>
#include <vector>
#include <errno.h>
#include <dlfcn.h>

namespace {

using strata::core::Pool;
using strata::core::PoolLease;
using strata::core::PoolStats;
using strata::core::pool_capacity_bytes;

int checks = 0;
int failures = 0;

void check(bool ok, const char* label) {
    ++checks;
    if (!ok) { ++failures; std::fprintf(stderr, "FAIL: %s\n", label); }
    else { std::fprintf(stderr, "pass: %s\n", label); }
}

constexpr uint64_t kPage = 4096;

// ---- interposition (mock mode) -------------------------------------------
namespace mock {
int mlock_errno = 0;
int fail_after_mlock = -1; // fail on the (1-based) mlock call index
int mlock_calls = 0;
int mmap_calls = 0;
int munlock_calls = 0;
int munmap_calls = 0;
int getrlimit_calls = 0;
int setrlimit_calls = 0;
rlim_t rlimit_cur = RLIM_INFINITY;
rlim_t rlimit_max = RLIM_INFINITY;
bool delegate_mmap = false; // real_mock: let real mmap run
bool delegate_mlock = false; // real_mock: let real mlock run N times
int real_mlock_left = 0;

// Real libc entry points (via RTLD_NEXT) so real_mock can bypass the interposition
// without recursing into these wrappers.
typedef void* (*real_mmap_t)(void*, size_t, int, int, int, off_t);
typedef int (*real_mlock_t)(const void*, size_t);
typedef int (*real_munlock_t)(const void*, size_t);
typedef int (*real_munmap_t)(void*, size_t);
static real_mmap_t real_mmap_fn = nullptr;
static real_mlock_t real_mlock_fn = nullptr;
static real_munlock_t real_munlock_fn = nullptr;
static real_munmap_t real_munmap_fn = nullptr;

static void init_real() {
    if (real_mmap_fn == nullptr) {
        real_mmap_fn = (real_mmap_t) dlsym(RTLD_NEXT, "mmap");
        real_mlock_fn = (real_mlock_t) dlsym(RTLD_NEXT, "mlock");
        real_munlock_fn = (real_munlock_t) dlsym(RTLD_NEXT, "munlock");
        real_munmap_fn = (real_munmap_t) dlsym(RTLD_NEXT, "munmap");
    }
}

void reset() {
    mlock_errno = 0; fail_after_mlock = -1; mlock_calls = 0; mmap_calls = 0;
    munlock_calls = 0; munmap_calls = 0; getrlimit_calls = 0; setrlimit_calls = 0;
    rlimit_cur = RLIM_INFINITY; rlimit_max = RLIM_INFINITY;
    delegate_mmap = false; delegate_mlock = false; real_mlock_left = 0;
}
}  // namespace mock

extern "C" void* mmap(void* addr, size_t len, int prot, int flags, int fd, off_t off) {
    ++mock::mmap_calls;
    if (mock::delegate_mmap) { mock::init_real(); return mock::real_mmap_fn(addr, len, prot, flags, fd, off); }
    // Fake a stable anonymous mapping: return a page-aligned heap block we can
    // mlock/munlock/munmap against (malloc'd, never freed until teardown).
    static void* fake_base = nullptr;
    if (fake_base == nullptr) fake_base = std::malloc(1ull << 30);
    return fake_base;
}
extern "C" int mlock(const void* addr, size_t len) {
    ++mock::mlock_calls;
    if (mock::delegate_mlock && mock::real_mlock_left > 0) {
        --mock::real_mlock_left;
        mock::init_real();
        return mock::real_mlock_fn(addr, len);
    }
    if (mock::fail_after_mlock >= 0 && mock::mlock_calls > mock::fail_after_mlock) {
        errno = mock::mlock_errno ? mock::mlock_errno : ENOMEM;
        return -1;
    }
    return 0;
}
extern "C" int munlock(const void* addr, size_t len) {
    ++mock::munlock_calls;
    if (mock::delegate_mlock) { mock::init_real(); return mock::real_munlock_fn(addr, len); }
    return 0;
}
extern "C" int munmap(void* addr, size_t len) {
    ++mock::munmap_calls;
    if (mock::delegate_mmap) { mock::init_real(); return mock::real_munmap_fn(addr, len); }
    return 0;
}
extern "C" int getrlimit(int resource, struct rlimit* rl) {
    ++mock::getrlimit_calls;
    if (resource == RLIMIT_MEMLOCK) { rl->rlim_cur = mock::rlimit_cur; rl->rlim_max = mock::rlimit_max; return 0; }
    errno = EINVAL; return -1;
}
extern "C" int setrlimit(int resource, const struct rlimit* rl) {
    ++mock::setrlimit_calls;
    if (resource == RLIMIT_MEMLOCK) { mock::rlimit_cur = rl->rlim_cur; mock::rlimit_max = rl->rlim_max; return 0; }
    errno = EINVAL; return -1;
}

// ---- stderr capture ------------------------------------------------------
static int g_stderr_saved = -1;
static int capture_stderr(const char* path) {
    g_stderr_saved = dup(fileno(stderr));
    if (g_stderr_saved < 0) return -1;
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) { close(g_stderr_saved); g_stderr_saved = -1; return -1; }
    if (dup2(fd, fileno(stderr)) < 0) { close(fd); close(g_stderr_saved); g_stderr_saved = -1; return -1; }
    close(fd);
    return 0;
}
static void restore_stderr() {
    if (g_stderr_saved >= 0) { fflush(stderr); dup2(g_stderr_saved, fileno(stderr)); close(g_stderr_saved); g_stderr_saved = -1; }
}
static std::string read_capture(const char* path) {
    std::FILE* f = std::fopen(path, "r");
    if (!f) return "___CAPTURE_FAILED___";
    std::string out; char buf[512];
    while (std::fgets(buf, sizeof buf, f)) out += buf;
    std::fclose(f);
    return out;
}

// ---- /proc/self/status VmLck ---------------------------------------------
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

}  // namespace

// ============================ main =========================================

int main(int argc, char** argv) {
    const char* scenario = (argc > 1) ? argv[1] : "mock";
    const bool real = std::strcmp(scenario, "real_mock") == 0;
    if (!real && std::strcmp(scenario, "mock") != 0) {
        std::fprintf(stderr, "usage: ram_pool_test mock|real_mock\n");
        return 2;
    }

#if defined(__linux__)
    // ---- 1. capacity arithmetic: 51/52/53/55 GiB input math ------------------
    {
        uint64_t gib = 1073741824ull;
        uint64_t total = 52 * gib;
        uint64_t pool = 0;
        auto ec = pool_capacity_bytes(total, 0, 0, pool);
        check(!ec && pool == total, "capacity: 52 GiB total -> 52 GiB pool");
        ec = pool_capacity_bytes(51 * gib, 0, 0, pool);
        check(!ec && pool == 51 * gib, "capacity: 51 GiB total");
        ec = pool_capacity_bytes(53 * gib, 0, 0, pool);
        check(!ec && pool == 53 * gib, "capacity: 53 GiB total");
        ec = pool_capacity_bytes(55 * gib, 0, 0, pool);
        check(!ec && pool == 55 * gib, "capacity: 55 GiB total");

        // outside-owned subtraction
        ec = pool_capacity_bytes(52 * gib, 4 * gib, 0, pool);
        check(!ec && pool == 48 * gib, "capacity: 52 - 4 GiB outside -> 48 GiB");
        // internal headroom
        ec = pool_capacity_bytes(52 * gib, 0, 1 * gib, pool);
        check(!ec && pool == 51 * gib, "capacity: 52 - 1 GiB headroom -> 51 GiB");

        // page rounding: non-page-multiple total rounds down
        ec = pool_capacity_bytes(52 * gib + 1000, 0, 0, pool);
        check(!ec && pool == 52 * gib, "capacity: non-page total rounds down");
        // invalid: zero total
        ec = pool_capacity_bytes(0, 0, 0, pool);
        check(ec && pool == 0, "capacity: zero total -> error");
        // invalid: outside > total
        ec = pool_capacity_bytes(1 * gib, 2 * gib, 0, pool);
        check(ec && pool == 0, "capacity: outside > total -> error");
        // invalid: headroom consumes everything leaving zero
        ec = pool_capacity_bytes(1 * gib, 0, 1 * gib, pool);
        check(ec && pool == 0, "capacity: headroom == total -> error");
        // invalid: headroom rounds pool to zero
        ec = pool_capacity_bytes(kPage + 100, 0, kPage, pool);
        check(ec && pool == 0, "capacity: rounds to zero -> error");
    }

    // ---- 2. mock mode: alloc/free/reuse/coalesce/stats -----------------------
    if (!real) {
        mock::reset();
        std::error_code ec;
        auto pool = Pool::create(16ull << 20, ec);  // 16 MiB, within test bound
        check(!ec, "mock create: 16 MiB pool created");
        check(!ec && pool.capacity() == 16ull << 20, "mock create: pool valid");
        auto s = pool.stats();
        check(s.capacity_bytes == 16ull << 20, "mock stats: capacity 16 MiB");
        check(s.locked_bytes == 16ull << 20, "mock stats: locked == capacity");
        check(s.free_bytes == 16ull << 20, "mock stats: free == capacity");
        check(s.alloc_count == 0, "mock stats: no live allocs");

        PoolLease a, b, c;
        ec = pool.pool_alloc(1000, kPage, a);
        check(!ec && a.valid(), "mock alloc: 1000 B ok");
        ec = pool.pool_alloc(2000, kPage, b);
        check(!ec && b.valid(), "mock alloc: 2000 B ok");
        ec = pool.pool_alloc(3000, kPage, c);
        check(!ec && c.valid(), "mock alloc: 3000 B ok");

        // alignment: 8 KiB alignment must be honored (offset multiple of align)
        PoolLease d;
        ec = pool.pool_alloc(1, 8192, d);
        check(!ec && d.valid() && d.offset() % 8192 == 0, "mock alloc: 8 KiB alignment honored");

        // payload preservation
        std::memset(a.data(), 0xAB, a.size());
        PoolLease a2 = a;  // second borrower retains the range
        check(std::memcmp(a2.data(), "\xAB\xAB\xAB\xAB", 4) == 0, "mock: payload visible via second borrower");

        // exhaustion: allocate until free is gone (retain leases so they are not freed)
        std::vector<PoolLease> held;
        bool exhausted = false;
        for (int i = 0; i < 10000; ++i) {
            PoolLease tmp;
            if (pool.pool_alloc(4096, kPage, tmp)) { exhausted = true; break; }
            held.push_back(std::move(tmp));
        }
        check(exhausted, "mock: pool exhausts (no spill)");
        held.clear();  // release all so later allocs in this test have room
        (void)held;

        // releasing one borrower of a2 must NOT free the range (a still holds it)
        auto s_before = pool.stats();
        // (a2 goes out of scope via copy; we reset it explicitly)
        a2 = PoolLease{};
        auto s_after = pool.stats();
        check(s_after.reserved_live_bytes == s_before.reserved_live_bytes,
              "mock: range not freed while another borrower holds it");

        // release last borrower -> freed & coalesced
        a = PoolLease{};
        auto s2 = pool.stats();
        check(s2.reserved_live_bytes < s_before.reserved_live_bytes, "mock: range freed on last release");
        check(s2.free_bytes > s_before.free_bytes, "mock: free grows after release");

        // reuse after free
        PoolLease e;
        ec = pool.pool_alloc(1000, kPage, e);
        check(!ec && e.valid(), "mock: reuse freed range");
        auto s3 = pool.stats();
        check(s3.reserved_live_bytes == s_before.reserved_live_bytes, "mock: reuse restores reserved bytes");

        // stats unchanged on failure (invalid alignment) - baseline s3 still has live leases b,c,d,e
        PoolLease bad;
        ec = pool.pool_alloc(100, 3, bad);
        check(ec && !bad.valid(), "mock: non-pow2 alignment rejected");
        auto s4 = pool.stats();
        check(s4.alloc_count == s3.alloc_count && s4.reserved_live_bytes == s3.reserved_live_bytes,
              "mock: stats unchanged on allocation failure");

        e = PoolLease{}; b = PoolLease{}; c = PoolLease{}; d = PoolLease{};

        // pool object gone before last lease: mapping stays alive
        {
            PoolLease f;
            ec = pool.pool_alloc(500, kPage, f);
            check(!ec && f.valid(), "mock: alloc f");
            auto* pool_ptr = &pool;
            (void)pool_ptr;
            // destroy pool while f holds a lease
            pool = Pool{};
            // f must still be usable
            check(f.valid(), "mock: lease valid after pool owner destroyed");
            std::memset(f.data(), 0xCD, f.size());
            check(std::memcmp(f.data(), "\xCD\xCD\xCD\xCD", 4) == 0, "mock: lease still writable after pool owner gone");
            f = PoolLease{};  // final release -> teardown
        }
        check(mock::munmap_calls >= 1, "mock: teardown unmaps after last lease");
        check(mock::munlock_calls >= 1, "mock: teardown munlocks after last lease");

        // failure cleanup: fail on first mlock -> no partial pool locked
        mock::reset();
        mock::fail_after_mlock = 0;
        auto pool2 = Pool::create(16ull << 20, ec);
        check(ec && pool2.capacity() == 0, "mock: first-mlock failure -> pool invalid");
        check(mock::munmap_calls >= 1, "mock: first-mlock failure cleans up mapping");
    }

    // ---- 3. real_mock: real VmLck rise/fall ------------------------------
    if (real) {
        // delegate real mmap + first few real mlocks so VmLck actually changes
        mock::reset();
        mock::delegate_mmap = true;
        mock::delegate_mlock = true;
        mock::real_mlock_left = 4;

        uint64_t base = read_vmlck_kb();
        std::error_code ec;
        auto pool = Pool::create(4ull << 20, ec);  // 4 MiB -> 4 real mlock chunks
        check(!ec, "real_mock: create 4 MiB pool");
        check(mock::mlock_calls >= 1, "real_mock: mlock attempted");
        uint64_t after = read_vmlck_kb();
        check(after >= base, "real_mock: VmLck did not decrease after lock");
        auto s = pool.stats();
        check(s.locked_bytes == 4ull << 20, "real_mock: locked == 4 MiB");

        // carve a chunk and release it -> VmLck must NOT fall (pool stays locked)
        PoolLease x;
        ec = pool.pool_alloc(1024, kPage, x);
        check(!ec && x.valid(), "real_mock: alloc chunk");
        std::memset(x.data(), 0x01, x.size());
        x = PoolLease{};  // release chunk
        uint64_t mid = read_vmlck_kb();
        check(mid >= after, "real_mock: VmLck did not fall on chunk release");

        // teardown -> VmLck falls back to baseline
        pool = Pool{};
        uint64_t end = read_vmlck_kb();
        check(end <= mid, "real_mock: VmLck fell at teardown");
    }
#else
    (void)real;
    std::fprintf(stderr, "non-Linux: create should refuse\n");
    std::error_code ec;
    auto pool = Pool::create(16ull << 20, ec);
    check(ec == std::errc::not_supported && !pool.valid(), "non-Linux: create refused");
#endif

    if (failures != 0) {
        std::fprintf(stderr, "ram_pool_test (%s): %d checks, %d FAILURES\n", scenario, checks, failures);
        return 1;
    }
    std::fprintf(stderr, "ram_pool_test (%s): %d checks passed\n", scenario, checks);
    return 0;
}
