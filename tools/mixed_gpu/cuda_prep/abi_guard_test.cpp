// tools/mixed_gpu/cuda_prep/abi_guard_test.cpp - CPU-only ABI/manifest guards (Phase 2 prep).
//
// Run ONLY after the readelf/nm evidence proves this binary has no GPU runtime dependency (it links no CUDA,
// no HIP, no hip_compat, no dlopen; the map's E2/E3 conjunction).  It validates the ABI's host-side surface
// and the fixture manifests; it does not test GPU numerics and cannot test the kernel gate (that gate lives
// in iq_kernels.cu and answers only in a runtime window).
#include "abi.hpp"
#include "abi_validate.hpp"
#include "manifest.hpp"
#include "sha256.hpp"

#include "strata/kernels/cpu/native_expert.hpp"
#include "strata/kernels/cpu/expert_layout.hpp"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <string>
#include <vector>

namespace cpu = strata::kernels::cpu;
namespace prep = strata::prep;
namespace fs = std::filesystem;

namespace {
int g_fail = 0;

void check(bool ok, const std::string& what) {
    std::printf("  %-88s %s\n", what.c_str(), ok ? "ok" : "FAIL");
    if (!ok) ++g_fail;
}

// ---- 1. the ABI host guards: every named refusal, checked BEFORE any runtime action ----
void abi_guards() {
    std::printf("abi guards (CPU-side validation core, no GPU runtime, no dlopen):\n");
    StrataPrepV1Expert r{};
    const int gu = 12, d = 7;  // q4_K / q5_1 at 2560/640 (ggml.h ids)
    prep::PrepV1Layout L;
    std::string err;
    check(prep::derive_layout(gu, d, 2560, 640, L, err) == STRATA_PREP_V1_OK, "derive_layout(q4_K, q5_1, 2560, 640)");
    std::vector<uint8_t> blob(L.bytes, 0x5a);
    std::vector<float> x(2560, 0.f), out(2560, 0.f);
    auto good = [&]() {
        r = StrataPrepV1Expert{};
        r.abi_version = STRATA_PREP_V1_ABI_VERSION;
        r.struct_size = sizeof(StrataPrepV1Expert);
        r.gu_type = gu;
        r.d_type = d;
        r.n_embd = 2560;
        r.n_ff = 640;
        r.nt = 1;
        r.blob_bytes = L.bytes;
        r.gu_row = L.gu_row;
        r.d_row = L.d_row;
        r.up_off = L.up_off;
        r.down_off = L.down_off;
        r.tail_off = 0;
        r.blob = blob.data();
        r.x = x.data();
        r.out = out.data();
    };
    good();
    prep::PrepV1Layout L2;
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_OK, "the good request validates");
    check(L2.bytes == L.bytes, "the derived layout matches the request");

