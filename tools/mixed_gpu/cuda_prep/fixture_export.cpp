// tools/mixed_gpu/cuda_prep/fixture_export.cpp - CPU-only synthetic fixture + oracle exporter (Phase 2 prep).
//
// WHAT THIS IS: labelled SYNTHETIC fixtures - random weights quantized by ggml into the native expert blob
// layout, with a recorded float reference and a recorded ggml-cpu CPU oracle.  They are NOT real model blobs,
// NOT a real-blob parity test, and no claim about production packs is made by them.  Real blobs come from
// tools/iq_pack.py packs and are exercised only in a later, explicitly authorized runtime window.
//
// SOURCE REUSE (the map's instruction, not new math):
//   * the synthetic_blob recipe and the per-row scale pattern are from src/kernels/native_expert_parity.cpp:496-519
//     (the existing parity tool), copied verbatim in the quant lambda below and attributed there;
//   * the float reference is the same parity tool's (a) path: ggml's own to_float dequantizer plus the FP64
//     SwiGLU accumulation (native_expert_parity.cpp:69-105);
//   * the CPU oracle is strata_kernels_cpu's own ggml-cpu decode (native_quant_act / native_gu_rows /
//     native_down_rows, include/strata/kernels/cpu/native_expert.hpp) - exactly the semantics llama.cpp's CPU
//     backend computes (its header note), no new quant math, no new kernels.
//
// CPU-only by construction: ggml (static, GGML_CUDA/HIP/METAL OFF in CMake) + strata_kernels_cpu.  No CUDA,
// no HIP, no hip_compat, no dlopen, no GPU runtime - proven by the readelf/nm evidence recorded next to the
// build, which must be checked before this program is run.
#include "abi.hpp"
#include "abi_validate.hpp"
#include "sha256.hpp"

#include "strata/kernels/cpu/native_expert.hpp"

#include "ggml.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <random>
#include <string>
#include <vector>

namespace cpu = strata::kernels::cpu;
namespace prep = strata::prep;
namespace fs = std::filesystem;

#ifndef STRATA_PREP_GGML_NOTE
#define STRATA_PREP_GGML_NOTE "unset"
#endif

