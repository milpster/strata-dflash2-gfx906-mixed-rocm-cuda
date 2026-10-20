#pragma once
// Experimental gfx906 HC injection. The arithmetic follows one inspected rocBLAS GSU3
// implementation, not every BF16 library implementation. A new library/build needs fresh
// full-word component and real-activation shadow qualification before candidate output.
#include "strata/core/weights.hpp"
#include <cuda_runtime.h>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

extern "C" int strata_hc_bf16_n4_supported(int device);
extern "C" int strata_hc_bf16_n4_launch(const uint16_t*, const uint16_t*, float*, float*, int, void*);

namespace strata::prefill::hc_n4 {
constexpr size_t kBytes = 3 * 4096 * 4 * sizeof(float);
constexpr size_t kCharge = 2u << 20;  // conservative allocator-page budget, not a measured physical cost
constexpr size_t kHeadroom = 256u << 20;  // existing post-initialization serving warning floor

[[noreturn]] inline void fatal(const char* why) {
    std::fprintf(stderr, "prefill HC N4: %s\n", why);
    std::exit(1);
}

// Optional, checked owner scope. OFF and an unallocated workspace make no device calls.
struct DeviceScope {
    int previous = -1;
    explicit DeviceScope(int owner, bool active) {
        if (!active) return;
        int current = -1;
        if (owner < 0 || cudaGetDevice(&current) != cudaSuccess) fatal("owner query failed");
        if (current != owner) {
            if (cudaSetDevice(owner) != cudaSuccess) fatal("owner switch failed");
            previous = current;
        }
    }
    ~DeviceScope() {
        if (previous >= 0 && cudaSetDevice(previous) != cudaSuccess) fatal("owner restoration failed");
    }
    DeviceScope(const DeviceScope&) = delete;
    DeviceScope& operator=(const DeviceScope&) = delete;
};

// Only public run() owns the stage chain's lifetime. Drain on exceptional unwind,
// before a caller can destroy any stage; normal run() dismisses after its existing drain.
template<class Drain>
struct DrainOnExit {
    Drain drain;
    bool active;
    void dismiss() { active = false; }
    ~DrainOnExit() {
        if (!active) return;
        try { if (!drain()) fatal("pipeline failed during exceptional cleanup"); }
        catch (...) { fatal("pipeline exception during exceptional cleanup"); }
    }
};

struct State {
    struct Counts { uint64_t calls=0, takes=0, fallback=0, shadow=0, words=0; } counts[3];
    float* scratch = nullptr;
    int owner = -1;
    bool prepared = false, enabled = false, shadow = false, report = false;
    const char* reason = "not-prepared";
    std::vector<uint32_t> candidate, reference;

    static bool flag(const char* name, bool& valid) {
        const char* p = std::getenv(name);
        if (!p || !std::strcmp(p, "0")) return false;
        if (!std::strcmp(p, "1")) return true;
        valid = false;
        return false;
    }

    // Called once, after serving's mandatory allocations, before INFO/READY. Never in a
    // chunk, relayout, loan sizing or cache selection. A refusal retains the library route.
    bool prepare(int device, bool compatible, std::string& err) {
        if (prepared) return true;
        bool valid = true;
        enabled = flag("STRATA_HC_BF16_N4", valid);
        shadow = flag("STRATA_HC_BF16_N4_SHADOW", valid);
        if (!valid || (shadow && !enabled)) { err = "prefill HC N4: switches must be 0/1 and shadow requires ON"; return false; }
        prepared = true;
        owner = device;
        report = std::getenv("STRATA_HC_BF16_N4") || std::getenv("STRATA_HC_BF16_N4_SHADOW");
        reason = enabled ? "ineligible" : "disabled";
        if (!enabled || !compatible) return true;
        DeviceScope on(owner, true);
        const int supported = strata_hc_bf16_n4_supported(owner);
        if (supported < 0) { (void) cudaGetLastError(); reason = "architecture-query"; return true; }
        if (!supported) { reason = "architecture"; return true; }
        size_t before = 0, total = 0, after = 0;
        if (cudaMemGetInfo(&before, &total) != cudaSuccess) {
            (void) cudaGetLastError(); reason = "memory-query"; return true;
        }
        reason = "headroom";
        if (before < kHeadroom + kCharge) return true;
        void* p = nullptr;
        if (const auto status = cudaMalloc(&p, kBytes); status != cudaSuccess) {
            if (status != cudaErrorMemoryAllocation) { err = "prefill HC N4: unexpected allocation error"; return false; }
            (void) cudaGetLastError(); reason = "allocation"; return true;
        }
        // Includes any concurrent pressure in the conservative refusal. No cache is shrunk.
        const bool queried = cudaMemGetInfo(&after, &total) == cudaSuccess;
        if (!queried || after < kHeadroom || (before > after && before - after > kCharge) ||
            (reinterpret_cast<uintptr_t>(p) & 255)) {
            if (cudaFree(p) != cudaSuccess) fatal("refused allocation free failed");
            if (!queried) (void) cudaGetLastError();
            reason = "post-allocation-budget";
            return true;
        }
        scratch = static_cast<float*>(p);
        reason = "active";
        std::fprintf(stderr, "HC_N4_ALLOC owner=%d requested=%zu budget=%zu floor=%zu free_before=%zu free_after=%zu\n",
                     owner, kBytes, kCharge, kHeadroom, before, after);
        return true;
    }

