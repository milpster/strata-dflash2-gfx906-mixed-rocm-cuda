// tools/mixed_gpu/cuda_prep/fixture_export_real.cpp - CPU-only REAL-blob fixture exporter (Phase 2 prep, S2).
//
// WHAT THIS IS: labelled REAL fixtures.  The weights are the model's own bytes, sliced straight out of a real
// GGUF pack (the layer's gate/up/down expert tensors, back to back in the native-expert blob form
// [gate rows | up rows | down rows], include/strata/kernels/cpu/native_expert.hpp:5).  Nothing here
// synthesizes, re-quantizes, perturbs or generates a weight: the blob bytes are read from the file and
// written to the fixture unchanged, so a fixture with synthetic=0 is a real model slice with provenance.
// A fixture with synthetic=1 (fixture_export.cpp) stays exactly what it always was: ggml-quantized random
// weights, never presented as real.
//
// WHAT THE FIXTURES PROVE AND DO NOT PROVE: they prove the exporter, the manifest and the CPU oracle run on
// real bytes, and they record what the bounded v1 set says about each real pair (measured, printed).  A real
// blob whose types sit outside the bounded v1 set (every IQ* / Q2_0 pack on this machine does, measured over
// all 96 expert triples of the Swift-1.5 pack: see the run evidence) is recorded with an honest REFUSED
// verdict and is NOT consumable by cuda_prep_check v1.  The refusal is the checker's own
// derive_layout/manifest_parse verdict - this tool reports it, it never relaxes it.
//
// SOURCE REUSE (same instruction as fixture_export.cpp: the map's, not new math):
//   * the float reference is the parity tool's (a) path (native_expert_parity.cpp:69-105) - ggml's own
//     to_float dequantizer plus the FP64 SwiGLU accumulation, copied here with the dimensions taken from
//     the layout (attribute: native_expert_parity.cpp:69-105, fixture_export.cpp:77-107);
//   * the CPU oracle is strata_kernels_cpu's ggml-cpu decode (native_quant_act / native_gu_rows /
//     native_quant_h / native_down_rows, include/strata/kernels/cpu/native_expert.hpp) - the same oracle
//     path the synthetic exporter records, no new quant math, no new kernels (attribute: fixture_export.cpp:111-122);
//   * the exactly-once reduction fixture (k=2, weights 0.5/0.5, shared added once unweighted) is the
//     pinned contract, copied from fixture_export.cpp:164-183;
//   * the layout table comes from the existing engine parser cpu::expert_layout_load over the pack's own
//     native_experts.txt, and every byte count is cross-checked three ways (GGUF tensor dims x ggml's own
//     ggml_row_size x the sidecar's declared blob_bytes) - a disagreement refuses the fixture, it never
//     adjusts the numbers.
//
// The activation INPUTS x (and the shared vector) are deterministic seeded pseudo-random floats: they are
// test inputs, not model data, and the manifest says so (input_kind=seeded-pseudo-random).  The weights are
// real, the references are computed from the real bytes.
//
// CPU-only by construction: ggml (static, every GPU backend OFF in CMake) + strata_kernels_cpu.  No CUDA, no
// HIP, no dlopen, no GPU runtime.  It reads two files (the sidecar, the GGUF) and writes the fixture.
#include "abi.hpp"
#include "abi_validate.hpp"
#include "manifest.hpp"
#include "sha256.hpp"

#include "strata/kernels/cpu/expert_layout.hpp"
#include "strata/kernels/cpu/native_expert.hpp"

#include "ggml.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <map>
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

// ---- minimal GGUF header reader: the tensor table only ----
// WHY: the exporter must slice real bytes, so it needs each expert tensor's type, dimensions and offset.
// Only the header is read (a few MB), never the tensor payloads, and the file is opened read-only.
struct TensorInfo {
    std::string name;
    std::vector<uint64_t> dims;
    int type = -1;
    uint64_t off = 0;  // relative to the aligned tensor-data start
};

