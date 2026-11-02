// tools/mixed_gpu/cuda_prep/abi.hpp - the PREPARATION ABI v1: host-buffer-only, plain C, versioned.
//
// This is NOT an engine API and NOT a production contract.  It is the Phase-2 verification surface: a decoded
// expert's rows move host->host through the shared library, one token, one outstanding request.  The wrapper in
// abi.cpp stages the host buffers on the device, runs the UNMODIFIED kernels from src/kernels/cuda/
// (quantize_q8_1_rows, native_expert_grouped, native_moe_combine) and stages the result back.  Nothing here is
// integrated with the engine, the server or any default path.
//
// Contract (v1, frozen for the preparation phase):
//   * Plain C, stdint/POD only; extern "C"; no C++ types cross the boundary; no exceptions cross the boundary.
//   * Every struct carries abi_version and struct_size; the callee checks both, plus every reserved_* == 0.
//   * Every buffer carries an explicit byte/element count; the callee checks the count exactly, before touching it.
//   * Status convention: 0 (STRATA_PREP_V1_OK) success; every nonzero is a named failure; strata_prep_v1_strerror
//     maps any int to text.  Negative codes are reserved for future use, never returned by v1.
//   * Decode only: exactly ONE input token per call (nt == 1), one serialized request (the library holds a
//     mutex; a concurrent call is refused, not queued).  Expert outputs are UNWEIGHTED.
//   * Routing weights are applied exactly once, in strata_prep_v1_combine only, k <= 15, by the pinned
//     native_moe_combine contract (first product rounds to F32, following products FMA in expert order,
//     shared added once and NOT router-weighted).
//   * Bounded formats v1 (R1: the native IQ pack): q4_0, q4_1, q4_K, q5_0, q5_1, q5_K, q8_0, and q6_K for gate/up
//     when the library was built with STRATA_Q6K_EXPERTS=1 (the root default); plus the ten gate/up x down classes
//     of the native production pack (Swift-1.5 IQ3_XXS, 48 layers): gate/up IQ2_XXS(16), IQ2_XS(17), IQ3_XXS(18),
//     IQ3_S(21), IQ2_S(22) with down IQ4_NL(20) or Q2_0(42).
//     Roles are separate lists, because the kernels are per-role (STRATA_GU_FMTS / STRATA_D_FMTS,
//     iq_kernels.cu:767-776): an IQ gate/up i-quant is refused as a down type, and IQ4_NL/Q2_0 are refused as a
//     gate/up type.  Everything else stays rejected with STRATA_PREP_V1_E_TYPE: IQ1_M (29), IQ4_XS (23), q3_K (11),
//     the trellis tq*, Hadamard-folded v5 layouts, NVFP4 (type 40).  q4/q5/q8/q6 role rules are unchanged.
//     This is a bounded set, not a claim that other formats work.
//   * The exports are hidden-visibility with an explicit version script: only strata_prep_v1_* is exported.
//     Engine/kernel/C++ symbols stay local; no CUDA symbol is exported from the HIP library and none of
//     HIP's from the CUDA library; neither library exports anything the other interposes on.
#ifndef STRATA_MIXED_GPU_CUDA_PREP_ABI_HPP
#define STRATA_MIXED_GPU_CUDA_PREP_ABI_HPP

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// The build compiles with -fvisibility=hidden; only these symbols are exported (the version script repeats the
// same list for the linker).  An engine C++ symbol, a cuda*/hip* symbol, or a kernel symbol never leaves a
// strata_prep_*.so.
#if defined(__GNUC__) && !defined(_WIN32)
#define STRATA_PREP_EXPORT __attribute__((visibility("default")))
#else
#define STRATA_PREP_EXPORT
#endif

#define STRATA_PREP_V1_ABI_VERSION 1u
#define STRATA_PREP_V1_MAX_K 15
#define STRATA_PREP_V1_MAX_N_EMBD 65536
#define STRATA_PREP_V1_MAX_N_FF 65536
#define STRATA_PREP_V1_MAX_BLOB_BYTES (1ull << 31)