    good();
    r.abi_version = 2;
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_E_VERSION, "wrong abi_version refused");
    good();
    r.struct_size = sizeof(StrataPrepV1Expert) - 4;
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_E_SIZE, "wrong struct_size refused");
    good();
    r.reserved0 = 1;
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_E_RESERVED, "reserved field refused");
    good();
    r.reserved1 = 7;
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_E_RESERVED, "reserved1 refused");
    good();
    r.blob = nullptr;
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_E_NULL, "null blob refused");
    good();
    r.x = nullptr;
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_E_NULL, "null x refused");
    good();
    r.out = nullptr;
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_E_NULL, "null out refused");
    good();
    r.nt = 3;
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_E_NT, "nt != 1 refused (decode-only)");
    good();
    r.blob_bytes = L.bytes - 1;
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_E_COUNT, "wrong blob byte count refused");
    good();
    r.n_embd = 2559;
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_E_DIM, "n_embd not a multiple of 32 refused");
    good();
    r.n_embd = -2560;
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_E_DIM, "negative n_embd refused");
    good();
    r.n_ff = 641;
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_E_DIM, "n_ff not a multiple of 32 refused");
    good();
    r.n_embd = 900000000000ll;
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_E_DIM, "n_embd above the ABI cap refused");
    good();
    r.gu_type = 23;  // IQ4_XS: inside the kernels' gu list, outside the bounded v1 host set (kept refused in R1)
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_E_TYPE, "IQ4_XS gate refused (bounded set)");
    good();
    r.gu_type = 40;  // NVFP4
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_E_TYPE, "NVFP4 refused (bounded set)");
    good();
    r.gu_type = 45;  // tqk6: needs the agention fork ggml (map B6)
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_E_TYPE, "trellis tq type refused (bounded set)");
    good();
    r.d_type = 14;  // q6_K as down
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_E_TYPE, "q6_K as down refused (bounded set)");
    good();
    r.gu_type = 14;  // q6_K as gate/up is inside the set with Q6K
    check(prep::validate_expert(r, L2, err, false) == STRATA_PREP_V1_E_TYPE,
          "q6_K gate refused when q6k_experts=false (kernel instances absent)");
    good();
    r.gu_row = L.gu_row + 16;
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_E_LAYOUT, "a layout field mismatch refused");
    good();
    r.tail_off = L.bytes;
    check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_E_TYPE, "a tail_off (NVFP4) refused in v1");

    // Overflow: 2 GiB cap, and a multiplication overflow path.
    good();
    r.n_embd = 65536;
    r.n_ff = 65536;
    r.blob_bytes = 0;
    check(prep::validate_expert(r, L2, err, true) != STRATA_PREP_V1_OK,
          "an over-cap request (65536 x 65536) refused, not accepted");

    StrataPrepV1Info info{};
    info.abi_version = STRATA_PREP_V1_ABI_VERSION;
    info.struct_size = sizeof(StrataPrepV1Info);
    check(prep::validate_info(info, err) == STRATA_PREP_V1_OK, "info request validates");
    info.abi_version = 0;
    check(prep::validate_info(info, err) == STRATA_PREP_V1_E_VERSION, "info wrong version refused");

    StrataPrepV1Combine c{};
    std::vector<float> parts(2 * 2560, 0.f), weights(2, 0.5f), shared(2560, 0.f);
    auto goodc = [&]() {
        c = StrataPrepV1Combine{};
        c.abi_version = STRATA_PREP_V1_ABI_VERSION;
        c.struct_size = sizeof(StrataPrepV1Combine);
        c.n_embd = 2560;
        c.k = 2;
        c.has_shared = 1;
        c.parts = parts.data();
        c.weights = weights.data();
        c.shared = shared.data();
        c.out = out.data();
    };
    goodc();
    check(prep::validate_combine(c, err) == STRATA_PREP_V1_OK, "the good combine validates");
    goodc();
    c.k = 16;
    check(prep::validate_combine(c, err) == STRATA_PREP_V1_E_K, "k = 16 refused (the combine contract is k <= 15)");
    goodc();
    c.k = 0;
    check(prep::validate_combine(c, err) == STRATA_PREP_V1_E_K, "k = 0 refused");
    goodc();
    c.has_shared = 1;
    c.shared = nullptr;
    check(prep::validate_combine(c, err) == STRATA_PREP_V1_E_NULL, "has_shared=1 with a null shared refused");
    goodc();
    c.out = (float*) c.parts;
    check(prep::validate_combine(c, err) == STRATA_PREP_V1_E_NULL, "parts == out refused (the kernel needs disjoint output)");
    goodc();
    c.abi_version = 99;
    check(prep::validate_combine(c, err) == STRATA_PREP_V1_E_VERSION, "combine wrong version refused");

    StrataPrepV1Supported q{};
    q.abi_version = STRATA_PREP_V1_ABI_VERSION;
    q.struct_size = sizeof(StrataPrepV1Supported);
    q.gu_type = 12;
    q.d_type = 7;
    q.n_embd = 2560;
    q.n_ff = 640;
    check(prep::validate_supported(q, err, true) == STRATA_PREP_V1_OK, "supported request validates (host side)");
    q.gu_type = 11;  // q3_K: in ggml, outside the bounded v1 set
    check(prep::validate_supported(q, err, true) == STRATA_PREP_V1_E_TYPE, "type outside the bounded v1 set refused by the host guard");
    std::printf("  NOTE: native_expert_supported (the kernel gate) lives in iq_kernels.cu: a CPU-only test cannot\n"
                "        run it, and this test does not pretend to.  It is exercised only in a runtime window.\n");
}

