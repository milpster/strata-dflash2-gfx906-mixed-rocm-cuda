// include/strata/core/expert_helper_candidates.hpp - what the out-of-process expert helper MAY hold,
// and how many slots a byte budget buys (M1, capacity-first-design.md §2).
//
// PURE CPU by construction, like the rest of the helper's client side: every function here takes
// MASKS and PROFILE DATA and returns data.  No device call, no cache object, no socket, no layout
// singleton - `src/program/generate.cpp` reads the real caches and the pack layout, this file decides
// nothing it cannot be told.  That is what makes the rules testable without a GPU: the candidate set,
// the byte arithmetic and the promotion gate below are the exact code the engine runs, pinned by
// tests/core/expert_helper_candidates_test.cpp.
//
// WHY THESE THREE FUNCTIONS EXIST (the M1 gaps they close, all measured in code, not assumed):
//   * the helper's candidates came from ONE stage's profile (`stages.back()->profile`), so one third of
//     the model's layers could ever use its bytes;
//   * the engine asked for a MAGIC 256 slots when no count was given, and the slot count - not the
//     card's budget - bound the fill;
//   * the rule that keeps the adaptive tiers from promoting an expert the helper holds was gated by
//     `STRATA_DISJOINT_ADAPT`, OFF by default, so a pair could become resident in two tiers at once.
// The contract after M1: a pair has exactly ONE owner (a stage cache, the primary cache, the helper,
// or the host files/arena); the helper's set is chosen from the whole ranking in ITS rank order; the
// engine asks for as many slots as the byte budget buys, and the helper's own free-VRAM budget stays
// authoritative (it fills what fits and reports what it filled - the engine mirrors the report).
#pragma once

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

#include "strata/core/expert_helper_protocol.hpp"   // the transport's named statuses (kOk, kErrGeometry)