bool gguf_read_header(const std::string& path, std::vector<TensorInfo>& out, uint64_t& data_start,
                      std::string& err) {
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        err = "cannot open " + path;
        return false;
    }
    char magic[4];
    f.read(magic, 4);
    if (!f || std::memcmp(magic, "GGUF", 4) != 0) {
        err = "not a GGUF file: " + path;
        return false;
    }
    uint32_t version = 0;
    uint64_t n_tensors = 0, n_kv = 0;
    f.read((char*) &version, 4);
    f.read((char*) &n_tensors, 8);
    f.read((char*) &n_kv, 8);
    if (!f || version < 2 || version > 3) {
        err = "GGUF version " + std::to_string(version) + " not read (this exporter knows v2/v3)";
        return false;
    }
    auto rd_string = [&](std::string& s) -> bool {
        uint64_t n = 0;
        f.read((char*) &n, 8);
        if (!f || n > (1ull << 26)) return false;  // a 64 MiB cap: a bigger string is a corrupt header
        s.assign(n, '\0');
        f.read(s.data(), (std::streamsize) n);
        return (bool) f;
    };
    // GGUF scalar types (gguf.h): 0 U8, 1 I8, 2 U16, 3 I16, 4 U32, 5 I32, 6 F32, 7 BOOL, 8 STRING,
    // 9 ARRAY, 10 U64, 11 I64, 12 F64.  Arrays recurse; strings length-prefixed.
    std::function<bool(int, uint64_t&)> skip_value = [&](int t, uint64_t& as_u64) -> bool {
        uint64_t u = 0;
        double d = 0;
        float fl = 0;
        int32_t i32 = 0;
        int16_t i16 = 0;
        int8_t i8 = 0;
        uint8_t u8 = 0;
        uint16_t u16 = 0;
        uint32_t u32 = 0;
        switch (t) {
            case 0: f.read((char*) &u8, 1); u = u8; break;
            case 1: f.read((char*) &i8, 1); u = (uint64_t) (int64_t) i8; break;
            case 2: f.read((char*) &u16, 2); u = u16; break;
            case 3: f.read((char*) &i16, 2); u = (uint64_t) (int64_t) i16; break;
            case 4: f.read((char*) &u32, 4); u = u32; break;
            case 5: f.read((char*) &i32, 4); u = (uint64_t) (int64_t) i32; break;
            case 6: f.read((char*) &fl, 4); u = 0; break;
            case 7: f.read((char*) &u8, 1); u = u8; break;
            case 8: {
                std::string s;
                if (!rd_string(s)) return false;
                u = 0;
                break;
            }
            case 10: f.read((char*) &u, 8); break;
            case 11: f.read((char*) &d, 8); u = 0; break;
            case 12: f.read((char*) &d, 8); u = 0; break;
            case 9: {
                int et = 0;
                uint64_t cnt = 0;
                f.read((char*) &et, 4);
                f.read((char*) &cnt, 8);
                if (!f || cnt > (1ull << 24)) return false;  // a corrupt count is refused, not followed
                for (uint64_t i = 0; i < cnt; ++i) {
                    uint64_t dummy = 0;
                    if (!skip_value(et, dummy)) return false;
                }
                u = 0;
                break;
            }
            default: return false;  // unknown value type: refuse to guess the header's shape
        }
        as_u64 = u;
        return (bool) f;
    };
    uint64_t alignment = 32;
    for (uint64_t i = 0; i < n_kv; ++i) {
        std::string key;
        if (!rd_string(key)) {
            err = "GGUF: KV string read failed in " + path;
            return false;
        }
        int t = 0;
        f.read((char*) &t, 4);
        uint64_t v = 0;
        if (!skip_value(t, v)) {
            err = "GGUF: KV value read failed (" + key + ") in " + path;
            return false;
        }
        if (key == "general.alignment") alignment = v;
    }
    if (alignment == 0) alignment = 32;
    for (uint64_t i = 0; i < n_tensors; ++i) {
        TensorInfo ti;
        if (!rd_string(ti.name)) {
            err = "GGUF: tensor name read failed in " + path;
            return false;
        }
        uint32_t nd = 0;
        f.read((char*) &nd, 4);
        if (!f || nd == 0 || nd > 4) {
            err = "GGUF: tensor " + ti.name + " has " + std::to_string(nd) + " dims (this exporter reads 1..4)";
            return false;
        }
        ti.dims.resize(nd);
        for (uint32_t k = 0; k < nd; ++k) f.read((char*) &ti.dims[k], 8);
        int32_t t = 0;
        f.read((char*) &t, 4);
        f.read((char*) &ti.off, 8);
        if (!f) {
            err = "GGUF: tensor info read failed at " + ti.name + " in " + path;
            return false;
        }
        ti.type = t;
        out.push_back(ti);
    }
    const uint64_t header_end = (uint64_t) f.tellg();
    data_start = (header_end + alignment - 1) / alignment * alignment;  // GGUF: the data starts aligned
    return true;
}

