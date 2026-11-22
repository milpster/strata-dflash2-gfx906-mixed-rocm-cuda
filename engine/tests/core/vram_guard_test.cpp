// tests/core/vram_guard_test.cpp - CPU checks for the per-GPU VRAM guard's state machine
// (include/strata/core/vram_guard.hpp + src/core/vram_guard.cpp, rounds 18o/18p): the 64 MiB steps
// on the card that ran low, the never-shrink hysteresis, the growth cap (a non-divisor cap clamps
// the last step instead of overshooting), the step window, the per-card isolation, the env
// activation and the no-reentry contract of the donor (round 18p's N1/N2/N4).  Pure host state -
// no device, no files (the TU makes no CUDA call).
#include "strata/core/vram_guard.hpp"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string>

using namespace strata::core;

static int failures = 0;
static void check(bool ok, const char* what) {
    if (!ok) {
        std::printf("FAIL: %s\n", what);
        ++failures;
    }
}

namespace {

/// a donor that records what it was asked to free and always reports full success.  On card 4 it
/// re-enters the guard the way a real probe in the recall path might: check() must answer
/// nothing-to-do on that thread while the donor runs (N1's no-reentry contract).
int64_t g_donated_mib = 0;
int64_t g_donated_calls = 0;
int64_t g_probe_saw = -2;   // -2: the probe never ran; -1: it saw nothing to do; else the floor it stepped to
int64_t fake_donor(int device, int64_t want_mib) {
    ++g_donated_calls;
    g_donated_mib += want_mib;
    if (device == 4) g_probe_saw = vram_guard::check(4, 35) ? vram_guard::floor_mib(4) : -1;
    return want_mib;
}

void set_env(const char* name, const char* value) {
#ifdef _WIN32
    _putenv((std::string(name) + "=" + value).c_str());
#else
    setenv(name, value, 1);
#endif
}

}   // namespace

int main() {
    // The env activation MUST come first: the guard reads STRATA_VRAM_GUARD once, at the first touch
    // of its config, and the configure() calls every case below use override it afterwards.
    set_env("STRATA_VRAM_GUARD", "1");
    set_env("STRATA_VRAM_GUARD_STEP_MIB", "128");
    check(vram_guard::enabled(), "env: STRATA_VRAM_GUARD=1 switches the guard on");
    vram_guard::reset(2);
    vram_guard::set_base_mib(2, 100);
    check(vram_guard::check(2, 35), "env: a card under the line is still low after the step");
    check(vram_guard::floor_mib(2) == 228 && vram_guard::steps(2) == 1,
          "env: STRATA_VRAM_GUARD_STEP_MIB=128 set the step (100 -> 228)");
    check(g_donated_calls == 0, "env: no donor is registered yet, so nothing was asked");

    // Registered BEFORE the inert case, so "the donor is never asked" can actually fail (round
    // 18p's N4: with the registration after it, the assert was vacuous).
    vram_guard::set_reclaim(fake_donor);

    // 0. Inert unless enabled: a card far under the line reports nothing to do, the floor stays at
    //    the boot reserve, the donor is never asked.
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

    // 3b. A NON-DIVISOR cap clamps the last step (round 18p's N2): 500 + 100 = 600, so the second
    //     step is 36 MiB, not 64 - the floor lands exactly on the cap and the cap line tells the
    //     truth.  The donor is asked for the clamped amount, the real one.
    vram_guard::configure(true, 64, 100, 0);
    vram_guard::reset(3);
    vram_guard::set_base_mib(3, 500);
    const int64_t donated3 = g_donated_mib;
    check(vram_guard::check(3, 35), "n2: the first 64 MiB step is taken");
    check(vram_guard::floor_mib(3) == 564, "n2: 500 -> 564");
    check(vram_guard::check(3, 30), "n2: the clamped step is taken");
    check(vram_guard::floor_mib(3) == 600 && vram_guard::steps(3) == 2,
          "n2: the last step clamps 564 -> 600 = base + max, never over");
    check(g_donated_mib - donated3 == 64 + 36, "n2: the donor was asked for 64 then the clamped 36");
    check(vram_guard::check(3, 30), "n2: a capped card still reports low");
    check(vram_guard::floor_mib(3) == 600 && vram_guard::steps(3) == 2, "n2: the cap holds");

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

    // 7. The donor runs OUTSIDE the guard's locks and its own probe must not recurse (N1): the
    //    fake donor on card 4 calls check() from inside itself; under the old single-lock shape
    //    that re-entered the step mutex (a deadlock), and without the no-reentry flag it would
    //    walk the floor a step per recursion.
    vram_guard::reset(4);
    const int64_t donated4 = g_donated_calls;
    check(vram_guard::check(4, 35), "n1: card 4 under the line is still low after the step");
    check(vram_guard::floor_mib(4) == 64 && vram_guard::steps(4) == 1, "n1: exactly one step on card 4");
    check(g_probe_saw == -1, "n1: the donor's own probe saw nothing to do (check is a no-op inside the donor)");
    check(g_donated_calls - donated4 == 1, "n1: the donor ran once, not once per recursion");

    if (failures == 0) std::printf("vram_guard_test: all checks passed\n");
    return failures == 0 ? 0 : 1;
}
