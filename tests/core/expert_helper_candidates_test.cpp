// tests/core/expert_helper_candidates_test.cpp - M1 (capacity): the pure candidate, budget and
// promotion-gate arithmetic the engine's expert-helper path runs (include/strata/core/
// expert_helper_candidates.hpp).  CPU-only by construction: no device, no socket, no pack, no GPU
// runtime and NO library linked at all - masks and profile pairs in, candidate lists, slot counts and
// aligned byte totals out.
//
// THREE kinds of evidence, kept apart:
//   PRODUCTION - checks that call the functions the engine calls (eligibility, budget plan, gate);
//   STATIC - text contracts on the ACTUAL sources (src/program/generate.cpp, the helper's fill) under
//     STRATA_HELPER_CAND_SOURCE_ROOT: the wiring a standalone predicate cannot show.  Printed as
//     STATIC, never as executed behaviour, and SKIPPED (not passed) when the files are unreadable.
#include "strata/core/expert_helper_candidates.hpp"

#include "strata/core/expert_cache.hpp"   // the slot layout's own alignment rule (the pin below)

#include <climits>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

using namespace strata::core::expert_helper;

namespace {

int failures = 0;
void check(bool ok, const char* what) {
    std::printf("  %-74s %s\n", what, ok ? "ok" : "FAIL");
    if (!ok) ++failures;
}
void check_rc(int rc, int want, const char* what) {
    check(rc == want, what);
    if (rc != want) std::printf("      rc=%d want=%d\n", rc, want);
}

typedef std::pair<int32_t, int32_t> Pair;
const int kLayers = 48, kExperts = 512;

std::vector<uint8_t> mask_all(int64_t n, uint8_t v) { return std::vector<uint8_t>((size_t) n, v); }

// A geometry the pack's own shapes make real (measured in the design's capacity map: the model
// alternates blob sizes per layer type, 1,868,800 / 2,329,600 / 2,129,920 B): 48 layers, 512 experts.
int64_t blob_of(int64_t l) {
    const int64_t sizes[3] = { 1868800, 2329600, 2129920 };
    return sizes[(size_t) (l % 3)];
}
std::vector<int64_t> blobs() {
    std::vector<int64_t> b((size_t) kLayers);
    for (int64_t l = 0; l < kLayers; ++l) b[(size_t) l] = blob_of(l);
    return b;
}

// A canonical GLOBAL ranking over all 48 layers, hottest first, in an order that is NOT layer order:
// flat index (l * kExperts + e) walked with a step coprime to 24,576 (25 is: 24,576 = 2^13 * 3), so
// every pair appears EXACTLY ONCE and consecutive ranks land in scattered layers.  "The result keeps
// the input's rank order" is then a real assertion, not a tautology, and a rank-ordered prefix is a
// set spread over all 48 layers - which is what the profile the fill consumes actually looks like.
std::vector<Pair> global_ranking() {
    std::vector<Pair> v;
    v.reserve((size_t) kLayers * kExperts);
    const int64_t n = (int64_t) kLayers * kExperts;
    for (int64_t r = 0; r < n; ++r) {
        const int64_t flat = (r * 25 + 7) % n;
        v.push_back(Pair((int32_t) (flat / kExperts), (int32_t) (flat % kExperts)));
    }
    return v;
}

// ================= (1) the alignment rule, pinned against its owner =================
void alignment_checks() {
    std::printf("(1) aligned slot bytes == ExpertCache::slot_stride(blob, 0), and its overflow bound\n");
    const int64_t vals[] = { 0, 1, 255, 256, 257, 511, 512, 1868800, 2329600, 2129920, 1234567,
                             1LL << 30, (1LL << 40) - 1 };
    bool all = true;
    for (const int64_t b : vals) {
        if (slot_bytes(b) != strata::core::ExpertCache::slot_stride(b, 0)) all = false;
    }
    check(all, "slot_bytes() equals the slot owner's expression on the pack's shapes and edges");
    check(slot_bytes(1868800) == 1868800, "a 256-divisible blob needs no padding (1,868,800 B)");
    check(slot_bytes(1234567) == 1234688, "an odd blob pads UP to the granule (1,234,567 -> 1,234,688)");
    check(slot_bytes(1) == 256, "one byte of blob costs a whole granule (256 B)");

    // The checked form's own bound: slot_stride ADDS 255 before it divides, so `blob + 255` is the
    // overflow point and the check runs before that addition, never after.
    int64_t out = -7;
    check(!slot_bytes_safe(-1, out) && out == -7, "a negative blob is refused and out is untouched");
    check(!slot_bytes_safe(INT64_MIN, out) && out == -7, "INT64_MIN is refused (it is negative)");
    check(!slot_bytes_safe(INT64_MAX, out) && out == -7, "INT64_MAX is refused: +255 would pass int64");
    check(slot_bytes_safe(INT64_MAX - 255, out), "INT64_MAX - 255 is the largest representable blob");
    check(out == (INT64_MAX - (INT64_MAX % 256)),
          "its aligned slot rounds DOWN inside int64 (no wrap to a small slot)");
    check(slot_bytes_safe(0, out) && out == 0, "a zero blob aligns to zero (the guard's edge, not a granule)");
    check(slot_bytes_safe(1, out) && out == 256, "the safe form agrees with slot_bytes on a normal value");
    bool agree = true;
    for (const int64_t b : vals) {
        int64_t o = 0;
        if (!slot_bytes_safe(b, o) || o != slot_bytes(b)) agree = false;
    }
    check(agree, "the safe form equals slot_bytes wherever the precondition holds");
}

// ================= (2) all-layer eligibility and rank order =================
void eligibility_checks() {
    std::printf("(2) every layer is eligible; the global rank order is preserved\n");
    const std::vector<Pair> ranked = global_ranking();
    const std::vector<uint8_t> owned = mask_all((int64_t) kLayers * kExperts, 0);
    const std::vector<uint8_t> had = mask_all(kLayers, 0);
    std::vector<Pair> out;
    CandidateStats st;
    std::string why;
    check_rc(build_candidates(ranked, owned, had, kLayers, kExperts, 0, kLayers - 1, out, st, why),
             kOk, "build_candidates accepts the full-geometry window 0..47");
    check(st.kept == (int64_t) kLayers * kExperts, "all 24,576 pairs are eligible with no owner and no fold");
    check(st.outside_window == 0 && st.owned == 0 && st.hadamard == 0 && st.duplicates == 0 && st.invalid == 0,
          "nothing is skipped in the clean case (the counts say so)");
    bool same_order = out.size() == ranked.size();
    for (size_t i = 0; same_order && i < out.size(); ++i)
        if (out[i] != ranked[i]) same_order = false;
    check(same_order, "the kept list IS the input's rank order, pair for pair (no re-sort, no stage concat)");
    bool every_layer = true;
    for (int64_t l = 0; l < kLayers; ++l) {
        bool found = false;
        for (const auto& pr : out)
            if (pr.first == (int32_t) l) { found = true; break; }
        if (!found) every_layer = false;
    }
    check(every_layer, "each of the 48 layers has eligible pairs (layer 0 and layer 47 included)");
    size_t first_l47 = out.size(), rank_of_l47 = 0;
    for (size_t i = 0; i < out.size(); ++i)
        if (out[i].first == 47) { first_l47 = i; break; }
    for (size_t i = 0; i < out.size(); ++i)
        if (out[i].first == 47) ++rank_of_l47;
    check(first_l47 < out.size() && rank_of_l47 == kExperts,
          "layer 47's pairs enter at THEIR ranks, not appended after layers 0..31");

    // An explicit upper bound keeps its documented meaning: an INCLUSIVE top layer, clamped to the model.
    std::vector<Pair> w_out;
    CandidateStats w_st;
    check_rc(build_candidates(ranked, owned, had, kLayers, kExperts, 0, 39, w_out, w_st, why), kOk,
             "the explicit top layer (39) is accepted");
    check(w_st.kept == 40 * kExperts && w_st.outside_window == 8 * kExperts,
          "--expert-helper-layers keeps inclusive-end semantics (40 layers kept, 8 outside)");
    check_rc(build_candidates(ranked, owned, had, kLayers, kExperts, 32, 31, w_out, w_st, why), kErrGeometry,
             "an empty window (lo > hi) is a named geometry refusal, not an empty success");
    check(!why.empty(), "the refusal carries the reason text");
}

// ================= (3) the exclusions, with counted reasons =================
void exclusion_checks() {
    std::printf("(3) owned pairs, Hadamard layers, invalid pairs and duplicates are excluded by name\n");
    const std::vector<uint8_t> had = mask_all(kLayers, 0);
    std::vector<uint8_t> owned = mask_all((int64_t) kLayers * kExperts, 0);
    // The primary owns (3, 10); a SPLIT STAGE owns (40, 11) - outside the primary's old 32..47 fence
    // AND outside its own stage's layers, which is exactly what M1 makes visible.
    owned[(size_t) (3 * kExperts + 10)] = 1;
    owned[(size_t) (40 * kExperts + 11)] = 1;
    std::vector<Pair> ranked;
    ranked.push_back(Pair(3, 10));        // primary-owned
    ranked.push_back(Pair(40, 11));       // stage-owned
    ranked.push_back(Pair(3, 10));        // a repeat of an owned pair
    ranked.push_back(Pair(7, 20));        // eligible
    ranked.push_back(Pair(7, 20));        // a repeat of an eligible pair (first rank wins)
    ranked.push_back(Pair(41, 5));        // Hadamard layer below
    ranked.push_back(Pair(-1, 0));        // invalid layer
    ranked.push_back(Pair(kLayers, 0));   // invalid layer (past the last)
    ranked.push_back(Pair(0, kExperts));  // invalid expert
    ranked.push_back(Pair(50, 9));        // invalid: past the last layer (geometry, checked first)
    std::vector<uint8_t> had2 = had;
    had2[41] = 1;
    std::vector<Pair> out;
    CandidateStats st;
    std::string why;
    check_rc(build_candidates(ranked, owned, had2, kLayers, kExperts, 0, 47, out, st, why), kOk,
             "the mixed exclusion case runs");
    check(st.considered == 10, "every ranked pair is counted (10 considered)");
    check(st.kept == 1 && out.size() == 1 && out[0] == Pair(7, 20), "exactly one pair survives: (7,20)");
    check(st.owned == 3,
          "the primary-owned pair, the stage-owned pair and the repeat of the owned pair are all owned");
    check(st.hadamard == 1, "the Hadamard-folded layer's pair is excluded, counted");
    check(st.invalid == 4,
          "the negative layer, the past-the-last layer, the past-the-last expert and the layer past the "
          "last are geometry (the geometry check comes first)");
    check(st.outside_window == 0, "with the full window nothing is outside it");
    check(st.duplicates == 1, "the repeat of the KEPT pair is a duplicate (the first rank wins)");
    std::vector<Pair> wout;
    CandidateStats wst;
    const std::vector<Pair> wranked{ Pair(45, 9), Pair(46, 9), Pair(44, 9) };
    check_rc(build_candidates(wranked, mask_all((int64_t) kLayers * kExperts, 0), had, kLayers, kExperts,
                              0, 44, wout, wst, why),
             kOk, "a valid pair above the explicit top layer");
    check(wst.outside_window == 2 && wst.kept == 1 && wout[0] == Pair(44, 9),
          "the explicit top layer is INCLUSIVE and the outside pairs are counted");

    // Disjointness as a set property: no candidate is owned, for the UNION of the owners.
    bool disjoint = true;
    for (const auto& pr : out)
        if (owned[(size_t) (pr.first * kExperts + pr.second)]) disjoint = false;
    check(disjoint, "the candidate set is disjoint from the owner mask (primary AND stages)");
    const std::vector<uint8_t> owned_all = mask_all((int64_t) kLayers * kExperts, 1);
    std::vector<Pair> out2;
    CandidateStats st2;
    check_rc(build_candidates(global_ranking(), owned_all, had, kLayers, kExperts, 0, 47, out2, st2, why), kOk,
             "a fully owned geometry is a success with zero candidates");
    check(st2.kept == 0 && out2.empty() && st2.owned == (int64_t) kLayers * kExperts,
          "nothing is kept and the refusal reason is the owner count (no silent empty set)");

    // The pair-count product is checked BEFORE it is computed: the mask is indexed by n_layers *
    // n_expert, so a product past an int64 index is a named refusal, never a wrapped small count.
    std::vector<Pair> tiny{ Pair(0, 0), Pair(1, 3) };
    std::vector<Pair> out3;
    CandidateStats st3;
    check_rc(build_candidates(tiny, {}, {}, INT64_MAX / 2, 4, 0, 1, out3, st3, why), kErrGeometry,
             "an n_layers x n_expert product past int64 is refused before the product is computed");
    check(out3.empty() && why.find("past what a pair index can represent") != std::string::npos,
          "the product refusal names the geometry it refused");
    std::vector<uint8_t> big_owned((size_t) 8, 0);
    big_owned[7] = 1;
    check_rc(build_candidates(tiny, big_owned, mask_all(2, 0), INT64_MAX / 4, 4, 0, 1, out3, st3, why), kOk,
             "the largest product at the bound plans normally (no false refusal at the edge)");
    check(st3.kept == 1 && out3[0] == Pair(0, 0) && st3.owned == 1,
          "the mask is indexed by the guarded product (pair (1,3) is the owned one)");
    check_rc(build_candidates(tiny, {}, {}, -3, kExperts, 0, 47, out3, st3, why), kErrGeometry,
             "a negative layer count is refused");
    check_rc(build_candidates(tiny, {}, {}, kLayers, 0, 0, 47, out3, st3, why), kErrGeometry,
             "a zero expert count is refused (it would also divide by zero)");
}

// ================= (4) the byte budget buys slots, exactly =================
void budget_checks() {
    std::printf("(4) --expert-helper-mib is exact ALIGNED slot bytes, and --expert-helper-slots stays a count\n");
    const std::vector<int64_t> b = blobs();
    // The first three candidates' exact aligned bytes, hand-computed by the pin in section (1).
    std::vector<Pair> cands;
    cands.push_back(Pair(0, 0));   // 1,868,800
    cands.push_back(Pair(1, 0));   // 2,329,600
    cands.push_back(Pair(2, 0));   // 2,129,920
    cands.push_back(Pair(3, 0));   // 1,868,800
    FillPlan p;
    std::string why;
    const int64_t s0 = slot_bytes(b[0]), s1 = slot_bytes(b[1]), s2 = slot_bytes(b[2]);
    check_rc(plan_fill(cands, b, 0, 0, kMaxPairs, p, why), kOk, "budget 0 = AUTO");
    check(p.auto_budget && p.slots == 4 && p.bytes == s0 + s1 + s2 + slot_bytes(b[3]),
          "AUTO asks for EVERY eligible pair and reports its exact aligned bytes");
    check(!p.budget_bounded && !p.slot_cap_bounded && !p.ceiling_bounded,
          "AUTO is bounded by nothing: the helper's card budget decides the fill");

    check_rc(plan_fill(cands, b, s0 + s1, 0, kMaxPairs, p, why), kOk, "a budget that covers exactly two slots");
    check(p.slots == 2 && p.bytes == s0 + s1 && p.budget_bounded,
          "the byte cap stops the scan after two slots, at its exact byte total");
    check_rc(plan_fill(cands, b, s0 + s1 + s2 - 1, 0, kMaxPairs, p, why), kOk,
             "a budget one byte short of three slots");
    check(p.slots == 2 && p.bytes == s0 + s1, "the third slot is not bought by a byte-short budget (no rounding up)");

    check_rc(plan_fill(cands, b, s0 + s1 + s2 + s0 - 1, 3, kMaxPairs, p, why), kOk,
             "the count cap (3) binds before the byte budget does");
    check(p.slots == 3 && p.slot_cap_bounded && !p.budget_bounded,
          "slots is the EXPLICIT cap, the byte budget did not bind");
    check_rc(plan_fill(cands, b, s0, 100, kMaxPairs, p, why), kOk,
             "the byte budget (1 slot) binds before the count cap (100)");
    check(p.slots == 1 && p.budget_bounded && !p.slot_cap_bounded, "count and byte caps interact: the tighter binds");

    check_rc(plan_fill(cands, b, s0 - 1, 0, kMaxPairs, p, why), kOk,
             "a budget too small for one aligned slot is a success with ZERO slots");
    check(p.slots == 0 && p.budget_bounded && !why.empty(),
          "the refusal names the budget and both slot figures - no slot is forced past the cap");
    check(p.first_slot == s0, "the plan reports the FIRST RANKED candidate's aligned slot");
    check(p.min_slot == s0, "and the smallest slot the candidates buy");

    // The refusal's "smallest" must be the TRUE minimum over the whole set: the budget scan stops at
    // the first ranked pair that does not fit, so a prefix's minimum is not the set's minimum.  Here
    // the first ranked slot is the LARGEST and the minimum sits at the END of the list.
    std::vector<Pair> mixed{ Pair(1, 0), Pair(0, 0), Pair(2, 0) };   // 2,329,600 / 1,868,800 / 2,129,920
    const int64_t smax = slot_bytes(b[1]), smin = slot_bytes(b[0]);
    check_rc(plan_fill(mixed, b, smax - 1, 0, kMaxPairs, p, why), kOk,
             "a budget below the first ranked slot, with a smaller slot LATER in the ranking");
    check(p.slots == 0 && p.first_slot == smax && p.min_slot == smin,
          "the plan's figures are the first ranked slot and the GLOBAL minimum, not a prefix minimum");
    check(why.find(std::to_string(smax)) != std::string::npos &&
          why.find(std::to_string(smin)) != std::string::npos,
          "the refusal text QUOTES both numbers (first ranked, and smallest any candidate buys)");

    check_rc(plan_fill({}, b, 0, 0, kMaxPairs, p, why), kOk, "no candidates at all");
    check(p.slots == 0 && p.candidates == 0 && !why.empty(),
          "zero candidates plans zero slots with a reason (the caller keeps the CPU path)");

    // The transport's ceiling on the fill frame's pair count is honored, and it is reported.
    std::vector<Pair> many((size_t) 1000, Pair(0, 0));
    check_rc(plan_fill(many, b, 0, 0, 500, p, why), kOk, "a slot ceiling below the candidate count");
    check(p.slots == 500 && p.ceiling_bounded && p.bytes == 500 * s0,
          "the ceiling bounds the plan and the byte total follows it exactly");

    // No overflow, ever: a huge per-slot blob is refused by name, not wrapped into a small total.
    std::vector<int64_t> huge_b((size_t) kLayers, (int64_t) 5000000000000000000LL);   // 5 EB a slot
    check_rc(plan_fill(std::vector<Pair>{ Pair(0, 0), Pair(1, 0) }, huge_b, INT64_MAX, 0, kMaxPairs, p, why),
             kErrGeometry, "two 5 EB slots overflow a byte total: named geometry refusal");
    check(p.slots == 0 && !why.empty(), "the overflow refusal allocates nothing (no negative byte total)");
    check_rc(plan_fill(cands, b, INT64_MAX, 0, kMaxPairs, p, why), kOk,
             "the largest representable budget is a normal budget");
    check(p.slots == 4 && p.bytes < INT64_MAX, "a real pack's bytes stay far below the overflow bound");

    // The ALIGNMENT's own overflow, checked BEFORE the padding runs (slot_stride adds 255 first):
    // a blob at int64's edge must never become a small slot through a wrapped addition.
    std::vector<int64_t> edge_b((size_t) kLayers, INT64_MAX);
    check_rc(plan_fill(std::vector<Pair>{ Pair(0, 0) }, edge_b, INT64_MAX, 0, kMaxPairs, p, why),
             kErrGeometry, "a blob of INT64_MAX is refused by name (its +255 padding is unrepresentable)");
    check(p.slots == 0 && p.candidates == 1 && !why.empty(),
          "the alignment refusal reports zero slots and carries its reason");
    edge_b[0] = INT64_MAX - 254;   // INT64_MAX - 255 is the bound; this is one past it
    check_rc(plan_fill(std::vector<Pair>{ Pair(0, 0) }, edge_b, INT64_MAX, 0, kMaxPairs, p, why),
             kErrGeometry, "one blob past the alignment bound is refused");
    edge_b[0] = INT64_MAX - 255;   // exactly the bound: representable
    check_rc(plan_fill(std::vector<Pair>{ Pair(0, 0) }, edge_b, INT64_MAX, 0, kMaxPairs, p, why),
             kOk, "a blob exactly at the alignment bound plans a slot");
    check(p.slots == 1 && p.bytes == INT64_MAX - (INT64_MAX % 256),
          "the edge slot's bytes stay inside int64 (rounded down, never wrapped)");
    check_rc(plan_fill(std::vector<Pair>{ Pair(0, 0), Pair(1, 0) }, edge_b, INT64_MAX, 0, kMaxPairs, p, why),
             kErrGeometry, "two edge-sized slots overflow the byte total: the sum's bound is checked too");
    std::vector<int64_t> neg_b = b;
    neg_b[2] = -5;
    check_rc(plan_fill(cands, neg_b, 0, 0, kMaxPairs, p, why), kErrGeometry,
             "a negative blob size is a named refusal, not a negative slot");
    check(p.slots == 0, "the negative-blob refusal allocates nothing");

    // A candidate whose layer has no blob size is a refusal, not a zero-sized slot.
    std::vector<int64_t> gap_b = b;
    gap_b[1] = 0;
    check_rc(plan_fill(cands, gap_b, 0, 0, kMaxPairs, p, why), kErrGeometry,
             "a candidate layer with no usable blob size is a named refusal");
    check_rc(plan_fill(cands, std::vector<int64_t>((size_t) 2, 1868800), 0, 0, kMaxPairs, p, why), kErrGeometry,
             "a candidate layer past a short blob table is a named refusal");
}

// ================= (5) the MiB CLI value is strict =================
void parse_checks() {
    std::printf("(5) --expert-helper-mib: strict CLI validation, unrepresentable values refused\n");
    int64_t mib = -1;
    std::string why;
    check_rc(parse_mib("0", mib, why), kOk, "\"0\" parses (AUTO)");
    check(mib == 0, "0 MiB is the AUTO value");
    check_rc(parse_mib("3400", mib, why), kOk, "\"3400\" parses");
    check(mib == 3400, "the value is exact");
    check_rc(parse_mib(std::to_string(kMaxMiB), mib, why), kOk, "the largest representable MiB parses");
    check(mib == kMaxMiB, "the byte value of the largest MiB fits int64");
    check_rc(parse_mib(std::to_string(kMaxMiB + 1), mib, why), kErrGeometry,
             "one MiB past the representable byte count is refused");
    check(!why.empty(), "the refusal says what is representable");
    check_rc(parse_mib("12x", mib, why), kErrGeometry, "\"12x\" is refused (atoll would read 12)");
    check_rc(parse_mib(" 7", mib, why), kErrGeometry, "a leading space is refused");
    check_rc(parse_mib("-5", mib, why), kErrGeometry, "a negative budget is refused");
    check_rc(parse_mib("", mib, why), kErrGeometry, "an empty value is refused");
    check_rc(parse_mib("99999999999999999999999999", mib, why), kErrGeometry,
             "a digit string past int64 is refused (no wrap)");
}

// ================= (6) the adaptive-promotion gate =================
void gate_checks() {
    std::printf("(6) STRATA_DISJOINT_ADAPT: explicit value wins, unset follows the registered proxy\n");
    check(disjoint_adapt_enabled(nullptr, true) == true, "env unset + registered proxy: ON (the M1 default)");
    check(disjoint_adapt_enabled(nullptr, false) == false,
          "env unset + no helper: OFF - the default path is the pre-M1 behavior, byte for byte");
    check(disjoint_adapt_enabled("0", true) == false, "=0 opts OUT even with a registered proxy");
    check(disjoint_adapt_enabled("0", false) == false, "=0 with no helper: OFF");
    check(disjoint_adapt_enabled("1", false) == true, "=1 opts IN with no helper (the pre-M1 opt-in)");
    check(disjoint_adapt_enabled("1", true) == true, "=1 with a proxy: ON");
    check(disjoint_adapt_enabled("", true) == false, "an empty value reads as OFF (the engine's own atoi rule)");
    check(disjoint_adapt_enabled("abc", true) == false, "junk reads as OFF (the engine's own atoi rule)");
    check(disjoint_adapt_enabled("0000", true) == false, "\"0000\" reads as OFF");
    check(disjoint_adapt_enabled("2", true) == true, "any nonzero value reads as ON");
}

// ================= (7) STATIC: the wiring the predicate alone cannot show =================
std::string g_root = STRATA_HELPER_CAND_SOURCE_ROOT;
bool read_all(const char* path, std::string& out) {
    std::ifstream f(path);
    if (!f) return false;
    std::ostringstream ss;
    ss << f.rdbuf();
    out = ss.str();
    return true;
}
bool has(const std::string& s, const char* needle) { return s.find(needle) != std::string::npos; }
size_t count_of(const std::string& s, const char* needle) {
    size_t n = 0, at = 0;
    const std::string nd(needle);
    while ((at = s.find(nd, at)) != std::string::npos) { ++n; at += nd.size(); }
    return n;
}

void static_checks() {
    std::printf("(7) STATIC text contract on the real sources (NOT executed behaviour)\n");
    std::string gen, helper;
    const bool ok_gen = read_all((g_root + "/src/program/generate.cpp").c_str(), gen);
    const bool ok_helper = read_all((g_root + "/tools/mixed_gpu/sidecar/helper_main.cpp").c_str(), helper);
    check(ok_gen, "static: src/program/generate.cpp is readable");
    check(ok_helper, "static: tools/mixed_gpu/sidecar/helper_main.cpp is readable");
    if (!ok_gen) {
        std::printf("      STATIC SKIPPED: the engine source was not readable\n");
        return;
    }
    // The gate is wired: the engine asks the pure predicate, with the env value and the proxy's own state.
    check(has(gen, "expert_helper::disjoint_adapt_enabled(") &&
              has(gen, "std::getenv(\"STRATA_DISJOINT_ADAPT\")"),
          "static: generate.cpp computes disjoint_adapt through the shared predicate + the env");
    check(has(gen, "helper_tier_p != nullptr && helper_tier_p->is_proxy()"),
          "static: the default-ON condition is a REGISTERED proxy tier, not an unconditional flag");
    // Every adaptive-promotion callsite consults helper_holds: the three `cand.emplace_back(u[e], e)` gates.
    check(count_of(gen, "!helper_holds(l, e)") >= 3,
          "static: every promotion callsite (>= 3) consults helper_holds before admitting a pair");
    check(has(gen, "if (t->is_proxy() && !t->proxy_residency_live()) continue;"),
          "static: the gate skips a proxy only on a CLOSED channel (residency released), not on a "
          "per-request disable");
    check(!has(gen, "if (t->is_proxy() && !t->helper_usable()) continue;"),
          "static: the gate does NOT use the dispatch predicate (a mid-request disable keeps ownership)");
    check(has(gen, "if (t->holds(l, e)) return true;"), "static: the gate asks each tier LIVE");
    // M1's capacity wiring: all-layer window, the global ranking, exact bytes, no magic slot count.
    check(has(gen, "const int64_t hlo = 0;"), "static: the candidate window starts at layer 0");
    check(has(gen, "multi_gpu ? profile_all : profile"),
          "static: the candidates come from the canonical global ranking (profile_all before the split)");
    check(!has(gen, "stages.back()->profile"),
          "static: the last-stage profile fence is gone (no stage-local candidate source remains)");
    check(!has(gen, "o.expert_helper_slots > 0 ? o.expert_helper_slots : 256"),
          "static: the implicit 256-slot default is gone");
    check(has(gen, "expert_helper::plan_fill(") && has(gen, "expert_helper::build_candidates("),
          "static: the engine calls the pure candidate builder and the budget planner");
    check(has(gen, "--expert-helper-mib") && has(gen, "expert_helper::parse_mib("),
          "static: --expert-helper-mib is parsed strictly (not atoll)");
    check(has(gen, "o.expert_helper_mib > 0 ? o.expert_helper_mib * 1048576ll : 0"),
          "static: the budget is MiB converted to BYTES for the planner");
    check(has(gen, "helper_tier.open_socket(o.expert_helper_socket, hplan.slots,") &&
              has(gen, "hlo, hhi,") && has(gen, "hplan, hwhy);"),
          "static: the planned slot count is what goes on the wire (no separate count is invented)");
    check(has(gen, "if (!o.expert_helper_socket.empty()) {"),
          "static: the whole helper path (its masks, plan and prints) is inside the socket opt-in");
    const size_t block_start = gen.find("if (!o.expert_helper_socket.empty()) {");
    const size_t prof_all = gen.find("multi_gpu ? profile_all : profile");
    check(block_start != std::string::npos && prof_all > block_start,
          "static: the global-ranking reference lives inside the opt-in block (nothing runs when off)");
    if (ok_helper) {
        // The helper side is UNCHANGED by M1 and stays authoritative: its live budget check, its
        // claimed-bitmap skip, its slot_stride spending, and its no-implicit-cap AUTO.
        check(has(helper, "cudaMemGetInfo(&free_b, &total_b)"),
              "static: the helper's own free-VRAM check stays the authoritative bound");
        check(has(helper, "ExpertCache::slot_stride((int64_t) lay.blob_bytes(l))"),
              "static: the helper spends aligned slot bytes with no guard (the engine budgets the same)");
        check(has(helper, "g_slots > 0 ? g_slots : INT64_MAX"),
              "static: the helper's AUTO has no implicit count - the engine's request is the only cap");
        check(has(helper, "if (ci < claimed.size() && claimed[ci]) continue;"),
              "static: the helper fills nothing a cache owns (the wire's owner mask, unchanged contract)");
        check(has(helper, "if ((int64_t) pairs.size() >= max_slots) { stop = \"slots\"; break; }"),
              "static: an explicit engine count is honored as an upper bound (and named as the stop reason)");
        check(has(helper, "\"--fill-mib\"") && has(helper, "g_fill_mib > 0"),
              "static: --fill-mib is an OPT-IN byte cap on the fill (unset/0 = the budget decides)");
        check(has(helper, "fill preflight:") && has(helper, "fill stopped:"),
              "static: every fill reports the device's raw free MiB before and after and WHY it stopped");
        check(has(helper, "req.slots <= 0) rc = kErrGeometry"),
              "static: a non-positive slot request is refused by the helper (the engine never sends one)");
    } else {
        std::printf("      STATIC SKIPPED: the helper source was not readable\n");
    }
}

}  // namespace

int main() {
    alignment_checks();
    eligibility_checks();
    exclusion_checks();
    budget_checks();
    parse_checks();
    gate_checks();
    static_checks();
    std::printf("%s: %d failure(s)\n", failures == 0 ? "expert_helper_candidates_test PASS" : "FAIL", failures);
    return failures == 0 ? 0 : 1;
}
