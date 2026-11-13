// include/strata/core/tail_skip.hpp - STRATA_ROUTE_TAIL_SKIP's gate, honest about helper rigs.
//
// The skip (expert_pool_dispatch_multi) zeroes a missed expert that EVERY token of a verify
// window routes at rank >= R: no copy, no compute, no renormalisation.  Until v0233 the gate
// hard-disabled the loop whenever a helper/remote expert tier was registered
// (`d.remote_count == 0`), because the blanket skip would also zero rows the helper holds - so
// on helper rigs the skip was silently INERT all of v0232 while the serve banner said ON (the
// "route tail skip" counter never moved; ab9 window, 2026-11-13).
//
// STRATA_TAIL_SKIP_HELPER (default off) restores the skip per entry on such rigs: only the
// misses NO tier holds - the ones the plan leaves to the CPU - are skipped, exactly the set a
// rig without a helper skips.  A helper-held row is never skipped: RemoteExperts::begin claims
// only what it holds, so a skipped row could not reach the helper anyway - the skip loop and
// the claim rule read the same holds().  With the knob absent every rig keeps today's bits:
// no helper, unchanged; helper, still suspended.  A peer tier keeps suspending the skip with
// or without the knob (its rows would be zeroed; unsupported, as before).
#pragma once

#include <cstdlib>

namespace strata::core {

/// STRATA_TAIL_SKIP_HELPER's value: any nonzero opts in, unset/empty/0 stays out (the
/// STRATA_DEC_BATCH shape - there is no negative semantics).
inline bool tail_skip_helper(const char* env) { return env != nullptr && env[0] != '\0' && std::atoi(env) != 0; }

/// Whether the dispatch's tail-skip loop runs at all: the rank is on, no peer tier is
/// registered, and either no helper/remote tier is registered or STRATA_TAIL_SKIP_HELPER opted
/// the per-entry skip in.  Pure, and the ONE decision both the skip loop and the serve banner
/// call, so the banner cannot claim ON over a suspended skip again.
inline bool tail_skip_gate(int tail_rank, bool peer, int remote_count, const char* helper_env) {
    return tail_rank > 0 && !peer && (remote_count == 0 || tail_skip_helper(helper_env));
}

}  // namespace strata::core
