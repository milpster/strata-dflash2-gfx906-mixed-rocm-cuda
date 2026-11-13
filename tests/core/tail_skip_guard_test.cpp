// tests/core/tail_skip_guard_test.cpp - CPU checks for the STRATA_ROUTE_TAIL_SKIP gate
// (include/strata/core/tail_skip.hpp): STRATA_TAIL_SKIP_HELPER's parsing, and the one gate
// decision the dispatch's skip loop and the serve banner share - the v0233 fix for the skip's
// silent suspension on helper rigs (the banner said ON while d.remote_count > 0 hard-disabled
// the loop, so the "route tail skip" counter never moved).  The default-off equivalence is
// walked exhaustively against the pre-v0233 rule, so a rig without the knob keeps today's
// bits.  CPU-only BY CONSTRUCTION - header-only decisions, the test links NO library at all.
#include "strata/core/tail_skip.hpp"

#include <cstdio>

using namespace strata::core;

static int failures = 0;
static void check(bool ok, const char* what) {
    if (!ok) {
        std::printf("FAIL: %s\n", what);
        ++failures;
    }
}

/// The pre-v0233 rule the dispatch applied inline (expert_source.cpp before the tail_skip_gate
/// call): the skip ran only with no peer tier and NO registered helper/remote tier at all -
/// spelled out here so the equivalence walk below checks against the OLD rule, not a
/// restatement of the new one.
static bool legacy_gate(int tail_rank, bool peer, int remote_count) {
    return tail_rank > 0 && !peer && remote_count == 0;
}

int main() {
    // 1. STRATA_TAIL_SKIP_HELPER parsing: unset, empty, "0" and unparsable stay OUT; any
    //    nonzero opts in (the STRATA_DEC_BATCH shape - no negative semantics).
    check(!tail_skip_helper(nullptr), "no env: the helper opt-in is off");
    check(!tail_skip_helper(""), "empty env: the helper opt-in is off");
    check(!tail_skip_helper("0"), "\"0\": off, spelled out");
    check(tail_skip_helper("1"), "\"1\": opted in");
    check(tail_skip_helper("2"), "\"2\": any nonzero opts in");
    check(tail_skip_helper("-1"), "\"-1\": nonzero, opted in");
    check(!tail_skip_helper("garbage"), "garbage: off");

    // 2. Default equivalence: with the knob ABSENT (or explicitly off) the gate is the
    //    pre-v0233 rule for every rank/peer/remote combination - a helper rig keeps the skip
    //    suspended exactly as v0232 did, a rig without a helper is unchanged bit for bit.
    const char* const knob_off[] = {nullptr, "", "0"};
    for (int rank = 0; rank <= 9; ++rank)
        for (int peer = 0; peer <= 1; ++peer)
            for (int remote = 0; remote <= 2; ++remote)
                for (const char* env : knob_off) {
                    char what[128];
                    std::snprintf(what, sizeof what, "knob off (rank %d, peer %d, remote %d): today's gate", rank, peer, remote);
                    check(tail_skip_gate(rank, peer != 0, remote, env) == legacy_gate(rank, peer != 0, remote), what);
                }

    // 3. The knob opens exactly the helper case: with it on, the gate is rank > 0 with no peer
    //    tier - remote_count no longer matters (the skip then holds per entry: a row a tier
    //    holds is the helper's to compute and is never skipped).
    for (int rank = 0; rank <= 9; ++rank)
        for (int peer = 0; peer <= 1; ++peer)
            for (int remote = 0; remote <= 2; ++remote) {
                char what[128];
                std::snprintf(what, sizeof what, "knob on (rank %d, peer %d, remote %d): rank on, peer off", rank, peer, remote);
                check(tail_skip_gate(rank, peer != 0, remote, "1") == (rank > 0 && peer == 0), what);
            }

    // 4. The two suspensions the banner names: a peer tier suspends the skip regardless of the
    //    knob (its rows would be zeroed - still unsupported); a helper tier suspends it only
    //    without the knob (v0232's silent state on the ab9 rig).
    check(!tail_skip_gate(7, true, 0, "1"), "a peer tier suspends the skip even with the knob");
    check(!tail_skip_gate(7, false, 1, nullptr), "a helper tier suspends the skip without the knob (v0232's silent state)");
    check(tail_skip_gate(7, false, 1, "1"), "a helper rig with the knob: the skip runs (per entry - only misses no tier holds)");
    check(tail_skip_gate(7, false, 2, "1"), "two remote tiers with the knob: still per entry");
    check(!tail_skip_gate(0, false, 0, "1"), "rank 0 (STRATA_ROUTE_TAIL_SKIP=0): nothing runs, knob or not");

    if (failures == 0) std::printf("tail_skip_guard_test: all checks passed\n");
    return failures == 0 ? 0 : 1;
}
