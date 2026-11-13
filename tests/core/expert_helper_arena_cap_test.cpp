// tests/core/expert_helper_arena_cap_test.cpp - CPU checks for the arena cap's plan
// (include/strata/core/relief_arena_cap.hpp): the arithmetic behind
// --expert-helper-relief-arena-cap-mib.  Pure host arithmetic, no device, no files.
//
// The shapes are the measured prod ones: the IQ3_S arena copies 24576 ranked pairs in 46.84 GiB (1905 KiB-scale
// pairs of 1.905 MiB), the IQ3_XXS arena the same 24576 pairs in 39.97 GiB; the helper mirrors 4615 pairs and the
// relief drops their host copies.  Sizes below are in MiB so a failure names a real shortfall.
#include "strata/core/relief_arena_cap.hpp"

#include <cstdint>
#include <cstdio>
#include <utility>
#include <vector>

using namespace strata::core;

static int failures = 0;
static void check(bool ok, const char* what) {
    if (!ok) {
        std::printf("FAIL: %s\n", what);
        ++failures;
    }
}

namespace {
constexpr int64_t LAYERS = 48, EXPERTS = 512;      // 24576 pairs, the prod profile's size
constexpr uint64_t MIB = 1ull << 20;

struct Fixture {
    uint64_t pair_bytes;                                       // the arena's stride for every layer
    std::vector<std::pair<int32_t, int32_t>> profile;          // ranked hottest first
    std::vector<uint8_t> hot;
    std::vector<uint8_t> dropped;
    uint64_t total() const { return pair_bytes * (uint64_t) LAYERS * (uint64_t) EXPERTS; }
};

/// A fixture over the 24576 pairs; `ranked` of them are in the profile, in layer order (hottest first).
Fixture make(uint64_t pair_kib, size_t ranked) {
    Fixture f;
    f.pair_bytes = pair_kib * 1024;
    f.hot.assign((size_t) LAYERS * (size_t) EXPERTS, 0);
    f.dropped.assign((size_t) LAYERS * (size_t) EXPERTS, 0);
    for (size_t i = 0; i < ranked; ++i)
        f.profile.emplace_back((int32_t) (i / (size_t) EXPERTS), (int32_t) (i % (size_t) EXPERTS));
    return f;
}

auto bytes_of(const Fixture& f) {
    return [&](int64_t) { return f.pair_bytes; };
}
}   // namespace