// ---- 1b. R1: the native pack's ten gate/up x down classes, accepted, with the role and geometry refusals ----
// The ten classes are the whole production pack (native_experts.txt: 48 layers, gu {16,17,18,21,22} x d {20,42}).
// The expected byte counts are ggml's own row sizes at the pinned commit over the pack's 2560 x 640 geometry,
// cross-checked against cpu::native_fmt below and against the sidecar's declared blob sizes by the exporter.
void native_pair_guards() {
    std::printf("native pack pair guards (R1: the ten real gu x d classes, 2560 x 640):\n");
    struct C {
        int gu, d;
        unsigned long long gu_row, d_row, bytes;
        const char* name;
    };
    const C classes[] = {
        {16, 20, 660, 360, 1766400, "IQ2_XXS/IQ4_NL"}, {16, 42, 660, 180, 1305600, "IQ2_XXS/Q2_0"},
        {17, 20, 740, 360, 1868800, "IQ2_XS/IQ4_NL"},  {17, 42, 740, 180, 1408000, "IQ2_XS/Q2_0"},
        {18, 20, 980, 360, 2176000, "IQ3_XXS/IQ4_NL"}, {18, 42, 980, 180, 1715200, "IQ3_XXS/Q2_0"},
        {21, 20, 1100, 360, 2329600, "IQ3_S/IQ4_NL"},  {21, 42, 1100, 180, 1868800, "IQ3_S/Q2_0"},
        {22, 20, 820, 360, 1971200, "IQ2_S/IQ4_NL"},   {22, 42, 820, 180, 1510400, "IQ2_S/Q2_0"},
    };
    std::vector<float> x(2560, 0.f), out(2560, 0.f);
    int accepted = 0;
    for (const C& c : classes) {
        prep::PrepV1Layout L;
        std::string err;
        const int rc = prep::derive_layout(c.gu, c.d, 2560, 640, L, err);
        check(rc == STRATA_PREP_V1_OK && L.gu_row == c.gu_row && L.d_row == c.d_row && L.bytes == c.bytes &&
                  L.tail_off == 0 && L.up_off == c.gu_row * 640 && L.down_off == 2 * c.gu_row * 640,
              std::string("derive_layout accepts ") + c.name + " with ggml's row sizes (" + std::to_string(c.bytes) +
                  " B)");
        std::vector<uint8_t> blob(L.bytes ? L.bytes : 1, 0xa5);
        StrataPrepV1Expert r{};
        r.abi_version = STRATA_PREP_V1_ABI_VERSION;
        r.struct_size = sizeof(StrataPrepV1Expert);
        r.gu_type = c.gu;
        r.d_type = c.d;
        r.n_embd = 2560;
        r.n_ff = 640;
        r.nt = 1;
        r.blob_bytes = L.bytes;
        r.gu_row = L.gu_row;
        r.d_row = L.d_row;
        r.up_off = L.up_off;
        r.down_off = L.down_off;
        r.tail_off = 0;
        r.blob = blob.data();
        r.x = x.data();
        r.out = out.data();
        prep::PrepV1Layout L2;
        check(prep::validate_expert(r, L2, err, true) == STRATA_PREP_V1_OK,
              std::string(c.name) + ": a full request validates (host guards only, kernel gate NOT run)");
        StrataPrepV1Supported q{};
        q.abi_version = STRATA_PREP_V1_ABI_VERSION;
        q.struct_size = sizeof(StrataPrepV1Supported);
        q.gu_type = c.gu;
        q.d_type = c.d;
        q.n_embd = 2560;
        q.n_ff = 640;
        check(prep::validate_supported(q, err, true) == STRATA_PREP_V1_OK,
              std::string(c.name) + ": supported-query host guards validate");
        ++accepted;
    }
    check(accepted == 10, "all ten real pair classes are in the bounded set");

    // Role refusals: a type the kernels have no instances for in that role stays E_TYPE (no blanket enable).
    prep::PrepV1Layout L;
    std::string err;
    check(prep::derive_layout(20, 20, 2560, 640, L, err) == STRATA_PREP_V1_E_TYPE,
          "IQ4_NL as gate/up refused (no gate/up kernel instances for it)");
    check(prep::derive_layout(42, 20, 2560, 640, L, err) == STRATA_PREP_V1_E_TYPE,
          "Q2_0 as gate/up refused (no gate/up kernel instances for it)");
    check(prep::derive_layout(42, 42, 2560, 640, L, err) == STRATA_PREP_V1_E_TYPE,
          "Q2_0 as gate/up refused in a Q2_0/Q2_0 pair");
    for (int gu_only : {16, 17, 18, 21, 22})
        check(prep::derive_layout(12, gu_only, 2560, 640, L, err) == STRATA_PREP_V1_E_TYPE,
              "gate/up-only i-quant " + std::to_string(gu_only) + " refused as down (no down instances)");
    check(prep::derive_layout(29, 20, 2560, 640, L, err) == STRATA_PREP_V1_E_TYPE,
          "IQ1_M (29) refused as gate/up (kept outside the bounded v1 set)");
    check(prep::derive_layout(12, 29, 2560, 640, L, err) == STRATA_PREP_V1_E_TYPE, "IQ1_M (29) refused as down");
    check(prep::derive_layout(23, 20, 2560, 640, L, err) == STRATA_PREP_V1_E_TYPE, "IQ4_XS (23) refused (outside v1)");
    check(prep::derive_layout(40, 42, 2560, 640, L, err) == STRATA_PREP_V1_E_TYPE, "NVFP4 refused (outside v1)");
    check(prep::derive_layout(12, 40, 2560, 640, L, err) == STRATA_PREP_V1_E_TYPE, "NVFP4 refused as down (outside v1)");
    check(prep::derive_layout(144, 146, 2560, 640, L, err) == STRATA_PREP_V1_E_TYPE, "trellis tq types refused");
    check(prep::derive_layout(11, 20, 2560, 640, L, err) == STRATA_PREP_V1_E_TYPE, "q3_K refused (outside v1)");
    check(!prep::bounded_d_type(14), "q6_K as down refused by the role rule (unchanged v1 rule)");
    check(prep::derive_layout(12, 14, 2560, 640, L, err) == STRATA_PREP_V1_E_DIM,
          "q6_K as down at n_ff 640 refused: 640 is not a whole number of q6_K 256-value blocks");
    check(prep::derive_layout(12, 14, 2560, 512, L, err) == STRATA_PREP_V1_OK,
          "q6_K as down geometry derives at n_ff 512 (whole 256-blocks) - the ROLE rule is what refuses it in v1");
    check(prep::derive_layout(14, 20, 2560, 640, L, err) == STRATA_PREP_V1_OK,
          "q6_K/IQ4_NL geometry derives (the q6_K gate role rule is the q6k_experts flag)");
    check(!prep::bounded_gu_type(14, false), "q6_K gate/up needs the Q6K kernel instances (unchanged v1 rule)");

    // Geometry refusals for the real classes: the grouped kernel's own gate, mirrored (iq_kernels.cu:4330-4335).
    check(prep::derive_layout(18, 20, 2592, 640, L, err) == STRATA_PREP_V1_E_DIM,
          "n_embd 2592 (a multiple of 32, not of 256) refused for an i-quant gate/up");
    check(prep::derive_layout(16, 42, 2560, 608, L, err) == STRATA_PREP_V1_E_DIM,
          "n_ff 608 (not a multiple of Q2_0's 64-value block) refused");
    check(prep::derive_layout(21, 20, 2560, 672, L, err) == STRATA_PREP_V1_OK,
          "n_ff 672 (whole 32-value IQ4_NL blocks) accepted for an i-quant pair");
    check(prep::derive_layout(16, 42, 65536, 65536, L, err) == STRATA_PREP_V1_E_OVERFLOW,
          "an i-quant blob past the 2 GiB cap refused by the byte overflow guard");
}

