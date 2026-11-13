// tests/core/conv_cache_knobs_test.cpp - CPU checks for the round-13 conversation-cache knobs
// (include/strata/program/conv_cache.hpp): the end-of-generation anchor decision behind
// STRATA_GEN_ANCHOR and the idle-park debounce arithmetic behind STRATA_PARK_DEBOUNCE_MS.
// Pure host arithmetic, no device, no files, no links.
//
// The shapes are the measured prod ones (round 12): the killerL continuation re-reads a 535-token
// generated tail at 9.6-16.6 ms/tok because the deepest published anchor (142280) sits at the last
// prompt-path checkpoint of a 142815-token conversation; the disk park re-streams 2770 MiB per
// idle boundary.  Default-off equivalence is checked EXHAUSTIVELY over the boolean conditions: with
// the knob off no combination of serve-loop state changes any decision.
#include "strata/program/conv_cache.hpp"

#include <cstdint>
#include <cstdio>

using namespace strata::program::conv_cache;

static int failures = 0;
static void check(bool ok, const char* what) {
    if (!ok) {
        std::printf("FAIL: %s\n", what);
        ++failures;
    }
}

int main() {
    // 1. STRATA_GEN_ANCHOR parsing: absent, empty, "0" and garbage are OFF (the default path,
    //    bit-for-bit); any nonzero integer is ON.
    check(!gen_anchor_on(nullptr), "no env: the anchor knob is off");
    check(!gen_anchor_on(""), "empty env: the anchor knob is off");
    check(!gen_anchor_on("0"), "\"0\": the anchor knob is off");
    check(!gen_anchor_on("garbage"), "garbage env: the anchor knob is off");
    check(gen_anchor_on("1"), "\"1\": the anchor knob is on");
    check(gen_anchor_on("2"), "\"2\": the anchor knob is on (any nonzero integer)");
    check(gen_anchor_on("-1"), "\"-1\": the anchor knob is on (any nonzero integer)");

    // 2. Default-off equivalence, exhaustive: with the knob off the capture decision is false for
    //    EVERY combination of the serve loop's own conditions - the request end is unchanged.
    for (int cancelled = 0; cancelled <= 1; ++cancelled)
        for (int live_ok = 0; live_ok <= 1; ++live_ok)
            for (int cache = 0; cache <= 1; ++cache)      // o.prompt_cache > 0
                for (int disk = 0; disk <= 1; ++disk)     // the disk tier on
                    for (int at_t = 0; at_t <= 1; ++at_t) // a checkpoint already at T
                        for (int64_t T = 0; T <= 2; T += 2)
                            check(!gen_anchor_should_capture(false, cancelled != 0, live_ok != 0, cache, disk != 0,
                                                             T, at_t != 0),
                                  "knob off: no capture whatever the serve loop state");

    // 3. The on-path decision: every condition true captures; each condition false alone refuses.
    check(gen_anchor_should_capture(true, false, true, 6, true, 142815, false),
          "all conditions true: the anchor captures");
    check(!gen_anchor_should_capture(true, true, true, 6, true, 142815, false),
          "a cancelled request is not a verified boundary");
    check(!gen_anchor_should_capture(true, false, false, 6, true, 142815, false),
          "live_ok false: the session is not this conversation");
    check(!gen_anchor_should_capture(true, false, true, 0, true, 142815, false),
          "prompt cache off: no checkpoint chain to add to");
    check(!gen_anchor_should_capture(true, false, true, 6, false, 142815, false),
          "disk tier off: no park to publish the anchor, no sidecar for the pair");
    check(!gen_anchor_should_capture(true, false, true, 6, true, 0, false),
          "an empty conversation has no position to anchor");
    check(!gen_anchor_should_capture(true, false, true, 6, true, 142815, true),
          "a checkpoint already at T IS the anchor: the capture would only churn the chain");

    // 4. The tail arithmetic, the measured shapes: 535 generated tokens past the deepest prompt
    //    anchor at killerL (142815 = 142280 + 535), 215 at longtg's 29k; nothing to save when the
    //    deepest anchor already sits at T or past it (a rewind cannot go negative).
    check(gen_anchor_saved_tokens(142815, 142280) == 535, "killerL: the anchor covers the 535-token tail");
    check(gen_anchor_saved_tokens(29345, 29130) == 215, "longtg: the anchor covers the 215-token tail");
    check(gen_anchor_saved_tokens(142815, 142815) == 0, "the anchor at T saves nothing more");
    check(gen_anchor_saved_tokens(142280, 142815) == 0, "a deeper anchor than T (a rewind) saves nothing");

    // 5. STRATA_PARK_DEBOUNCE_MS parsing: absent, empty, "0", negative and garbage are 0 (off, the
    //    default path bit-for-bit); a plain positive number is the window; ten minutes is the clamp.
    check(park_debounce_ms(nullptr) == 0, "no env: the debounce is off");
    check(park_debounce_ms("") == 0, "empty env: the debounce is off");
    check(park_debounce_ms("0") == 0, "\"0\": the debounce is off");
    check(park_debounce_ms("-1500") == 0, "negative: the debounce is off");
    check(park_debounce_ms("garbage") == 0, "garbage env: the debounce is off");
    check(park_debounce_ms("1") == 1, "\"1\": a 1 ms window");
    check(park_debounce_ms("1500") == 1500, "\"1500\": the round-13 GPU-window arm");
    check(park_debounce_ms("600000") == 600000, "ten minutes is inside the clamp");
    check(park_debounce_ms("600001") == 600000, "past ten minutes clamps to it");
    check(park_debounce_ms("99999999999") == 600000, "an absurd value clamps to ten minutes");

    // 6. Default-off equivalence, exhaustive over the clock terms: debounce 0 never skips, whatever
    //    the stamps are - the park path is unchanged with the knob unset.
    for (int64_t now = 0; now <= 4000; now += 1000)
        for (int64_t last = -1; last <= 3000; last += 1000)
            check(!park_debounced(0, now, last), "debounce 0: a park is never skipped");

    // 7. The window is [0, N): the recipe's 1500 ms skips a park 1499 ms after the last write,
    //    takes one exactly 1500 ms after, and the first park of a process (no last write) always
    //    goes through - a fresh boot still publishes.
    check(park_debounced(1500, 8499, 7000), "1499 ms since the last park: skipped");
    check(!park_debounced(1500, 8500, 7000), "exactly 1500 ms since the last park: parks");
    check(!park_debounced(1500, 8501, 7000), "1501 ms since the last park: parks");
    check(!park_debounced(1500, 0, -1), "no park yet: the first park always goes through");
    check(park_debounced(1500, 7000, 7000), "0 ms since the write (same clock sample): inside the window");
    check(park_debounced(1500, 7001, 7000), "1 ms after a park: skipped (the mid-burst case)");

    if (failures == 0) std::printf("conv_cache_knobs_test: all checks passed\n");
    return failures == 0 ? 0 : 1;
}
