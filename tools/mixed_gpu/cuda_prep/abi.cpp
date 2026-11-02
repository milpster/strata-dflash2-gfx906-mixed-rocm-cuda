// tools/mixed_gpu/cuda_prep/abi.cpp - the GPU side of the PREPARATION ABI v1 (compile-only Phase 2 asset).
//
// Compiled into strata_prep_cuda.so (nvcc, sm_86) and strata_prep_hip.so (HIP, gfx906) - the source is the
// same: the CUDA runtime names go through the root's hip_compat layer in the HIP build, exactly as the root
// project does (CMakeLists.txt:192-211).  It is the wrapper ONLY: the kernels are the unmodified
// src/kernels/cuda/iq_kernels.cu and native_moe.cu.  No engine, no helper scheduling, no graph capture, no
// stream pool - a decode-only staging wrapper for one token, serialized by a mutex.
//
// Every host buffer is staged to the device, every kernel call is checked, the result is staged back, and every
// allocation is freed on every path (a scope guard, so no leak on a failure).  The host-side guards from
// abi_validate.cpp run before the first cuda*/hip* call; the kernel gate native_expert_supported runs here,
// in this translation unit, which is why a CPU build reports STRATA_PREP_V1_E_BACKEND for
// strata_prep_v1_supported instead of pretending to know the kernel's instances.
#include "abi.hpp"
#include "abi_validate.hpp"

#include "strata/kernels/iq_kernels.hpp"
#include "strata/kernels/native_moe.hpp"

#include <cuda_runtime.h>

#include <cstdint>
#include <cstring>
#include <mutex>
#include <string>

namespace prep = strata::prep;

