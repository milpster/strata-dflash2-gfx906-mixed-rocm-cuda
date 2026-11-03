// src/core/dflash_tap_collect.cpp - implementation of the bounded cross-device tap
// collector (include/strata/core/dflash_tap_collect.hpp).  Serial-only transport:
// D2H runs host-synchronously inside the producer's completion context; H2D/D2D run
// on the drafter stream, stream-ordered against the drafter's fusion reads.
// All slot bookkeeping and transfer enqueue is serialized by mu_ (the prefill chain's
// producer stages chunk N+1 on its host thread while the final stage assembles chunk
// N on its own - prefill.cpp:4948-4963 is the concurrent-call evidence).
#include <strata/core/dflash_tap_collect.hpp>
#include <strata/core/dflash.hpp>   // F1: deferral_context() (declared there, defined in dflash_runtime.cpp)
#include <strata/core/on_device.hpp>

#include <chrono>
#include <cstdio>
#include <stdexcept>
#include <utility>

namespace strata::core {

namespace {
constexpr bool valid_type_bytes(int type_bytes) { return type_bytes == 2 || type_bytes == 4; }
// F2's mode flag is DFlashTapCollector::async_d2h_on() (the header): read ONCE at the
// collector's startup, never per chunk; flag off, or a slice without a producer stream
// (the decode callers), keeps today's blocking D2H byte-for-byte.
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

// ---- F2 staging worker (TAP_OVERLAP_DESIGN §2 changes 8,10,11): ONE thread, created at
// enable(), joined at disable().  It drains each async D2H's event on the PRODUCER's device
// (same device as the event's stream - the host-side cross-device pattern production already
// runs at stage()'s slot-event drain, cpp:205), then sets the staged flag under mu_ ONLY if
// the cancellation epoch still matches (the L10 contract: an old request's producer never
// stages into a new one).  A copy fault becomes a sticky per-kind error assemble() surfaces.
// "staged" keeps its exact L14 meaning - bytes complete and drained - because the flag is
// set only after this host-side drain.  No cross-device STREAM event is added (§2 change 11).
void DFlashTapCollector::worker_loop() {
    for (;;) {
        PendingD2H item{};
        {
            std::unique_lock<std::mutex> qk(q_mu_);
            q_cv_.wait(qk, [&] { return !q_.empty() || worker_stop_; });
            if (!q_.empty()) {
                item = q_.front();
                q_.erase(q_.begin());
            } else if (worker_stop_) {
                return;   // disable(): the queue is empty; disable drains the events itself
            }
        }
        if (item.ev == nullptr) continue;
        cudaSetDevice(item.dev);   // the event's stream's device: the same-device drain
        const bool ok = cudaEventSynchronize(item.ev) == cudaSuccess;
        std::unique_lock<std::mutex> lock(mu_);
        if (gen_ != item.epoch) {   // reset/disable since enqueue: drop the flag (L10 contract)
            cv_.notify_all();
            continue;
        }
        Kind& k = kind(item.type_bytes);
        if (!ok) {   // §2 change 10: a copy fault is sticky per kind, surfaced by assemble()
            k.async_fault = true;
            k.async_fault_msg = "dflash: tap collect: D2H of the producer slice failed";
            cv_.notify_all();
            continue;
        }
        Slot& s = k.slots[item.slot];
        if (s.key == item.key) s.staged[item.tap] = true;   // the slot was not re-bound since
        cv_.notify_all();   // L14: a consumer in its bounded assemble() wake check re-reads flags
    }
}

void DFlashTapCollector::worker_start() {
    if (!async_d2h_on() || pin_bytes_ <= 0) return;   // F2 off: no thread, today's path
    worker_stop_ = false;                              // mu_ is held by enable() here
    try {
        worker_ = std::thread([this] { worker_loop(); });
    } catch (const std::system_error&) {
        // No thread: the async D2H degrades to today's blocking copy (the flag-off path),
        // never a lost drain.  Loud, non-fatal.
        std::fprintf(stderr, "strata: dflash tap collect: staging worker thread failed to start; "
                             "async D2H off (blocking copy)\n");
    }
}

// Join the staging worker: it drains every queued async D2H (each a same-device host drain)
// before exiting, so every enqueued copy has landed its event when this returns.  The caller
// (disable) HOLDS `lock` on mu_; mu_ is RELEASED around worker_.join() only - the worker
// takes mu_ to set staged flags - and re-taken before return.  (Review fix: the previous
// body locked mu_ at entry while disable() already held it - a deadlock on every disable.)
void DFlashTapCollector::worker_stop(std::unique_lock<std::mutex>& lock) {
    worker_stop_ = true;   // atomic; no mutex needed to publish
    q_cv_.notify_all();    // wake the worker (notifying without holding q_mu_ is legal)
    if (worker_.joinable()) {
        lock.unlock();
        worker_.join();
        lock.lock();
    }
}

// mu_ HELD by the caller.  The pinned rows are written by the producer device and read by
// the drafter's H2D: reset/freeze needs every enqueued D2H drained FIRST (§2 change 12).
// A cross-device HOST sync - the proven pattern (§2 change 11's evidence: cpp:205).
void DFlashTapCollector::drain_async_d2h(Kind& k) {
    int cur = 0;
    cudaGetDevice(&cur);
    bool switched = false;
    for (int i = 0; i < kSlots; ++i)
        for (int t = 0; t < kDFlashMaxTaps; ++t) {
            if (k.slots[i].d2h_ev[t] == nullptr || k.slots[i].d2h_dev[t] < 0) continue;
            const int dev = k.slots[i].d2h_dev[t];   // the device the async D2H ran on (stage() set it)
            if (dev != cur && cudaSetDevice(dev) == cudaSuccess) switched = true;
            (void) cudaEventSynchronize(k.slots[i].d2h_ev[t]);
        }
    if (switched && cur >= 0) cudaSetDevice(cur);
}

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
    // F2 (§2 change 8): one per-slot per-tap completion event where pinned staging exists
    // (FOREIGN taps only, the same set as pin[]).  On the drafter device, like the slot's
    // H2D event; reused like it (the worker's host drain is its consumption).  Created here
    // once; drained and destroyed at disable() (§2 change 12).
    const auto arm_async_events = [&](Kind& k) -> bool {
        for (int i = 0; i < kSlots; ++i)
            for (int t = 0; t < plan_.n_taps; ++t) {
                if (k.slots[i].pin[t] == nullptr) continue;
                if (cudaEventCreate(&k.slots[i].d2h_ev[t]) != cudaSuccess) {
                    err = "dflash tap collect: staging event creation failed";
                    return false;
                }
                k.slots[i].d2h_dev[t] = -1;
            }
        return true;
    };
    if (!arm_async_events(f32_) || !arm_async_events(bf16_)) { disable(); return false; }
    worker_start();   // F2 off: no thread (today's path, byte-for-byte)
    return true;
}

void DFlashTapCollector::disable() {
    std::unique_lock<std::mutex> lock(mu_);   // every metadata mutation is mu_-guarded (L10 review)
    worker_stop(lock);   // F2 (§2 change 12): join the staging worker FIRST - it drains every
                         // queued async D2H; the events are drained below before pinned rows free
    gen_ += 1;                               // pending producers reject deterministically
    const OnDevice on_dev(dev_);   // events and device planes live on dev_; drain needs its context
    const auto free_kind = [&](Kind& k) {
        drain_async_d2h(k);   // F2: the pinned rows are written on the PRODUCER's device -
                              // drain every enqueued D2H there before free/destroy (§2 change 12)
        for (int i = 0; i < kSlots; ++i) {
            Slot& s = k.slots[i];
            if (s.ev != nullptr) (void) cudaEventSynchronize(s.ev);   // the collector owns its outstanding H2Ds
            for (int t = 0; t < kDFlashMaxTaps; ++t) {
                if (s.pin[t] != nullptr) cudaFreeHost(s.pin[t]);
                s.pin[t] = nullptr; s.staged[t] = false;
                if (s.d2h_ev[t] != nullptr) cudaEventDestroy(s.d2h_ev[t]);
                s.d2h_ev[t] = nullptr; s.d2h_dev[t] = -1;
            }
            if (s.ev != nullptr) cudaEventDestroy(s.ev);
            s.ev = nullptr; s.key = -1; s.rows = -1; s.awaiting = false;
        }
        k.rows_cap = 0;
        k.async_fault = false; k.async_fault_msg.clear();   // F2: fresh state on re-enable
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
        drain_async_d2h(k);   // F2 (§2 change 12): the pinned rows are written on the producer
                              // device - drain every enqueued D2H there BEFORE the bindings
                              // reset (the cross-device HOST pattern, §2 change 11's evidence)
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
        const uint8_t* src = (const uint8_t*) slice.plane + (size_t) t * slice.stride_rows * N * type_bytes;
        // F2 (§2 change 8, worker-thread form): with the flag AND a producer stream on the
        // slice (the prompt wiring, generate.cpp), the D2H becomes cudaMemcpyAsync on the
        // PRODUCER's own stream (m.cs): stream-ordered ahead of the next chunk's plane
        // writes on that same stream - the plane needs no double buffer (design §3), and NO
        // cross-device stream event exists (design §2 change 11).  The lock is RELEASED for
        // the enqueue, so assemble() on the drafter thread overlaps the copy (the map's
        // mu_-held D2H is the serialized term).  The staged flag lands on the worker's
        // host-side event drain, so it keeps its exact L14 meaning: bytes complete and
        // drained.  No flag, no stream (decode callers): today's blocking copy below.
        const bool async_d2h = s.d2h_ev[t] != nullptr && slice.stream != nullptr && async_d2h_on();
        if (async_d2h) {
            const int pdev = plan_.dev[slice.stage];
            cudaEvent_t ev = s.d2h_ev[t];
            const uint64_t epoch = gen_;   // captured under mu_ (the L10 contract)
            s.d2h_dev[t] = pdev;
            lock.unlock();
            const OnDevice on_producer(pdev);
            const bool ok = cudaMemcpyAsync(s.pin[t], src, bytes, cudaMemcpyDeviceToHost, slice.stream) == cudaSuccess
                && cudaEventRecord(ev, slice.stream) == cudaSuccess;
            if (!ok) {
                (void) cudaStreamSynchronize(slice.stream);   // drain before recording: the
                                                              // pinned rows stay owned by the host
                lock.lock();
                err = "dflash tap collect: D2H of the producer slice failed";
                return false;
            }
            {
                std::lock_guard<std::mutex> qk(q_mu_);
                q_.push_back(PendingD2H{type_bytes, slot, t, pdev, key, s.pin[t], epoch, ev});
            }
            q_cv_.notify_one();
            lock.lock();
            continue;   // the worker sets staged[t] after the same-device event drain
        }
        const OnDevice on_producer(plan_.dev[slice.stage]);
        if (cudaMemcpy(s.pin[t], src, bytes, cudaMemcpyDeviceToHost) != cudaSuccess) {
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
                    err = "dflash tap collect: producer rows do not cover the assembled rows - refusing a stale read "
                          "(tap " + std::to_string(t) + " owner " + std::to_string(plan_.owner[t]) +
                          " slice stage " + std::to_string(slices[i].stage) +
                          " plane " + (slices[i].plane == nullptr ? "NULL" : "ok") +
                          " rows " + std::to_string(rows) +
                          " > stride_rows " + std::to_string(slices[i].stride_rows) +
                          " or slice rows " + std::to_string(slices[i].rows) + ")";
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
    // F2 (§2 change 10): a copy fault the staging worker recorded is surfaced HERE, on the
    // consumer side of the same fatal path (prefill.cpp:6633 -> the request fails), with the
    // synchronous D2H failure's exact text.  Checked before any transfer enqueues.
    if (k.async_fault) { err = k.async_fault_msg; return false; }
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
    // F1 (§2 change 4, hazard H1 - MANDATORY with the deferral): the drafter-local D2D reads
    // the producer stage's SINGLE-buffered plane on cs_; with add_context's tail host sync
    // deferred, nothing ordered it against the successor chunk's writes on the stage's own
    // stream.  Record an event on cs_ once the plane's copies are enqueued and drain it on
    // this (the consumer's) host thread - same device, same device's stream, the proven
    // pattern the producer uses at stage()'s slot-event drain (cpp:205).  The barrier covers
    // only the enqueued plane copies (2x40 MiB D2D in the bench geometry), not the fusion.
    // Flag off: the host sync the deferred one replaced already covers this - no event, no
    // sync, today's code.
    if (deferral_context() && cs_ != nullptr) {
        static cudaEvent_t d2d_barrier = nullptr;   // one consumer thread per collector (serial-only)
        if (d2d_barrier == nullptr && cudaEventCreateWithFlags(&d2d_barrier, cudaEventDisableTiming) != cudaSuccess) {
            err = "dflash tap collect: creating the D2D barrier event failed"; return false;
        }
        if (cudaEventRecord(d2d_barrier, cs_) != cudaSuccess) {
            err = "dflash tap collect: recording the D2D barrier event failed"; return false;
        }
        if (cudaEventSynchronize(d2d_barrier) != cudaSuccess) {
            err = "dflash: prompt context consumption failed"; return false;
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