// ggml type ids the v1 bounded set accepts (include/ggml.h numbering, stable across ggml).
#define STRATA_PREP_V1_TYPE_Q4_0 2
#define STRATA_PREP_V1_TYPE_Q4_1 3
#define STRATA_PREP_V1_TYPE_Q5_0 6
#define STRATA_PREP_V1_TYPE_Q5_1 7
#define STRATA_PREP_V1_TYPE_Q8_0 8
#define STRATA_PREP_V1_TYPE_Q6_K 14
#define STRATA_PREP_V1_TYPE_Q4_K 12
#define STRATA_PREP_V1_TYPE_Q5_K 13
// The native production pack's types (R1).  ggml.h ids: IQ2_XXS 16, IQ2_XS 17, IQ3_XXS 18, IQ4_NL 20, IQ3_S 21,
// IQ2_S 22, Q2_0 42.  Named with their role: 16/17/18/21/22 are accepted as gate/up only; 20/42 as down only.
#define STRATA_PREP_V1_TYPE_IQ2_XXS 16
#define STRATA_PREP_V1_TYPE_IQ2_XS 17
#define STRATA_PREP_V1_TYPE_IQ3_XXS 18
#define STRATA_PREP_V1_TYPE_IQ4_NL 20
#define STRATA_PREP_V1_TYPE_IQ3_S 21
#define STRATA_PREP_V1_TYPE_IQ2_S 22
#define STRATA_PREP_V1_TYPE_Q2_0 42
// Named so their refusal is explicit and never silently mapped onto an accepted type: IQ1_M (29) and IQ4_XS (23)
// are outside the bounded set in v1, in either role.
#define STRATA_PREP_V1_TYPE_IQ1_M 29
#define STRATA_PREP_V1_TYPE_IQ4_XS 23
#define STRATA_PREP_V1_TYPE_NVFP4 40  // named so the refusal is explicit, not silently mapped

// Status codes: 0 ok, 1..N named failures, <0 reserved.  Every failure is checked before any allocation, copy,
// launch, synchronization, library call or free happens.
enum StrataPrepV1Status {
    STRATA_PREP_V1_OK = 0,
    STRATA_PREP_V1_E_NULL = 1,              // a required pointer is null
    STRATA_PREP_V1_E_VERSION = 2,           // abi_version != STRATA_PREP_V1_ABI_VERSION
    STRATA_PREP_V1_E_SIZE = 3,              // struct_size != sizeof(the struct the callee knows)
    STRATA_PREP_V1_E_RESERVED = 4,          // a reserved_* field is nonzero
    STRATA_PREP_V1_E_COUNT = 5,             // an element/byte count does not match the documented contract
    STRATA_PREP_V1_E_OVERFLOW = 6,          // a count/offset arithmetic overflow
    STRATA_PREP_V1_E_DIM = 7,               // n_embd / n_ff out of range or not a multiple of 32
    STRATA_PREP_V1_E_TYPE = 8,              // a ggml type outside the bounded v1 set
    STRATA_PREP_V1_E_UNSUPPORTED = 9,       // inside the bounded set, the kernel gate refuses the pair
    STRATA_PREP_V1_E_LAYOUT = 10,           // a supplied layout field disagrees with native_expert_layout
    STRATA_PREP_V1_E_K = 11,                // k out of [1, STRATA_PREP_V1_MAX_K]
    STRATA_PREP_V1_E_NT = 12,               // nt != 1 (decode-only: exactly one token)
    STRATA_PREP_V1_E_BUSY = 13,             // a request is already outstanding (one-at-a-time, mutex-held)
    STRATA_PREP_V1_E_CUDA = 14,             // a CUDA/HIP runtime call failed (driver reports cudaError_t text)
    STRATA_PREP_V1_E_INTERNAL = 15,         // any other failure, reported truthfully
    STRATA_PREP_V1_E_BACKEND = 16           // the library's backend id does not match what was asked
};

// The library's self-description.  Filled by the callee; the caller pre-fills abi_version and struct_size.
struct StrataPrepV1Info {
    uint32_t abi_version;  // in: STRATA_PREP_V1_ABI_VERSION; out: the library's version
    uint32_t struct_size;  // in: sizeof(StrataPrepV1Info); out: unchanged on success
    int32_t backend;       // out: 0 = CPU (no GPU path), 1 = CUDA, 2 = HIP
    int32_t q6k_experts;   // out: 1 when the library was built with STRATA_Q6K_EXPERTS=1
    int32_t max_k;         // out: STRATA_PREP_V1_MAX_K
    int32_t reserved0;     // in: 0; out: 0
    uint64_t reserved1;    // in: 0ull; out: 0ull
};

