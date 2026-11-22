// tests/core/expert_helper_reserve_test.cpp - CPU checks for the v0232 honest reserve floor
// (include/strata/core/expert_helper_reserve.hpp): the clamp range the CUDA helper's
// --reserve-mib resolves to, the measured shapes it exists for, and the boundary arithmetic the
// startup warning keys on.  The helper's parse() itself is nvcc-only (cuda_runtime.h), so the
// DECISION is pinned here in the pure header both the helper and this test compile - the helper
// applies clamp_reserve_mib(given) exactly once, right after its own >= 0 validation.
// Pure host arithmetic, no device, no files, no links.
#include "strata/core/expert_helper_reserve.hpp"

#include <cstdint>
#include <cstdio>

using namespace strata::core::expert_helper;

static int failures = 0;
static void check(bool ok, const char* what) {
    if (!ok) {
        std::printf("FAIL: %s\n", what);
        ++failures;
    }
}

int main() {
    // 1. The range: the operator's honest floor (64, replacing the effective engine-side 512 that
    //    silently won before v0232) and the ceiling that keeps a typo from emptying the fill.
    check(kReserveFloorMiB == 64, "the floor is 64 MiB (the honest floor, testable)");
    check(kReserveCeilMiB == 4096, "the ceiling is 4096 MiB");
    check(kReserveWarnBelowMiB == 256, "the below-256 warning threshold");
    check(kReserveFloorMiB < kReserveWarnBelowMiB && kReserveWarnBelowMiB < kReserveCeilMiB,
          "floor < warn threshold < ceiling");

    // 2. The clamp: in-range values pass through untouched (including the 512 default and the
    //    launcher's old 300/320 - both now BIND, which is the point of the change), out-of-range
    //    values land on the nearest end.
    const int64_t in_range[] = {64, 100, 128, 200, 256, 300, 320, 512, 1024, 2048, 4096};
    for (int64_t v : in_range)
        check(clamp_reserve_mib(v) == v, "in-range reserve passes through");
    check(clamp_reserve_mib(0) == 64, "0 clamps up to the floor (parse's >= 0 keeps it a number)");
    check(clamp_reserve_mib(1) == 64, "1 clamps up to the floor");
    check(clamp_reserve_mib(63) == 64, "63 clamps up to the floor");
    check(clamp_reserve_mib(65) == 65, "65 is in range");
    check(clamp_reserve_mib(4097) == 4096, "4097 clamps down to the ceiling");
    check(clamp_reserve_mib(51200) == 4096, "a fat-fingered 51200 clamps to the ceiling");
    check(clamp_reserve_mib(8796093022207) == 4096, "kMaxMiB-scale values clamp to the ceiling");

    // 3. The measured shapes (round-14/16 journal): the preflight showed "reserve 512" for
    //    --reserve-mib 300 and 320 because the option was dead and the engine's hello default won.
    //    After v0232 the SAME launch lines resolve to 300 and 320 - the honest floor is testable -
    //    and the launcher's raised default keeps today's effective 512.
    check(clamp_reserve_mib(300) == 300, "the launcher's old 300 default now binds (was: dead, 512 won)");
    check(clamp_reserve_mib(320) == 320, "the measured 320 arm now binds");
    check(clamp_reserve_mib(512) == 512, "the launcher's new default (and the option's own) stays 512");
    check(clamp_reserve_mib(64) == 64, "the operator's floor probe binds at exactly 64");

    // 4. The warning predicate the startup line keys on (printed once by main, after the clamp):
    //    warn for every final value under 256 - the clamped floor included - and for nothing else.
    for (int64_t v : {64, 100, 128, 200, 255})
        check(clamp_reserve_mib(v) < kReserveWarnBelowMiB, "a final reserve under 256 warns");
    for (int64_t v : {256, 257, 300, 512, 4096})
        check(!(clamp_reserve_mib(v) < kReserveWarnBelowMiB), "256 and up stay quiet");
    check(clamp_reserve_mib(255) == 255 && clamp_reserve_mib(255) < kReserveWarnBelowMiB,
          "the 255 boundary warns (the threshold is exclusive)");

    if (failures == 0) std::printf("expert_helper_reserve_test: all checks passed\n");
    return failures == 0 ? 0 : 1;
}
