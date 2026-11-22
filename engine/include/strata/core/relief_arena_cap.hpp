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

#include <algorithm>
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
    int64_t decode_pairs = 0;        ///< the pairs the decode budget protected (never dropped)
    uint64_t decode_bytes = 0;       ///< their bytes, inside resident_after
    bool capped = false;             ///< false = nothing to do (the cap already fits, or nothing is droppable)
    bool short_of_cap = false;       ///< true = everything droppable was dropped and the cap was still not reached
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
/// `decode_hot_bytes` is the SECOND keep budget - a FLOOR on the arena's copies the DECODE path pays for,
/// in effect when the cap is tightened below what decode needs.  Decode's working set is everything outside
/// the tiers (the adaptive tier's swap-in candidates, the CPU pool's experts): on iq3_s that is 24,576 -
/// 7,858 cache-resident - 4,615 helper-mirrored = 12,103 pairs, of which a cap 20000 / hot 15000 leaves
/// 9,083 file-served - and that is the measured decode tax (longtg 35.3 -> 21.0, tg1024 37.8 -> 28.1,
/// 8k PP ~200-230 -> ~128).  Note the cap alone keeps the hottest of them: a coldest-first drop keeps
/// HOTTEST-first, so at a cap that fits above the hot set this budget protects the same pairs the cap was
/// going to keep and changes nothing.  It bites when `hot_bytes + decode_hot_bytes > cap_bytes`: the cap's
/// own keep is shorter than the decode band, so the floor extends residency to the hot set plus the
/// `decode_hot_bytes` prefix (resident_after lands there, above the cap, reported `short_of_cap`) instead
/// of letting the cap eat into the pairs decode pays for.  0 = today's uniform cap.
///
/// The walk is coldest first - the pairs the profile never ranked, in layer order, then the profile read
/// backwards - and stops the moment the resident copy fits the cap, so the hottest pairs are the last to go.
/// Cap 0 takes everything droppable (the hot set and the decode budget stay).  A cap at or above what is
/// resident drops nothing.  When the two budgets together leave less than the cap's shortfall, the plan drops
/// every droppable pair, says so (`short_of_cap`) and reports where it landed.
template <typename PairBytes>
ReliefArenaCapPlan relief_arena_cap_plan(int64_t n_layers, int64_t n_expert, uint64_t arena_total_bytes,
                                         uint64_t cap_bytes,
                                         const std::vector<std::pair<int32_t, int32_t>>& profile,
                                         const std::vector<uint8_t>& hot,
                                         const std::vector<uint8_t>& dropped,
                                         PairBytes pair_bytes, uint64_t decode_hot_bytes = 0) {
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
    // The decode budget claims its pairs FIRST, from the profile's hot end: these are the arena's copies
    // decode pays for - the adaptive tier's swap-in candidates and the CPU pool's experts, i.e. the pairs
    // just outside the caches and the helper's mirror.  They are claimed before the drop walk, so the cap's
    // bytes come off what is left: the prefill-cold tail.  A contiguous hot prefix - the first pair that
    // would overflow the budget stops it.
    uint64_t decode_budget = 0;
    for (const auto& pr : profile) {
        if (decode_budget >= decode_hot_bytes) break;
        if (!in_range(pr)) continue;
        const size_t key = (size_t) pr.first * (size_t) n_expert + (size_t) pr.second;
        if (taken[key]) continue;   // the hot set and the caller's drops are decided already
        const uint64_t b = pair_bytes(pr.first);
        if (decode_budget + b > decode_hot_bytes) break;
        taken[key] = 1;
        decode_budget += b;
        ++plan.decode_pairs;
        plan.decode_bytes += b;
    }
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
    plan.short_of_cap = resident > cap_bytes;   // the budgets left less than the cap's shortfall
    return plan;
}