// ---- 2. the CPU-side layout cross-check: the guard table vs ggml-cpu's own native_fmt ----
void layout_cross_check() {
    std::printf("layout cross-check (prep::derive_layout vs cpu::native_fmt):\n");
    const int pairs[][2] = {{2, 2},   {2, 3},   {12, 7},  {12, 8}, {6, 8},  {13, 8}, {8, 8},  {12, 6},
                            {14, 8}, {16, 20}, {16, 42}, {17, 20}, {17, 42}, {18, 20}, {18, 42}, {21, 20},
                            {21, 42}, {22, 20}, {22, 42}};  // the last ten: R1, every class of the real pack
    for (auto& p : pairs) {
        cpu::NativeFmt f;
        std::string e;
        if (!cpu::native_fmt(p[0], p[1], 2560, 640, f, e)) {
            check(false, "native_fmt(" + std::to_string(p[0]) + "," + std::to_string(p[1]) + ") loads: " + e);
            continue;
        }
        prep::PrepV1Layout L;
        std::string err;
        const int rc = prep::derive_layout(p[0], p[1], 2560, 640, L, err);
        const std::string label = std::to_string(p[0]) + "/" + std::to_string(p[1]);
        check(rc == STRATA_PREP_V1_OK && L.gu_row == f.gu_row && L.d_row == f.d_row && L.up_off == f.up_off &&
                  L.down_off == f.down_off && L.bytes == f.bytes,
              "derive_layout == native_fmt for types " + label);
        // The activation formats ggml-cpu uses for this pair, recorded for evidence: the CUDA path stages the
        // gate/up activation as Q8_1 (10) for every accepted gate/up type; ggml-cpu's own vec_dot_type is what
        // the recorded CPU oracle used, and the two differ by design (the repo's 3e-2 tolerance, not bitwise).
        std::printf("      %s: ggml-cpu gu_act=%d d_act=%d act_bytes=%zu h_bytes=%zu (guard gu_row=%zu d_row=%zu"
                    " blob=%zu)\n",
                    label.c_str(), f.gu_act, f.d_act, f.act_bytes, f.h_bytes, L.gu_row, L.d_row, L.bytes);
    }
}