    static bool span(const void* p, uint64_t bytes, uintptr_t& lo, uintptr_t& hi) {
        lo = reinterpret_cast<uintptr_t>(p);
        if (!p || !bytes || bytes > UINTPTR_MAX - lo) return false;
        hi = lo + static_cast<uintptr_t>(bytes);
        return true;
    }
    static bool disjoint(uintptr_t a, uintptr_t b, uintptr_t c, uintptr_t d) { return b <= c || d <= a; }

    // 0: declined without launch; 1: candidate complete/enqueued; -1: fatal caller error.
    // The single call site fixes alpha=1, beta=0, W-transpose, packed W/X and FP32 output.
    template<class Project>
    int call(Project& gm, const core::WeightRef* w, const uint16_t* x, float* y,
             int64_t t, bool f16, const uint16_t* low, int64_t ldx, void* stream, std::string& err) {
        if (!report && !enabled) return 0;
        Counts& c = counts[t == 4096 ? 0 : t == 4095 ? 1 : 2];
        ++c.calls;
        bool take = scratch && !f16 && !low && (t == 4096 || t == 4095) && ldx == 10240 &&
                    w && w->kind == core::WeightKind::Bf16InF32 && w->data && w->ne0 == 10240 && w->ne1 == 4 &&
                    w->bytes >= 81920 && gm.stream() == stream;
        uintptr_t xb=0, xe=0, wb=0, we=0, yb=0, ye=0, sb=0, se=0;
        if (take) {
            take = span(scratch, kBytes, sb, se) && !(sb & 255) &&
                   span(x, uint64_t(t)*10240*2, xb, xe) && !(xb & 1) &&
                   span(w->data, 81920, wb, we) && !(wb & 1) &&
                   span(y, uint64_t(t)*4*sizeof(float), yb, ye) && !(yb & 3) &&
                   disjoint(sb,se,xb,xe) && disjoint(sb,se,wb,we) && disjoint(sb,se,yb,ye) &&
                   disjoint(xb,xe,wb,we) && disjoint(xb,xe,yb,ye) && disjoint(wb,we,yb,ye);
        }
        if (!take) { ++c.fallback; return 0; }
        int current = -1;
        if (cudaGetDevice(&current) != cudaSuccess || current != owner) {
            err = "prefill HC N4: current owner differs"; return -1;
        }
        const size_t words = size_t(t)*4, bytes = words*sizeof(uint32_t);
        if (shadow) {
            try { candidate.resize(16384); reference.resize(16384); }
            catch (...) { err = "prefill HC N4: bounded shadow host allocation"; return -1; }
        }
        if (strata_hc_bf16_n4_launch(x, static_cast<const uint16_t*>(w->data), y, scratch, int(t), stream) != int(cudaSuccess))
            fatal("candidate launch failed; cannot fall back after launch");
        ++c.takes;
        if (shadow) {
            const auto st = static_cast<cudaStream_t>(stream);
            const auto copy = cudaMemcpyAsync(candidate.data(), y, bytes, cudaMemcpyDeviceToHost, st);
            const auto sync = cudaStreamSynchronize(st);
            if (copy != cudaSuccess || sync != cudaSuccess) fatal("candidate shadow copy/sync failed");
            // Original result remains in Y; this checks activations, not candidate-output trajectory.
            gm.bf16(x, static_cast<const uint16_t*>(w->data), y, t, 4, 10240, 4, 0.0f, 0);
            if (cudaGetLastError() != cudaSuccess) fatal("reference shadow launch failed");
            const auto rcopy = cudaMemcpyAsync(reference.data(), y, bytes, cudaMemcpyDeviceToHost, st);
            const auto rsync = cudaStreamSynchronize(st);
            if (rcopy != cudaSuccess || rsync != cudaSuccess) fatal("reference shadow copy/sync failed");
            for (size_t i=0; i<words; ++i)
                if (candidate[i] != reference[i] || (candidate[i] & 0x7f800000u) == 0x7f800000u)
                    fatal("shadow full-word/finite comparison failed");
            ++c.shadow; c.words += words;
        }
        return 1;
    }

    // Caller has joined any successor and drained its existing compute stream on owner.
    void release() {
        if (!scratch) return;
        DeviceScope on(owner, true);
        if (cudaFree(scratch) != cudaSuccess) fatal("workspace free failed");
        scratch = nullptr;
    }
    ~State() {
        if (scratch) fatal("workspace released without its owner drain");
        for (auto* v : {&candidate, &reference}) {
            volatile uint32_t* p = v->data();
            for (size_t i=0; i<v->size(); ++i) p[i] = 0;
        }
        if (!report) return;
        std::fprintf(stderr, "HC_N4_STATE owner=%d enabled=%d shadow_mode=%d reason=%s\n", owner, int(enabled), int(shadow), reason);
        for (int i=0; i<3; ++i) {
            const auto& c = counts[i];
            if (c.calls) std::fprintf(stderr, "HC_N4 owner=%d T=%d calls=%llu takes=%llu fallback=%llu shadow=%llu words=%llu\n",
                owner, i==0 ? 4096 : i==1 ? 4095 : 0, (unsigned long long)c.calls, (unsigned long long)c.takes,
                (unsigned long long)c.fallback, (unsigned long long)c.shadow, (unsigned long long)c.words);
        }
    }
};
}  // namespace strata::prefill::hc_n4
