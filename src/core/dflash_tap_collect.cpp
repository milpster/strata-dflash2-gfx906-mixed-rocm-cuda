// src/core/dflash_tap_collect.cpp - implementation of the bounded cross-device tap
// collector (include/strata/core/dflash_tap_collect.hpp).  Serial-only transport:
// D2H runs host-synchronously inside the producer's completion context; H2D/D2D run
// on the drafter stream, stream-ordered against the drafter's fusion reads.
// All slot bookkeeping and transfer enqueue is serialized by mu_ (the prefill chain's
// producer stages chunk N+1 on its host thread while the final stage assembles chunk
// N on its own - prefill.cpp:4948-4963 is the concurrent-call evidence).
#include <strata/core/dflash_tap_collect.hpp>
#include <strata/core/on_device.hpp>

#include <chrono>
#include <cstdio>

namespace strata::core {

namespace {
constexpr bool valid_type_bytes(int type_bytes) { return type_bytes == 2 || type_bytes == 4; }
}  // namespace

DFlashTapCollector::Kind& DFlashTapCollector::kind(int type_bytes) { return type_bytes == 4 ? f32_ : bf16_; }

int DFlashTapCollector::acquire(Slot (&slots)[kSlots], int64_t key, std::string& err) {
    int first_empty = -1, oldest_retired = -1;
    for (int i = 0; i < kSlots; ++i) {
        if (slots[i].key == key) return i;   // the existing binding wins: unique per key
        if (slots[i].key < 0) {
            if (first_empty < 0) first_empty = i;
            continue;
        }
        if (slots[i].awaiting) continue;   // L14: the consumer's binding - its chunk is
                                           // being assembled, no producer re-binds it
        bool staged_live = false;
        for (int t = 0; t < kDFlashMaxTaps; ++t) staged_live |= slots[i].staged[t];
        if (!staged_live && (oldest_retired < 0 || slots[i].key < slots[oldest_retired].key)) oldest_retired = i;
    }
    // L10 lifecycle (window-1 GPU failure: "tap 0 was never staged"): acquire() used to
    // evict the oldest key whenever both slots were bound, destroying UNCONSUMED producer
    // staging - the producer stages chunk N+2 while the consumer has only assembled N.
    // Reuse order is now: EMPTY slot, then the oldest RETIRED slot (assembled, flags
    // cleared, H2D queued+recorded - the caller drains `ev` before any host rewrite).
    // Both slots STAGED = backpressure: unconsumed staging is NEVER evicted.
    const int victim = first_empty >= 0 ? first_empty : oldest_retired;
    if (victim < 0) {
        err = "dflash tap collect: backpressure - both slots hold unconsumed staging";
        return -1;
    }
    Slot& s = slots[victim];
    s.key = key; s.rows = -1;
    for (int t = 0; t < kDFlashMaxTaps; ++t) s.staged[t] = false;
    return victim;
}

int DFlashTapCollector::acquire_slot(Kind& k, int64_t key, std::string& err, std::unique_lock<std::mutex>& lock) {
    for (;;) {
        int idx = acquire(k.slots, key, err);
        if (idx >= 0) return idx;
        // Bounded producer wait with the metadata mutex RELEASED.  Every wake source is
        // external to this call: assemble()'s retire, reset_staging(), disable().
        err.clear();
        const uint64_t epoch = gen_;   // captured under mu_ at wait entry
        cv_.wait(lock);
        // Cancellation (L10 review): a reset/disable during the wait MUST reject the
        // producer even when the collector stays enabled - an old request's producer
        // never stages into a new request.  Deterministic refusal, no hang.
        if (!enabled() || gen_ != epoch) {
            err = "dflash tap collect: staging cancelled (request reset/disabled mid-wait)";
            return -1;
        }
    }
}

bool DFlashTapCollector::acquire_producer(int type_bytes, int64_t key, std::string& err) {
    std::unique_lock<std::mutex> lock(mu_);
    Kind& k = kind(type_bytes);
    return acquire_slot(k, key, err, lock) >= 0;
}

uint64_t DFlashTapCollector::staging_epoch() {
    std::lock_guard<std::mutex> lock(mu_);
    return gen_;
}

DFlashTapCollector::Kind& DFlashTapCollector::kind_for_test(int type_bytes) { return kind(type_bytes); }

bool DFlashTapCollector::enable(const DFlashTapPlan& plan, int drafter_dev, int64_t f32_rows,
                                int64_t bf16_rows, std::string& err) {
    disable();
    if (plan.n_taps <= 0) { err = "dflash tap collect: plan not built"; return false; }
    if (drafter_dev < 0) { err = "dflash tap collect: no drafter device"; return false; }
    if (f32_rows <= 0 && bf16_rows <= 0) { err = "dflash tap collect: both row caps are zero"; return false; }
    plan_ = plan; dev_ = drafter_dev;
    const OnDevice on_dev(dev_);   // the planes land on the drafter's card, standalone included
    const int64_t N = plan_.n_embd;
    const auto alloc_kind = [&](Kind& k, int64_t rows_cap, int type_bytes) -> bool {
        k.rows_cap = rows_cap; k.type_bytes = type_bytes;
        for (int i = 0; i < kSlots; ++i) {
            Slot& s = k.slots[i];
            s.key = -1; s.rows = -1; s.awaiting = false;
            for (int t = 0; t < plan_.n_taps; ++t) {
                s.staged[t] = false;
                if (plan_.tap_device(t) == dev_) continue;   // local taps go D2D: no host staging
                const size_t bytes = (size_t) rows_cap * N * type_bytes;
                if (cudaHostAlloc((void**) &s.pin[t], bytes, cudaHostAllocMapped | cudaHostAllocPortable) !=
                    cudaSuccess) {
                    err = "dflash tap collect: pinned staging allocation failed";
                    return false;
                }
                pin_bytes_ += (int64_t) bytes;
            }
            if (cudaEventCreate(&s.ev) != cudaSuccess) {
                err = "dflash tap collect: staging event creation failed";
                return false;
            }
        }
        return true;
    };
    if (f32_rows > 0) {
        f32_rows_ = f32_rows;
        if (cudaMalloc((void**) &plane_f32_, plan_.plane_bytes(f32_rows, 4)) != cudaSuccess) {
            err = "dflash tap collect: FP32 plane allocation failed"; disable(); return false;
        }
        plane_bytes_ += plan_.plane_bytes(f32_rows, 4);
        if (!alloc_kind(f32_, f32_rows, 4)) { disable(); return false; }
    }
    if (bf16_rows > 0) {
        bf16_rows_ = bf16_rows;
        if (cudaMalloc((void**) &plane_bf16_, plan_.plane_bytes(bf16_rows, 2)) != cudaSuccess) {
            err = "dflash tap collect: BF16 plane allocation failed"; disable(); return false;
        }
        plane_bytes_ += plan_.plane_bytes(bf16_rows, 2);
        if (!alloc_kind(bf16_, bf16_rows, 2)) { disable(); return false; }
    }
    return true;
}

void DFlashTapCollector::disable() {
    std::lock_guard<std::mutex> lock(mu_);   // every metadata mutation is mu_-guarded (L10 review)
    gen_ += 1;                               // pending producers reject deterministically
    const OnDevice on_dev(dev_);   // events and device planes live on dev_; drain needs its context
    const auto free_kind = [&](Kind& k) {
        for (int i = 0; i < kSlots; ++i) {
            Slot& s = k.slots[i];
            if (s.ev != nullptr) (void) cudaEventSynchronize(s.ev);   // the collector owns its outstanding H2Ds
            for (int t = 0; t < kDFlashMaxTaps; ++t) {
                if (s.pin[t] != nullptr) cudaFreeHost(s.pin[t]);
                s.pin[t] = nullptr; s.staged[t] = false;
            }
            if (s.ev != nullptr) cudaEventDestroy(s.ev);
            s.ev = nullptr; s.key = -1; s.rows = -1; s.awaiting = false;
        }
        k.rows_cap = 0;
    };
    if (plane_f32_ != nullptr) cudaFree(plane_f32_);
    if (plane_bf16_ != nullptr) cudaFree(plane_bf16_);
    plane_f32_ = nullptr; plane_bf16_ = nullptr;
    free_kind(f32_); free_kind(bf16_);
    plan_ = DFlashTapPlan{}; dev_ = -1; f32_rows_ = 0; bf16_rows_ = 0;
    plane_bytes_ = 0; pin_bytes_ = 0; cs_ = nullptr;
    cv_.notify_all();   // the collector is gone: pending producer waiters reject, no hang
}

void DFlashTapCollector::reset_staging() {
    std::lock_guard<std::mutex> lock(mu_);
    const OnDevice on_dev(dev_ >= 0 ? dev_ : -1);
    const auto reset_kind = [&](Kind& k) {
        for (int i = 0; i < kSlots; ++i) {
            Slot& s = k.slots[i];
            if (s.ev != nullptr) (void) cudaEventSynchronize(s.ev);   // drop nothing mid-H2D
            s.key = -1; s.rows = -1; s.awaiting = false;
            for (int t = 0; t < kDFlashMaxTaps; ++t) s.staged[t] = false;
        }
    };
    reset_kind(f32_); reset_kind(bf16_);
    gen_ += 1;      // new epoch: pending producers of the ABORTED request reject on wake
    cv_.notify_all();
}

bool DFlashTapCollector::stage(int64_t key, const Slice& slice, int type_bytes, std::string& err) {
    if (!valid_type_bytes(type_bytes)) { err = "dflash tap collect: type_bytes must be 2 (BF16) or 4 (FP32)"; return false; }
    std::unique_lock<std::mutex> lock(mu_);
    if (!enabled()) { err = "dflash tap collect: not enabled"; return false; }
    Kind& k = kind(type_bytes);
    if (k.rows_cap <= 0) { err = "dflash tap collect: that plane kind is not enabled"; return false; }
    // L15: a NULL plane is only refused when this slice actually owns a FOREIGN tap
    // (checked inside the ownership loop below).  A slice that owns no foreign taps
    // contributes nothing to staging - its taps are D2D at assemble - so a verifier
    // with no carved tap plane must NOT refuse the whole window here.
    if (slice.stage < 0 || slice.stage >= plan_.n_stages) { err = "dflash tap collect: producer stage out of range"; return false; }
    if (slice.rows <= 0 || slice.rows > k.rows_cap || slice.rows > slice.stride_rows) {
        err = "dflash tap collect: producer rows exceed the plane capacity/stride";
        return false;
    }
    const int64_t N = plan_.n_embd;
    int slot = -1;
    for (int t = 0; t < plan_.n_taps; ++t) {
        if (plan_.owner[t] != slice.stage) continue;
        if (plan_.tap_device(t) == dev_) continue;   // drafter-device taps: assemble copies them D2D
        if (slice.plane == nullptr) { err = "dflash tap collect: producer plane is null"; return false; }   // L15: a real foreign producer with no plane - loud refusal
        if (slot < 0) {
            slot = acquire_slot(k, key, err, lock);   // backpressure: waits for the consumer's retire
            if (slot < 0) return false;
            Slot& s = k.slots[slot];
            if (s.rows >= 0 && s.rows != slice.rows) { err = "dflash tap collect: chunk slot rows disagree"; return false; }
            s.rows = slice.rows;
            if (s.ev != nullptr && cudaEventSynchronize(s.ev) != cudaSuccess) {
                err = "dflash tap collect: draining the slot's last H2D failed";   // pinned rows are read by the H2D
                return false;
            }
        }
        Slot& s = k.slots[slot];
        if (s.staged[t]) { err = "dflash tap collect: tap " + std::to_string(t) + " staged twice for this chunk"; return false; }
        const size_t bytes = (size_t) slice.rows * N * type_bytes;
        const OnDevice on_producer(plan_.dev[slice.stage]);
        if (cudaMemcpy(s.pin[t],
                       (const uint8_t*) slice.plane + (size_t) t * slice.stride_rows * N * type_bytes,
                       bytes, cudaMemcpyDeviceToHost) != cudaSuccess) {
            err = "dflash tap collect: D2H of the producer slice failed";
            return false;
        }
        s.staged[t] = true;
    }
    cv_.notify_all();   // L14: this key now has live staging - wake a consumer in its
                       // bounded assemble() wait (no copy is enqueued yet: the wake
                       // only re-checks the flags, under mu_, before any transfer)
    return true;
}

bool DFlashTapCollector::assemble(int64_t key, const Slice* slices, int n_slices, int64_t rows,
                                  int type_bytes, std::string& err) {
    if (!valid_type_bytes(type_bytes)) { err = "dflash tap collect: type_bytes must be 2 (BF16) or 4 (FP32)"; return false; }
    std::unique_lock<std::mutex> lock(mu_);
    if (!enabled()) { err = "dflash tap collect: not enabled"; return false; }
    Kind& k = kind(type_bytes);
    if (k.rows_cap <= 0) { err = "dflash tap collect: that plane kind is not enabled"; return false; }
    if (rows <= 0 || rows > k.rows_cap) { err = "dflash tap collect: assemble rows exceed the plane capacity"; return false; }
    int cur = 0;
    if (cudaGetDevice(&cur) != cudaSuccess) { err = "dflash tap collect: querying the current device failed"; return false; }
    if (cur != dev_) { err = "dflash tap collect: assemble must run on the drafter's device"; return false; }
    // L14 (the dflash_w3 model window): validate every plan tap's producer slice ONCE
    // here, including the geometry guard the copy loop used to apply per tap; the
    // consumer's bounded wait below must re-check nothing that needs a device query.
    int need[kDFlashMaxTaps] = {};   // tap -> producer slice index, -1: same-device slice (no staging)
    for (int t = 0; t < plan_.n_taps; ++t) {
        int producers = 0, who = -1;
        for (int i = 0; i < n_slices; ++i)
            if (plan_.owner[t] == slices[i].stage) {
                ++producers;
                who = i;
                // L15: this guard covers EVERY producer slice, drafter-device (D2D)
                // included: a null plane is a loud refusal, never a null-source copy.
                if (slices[i].plane == nullptr || rows > slices[i].stride_rows || rows > slices[i].rows) {
                    err = "dflash tap collect: producer rows do not cover the assembled rows - refusing a stale read";
                    return false;
                }
            }
        if (producers != 1) {
            err = "dflash tap collect: tap " + std::to_string(t) + " has " + std::to_string(producers) +
                  " producer slices - refusing a stale-plane read";
            return false;
        }
        need[t] = plan_.tap_device(t) == dev_ ? -1 : who;
    }
    int slot = acquire_slot(k, key, err, lock);   // backpressure refusal stands (the original contract)
    if (slot < 0) return false;
    Slot& s = k.slots[slot];
    if (s.rows >= 0 && s.rows != rows) { err = "dflash tap collect: chunk slot rows disagree"; return false; }
    // L14 CONSUMER ORDERING: a foreign tap's rows exist in the pinned slot ONLY after
    // its producer's stage() ran for this key.  The chain's contract is producer-first
    // (prefill fires dflash_on_taps at every stage's chunk end, prefill.cpp's hand-off path);
    // the collector ENFORCES it instead of failing on a first-comer race.  NO staged
    // flag for the key means NO producer ran: deterministic refusal, fast, exactly the
    // original loud failure (the fail-closed tests keep their immediate refusals).
    // PARTIAL staging means producers are in flight: bind the slot as `awaiting` (no
    // producer re-binds it) and wait, bounded and epoch-guarded, for the remaining
    // flags.  stage() notifies; reset_staging()/disable() reject; an expired wait
    // keeps the original per-tap refusal - a loud failure, never a stale-plane read.
    int needed = 0, staged_count = 0;
    for (int t = 0; t < plan_.n_taps; ++t)
        if (need[t] >= 0) { ++needed; if (s.staged[t]) ++staged_count; }
    if (needed > 0 && staged_count == 0) {
        for (int t = 0; t < plan_.n_taps; ++t)
            if (need[t] >= 0) { err = "dflash tap collect: tap " + std::to_string(t) + " was never staged"; return false; }
    }
    if (needed > 0 && staged_count < needed) {
        s.awaiting = true;
        const uint64_t epoch = gen_;   // captured under mu_ at wait entry (as acquire_slot does)
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(kAssembleWaitMs);
        for (;;) {
            staged_count = 0;
            for (int t = 0; t < plan_.n_taps; ++t)
                if (need[t] >= 0 && s.staged[t]) ++staged_count;
            if (staged_count == needed) break;
            if (cv_.wait_until(lock, deadline) == std::cv_status::timeout || !enabled() || gen_ != epoch) {
                // Release the binding on EVERY refusal path (nothing was staged under
                // it) and wake anyone waiting on this collector.
                s.awaiting = false;
                s.key = -1; s.rows = -1;
                cv_.notify_all();
                std::fprintf(stderr, "strata serve: dflash tap collect: assemble key %lld - producers did not "
                                      "stage %d ms after the consumer's chunk (broken prefill tap chain) - refusing\n",
                             (long long) key, kAssembleWaitMs);
                if (!enabled() || gen_ != epoch) {
                    // reset_staging()/disable() since the wait began: the deterministic
                    // cancellation, exactly the producer backpressure's contract - the
                    // request's own error path, no per-tap guesswork.
                    err = "dflash tap collect: staging cancelled (request reset/disabled mid-wait)";
                    return false;
                }
                // The wait EXPIRED with the epoch unchanged: the chain is broken - the
                // loud original per-tap refusal, never a stale-plane read.
                for (int t = 0; t < plan_.n_taps; ++t)
                    if (need[t] >= 0 && !s.staged[t]) { err = "dflash tap collect: tap " + std::to_string(t) + " was never staged"; return false; }
                err = "dflash tap collect: tap 0 was never staged";   // unreachable: the loop above catches every unstaged foreign tap
                return false;
            }
        }
        s.awaiting = false;
    }
    const int64_t N = plan_.n_embd;
    const size_t bytes = (size_t) rows * N * type_bytes;
    uint8_t* plane = type_bytes == 4 ? (uint8_t*) plane_f32_ : (uint8_t*) plane_bf16_;
    for (int t = 0; t < plan_.n_taps; ++t) {
        uint8_t* dst = plane + (size_t) t * k.rows_cap * N * type_bytes;   // plane rows use the full capacity stride
        if (need[t] < 0) {   // drafter-device slice: D2D from the producer's plane
            int who = -1;
            for (int i = 0; i < n_slices; ++i)
                if (plan_.owner[t] == slices[i].stage) who = i;
            const uint8_t* src = (const uint8_t*) slices[who].plane + (size_t) t * slices[who].stride_rows * N * type_bytes;
            if (cudaMemcpyAsync(dst, src, bytes, cudaMemcpyDeviceToDevice, cs_) != cudaSuccess) {
                err = "dflash tap collect: same-device tap copy failed"; return false;
            }
        } else {
            if (cudaMemcpyAsync(dst, s.pin[t], bytes, cudaMemcpyHostToDevice, cs_) != cudaSuccess) {
                err = "dflash tap collect: H2D of the staged slice failed"; return false;
            }
        }
    }
    s.rows = -1;
    s.awaiting = false;
    for (int t = 0; t < kDFlashMaxTaps; ++t) s.staged[t] = false;
    if (s.ev != nullptr && cudaEventRecord(s.ev, cs_) != cudaSuccess) {
        err = "dflash tap collect: recording the slot's H2D completion event failed";   // pinned reuse needs it
        return false;
    }
    cv_.notify_all();   // the slot is RETIRED: wake any producer in backpressure, and a parked consumer
    return true;
}

}  // namespace strata::core