// ---- 3. manifest round-trip + the negative manifests ----
void manifest_roundtrip(const fs::path& fixtures) {
    std::printf("manifest round-trip (SYNTHETIC fixtures + negative manifests):\n");
    const std::vector<std::string> labels = {"q4_0_q4_0",  "q4_0_q4_1", "q4_K_q5_1", "q4_K_q8_0", "q5_0_q8_0",
                                             "q5_K_q8_0",  "q8_0_q8_0", "q4_K_q5_0", "q6_K_q8_0",
                                             // R1: the ten native pair classes in synthetic form
                                             "iq2_xxs_iq4_nl", "iq2_xxs_q2_0", "iq2_xs_iq4_nl", "iq2_xs_q2_0",
                                             "iq3_xxs_iq4_nl", "iq3_xxs_q2_0", "iq3_s_iq4_nl",  "iq3_s_q2_0",
                                             "iq2_s_iq4_nl",   "iq2_s_q2_0"};
    for (const std::string& label : labels) {
        const fs::path base = fixtures / label;
        prep::Manifest m;
        std::string err;
        if (!prep::manifest_parse((base / "manifest.txt").string(), m, err)) {
            check(false, label + ": manifest parses: " + err);
            continue;
        }
        check(m.label == label && m.synthetic == 1, label + ": manifest header (label + SYNTHETIC)");
        bool hashes = true;
        for (const std::string& file : prep::manifest_files()) {
            std::string hex;
            if (!prep::sha256_file((base / file).string(), hex) || hex != m.sha256[file]) hashes = false;
        }
        check(hashes, label + ": every recorded file's SHA-256 matches the manifest");
        prep::PrepV1Layout L;
        std::string lerr;
        check(prep::derive_layout(m.gu_type, m.d_type, m.n_embd, m.n_ff, L, lerr) == STRATA_PREP_V1_OK &&
                  (unsigned long long) L.bytes == m.blob_bytes,
              label + ": manifest geometry derives from the types/dimensions");
        // The declared byte counts must equal the recorded file sizes exactly.
        const auto blob_sz = (unsigned long long) fs::file_size(base / "blob.bin");
        const auto x_sz = (unsigned long long) fs::file_size(base / "x.bin");
        const auto ref_sz = (unsigned long long) fs::file_size(base / "ref_f32.bin");
        check(blob_sz == m.blob_bytes && x_sz == (unsigned long long) m.n_embd * 4 &&
                  ref_sz == (unsigned long long) m.n_embd * 4,
              label + ": exact recorded byte counts (blob / x / reference)");
        const auto parts_sz = (unsigned long long) fs::file_size(base / "combine_parts.bin");
        const auto weights_sz = (unsigned long long) fs::file_size(base / "combine_weights.bin");
        check(parts_sz == (unsigned long long) m.k * m.n_embd * 4 && weights_sz == (unsigned long long) m.k * 4,
              label + ": combine parts/weights element counts");
    }

    const std::vector<std::pair<std::string, std::string>> neg = {
        {"bad_version.txt", "version"},
        {"missing_keys.txt", "missing"},
        {"bad_type.txt", "outside"},
        {"overflow.txt", "overflow"},
        {"bad_count.txt", "layout"},
        {"bad_k.txt", "k"},
        {"bad_nt.txt", "nt"},
        // R1: the newly accepted types must NOT make these refuse less - the role and set rules stay strict.
        {"bad_iq1m_down.txt", "outside"},
        {"bad_iq_gu_as_down.txt", "outside"},
        {"bad_iq4nl_as_gu.txt", "outside"},
        {"bad_iq_geom.txt", "dimension"},
    };
    for (auto& [file, why] : neg) {
        prep::Manifest m;
        std::string err;
        const bool ok = prep::manifest_parse((fixtures / file).string(), m, err);
        check(!ok && err.find(why) != std::string::npos, "negative manifest " + file + " refused (" + why + ")");
    }
}

