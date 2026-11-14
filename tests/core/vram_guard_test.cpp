// tests/core/vram_guard_test.cpp - CPU checks for the per-GPU VRAM guard's state machine
// (include/strata/core/vram_guard.hpp + src/core/vram_guard.cpp, round 18o): the 64 MiB steps
// on the card that ran low, the never-shrink hysteresis, the growth cap, the step window and
// the per-card isolation.  Pure host state - no device, no files (the TU makes no CUDA call).
#include "strata/core/vram_guard.hpp"

#include <cstdint>
#include <cstdio>

using namespace strata::core;

static int failures = 0;
static void check(bool ok, const char* what) {
    if (!ok) {
        std::printf("FAIL: %s\n", what);
        ++failures;
    }
}

namespace {

/// a donor that records what it was asked to free and always reports full success
int64_t g_donated_mib = 0;
int64_t g_donated_calls = 0;
int64_t fake_donor(int device, int64_t want_mib) {
    (void) device;
    ++g_donated_calls;
    g_donated_mib += want_mib;
    return want_mib;
}

}   // namespace

int main() {
    // 0. Inert unless STRATA_VRAM_GUARD=1: a card far under the line reports nothing to do,
    //    the floor stays at the boot reserve, the donor is never asked.
    vram_guard::reset(0);
    vram_guard::configure(false, 64, 4096, 0);
    vram_guard::set_base_mib(0, 700);
    check(!vram_guard::enabled(), "off: enabled() is false");
    check(!vram_guard::check(0, 35), "off: a card under the line reports nothing to do");
    check(vram_guard::floor_mib(0) == 700, "off: the floor stays at the boot reserve");
    check(vram_guard::steps(0) == 0, "off: no step");
    check(g_donated_calls == 0, "off: the donor is never asked");

    // 1. The 64 MiB steps on the card that ran low (the prod shape: 35 MiB free on a 700 MiB
    //    reserve), each step asking the donor for the same amount.
    vram_guard::reset(0);
    vram_guard::configure(true, 64, 4096, 0);
    vram_guard::set_reclaim(fake_donor);
    vram_guard::set_base_mib(0, 700);
    check(vram_guard::enabled(), "on: enabled() is true");
    const int64_t donated0 = g_donated_mib;
    check(vram_guard::check(0, 35), "on: a card under the line is still low after the step");
    check(vram_guard::floor_mib(0) == 764, "the floor steps 700 -> 764 (exactly one 64 MiB step)");
    check(vram_guard::steps(0) == 1, "one step counted");
    check(g_donated_mib - donated0 == 64, "the donor was asked for the same 64 MiB");
    check(vram_guard::base_mib(0) == 700, "the boot floor is what the cap counts from");

    // 2. Hysteresis: a recovered card neither steps nor gives anything back, and the next dip
    //    steps again from where the floor stands.
    check(!vram_guard::check(0, 2048), "a card over the line reports nothing to do");
    check(vram_guard::floor_mib(0) == 764, "a recovered card keeps its floor (never shrinks)");
    check(vram_guard::check(0, 40), "a card that dips again is still low");
    check(vram_guard::floor_mib(0) == 828 && vram_guard::steps(0) == 2, "the next dip steps 764 -> 828");

    // 3. The cap: growth stops at exactly base + max (700 + 192 = 892) and further low reports
    //    change nothing.
    vram_guard::configure(true, 64, 192, 0);
    check(vram_guard::check(0, 35), "the step to the cap is taken");
    check(vram_guard::floor_mib(0) == 892 && vram_guard::steps(0) == 3, "the floor reaches base + max = 892");
    const int64_t donated_at_cap = g_donated_mib;
    check(vram_guard::check(0, 30), "a capped card still reports low (the caller keeps its warning)");
    check(vram_guard::floor_mib(0) == 892 && vram_guard::steps(0) == 3, "the cap holds the floor and the count");
    check(g_donated_mib == donated_at_cap, "the capped step asks the donor for nothing");

    // 4. The step window: a low card that stepped just now waits one window before the next
    //    step, so a tight check loop cannot walk the floor to the cap in seconds.
    vram_guard::configure(true, 64, 4096, 1000000000);
    check(vram_guard::check(0, 30), "a recent step still reports low");
    check(vram_guard::floor_mib(0) == 892 && vram_guard::steps(0) == 3, "no second step inside the window");

    // 5. Per-card: card 1's dip steps card 1 only - the mandate is "on the gpu it happened at".
    vram_guard::reset(1);
    vram_guard::set_base_mib(1, 300);
    check(vram_guard::check(1, 20), "card 1 under the line is still low after its own step");
    check(vram_guard::floor_mib(1) == 364 && vram_guard::steps(1) == 1, "card 1 steps 300 -> 364");
    check(vram_guard::floor_mib(0) == 892 && vram_guard::steps(0) == 3, "card 0's floor and count are untouched");
    vram_guard::set_base_mib(1, 500);
    check(vram_guard::floor_mib(1) == 500, "a rising boot floor lifts the floor");
    vram_guard::configure(true, 64, 4096, 0);
    check(vram_guard::check(1, 20), "card 1 steps again once the window allows");
    check(vram_guard::floor_mib(1) == 564, "the lifted floor steps 500 -> 564");

    // 6. Out-of-range devices are ignored, not indexed.
    check(!vram_guard::check(-1, 5), "a negative device index is ignored");
    check(!vram_guard::check(16, 5), "a device past the table is ignored");
    check(vram_guard::floor_mib(-1) == 0 && vram_guard::floor_mib(16) == 0, "out-of-range reads answer 0");

    if (failures == 0) std::printf("vram_guard_test: all checks passed\n");
    return failures == 0 ? 0 : 1;
}