namespace {

constexpr int64_t H = 2560, FF = 640;  // the native experts' fixed dimensions (native_expert_parity.cpp:62)

// The recipe from native_expert_parity.cpp:496-519, verbatim (attribute): rows of different magnitudes
// quantized by ggml_quantize_chunk into [gate rows | up rows | down rows].
std::vector<uint8_t> synthetic_blob(const cpu::NativeFmt& f, int seed) {
    std::vector<uint8_t> blob(f.bytes);
    std::mt19937 rng(seed);
    std::normal_distribution<float> nd(0.f, 1.f);
    auto quant = [&](int type, int64_t rows, int64_t cols, uint8_t* dst) {
        std::vector<float> w((size_t) (rows * cols));
        for (int64_t r = 0; r < rows; ++r) {
            const float scale = 0.02f * (0.5f + (float) (r % 7) / 7.0f);
            for (int64_t c = 0; c < cols; ++c) w[(size_t) (r * cols + c)] = scale * nd(rng);
        }
        const std::vector<float> imatrix((size_t) cols, 1.0f);
        ggml_quantize_chunk((ggml_type) type, w.data(), dst, 0, rows, cols,
                            ggml_quantize_requires_imatrix((ggml_type) type) ? imatrix.data() : nullptr);
    };
    quant(f.gu_type, FF, H, blob.data());
    quant(f.gu_type, FF, H, blob.data() + f.up_off);
    quant(f.d_type, H, FF, blob.data() + f.down_off);
    if (f.tail_off) {
        const bool g4 = f.gu_type == 40, d4 = f.d_type == 40;
        const float tail[4] = {g4 ? 0.75f : 1.f, g4 ? 1.25f : 1.f, d4 ? 0.5f : 1.f, 0.f};
        std::memcpy(blob.data() + f.tail_off, tail, sizeof tail);
    }
    return blob;
}

// The float reference (the parity tool's (a) path, native_expert_parity.cpp:69-105), one token.
void float_reference(const cpu::NativeFmt& f, const std::vector<uint8_t>& blob, const float* x, float* ref) {
    const auto* tg = ggml_get_type_traits((ggml_type) f.gu_type);
    const auto* td = ggml_get_type_traits((ggml_type) f.d_type);
    std::vector<float> G((size_t) FF * H), U((size_t) FF * H), D((size_t) H * FF);
    for (int64_t r = 0; r < FF; ++r) {
        tg->to_float(blob.data() + r * f.gu_row, G.data() + r * H, H);
        tg->to_float(blob.data() + f.up_off + r * f.gu_row, U.data() + r * H, H);
    }
    for (int64_t r = 0; r < H; ++r) td->to_float(blob.data() + f.down_off + r * f.d_row, D.data() + r * FF, FF);
    if (f.tail_off) {
        float tail[4];
        std::memcpy(tail, blob.data() + f.tail_off, sizeof tail);
        for (auto& v : G) v *= tail[0];
        for (auto& v : U) v *= tail[1];
        for (auto& v : D) v *= tail[2];
    }
    std::vector<float> h(FF);
    for (int64_t r = 0; r < FF; ++r) {
        double g = 0, u = 0;
        for (int64_t i = 0; i < H; ++i) {
            g += (double) G[r * H + i] * x[i];
            u += (double) U[r * H + i] * x[i];
        }
        h[r] = (float) (g / (1.0 + std::exp(-g)) * u);
    }
    for (int64_t r = 0; r < H; ++r) {
        double o = 0;
        for (int64_t i = 0; i < FF; ++i) o += (double) D[r * FF + i] * h[i];
        ref[r] = (float) o;
    }
}

// The ggml-cpu CPU oracle (the parity tool's (b) path): quantize the activation, then the rows, exactly as
// native_expert_parity.cpp:106-119 does.
void cpu_oracle(const cpu::NativeFmt& f, const std::vector<uint8_t>& blob, const float* x, float* out) {
    std::vector<uint8_t> act(cpu::kNativeActBytes), hq(cpu::kNativeHBytes);
    std::vector<float> ff(FF);
    cpu::native_quant_act(f, x, act.data());
    const void* a[1] = {act.data()};
    float* ffp[1] = {ff.data()};
    cpu::native_gu_rows(f, blob.data(), a, 1, ffp, 0, (int) FF);
    cpu::native_quant_h(f, ff.data(), hq.data());
    const void* hp[1] = {hq.data()};
    float* op[1] = {out};
    cpu::native_down_rows(f, blob.data(), hp, 1, op, 0, (int) H);
}

void write_bin(const std::string& path, const void* p, size_t bytes) {
    std::ofstream o(path, std::ios::binary);
    o.write((const char*) p, (std::streamsize) bytes);
}

std::string sha(const std::string& path) {
    std::string h;
    if (!prep::sha256_file(path, h)) return "MISSING";
    return h;
}

bool write_fixture(const std::string& dir, int gu, int d, int seed, std::string& err) {
    cpu::NativeFmt f;
    std::string e2;
    if (!cpu::native_fmt(gu, d, H, FF, f, e2)) {
        err = "native_fmt(" + std::to_string(gu) + "," + std::to_string(d) + "): " + e2;
        return false;
    }
    prep::PrepV1Layout L;
    if (const int rc = prep::derive_layout(gu, d, H, FF, L, err)) {
        err += " (derive_layout rc=" + std::to_string(rc) + ")";
        return false;
    }
    if (L.bytes != f.bytes) {
        err = "layout byte count disagrees with native_fmt (" + std::to_string(L.bytes) + " vs " +
              std::to_string(f.bytes) + ")";
        return false;
    }
    const std::string label = std::string(ggml_type_name((ggml_type) gu)) + "_" + ggml_type_name((ggml_type) d);
    const std::vector<uint8_t> blob = synthetic_blob(f, seed);

    std::mt19937 rng(11 + seed);
    std::normal_distribution<float> nd(0.f, 1.f);
    std::vector<float> x((size_t) H);
    for (auto& v : x) v = nd(rng);

    std::vector<float> ref((size_t) H), cpu_out((size_t) H);
    float_reference(f, blob, x.data(), ref.data());
    cpu_oracle(f, blob, x.data(), cpu_out.data());

    // The exactly-once reduction fixture: k=2, the expert's raw rows twice, weights 0.5/0.5, a shared row.
    // The recorded oracle is the pinned contract in scalar form: the first product rounds to F32, the next
    // product accumulates with FMA in expert order, shared is added once and NOT router-weighted.
    const int k = 2;
    std::vector<float> parts((size_t) k * H);
    for (int i = 0; i < H; ++i) {
        parts[(size_t) i] = cpu_out[(size_t) i];
        parts[(size_t) H + i] = cpu_out[(size_t) i];
    }
    const float weights[2] = {0.5f, 0.5f};
    std::vector<float> shared((size_t) H);
    std::mt19937 rs(seed * 7 + 3);
    std::normal_distribution<float> nds(0.f, 0.1f);
    for (auto& v : shared) v = nds(rs);
    std::vector<float> combine((size_t) H);
    for (int64_t i = 0; i < H; ++i) {
        float y = (float) (weights[0] * parts[(size_t) i]);  // the first product rounds to F32
        y = std::fma(weights[1], parts[(size_t) H + i], y);  // the next product is an FMA in expert order
        combine[(size_t) i] = y + shared[(size_t) i];        // shared added once, unweighted
    }

    const std::string base = dir + "/" + label;
    std::error_code ec;
    fs::create_directories(dir + "/negative", ec);
    fs::create_directories(base, ec);
    write_bin(base + "/blob.bin", blob.data(), blob.size());
    write_bin(base + "/x.bin", x.data(), x.size() * sizeof(float));
    write_bin(base + "/ref_f32.bin", ref.data(), ref.size() * sizeof(float));
    write_bin(base + "/cpu_out.bin", cpu_out.data(), cpu_out.size() * sizeof(float));
    write_bin(base + "/combine_parts.bin", parts.data(), parts.size() * sizeof(float));
    write_bin(base + "/combine_weights.bin", weights, sizeof weights);
    write_bin(base + "/combine_shared.bin", shared.data(), shared.size() * sizeof(float));
    write_bin(base + "/combine_oracle.bin", combine.data(), combine.size() * sizeof(float));

    std::ofstream m(base + "/manifest.txt", std::ios::binary);
    m << "# strata_prep_v1 fixture manifest v1 (SYNTHETIC: ggml-quantized random weights, NOT a real model blob)\n"
      << "version=1\n"
      << "label=" << label << "\n"
      << "synthetic=1\n"
      << "seed=" << seed << "\n"
      << "gu_type=" << gu << "\n"
      << "d_type=" << d << "\n"
      << "n_embd=" << H << "\n"
      << "n_ff=" << FF << "\n"
      << "nt=1\n"
      << "k=2\n"
      << "blob_bytes=" << (unsigned long long) f.bytes << "\n"
      << "gu_row=" << (unsigned long long) f.gu_row << "\n"
      << "d_row=" << (unsigned long long) f.d_row << "\n"
      << "up_off=" << (unsigned long long) f.up_off << "\n"
      << "down_off=" << (unsigned long long) f.down_off << "\n"
      << "tail_off=0\n"
      << "ggml=" << STRATA_PREP_GGML_NOTE << "\n"
      << "sha256_blob.bin=" << sha(base + "/blob.bin") << "\n"
      << "sha256_x.bin=" << sha(base + "/x.bin") << "\n"
      << "sha256_ref_f32.bin=" << sha(base + "/ref_f32.bin") << "\n"
      << "sha256_cpu_out.bin=" << sha(base + "/cpu_out.bin") << "\n"
      << "sha256_combine_parts.bin=" << sha(base + "/combine_parts.bin") << "\n"
      << "sha256_combine_weights.bin=" << sha(base + "/combine_weights.bin") << "\n"
      << "sha256_combine_shared.bin=" << sha(base + "/combine_shared.bin") << "\n"
      << "sha256_combine_oracle.bin=" << sha(base + "/combine_oracle.bin") << "\n";
    if (!m) {
        err = "cannot write " + base + "/manifest.txt";
        return false;
    }
    std::printf("SYNTHETIC fixture %-12s blob %8zu  sha256(blob) %s\n", label.c_str(), (size_t) f.bytes,
                sha(base + "/blob.bin").c_str());
    return true;
}

void write_negatives(const std::string& dir) {
    std::error_code ec;
    fs::create_directories(dir, ec);
    // Malformed manifests for the parser/guard tests: refused, never accepted.
    std::ofstream(dir + "/bad_version.txt") << "version=99\nlabel=x\nsynthetic=1\n";
    std::ofstream(dir + "/missing_keys.txt") << "version=1\nlabel=x\n";
    // R1 keeps every one of these refusals strict while the ten native classes are accepted: IQ1_M and IQ4_XS
    // stay outside the set in either role, IQ4_NL/Q2_0 are refused as gate/up, the gate/up i-quants are refused
    // as down, and an i-quant gate/up needs whole 256-value blocks in n_embd.
    std::ofstream(dir + "/bad_type.txt")
        << "version=1\nlabel=iq1m\nsynthetic=1\nseed=1\ngu_type=29\nd_type=20\nn_embd=2560\nn_ff=640\nnt=1\nk=2\n"
           "blob_bytes=1\ngu_row=0\nd_row=0\nup_off=0\ndown_off=0\ntail_off=0\n";  // IQ1_M: outside the v1 set
    std::ofstream(dir + "/bad_iq1m_down.txt")
        << "version=1\nlabel=iq1m_down\nsynthetic=1\nseed=1\ngu_type=12\nd_type=29\nn_embd=2560\nn_ff=640\nnt=1\nk=2\n"
           "blob_bytes=1\ngu_row=0\nd_row=0\nup_off=0\ndown_off=0\ntail_off=0\n";
    std::ofstream(dir + "/bad_iq_gu_as_down.txt")
        << "version=1\nlabel=iq2xxs_as_down\nsynthetic=1\nseed=1\ngu_type=12\nd_type=16\nn_embd=2560\nn_ff=640\n"
           "nt=1\nk=2\nblob_bytes=1\ngu_row=0\nd_row=0\nup_off=0\ndown_off=0\ntail_off=0\n";
    std::ofstream(dir + "/bad_iq4nl_as_gu.txt")
        << "version=1\nlabel=iq4nl_as_gate\nsynthetic=1\nseed=1\ngu_type=20\nd_type=20\nn_embd=2560\nn_ff=640\n"
           "nt=1\nk=2\nblob_bytes=1\ngu_row=0\nd_row=0\nup_off=0\ndown_off=0\ntail_off=0\n";
    std::ofstream(dir + "/bad_iq_geom.txt")
        << "version=1\nlabel=iq3xxs_odd_nembd\nsynthetic=1\nseed=1\ngu_type=18\nd_type=20\nn_embd=2592\nn_ff=640\n"
           "nt=1\nk=2\nblob_bytes=1\ngu_row=0\nd_row=0\nup_off=0\ndown_off=0\ntail_off=0\n";  // 2592 % 256 != 0
    std::ofstream(dir + "/overflow.txt")
        << "version=1\nlabel=ovf\nsynthetic=1\nseed=1\ngu_type=12\nd_type=7\nn_embd=900000000000\nn_ff=640\n"
           "nt=1\nk=2\nblob_bytes=999999999999999999999\n";
    std::ofstream(dir + "/bad_count.txt")
        << "version=1\nlabel=q4K_q5_1_trunc\nsynthetic=1\nseed=1\ngu_type=12\nd_type=7\nn_embd=2560\nn_ff=640\n"
           "nt=1\nk=2\nblob_bytes=1234\ngu_row=1440\nd_row=480\nup_off=921600\ndown_off=1843200\ntail_off=0\n";
    std::ofstream(dir + "/bad_k.txt")
        << "version=1\nlabel=k16\nsynthetic=1\nseed=1\ngu_type=12\nd_type=7\nn_embd=2560\nn_ff=640\nnt=1\nk=16\n"
           "blob_bytes=3072000\n";
    std::ofstream(dir + "/bad_nt.txt")
        << "version=1\nlabel=nt3\nsynthetic=1\nseed=1\ngu_type=12\nd_type=7\nn_embd=2560\nn_ff=640\nnt=3\nk=2\n"
           "blob_bytes=3072000\n";
}

}  // namespace