// ---- 3b. the REAL fixtures: manifest parse, provenance, byte counts, class/layer coverage ----
// The R1 evidence: real model bytes (synthetic=0), one fixture per production layer, all ten pair classes.
// This checks the manifest and the recorded files only - no check is relaxed, nothing is executed on a GPU.
void real_manifest_roundtrip(const fs::path& dir, bool require_coverage) {
    std::printf("manifest round-trip (REAL fixtures, synthetic=0):\n");
    std::error_code ec;
    if (!fs::is_directory(dir, ec)) {
        check(false, "real fixtures dir present: " + dir.string());
        return;
    }
    std::vector<std::string> labels;
    for (const auto& de : fs::directory_iterator(dir, ec))
        if (de.is_directory() && fs::exists(de.path() / "manifest.txt", ec)) labels.push_back(de.path().filename().string());
    std::sort(labels.begin(), labels.end());
    std::map<std::string, int> by_class;
    std::map<int, int> by_layer;
    unsigned long long min_blob = ~0ull, max_blob = 0;
    int bad = 0, hashes_bad = 0;
    for (const std::string& label : labels) {
        const fs::path base = dir / label;
        prep::Manifest m;
        std::string err;
        if (!prep::manifest_parse((base / "manifest.txt").string(), m, err)) {
            std::printf("  %-32s manifest REFUSED: %s\n", label.c_str(), err.c_str());
            ++bad;
            continue;
        }
        bool ok = (m.synthetic == 0) && label.rfind("real_", 0) == 0;
        ok = ok && label.find("_L") != std::string::npos;  // the label names its source layer
        bool hashes = true;
        for (const std::string& file : prep::manifest_files()) {
            std::string hex;
            if (!prep::sha256_file((base / file).string(), hex) || hex != m.sha256[file]) hashes = false;
        }
        const auto blob_sz = (unsigned long long) fs::file_size(base / "blob.bin", ec);
        const auto x_sz = (unsigned long long) fs::file_size(base / "x.bin", ec);
        const auto out_sz = (unsigned long long) fs::file_size(base / "cpu_out.bin", ec);
        ok = ok && blob_sz == m.blob_bytes && x_sz == (unsigned long long) m.n_embd * 4 &&
             out_sz == (unsigned long long) m.n_embd * 4 && hashes;
        // The verdict file the exporter wrote from the checker's own gate, as recorded.
        std::ifstream v(base / "v1-verdict.txt", std::ios::binary);
        ok = ok && (bool) v;
        if (!ok) ++bad;
        if (!hashes) ++hashes_bad;
        int src_layer = -1;
        {
            std::ifstream mf(base / "manifest.txt", std::ios::binary);
            std::string line;
            while (std::getline(mf, line)) {
                if (line.rfind("source_layer=", 0) == 0) src_layer = std::atoi(line.c_str() + 13);
            }
        }
        by_class[std::to_string(m.gu_type) + "/" + std::to_string(m.d_type)]++;
        by_layer[src_layer]++;
        if (m.blob_bytes < min_blob) min_blob = m.blob_bytes;
        if (m.blob_bytes > max_blob) max_blob = m.blob_bytes;
        std::printf("  %-32s gu=%2d d=%2d blob=%-9llu layer=%d sha256 all-match=%s\n", label.c_str(), m.gu_type,
                    m.d_type, (unsigned long long) m.blob_bytes, src_layer, hashes ? "yes" : "NO");
    }
    check(bad == 0, "every real fixture parses, is labelled real_/synthetic=0, and matches its recorded SHA-256" +
                        std::string(bad ? " (" + std::to_string(bad) + " problem(s), " + std::to_string(hashes_bad) +
                                          " hash mismatch(es))"
                                         : ""));
    std::printf("  coverage: %zu fixture(s), %zu pair class(es), %zu layer(s), blob bytes %llu..%llu\n",
                labels.size(), by_class.size(), by_layer.size(), min_blob, max_blob);
    for (const auto& [k, n] : by_class) std::printf("    class %-8s %2d fixture(s)\n", k.c_str(), n);
    if (require_coverage) {
        check(labels.size() >= 48, "one fixture for every one of the 48 production layers");
        check(by_class.size() >= 10, "all ten real pair classes are covered");
        check(by_layer.size() >= 48, "every layer id 0..47 appears in the coverage set");
        check(min_blob == 1305600ull && max_blob == 2329600ull,
              "the real blob byte range is the pack's own 1305600..2329600 (measured, not estimated)");
    }
}

