// src/core/dflash_tap_collect.cpp - implementation of the bounded cross-device tap
// collector (include/strata/core/dflash_tap_collect.hpp).  Serial-only transport:
// D2H runs host-synchronously inside the producer's completion context; H2D/D2D run
// on the drafter stream, stream-ordered against the drafter's fusion reads.
#include <strata/core/dflash_tap_collect.hpp>
#include <strata/core/on_device.hpp>

namespace strata::core {

DFlashTapCollector::Kind& DFlashTapCollector::kind(int type_bytes) { return type_bytes == 4 ? f32_ : bf16_; }

bool DFlashTapCollector::enable(const DFlashTapPlan& plan, int drafter_dev, int64_t f32_rows,
                                int64_t bf16_rows, std::string& err) {
    disable();
    if (plan.n_taps <= 0) { err = "dflash tap collect: plan not built"; return false; }
    if (drafter_dev < 0) { err = "dflash tap collect: no drafter device"; return false; }
    if (f32_rows <= 0 && bf16_rows <= 0) { err = "dflash tap collect: both row caps are zero"; return false; }
    plan_ = plan; dev_ = drafter_dev;
    const int64_t N = plan_.n_embd;
    const auto alloc_kind = [&](Kind& k, int64_t rows_cap, int type_bytes) -> bool {
        k.rows_cap = rows_cap; k.type_bytes = type_bytes;
        for (int i = 0; i < kSlots; ++i) {
            Slot& s = k.slots[i];
            s.key = -1; s.rows = -1;
            for (int t = 0; t < plan_.n_taps; ++t) {
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
    const auto free_kind = [&](Kind& k) {
        for (int i = 0; i < kSlots; ++i) {
            Slot& s = k.slots[i];
            for (int t = 0; t < kDFlashMaxTaps; ++t) {
                if (s.pin[t] != nullptr) cudaFreeHost(s.pin[t]);
                s.pin[t] = nullptr; s.staged[t] = false;
            }
            if (s.ev != nullptr) cudaEventDestroy(s.ev);
            s.ev = nullptr; s.key = -1; s.rows = -1;
        }
        k.rows_cap = 0;
    };
    if (plane_f32_ != nullptr) cudaFree(plane_f32_);
    if (plane_bf16_ != nullptr) cudaFree(plane_bf16_);
    plane_f32_ = nullptr; plane_bf16_ = nullptr;
    free_kind(f32_); free_kind(bf16_);
    plan_ = DFlashTapPlan{}; dev_ = -1; f32_rows_ = 0; bf16_rows_ = 0;
    plane_bytes_ = 0; pin_bytes_ = 0; cs_ = nullptr;
}

int DFlashTapCollector::acquire(Kind& k, int64_t key) {
    int victim = -1;
    for (int i = 0; i < kSlots; ++i) {
        if (k.slots[i].key == key) return i;
        if (k.slots[i].key < 0) victim = i;
    }
    if (victim < 0) {   // all bound: evict the oldest binding (keys grow with position)
        victim = 0;
        for (int i = 1; i < kSlots; ++i)
            if (k.slots[i].key < k.slots[victim].key) victim = i;
        // Drain the evicted slot's last H2D before reusing its pinned rows.
        if (k.slots[victim].ev != nullptr) cudaEventSynchronize(k.slots[victim].ev);
    }
    Slot& s = k.slots[victim];
    s.key = key; s.rows = -1;
    for (int t = 0; t < kDFlashMaxTaps; ++t) s.staged[t] = false;
    return victim;
}

bool DFlashTapCollector::stage(int64_t key, const Slice& slice, int type_bytes, std::string& err) {
    if (!enabled()) { err = "dflash tap collect: not enabled"; return false; }
    Kind& k = type_bytes == 4 ? f32_ : bf16_;
    if (k.rows_cap <= 0) { err = "dflash tap collect: that plane kind is not enabled"; return false; }
    if (slice.plane == nullptr) { err = "dflash tap collect: producer plane is null"; return false; }
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
        if (slot < 0) {
            slot = acquire(k, key);
            Slot& s = k.slots[slot];
            if (s.rows >= 0 && s.rows != slice.rows) { err = "dflash tap collect: chunk slot rows disagree"; return false; }
            s.rows = slice.rows;
            if (s.ev != nullptr) cudaEventSynchronize(s.ev);   // last H2D has read its pinned copy
        }
        const size_t bytes = (size_t) slice.rows * N * type_bytes;
        const OnDevice on_producer(plan_.dev[slice.stage]);
        if (cudaMemcpy(k.slots[slot].pin[t],
                       (const uint8_t*) slice.plane + (size_t) t * slice.stride_rows * N * type_bytes,
                       bytes, cudaMemcpyDeviceToHost) != cudaSuccess) {
            err = "dflash tap collect: D2H of the producer slice failed";
            return false;
        }
        k.slots[slot].staged[t] = true;
    }
    return true;
}

bool DFlashTapCollector::assemble(int64_t key, const Slice* slices, int n_slices, int64_t rows,
                                  int type_bytes, std::string& err) {
    if (!enabled()) { err = "dflash tap collect: not enabled"; return false; }
    Kind& k = type_bytes == 4 ? f32_ : bf16_;
    if (k.rows_cap <= 0) { err = "dflash tap collect: that plane kind is not enabled"; return false; }
    if (rows <= 0 || rows > k.rows_cap) { err = "dflash tap collect: assemble rows exceed the plane capacity"; return false; }
    int cur = 0;
    cudaGetDevice(&cur);
    if (cur != dev_) { err = "dflash tap collect: assemble must run on the drafter's device"; return false; }
    int slot = -1;
    for (int t = 0; t < plan_.n_taps; ++t) {
        int producers = 0, who = -1;
        for (int i = 0; i < n_slices; ++i)
            if (plan_.owner[t] == slices[i].stage) {
                ++producers;
                who = i;
                if (slices[i].plane == nullptr || rows > slices[i].stride_rows) {
                    err = "dflash tap collect: producer rows exceed its plane stride";
                    return false;
                }
            }
        if (producers != 1) {
            err = "dflash tap collect: tap " + std::to_string(t) + " has " + std::to_string(producers) +
                  " producer slices - refusing a stale-plane read";
            return false;
        }
        if (slot < 0) slot = acquire(k, key);
        Slot& s = k.slots[slot];
        if (s.rows >= 0 && s.rows != rows) { err = "dflash tap collect: chunk slot rows disagree"; return false; }
        const int64_t N = plan_.n_embd;
        const size_t bytes = (size_t) rows * N * type_bytes;
        void* dst = (type_bytes == 4 ? (void*) plane_f32_ : (void*) plane_bf16_) + (size_t) t * k.rows_cap * N * type_bytes;
        if (plan_.tap_device(t) == dev_) {
            const void* src = (const uint8_t*) slices[who].plane + (size_t) t * slices[who].stride_rows * N * type_bytes;
            if (cudaMemcpyAsync(dst, src, bytes, cudaMemcpyDeviceToDevice, cs_) != cudaSuccess) {
                err = "dflash tap collect: same-device tap copy failed"; return false;
            }
        } else {
            if (!s.staged[t]) { err = "dflash tap collect: tap " + std::to_string(t) + " was never staged"; return false; }
            if (cudaMemcpyAsync(dst, s.pin[t], bytes, cudaMemcpyHostToDevice, cs_) != cudaSuccess) {
                err = "dflash tap collect: H2D of the staged slice failed"; return false;
            }
        }
    }
    if (slot >= 0) {
        k.slots[slot].rows = -1;
        for (int t = 0; t < kDFlashMaxTaps; ++t) k.slots[slot].staged[t] = false;
        if (k.slots[slot].ev != nullptr) cudaEventRecord(k.slots[slot].ev, cs_);   // drains protect pinned reuse
    }
    return true;
}

}  // namespace strata::core