namespace {

// STRATA_Q6K_EXPERTS as a bool: the root adds -DSTRATA_Q6K_EXPERTS=1 by default (CMakeLists.txt:139-141).
constexpr bool q6k_experts_enabled() {
#ifdef STRATA_Q6K_EXPERTS
    return STRATA_Q6K_EXPERTS != 0;
#else
    return false;
#endif
}

// One outstanding serialized request (the ABI's contract), per library instance.
std::mutex g_gate;

// RAII owner of the device buffers of one call; frees on every path and records a cudaFree failure truthfully.
struct Device {
    void* p[8] = {};
    int n = 0;
    int free_err = 0;
    void* take(size_t bytes) {
        void* v = nullptr;
        if (bytes == 0) bytes = 16;  // never cudaMalloc(0): a zero-size alloc is a runtime call we do not make
        if (cudaMalloc(&v, bytes) != cudaSuccess) return nullptr;
        this->p[this->n++] = v;
        return v;
    }
    ~Device() {
        for (int i = 0; i < n; ++i)
            if (cudaFree(p[i]) != cudaSuccess) free_err = 1;
    }
};

int cuda_status(cudaError_t e, std::string& err) {
    err = cudaGetErrorString(e);
    return STRATA_PREP_V1_E_CUDA;
}

// The ABI's stream: the LEGACY default stream, as a non-null sentinel.  The pinned kernels take void* stream
// and their contracts reject nullptr (native_moe.cu:153-155 "requires a stream ..."); cudaStreamLegacy /
// hipStreamLegacy ARE the default stream and are non-null, so the kernel checks pass with unchanged
// default-stream semantics.  Every call is synchronized before the host result is read.
#if defined(STRATA_HIP_GFX906) || defined(__HIP__)
// hipStreamLegacy is not exported through the root's hip_compat headers; both vendors' legacy-default-stream
// sentinel is the literal 0x1 (cudaStreamLegacy == (cudaStream_t)0x1, hipStreamLegacy == (hipStream_t)0x1).
void* const kStream = (void*) 0x1;
#else
void* const kStream = (void*) cudaStreamLegacy;
#endif

// The group/entry arrays the grouped kernel reads device-side (iq_kernels.hpp:72-74), in one allocation:
// grp_ptr[0] | grp_start[0..1] | n_groups | ent_dst[0] | ent_tok[0].  One group, one entry, token 0 -> row 0.
struct GroupMeta {
    unsigned long long grp_ptr;
    int32_t grp_start[2];
    int32_t n_groups;
    int32_t ent_dst;
    int32_t ent_tok;
};

int stage_and_run(const prep::PrepV1Layout& L, const StrataPrepV1Expert& req, std::string& err) {
    // v1 has no NVFP4 tail and no Hadamard-folded expert: the guards refuse tail_off, and the native pack has
    // neither (design gate C3).  Assert it here, where the kernel layout is built, so a layout that grew a tail
    // or a rotation is refused, never handed to the kernel.
    if (L.tail_off != 0) {
        err = "stage: v1 accepts no NVFP4 tail (tail_off != 0)";
        return STRATA_PREP_V1_E_TYPE;
    }
    strata::kernels::NativeExpertLayout k{};
    k.gu_type = L.gu_type;
    k.d_type = L.d_type;
    k.n_embd = L.n_embd;
    k.n_ff = L.n_ff;
    k.gu_row = L.gu_row;
    k.d_row = L.d_row;
    k.up_off = L.up_off;
    k.down_off = L.down_off;
    k.bytes = L.bytes;
    k.tail_off = 0;  // v1: no NVFP4 tail, no Hadamard
    k.had_block = 0;
    k.d_signs = nullptr;  // had_block 0: identity (the native pack is not Hadamard-folded)

    // The activation staging is Q8_1 for EVERY accepted gate/up type, which is exactly what the engine's native
    // path does: RemoteExperts::begin for a native pack runs quantize_q8_1_rows(x, 1, n_embd, act) and then
    // native_expert_grouped (src/core/remote_experts.cpp:331-342), and the grouped kernel reads every gate/up
    // type's rows against block_q8_1 (iq_kernels.cu:788-795 row_dot over Fmt<TY> and block_q8_1).  The down side
    // quantizes h inside native_expert_grouped, so the ABI stages no h buffer at all (design gate C3).
    const size_t xq_bytes = (size_t) (L.n_embd / 32) * 36;  // one token as q8_1 blocks (block_q8_1 is 36 bytes)
    const size_t scratch = strata::kernels::native_expert_scratch_bytes(1, L.n_ff);

    Device dev;
    uint8_t* d_blob = (uint8_t*) dev.take(L.bytes);
    float* d_x = (float*) dev.take((size_t) L.n_embd * sizeof(float));
    float* d_out = (float*) dev.take((size_t) L.n_embd * sizeof(float));
    uint8_t* d_xq = (uint8_t*) dev.take(xq_bytes);
    uint8_t* d_scratch = (uint8_t*) dev.take(scratch);
    uint8_t* d_meta = (uint8_t*) dev.take(sizeof(GroupMeta));
    if (d_blob == nullptr || d_x == nullptr || d_out == nullptr || d_xq == nullptr || d_scratch == nullptr ||
        d_meta == nullptr) {
        err = "cudaMalloc failed";
        return STRATA_PREP_V1_E_CUDA;
    }
    GroupMeta h{};
    h.grp_ptr = (unsigned long long) (uintptr_t) d_blob;  // the group's blob at its DEVICE address
    h.grp_start[0] = 0;
    h.grp_start[1] = 1;  // the entries of group 0 are [0, 1): one entry
    h.n_groups = 1;
    h.ent_dst = 0;       // the entry writes row 0 of out
    h.ent_tok = 0;       // the entry reads token 0's q8_1 activation (the one input token)
    if (cudaMemcpy(d_blob, req.blob, L.bytes, cudaMemcpyHostToDevice) != cudaSuccess) {
        err = "cudaMemcpy blob";
        return STRATA_PREP_V1_E_CUDA;
    }
    if (cudaMemcpy(d_x, req.x, (size_t) L.n_embd * sizeof(float), cudaMemcpyHostToDevice) != cudaSuccess) {
        err = "cudaMemcpy x";
        return STRATA_PREP_V1_E_CUDA;
    }
    if (cudaMemcpy(d_meta, &h, sizeof(h), cudaMemcpyHostToDevice) != cudaSuccess) {
        err = "cudaMemcpy group meta";
        return STRATA_PREP_V1_E_CUDA;
    }

    strata::kernels::quantize_q8_1_rows(d_x, 1, L.n_embd, d_xq, kStream);
    if (const cudaError_t e = cudaGetLastError(); e != cudaSuccess) return cuda_status(e, err);

    strata::kernels::native_expert_grouped(k, &((GroupMeta*) d_meta)->grp_ptr, ((GroupMeta*) d_meta)->grp_start,
                                           &((GroupMeta*) d_meta)->n_groups, &((GroupMeta*) d_meta)->ent_dst,
                                           &((GroupMeta*) d_meta)->ent_tok, 1, 1, d_xq, d_scratch, d_out, kStream, 1);
    if (const cudaError_t e = cudaGetLastError(); e != cudaSuccess) return cuda_status(e, err);

    if (cudaMemcpy(req.out, d_out, (size_t) L.n_embd * sizeof(float), cudaMemcpyDeviceToHost) != cudaSuccess) {
        err = "cudaMemcpy out";
        return STRATA_PREP_V1_E_CUDA;
    }
    if (const cudaError_t e = cudaDeviceSynchronize(); e != cudaSuccess) return cuda_status(e, err);
    if (dev.free_err) {
        err = "cudaFree failed (recorded, the result above is staged)";
        return STRATA_PREP_V1_E_CUDA;
    }
    return STRATA_PREP_V1_OK;
}

int run_combine(const StrataPrepV1Combine& req, std::string& err) {
    const size_t floats = (size_t) req.k * (size_t) req.n_embd;
    Device dev;
    float* d_parts = (float*) dev.take(floats * sizeof(float));
    float* d_w = (float*) dev.take((size_t) req.k * sizeof(float));
    float* d_shared = req.has_shared ? (float*) dev.take((size_t) req.n_embd * sizeof(float)) : nullptr;
    float* d_out = (float*) dev.take((size_t) req.n_embd * sizeof(float));
    if (d_parts == nullptr || d_w == nullptr || d_out == nullptr || (req.has_shared && d_shared == nullptr)) {
        err = "cudaMalloc failed";
        return STRATA_PREP_V1_E_CUDA;
    }
    if (cudaMemcpy(d_parts, req.parts, floats * sizeof(float), cudaMemcpyHostToDevice) != cudaSuccess) {
        err = "cudaMemcpy parts";
        return STRATA_PREP_V1_E_CUDA;
    }
    if (cudaMemcpy(d_w, req.weights, (size_t) req.k * sizeof(float), cudaMemcpyHostToDevice) != cudaSuccess) {
        err = "cudaMemcpy weights";
        return STRATA_PREP_V1_E_CUDA;
    }
    if (req.has_shared &&
        cudaMemcpy(d_shared, req.shared, (size_t) req.n_embd * sizeof(float), cudaMemcpyHostToDevice) != cudaSuccess) {
        err = "cudaMemcpy shared";
        return STRATA_PREP_V1_E_CUDA;
    }
    // The exactly-once weighted reduction is the pinned native_moe_combine kernel: the first product rounds to
    // F32, the following products accumulate with FMA in expert order, shared is added once and NOT weighted.
    strata::kernels::native_moe_combine(d_parts, d_w, d_shared, d_out, req.n_embd, req.k, kStream);
    if (const cudaError_t e = cudaGetLastError(); e != cudaSuccess) return cuda_status(e, err);
    if (cudaMemcpy(req.out, d_out, (size_t) req.n_embd * sizeof(float), cudaMemcpyDeviceToHost) != cudaSuccess) {
        err = "cudaMemcpy out";
        return STRATA_PREP_V1_E_CUDA;
    }
    if (const cudaError_t e = cudaDeviceSynchronize(); e != cudaSuccess) return cuda_status(e, err);
    if (dev.free_err) {
        err = "cudaFree failed (recorded, the result above is staged)";
        return STRATA_PREP_V1_E_CUDA;
    }
    return STRATA_PREP_V1_OK;
}

}  // namespace