namespace strata {
namespace core {
namespace expert_helper {

/// An aligned cache slot's bytes for a blob of `blob` bytes: EXACTLY
/// `ExpertCache::slot_stride(blob, 0)` - the same expression the CUDA helper's fill
/// (`tools/mixed_gpu/sidecar/helper_main.cpp`, `serve_fill`) spends against the card's budget.
/// One pair, one slot, so "the helper's resident bytes" is the SUM of this over its pairs: NOT a
/// `bytes / max_blob` estimate, and NOT the helper process's total memory (scratch and window
/// buffers are the helper's own accounting, bounded separately by `--reserve-mib`).
/// PRECONDITION `0 <= blob <= INT64_MAX - 255` (the expression adds 255 before it divides): use
/// `slot_bytes_safe` when the blob came from outside - an unrepresentable blob is refused by name,
/// never wrapped into a small slot.
int64_t slot_bytes(int64_t blob);

/// The checked form of the same rule: false when `blob` cannot be padded without passing int64
/// (negative, or `blob + 255` past `INT64_MAX`).  `out` is untouched on failure.
bool slot_bytes_safe(int64_t blob, int64_t& out);

/// The pair-eligibility counts, reported (never silently dropped): `generate.cpp` prints what the
/// helper skipped and why, so a narrowed candidate set is visible in the log, not inferred.
struct CandidateStats {
    int64_t considered = 0;      // pairs the ranking named
    int64_t kept = 0;            // pairs the helper may hold, in rank order
    int64_t invalid = 0;         // layer/expert outside the model's geometry
    int64_t outside_window = 0;  // layer outside [layer_lo, layer_hi]
    int64_t owned = 0;           // a cache already owns it (the primary's or a stage's)
    int64_t hadamard = 0;        // a Hadamard-folded layer: the in-process tier's rotation path
    int64_t duplicates = 0;      // a pair the ranking named more than once (the first rank wins)
};

/// THE helper's candidate set, in RANK ORDER: `ranked` is the engine's canonical global ranking -
/// the whole profile, hottest first, BEFORE the layer split filtered each stage's share out of it -
/// and the result keeps that order exactly (no stage0-then-stage1 concatenation, no invented score).
/// Removed, in this order: geometry-invalid pairs, pairs outside the window (INCLUSIVE ends), pairs
/// an owner mask marks (the primary cache AND every stage cache: `owned` is byte-per-pair, flat
/// `layer * n_expert + expert`, the same shape the wire's claimed bitmap is built from),
/// Hadamard-folded layers, and repeated pairs.  An empty result is a success with kept = 0: the
/// caller prints the reason and keeps the CPU path (the helper has always been optional).
/// Geometry the function cannot honor is a named `kErrGeometry` (`why` says which), never a partial
/// answer: `layer_hi < layer_lo`, a missing mask, `n_layers <= 0`, `n_expert <= 0`, or a
/// `n_layers * n_expert` pair count past what an int64 index can represent (the mask is indexed by
/// that product, so the product is checked BEFORE it is computed).
int build_candidates(const std::vector<std::pair<int32_t, int32_t>>& ranked,
                     const std::vector<uint8_t>& owned, const std::vector<uint8_t>& had_folded,
                     int64_t n_layers, int64_t n_expert, int64_t layer_lo, int64_t layer_hi,
                     std::vector<std::pair<int32_t, int32_t>>& out, CandidateStats& stats,
                     std::string& why);

/// What a byte budget buys, in the helper's fill order.  `budget_bytes <= 0` means AUTO: no byte cap,
/// every candidate is asked for and the HELPER's own free-VRAM budget (its `cudaMemGetInfo` less the
/// reserve, the scratch, the window buffers and the context's headroom) decides - that is the
/// "as much as the card allows" M1 default, with NO magic slot count anywhere in the engine.
/// `explicit_slots > 0` is an upper bound on the COUNT (`--expert-helper-slots` keeps its meaning).
/// `max_slots` is the transport's ceiling (`kMaxPairs`: the fill frame is int32-counted).
///
/// The scan walks the candidates in rank order and stops at the FIRST pair whose aligned slot would
/// cross the budget: `slots` is then the count of the prefix that fits, and `bytes` the exact aligned
/// bytes of that prefix, so the engine's request never asks for more bytes than the budget allows.
/// No minimum slot is ever forced, and no budget is rounded up: an explicit budget smaller than the
/// smallest aligned slot buys ZERO slots (the caller refuses the helper and keeps the CPU path).
/// `slots` is always finite and non-negative, `bytes` never overflows, and a candidate whose layer has
/// no usable blob size is a named `kErrGeometry`.
struct FillPlan {
    int64_t candidates = 0;        // what was eligible
    int64_t slots = 0;             // what to ask for (0: nothing fits - the caller refuses)
    int64_t bytes = 0;             // the exact aligned bytes of those slots
    int64_t first_slot = 0;        // the FIRST RANKED candidate's aligned bytes (refusal text)
    int64_t min_slot = 0;          // the smallest aligned slot ANY candidate buys (full scan)
    bool auto_budget = false;      // no byte cap: the helper's card budget decides
    bool budget_bounded = false;   // the byte cap stopped the scan
    bool slot_cap_bounded = false; // the explicit slot cap stopped the scan
    bool ceiling_bounded = false;  // the transport's ceiling stopped the scan
};

int plan_fill(const std::vector<std::pair<int32_t, int32_t>>& candidates,
              const std::vector<int64_t>& blob_bytes, int64_t budget_bytes, int64_t explicit_slots,
              int64_t max_slots, FillPlan& plan, std::string& why);

/// STRICT CLI parse of a MiB count: ASCII digits only, no sign, no spaces, no trailing junk, no
/// value whose byte count cannot be represented in int64 (`mib * 1048576` must fit).  A bare
/// `std::atoll` accepts "12x", " 7", "-3" and wraps in silence; an unrepresentable budget is not a
/// budget, so it is refused by name.  `kOk` sets `mib`; anything else leaves `why` as the reason.
int parse_mib(const std::string& text, int64_t& mib, std::string& why);

/// The adaptive-promotion gate (the rule that keeps an expert the helper holds out of the primary's
/// promotion candidates).  Precedence, exactly as documented in docs/DISJOINT_EXPERT_CACHE.md:
/// an explicit `STRATA_DISJOINT_ADAPT` wins (=0 opts out, =1 opts in, and the value is read with the
/// same `atoi` the engine has always used, so "0" and junk both mean off); with the environment
/// UNSET the gate is ON if and only if a helper proxy tier registered successfully.  No helper = the
/// old behaviour, byte for byte: the gate OFF costs nothing to consult and admits no pair the helper
/// owns, and it never blocks a local promotion after the helper's loss (the gate is asked LIVE, from
/// `RemoteExperts::holds`, on every call).  The gate's PER-TIER question is RESIDENCY
/// (`RemoteExperts::proxy_residency_live`), not dispatch (`helper_usable`): a proxy disabled for the
/// rest of a request keeps its helper's VRAM and stays consulted; only a closed channel releases it.
/// The transitions are pinned by tests/core/expert_helper_ownership_test.cpp.
bool disjoint_adapt_enabled(const char* env_value, bool helper_registered);

}  // namespace expert_helper
}  // namespace core
}  // namespace strata