const TensorInfo* find_tensor(const std::vector<TensorInfo>& ts, const std::string& name) {
    for (const auto& t : ts)
        if (t.name == name) return &t;
    return nullptr;
}

// Read exactly `bytes` at absolute offset `at`; false on any short read (a real slice must be complete).
bool read_exact(std::ifstream& f, uint64_t at, size_t bytes, uint8_t* dst) {
    f.seekg((std::streamoff) at);
    if (!f) return false;
    f.read((char*) dst, (std::streamsize) bytes);
    return (size_t) f.gcount() == bytes;
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

// The float reference (parity tool's (a) path, native_expert_parity.cpp:69-105 / fixture_export.cpp:77-107),
// with the dimensions taken from the layout instead of the fixture tool's fixed 2560/640.
void float_reference(const cpu::NativeFmt& f, const std::vector<uint8_t>& blob, const float* x, float* ref) {
    const int64_t H = f.n_embd, FF = f.n_ff;
    const auto* tg = ggml_get_type_traits((ggml_type) f.gu_type);
    const auto* td = ggml_get_type_traits((ggml_type) f.d_type);
    std::vector<float> G((size_t) FF * H), U((size_t) FF * H), D((size_t) H * FF);
    for (int64_t r = 0; r < FF; ++r) {
        tg->to_float(blob.data() + r * f.gu_row, G.data() + r * H, H);
        tg->to_float(blob.data() + f.up_off + r * f.gu_row, U.data() + r * H, H);
    }
    for (int64_t r = 0; r < H; ++r) td->to_float(blob.data() + f.down_off + r * f.d_row, D.data() + r * FF, FF);
    if (f.tail_off) {  // NVFP4 only: the real pack has no tail in v1, this keeps the reference honest to the format
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

// The ggml-cpu CPU oracle (the parity tool's (b) path, fixture_export.cpp:111-122) - the same call sequence.
void cpu_oracle(const cpu::NativeFmt& f, const std::vector<uint8_t>& blob, const float* x, float* out) {
    std::vector<uint8_t> act(f.act_bytes), hq(f.h_bytes);
    std::vector<float> ff((size_t) f.n_ff);
    cpu::native_quant_act(f, x, act.data());
    const void* a[1] = {act.data()};
    float* ffp[1] = {ff.data()};
    cpu::native_gu_rows(f, blob.data(), a, 1, ffp, 0, (int) f.n_ff);
    cpu::native_quant_h(f, ff.data(), hq.data());
    const void* hp[1] = {hq.data()};
    float* op[1] = {out};
    cpu::native_down_rows(f, blob.data(), hp, 1, op, 0, (int) f.n_embd);
}

struct Spec {
    int layer;
    int expert;
    bool shexp;  // the layer's shared expert (one blob, the *_shexp tensors)
};

// What one written fixture contributes to the coverage record (R1: class/layer coverage, real byte range).
struct Rec {
    std::string label;
    int layer = -1, expert = 0, gu = -1, d = -1;
    bool shexp = false;
    unsigned long long bytes = 0;
    std::string sha_blob;
    std::string verdict;
};

std::string role_name(int layer, const char* role, bool shexp) {
    return "blk." + std::to_string(layer) + ".ffn_" + role + (shexp ? "_shexp" : "_exps") + ".weight";
}

// One real fixture.  Returns false with a plain-language reason; a refusal writes NOTHING.
bool write_real_fixture(const std::string& out_dir, const std::string& gguf_path, const cpu::ExpertLayout& L,
                        const std::vector<TensorInfo>& ts, uint64_t data_start, const Spec& sp, int seed,
                        std::string& err, Rec* rec = nullptr) {
    const int layer = sp.layer;
    const cpu::NativeFmt& side = L.fmt[(size_t) layer];
    const std::string gname = role_name(layer, "gate", sp.shexp), uname = role_name(layer, "up", sp.shexp),
                    dname = role_name(layer, "down", sp.shexp);
    const TensorInfo *tg = find_tensor(ts, gname), *tu = find_tensor(ts, uname), *td = find_tensor(ts, dname);
    if (!tg || !tu || !td) {
        err = "layer " + std::to_string(layer) + (sp.shexp ? " shared expert" : " routed experts") +
              ": the GGUF has no gate/up/down tensor triple (" + gname + ", " + uname + ", " + dname + ")";
        return false;
    }
    // Cross-check 1: the GGUF tensor types must be what the pack's sidecar declares for this layer.
    if (tg->type != side.gu_type || tu->type != side.gu_type || td->type != side.d_type) {
        err = "layer " + std::to_string(layer) + ": GGUF types gate=" + std::to_string(tg->type) + " up=" +
              std::to_string(tu->type) + " down=" + std::to_string(td->type) +
              " but native_experts.txt declares gu=" + std::to_string(side.gu_type) + " d=" +
              std::to_string(side.d_type) + " (the pack and the GGUF disagree; refusing, not guessing)";
        return false;
    }
    // Cross-check 2: ggml's own row sizes over the tensor dims give the per-expert slice sizes.
    const int64_t n_embd = side.n_embd, n_ff = side.n_ff;
    if ((int64_t) tg->dims[0] != n_embd || (int64_t) tg->dims[1] != n_ff) {
        err = "layer " + std::to_string(layer) + " gate tensor dims " + std::to_string(tg->dims[0]) + "x" +
              std::to_string(tg->dims[1]) + " disagree with the layout's n_embd/n_ff " + std::to_string(n_embd) +
              "x" + std::to_string(n_ff);
        return false;
    }
    const size_t gu_row = (size_t) ggml_row_size((ggml_type) tg->type, (int64_t) tg->dims[0]);
    const size_t d_row = (size_t) ggml_row_size((ggml_type) td->type, (int64_t) td->dims[0]);
    const size_t gu_slice = gu_row * (size_t) n_ff;   // one expert's gate rows
    const size_t d_slice = d_row * (size_t) n_embd;   // one expert's down rows
    if (tu->dims != tg->dims || gu_row != (size_t) ggml_row_size((ggml_type) tu->type, (int64_t) tu->dims[0])) {
        err = "layer " + std::to_string(layer) + ": gate and up tensors are not the same shape/row size";
        return false;
    }
    const size_t blob_bytes = 2 * gu_slice + d_slice;
    // Cross-check 3: the sidecar's declared blob size must equal the measured geometry exactly.
    if ((size_t) L.bytes[(size_t) layer] != blob_bytes) {
        err = "layer " + std::to_string(layer) + ": native_experts.txt declares blob " +
              std::to_string(L.bytes[(size_t) layer]) + " B, ggml's row sizes over the tensor dims make " +
              std::to_string(blob_bytes) + " B (refusing: the bytes are never fitted to a claim)";
        return false;
    }
    if (!sp.shexp && tg->dims.size() < 3 || (sp.shexp && tg->dims.size() != 2)) {
        err = "layer " + std::to_string(layer) + ": tensor dims do not describe " +
              (sp.shexp ? "one shared expert" : "a stack of routed experts");
        return false;
    }
    const int64_t n_expert = sp.shexp ? 1 : (int64_t) (tg->dims.size() >= 3 ? tg->dims[2] : 1);
    if (sp.expert < 0 || sp.expert >= n_expert) {
        err = "expert " + std::to_string(sp.expert) + " outside layer " + std::to_string(layer) + "'s " +
              std::to_string(n_expert) + " experts";
        return false;
    }
    // The expert's slices: GGUF stores the stack as expert-major runs of gu_slice / d_slice bytes.
    std::ifstream in(gguf_path, std::ios::binary);
    if (!in) {
        err = "cannot open " + gguf_path;
        return false;
    }
    std::vector<uint8_t> blob(blob_bytes);
    const uint64_t g_at = data_start + tg->off + (uint64_t) sp.expert * gu_slice;
    const uint64_t u_at = data_start + tu->off + (uint64_t) sp.expert * gu_slice;
    const uint64_t d_at = data_start + td->off + (uint64_t) sp.expert * d_slice;
    if (!read_exact(in, g_at, gu_slice, blob.data()) ||
        !read_exact(in, u_at, gu_slice, blob.data() + gu_slice) ||
        !read_exact(in, d_at, d_slice, blob.data() + 2 * gu_slice)) {
        err = "layer " + std::to_string(layer) + " expert " + std::to_string(sp.expert) +
              ": a real slice is short (file ends before the tensor): " + gguf_path;
        return false;
    }
    in.close();

    // The blob's own geometry, recomputed with the engine's own native_fmt (the same table the pool uses).
    cpu::NativeFmt f;
    std::string ferr;
    if (!cpu::native_fmt(side.gu_type, side.d_type, n_embd, n_ff, f, ferr)) {
        err = "native_fmt(" + std::to_string(side.gu_type) + "," + std::to_string(side.d_type) + "): " + ferr;
        return false;
    }
    if (f.bytes != blob_bytes) {
        err = "native_fmt blob " + std::to_string(f.bytes) + " != measured slice total " + std::to_string(blob_bytes);
        return false;
    }

    // The test input x is deterministic seeded pseudo-random - an INPUT, not model data; the manifest says so.
    std::mt19937 rng(1000 + seed * 17 + sp.layer * 5 + sp.expert);
    std::normal_distribution<float> nd(0.f, 1.f);
    std::vector<float> x((size_t) n_embd);
    for (auto& v : x) v = nd(rng);

    std::vector<float> ref((size_t) n_embd), cpu_out((size_t) n_embd);
    float_reference(f, blob, x.data(), ref.data());
    cpu_oracle(f, blob, x.data(), cpu_out.data());

    // The exactly-once reduction fixture, the pinned contract copied from fixture_export.cpp:164-183.
    const int k = 2;
    std::vector<float> parts((size_t) k * n_embd);
    for (int64_t i = 0; i < n_embd; ++i) {
        parts[(size_t) i] = cpu_out[(size_t) i];
        parts[(size_t) n_embd + i] = cpu_out[(size_t) i];
    }
    const float weights[2] = {0.5f, 0.5f};
    std::vector<float> shared((size_t) n_embd);
    std::mt19937 rs(seed * 7 + 3 + sp.layer);
    std::normal_distribution<float> nds(0.f, 0.1f);
    for (auto& v : shared) v = nds(rs);
    std::vector<float> combine((size_t) n_embd);
    for (int64_t i = 0; i < n_embd; ++i) {
        float y = (float) (weights[0] * parts[(size_t) i]);  // the first product rounds to F32
        y = std::fma(weights[1], parts[(size_t) n_embd + i], y);  // the next product is an FMA in expert order
        combine[(size_t) i] = y + shared[(size_t) i];             // shared added once, unweighted
    }

    const std::string label = std::string("real_") + ggml_type_name((ggml_type) f.gu_type) + "_" +
                              ggml_type_name((ggml_type) f.d_type) + (sp.shexp ? "_shexp" : "") + "_L" +
                              std::to_string(layer) + "E" + std::to_string(sp.expert);
    const std::string base = out_dir + "/" + label;
    std::error_code ec;
    fs::create_directories(base, ec);
    if (!fs::is_directory(base, ec)) {
        err = "cannot create " + base;
        return false;
    }
    write_bin(base + "/blob.bin", blob.data(), blob.size());
    write_bin(base + "/x.bin", x.data(), x.size() * sizeof(float));
    write_bin(base + "/ref_f32.bin", ref.data(), ref.size() * sizeof(float));
    write_bin(base + "/cpu_out.bin", cpu_out.data(), cpu_out.size() * sizeof(float));
    write_bin(base + "/combine_parts.bin", parts.data(), parts.size() * sizeof(float));
    write_bin(base + "/combine_weights.bin", weights, sizeof weights);
    write_bin(base + "/combine_shared.bin", shared.data(), shared.size() * sizeof(float));
    write_bin(base + "/combine_oracle.bin", combine.data(), combine.size() * sizeof(float));

    // The v1 verdict, from the checker's OWN gate (prep::derive_layout), on the REAL pair - never relaxed.
    prep::PrepV1Layout v1;
    std::string verr;
    const int vrc = prep::derive_layout(f.gu_type, f.d_type, n_embd, n_ff, v1, verr);
    const std::string verdict = vrc == STRATA_PREP_V1_OK ? "consumable" : "REFUSED(" + verr + ")";

    if (rec != nullptr) {  // the coverage record the exporter writes after the last fixture
        rec->label = label;
        rec->layer = layer;
        rec->expert = sp.expert;
        rec->shexp = sp.shexp;
        rec->gu = f.gu_type;
        rec->d = f.d_type;
        rec->bytes = (unsigned long long) blob_bytes;
        rec->verdict = verdict;
    }

    std::ofstream m(base + "/manifest.txt", std::ios::binary);
    m << "# strata_prep_v1 fixture manifest v1 (REAL: the model's own bytes sliced from a real GGUF, NOT synthetic)\n"
      << "version=1\n"
      << "label=" << label << "\n"
      << "synthetic=0\n"
      << "seed=" << seed << "\n"
      << "gu_type=" << f.gu_type << "\n"
      << "d_type=" << f.d_type << "\n"
      << "n_embd=" << n_embd << "\n"
      << "n_ff=" << n_ff << "\n"
      << "nt=1\n"
      << "k=2\n"
      << "blob_bytes=" << (unsigned long long) blob_bytes << "\n"
      << "gu_row=" << (unsigned long long) f.gu_row << "\n"
      << "d_row=" << (unsigned long long) f.d_row << "\n"
      << "up_off=" << (unsigned long long) f.up_off << "\n"
      << "down_off=" << (unsigned long long) f.down_off << "\n"
      << "tail_off=0\n"
      << "ggml=" << STRATA_PREP_GGML_NOTE << "\n"
      << "input_kind=seeded-pseudo-random (x and the shared vector are test inputs, not model data)\n"
      << "production_activation=0 (no production activation is claimed: x is a seeded test input, the blob is a model slice, nothing here was produced by the serving engine)\n"
      << "source_gguf=" << gguf_path << "\n"
      << "source_layer=" << layer << "\n"
      << "source_expert=" << sp.expert << "\n"
      << "source_kind=" << (sp.shexp ? "shared-expert (ffn_*_shexp)" : "routed-expert (ffn_*_exps)") << "\n"
      << "source_tensor_gate=" << gname << "\n"
      << "source_tensor_up=" << uname << "\n"
      << "source_tensor_down=" << dname << "\n"
      << "source_gate_off=" << g_at << "\n"
      << "source_up_off=" << u_at << "\n"
      << "source_down_off=" << d_at << "\n"
      << "source_gate_bytes=" << (unsigned long long) gu_slice << "\n"
      << "source_up_bytes=" << (unsigned long long) gu_slice << "\n"
      << "source_down_bytes=" << (unsigned long long) d_slice << "\n"
      << "v1_verdict=" << verdict << "\n"
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
    m.close();  // flush: the read-back below must see the bytes the manifest was written with

    // The manifest is then run through the SAME parser the driver uses, and its verdict is recorded as written.
    prep::Manifest pm;
    std::string perr;
    const bool parsed = prep::manifest_parse(base + "/manifest.txt", pm, perr);
    std::ofstream v(base + "/v1-verdict.txt", std::ios::binary);
    v << "REAL fixture " << label << " (synthetic=0): the blob bytes are the model's own, sliced from "
      << gguf_path << " at layer " << layer << " expert " << sp.expert << ".\n"
      << "derive_layout (the checker's bounded-set gate): " << verdict << "\n"
      << "manifest_parse (the checker's manifest parser): " << (parsed ? "accepted" : "refused: " + perr) << "\n"
      << "cuda_prep_check consumes this fixture only if both lines above accept; a REFUSED real fixture is\n"
      << "honest evidence of the real pack's geometry, NOT a fixture v1 can run.  No check was relaxed here.\n";

    std::printf("REAL fixture %-28s blob %8zu  sha256(blob) %s  v1_verdict=%s  manifest_parse=%s\n", label.c_str(),
                blob_bytes, sha(base + "/blob.bin").c_str(), verdict.c_str(), parsed ? "accepted" : "refused");
    if (rec != nullptr) rec->sha_blob = sha(base + "/blob.bin");
    return true;
}

}  // namespace

int main(int argc, char** argv) {
    // The provenance hash is proven before anything is hashed: the published FIPS 180-4 vectors run first
    // (the first sha256.cpp had a mistyped K table - see the history note there).  Exit 3 = selftest failed.
    {
        std::string srep;
        if (!prep::sha256_selftest(srep)) {
            std::fprintf(stderr, "fixture_export_real: sha256 selftest FAILED, refusing to write any manifest:\n%s",
                         srep.c_str());
            return 3;
        }
        std::printf("sha256 selftest: PASS (15 published vectors, whole-buffer and streamed)\n");
    }
    if (argc < 4) {
        std::printf(
            "usage: fixture_export_real <pack-dir-with-native_experts.txt> <native.gguf> <out-dir> "
            "[[L:]layer[:expert] ...]\n"
            "  writes REAL-blob fixtures (synthetic=0) under <out-dir>, one dir per selected layer/expert.\n"
            "  The weights are the model's own bytes; x is a seeded pseudo-random test input (labelled): no\n"
            "  production activation is claimed.  A pair outside the bounded v1 set is written with an honest\n"
            "  REFUSED verdict, never faked.  Writes <out-dir>/coverage.txt: class/layer coverage, the real\n"
            "  blob byte range and every blob's SHA-256 (the historical fixtures-real/ set is never touched;\n"
            "  pass a new out-dir, e.g. fixtures-real-r1).\n"
            "  Default specs: L0:0 L1:0 L11:0 (routed experts, expert 0)\n"
            "  All 48 layers of the Swift-1.5 pack: pass L0:0 L1:0 ... L47:0 (one routed expert per layer).\n");
        return 2;
    }
    const std::string pack_dir = argv[1], gguf_path = argv[2], out_dir = argv[3];

    std::vector<Spec> specs;
    for (int i = 4; i < argc; ++i) {
        std::string s = argv[i];
        if (!s.empty() && (s[0] == 'L' || s[0] == 'l')) s = s.substr(1);
        Spec sp{};
        sp.expert = 0;
        sp.shexp = false;
        const char* p = s.c_str();
        char* end = nullptr;
        sp.layer = (int) std::strtol(p, &end, 10);
        if (end && *end == ':') sp.expert = (int) std::strtol(end + 1, &end, 10);
        if (end && *end == 's') sp.shexp = true;  // trailing 's': the layer's shared expert
        specs.push_back(sp);
    }
    if (specs.empty()) specs = {{0, 0, false}, {1, 0, false}, {11, 0, false}};

    // The pack's own layout table, through the engine's existing parser (the same one abi_guard_test exercises).
    std::string err;
    if (!cpu::expert_layout_load(pack_dir, 48, 512, err)) {
        std::fprintf(stderr, "fixture_export_real: layout refused: %s\n", err.c_str());
        return 1;
    }
    const cpu::ExpertLayout& L = cpu::expert_layout();
    std::vector<TensorInfo> ts;
    uint64_t data_start = 0;
    if (!gguf_read_header(gguf_path, ts, data_start, err)) {
        std::fprintf(stderr, "fixture_export_real: %s\n", err.c_str());
        return 1;
    }
    std::printf("layout: %s (%s, %lld layers x %lld experts, GGUF %zu tensors, data start %llu)\n", pack_dir.c_str(),
                L.native ? "native pack" : "NOT a native pack", (long long) L.n_layers, (long long) L.n_expert,
                ts.size(), (unsigned long long) data_start);

    int seed = 1, failed = 0;
    std::vector<Rec> recs;
    for (const Spec& sp : specs) {
        std::string e;
        Rec r;
        if (!write_real_fixture(out_dir, gguf_path, L, ts, data_start, sp, seed++, e, &r)) {
            std::fprintf(stderr, "  REFUSED layer %d expert %d: %s\n", sp.layer, sp.expert, e.c_str());
            ++failed;
            continue;
        }
        recs.push_back(r);
    }

    // The coverage record (R1): every fixture's class, layer, expert, byte count, blob SHA-256 and the checker's
    // own verdict, plus the per-class and per-layer tallies.  Written beside the fixtures, never over them.
    std::map<std::string, int> by_class;
    std::map<int, int> by_layer;
    unsigned long long min_b = ~0ull, max_b = 0;
    for (const Rec& r : recs) {
        by_class[std::to_string(r.gu) + "/" + std::to_string(r.d)]++;
        by_layer[r.layer]++;
        if (r.bytes < min_b) min_b = r.bytes;
        if (r.bytes > max_b) max_b = r.bytes;
    }
    std::error_code ec;
    fs::create_directories(out_dir, ec);
    std::ofstream cov(out_dir + "/coverage.txt", std::ios::binary);
    cov << "# strata_prep_v1 real-fixture coverage (fixture_export_real).  Every blob byte is the model's own,\n"
           "# sliced from the GGUF unchanged.  x is a seeded pseudo-random TEST INPUT: no production activation\n"
           "# is claimed by any line here.  v1_verdict is the checker's own bounded-set gate on the real pair.\n"
        << "source_gguf=" << gguf_path << "\n"
        << "source_sidecar=" << pack_dir << "/native_experts.txt\n"
        << "fixtures=" << recs.size() << "\n"
        << "pair_classes=" << by_class.size() << "\n"
        << "layers=" << by_layer.size() << "\n"
        << "blob_bytes_min=" << min_b << "\n"
        << "blob_bytes_max=" << max_b << "\n";
    for (const Rec& r : recs)
        cov << "fixture label=" << r.label << " layer=" << r.layer << " expert=" << r.expert
            << " kind=" << (r.shexp ? "shared" : "routed") << " gu_type=" << r.gu << " d_type=" << r.d
            << " blob_bytes=" << r.bytes << " sha256_blob=" << r.sha_blob << " v1_verdict=" << r.verdict << "\n";
    for (const auto& [k, n] : by_class) cov << "class " << k << " fixtures=" << n << "\n";
    for (const auto& [k, n] : by_layer) cov << "layer " << k << " fixtures=" << n << "\n";
    cov.close();

    std::printf("coverage: %zu fixture(s), %zu pair class(es), %zu layer(s), blob bytes %llu..%llu  -> %s\n",
                recs.size(), by_class.size(), by_layer.size(), min_b, max_b, (out_dir + "/coverage.txt").c_str());
    for (const auto& [k, n] : by_class) std::printf("  class %-8s %2d fixture(s)\n", k.c_str(), n);
    std::printf("real fixtures are REAL model bytes (synthetic=0); weights were read from the pack unchanged; "
                "x inputs are seeded pseudo-random test inputs (no production activation claim)\n");
    return failed ? 1 : 0;
}
