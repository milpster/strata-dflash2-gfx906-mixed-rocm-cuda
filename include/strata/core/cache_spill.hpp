// include/strata/core/cache_spill.hpp - STRATA_CACHE_SPILL: a layer split's expert cache uses the idle
// stage card's spare VRAM (no GPU needed to check the policy).
//
// WHY THIS EXISTS.  A split gives every stage its own cache on its own GPU, and a stage's cache holds only
// the pairs of the layers that stage executes (`Prefill`/stage fill, src/program/generate.cpp:9061-9115):
// the slots a stage can fill are capped by `room` (its card's free VRAM) AND by its own layers' pair count.
// At `--layer-split 44` on two Radeon VIIs the first stage owns 44 layers and fills its card to ~404 MiB
// free (3695 slots, 5.94 GiB); the second stage owns 4 layers, so its 2048 pairs are filled long before
// its card runs out - ~2.8 GiB of that card's VRAM is never claimed by anything.  Against split 32 that is
// 1350 fewer cache slots: the decode hit rate fell 92-94% -> 82-86% and ~26% of routed experts pay a PCIe
// hop per token (measured 2026-11-10).
//
// The spill is the machinery the engine already has for another card's VRAM - the in-process remote tier
// (`--expert-cache-device1..3`, src/core/remote_experts.cpp:208): a cache on that device, filled from the
// ranked pairs no other cache holds, computing rows there with the SAME kernels the primary's hit path uses
// and returning them through the pinned host rows.  A tier's slots never overlap a stage's (the `claimed`
// bitmap) or the helper proxy's (the `howned` bitmap), so which card serves a routed expert is decided by
// where its blob lives, never by a policy that could reorder a layer's claims: a blob is the same bytes
// wherever it lies, so the rows are byte-identical and only their latency differs.
//
// Unset (the default) keeps every start byte-for-byte as it is: nothing below is read at all.
#pragma once

#include <cstdint>
#include <vector>

namespace strata::core::cache_spill {

/// `STRATA_CACHE_SPILL`: unset or "0" = off (today's start, byte for byte); "1" = spill the split's cache
/// onto the stage card with spare VRAM.  Read once at startup, like every other STRATA_* gate.
inline bool enabled(const char* env) { return env != nullptr && env[0] != '0'; }

/// The card to spill onto: the LAST stage's device - the card holding fewest layers is the one with the
/// headroom (a split's stages are rising layer ranges on rising devices).  -1 when there is no later stage
/// (a split on one GPU, `--split-device 0`), where the spill has no card to spill to.
inline int pick_device(const std::vector<int>& stage_devs) { return stage_devs.empty() ? -1 : stage_devs.back(); }

/// The tier's own guard, stated here so the number the engine promises and the number `RemoteExperts::open`
/// spends are the same rule: a tier fills ranked pairs while `needed + bytes + kTierGuardB <= free`
/// (src/core/remote_experts.cpp:235, the 512 MiB that covers the context, the staging and the driver's
/// later allocations on that card).  `slots` counts the pairs of `ranked` that fit at `blob` each.
inline int64_t slots_within(int64_t free_b, int64_t blob, int64_t ranked_pairs) {
    constexpr int64_t kTierGuardB = 512ll << 20;
    if (blob <= 0 || free_b <= kTierGuardB || ranked_pairs <= 0) return 0;
    const int64_t room = (free_b - kTierGuardB) / blob;
    return room < ranked_pairs ? room : ranked_pairs;
}

}  // namespace strata::core::cache_spill
