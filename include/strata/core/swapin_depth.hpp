// include/strata/core/swapin_depth.hpp - the pack-file stager's read window: STRATA_SWAPIN_DEPTH.
//
// The prompt path stages a chunk's routed experts with a pool of copy threads: each thread claims
// a RUN of consecutive jobs, and the arena serves the run's dropped pairs (the relief's swap-ins)
// as ONE unbuffered read_direct batch - O_DIRECT descriptors, every window of the run queued with
// one io_submit before the first wait.  The relief serializes those file batches on the arena's
// file mutex, so the RUN SIZE IS THE READ QUEUE'S DEPTH: the pp-opt default of 8 (~16 MiB of ~2 MiB
// blobs in flight) held a 142k prefill to a flat ~1.0 GiB/s on an NVMe that sustains 2.5-4+ GiB/s
// once the queue is deep (round 14: read side 377 s of a 313 s wall, 100% I/O-bound).
//
// STRATA_SWAPIN_DEPTH widens that window: the run size AND the ring of staging buffers that keeps
// one run's host buffers distinct, so up to N ~2 MiB blobs are in flight before the batch waits.
// The window is allocated once (the ring's buffers at stager init, the run's arrays per thread) and
// reused.  With the knob absent the numbers are today's bit-for-bit; read advice (FADV_RANDOM), the
// eviction policy and the default scheduling are untouched.
#pragma once

#include <algorithm>
#include <cstdlib>

namespace strata::core {

/// Whether a read_direct batch of `total_bytes` reads through the ONE shared deep window (kept on
/// the source, mutex-guarded for the batch) instead of the calling thread's retained scratch.
/// STRATA_SWAPIN_DEPTH runs land here: the deep staging stays ONE reused buffer whatever the
/// stager's thread count is, while every default-sized batch (the 8-pair run is ~16 MiB) keeps
/// today's exact per-thread path.
inline bool swapin_shared_window(int64_t total_bytes) { return total_bytes > (64ll << 20); }

/// The depth knob's value: absent, empty, unparsable or <= 0 = the default 8 (today's exact
/// scheduling); anything else clamped to [1, 512].  Suggested values 32 / 64 / 128.
inline int swapin_depth(const char* env) {
    if (env == nullptr) return 8;
    const long v = std::atol(env);
    if (v <= 0) return 8;
    return (int) std::min<long>(v, 512);
}

/// The stager window the knobs resolve to - exactly what prefill applies.  `run`: jobs per
/// copy_blobs batch, i.e. the read queue's depth in blobs.  `ring`: staging buffers, so a run
/// never wraps onto its own unfinished buffers (a run is capped by the ring).  `note`: the depth
/// knob changed the default window (prefill says so once, the swap-in line carries the depth).
///
/// Precedence keeps every existing knob surgical: an explicit STRATA_STAGER_BATCH (the A/B arm)
/// still wins for the run size, an explicit STRATA_STAGER_RING still wins for the ring (and so
/// caps the run); the depth knob only fills the defaults it owns.  Without a depth knob the
/// outputs are today's numbers bit-for-bit, for every combination of the other knobs.
struct SwapinWindow { int run; int ring; bool note; };

inline SwapinWindow swapin_window(const char* depth_env, const char* batch_env, const char* ring_env,
                                  bool files, int threads) {
    const bool depth_on = depth_env != nullptr && std::atol(depth_env) > 0;
    const int depth = depth_on ? swapin_depth(depth_env) : 8;
    const int run_cap = depth_on ? 512 : 32;   // the stager's kMaxBatch cap, lifted only by the knob
    const int want = batch_env != nullptr
                         ? (int) std::clamp<long>(std::atol(batch_env), 1, run_cap)
                         : (files ? std::min(depth, run_cap) : 1);
    int ring = files ? 4 * threads : 16;       // today's rule (the stager's own default when not files)
    if (ring_env != nullptr) ring = (int) std::clamp<long>(std::atol(ring_env), 2, 256);
    else if (files && depth_on) ring = std::max(ring, depth);   // the window: room for one whole run
    const bool note = files && depth_on && depth != 8;
    return SwapinWindow{std::min(want, ring), ring, note};
}

}  // namespace strata::core