extern "C" {

uint32_t strata_prep_v1_abi_version(void) { return STRATA_PREP_V1_ABI_VERSION; }

int strata_prep_v1_get_info(StrataPrepV1Info* info) {
    if (info == nullptr) return STRATA_PREP_V1_E_NULL;
    std::string err;
    if (const int rc = prep::validate_info(*info, err)) return rc;
    info->abi_version = STRATA_PREP_V1_ABI_VERSION;
    info->struct_size = sizeof(StrataPrepV1Info);
#if defined(STRATA_HIP_GFX906) || defined(__HIP__)
    info->backend = 2;  // HIP (the gfx906 compat build)
#else
    info->backend = 1;  // NVIDIA CUDA
#endif
    info->q6k_experts = q6k_experts_enabled() ? 1 : 0;
    info->max_k = STRATA_PREP_V1_MAX_K;
    info->reserved0 = 0;
    info->reserved1 = 0;
    return STRATA_PREP_V1_OK;
}

const char* strata_prep_v1_strerror(int status) {
    switch (status) {
        case STRATA_PREP_V1_OK: return "ok";
        case STRATA_PREP_V1_E_NULL: return "null pointer";
        case STRATA_PREP_V1_E_VERSION: return "abi version";
        case STRATA_PREP_V1_E_SIZE: return "struct size";
        case STRATA_PREP_V1_E_RESERVED: return "reserved field";
        case STRATA_PREP_V1_E_COUNT: return "count";
        case STRATA_PREP_V1_E_OVERFLOW: return "overflow";
        case STRATA_PREP_V1_E_DIM: return "dimension";
        case STRATA_PREP_V1_E_TYPE: return "type outside the bounded v1 set";
        case STRATA_PREP_V1_E_UNSUPPORTED: return "kernel gate refuses the type pair";
        case STRATA_PREP_V1_E_LAYOUT: return "layout field mismatch";
        case STRATA_PREP_V1_E_K: return "k out of 1..15";
        case STRATA_PREP_V1_E_NT: return "nt must be 1 (decode-only)";
        case STRATA_PREP_V1_E_BUSY: return "a request is already outstanding";
        case STRATA_PREP_V1_E_CUDA: return "cuda/hip runtime call failed";
        case STRATA_PREP_V1_E_INTERNAL: return "internal";
        case STRATA_PREP_V1_E_BACKEND: return "backend does not implement this call";
        default: return "unknown status";
    }
}

int strata_prep_v1_supported(StrataPrepV1Supported* q) {
    if (q == nullptr) return STRATA_PREP_V1_E_NULL;
    std::string err;
#ifdef STRATA_PREP_CPU_BUILD  // the CPU-only build has no kernel gate: report it truthfully
    (void) err;
    q->supported = 0;
    return STRATA_PREP_V1_E_BACKEND;
#else
    if (const int rc = prep::validate_supported(*q, err, q6k_experts_enabled())) return rc;
    q->supported = strata::kernels::native_expert_supported(q->gu_type, q->d_type, q->n_embd, q->n_ff) ? 1 : 0;
    q->abi_version = STRATA_PREP_V1_ABI_VERSION;
    q->struct_size = sizeof(StrataPrepV1Supported);
    q->reserved0 = 0;
    q->reserved1 = 0;
    return STRATA_PREP_V1_OK;
#endif
}

int strata_prep_v1_expert(const StrataPrepV1Expert* req) {
    if (req == nullptr) return STRATA_PREP_V1_E_NULL;
    std::string err;
#ifdef STRATA_PREP_CPU_BUILD
    (void) err;
    return STRATA_PREP_V1_E_BACKEND;
#else
    prep::PrepV1Layout layout{};
    if (const int rc = prep::validate_expert(*req, layout, err, q6k_experts_enabled())) return rc;
    // The kernel gate lives here (iq_kernels.cu:4330): a bounded type the kernel has no instances for is a
    // refusal, never an attempt.
    if (!strata::kernels::native_expert_supported(req->gu_type, req->d_type, req->n_embd, req->n_ff))
        return STRATA_PREP_V1_E_UNSUPPORTED;
    std::lock_guard<std::mutex> lk(g_gate);  // one outstanding request, serialized (the ABI's contract)
    // A pinned kernel's contract check throws std::invalid_argument; the extern "C" boundary converts that to
    // a status code, never a terminate (found live in the Phase-3 window: a null stream aborted the process).
    try {
        return stage_and_run(layout, *req, err);
    } catch (...) {
        return STRATA_PREP_V1_E_INTERNAL;
    }
#endif
}

int strata_prep_v1_combine(const StrataPrepV1Combine* req) {
    if (req == nullptr) return STRATA_PREP_V1_E_NULL;
    std::string err;
#ifdef STRATA_PREP_CPU_BUILD
    (void) err;
    return STRATA_PREP_V1_E_BACKEND;
#else
    if (const int rc = prep::validate_combine(*req, err)) return rc;
    std::lock_guard<std::mutex> lk(g_gate);  // one outstanding request, serialized
    // Same extern "C" boundary rule as expert(): a kernel contract throw becomes a status, never a terminate.
    try {
        return run_combine(*req, err);
    } catch (...) {
        return STRATA_PREP_V1_E_INTERNAL;
    }
#endif
}

}  // extern "C"