// ---- 4. the real packs' native_experts.txt parse through the existing CPU parser ----
void parser_guard(const fs::path& data) {
    std::printf("native_experts.txt parser guard (existing CPU parser, real pack copies):\n");
    for (const char* pack : {"q2_0", "iq2_xs", "iq3_xxs", "iq3_s", "coder-iq1_m", "swift-iq2_xs"}) {
        fs::path dir = fs::temp_directory_path() / ("strata-prep-parser-" + std::string(pack));
        std::error_code ec;
        fs::remove_all(dir, ec);
        fs::create_directories(dir, ec);
        fs::copy_file(data / (std::string(pack) + ".txt"), dir / "native_experts.txt",
                      fs::copy_options::overwrite_existing, ec);
        std::string err;
        const bool ok = cpu::expert_layout_load(dir.string(), 48, 512, err);
        check(ok, std::string(pack) + ": parses" + (ok ? "" : ": " + err));
        fs::remove_all(dir, ec);
    }
    // A header version above kExpertLayoutVersion is a refusal (a newer packer wrote it).
    fs::path dir = fs::temp_directory_path() / "strata-prep-parser-vnew";
    std::error_code ec;
    fs::remove_all(dir, ec);
    fs::create_directories(dir, ec);
    {
        std::ofstream o(dir / "native_experts.txt");
        o << "# strata native experts v99: layer gu_type d_type offset blob_bytes gate_off up_off down_off\n"
          << "0 12 7 0 1478400 0 0 0\n";
    }
    std::string err;
    check(!cpu::expert_layout_load(dir.string(), 48, 512, err), "a header version above the read limit is refused");
    fs::remove_all(dir, ec);
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        std::printf(
            "usage: abi_guard_test <native_experts-data-dir> [fixtures-dir] [real-fixtures-dir]\n"
            "  CPU-only.  Runs the ABI host guards (incl. R1's ten native pair classes), the ggml-cpu layout\n"
            "  cross-check, the synthetic manifest round-trip, the REAL-fixture coverage pass (when a real\n"
            "  fixtures dir is given) and the native_experts.txt parser guards.\n");
        return 2;
    }
    // The manifest hashes only mean something if the hash is FIPS-correct: the published vectors run first,
    // as a checked guard (the first sha256.cpp had a mistyped K table - see the history note in sha256.cpp).
    {
        std::string srep;
        const bool sh_ok = prep::sha256_selftest(srep);
        check(sh_ok, "sha256 selftest: 15 published vectors, whole-buffer and streamed updates");
        if (!sh_ok) std::fputs(srep.c_str(), stderr);
    }
    abi_guards();
    native_pair_guards();
    layout_cross_check();
    if (argc >= 3) {
        manifest_roundtrip(fs::path(argv[2]));
    } else {
        std::printf("manifest round-trip: NOT RUN (no fixtures dir given)\n");
    }
    if (argc >= 4) {
        real_manifest_roundtrip(fs::path(argv[3]), true);
    } else {
        std::printf("real-fixture coverage: NOT RUN (no real fixtures dir given)\n");
    }
    parser_guard(fs::path(argv[1]));
    std::printf("%s: %d failure(s)\n", g_fail ? "abi_guard_test FAIL" : "abi_guard_test ok", g_fail);
    return g_fail ? 1 : 0;
}
