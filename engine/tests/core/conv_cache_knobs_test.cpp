// tests/core/conv_cache_knobs_test.cpp - CPU checks for the round-13 conversation-cache knobs
// (include/strata/program/conv_cache.hpp): the end-of-generation anchor decision behind
// STRATA_GEN_ANCHOR (round 15: its STRATA_GEN_ANCHOR_MIN_TOKENS context gate) and the idle-park
// debounce arithmetic behind STRATA_PARK_DEBOUNCE_MS.
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

    // the gate's range endpoints (gen_anchor_min_tokens's lo, default and hi), used by the walks below
    const int64_t min_tokens_i[3] = {1024, 32768, 1048576};

    // 2. Default-off equivalence, exhaustive: with the knob off the capture decision is false for
    //    EVERY combination of the serve loop's own conditions - the request end is unchanged.
    //    The gate term is walked too: whatever STRATA_GEN_ANCHOR_MIN_TOKENS resolves to, the knob
    //    off decides everything.
    for (int min_i = 0; min_i <= 2; ++min_i)            // gen_anchor_min_tokens's range: lo, default, hi
        for (int cancelled = 0; cancelled <= 1; ++cancelled)
            for (int live_ok = 0; live_ok <= 1; ++live_ok)
                for (int cache = 0; cache <= 1; ++cache)      // o.prompt_cache > 0
                    for (int disk = 0; disk <= 1; ++disk)     // the disk tier on
                        for (int at_t = 0; at_t <= 1; ++at_t) // a checkpoint already at T
                            for (int64_t T = 0; T <= 2; T += 2)
                                check(!gen_anchor_should_capture(false, cancelled != 0, live_ok != 0, cache, disk != 0,
                                                                 T, at_t != 0, min_tokens_i[min_i]),
                                      "knob off: no capture whatever the serve loop state or the gate");

    // 3. The on-path decision: every condition true captures; each condition false alone refuses.
    //    min_tokens 0 is the ungated decision (the gate must not change it at all).
    check(gen_anchor_should_capture(true, false, true, 6, true, 142815, false, 0),
          "all conditions true: the anchor captures");
    check(!gen_anchor_should_capture(true, true, true, 6, true, 142815, false, 0),
          "a cancelled request is not a verified boundary");
    check(!gen_anchor_should_capture(true, false, false, 6, true, 142815, false, 0),
          "live_ok false: the session is not this conversation");
    check(!gen_anchor_should_capture(true, false, true, 0, true, 142815, false, 0),
          "prompt cache off: no checkpoint chain to add to");
    check(!gen_anchor_should_capture(true, false, true, 6, false, 142815, false, 0),
          "disk tier off: no park to publish the anchor, no sidecar for the pair");
    check(!gen_anchor_should_capture(true, false, true, 6, true, 0, false, 0),
          "an empty conversation has no position to anchor");
    check(!gen_anchor_should_capture(true, false, true, 6, true, 142815, true, 0),
          "a checkpoint already at T IS the anchor: the capture would only churn the chain");

    // 3b. STRATA_GEN_ANCHOR_MIN_TOKENS parsing: absent, empty, "0", negative and garbage are the
    //     default 32768 (0 is NOT a gate - it would re-collapse the small-context arm); a value in
    //     range is itself; below 1024 clamps up, above 1048576 clamps down.
    check(gen_anchor_min_tokens(nullptr) == 32768, "no env: the gate is the 32768 default");
    check(gen_anchor_min_tokens("") == 32768, "empty env: the gate is the 32768 default");
    check(gen_anchor_min_tokens("0") == 32768, "\"0\": 0 is not a gate, the default stands");
    check(gen_anchor_min_tokens("-4096") == 32768, "negative: the default stands");
    check(gen_anchor_min_tokens("garbage") == 32768, "garbage env: the default stands");
    check(gen_anchor_min_tokens("32768") == 32768, "\"32768\": the default, spelled out");
    check(gen_anchor_min_tokens("65536") == 65536, "\"65536\": an in-range gate");
    check(gen_anchor_min_tokens("1048576") == 1048576, "\"1048576\": the top of the range");
    check(gen_anchor_min_tokens("1") == 1024, "below the range clamps up to 1024");
    check(gen_anchor_min_tokens("1024") == 1024, "\"1024\": the bottom of the range");
    check(gen_anchor_min_tokens("1023") == 1024, "one under the bottom clamps up to 1024");
    check(gen_anchor_min_tokens("1048577") == 1048576, "one past the top clamps down to 1048576");
    check(gen_anchor_min_tokens("99999999999") == 1048576, "an absurd value clamps down to 1048576");

    // 3c. The gate inside the decision: at or above the gate the decision is exactly the ungated
    //     one for EVERY serve-loop combination (the long-context arm keeps its round-13 win); below
    //     the gate a small conversation captures nothing even with the anchor on (the W3/W5
    //     small-context collapse cannot happen); the boundary is inclusive (T == gate captures).
    for (int cancelled = 0; cancelled <= 1; ++cancelled)
        for (int live_ok = 0; live_ok <= 1; ++live_ok)
            for (int cache = 0; cache <= 1; ++cache)
                for (int disk = 0; disk <= 1; ++disk)
                    for (int at_t = 0; at_t <= 1; ++at_t)
                        for (int64_t T = 32768; T <= 142815; T += 52137)   // at, partway, past the gate
                            check(gen_anchor_should_capture(true, cancelled != 0, live_ok != 0, cache, disk != 0,
                                                            T, at_t != 0, 32768) ==
                                      gen_anchor_should_capture(true, cancelled != 0, live_ok != 0, cache, disk != 0,
                                                                T, at_t != 0, 0),
                                  "at/above the gate: the decision is the ungated one, every condition");
    check(gen_anchor_should_capture(true, false, true, 6, true, 2048, false, 32768) == false,
          "the 2k warm-TG shape (2048) captures nothing at the default gate");
    check(gen_anchor_should_capture(true, false, true, 6, true, 32000, false, 32768) == false,
          "32000 is below the default gate: no anchor");
    check(gen_anchor_should_capture(true, false, true, 6, true, 32767, false, 32768) == false,
          "one token under the gate: no anchor");
    check(gen_anchor_should_capture(true, false, true, 6, true, 32768, false, 32768),
          "T == gate: the boundary is inclusive, the anchor captures");
    check(gen_anchor_should_capture(true, false, true, 6, true, 32769, false, 32768),
          "one token past the gate: the anchor captures");
    check(gen_anchor_should_capture(true, false, true, 6, true, 142815, false, 32768),
          "killerL's 142815: the long-ctx anchor keeps its win at the default gate");
    check(gen_anchor_should_capture(true, false, true, 6, true, 160000, false, 32768),
          "longtg's 160k: the long-ctx anchor keeps its win at the default gate");
    check(gen_anchor_should_capture(true, false, true, 6, true, 2048, false, 1024),
          "a lowered gate (1024) anchors the 2k shape: the arm is measurable both ways");
    check(gen_anchor_should_capture(true, false, true, 6, true, 2048, false, 1048576) == false,
          "a raised gate (1048576) anchors nothing at 2k");

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
