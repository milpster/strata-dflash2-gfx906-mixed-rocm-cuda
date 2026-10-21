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
// on_taps fires on the stage's host thread after ITS stream sync, and an intermediate
// stage returns "while later GPUs are still processing the previous chunk"): a producer
// thread staging chunk N+1 overlaps the final stage's thread assembling chunk N, so all
// slot bookkeeping and transfer enqueue runs under mu_.  No lock is ever held across a
// wait that requires another collector call: the event drained by acquire() was always
// recorded by an assemble() that already released the lock.
// Serial-only by design: --pipeline-windows overlap with dflash + split is refused.
#pragma once

#include <strata/core/dflash_tap_plan.hpp>

#include <cuda_runtime.h>

#include <cstdint>
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

    /// One staging slot of one kind (public + testable: the CPU slot lifecycle in
    /// dflash_tap_plan_test runs the real acquire() with ev == nullptr, no CUDA call).
    struct Slot {
        int64_t key = -1;                              ///< chunk pos0 bound to the slot
        int64_t rows = -1;                             ///< rows staged under that key (geometry guard)
        cudaEvent_t ev = nullptr;                      ///< records the slot's last H2D
        uint8_t* pin[kDFlashMaxTaps] = {};             ///< per-tap staging rows (FOREIGN taps only)
        bool staged[kDFlashMaxTaps] = {};              ///< tap staged into this slot
    };

    /// Pick (and (re)bind) the slot for `key`: an existing binding wins; otherwise the
    /// oldest binding is evicted after draining its H2D event.  Returns the slot index,
    /// -1 when the evicted slot's event drain failed (err set).
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

  private:
    /// The slots for one kind; `rows_cap` bounds one tap's staged rows.
    struct Kind { Slot slots[kSlots]; int64_t rows_cap = 0; int type_bytes = 0; };

    Kind& kind(int type_bytes);

    DFlashTapPlan plan_;
    int dev_ = -1;
    int64_t f32_rows_ = 0, bf16_rows_ = 0;
    float* plane_f32_ = nullptr;
    uint16_t* plane_bf16_ = nullptr;
    Kind f32_{}, bf16_{};
    cudaStream_t cs_ = nullptr;   ///< the drafter's stream (planes are written on it)
    int64_t plane_bytes_ = 0, pin_bytes_ = 0;
    std::mutex mu_;   ///< serializes slot bookkeeping + transfer enqueue across stage/assemble
};

}  // namespace strata::core
