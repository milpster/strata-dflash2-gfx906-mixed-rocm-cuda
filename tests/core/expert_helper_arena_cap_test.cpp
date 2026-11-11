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

    if (failures) {
        std::printf("expert_helper_arena_cap_test: %d FAILURES\n", failures);
        return 1;
    }
    std::printf("expert_helper_arena_cap_test: all checks passed\n");
    return 0;
}
