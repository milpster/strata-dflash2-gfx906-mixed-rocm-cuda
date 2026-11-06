// include/strata/core/dflash_tap_collect.hpp - compact cross-device transport of the
// DFlash2 target tap planes onto the drafter's device under --layer-split
// (docs/DFLASH2.md capture contract; ownership model: dflash_tap_plan.hpp).
//
// The producers (Verifier windows / Prefill chunks) capture ONLY the tap slices whose
// boundary layer sits in their stage's layer range, rows [0, T) of plane[t][stride][N].
// The drafter runs on one device and must see all n_taps slices in tap order.  FOREIGN
// slices move through pinned host staging in two bounded phases per chunk/window:
//   stage()   - the producer's rows are host-visible complete (the chain syncs the
//               producer stream before the tap callback fires; the decode chain's
//               handoff completes stage k before the final stage ends); a SYNC D2H of
//               the compact rows only (never the padded stride, never history) into a
//               slot, with the slot's completion event drained first.
//   assemble()- from the drafter's context: same-device slices go D2D, staged slots go
//               H2D, all on the drafter stream cs_ (stream order protects the plane
//               from the previous window's fusion reads); the slot event is recorded so
//               the next slot reuse waits for the H2D to finish reading the pinned copy.
// Allocations happen ONCE at enable() (plane rows capped by the prefill chunk and the
// verify window); per-token calls are pure copies.  Pinned staging exists ONLY for
// FOREIGN taps - a drafter-device tap is copied D2D and never touches the host.
//
// Concurrency contract (parent review 2026-10-21, evidence: prefill.cpp:4948-4963 -
// dflash_on_taps fires on the stage's host thread after ITS stream sync, and an intermediate
// stage returns "while later GPUs are still processing the previous chunk"): a producer
// thread staging chunk N+1 overlaps the final stage's thread assembling chunk N, so all
// slot bookkeeping and transfer enqueue runs under mu_.  No lock is ever held across a
// wait that requires another collector call: the producer's backpressure wait is on cv_
// (mutex released, woken by assemble's retire / reset / disable), and every event drained
// under the lock was recorded by an assemble() that already released the lock.
// LIFECYCLE (window-1 GPU failure, L10): a slot is EMPTY, STAGED (unconsumed producer
// data - acquire NEVER evicts it), or RETIRED (assembled; H2D queued+recorded).  Reuse =
// empty, then oldest retired (caller drains the recorded H2D before host rewrite).  When
// both slots are STAGED the producer WAITS: the bounded prefill chain (N+2 staged while N
// is unconsumed) is backpressure, never data loss.  assemble() waits ONLY for its own
// key's producers (bounded, see CONSUMER ORDERING).
// CANCELLATION (L10 review): the wait is epoch-guarded.  acquire_slot captures gen_ under
// mu_ before cv_.wait; reset_staging()/disable() increment gen_ and notify - a producer
// woken after a reset is REJECTED even though the collector stays enabled, so an old
// request's producer never stages into a new request.  Every metadata mutation (slots,
// keys, gen_) runs under mu_, disable() included.
// CONSUMER ORDERING (L14: the dflash_w3 model window's "tap 0 was never staged"): the
// chain MUST stage a chunk's foreign slices before the final stage assembles the same
// key - prefill fires dflash_on_taps at EVERY stage's chunk end (prefill.cpp: the hand-off
// path), not only at the chain's last one.  The collector ENFORCES the contract:
// assemble() for a key with NO staged foreign flag refuses FAST (no producer ran -
// the original loud failure; the fail-closed refusals stay immediate); with PARTIALLY
// staged flags it BINDS its slot (marked `awaiting`, so no producer binds a new key
// over it) and WAITS on cv_ - bounded by kAssembleWaitMs, epoch-guarded exactly like
// the producer's backpressure (reset_staging/disable reject it), woken by stage()'s
// flag set.  An expired wait keeps the original per-tap refusal: a loud failure,
// never a stale-plane read, never an unbounded hang.
// No deadlock: the consumer waits for a PRODUCER to stage (an independent collector
// call, the mutex released); only assemble retires, and a producer parked in
// backpressure holds keys the consumer is NOT waiting for (a parked producer's own key
// is un-staged, so that key's producer already staged), and the consumer's wait is
// bounded regardless.
// LIFETIME (explicit ownership, not magic): the collector object must outlive every thread
// that may call stage()/assemble().  The engine pipeline guarantees this by JOINS - the
// prefill stage threads are joined before the session object that owns the collector is
// torn down (prefill.cpp pipeline ownership); the destructor calls disable() and is only
// reached after those joins.  Do not destroy the collector while callbacks can fire.
// Serial-only by design: --pipeline-windows overlap with dflash + split is refused.
#pragma once

