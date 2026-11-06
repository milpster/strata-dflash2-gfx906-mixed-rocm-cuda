// tools/mixed_gpu/cuda_prep/abi_validate.hpp - the host-side validation core of the PREPARATION ABI v1.
//
// This is the shared CPU-testable half of the ABI's guards: version, struct size, reserved fields, nulls,
// counts, overflow, dimensions, the bounded type set, k and nt.  It is plain host code with no CUDA, no HIP,
// no ggml, no kernel calls, so it compiles and runs in a CPU-only translation unit (abi_guard_test.cpp) AND is
// the same code the GPU wrapper (abi.cpp) runs before any allocation, copy, launch, synchronization or free.
//
// The GPU kernel gate (native_expert_supported, iq_kernels.cu:4330) lives in the GPU translation unit: a CPU
// build reports STRATA_PREP_V1_E_BACKEND for strata_prep_v1_supported, as the ABI documents.  Inventing a
// CPU-side copy of that gate is forbidden here.
//
// Row sizes for the bounded set are ggml's own (ggml-common.h block structs at the pinned commit: the blocks
// of q4_0/q4_1/q5_0/q5_1/q8_0 hold 32 values in 18/20/22/24/34 bytes, of q4_K/q5_K/q6_K 256 values in
// 144/176/210, and - R1, the native pack - of iq2_xxs/iq2_xs/iq3_xxs/iq3_s/iq2_s 256 values in 66/74/98/110/82
// bytes, of iq4_nl 32 values in 18, of q2_0 64 values in 18).  They are replicated as a table so the guards are
// testable without ggml; a disagreement with ggml's ggml_row_size is a bug in this table, which the fixture
// exporter cross-checks.  The table is geometry only: the two role lists below decide which type may be gate/up
// and which may be down, because the GPU kernels have instances per role.
#ifndef STRATA_MIXED_GPU_CUDA_PREP_ABI_VALIDATE_HPP
#define STRATA_MIXED_GPU_CUDA_PREP_ABI_VALIDATE_HPP

#include "abi.hpp"

#include <cstddef>
#include <cstdint>
#include <string>

namespace strata::prep {

// The derived, checked geometry of one expert blob (v1: no tail, no Hadamard).
struct PrepV1Layout {
    int gu_type = -1, d_type = -1;
    int64_t n_embd = 0, n_ff = 0;
    size_t gu_row = 0, d_row = 0;
    size_t up_off = 0, down_off = 0;
    size_t bytes = 0;
    size_t tail_off = 0;  // v1: always 0
};

// ggml block geometry for one bounded type; false for any type outside the v1 set.
bool bounded_type_info(int type, int64_t& block_values, size_t& block_bytes) noexcept;

// True for gu_type accepted in v1 (q6_K accepted as gate/up; STRATA_Q6K_EXPERTS decides the kernel instances).
bool bounded_gu_type(int gu_type, bool q6k_experts) noexcept;
// True for d_type accepted in v1.  q6_K is NOT accepted as down in v1 (the root parity refuses it too).
bool bounded_d_type(int d_type) noexcept;

// Derive the blob geometry with checked arithmetic.  Returns 0 (OK) or an ABI status.
int derive_layout(int gu_type, int d_type, int64_t n_embd, int64_t n_ff, PrepV1Layout& out, std::string& err);

// Validate an expert request BEFORE any device action: version, size, reserved, nulls, nt, dimensions, the
// bounded type set, the derived layout, tail_off == 0, exact blob byte count.  Writes the derived layout when
// it returns OK.  No allocation, copy, launch, sync, kernel call, library load or free happens in this call.
int validate_expert(const StrataPrepV1Expert& req, PrepV1Layout& layout, std::string& err, bool q6k_experts);

// Validate a combine request BEFORE any device action: version, size, reserved, nulls, k, dimensions,
// exact element counts (parts == k * n_embd, weights == k, shared/output == n_embd), the shared flag.
int validate_combine(const StrataPrepV1Combine& req, std::string& err);

// Validate the fields of a supported-query BEFORE any device action (the kernel gate itself is NOT here).
int validate_supported(const StrataPrepV1Supported& q, std::string& err, bool q6k_experts);

// Validate an info request's inbound fields.
int validate_info(const StrataPrepV1Info& info, std::string& err);

}  // namespace strata::prep

#endif  // STRATA_MIXED_GPU_CUDA_PREP_ABI_VALIDATE_HPP
