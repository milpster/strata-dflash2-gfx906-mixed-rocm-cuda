// src/core/expert_helper_candidates.cpp - the helper's eligibility, byte budget and promotion gate
// (M1).  See the header for the contract; this file is the single source of the arithmetic.
//
// CPU-only by construction: the only dependency is the slot layout's own alignment rule
// (`ExpertCache::slot_stride`), a static expression in a header - so the test binary that pins these
// rules links no device runtime, exactly like the helper's other host-side suites.
#include "strata/core/expert_helper_candidates.hpp"

#include "strata/core/expert_cache.hpp"

#include <climits>
#include <cstdlib>

namespace strata {
namespace core {
namespace expert_helper {

int64_t slot_bytes(int64_t blob) {
    // THE one alignment rule, taken from the owner of the slots, so the engine's budget and the
    // helper's spending cannot drift apart by a granule.  The precondition (see the header) is what
    // `slot_bytes_safe` checks: `slot_stride` adds 255 before it divides, and that addition is the
    // overflow point - so nothing unrepresentable is ever added here.
    return ExpertCache::slot_stride(blob, 0);
}

bool slot_bytes_safe(int64_t blob, int64_t& out) {
    // The padding arithmetic's own bound, checked BEFORE it runs: `blob + 255` must stay inside int64.
    if (blob < 0 || blob > INT64_MAX - 255) return false;
    out = slot_bytes(blob);
    return true;
}

int build_candidates(const std::vector<std::pair<int32_t, int32_t>>& ranked,
                     const std::vector<uint8_t>& owned, const std::vector<uint8_t>& had_folded,
                     int64_t n_layers, int64_t n_expert, int64_t layer_lo, int64_t layer_hi,
                     std::vector<std::pair<int32_t, int32_t>>& out, CandidateStats& stats,
                     std::string& why) {
    out.clear();
    stats = CandidateStats();
    why.clear();
    if (n_layers <= 0 || n_expert <= 0) {
        why = "the candidate window needs a positive layer and expert count";
        return expert_helper::kErrGeometry;
    }
    if (layer_lo < 0 || layer_hi < layer_lo || layer_hi >= n_layers) {
        why = "the candidate window " + std::to_string(layer_lo) + ".." + std::to_string(layer_hi) +
              " is outside the model's layers (0.." + std::to_string(n_layers - 1) + ")";
        return expert_helper::kErrGeometry;
    }
    const int64_t pairs_limit = INT64_MAX / n_expert;   // n_expert > 0 is checked above
    if (n_layers > pairs_limit) {
        why = "the pair count " + std::to_string(n_layers) + " x " + std::to_string(n_expert) +
              " is past what a pair index can represent";
        return expert_helper::kErrGeometry;
    }
    const int64_t flat_pairs = n_layers * n_expert;      // guarded above: this cannot overflow
    // The scan touches ONLY pairs in [layer_lo, layer_hi], so the masks must cover through the
    // window's TOP layer (absolute indexing, l * n_expert + e).  The bound arithmetic is safe:
    // layer_hi < n_layers is checked above, so (layer_hi + 1) * n_expert <= flat_pairs.
    const int64_t window_top_pairs = (layer_hi + 1) * n_expert;
    if ((int64_t) owned.size() < window_top_pairs) {
        why = "the owner mask covers " + std::to_string(owned.size()) + " pairs, the window's top layer " +
              std::to_string(layer_hi) + " needs " + std::to_string(window_top_pairs);
        return expert_helper::kErrGeometry;
    }
    if ((int64_t) had_folded.size() < layer_hi + 1) {
        why = "the Hadamard mask covers " + std::to_string(had_folded.size()) + " layers, the window's top layer " +
              std::to_string(layer_hi) + " needs " + std::to_string(layer_hi + 1);
        return expert_helper::kErrGeometry;
    }
    std::vector<uint8_t> seen((size_t) window_top_pairs, 0);
    out.reserve(ranked.size());
    for (const auto& pr : ranked) {
        ++stats.considered;
        const int64_t l = pr.first, e = pr.second;
        if (l < 0 || l >= n_layers || e < 0 || e >= n_expert) { ++stats.invalid; continue; }
        if (l < layer_lo || l > layer_hi) { ++stats.outside_window; continue; }
        const size_t flat = (size_t) (l * n_expert + e);
        if (owned[flat]) { ++stats.owned; continue; }
        if (had_folded[(size_t) l]) { ++stats.hadamard; continue; }
        if (seen[flat]) { ++stats.duplicates; continue; }
        seen[flat] = 1;
        ++stats.kept;
        out.push_back(pr);
    }
    return expert_helper::kOk;
}

int plan_fill(const std::vector<std::pair<int32_t, int32_t>>& candidates,
              const std::vector<int64_t>& blob_bytes, int64_t budget_bytes, int64_t explicit_slots,
              int64_t max_slots, FillPlan& plan, std::string& why) {
    plan = FillPlan();
    why.clear();
    plan.candidates = (int64_t) candidates.size();
    plan.auto_budget = budget_bytes <= 0;
    const int64_t ceiling = max_slots > 0 ? max_slots : expert_helper::kMaxPairs;
    int64_t cap = ceiling;
    if (explicit_slots > 0 && explicit_slots < cap) cap = explicit_slots;
    if (candidates.empty()) {
        why = "no expert pair is eligible for the helper";
        return expert_helper::kOk;   // slots 0: the caller refuses the helper and keeps the CPU path
    }
    // GEOMETRY FIRST, over the WHOLE candidate list: every candidate's blob must be usable and its
    // padding representable (the alignment adds 255, so that addition is the overflow point - it is
    // checked before it runs).  The two numbers the refusal text quotes are computed here, on the full
    // set, because the budget scan below stops at the first ranked pair that does not fit: a prefix's
    // minimum is not the set's minimum, and a refusal that called it "the smallest slot" would be false.
    int64_t first_slot = 0, min_slot = -1;
    for (size_t i = 0; i < candidates.size(); ++i) {
        const int64_t l = candidates[i].first;
        if (l < 0 || l >= (int64_t) blob_bytes.size() || blob_bytes[(size_t) l] <= 0) {
            why = "candidate layer " + std::to_string(l) + " has no usable blob size in the pack layout";
            return expert_helper::kErrGeometry;
        }
        int64_t bytes = 0;
        if (!slot_bytes_safe(blob_bytes[(size_t) l], bytes)) {
            why = "candidate layer " + std::to_string(l) + " has a blob of " +
                  std::to_string(blob_bytes[(size_t) l]) + " B whose aligned slot cannot be represented";
            return expert_helper::kErrGeometry;
        }
        if (i == 0) first_slot = bytes;
        if (min_slot < 0 || bytes < min_slot) min_slot = bytes;
    }
    plan.first_slot = first_slot;
    plan.min_slot = min_slot;
    // The prefix the budget buys, in rank order: stop at the first pair whose aligned slot would cross
    // the cap.  No rounding up, no forced slot, no unbounded addition (the sum's own bound is checked).
    int64_t total = 0;
    int64_t slots = 0;
    for (const auto& pr : candidates) {
        int64_t bytes = 0;
        (void) slot_bytes_safe(blob_bytes[(size_t) pr.first], bytes);   // validated in the scan above
        if (slots >= cap) {   // the count bound stopped the scan: the cap, not the bytes
            if (explicit_slots > 0 && cap == explicit_slots) plan.slot_cap_bounded = true;
            if (cap == ceiling) plan.ceiling_bounded = true;
            break;
        }
        if (total > INT64_MAX - bytes) {
            why = "the slot byte total is past what a byte count can represent";
            return expert_helper::kErrGeometry;
        }
        if (!plan.auto_budget && total + bytes > budget_bytes) {
            plan.budget_bounded = true;   // the byte cap stopped the scan
            break;
        }
        total += bytes;
        ++slots;
    }
    plan.slots = slots;
    plan.bytes = total;
    if (slots == 0) {
        // Reachable only with an explicit byte cap below the first ranked slot (AUTO always buys at
        // least the first pair: the cap is >= 1 and the geometry is validated above).
        why = "the byte budget of " + std::to_string(budget_bytes) + " B buys no slot: the first ranked "
              "pair's aligned slot is " + std::to_string(first_slot) + " B and the smallest any candidate "
              "buys is " + std::to_string(min_slot) + " B";
    }
    return expert_helper::kOk;
}

int parse_mib(const std::string& text, int64_t& mib, std::string& why) {
    mib = 0;
    why.clear();
    if (text.empty()) {
        why = "the MiB value is empty";
        return expert_helper::kErrGeometry;
    }
    int64_t v = 0;
    for (const char c : text) {
        if (c < '0' || c > '9') {
            why = "'" + text + "' is not a plain decimal MiB count (digits only, no sign)";
            return expert_helper::kErrGeometry;
        }
        if (v > (INT64_MAX - (int64_t) (c - '0')) / 10) {
            why = "'" + text + "' overflows a byte count";
            return expert_helper::kErrGeometry;
        }
        v = v * 10 + (int64_t) (c - '0');
    }
    if (v > expert_helper::kMaxMiB) {
        why = std::to_string(v) + " MiB does not fit a byte count (" + std::to_string(expert_helper::kMaxMiB) +
              " MiB is the largest usable value)";
        return expert_helper::kErrGeometry;
    }
    mib = v;
    return expert_helper::kOk;
}

bool disjoint_adapt_enabled(const char* env_value, bool helper_registered) {
    if (env_value != nullptr) return std::atoi(env_value) != 0;   // the explicit env wins, as before
    return helper_registered;   // unset: ON exactly when a helper proxy registered
}

}  // namespace expert_helper
}  // namespace core
}  // namespace strata