#include <strata/core/dflash_tap_plan.hpp>

#include <cuda_runtime.h>

#include <cstdint>
#include <condition_variable>
#include <mutex>
#include <string>

namespace strata::core {

/// Bounded collector: assembled planes on the drafter device + pinned staging slots.
class DFlashTapCollector {
  public:
    /// One producer's plane for one chunk/window: its stage, its (device) plane, its
    /// row stride per tap, and its valid row count.  Rows beyond `rows` are stale.
    struct Slice {
        int stage = 0;
        const void* plane = nullptr;
        int64_t stride_rows = 0;
        int64_t rows = 0;
    };

    static constexpr int kSlots = 2;   ///< prefill chains pipeline two chunks

    /// The consumer's (assemble's) bounded wait for its own key's producers, in
    /// milliseconds.  Generous against a healthy chain (a producer stages at its chunk
    /// end, microseconds to a chunk's compute before the consumer needs it): expiry
    /// means a broken producer chain, and the refusal is the loud, original failure.
    static constexpr int kAssembleWaitMs = 10000;

    /// One staging slot of one kind (public + testable: the CPU slot lifecycle in
    /// dflash_tap_plan_test runs the real acquire() with ev == nullptr, no CUDA call).
    struct Slot {
        int64_t key = -1;                              ///< chunk pos0 bound to the slot
        int64_t rows = -1;                             ///< rows staged under that key (geometry guard)
        bool awaiting = false;                         ///< assemble() bound this slot and waits for its staged flags
        cudaEvent_t ev = nullptr;                      ///< records the slot's last H2D
        uint8_t* pin[kDFlashMaxTaps] = {};             ///< per-tap staging rows (FOREIGN taps only)
        bool staged[kDFlashMaxTaps] = {};              ///< tap staged into this slot
        /// Lifecycle (L10): key<0 = EMPTY; key>=0 with a staged flag = STAGED (unconsumed
        /// producer data - NEVER evicted); key>=0 with no staged flags = RETIRED (assembled:
        /// the H2D is queued on cs_ and `ev` records it; reuse must drain `ev` first).
        /// L14: `awaiting` marks the consumer's binding (no flags yet, the chunk not
        /// staged) - acquire() never re-binds it, so the chunk's producers land on it.
    };

    /// Pick (and (re)bind) the slot for `key`: an existing binding wins; otherwise an EMPTY
    /// slot, then the oldest RETIRED slot.  A slot holding UNCONSUMED staging is never a
    /// victim (window-1 GPU failure: staging chunk N+2 destroyed the unconsumed N).  When
    /// both slots are STAGED, returns -1 with a backpressure message - the member
    /// acquire_slot() waits for the consumer's retire.  Pure bookkeeping: no CUDA calls.
    static int acquire(Slot (&slots)[kSlots], int64_t key, std::string& err);

    /// Allocate the planes (on drafter_dev, which must be current) and the pinned
    /// slots for the FOREIGN taps.  `f32_rows` caps the verify-window rows,
    /// `bf16_rows` the prompt-chunk rows; zero disables that kind.  Re-enable frees
    /// the previous allocation.
    bool enable(const DFlashTapPlan& plan, int drafter_dev, int64_t f32_rows, int64_t bf16_rows,
                std::string& err);
    /// Frees planes/staging/events, DRAINING every recorded H2D first (the collector
    /// owns its outstanding transfers, standalone use included).  Called from the
    /// drafter's release and from the destructor.
    void disable();
    ~DFlashTapCollector() { disable(); }