int main() {
    // 1. Nothing to do: the resident copy already fits the cap.
    {
        Fixture f = make(1024, 24576);
        const auto p = relief_arena_cap_plan(LAYERS, EXPERTS, f.total(), f.total(), f.profile, f.hot, f.dropped,
                                             bytes_of(f));
        check(!p.capped && p.drop.empty(), "a cap at the full arena drops nothing");
        check(p.resident_after == f.total(), "a cap at the full arena keeps every byte resident");
    }

    // 2. Cap 0 with no hot set: every pair the arena copies is droppable, and the bytes add up exactly.
    {
        Fixture f = make(1024, 24576);
        const auto p = relief_arena_cap_plan(LAYERS, EXPERTS, f.total(), 0, f.profile, f.hot, f.dropped, bytes_of(f));
        check(p.capped, "cap 0 caps");
        check(p.drop.size() == 24576, "cap 0 drops every pair of the arena");
        check(p.dropped_bytes == f.total(), "cap 0's dropped bytes are the whole arena (24 GiB at 1 MiB x 24576)");
        check(p.resident_after == 0, "cap 0 leaves nothing resident");
        check(p.kept_pairs == 0, "cap 0 keeps no pair");
    }

    // 3. The hot set is never dropped: it is the VRAM tier's backing store.
    {
        Fixture f = make(1024, 24576);
        for (size_t i = 0; i < 1000; ++i) f.hot[i] = 1;   // the hottest 1000 pairs
        const auto p = relief_arena_cap_plan(LAYERS, EXPERTS, f.total(), 0, f.profile, f.hot, f.dropped, bytes_of(f));
        bool hot_kept = true;
        for (const auto& pr : p.drop)
            if (f.hot[(size_t) pr.first * (size_t) EXPERTS + (size_t) pr.second]) hot_kept = false;
        check(hot_kept, "the cap drops no hot pair at any cap, 0 included");
        check(p.drop.size() == 24576 - 1000, "cap 0 drops the set minus the hot prefix");
        check(p.resident_after == 1000 * MIB, "the hot prefix is exactly what stays resident at cap 0");
        check(p.kept_pairs == 1000, "kept_pairs counts the hot prefix");
    }

    // 4. Coldest first: the pairs the profile never ranked go before the ranked tail, and the ranked tail
    //    is taken from its coldest end (the profile read backwards), stopping at the cap.
    {
        Fixture f = make(1024, 24576);
        f.profile.resize(1000);   // 1000 ranked pairs, 23576 unranked (the coldest the arena holds)
        const auto p = relief_arena_cap_plan(LAYERS, EXPERTS, f.total(), 12000 * MIB, f.profile, f.hot, f.dropped,
                                             bytes_of(f));
        check(p.capped, "a 12 GiB cap on a 24 GiB arena caps");
        check(p.resident_after <= 12000 * MIB, "the plan lands at or under the cap");
        check(p.drop.size() == 24576 - 12000, "exactly enough pairs drop: no more");
        check(p.dropped_bytes == (24576 - 12000) * MIB, "the dropped bytes are the pairs taken, not an estimate");
        check(p.kept_pairs == 12000, "the kept pairs are what fits the cap");
        const size_t last_key = (size_t) p.drop.back().first * (size_t) EXPERTS + (size_t) p.drop.back().second;
        check(last_key == 1000 + (24576 - 12000) - 1, "only the unranked pairs are needed, in layer order from the coldest");
    }

    // 4b. A cap the unranked pairs cannot cover reaches the ranked tail - from its coldest end, hottest last.
    {
        Fixture f = make(1024, 1000);
        const auto p = relief_arena_cap_plan(LAYERS, EXPERTS, f.total(), 500 * MIB, f.profile, f.hot, f.dropped,
                                             bytes_of(f));
        check(p.drop.size() == 23576 + 500, "the unranked pairs first (23576), then 500 of the ranked tail");
        check(p.drop[23576] == f.profile[999], "the ranked part starts at the profile's coldest entry");
        check(p.drop.back() == f.profile[500], "the cap stops at the ranked pair 500: everything hotter stays");
        check(p.kept_pairs == 500, "500 MiB of the hottest ranked pairs stay resident");
        const auto all = relief_arena_cap_plan(LAYERS, EXPERTS, f.total(), 0, f.profile, f.hot, f.dropped,
                                               bytes_of(f));
        check(all.drop.size() == 24576 && all.drop.back() == f.profile[0], "cap 0 takes the profile last, hottest pair last");
    }

    // 5. What the caller already dropped is subtracted and never dropped twice.
    {
        Fixture f = make(1024, 24576);
        for (size_t i = 0; i < 4615; ++i) f.dropped[i] = 1;   // the helper's mirrored pairs (the hottest here)
        const auto p = relief_arena_cap_plan(LAYERS, EXPERTS, f.total(), 15000 * MIB, f.profile, f.hot, f.dropped,
                                             bytes_of(f));
        bool no_double = true;
        for (const auto& pr : p.drop)
            if (f.dropped[(size_t) pr.first * (size_t) EXPERTS + (size_t) pr.second]) no_double = false;
        check(no_double, "the plan drops no pair the caller already gave back");
        check(p.resident_after <= 15000 * MIB, "the cap accounts the caller's drop: 24576 - 4615 - tail <= 15000 MiB");
        check(p.drop.size() == 19961 - 15000, "the tail the cap takes is what the helper's drop left over");
        check(p.kept_pairs == 15000, "the arena keeps 15000 pairs (the caller's 4615 are gone)");
    }

    // 6. The cap fits with the caller's drop alone: the plan adds nothing.
    {
        Fixture f = make(1024, 24576);
        for (size_t i = 0; i < 12576; ++i) f.dropped[i] = 1;
        const auto p = relief_arena_cap_plan(LAYERS, EXPERTS, f.total(), 12001 * MIB, f.profile, f.hot, f.dropped,
                                             bytes_of(f));
        check(!p.capped && p.drop.empty(), "when the relief's drop already fits the cap the plan adds nothing");
        check(p.resident_after == 12000 * MIB, "the resident total after the relief's drop is what the cap sees");
    }

    // 7. A repeated profile entry is one pair: the drop and the bytes count it once.
    {
        Fixture f = make(1024, 12288);
        f.profile.insert(f.profile.end(), f.profile.begin(), f.profile.end());   // every ranked pair twice
        const auto p = relief_arena_cap_plan(LAYERS, EXPERTS, f.total(), 0, f.profile, f.hot, f.dropped, bytes_of(f));
        check(p.drop.size() == 24576, "a ranked pair listed twice drops once");
        bool dup = false;
        std::vector<uint8_t> seen((size_t) LAYERS * (size_t) EXPERTS, 0);
        for (const auto& pr : p.drop) {
            const size_t key = (size_t) pr.first * (size_t) EXPERTS + (size_t) pr.second;
            if (seen[key]) dup = true;
            seen[key] = 1;
        }
        check(!dup, "the drop list has no duplicate pair");
    }

    // 8. Out-of-range entries are ignored, not indexed.
    {
        Fixture f = make(1024, 1024);
        f.profile.emplace_back(-1, 0);
        f.profile.emplace_back(LAYERS, 0);
        f.profile.emplace_back(0, EXPERTS);
        const auto p = relief_arena_cap_plan(LAYERS, EXPERTS, f.total(), 0, f.profile, f.hot, f.dropped, bytes_of(f));
        bool in_range = true;
        for (const auto& pr : p.drop)
            if (pr.first < 0 || pr.first >= LAYERS || pr.second < 0 || pr.second >= EXPERTS) in_range = false;
        check(in_range, "an out-of-range profile entry drops nothing out of range");
        check(p.drop.size() == 24576, "the out-of-range entries do not add a drop");
    }

    // 9. The measured IQ3_S shape: 1.905 MiB a pair (46.84 GiB over 24576 pairs), the helper's 4615 pairs
    //    already given back, the cap asked at 20 GiB.  The numbers the boot line reports.
    {
        const uint64_t pair_b = 1992192;   // 1.9 MiB rounded: 24576 x 1992192 = 46.875 GiB, the arena's measured copy
        Fixture f = make(1945, 24576);
        f.pair_bytes = pair_b;
        for (size_t i = 0; i < 4615; ++i) f.dropped[i] = 1;
        const auto p = relief_arena_cap_plan(LAYERS, EXPERTS, f.total(), 20000 * MIB, f.profile, f.hot, f.dropped,
                                             [&](int64_t) { return pair_b; });
        check(p.capped, "the iq3_s arena at a 20 GiB cap caps");
        check(p.resident_after <= 20000 * MIB, "the arena ends at or under 20 GiB resident");
        const uint64_t relief_all = p.dropped_bytes + 4615ull * pair_b;
        check(relief_all > 26000ull * MIB, "the whole relief (helper's 8.7 GiB + the tail) gives back over 26 GiB");
        check(p.kept_pairs == (int64_t) (24576 - 4615 - p.drop.size()), "the kept pairs are the arena's hot prefix");
    }

    // 10. The decode budget OFF is byte-identical to the uniform cap: same plan, pair for pair.
    {
        Fixture f = make(1024, 24576);
        for (size_t i = 0; i < 4615; ++i) f.dropped[i] = 1;
        const auto uniform = relief_arena_cap_plan(LAYERS, EXPERTS, f.total(), 20000 * MIB, f.profile, f.hot,
                                                   f.dropped, bytes_of(f));
        const auto off = relief_arena_cap_plan(LAYERS, EXPERTS, f.total(), 20000 * MIB, f.profile, f.hot,
                                               f.dropped, bytes_of(f), 0);
        check(uniform.drop == off.drop, "decode budget 0: the same drop list as the uniform cap");
        check(uniform.dropped_bytes == off.dropped_bytes && uniform.resident_after == off.resident_after,
              "decode budget 0: the same bytes and the same resident total");
        check(off.decode_pairs == 0 && off.decode_bytes == 0, "decode budget 0 protects nothing");
    }

    // 11. The budget protects the HOTTEST claimable pairs: the cap's bytes come off the tail, the protected
    //     prefix stays, and the plan lands exactly on the cap when the pool is big enough.
    {
        Fixture f = make(1024, 24576);
        const auto p = relief_arena_cap_plan(LAYERS, EXPERTS, f.total(), 12000 * MIB, f.profile, f.hot, f.dropped,
                                             bytes_of(f), 2000 * MIB);
        check(p.decode_pairs == 2000 && p.decode_bytes == 2000 * MIB, "the budget protects 2000 pairs (2000 MiB)");
        check(p.resident_after == 12000 * MIB, "the cap is reached exactly: 24576 - 12576 dropped = 12000 MiB");
        check(p.drop.size() == 12576, "the drop is the cap's shortfall, not more");
        check(!p.short_of_cap, "the pool (22576 pairs) covers the shortfall (12576)");
        bool prefix = true;
        std::vector<uint8_t> prot((size_t) LAYERS * (size_t) EXPERTS, 0);
        for (size_t i = 0; i < 2000; ++i) prot[(size_t) f.profile[i].first * (size_t) EXPERTS + f.profile[i].second] = 1;
        for (const auto& pr : p.drop)
            if (prot[(size_t) pr.first * (size_t) EXPERTS + (size_t) pr.second]) prefix = false;
        check(prefix, "no protected pair is ever dropped");
        check(p.drop.back() == f.profile[12000], "the last pair the cap takes is the first rank past the kept prefix");
        check(p.kept_pairs == 12000, "kept_pairs counts the protected prefix inside the cap");
    }

    // 12. The budget skips what is already decided: the hot set and the relief's drops are not protected
    //     twice, so the protected prefix starts at the first pair neither covers - decode's universe, the
    //     pairs just outside the caches and the helper's mirror.
    {
        Fixture f = make(1024, 24576);
        for (size_t i = 0; i < 100; ++i) f.hot[i] = 1;          // the hottest 100 pairs: the hot budget
        for (size_t i = 100; i < 200; ++i) f.dropped[i] = 1;    // the helper mirrors the next 100
        const auto p = relief_arena_cap_plan(LAYERS, EXPERTS, f.total(), 12000 * MIB, f.profile, f.hot, f.dropped,
                                             bytes_of(f), 1000 * MIB);
        check(p.decode_pairs == 1000 && p.decode_bytes == 1000 * MIB, "the budget protects 1000 pairs, after the hot set and the drops");
        std::vector<uint8_t> drop_seen((size_t) LAYERS * (size_t) EXPERTS, 0);
        for (const auto& pr : p.drop) drop_seen[(size_t) pr.first * (size_t) EXPERTS + (size_t) pr.second] = 1;
        const auto claimable_hottest = f.profile[200];   // 0..99 hot, 100..199 dropped: the first claimable is 200
        const auto protected_last = f.profile[1199];
        check(drop_seen[(size_t) claimable_hottest.first * (size_t) EXPERTS + claimable_hottest.second] == 0,
              "the protected prefix starts at the claimable hottest pair");
        check(drop_seen[(size_t) protected_last.first * (size_t) EXPERTS + protected_last.second] == 0,
              "the protected prefix ends at profile[1199] (1000 pairs)");
        check(drop_seen[(size_t) f.profile[12100].first * (size_t) EXPERTS + f.profile[12100].second] == 1,
              "the cap's own keep ends right after the protected prefix and the tail drops from there");
        check(p.drop.size() == 12476 && p.resident_after == 12000 * MIB,
              "the drop is the shortfall after both budgets (24576 - 100 given back - 12476 dropped = 12000 MiB)");
        check(!p.short_of_cap, "the pool covers the shortfall");
    }

    // 13. A contiguous prefix: the first pair that would overflow the budget stops it (no gaps, no overspend).
    {
        Fixture f = make(700, 24576);   // 700 KiB a pair
        const uint64_t budget = 1000 * MIB;
        const auto p = relief_arena_cap_plan(LAYERS, EXPERTS, f.total(), 12000 * MIB, f.profile, f.hot, f.dropped,
                                             bytes_of(f), budget);
        check(p.decode_bytes <= budget, "the protected bytes never exceed the budget");
        check(p.decode_bytes + 700 * 1024 > budget, "the next pair would overflow, so the prefix stops there");
        check(p.decode_pairs == budget / (700 * 1024), "the prefix is exactly the pairs that fit");
    }

    // 14. The budgets together leave less than the cap's shortfall: the plan drops every droppable pair and
    //     says it landed short (never a silent failure, never a hot pair dropped).
    {
        Fixture f = make(1024, 24576);
        const auto p = relief_arena_cap_plan(LAYERS, EXPERTS, f.total(), 0, f.profile, f.hot, f.dropped,
                                             bytes_of(f), 24000 * MIB);
        check(p.short_of_cap, "cap 0 with a 24000 MiB decode budget reports it landed short");
        check(p.decode_pairs == 24000, "the budget protects 24000 pairs");
        check(p.drop.size() == 576 && p.dropped_bytes == 576 * MIB, "everything droppable (576 pairs) drops");
        check(p.resident_after == 24000 * MIB, "the arena lands at the protected 24000 MiB");
        check(p.kept_pairs == 24000, "kept_pairs is the protected set");
    }

    // 15. The measured IQ3_S shape with the split: 1.905 MiB a pair (46.84 GiB), the hot budget's 7500 pairs
    //     (the 15000 MiB stride budget), the helper's 4615 pairs already dropped, cap 20000 MiB, decode budget
    //     6000 MiB.  The plan must spend the cap on the prefill-cold tail only and report where it lands.
    {
        const uint64_t pair_b = 1992192;   // 24576 x 1992192 = 46.875 GiB, the arena's measured copy
        Fixture f = make(1945, 24576);
        f.pair_bytes = pair_b;
        for (size_t i = 0; i < 7500; ++i) f.hot[i] = 1;              // the hot budget's pairs (tier backing)
        for (size_t i = 7500; i < 7500 + 4615; ++i) f.dropped[i] = 1;   // the helper's mirror
        const auto p = relief_arena_cap_plan(LAYERS, EXPERTS, f.total(), 20000 * MIB, f.profile, f.hot, f.dropped,
                                             [&](int64_t) { return pair_b; }, 6000 * MIB);
        check(p.decode_pairs == (6000 * MIB) / pair_b, "the decode budget protects ~3150 pairs (6000 MiB)");
        const uint64_t resident_before = f.total() - 4615ull * pair_b;
        check(p.dropped_bytes == resident_before - p.resident_after, "the plan's bytes are exactly what left the arena");
        check(p.resident_after <= 20300ull * MIB && p.resident_after >= 20000ull * MIB,
              "the cap 20000 with a 6000 MiB decode budget lands ~20.0-20.3 GiB resident: the memory of the measured point, with decode served from RAM");
        check(p.kept_pairs == 7500 + p.decode_pairs, "what stays is the hot set plus the decode prefix");
        std::vector<uint8_t> prot((size_t) LAYERS * (size_t) EXPERTS, 0);
        for (size_t i = 7500 + 4615; i < 7500 + 4615 + (size_t) p.decode_pairs; ++i)
            prot[(size_t) f.profile[i].first * (size_t) EXPERTS + (size_t) f.profile[i].second] = 1;
        bool tails_only = true;
        for (const auto& pr : p.drop)
            if (prot[(size_t) pr.first * (size_t) EXPERTS + (size_t) pr.second]) tails_only = false;
        check(tails_only, "the drop is the prefill-cold tail: no decode-protected pair drops");
    }

    // 16. The re-rank with NO heat: nothing demands promotion, nothing moves (a fresh boot re-rank is a no-op).
    {
        Fixture f = make(1024, 24576);
        std::vector<uint8_t> tier((size_t) LAYERS * (size_t) EXPERTS, 0), resident((size_t) LAYERS * (size_t) EXPERTS, 0);
        for (size_t i = 0; i < 4615; ++i) tier[i] = 1;                       // the helper's mirror: tier-served, dropped
        for (size_t i = 4615; i < 15000; ++i) resident[i] = 1;              // the cap's keep
        const std::vector<int64_t> heat((size_t) LAYERS * (size_t) EXPERTS, 0);
        const auto p = relief_arena_reorder_plan(LAYERS, EXPERTS, 15000 * MIB, 0, f.profile, f.hot, tier,
                                                 resident, heat, bytes_of(f));
        check(!p.reordered && p.promote.empty() && p.demote.empty(), "no heat: the re-rank moves nothing");
        check(p.resident_after == (15000 - 4615) * MIB, "no heat: the residency is the keep set's bytes");
        check(p.swaps == 0 && !p.floor_bound, "no heat: no swaps, not floor-bound");
    }

    // 17. Equal heat is no STRICT win: a dropped pair as hot as the coldest resident pair does not displace it.
    {
        Fixture f = make(1024, 24576);
        std::vector<uint8_t> tier((size_t) LAYERS * (size_t) EXPERTS, 0), resident((size_t) LAYERS * (size_t) EXPERTS, 0);
        std::vector<int64_t> heat((size_t) LAYERS * (size_t) EXPERTS, 0);
        for (size_t i = 0; i < 4615; ++i) tier[i] = 1;
        for (size_t i = 4615; i < 15000; ++i) resident[i] = 1;
        for (size_t i = 15000; i < 15010; ++i) heat[i] = 7;   // the cap's evictions demand 7
        for (size_t i = 4615; i < 15000; ++i) heat[i] = 7;    // every resident pair demands 7 too
        const auto p = relief_arena_reorder_plan(LAYERS, EXPERTS, 15000 * MIB, 0, f.profile, f.hot, tier,
                                                 resident, heat, bytes_of(f));
        check(!p.reordered && p.promote.empty(), "equal heat: no strict win, nothing promotes");
    }

    // 18. A strict win moves the band: the hottest demanding eviction promotes against the coldest demanding
    //     resident pair, at net 0 (uniform sizes) - the RIGHT pairs at the SAME price.
    {
        Fixture f = make(1024, 24576);
        std::vector<uint8_t> tier((size_t) LAYERS * (size_t) EXPERTS, 0), resident((size_t) LAYERS * (size_t) EXPERTS, 0);
        std::vector<int64_t> heat((size_t) LAYERS * (size_t) EXPERTS, 0);
        for (size_t i = 0; i < 4615; ++i) tier[i] = 1;
        for (size_t i = 4615; i < 15000; ++i) resident[i] = 1;
        heat[15000] = 50; heat[15001] = 40; heat[15002] = 30;   // the evictions decode demands most
        heat[4615] = 1; heat[4616] = 0; heat[4617] = 0;         // the coldest resident pairs, decode touches least
        const auto p = relief_arena_reorder_plan(LAYERS, EXPERTS, 15000 * MIB, 0, f.profile, f.hot, tier,
                                                 resident, heat, bytes_of(f));
        check(p.reordered && p.swaps == 3, "three strict wins: three net swaps");
        check(p.promote.size() == 3 && p.demote.size() == 3, "each promote paid by a demote");
        check(p.promote[0] == f.profile[15000] && p.promote[2] == f.profile[15002],
              "the promote list is hottest decode first");
        const size_t d0 = (size_t) p.demote[0].first * (size_t) EXPERTS + p.demote[0].second;
        check(d0 >= 4615 && heat[d0] <= 1, "the demote starts at the coldest demanding resident pair");
        check(p.resident_after == (15000 - 4615) * MIB, "uniform sizes: the net swap pins residency at the cap");
        check(p.promoted_bytes == p.demoted_bytes, "the promoted and demoted bytes add up equal");
    }

    // 19. The untouchable sets never move: a hot pair is never demoted, a tier-served pair is never promoted,
    //     however the heat ranks them.
    {
        Fixture f = make(1024, 24576);
        std::vector<uint8_t> tier((size_t) LAYERS * (size_t) EXPERTS, 0), resident((size_t) LAYERS * (size_t) EXPERTS, 0);
        std::vector<int64_t> heat((size_t) LAYERS * (size_t) EXPERTS, 0);
        for (size_t i = 0; i < 1000; ++i) f.hot[i] = 1;
        for (size_t i = 1000; i < 1000 + 4615; ++i) tier[i] = 1;
        for (size_t i = 1000 + 4615; i < 15000; ++i) resident[i] = 1;
        heat[500] = 5;                      // hot pairs look cold - the demote must skip them
        heat[1000] = 9999;                  // a tier pair screams for promotion - it must not promote
        heat[15000] = 300;
        heat[4615 + 1000] = 0; heat[4615 + 1001] = 0;
        const auto p = relief_arena_reorder_plan(LAYERS, EXPERTS, 15000 * MIB, 0, f.profile, f.hot, tier,
                                                 resident, heat, bytes_of(f));
        bool clean = true;
        for (const auto& pr : p.promote)
            if (tier[(size_t) pr.first * (size_t) EXPERTS + pr.second]) clean = false;
        for (const auto& pr : p.demote) {
            const size_t key = (size_t) pr.first * (size_t) EXPERTS + pr.second;
            if (f.hot[key] || tier[key]) clean = false;
        }
        check(clean, "the re-rank promotes no tier pair and demotes no hot pair");
        check(p.promote.size() == 1 && p.swaps == 1, "the screaming tier pair stays file-served; only the cap eviction promotes");
    }

    // 20. The decode floor is a GUARANTEE: every swap the plan applies keeps residency at hot + floor, and the
    //     uniform-size band re-ranks fully (net 0 keeps the band exactly where the floor put it).
    {
        Fixture f = make(1024, 24576);
        std::vector<uint8_t> tier((size_t) LAYERS * (size_t) EXPERTS, 0), resident((size_t) LAYERS * (size_t) EXPERTS, 0);
        std::vector<int64_t> heat((size_t) LAYERS * (size_t) EXPERTS, 0);
        for (size_t i = 0; i < 1000; ++i) { f.hot[i] = 1; resident[i] = 1; }        // hot: 1000 MiB, resident
        for (size_t i = 1000; i < 1000 + 4615; ++i) tier[i] = 1;
        for (size_t i = 1000 + 4615; i < 10000; ++i) resident[i] = 1;              // resident band: 4385 MiB
        for (size_t i = 10000; i < 24576; ++i) heat[i] = 20;                       // every eviction demands
        const auto p = relief_arena_reorder_plan(LAYERS, EXPERTS, 10000 * MIB, 4000 * MIB, f.profile, f.hot, tier,
                                                 resident, heat, bytes_of(f));
        check(!p.floor_bound, "net 0 at the floor's band: the re-rank is not floor-bound");
        check(p.swaps == 4385, "the whole demanding band swaps against the keep");
        check(p.resident_after == 5385 * MIB, "net 0: residency holds at hot + band (above hot + floor)");
        check(p.resident_after >= 1000ull * MIB + 4000ull * MIB, "the floor's guarantee is intact");
    }

    // 21. The cap is the CEILING with unequal sizes: a promote bigger than the demotes it pays needs cap
    //     headroom, so the plan takes coldest-first headroom demotes (counted, not counted as swaps).
    {
        Fixture f = make(1024, 24576);
        const uint64_t big = 4 * MIB;   // layer 0's pairs are 4x the band's
        std::vector<uint8_t> tier((size_t) LAYERS * (size_t) EXPERTS, 0), resident((size_t) LAYERS * (size_t) EXPERTS, 0);
        std::vector<int64_t> heat((size_t) LAYERS * (size_t) EXPERTS, 0);
        for (size_t i = 4615; i < 4615 + 4615; ++i) tier[i] = 1;          // the helper's mirror: tier-served (layers 9..17)
        for (size_t i = 4615 + 4615; i < 4615 + 4615 + 5770; ++i) resident[i] = 1;   // the keep (1 MiB pairs)
        heat[0] = 50;                                       // a layer-0 eviction (4 MiB) demands promotion
        for (size_t i = 4615 + 4615; i < 15000; ++i) heat[i] = 1;   // every resident payer demands 1
        const auto pb = [&](int64_t l) { return (uint64_t) (l == 0 ? big : 1 * MIB); };
        const auto p = relief_arena_reorder_plan(LAYERS, EXPERTS, 5770 * MIB, 0, f.profile,
                                                 f.hot, tier, resident, heat, pb);
        check(p.reordered && p.swaps == 1, "one net swap paid for");
        check(p.headroom_demotes == 3, "the 4 MiB promote cost three 1 MiB headroom demotes");
        check(p.resident_after <= 5770ull * MIB, "residency stayed at or under the cap");
        check(p.promoted_bytes == big && p.demoted_bytes == 4 * MIB, "the bytes add up: 4 MiB in, 4 MiB out");
    }

    // 22. The prod shape (iq3_s s34 at cap 20000 / floor 6000 / hot 15000): the boot plan lands the residency at
    //     hot + the decode prefix (~20.2 GiB, short_of_cap), evicting everything colder.  The decode walk
    //     touches a band the cap evicted.  The re-rank spends the SAME residency on that band: the evictions
    //     decode demands promote, the keep's untouched cold end demotes, residency holds at the floor's band.
    {
        const uint64_t pair_b = 1992192;   // 24576 x 1992192 = 46.875 GiB, the arena's measured copy
        Fixture f = make(1945, 24576);
        f.pair_bytes = pair_b;
        std::vector<uint8_t> tier((size_t) LAYERS * (size_t) EXPERTS, 0), resident((size_t) LAYERS * (size_t) EXPERTS, 0);
        std::vector<int64_t> heat((size_t) LAYERS * (size_t) EXPERTS, 0);
        const int64_t n_keep = (int64_t) (6000ull * MIB) / pair_b;   // 3158 pairs = the decode prefix (6000 MiB)
        for (size_t i = 0; i < 7500; ++i) { f.hot[i] = 1; resident[i] = 1; }
        for (size_t i = 7500; i < 7500 + 4615; ++i) tier[i] = 1;
        for (size_t i = 7500 + 4615; i < 7500 + 4615 + n_keep; ++i) resident[i] = 1;   // the cap's keep (floor band)
        for (size_t i = 7500 + 4615 + n_keep; i < 7500 + 4615 + n_keep + 1500; ++i) heat[i] = 40;   // decode's band, evicted
        const uint64_t resident_b = (7500ull + (uint64_t) n_keep) * pair_b;            // the boot landing (hot + prefix)
        const auto p = relief_arena_reorder_plan(LAYERS, EXPERTS, 20000ull * MIB, 6000 * MIB, f.profile,
                                                 f.hot, tier, resident, heat, [&](int64_t) { return pair_b; });
        check(p.swaps == 1500, "the whole demanding band promotes against the keep's untouched cold end");
        check(p.resident_after == resident_b, "net 0: residency holds at the floor's band - the RIGHT pairs at the SAME price");
        check(p.resident_after >= 7500ull * pair_b, "the hot set's bytes never demote");
        check(p.promote.front() == f.profile[7500 + 4615 + n_keep], "the promote starts at the hottest demanding eviction");
        check(p.demote.front() == f.profile[7500 + 4615 + n_keep - 1], "the demote starts at the keep's profile-coldest pair (zero heat)");
    }

    // 23. Ties break on the profile: equal heat promotes the hotter rank first and demotes the colder rank
    //     first, so the re-rank is deterministic with the boot cap's ordering.
    {
        Fixture f = make(1024, 24576);
        std::vector<uint8_t> tier((size_t) LAYERS * (size_t) EXPERTS, 0), resident((size_t) LAYERS * (size_t) EXPERTS, 0);
        std::vector<int64_t> heat((size_t) LAYERS * (size_t) EXPERTS, 0);
        for (size_t i = 0; i < 4615; ++i) tier[i] = 1;
        for (size_t i = 4615; i < 15000; ++i) resident[i] = 1;
        for (size_t i = 15000; i < 15010; ++i) heat[i] = 10;    // ten equal-heat evictions
        for (size_t i = 4615; i < 4625; ++i) heat[i] = 0;       // the demote targets: zero heat
        for (size_t i = 4625; i < 15000; ++i) heat[i] = 5;      // the band's rest demands 5
        const auto p = relief_arena_reorder_plan(LAYERS, EXPERTS, 15000 * MIB, 0, f.profile, f.hot, tier,
                                                 resident, heat, bytes_of(f));
        check(p.swaps == 10, "ten equal-heat evictions, ten demotes");
        check(p.promote[0] == f.profile[15000] && p.promote[9] == f.profile[15009],
              "equal heat: the promote order is the profile's hot end");
        check(p.demote[0] == f.profile[4624] && p.demote[9] == f.profile[4615],
              "equal heat: the demote order is the profile's cold end");
    }

    // 24. Round 9 - the heat MIX (arena_rerank_heat).  tag 0 is the mixed counter, tag 1 the decode bank,
    //     tag 2 the decode bank + read_weight/8 of the read bank (the read bank = mixed - decode).
    {
        check(arena_rerank_heat(0, 2, 64, 0) == 64, "tag 0: the mixed counter, unchanged");
        check(arena_rerank_heat(1, 2, 64, 0) == 0, "tag 1: the read bank alone earns nothing");
        check(arena_rerank_heat(1, 2, 64, 9) == 9, "tag 1: the decode bank passes through");
        check(arena_rerank_heat(2, 0, 64, 9) == 9, "tag 2 with read_weight 0 is exactly tag 1");
        check(arena_rerank_heat(2, 2, 64, 0) == 16, "tag 2: a read-only pair keeps 2/8 of its read heat");
        check(arena_rerank_heat(2, 8, 64, 0) == 64, "tag 2 with read_weight 8 is the read bank at full weight");
        check(arena_rerank_heat(2, 2, 100, 40) == 55, "tag 2: 40 decode + (100-40) read x 2/8");
        check(arena_rerank_heat(2, 2, 30, 40) == 40, "tag 2: a bank that lags the decode bank adds no negative floor");
        check(arena_rerank_heat(3, 2, 64, 9) == 22, "a tag above 2 behaves as tag 2 (9 decode + 55 read x 2/8)");
    }

    // 25. Round 9 - the panel boundary, replayed on the v0227 iq3_s shape.  The boot cap's keep [0,12000) is
    //     the long read's SWEEP (10000 pairs heat 240, decode never touches them), 1000 residents are cold in
    //     both banks, 1000 residents are the CURRENT decode set (30/30).  The cap's evictions hold the read
    //     sweep's tail (1800 pairs, mixed 120, decode 0) and the warm generation's decode set (1200 pairs,
    //     30/30 - the pairs the 2nd gen swaps in).  The band is cap-bound (12000 MiB of 1 MiB pairs), so every
    //     promote is paid by a demote.  Tag 0 starves decode's set behind the read-heat evictions (the round-7
    //     finding); tag 1 converges it and burns 200 sweep pairs (the round-8 harm); tag 2 converges it on the
    //     zero-heat pairs and leaves the sweep's placement home.
    {
        const auto panel = [&](int64_t tag, int64_t rw) {
            Fixture f = make(1024, 24576);
            std::vector<uint8_t> tier((size_t) LAYERS * (size_t) EXPERTS, 0), resident((size_t) LAYERS * (size_t) EXPERTS, 0);
            std::vector<int64_t> mixed((size_t) LAYERS * (size_t) EXPERTS, 0), decode((size_t) LAYERS * (size_t) EXPERTS, 0);
            for (size_t i = 0; i < 12000; ++i) resident[i] = 1;                       // the boot cap's keep
            for (size_t i = 0; i < 11000; ++i) mixed[i] = 240;                        // the long read's sweep (resident)
            for (size_t i = 13200; i < 15000; ++i) mixed[i] = 120;                    // the sweep's evicted tail
            for (size_t i = 8000; i < 9000; ++i) { mixed[i] = 30; decode[i] = 30; }   // the current decode set (resident)
            for (size_t i = 12000; i < 13200; ++i) { mixed[i] = 30; decode[i] = 30; } // the warm gen's swap-ins (evicted)
            std::vector<int64_t> heat((size_t) LAYERS * (size_t) EXPERTS);
            for (size_t i = 0; i < heat.size(); ++i) heat[i] = arena_rerank_heat(tag, rw, mixed[i], decode[i]);
            return relief_arena_reorder_plan(LAYERS, EXPERTS, 12000 * MIB, 0, f.profile, f.hot, tier,
                                             resident, heat, bytes_of(f));
        };
        auto index_of = [](const std::pair<int32_t, int32_t>& p) { return (size_t) p.first * (size_t) EXPERTS + (size_t) p.second; };
        auto count_in = [&](const std::vector<std::pair<int32_t, int32_t>>& v, size_t lo, size_t hi) {
            size_t n = 0;
            for (const auto& p : v)
                if (index_of(p) >= lo && index_of(p) < hi) ++n;
            return n;
        };
        const auto p0 = panel(0, 2), p1 = panel(1, 2), p2 = panel(2, 2), p2w8 = panel(2, 8);

        // Tag 0 (mixed heat): the read sweep's evictions (120) outrank the warm set (30), so the strict-win
        // loop spends its swaps promoting read-phase pairs and demoting the current decode set, and decode's
        // warm set gets nothing.
        check(p0.swaps == 1800, "tag 0: 1800 swaps, all spent on the read sweep's evictions");
        check(count_in(p0.promote, 12000, 13200) == 0, "tag 0: decode's warm set never promotes - the band never converges (round 7)");
        check(count_in(p0.demote, 8000, 9000) == 800, "tag 0: 800 of the current decode set's residents demote against the read-heat promotions");

        // Tag 1 (decode bank): the whole warm set lands in one boundary, and the demote reaches the sweep.
        check(p1.swaps == 1200, "tag 1: decode's whole set converges in one boundary (the round-8 warm recovery)");
        check(count_in(p1.promote, 12000, 13200) == 1200, "tag 1: every warm-set pair promotes");
        check(count_in(p1.demote, 0, 11000) == 200, "tag 1: 200 read-sweep pairs demote once the zero-heat pool runs out (the round-8 first-pass harm)");

        // Tag 2 (decode + read/4 floor): decode's set promotes first, only the both-bank-cold pairs demote,
        // and the strict-win rule stops before touching the sweep.
        check(p2.swaps == 1000, "tag 2: the zero-heat residents pay 1000 swaps, then the sweep's floor stops the loop");
        check(p2.promote[0] == std::make_pair((int32_t) (12000 / EXPERTS), (int32_t) (12000 % EXPERTS)),
              "tag 2: the promote starts at decode's own set (the recovery keeps its direction)");
        check(count_in(p2.promote, 12000, 13200) == 1000, "tag 2: 1000 of the warm set lands this boundary, the rest converges next");
        check(count_in(p2.demote, 0, 11000) == 0, "tag 2: the read sweep's placement survives the boundary (the first-pass set stays home)");
        check(count_in(p2.demote, 8000, 9000) == 0, "tag 2: the current decode set's residents are not demoted");

        // read_weight is the dial between the two known behaviors: 0 is tag 1, 8 puts the read bank back on top.
        const auto p2w0 = panel(2, 0);
        check(p2w0.swaps == 1200 && count_in(p2w0.demote, 0, 11000) == 200, "tag 2 read_weight 0 is exactly tag 1 (the harm returns)");
        check(p2w8.swaps == 1800 && count_in(p2w8.promote, 12000, 13200) == 0,
              "tag 2 read_weight 8 restores the mixed-heat starvation - the read bank must stay a floor, not a ranker");
        check(count_in(p2w8.demote, 8000, 9000) == 800, "tag 2 read_weight 8 burns the current decode set's residents, as tag 0 does");
    }

    if (failures) {
        std::printf("expert_helper_arena_cap_test: %d FAILURES\n", failures);
        return 1;
    }
    std::printf("expert_helper_arena_cap_test: all checks passed\n");
    return 0;
}
