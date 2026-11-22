// tests/core/ram_pool_nonlinux_probe.cpp - Simulated non-Linux guard probe.
//
// Compiled with -U__linux__ to verify the __linux__-guarded code paths degrade
// to a self-contained refusal contract on platforms where Linux types and
// syscalls (mmap, mlock, munlock, munmap, rlimit) do not exist.  This is a
// simulated preprocessor coverage check on a Linux host with a CPU-only
// compiler; it is NOT a real Windows/macOS toolchain validation.
// No POSIX includes/functions are used unguarded here.
//
// Expected behavior when __linux__ is undefined:
//   Pool::create          -> std::errc::not_supported, capacity() == 0
//   pool_capacity_bytes   -> pure arithmetic, works on every platform
//   default PoolLease     -> invalid()
//   default Pool          -> capacity() == 0, base() == nullptr

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <system_error>

namespace {

int checks = 0;
int failures = 0;
void check(bool ok, const char* label) {
    ++checks;
    if (!ok) { ++failures; std::fprintf(stderr, "FAIL: %s\n", label); }
    else { std::fprintf(stderr, "pass: %s\n", label); }
}

}  // namespace

// Force the non-Linux path: undefine __linux__ before including the Strata
// header.  This simulates a platform where Linux-only types/syscalls are
// unavailable.
#undef __linux__

#include "strata/core/ram_pool.hpp"

using namespace strata::core;

int main() {
    // Pure arithmetic works on every platform.
    uint64_t pool = 0;
    std::error_code ec = pool_capacity_bytes(52ull << 30, 0, 0, pool);
    check(!ec && pool == (52ull << 30), "non-Linux probe: capacity arithmetic works");

    // Pool::create refuses on non-Linux with a clear error.
    ec = {};
    Pool p = Pool::create(4ull << 20, ec);
    check(ec == std::errc::not_supported, "non-Linux probe: create returns not_supported");
    check(p.capacity() == 0, "non-Linux probe: create capacity == 0");
    check(p.base() == nullptr, "non-Linux probe: create base == nullptr");

    // Default PoolLease is invalid.
    PoolLease lease;
    check(!lease.valid(), "non-Linux probe: default lease invalid");
    check(lease.data() == nullptr, "non-Linux probe: default lease data null");
    check(lease.size() == 0, "non-Linux probe: default lease size zero");
    check(lease.offset() == 0, "non-Linux probe: default lease offset zero");

    // Default Pool is empty.
    Pool empty;
    check(empty.capacity() == 0, "non-Linux probe: default pool capacity zero");
    check(empty.base() == nullptr, "non-Linux probe: default pool base null");
    PoolStats s = empty.stats();
    check(s.capacity_bytes == 0 && s.locked_bytes == 0 && s.free_bytes == 0 &&
              s.alloc_count == 0,
          "non-Linux probe: default pool stats all zero");

    if (failures != 0) {
        std::fprintf(stderr, "ram_pool_nonlinux_probe: %d checks, %d FAILURES\n", checks, failures);
        return 1;
    }
    std::fprintf(stderr, "ram_pool_nonlinux_probe: %d checks passed\n", checks);
    return 0;
}