    /// Phase 1, producer context: compact-copy the slice's owned tap rows into the
    /// chunk slot `key` (prefill chains key by chunk pos0; decode keys one window).
    /// type_bytes is 4 (FP32 verify) or 2 (BF16 prompt).  Slices produced on the
    /// drafter's own device skip staging; assemble() copies them D2D from the plane.
    /// L15: a NULL plane refuses ONLY when the slice owns a cross-device tap (a real
    /// producer with no plane - loud refusal).  A slice owning no foreign taps
    /// contributes nothing to staging (its taps are D2D at assemble), so a null plane
    /// must not refuse it - decode verifiers without a carved tap plane are legal
    /// non-producers.
    bool stage(int64_t key, const Slice& slice, int type_bytes, std::string& err);

    /// Phase 2, drafter context: assemble every plan tap for rows [0, rows) into the
    /// plane: staged slots H2D, drafter-device slices D2D from `slices` (all producers
    /// pass their slice here), all on cs_.  Every plan tap must have exactly one
    /// producer slice whose advertised rows cover `rows` - a missing producer is a
    /// hard refusal, never a stale-plane read.
    bool assemble(int64_t key, const Slice* slices, int n_slices, int64_t rows,
                  int type_bytes, std::string& err);

    /// Abandoned-request hygiene: drop every slot binding (draining in-flight H2Ds
    /// first) so a NEW conversation reusing the same pos0 keys never inherits a stale
    /// rows guard or staged flags from an aborted request.  Call at request start.
    void reset_staging();

    bool enabled() const { return plan_.n_taps > 0; }
    const DFlashTapPlan& plan() const { return plan_; }
    int drafter_device() const { return dev_; }
    void set_stream(cudaStream_t cs) { cs_ = cs; }

    /// The assembled planes' layout: tap t's rows at plane + t * stride_rows * n_embd.
    const float* plane_f32() const { return plane_f32_; }
    const uint16_t* plane_bf16() const { return plane_bf16_; }
    int64_t stride_rows_f32() const { return f32_rows_; }
    int64_t stride_rows_bf16() const { return bf16_rows_; }

    /// Reported bytes: device planes + pinned staging (diagnostics / memory accounting).
    int64_t device_bytes() const { return plane_bytes_; }
    int64_t pinned_bytes() const { return pin_bytes_; }

    /// The slots for one kind; `rows_cap` bounds one tap's staged rows.  Public for the
    /// CPU bookkeeping tests; production reaches slots only through kind()/stage/assemble.
    struct Kind { Slot slots[kSlots]; int64_t rows_cap = 0; int type_bytes = 0; };
    Kind& kind_for_test(int type_bytes);
    /// CPU-testable producer path: the exact acquire_slot body (no CUDA call inside it);
    /// the GPU cancellation-waiter fixture exercises the same path for real.
    /// The production producer path is stage(); the consumer path is assemble().
    bool acquire_producer(int type_bytes, int64_t key, std::string& err);
    /// Current cancellation epoch (mu_-guarded); reset_staging()/disable() increment it.
    uint64_t staging_epoch();

  private:
    Kind& kind(int type_bytes);

    /// Producer entry: static acquire() with the metadata mutex HELD by the caller; when
    /// both slots hold unconsumed staging, the producer WAITS on cv_ (the mutex is
    /// released - never a lock across progress that needs another collector call) and is
    /// woken by assemble()'s retire, by reset_staging(), and by disable().  A woken
    /// producer is REJECTED when the collector was disabled/reset since the wait started:
    /// the epoch token (captured under mu_ at wait entry) must be unchanged, so a pending
    /// producer of an OLD request never stages into a NEW request - even while the
    /// collector stays enabled (reset_staging keeps enabled true on purpose).
    int acquire_slot(Kind& k, int64_t key, std::string& err, std::unique_lock<std::mutex>& lock);

    DFlashTapPlan plan_;
    int dev_ = -1;
    int64_t f32_rows_ = 0, bf16_rows_ = 0;
    float* plane_f32_ = nullptr;
    uint16_t* plane_bf16_ = nullptr;
    Kind f32_{}, bf16_{};
    cudaStream_t cs_ = nullptr;   ///< the drafter's stream (planes are written on it)
    int64_t plane_bytes_ = 0, pin_bytes_ = 0;
    std::mutex mu_;   ///< serializes slot bookkeeping + transfer enqueue across stage/assemble
    std::condition_variable cv_;   ///< backpressure wake: retire / reset / disable
    uint64_t gen_ = 0;             ///< cancellation epoch, read/written only under mu_
};

}  // namespace strata::core