/// The runtime re-rank of the arena's keep set by DECODE heat.
///
/// WHY.  The cap above decides the keep set at BOOT from the profile - a PROMPT-trace ranking (tools/make_profile.py).
/// Decode wants a different set: the pairs a decode walk touches and the dropped pairs a decode walk keeps pulling
/// back from the pack files.  A cap that keeps the profile's hot pairs keeps the WRONG pairs for decode.  Measured on
/// iq3_s s34 (v0222, cap 20000): residency 20k pays the pre-diet decode band ONLY where the kept pairs are the ones
/// decode touches; at 29,999 residency (full pre-diet band) longtg hits 35.6/35.1 and tg1024 42.0, but at 19,998 the
/// same model decodes 27.4/29.9 and ~30 - the gap is the pairs the cap kept that decode never uses, and the dropped
/// pairs decode pays the swap-in for.  This re-rank spends the SAME cap bytes on the RIGHT pairs: promote the
/// dropped pairs decode demands most, demote the resident pairs decode touches least, at a safe boundary.
///
/// COMPOSES with the cap + floor + hot + tier flags.  The floor stays a GUARANTEE and the hot set and the relief's
/// tier drops NEVER move; the re-rank decides only WITHIN the droppable band (not hot, not tier-served):
///   - `hot` and `tier_dropped` (the helper's rows, the spill tier's, the stage caches' - served by the helper/tier,
///     promoting them buys nothing, per the decode-floor's rule above) are UNTUCHABLE: never promoted, never demoted.
///   - The DECODE FLOOR is `decode_floor_bytes`: demotes never push the resident copy below `hot_bytes +
///     decode_floor_bytes`, the same guarantee relief_arena_cap_plan makes; a re-rank bound by it reports `floor_bound`.
///   - `cap_bytes` is the ceiling, EXCEPT where the floor's guarantee sits above it (the boot plan's
///     short_of_cap landing: hot + floor > cap): the band's ceiling is then max(cap, hot + floor) - the
///     residency the engine already runs at.  A swap that would push residency over the ceiling demotes to
///     free headroom first; the floor guards every swap that REDUCES residency (a net-0 swap, the uniform-size
///     prod case, holds the band where the boot plan landed it - the RIGHT pairs at the SAME price).
///
/// HEAT is the decode signal measured at runtime (ArenaExpertSource's per-pair counters): the resident pairs a decode
/// walk touches, and the dropped pairs a decode walk swaps in from the pack files.  A pair with 0 heat demands nothing.
/// `resident[i]` is the CURRENT keep set (1 = host-copied now); `!resident` pairs outside the untouchable set are the
/// cap's evictions - the promotable set.  Ties break on the profile (colder pairs demote first, hotter pairs promote
/// first), so the plan is deterministic with the boot cap's ordering.
///
/// Pure host arithmetic, no device, no allocation, no side effect - the caller owns set_unfilled() / register and the
/// refills; this says which pairs to swap.  The swap loop promotes the hottest demanding pair against the coldest
/// resident pair, and stops the moment no strict heat win is left (`heat[P] <= heat[D]`): the band re-ranks to
/// decode's usage, the cap and the floor hold.
struct ReliefArenaReorderPlan {
    std::vector<std::pair<int32_t, int32_t>> promote;   ///< dropped pairs to bring resident, hottest decode first
    std::vector<std::pair<int32_t, int32_t>> demote;    ///< resident pairs to give back, coldest decode first
    uint64_t promoted_bytes = 0;
    uint64_t demoted_bytes = 0;
    uint64_t resident_after = 0;                        ///< residency after every swap (<= cap)
    int64_t swaps = 0;                                  ///< net swaps (a promote paid by a demote)
    int64_t headroom_demotes = 0;                       ///< demotes alone, to free cap headroom for a promote
    bool reordered = false;                             ///< false = nothing moved (no heat, no strict win)
    bool floor_bound = false;                           ///< demotes stopped on the decode floor's guarantee
};