// One decode call: exactly one token x (n_embd floats) through ONE expert blob; the expert's raw rows land in
// out (n_embd floats, UNWEIGHTED).  The layout fields describe the blob [gate rows | up rows | down rows] and
// are checked against native_expert_layout(gu_type, d_type, n_embd, n_ff); the callee never trusts them.
struct StrataPrepV1Expert {
    uint32_t abi_version;   // in: STRATA_PREP_V1_ABI_VERSION
    uint32_t struct_size;   // in: sizeof(StrataPrepV1Expert)
    int32_t gu_type;        // gate/up ggml type (bounded set; q6_K only with Q6K experts; IQ2_XXS/IQ2_XS/IQ3_XXS/
                            // IQ3_S/IQ2_S accepted as gate/up; IQ4_NL and Q2_0 are refused as gate/up)
    int32_t d_type;         // down ggml type (bounded set; q6_K is not accepted as down in v1; IQ4_NL/Q2_0 are
                            // accepted as down; the gate/up i-quants are refused as down)
    int64_t n_embd;         // 32 | n_embd, 0 < n_embd <= STRATA_PREP_V1_MAX_N_EMBD
    int64_t n_ff;           // 32 | n_ff, 0 < n_ff <= STRATA_PREP_V1_MAX_N_FF
    int32_t nt;             // in: must be 1; every other value is E_NT
    int32_t reserved0;      // in: 0
    size_t blob_bytes;      // exact: 2*gu_row*n_ff + d_row*n_embd (q6_K gu_row, IQ row sizes), 0 tail
    size_t gu_row;          // bytes per gate row (also per up row); from native_expert_layout
    size_t d_row;           // bytes per down row
    size_t up_off;          // byte offset of the up block inside the blob
    size_t down_off;        // byte offset of the down block
    size_t tail_off;        // v1: must be 0 (no NVFP4 tail, no Hadamard)
    const uint8_t* blob;    // in: host, blob_bytes bytes, [gate | up | down]
    const float* x;         // in: host, n_embd floats (nt == 1)
    float* out;             // out: host, n_embd floats, raw expert rows (UNWEIGHTED)
    uint64_t reserved1;     // in: 0ull
};

// The exactly-once weighted reduction: parts (k rows of n_embd, e.g. from k experts), weights (k floats),
// optional shared (n_embd, added once, NOT router-weighted), out (n_embd).  k in [1, 15].  The reduction is the
// pinned native_moe_combine kernel: first product rounds to F32, following products FMA in expert order.
struct StrataPrepV1Combine {
    uint32_t abi_version;  // in: STRATA_PREP_V1_ABI_VERSION
    uint32_t struct_size;  // in: sizeof(StrataPrepV1Combine)
    int64_t n_embd;        // 32 | n_embd, range as above
    int32_t k;             // in: 1..STRATA_PREP_V1_MAX_K
    int32_t has_shared;    // in: 0 = shared null and unused, 1 = shared nonnull
    int32_t reserved0;     // in: 0
    const float* parts;    // in: host, k * n_embd floats (row r = expert r's UNWEIGHTED rows)
    const float* weights;  // in: host, k floats
    const float* shared;   // in: host, n_embd floats when has_shared, else nullptr
    float* out;            // out: host, n_embd floats
    uint64_t reserved1;    // in: 0ull
};

// The type-support query: v1's gate is native_expert_supported, which lives in the GPU translation unit.
// CPU-side code cannot test the GPU kernel gate; on a CPU build this call reports E_BACKEND truthfully.
struct StrataPrepV1Supported {
    uint32_t abi_version;  // in: STRATA_PREP_V1_ABI_VERSION
    uint32_t struct_size;  // in: sizeof(StrataPrepV1Supported)
    int32_t gu_type;       // in: bounded set, else E_TYPE
    int32_t d_type;        // in: bounded set, else E_TYPE
    int64_t n_embd;        // in: 32 | n_embd, range as above
    int64_t n_ff;          // in: 32 | n_ff, range as above
    int32_t supported;     // out: 1 the pair runs on this library's kernels, 0 it does not (not a failure)
    int32_t reserved0;     // in: 0; out: 0
    uint64_t reserved1;    // in: 0ull; out: 0ull
};

// Exported surface (the version script exports EXACTLY these, nothing else):
STRATA_PREP_EXPORT uint32_t strata_prep_v1_abi_version(void);
STRATA_PREP_EXPORT int strata_prep_v1_get_info(StrataPrepV1Info* info);
STRATA_PREP_EXPORT const char* strata_prep_v1_strerror(int status);
STRATA_PREP_EXPORT int strata_prep_v1_supported(StrataPrepV1Supported* q);
STRATA_PREP_EXPORT int strata_prep_v1_expert(const StrataPrepV1Expert* req);
STRATA_PREP_EXPORT int strata_prep_v1_combine(const StrataPrepV1Combine* req);

#ifdef __cplusplus
}  // extern "C"
#endif

#endif  // STRATA_MIXED_GPU_CUDA_PREP_ABI_HPP
