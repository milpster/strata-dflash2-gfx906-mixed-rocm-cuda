// include/strata/core/relief_arena_cap.hpp - the arithmetic of --expert-helper-relief-arena-cap-mib.
//
// The expert arena keeps a host copy of EVERY expert the pack holds: 24576 ranked pairs is 46.84 GiB for the
// IQ3_S pack and 39.97 GiB for IQ3_XXS, on the same split and the same slots.  The relief gives back the copies
// the GPU tiers serve (the helper's rows, the spill tier's, the stage caches' hot set), and the rest stays
// resident whatever the machine has.  The cap bounds the WHOLE resident copy: the arena keeps the hottest pairs,
// in the profile's own ranking, up to the cap, and gives the ranked tail back to the OS.  A dropped pair is
// served from the pack files through the relief's existing paths (the staged read of ArenaExpertSource::blob,
// the batched swap-in of ExpertSource::swap_blobs_batch), exactly like the pairs the relief already drops.
//
// This is the plan only: pure host arithmetic, no GPU header, no allocation, no side effect.  The caller owns the
// drop set (set_unfilled) and the registration (register_complement); it feeds in what is ALREADY dropped and
// what the hot budget protects, and gets back the tail to add.
#pragma once

#include <cstdint>
#include <cstddef>
#include <utility>
#include <vector>

namespace strata::core {

/// The pairs to give back and what the arena keeps after it.  `drop` is in the order the plan took them
/// (coldest first); the caller appends it to its drop set.
struct ReliefArenaCapPlan {
    std::vector<std::pair<int32_t, int32_t>> drop;
    uint64_t dropped_bytes = 0;      ///< the bytes of THIS plan (what the caller already dropped is not counted)
    uint64_t resident_after = 0;     ///< the arena's host copies that stay, after the whole drop set
    int64_t kept_pairs = 0;          ///< the pairs inside that resident bytes
    bool capped = false;             ///< false = nothing to do (the cap already fits, or nothing is droppable)
};

/// Plans the tail drop.
///   `arena_total_bytes`: what the arena copies in total (expert_layout().total).
///   `cap_bytes`:        the most the caller wants resident AFTER every drop (the flag's MiB).
///   `profile`:          the ranked pairs, hottest first.  Unranked pairs are colder than every ranked one.
///   `hot`:              1 per (layer*n_expert+expert): the hot budget's pairs.  The cap NEVER drops them:
///                       they are the VRAM tier's backing store, and dropping a hot pair is the prefill
///                       collapse the cold-only stage drop was written to avoid (b749c246c).
///   `dropped`:          1 per pair the caller already gave back (the helper's rows, the spill tier's, the
///                       stage caches' cold set).  Their bytes leave the resident total; they are not dropped twice.
/// `pair_bytes(layer)` is what the arena copies for one pair of that layer (the caller passes
/// expert_layout().blob_bytes(layer), the same measure as `arena_total_bytes`, so the plan's bytes add up
/// exactly).  The caller's hot budget is priced in the cache slot's stride - a larger number for the same
/// pairs - so the cap is conservative against the hot set: the budget protects MORE arena bytes than its
/// own MiB figure.
///
/// The walk is coldest first - the pairs the profile never ranked, in layer order, then the profile read
/// backwards - and stops the moment the resident copy fits the cap, so the hottest pairs are the last to go.
/// Cap 0 takes everything droppable (the hot set stays).  A cap at or above what is resident drops nothing.
template <typename PairBytes>
ReliefArenaCapPlan relief_arena_cap_plan(int64_t n_layers, int64_t n_expert, uint64_t arena_total_bytes,
                                         uint64_t cap_bytes,
                                         const std::vector<std::pair<int32_t, int32_t>>& profile,
                                         const std::vector<uint8_t>& hot,
                                         const std::vector<uint8_t>& dropped,
                                         PairBytes pair_bytes) {
    ReliefArenaCapPlan plan;
    const size_t cells = n_layers < 0 || n_expert <= 0 ? 0 : (size_t) n_layers * (size_t) n_expert;
    std::vector<uint8_t> taken = hot;
    if (taken.size() < cells) taken.resize(cells, 0);
    uint64_t resident = arena_total_bytes;
    int64_t dropped_before = 0;
    auto in_range = [&](const std::pair<int32_t, int32_t>& pr) {
        return pr.first >= 0 && pr.first < n_layers && pr.second >= 0 && pr.second < n_expert;
    };
    for (size_t i = 0; i < cells && i < dropped.size(); ++i) {
        if (!dropped[i]) continue;
        taken[i] = 1;   // never dropped twice
        ++dropped_before;
        const uint64_t b = pair_bytes((int64_t) (i / (size_t) n_expert));
        resident = resident > b ? resident - b : 0;   // the caller's bytes leave the resident total
    }
    plan.kept_pairs = (int64_t) cells - dropped_before;   // (the plan's drop is taken off below)
    if (resident <= cap_bytes) {
        plan.resident_after = resident;
        return plan;
    }
    plan.capped = true;
    auto drop_pair = [&](const std::pair<int32_t, int32_t>& pr) {
        if (!in_range(pr)) return;
        const size_t key = (size_t) pr.first * (size_t) n_expert + (size_t) pr.second;
        if (taken[key]) return;
        const uint64_t b = pair_bytes(pr.first);
        taken[key] = 1;
        resident = resident > b ? resident - b : 0;
        plan.drop.emplace_back(pr.first, pr.second);
        plan.dropped_bytes += b;
    };
    // the pairs the profile never ranked first: they are the coldest the arena holds
    std::vector<uint8_t> ranked(cells, 0);
    for (const auto& pr : profile)
        if (in_range(pr)) ranked[(size_t) pr.first * (size_t) n_expert + (size_t) pr.second] = 1;
    for (int64_t l = 0; l < n_layers && resident > cap_bytes; ++l)
        for (int64_t e = 0; e < n_expert && resident > cap_bytes; ++e) {
            const size_t key = (size_t) l * (size_t) n_expert + (size_t) e;
            if (ranked[key] || taken[key]) continue;
            drop_pair({(int32_t) l, (int32_t) e});
        }
    for (size_t i = profile.size(); i-- > 0 && resident > cap_bytes;)
        drop_pair(profile[i]);
    plan.resident_after = resident;
    plan.kept_pairs -= (int64_t) plan.drop.size();
    return plan;
}

}   // namespace strata::core