/// The re-rank's heat MIX for one pair out of its two banks (helper-ipc-diet round 9).  `mixed` is the
/// all-phase counter (ArenaExpertSource::heat_: the read's swap-ins and the decode's together), `decode`
/// is the decode-phase bank (decode_heat_, the subset the round-8 phase tag banks).  The read bank is
/// their difference.  STRATA_ARENA_RERANK_PHASE_TAG selects:
///   0 (default): the mixed counter - the round-6/7 behavior.  A long prompt's read sweep (thousands of
///      pairs) drowns the decode window's demand, so the band never converges on decode's set.
///   1: the decode bank alone - the round-8 knob.  The promote side is decode's own set (the v0227 RR
///      panel recovered the warm legs: longtg's continuation verify 124.9 -> 97.7 ms, killerL's 91.8 ->
///      86.2 with acceptance 57.8 -> 63.6), and the demote side is EVERY resident pair decode never
///      touched - including the wide set a fresh long read/verify needs.  The same panel paid that on
///      the first-pass 142k gens (verify 119.6 -> 161.4 ms, 109.5 -> 134.9 ms).
///   2: the decode bank plus read_weight/8 of the read bank - plans on decode's set (the round-8 win)
///      while a read-hot resident keeps a floor the promote must STRICTLY beat, so the prefill's wide
///      placement survives the boundary (the round-8 harm).  read_weight 0..8: 0 is tag 1, 8 puts the
///      read bank at full weight.
inline int64_t arena_rerank_heat(int64_t tag, int64_t read_weight, int64_t mixed, int64_t decode) {
    if (tag == 0) return mixed;
    if (tag == 1) return decode;
    const int64_t read = mixed > decode ? mixed - decode : 0;
    return decode + read * read_weight / 8;
}

