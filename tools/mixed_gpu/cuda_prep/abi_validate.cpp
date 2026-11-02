// tools/mixed_gpu/cuda_prep/abi_validate.cpp - the implementation of the host-side ABI v1 guards.
//
// Compiled twice on purpose: as the validation core of the CUDA and HIP wrapper libraries (abi.cpp calls it
// before any cuda*/hip* call), and as the only code the CPU-only guard test (abi_guard_test.cpp) links.
// No CUDA, no HIP, no ggml, no kernels, no dlopen here - by construction.
#include "abi_validate.hpp"

namespace strata::prep {

bool bounded_type_info(int type, int64_t& block_values, size_t& block_bytes) noexcept {
    switch (type) {
        case STRATA_PREP_V1_TYPE_Q4_0: block_values = 32; block_bytes = 18; return true;
        case STRATA_PREP_V1_TYPE_Q4_1: block_values = 32; block_bytes = 20; return true;
        case STRATA_PREP_V1_TYPE_Q5_0: block_values = 32; block_bytes = 22; return true;
        case STRATA_PREP_V1_TYPE_Q5_1: block_values = 32; block_bytes = 24; return true;
        case STRATA_PREP_V1_TYPE_Q8_0: block_values = 32; block_bytes = 34; return true;
        case STRATA_PREP_V1_TYPE_Q4_K: block_values = 256; block_bytes = 144; return true;
        case STRATA_PREP_V1_TYPE_Q5_K: block_values = 256; block_bytes = 176; return true;
        case STRATA_PREP_V1_TYPE_Q6_K: block_values = 256; block_bytes = 210; return true;
        // R1, the native pack (ggml-common.h at the pinned commit, sizeof(block_*), and iq_kernels.cu:4182-4202
        // iq_row_bytes, which divides by 256 for these and by 32 / 64 for IQ4_NL / Q2_0).
        case STRATA_PREP_V1_TYPE_IQ2_XXS: block_values = 256; block_bytes = 66; return true;   // block_iq2_xxs
        case STRATA_PREP_V1_TYPE_IQ2_XS: block_values = 256; block_bytes = 74; return true;    // block_iq2_xs
        case STRATA_PREP_V1_TYPE_IQ3_XXS: block_values = 256; block_bytes = 98; return true;   // block_iq3_xxs
        case STRATA_PREP_V1_TYPE_IQ3_S: block_values = 256; block_bytes = 110; return true;    // block_iq3_s
        case STRATA_PREP_V1_TYPE_IQ2_S: block_values = 256; block_bytes = 82; return true;     // block_iq2_s
        case STRATA_PREP_V1_TYPE_IQ4_NL: block_values = 32; block_bytes = 18; return true;     // block_iq4_nl
        case STRATA_PREP_V1_TYPE_Q2_0: block_values = 64; block_bytes = 18; return true;       // block_q2_0
        default: return false;  // IQ1_M(29), IQ4_XS(23), q3_K(11), tq*(144-146), NVFP4(40), BF16, F32, ...
    }
}

// The role tables.  The grouped kernels are instantiated per role - STRATA_GU_FMTS / STRATA_D_FMTS
// (iq_kernels.cu:767-776) - so a type in the geometry table is NOT permission to use it in a role the kernels
// have no instances for.  These two switches are the bounded v1 role rules (R1: only the native pack's ten
// classes are added; every pre-existing q4/q5/q8/q6 rule is unchanged).
bool bounded_gu_type(int gu_type, bool q6k_experts) noexcept {
    switch (gu_type) {
        case STRATA_PREP_V1_TYPE_Q4_0:
        case STRATA_PREP_V1_TYPE_Q4_1:
        case STRATA_PREP_V1_TYPE_Q5_0:
        case STRATA_PREP_V1_TYPE_Q5_1:
        case STRATA_PREP_V1_TYPE_Q8_0:
        case STRATA_PREP_V1_TYPE_Q4_K:
        case STRATA_PREP_V1_TYPE_Q5_K:
            return true;
        case STRATA_PREP_V1_TYPE_Q6_K: return q6k_experts;  // gate/up only, and only with the Q6K instances
        // R1: the native pack's gate/up i-quants (all five are in STRATA_GU_FMTS, with and without Q6K experts).
        case STRATA_PREP_V1_TYPE_IQ2_XXS:
        case STRATA_PREP_V1_TYPE_IQ2_XS:
        case STRATA_PREP_V1_TYPE_IQ3_XXS:
        case STRATA_PREP_V1_TYPE_IQ3_S:
        case STRATA_PREP_V1_TYPE_IQ2_S:
            return true;
        default: return false;  // IQ4_NL(20)/Q2_0(42) are down-only; IQ1_M/IQ4_XS/q3_K/tq*/NVFP4 are outside v1
    }
}

bool bounded_d_type(int d_type) noexcept {
    switch (d_type) {
        case STRATA_PREP_V1_TYPE_Q4_0:
        case STRATA_PREP_V1_TYPE_Q4_1:
        case STRATA_PREP_V1_TYPE_Q5_0:
        case STRATA_PREP_V1_TYPE_Q5_1:
        case STRATA_PREP_V1_TYPE_Q8_0:
        case STRATA_PREP_V1_TYPE_Q4_K:
        case STRATA_PREP_V1_TYPE_Q5_K:
            return true;
        // R1: the native pack's down types (both are in STRATA_D_FMTS: IQ4_NL 32-value blocks, Q2_0 64-value
        // blocks; the grouped kernel's d_qk() is 32 / 64 for them, iq_kernels.cu:3258-3265).
        case STRATA_PREP_V1_TYPE_IQ4_NL:
        case STRATA_PREP_V1_TYPE_Q2_0:
            return true;
        default: return false;  // v1: q6_K as down is refused (the root parity refuses it too), and so are the
                                // gate/up-only i-quants 16/17/18/21/22 (no down instances for them)
    }
}

namespace {

// Checked n * m; false on overflow.
bool mul_ok(uint64_t n, uint64_t m, uint64_t& out) noexcept {
    if (n != 0 && m > UINT64_MAX / n) return false;
    out = n * m;
    return true;
}

int dim_ok(int64_t n_embd, int64_t n_ff, std::string& err) {
    if (n_embd <= 0 || n_embd > STRATA_PREP_V1_MAX_N_EMBD) {
        err = "n_embd out of range";
        return STRATA_PREP_V1_E_DIM;
    }
    if (n_ff <= 0 || n_ff > STRATA_PREP_V1_MAX_N_FF) {
        err = "n_ff out of range";
        return STRATA_PREP_V1_E_DIM;
    }
    if (n_embd % 32 != 0 || n_ff % 32 != 0) {
        err = "n_embd and n_ff must be multiples of 32 (every bounded type's block is 32 or 256)";
        return STRATA_PREP_V1_E_DIM;
    }
    return STRATA_PREP_V1_OK;
}

}  // namespace

int derive_layout(int gu_type, int d_type, int64_t n_embd, int64_t n_ff, PrepV1Layout& out, std::string& err) {
    int64_t bv_gu = 0, bv_d = 0;
    size_t bb_gu = 0, bb_d = 0;
    if (!bounded_type_info(gu_type, bv_gu, bb_gu)) {
        err = "gu_type outside the bounded v1 set (q4_0/q4_1/q4_K/q5_0/q5_1/q5_K/q8_0, q6_K as gate/up, "
              "IQ2_XXS/IQ2_XS/IQ3_XXS/IQ3_S/IQ2_S as gate/up)";
        return STRATA_PREP_V1_E_TYPE;
    }
    if (!bounded_type_info(d_type, bv_d, bb_d)) {
        err = "d_type outside the bounded v1 set (q4_0/q4_1/q4_K/q5_0/q5_1/q5_K/q8_0, IQ4_NL, Q2_0)";
        return STRATA_PREP_V1_E_TYPE;
    }
    // Roles (design gate C3): the geometry table is not a role permission.  The grouped kernels have no gate/up
    // instances for IQ4_NL/Q2_0 and no down instances for the gate/up i-quants, so a request that puts a type in
    // the role its kernels do not cover is E_TYPE here, not a device attempt.
    if (gu_type == STRATA_PREP_V1_TYPE_IQ4_NL || gu_type == STRATA_PREP_V1_TYPE_Q2_0) {
        err = "gu_type is a down-only type of the native pack (IQ4_NL/Q2_0 have no gate/up kernel instances)";
        return STRATA_PREP_V1_E_TYPE;
    }
    if (d_type == STRATA_PREP_V1_TYPE_IQ2_XXS || d_type == STRATA_PREP_V1_TYPE_IQ2_XS ||
        d_type == STRATA_PREP_V1_TYPE_IQ3_XXS || d_type == STRATA_PREP_V1_TYPE_IQ3_S ||
        d_type == STRATA_PREP_V1_TYPE_IQ2_S) {
        err = "d_type is a gate/up-only i-quant (no down kernel instances for IQ2_XXS/IQ2_XS/IQ3_XXS/IQ3_S/IQ2_S)";
        return STRATA_PREP_V1_E_TYPE;
    }
    if (n_embd % bv_gu != 0) {
        err = "n_embd is not a multiple of the gate/up block size";
        return STRATA_PREP_V1_E_DIM;
    }
    if (n_ff % bv_d != 0) {
        err = "n_ff is not a multiple of the down block size";
        return STRATA_PREP_V1_E_DIM;
    }
    // The native i-quant gate/up classes carry the grouped kernel's own geometry gate (iq_kernels.cu:4330-4335):
    // n_embd whole 256-value blocks, and (n_ff * n_embd) a whole number of 256-value blocks.  Mirroring it here
    // refuses a geometry the kernel would refuse at the host guard, before any device action.
    if (gu_type == STRATA_PREP_V1_TYPE_IQ2_XXS || gu_type == STRATA_PREP_V1_TYPE_IQ2_XS ||
        gu_type == STRATA_PREP_V1_TYPE_IQ3_XXS || gu_type == STRATA_PREP_V1_TYPE_IQ3_S ||
        gu_type == STRATA_PREP_V1_TYPE_IQ2_S) {
        if (n_embd % 256 != 0) {
            err = "n_embd is not a multiple of 256 (the i-quant gate/up blocks the grouped kernel reads)";
            return STRATA_PREP_V1_E_DIM;
        }
        if (((uint64_t) n_ff * (uint64_t) n_embd) % 256ull != 0) {
            err = "n_ff * n_embd is not a whole number of 256-value blocks (native_expert_supported)";
            return STRATA_PREP_V1_E_DIM;
        }
    }
    if (!mul_ok((uint64_t) bb_gu, (uint64_t) n_embd / (uint64_t) bv_gu, (uint64_t&) out.gu_row)) {
        err = "gu_row overflow";
        return STRATA_PREP_V1_E_OVERFLOW;
    }
    if (!mul_ok((uint64_t) bb_d, (uint64_t) n_ff / (uint64_t) bv_d, (uint64_t&) out.d_row)) {
        err = "d_row overflow";
        return STRATA_PREP_V1_E_OVERFLOW;
    }
    uint64_t up, down, tail;
    if (!mul_ok((uint64_t) n_ff, (uint64_t) out.gu_row, up) || !mul_ok(up, 2u, down) ||
        !mul_ok((uint64_t) n_embd, (uint64_t) out.d_row, tail)) {
        err = "blob offset overflow";
        return STRATA_PREP_V1_E_OVERFLOW;
    }
    if (down + tail < down || down + tail > STRATA_PREP_V1_MAX_BLOB_BYTES) {
        err = "blob bytes overflow (max 2 GiB in v1)";
        return STRATA_PREP_V1_E_OVERFLOW;
    }
    out.gu_type = gu_type;
    out.d_type = d_type;
    out.n_embd = n_embd;
    out.n_ff = n_ff;
    out.up_off = (size_t) up;
    out.down_off = (size_t) down;
    out.bytes = (size_t) (down + tail);
    out.tail_off = 0;  // v1: NVFP4 tails are out of the bounded set
    return STRATA_PREP_V1_OK;
}

int validate_expert(const StrataPrepV1Expert& req, PrepV1Layout& layout, std::string& err, bool q6k_experts) {
    if (req.abi_version != STRATA_PREP_V1_ABI_VERSION) {
        err = "expert: abi_version is not STRATA_PREP_V1_ABI_VERSION";
        return STRATA_PREP_V1_E_VERSION;
    }
    if (req.struct_size != sizeof(StrataPrepV1Expert)) {
        err = "expert: struct_size does not match sizeof(StrataPrepV1Expert)";
        return STRATA_PREP_V1_E_SIZE;
    }
    if (req.reserved0 != 0 || req.reserved1 != 0) {
        err = "expert: a reserved field is nonzero";
        return STRATA_PREP_V1_E_RESERVED;
    }
    if (req.nt != 1) {
        err = "expert: v1 is decode-only, nt must be 1";
        return STRATA_PREP_V1_E_NT;
    }
    if (req.tail_off != 0) {
        err = "expert: v1 accepts no NVFP4/Hadamard tail (tail_off must be 0)";
        return STRATA_PREP_V1_E_TYPE;
    }
    if (req.blob == nullptr || req.x == nullptr || req.out == nullptr) {
        err = "expert: blob/x/out must be non-null";
        return STRATA_PREP_V1_E_NULL;
    }
    if (int rc = dim_ok(req.n_embd, req.n_ff, err)) return rc;
    if (!bounded_gu_type(req.gu_type, q6k_experts)) {
        err = "expert: gu_type outside the bounded v1 set";
        return STRATA_PREP_V1_E_TYPE;
    }
    if (!bounded_d_type(req.d_type)) {
        err = "expert: d_type outside the bounded v1 set (q6_K is not a v1 down type)";
        return STRATA_PREP_V1_E_TYPE;
    }
    if (int rc = derive_layout(req.gu_type, req.d_type, req.n_embd, req.n_ff, layout, err)) return rc;
    if (req.blob_bytes != layout.bytes) {
        err = "expert: blob_bytes does not match the derived layout";
        return STRATA_PREP_V1_E_COUNT;
    }
    if (req.gu_row != layout.gu_row || req.d_row != layout.d_row || req.up_off != layout.up_off ||
        req.down_off != layout.down_off) {
        err = "expert: a layout field disagrees with native_expert_layout's derivation";
        return STRATA_PREP_V1_E_LAYOUT;
    }
    return STRATA_PREP_V1_OK;
}

int validate_combine(const StrataPrepV1Combine& req, std::string& err) {
    if (req.abi_version != STRATA_PREP_V1_ABI_VERSION) {
        err = "combine: abi_version is not STRATA_PREP_V1_ABI_VERSION";
        return STRATA_PREP_V1_E_VERSION;
    }
    if (req.struct_size != sizeof(StrataPrepV1Combine)) {
        err = "combine: struct_size does not match sizeof(StrataPrepV1Combine)";
        return STRATA_PREP_V1_E_SIZE;
    }
    if (req.reserved0 != 0 || req.reserved1 != 0) {
        err = "combine: a reserved field is nonzero";
        return STRATA_PREP_V1_E_RESERVED;
    }
    if (req.k < 1 || req.k > STRATA_PREP_V1_MAX_K) {
        err = "combine: k must be 1..15 (the pinned native_moe_combine contract)";
        return STRATA_PREP_V1_E_K;
    }
    if (req.n_embd <= 0 || req.n_embd > STRATA_PREP_V1_MAX_N_EMBD) {
        err = "combine: n_embd out of range";
        return STRATA_PREP_V1_E_DIM;
    }
    if (req.n_embd % 32 != 0) {
        err = "combine: n_embd must be a multiple of 32";
        return STRATA_PREP_V1_E_DIM;
    }
    if (req.has_shared != 0 && req.has_shared != 1) {
        err = "combine: has_shared must be 0 or 1";
        return STRATA_PREP_V1_E_RESERVED;
    }
    if (req.has_shared == 1 && req.shared == nullptr) {
        err = "combine: has_shared is 1 but shared is null";
        return STRATA_PREP_V1_E_NULL;
    }
    if (req.has_shared == 0 && req.shared != nullptr) {
        err = "combine: shared must be null when has_shared is 0";
        return STRATA_PREP_V1_E_NULL;
    }
    if (req.parts == nullptr || req.weights == nullptr || req.out == nullptr) {
        err = "combine: parts/weights/out must be non-null";
        return STRATA_PREP_V1_E_NULL;
    }
    if (req.parts == req.out) {
        err = "combine: parts and out must be disjoint (the kernel requires a nonnull, disjoint output)";
        return STRATA_PREP_V1_E_NULL;
    }
    return STRATA_PREP_V1_OK;
}

int validate_supported(const StrataPrepV1Supported& q, std::string& err, bool q6k_experts) {
    if (q.abi_version != STRATA_PREP_V1_ABI_VERSION) {
        err = "supported: abi_version is not STRATA_PREP_V1_ABI_VERSION";
        return STRATA_PREP_V1_E_VERSION;
    }
    if (q.struct_size != sizeof(StrataPrepV1Supported)) {
        err = "supported: struct_size does not match sizeof(StrataPrepV1Supported)";
        return STRATA_PREP_V1_E_VERSION;
    }
    if (q.reserved0 != 0 || q.reserved1 != 0) {
        err = "supported: a reserved field is nonzero";
        return STRATA_PREP_V1_E_RESERVED;
    }
    if (int rc = dim_ok(q.n_embd, q.n_ff, err)) return rc;
    if (!bounded_gu_type(q.gu_type, q6k_experts)) {
        err = "supported: gu_type outside the bounded v1 set";
        return STRATA_PREP_V1_E_TYPE;
    }
    if (!bounded_d_type(q.d_type)) {
        err = "supported: d_type outside the bounded v1 set";
        return STRATA_PREP_V1_E_TYPE;
    }
    return STRATA_PREP_V1_OK;
}

int validate_info(const StrataPrepV1Info& info, std::string& err) {
    if (info.abi_version != STRATA_PREP_V1_ABI_VERSION) {
        err = "info: abi_version is not STRATA_PREP_V1_ABI_VERSION";
        return STRATA_PREP_V1_E_VERSION;
    }
    if (info.struct_size != sizeof(StrataPrepV1Info)) {
        err = "info: struct_size does not match sizeof(StrataPrepV1Info)";
        return STRATA_PREP_V1_E_SIZE;
    }
    if (info.reserved0 != 0 || info.reserved1 != 0) {
        err = "info: a reserved field is nonzero";
        return STRATA_PREP_V1_E_RESERVED;
    }
    return STRATA_PREP_V1_OK;
}

}  // namespace strata::prep