int main(int argc, char** argv) {
    std::string dir = argc > 2 ? argv[2] : "fixtures";
    if (argc < 2) {
        std::printf("usage: fixture_export <fixture-dir> [dir]\n  writes SYNTHETIC fixtures under <fixture-dir>\n");
        return 2;
    }
    // The provenance hash is proven before anything is hashed (published FIPS 180-4 vectors; exit 3 on failure).
    {
        std::string srep;
        if (!prep::sha256_selftest(srep)) {
            std::fprintf(stderr, "fixture_export: sha256 selftest FAILED, refusing to write any manifest:\n%s",
                         srep.c_str());
            return 3;
        }
        std::printf("sha256 selftest: PASS (15 published vectors, whole-buffer and streamed)\n");
    }
    dir = argv[1];
    const int pairs[][2] = {
        {2, 2}, {2, 3}, {12, 7}, {12, 8}, {6, 8}, {13, 8}, {8, 8}, {12, 6}, {14, 8},
        // R1: the ten native pack classes, in SYNTHETIC form (ggml-quantized random weights, labelled
        // synthetic=1 - they are NOT real model bytes).  They give the parity driver the same ten classes the
        // real pack uses, on weights this tool generated, beside the real fixtures.
        {16, 20}, {16, 42}, {17, 20}, {17, 42}, {18, 20}, {18, 42}, {21, 20}, {21, 42}, {22, 20}, {22, 42},
    };  // ggml.h ids: q4_0/q4_0, q4_0/q4_1, q4_K/q5_1, q4_K/q8_0, q5_0/q8_0, q5_K/q8_0, q8_0/q8_0, q4_K/q5_0,
        // q6_K/q8_0; then IQ2_XXS/IQ2_XS/IQ3_XXS/IQ3_S/IQ2_S with IQ4_NL and Q2_0 (the production pack's ten)
    int seed = 1;
    for (auto& p : pairs) {
        std::string err;
        if (!write_fixture(dir, p[0], p[1], seed++, err)) {
            std::fprintf(stderr, "fixture_export FAILED: %s\n", err.c_str());
            return 1;
        }
    }
    write_negatives(dir);
    std::printf("fixtures are SYNTHETIC (ggml-quantized random weights); no real model blob was read, loaded or copied\n");
    return 0;
}