template <typename PairBytes>
ReliefArenaReorderPlan relief_arena_reorder_plan(int64_t n_layers, int64_t n_expert, uint64_t cap_bytes,
                                                 uint64_t decode_floor_bytes,
                                                 const std::vector<std::pair<int32_t, int32_t>>& profile,
                                                 const std::vector<uint8_t>& hot,
                                                 const std::vector<uint8_t>& tier_dropped,
                                                 const std::vector<uint8_t>& resident,
                                                 const std::vector<int64_t>& heat,
                                                 PairBytes pair_bytes) {
    ReliefArenaReorderPlan plan;
    const size_t cells = n_layers < 0 || n_expert <= 0 ? 0 : (size_t) n_layers * (size_t) n_expert;
    if (cells == 0) return plan;
    auto pb = [&](size_t i) { return (uint64_t) pair_bytes((int64_t) (i / (size_t) n_expert)); };

    // The profile's rank per pair: 0 = hottest, larger = colder, unranked = coldest (profile.size()).  The tie-break
    // matches the boot cap: colder pairs are the first to demote, hotter the first to promote.
    std::vector<uint64_t> rank(cells, profile.size());
    for (size_t r = 0; r < profile.size(); ++r) {
        const auto& pr = profile[r];
        if (pr.first < 0 || pr.first >= n_layers || pr.second < 0 || pr.second >= n_expert) continue;
        const size_t key = (size_t) pr.first * (size_t) n_expert + (size_t) pr.second;
        if (rank[key] == profile.size()) rank[key] = r;   // the profile's first sighting of a pair is its rank
    }

    std::vector<uint8_t> untouchable(cells, 0);
    int64_t hot_bytes = 0;
    uint64_t resident_bytes = 0;
    std::vector<size_t> promote_cand, demote_cand;
    for (size_t i = 0; i < cells; ++i) {
        const bool h = i < hot.size() && hot[i] != 0;
        const bool t = i < tier_dropped.size() && tier_dropped[i] != 0;
        untouchable[i] = (uint8_t) (h || t);
        if (h) hot_bytes += (int64_t) pb(i);
        const bool res = i < resident.size() && resident[i] != 0;
        if (res) {
            resident_bytes += pb(i);
            if (!untouchable[i]) demote_cand.push_back(i);   // the resident band: demote candidates
        } else if (!untouchable[i]) {
            promote_cand.push_back(i);                       // the cap's evictions: promote candidates
        }
    }
    plan.resident_after = resident_bytes;
    const int64_t floor_min = hot_bytes + (int64_t) decode_floor_bytes;   // the decode floor's guarantee
    // The band's CEILING is max(cap, floor): the cap flag is the ceiling, but when the floor's guarantee sits
    // above the cap (the boot plan's short_of_cap landing: hot + floor > cap), the floor IS the residency the
    // engine runs at - a re-rank at net 0 there costs nothing and must not be blocked by the cap line.
    const int64_t ceiling = floor_min > (int64_t) cap_bytes ? floor_min : (int64_t) cap_bytes;

    // Demote: coldest decode first, ties by profile-cold (larger rank first).  Promote: hottest decode first, ties by
    // profile-hot (smaller rank first).  Both stable so equal-heat, equal-rank pairs keep layer order.
    std::sort(demote_cand.begin(), demote_cand.end(), [&](size_t a, size_t b) {
        const int64_t ha = a < heat.size() ? heat[a] : 0, hb = b < heat.size() ? heat[b] : 0;
        if (ha != hb) return ha < hb;
        return rank[a] > rank[b];
    });
    std::sort(promote_cand.begin(), promote_cand.end(), [&](size_t a, size_t b) {
        const int64_t ha = a < heat.size() ? heat[a] : 0, hb = b < heat.size() ? heat[b] : 0;
        if (ha != hb) return ha > hb;
        return rank[a] < rank[b];
    });

    // The swap loop.  A promote is taken only for a STRICT heat win over the coldest resident pair.  A promote that
    // would push residency over the cap is paid first by a headroom demote (which respects the floor).
    size_t pi = 0, di = 0;
    int64_t res = (int64_t) resident_bytes;
    auto heat_of = [&](size_t i) { return i < heat.size() ? heat[i] : 0; };
    while (pi < promote_cand.size() && di < demote_cand.size()) {
        const size_t P = promote_cand[pi], D = demote_cand[di];
        const int64_t ph = heat_of(P);
        if (ph <= 0) break;                                     // nothing demands promotion
        const int64_t dh = heat_of(D);
        if (ph <= dh) break;                                    // no strict decode win left
        const int64_t net = (int64_t) pb(P) - (int64_t) pb(D);
        if (res + net > ceiling) {                              // promote needs headroom under the ceiling: demote the coldest first
            if (res - (int64_t) pb(D) < floor_min) { plan.floor_bound = true; break; }
            res -= (int64_t) pb(D);
            plan.demote.emplace_back((int32_t) (D / (size_t) n_expert), (int32_t) (D % (size_t) n_expert));
            plan.demoted_bytes += pb(D);
            ++plan.headroom_demotes;
            plan.reordered = true;
            ++di;
            continue;                                           // re-weigh P against the next coldest resident pair
        }
        // A net swap that keeps residency at or under the ceiling and above the floor.  The floor bites only
        // when the swap REDUCES residency below it (net < 0): the prod uniform-size case swaps at net 0, which
        // holds the band where the boot plan landed it - the right pairs at the same price, floor intact.
        if (net < 0 && res + net < floor_min) { plan.floor_bound = true; break; }
        res += net;
        plan.promote.emplace_back((int32_t) (P / (size_t) n_expert), (int32_t) (P % (size_t) n_expert));
        plan.promoted_bytes += pb(P);
        plan.demote.emplace_back((int32_t) (D / (size_t) n_expert), (int32_t) (D % (size_t) n_expert));
        plan.demoted_bytes += pb(D);
        ++plan.swaps;
        plan.reordered = true;
        ++pi;
        ++di;
    }
    plan.resident_after = (uint64_t) (res < 0 ? 0 : res);
    return plan;
}

}   // namespace strata::core
