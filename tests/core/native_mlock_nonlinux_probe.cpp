// tests/core/native_mlock_nonlinux_probe.cpp - Simulated non-Linux guard probe.
//
// Compiled with -U__linux__ to verify the __linux__-guarded code paths degrade
// to a self-contained OFF/no-op contract on platforms where Linux types and
// syscalls (strcmp on env, rlimit/getrlimit/setrlimit, mlock, errno) do not
// exist.  This is a simulated preprocessor coverage check on a Linux host with
// a CPU-only compiler; it is NOT a real Windows/macOS toolchain validation.
// No POSIX includes/functions are used unguarded here.
//
// Expected behavior when __linux__ is undefined:
//   native_mlock_detail::on()  -> false
//   native_mlock_span()        -> all-zero Result, complete=true, no syscalls
//   native_mlock_report()      -> no output, no crash
//   raise_limit_once()         -> no-op (not called by the no-op span path)
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace {

int checks = 0;
int failures = 0;
void check(bool ok, const char* label) {
    ++checks;
    if (!ok) { ++failures; std::fprintf(stderr, "FAIL: %s\n", label); }
    else { std::fprintf(stderr, "pass: %s\n", label); }
}

}  // namespace

// Force the non-Linux path: undefine __linux__ before including the Strata header.
// This simulates a platform where Linux-only types/syscalls are unavailable.
#undef __linux__

#include "strata/core/pin_budget.hpp"

using namespace strata::core;

int main() {
    // on() must return false without touching the environment or strcmp
    check(native_mlock_detail::on() == false, "non-Linux probe: on() == false");

    // native_mlock_span with the knob off is an all-zero, complete Result
    native_mlock_detail::Result r = native_mlock_span(nullptr, 0, "probe");
    check(r.requested == 0, "non-Linux probe: requested == 0");
    check(r.locked == 0, "non-Linux probe: locked == 0");
    check(r.failed == 0, "non-Linux probe: failed == 0");
    check(r.complete == true, "non-Linux probe: complete == true");
    check(r.seconds == 0.0, "non-Linux probe: seconds == 0");

    // native_mlock_report must not crash or print
    native_mlock_report("probe");

    if (failures != 0) {
        std::fprintf(stderr, "native_mlock_nonlinux_probe: %d checks, %d FAILURES\n", checks, failures);
        return 1;
    }
    std::fprintf(stderr, "native_mlock_nonlinux_probe: %d checks passed\n", checks);
    return 0;
}
