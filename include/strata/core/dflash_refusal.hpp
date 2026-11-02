// include/strata/core/dflash_refusal.hpp - how a DFlash draft proposal's failure is classified.
//
// The drafter's last step reads its block's candidate token ids out of mapped host memory and
// checks them against the target's vocabulary (the ids the selector produced are host ints; no
// device access is needed here).  A candidate id outside [0, vocab) is the ONE draft failure that
// keeps the target-verified state intact: nothing was committed, the window's rows are already
// emitted, and the request can simply continue target-only.  Every other draft failure (device,
// stream, graph, geometry, capacity, ring, staging, append) means the drafter's state is not
// trustworthy and stays fatal at the call site.
//
// This header is pure C++ (no CUDA, no dependency): the emitter in src/core/dflash_runtime.cpp,
// both decode call sites in src/program/generate.cpp and tests/core/dflash_refusal_test.cpp all
// call THESE functions - the predicate and the classification live here once, not per call site.
// The classification is TYPED: a call site reads the drafter's DflashRefusal status, never the
// error text, so a message's wording can never flip a fatal into a recovery.
#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

namespace strata::core {

/// What the LAST DFlashDrafter::propose() call refused with.  Cleared at propose()'s entry, set
/// ONLY by the vocabulary check on the host candidates; every other failure leaves kNone, which
/// the call sites treat as fatal (docs/DFLASH.md semantics; r4-dflash-design.md).
enum class DflashRefusal : unsigned char {
    kNone = 0,                 ///< no numeric refusal: whatever failed, the failure is fatal
    kNonFiniteCandidates = 1,  ///< a candidate id left [0, vocab): disable drafting for this request
};

/// Does this block's host candidate ids refuse?  EXACTLY the guard the emitter has always run on
/// the mapped host picks (`p < 0 || p >= vocab`), moved here so the emitter, the tests and any
/// future call site share one predicate.  Malformed arguments (null buffer, empty block, empty
/// vocabulary) are NOT a numeric refusal - the caller keeps its fatal classification, and
/// propose() validates all three before reaching this check.
inline bool dflash_candidates_refused(const int32_t* host_picks, int block, int64_t vocab) {
    if (!host_picks || block < 1 || vocab < 1) return false;
    for (int i = 0; i < block; ++i) {
        const int32_t p = host_picks[i];
        if (p < 0 || p >= vocab) return true;
    }
    return false;
}

/// Is a refusal of this class fatal?  Only the numeric class is recoverable (request-local draft
/// disable); kNone - and any value that is not the numeric class - stays fatal at the call site.
inline bool dflash_refusal_is_fatal(DflashRefusal r) { return r != DflashRefusal::kNonFiniteCandidates; }

/// The emitter's host handoff, validate-then-copy: check the mapped host candidate ids FIRST, and
/// write NOTHING when they refuse (the caller's `out` and `probabilities` keep their previous
/// bytes - the caller must not read a refused proposal's output).  When they pass, copy the block's
/// ids into `out` and, when the caller asked for probabilities, copy the block's probabilities from
/// `host_probs` (the mapped row area behind the ids).  Identical bytes to the previous
/// copy-then-check order, because the check ran on the same values.
/// Returns false on a refusal, and also on a malformed handoff (null ids/out, empty block, empty
/// vocabulary, probabilities asked for without a source); the caller classifies a false here as
/// kNone = fatal, never as the numeric class.
inline bool dflash_emit_candidates(const int32_t* host_picks, int block, int64_t vocab,
                                  int32_t* out, const int32_t* host_probs, float* probabilities) {
    if (!host_picks || !out || block < 1 || vocab < 1) return false;
    if (probabilities && !host_probs) return false;
    if (dflash_candidates_refused(host_picks, block, vocab)) return false;
    std::memcpy(out, host_picks, (size_t) block * sizeof(int32_t));
    if (probabilities) std::memcpy(probabilities, host_probs, (size_t) block * sizeof(float));
    return true;
}

}  // namespace strata::core
