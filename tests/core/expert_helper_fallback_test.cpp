// tests/core/expert_helper_fallback_test.cpp - R2 (opt-in CUDA expert helper): the CPU recompute
// of the helper's CLAIMED rows, on the REAL fallback code (`expert_helper_recompute_claimed`) and
// the REAL CPU kernels (strata_kernels_cpu), with no GPU anything.
//
// The contract this test pins, in the exact path the finish seam runs when the helper's reply
// fails mid-request:
//   * exactly the claimed rows are recomputed, once each - rows the helper did not claim keep what
//     the dispatch left in them (the zeroed rows the CPU pass owns), untouched by the fallback;
//   * a recomputed row is BITWISE the row the all-CPU path produces for the same window: same
//     quantizer (act_quant_q8_1 / act_quant_any / native_quant_act, per the pack), same job
//     assembly, same pool call, same bytes - the fallback invents nothing;
//   * d.job_of is reset for every touched expert after the run (the dispatch's invariant);
//   * the accounting (multi_entries, cache_refused) counts the recomputed entries;
//   * a blob the source cannot produce keeps the latching failure path (failed + the source's
//     message + `missing`) and resets the jobs it touched - it never yields a silent row;
//   * the window contract (n_tok <= 8, k = 10, claims inside the window, a layer the layout has)
//     is refused, not guessed.
//
// CPU-only by construction: the pool, the quantizers, the layout and this driver - no device call,
// no GPU library linked (proved by nm on the built objects).  Exit 0 = PASS.
#include "strata/core/expert_source.hpp"

#include "strata/kernels/cpu/expert.hpp"
#include "strata/kernels/cpu/expert_layout.hpp"
#include "strata/kernels/cpu/native_expert.hpp"
#include "strata/kernels/cpu/pool.hpp"

#include "ggml.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <limits>
#include <random>
#include <string>
#include <vector>

#include <sys/stat.h>   // mkdtemp (the canonical-layout fixtures' scratch dir)
#include <unistd.h>

namespace cpu = strata::kernels::cpu;
using strata::core::ExpertDispatch;
using strata::core::ExpertSource;

// ExpertSource's two out-of-line virtuals (their definitions live in expert_source.cpp, the
// dispatch file this CPU-only test deliberately does NOT link).  Defining them HERE emits the
// base vtable in this translation unit: the same default contract as the engine's file
// (copy_blob: the blob's bytes when the source can produce it), exercised only through `blob`.
namespace strata::core {
bool ExpertSource::copy_blob(int64_t layer, int64_t expert, uint8_t* dst) {
    const uint8_t* b = blob(layer, expert);
    if (b == nullptr) return false;
    std::memcpy(dst, b, (size_t) cpu::expert_layout().blob_bytes(layer));
    return true;
}
bool ExpertSource::copy_blobs(const int32_t* layers, const int32_t* experts, uint8_t* const* dst, size_t n) {
    bool all = true;
    for (size_t i = 0; i < n; ++i)
        if (!copy_blob((int64_t) layers[i], (int64_t) experts[i], dst[i])) all = false;
    return all;
}
}  // namespace strata::core

namespace {

int failures = 0;
void check(bool ok, const char* what) {
    std::printf("  %-64s %s\n", what, ok ? "ok" : "FAIL");
    if (!ok) ++failures;
}

constexpr float kSentinel = -1234.5f;

/// A source over a table of blobs; an entry with an empty blob answers nullptr (a hole).
struct FakeSource final : ExpertSource {
    std::vector<std::vector<uint8_t>> blobs;   // index = layer * n_expert + expert
    const uint8_t* blob(int64_t layer, int64_t expert) override {
        const size_t i = (size_t) layer * (size_t) cpu::NE + (size_t) expert;
        if (i >= blobs.size() || blobs[i].empty()) return nullptr;
        return blobs[i].data();
    }
};

// a synthetic Strata-form (canonical Q2_0 / s2) expert blob - pool_tasks_test's generator
void make_s2_blob(uint8_t* b, std::mt19937& rng) {
    for (size_t i = 0; i < cpu::O_GU_SCALES; ++i) b[i] = (uint8_t) rng();
    for (size_t i = cpu::O_GU_SCALES; i < cpu::BLOB; i += 2) {
        const uint16_t h = (uint16_t) (0x1C00 + rng() % 0x0800);
        std::memcpy(b + i, &h, 2);
    }
}

/// The all-CPU reference for one window and one claim list: the quantizer the dispatch's CPU path
/// selects for this pack/layer, the same per-expert job assembly, the same pool call.  Written
/// from the dispatch's contract, INDEPENDENT of the fallback's code, so equality is evidence.
/// Rows outside `claimed` stay untouched (the caller seeds them with a sentinel).
void reference(cpu::ExpertPool& pool, FakeSource& src, int64_t layer, const float* x,
               const int32_t* ids, int64_t n_tok, const int32_t* claimed, int64_t n_claimed,
               bool native, const cpu::NativeFmt* fmt, float* out) {
    std::vector<cpu::ActQ> act((size_t) n_tok);
    std::vector<uint8_t> nact((size_t) n_tok * cpu::kNativeActBytes);
    std::vector<cpu::ExpertJobMulti> jobs((size_t) n_claimed);
    std::vector<int16_t> job_of((size_t) cpu::NE, -1);
    int njobs = 0;
    for (int64_t c = 0; c < n_claimed; ++c) {
        const int32_t i = claimed[c];
        const int64_t t = i / 10;
        const int32_t e = ids[i];
        int16_t& jo = job_of[(size_t) e];
        if (jo < 0) {
            jo = (int16_t) njobs++;
            jobs[(size_t) jo].blob = src.blob(layer, e);
            jobs[(size_t) jo].nt = 0;
            jobs[(size_t) jo].fmt = nullptr;
        }
        cpu::ExpertJobMulti& jb = jobs[(size_t) jo];
        jb.act[jb.nt] = &act[(size_t) t];
        jb.nact[jb.nt] = native ? nact.data() + (size_t) t * cpu::kNativeActBytes : nullptr;
        jb.out[jb.nt] = out + (size_t) i * cpu::H;
        ++jb.nt;
    }
    std::vector<uint8_t> used((size_t) n_tok, 0);
    for (int64_t c = 0; c < n_claimed; ++c) used[(size_t) (claimed[c] / 10)] = 1;
    for (int64_t t = 0; t < n_tok; ++t) {
        if (!used[(size_t) t]) continue;
        if (native && cpu::q2_native_kernels(fmt->gu_type))
            cpu::act_quant_any(x + (size_t) t * cpu::H, cpu::H, act[(size_t) t]);
        else if (native)
            cpu::native_quant_act(*fmt, x + (size_t) t * cpu::H,
                                  nact.data() + (size_t) t * cpu::kNativeActBytes);
        else
            cpu::act_quant_q8_1(x + (size_t) t * cpu::H, cpu::H, act[(size_t) t]);
    }
    if (njobs > 0) {
        if (native) pool.run_split_multi_native(*fmt, jobs.data(), njobs);
        else pool.run_split_multi(jobs.data(), njobs);
    }
}

std::vector<float> make_x(int64_t n_tok, uint32_t seed) {
    std::mt19937 rng(seed);
    std::vector<float> x((size_t) n_tok * cpu::H);
    for (size_t i = 0; i < x.size(); ++i)
        x[i] = 0.02f * std::sin((float) i * 0.017f) + 0.001f * (float) (rng() % 1000) / 1000.0f;
    return x;
}

// The canonical (non-native) Q2_0 layout: the pack dir has no native_experts.txt.
bool load_canonical(std::string& err) {
    char template_path[] = "/tmp/strata-fallback-empty-XXXXXX";
    const char* dir = ::mkdtemp(template_path);
    if (dir == nullptr) { err = "mkdtemp failed"; return false; }
    return cpu::expert_layout_load(dir, 4, cpu::NE, err);
}

/// The published fixtures' layer 0 (tests/data/native_experts/iq2_xs.txt: `0 22 42 0 1510400 ...`):
/// IQ2_S gate/up + Q2_0 down, built through native_fmt so the blob layout is the engine's own
/// contract.  IQ2_S (not IQ2_XS, ggml 17) is the pair this test quantizes: ggml's
/// `ggml_quantize_requires_imatrix` covers 16/17/19 only, so the fixture's own layer-0 types
/// quantize from plain weights; the imatrix-of-ones guard below keeps that true for any type.
/// The fixtures are FORMAT tables, not pack directories, so the test writes its own
/// native_experts.txt (load_native_layout) and synthesizes the bytes.
constexpr int kFixtureGu = GGML_TYPE_IQ2_S;   // 22 in the pinned ggml
constexpr int kFixtureD = GGML_TYPE_Q2_0;     // 42

bool make_native_fmt(cpu::NativeFmt& fmt, std::string& err) {
    if (!cpu::native_experts_available()) { err = "built without native experts (no ggml-cpu path)"; return false; }
    if (!cpu::native_fmt(kFixtureGu, kFixtureD, cpu::H, cpu::FF, fmt, err)) return false;
    if (fmt.had_gu || fmt.had_d) { err = "the fixtures' layer 0 is not Hadamard-folded; the test expects none"; return false; }
    return true;
}

/// A pack dir whose native_experts.txt says every layer is the fixtures' layer-0 format, and the
/// layout loaded from it - the fallback reads `expert_layout().fmt[layer]`, so the test must give
/// it the same table the blobs are built with.  (Layers 0..3, like the canonical scenario's dir.)
bool load_native_layout(std::string& err) {
    char template_path[] = "/tmp/strata-fallback-native-XXXXXX";
    const char* dir = ::mkdtemp(template_path);
    if (dir == nullptr) { err = "mkdtemp failed"; return false; }
    cpu::NativeFmt fmt;
    if (!make_native_fmt(fmt, err)) return false;
    std::ofstream out(std::string(dir) + "/native_experts.txt");
    if (!out) { err = "could not write native_experts.txt"; return false; }
    out << "# strata native experts v2: layer gu_type d_type offset blob_bytes (the fallback test)\n";
    for (int64_t l = 0; l < 4; ++l)
        out << l << ' ' << kFixtureGu << ' ' << kFixtureD << ' '
            << (uint64_t) l * (uint64_t) cpu::NE * (uint64_t) fmt.bytes << ' ' << fmt.bytes << '\n';
    if (!out) { err = "could not write native_experts.txt"; return false; }
    out.close();
    if (!cpu::expert_layout_load(dir, 4, cpu::NE, err)) return false;
    if (!cpu::expert_layout().native) { err = "the layout did not take the native table"; return false; }
    return true;
}

/// ggml's own quantizer for one projection of a synthetic native blob, with the parity harness's
/// imatrix guard (src/kernels/native_expert_parity.cpp:506): an IQ type that needs an importance
/// matrix gets a flat one, every other type gets nullptr.
void quant_rows(int type, const float* w, uint8_t* dst, int64_t rows, int64_t cols) {
    const std::vector<float> imatrix((size_t) cols, 1.0f);
    ggml_quantize_chunk((ggml_type) type, w, dst, 0, rows, cols,
                        ggml_quantize_requires_imatrix((ggml_type) type) ? imatrix.data() : nullptr);
}

// =============================== the scenarios ===============================

void scenario_canonical(cpu::ExpertPool& pool) {
    std::string err;
    if (!load_canonical(err)) { check(false, ("canonical layout load: " + err).c_str()); return; }
    check(!cpu::expert_layout().native, "the fixtures' pack dir gives the canonical Q2_0 layout");

    const int64_t layer = 2, n_tok = 8, k = 10;
    const std::vector<int32_t> claims = {0, 1, 10, 20, 31, 44, 55, 63, 70, 79};
    const auto x = make_x(n_tok, 7);
    auto is_claimed = [&](int32_t i) {
        for (const int32_t c : claims) if (c == i) return true;
        return false;
    };

    FakeSource src;
    src.blobs.resize((size_t) 4 * cpu::NE);
    std::mt19937 rng(99);
    std::vector<int32_t> ids((size_t) n_tok * k);
    for (int64_t t = 0; t < n_tok; ++t)
        for (int32_t j = 0; j < k; ++j) ids[(size_t) t * k + (size_t) j] = (int32_t) (1 + (t + j) % 4);
    for (int32_t e = 1; e <= 4; ++e) {
        src.blobs[(size_t) layer * cpu::NE + (size_t) e].resize(cpu::BLOB);
        make_s2_blob(src.blobs[(size_t) layer * cpu::NE + (size_t) e].data(), rng);
    }

    ExpertDispatch d;
    d.pool = &pool;
    d.src = &src;
    d.n_expert = cpu::NE;
    d.job_of.assign((size_t) cpu::NE, -1);

    std::vector<float> out((size_t) n_tok * k * cpu::H, kSentinel);
    const bool ok = strata::core::expert_helper_recompute_claimed(
        d, x.data(), ids.data(), n_tok, k, claims.data(), (int64_t) claims.size(),
        /*native=*/false, layer, out.data());
    check(ok, "canonical: the fallback accepts the window");
    check(!d.failed, "canonical: no source failure");

    std::vector<float> ref((size_t) n_tok * k * cpu::H, kSentinel);
    reference(pool, src, layer, x.data(), ids.data(), n_tok, claims.data(), (int64_t) claims.size(),
              false, nullptr, ref.data());
    check(std::memcmp(ref.data(), out.data(), ref.size() * sizeof(float)) == 0,
          "canonical: every recomputed row is BITWISE the all-CPU row");
    bool sentinel_ok = true;
    for (int64_t i = 0; i < n_tok * k; ++i) {
        const bool claimed = is_claimed((int32_t) i);
        if (!claimed && out[(size_t) i * cpu::H] != kSentinel) sentinel_ok = false;
        if (claimed && !std::isfinite(out[(size_t) i * cpu::H])) sentinel_ok = false;
    }
    check(sentinel_ok, "canonical: unclaimed rows untouched, claimed rows finite");
    bool reset = true;
    for (int64_t e = 1; e <= 4; ++e)
        if (d.job_of[(size_t) e] != -1) reset = false;
    check(reset, "canonical: d.job_of reset for every touched expert");
    check(d.multi_entries == (int64_t) claims.size() && d.cache_refused == (int64_t) claims.size(),
          "canonical: the recomputed entries are counted once each");
    for (auto& j : d.jobs_multi) j.nt = 0;

    // one-token window: the fallback serves a single token's claims (the multi path's floor)
    {
        std::vector<int32_t> claims1 = {0, 3, 9};
        std::vector<float> out1((size_t) 1 * k * cpu::H, kSentinel);
        const bool ok1 = strata::core::expert_helper_recompute_claimed(
            d, x.data(), ids.data(), 1, k, claims1.data(), (int64_t) claims1.size(), false, layer, out1.data());
        std::vector<float> ref1((size_t) 1 * k * cpu::H, kSentinel);
        reference(pool, src, layer, x.data(), ids.data(), 1, claims1.data(), (int64_t) claims1.size(),
                  false, nullptr, ref1.data());
        check(ok1 && std::memcmp(ref1.data(), out1.data(), ref1.size() * sizeof(float)) == 0,
              "canonical: a one-token window recomputes bitwise the same rows");
    }
}

void scenario_native(cpu::ExpertPool& pool) {
    std::string err;
    if (!load_native_layout(err)) {
        check(false, ("native layout (the fixtures' layer-0 format): " + err).c_str());
        return;
    }
    // bind AFTER the load: expert_layout() is a reference to the process-wide table
    const auto& lay = cpu::expert_layout();
    const cpu::NativeFmt& fmt = lay.fmt[0];
    check(fmt.gu_type == kFixtureGu && fmt.d_type == kFixtureD, "the loaded layout is the fixtures' layer 0");
    std::printf("  native layer (fixtures' iq2_xs.txt layer 0): gu=%d down=%d bytes=%zu (q2 native kernels: %d)\n",
                fmt.gu_type, fmt.d_type, fmt.bytes, (int) cpu::q2_native_kernels(fmt.gu_type));

    const int64_t layer = 0, n_tok = 8, k = 10;
    std::vector<int32_t> ids((size_t) n_tok * k);
    for (int64_t t = 0; t < n_tok; ++t)
        for (int32_t j = 0; j < k; ++j) ids[(size_t) t * k + (size_t) j] = (int32_t) (10 + (t * 3 + j) % 5);
    const std::vector<int32_t> claims_u = {0, 5, 11, 27, 38, 41, 52, 60, 74};
    // (the helper's claim list is unique by construction; a duplicate claim is a contract case,
    //  exercised in the canonical failures, not a compute case)

    FakeSource src;
    src.blobs.resize((size_t) 4 * cpu::NE);
    for (int32_t e = 10; e < 15; ++e) {
        std::vector<float> w((size_t) cpu::H * cpu::FF);
        for (size_t i = 0; i < w.size(); ++i) w[i] = 0.02f * std::sin((float) i * 0.013f);
        auto& blob = src.blobs[(size_t) layer * cpu::NE + (size_t) e];
        blob.assign(fmt.bytes, 0);
        quant_rows(fmt.gu_type, w.data(), blob.data(), cpu::FF, cpu::H);
        quant_rows(fmt.gu_type, w.data(), blob.data() + fmt.up_off, cpu::FF, cpu::H);
        quant_rows(fmt.d_type, w.data(), blob.data() + fmt.down_off, cpu::H, cpu::FF);
    }

    const auto x = make_x(n_tok, 21);
    ExpertDispatch d;
    d.pool = &pool;
    d.src = &src;
    d.n_expert = cpu::NE;
    d.job_of.assign((size_t) cpu::NE, -1);

    std::vector<float> out((size_t) n_tok * k * cpu::H, kSentinel);
    const bool ok = strata::core::expert_helper_recompute_claimed(
        d, x.data(), ids.data(), n_tok, k, claims_u.data(), (int64_t) claims_u.size(),
        /*native=*/true, layer, out.data());
    check(ok, "native: the fallback accepts the window");
    std::vector<float> ref((size_t) n_tok * k * cpu::H, kSentinel);
    reference(pool, src, layer, x.data(), ids.data(), n_tok, claims_u.data(), (int64_t) claims_u.size(),
              true, &fmt, ref.data());
    check(std::memcmp(ref.data(), out.data(), ref.size() * sizeof(float)) == 0,
          "native: every recomputed row is BITWISE the all-CPU row");
    bool finite = true;
    for (const int32_t i : claims_u)
        if (!std::isfinite(out[(size_t) i * cpu::H])) finite = false;
    check(finite, "native: every recomputed row is finite");
    check(out[(size_t) 1 * cpu::H] == kSentinel, "native: an unclaimed row is untouched");
}

void scenario_failures_canonical(cpu::ExpertPool& pool) {
    std::string err;
    if (!load_canonical(err)) { check(false, "canonical layout for the failure cases"); return; }
    const int64_t layer = 1, n_tok = 8, k = 10;
    std::vector<int32_t> ids((size_t) n_tok * k, 3);
    FakeSource src;
    src.blobs.resize((size_t) 4 * cpu::NE);   // all holes: no blob is producible
    ExpertDispatch d;
    d.pool = &pool;
    d.src = &src;
    d.n_expert = cpu::NE;
    d.job_of.assign((size_t) cpu::NE, -1);
    const auto x = make_x(n_tok, 5);
    std::vector<float> out((size_t) n_tok * k * cpu::H, kSentinel);
    const std::vector<int32_t> claims = {0, 10};
    const bool ok = strata::core::expert_helper_recompute_claimed(
        d, x.data(), ids.data(), n_tok, k, claims.data(), (int64_t) claims.size(), false, layer, out.data());
    check(!ok, "a missing blob fails the window");
    check(d.failed, "a missing blob LATCHES d.failed (the dispatch's failure path)");
    check(d.missing == 1, "the missing expert is counted");
    bool untouched = true;
    for (float v : out) if (v != kSentinel) untouched = false;
    check(untouched, "a failed recompute writes NO row");
    check(d.job_of[3] == -1, "a failed recompute resets the job map it touched");

    // a claim list with a repeated entry: the row is written once (the engine's own claim list is
    // unique; a repeat must NOT compute the row twice into two job entries)
    {
        FakeSource s2;
        s2.blobs.resize((size_t) 4 * cpu::NE);
        std::mt19937 r2(55);
        s2.blobs[(size_t) layer * cpu::NE + 3].resize(cpu::BLOB);
        make_s2_blob(s2.blobs[(size_t) layer * cpu::NE + 3].data(), r2);
        ExpertDispatch h;
        h.pool = &pool;
        h.src = &s2;
        h.n_expert = cpu::NE;
        h.job_of.assign((size_t) cpu::NE, -1);
        std::vector<int32_t> claims = {0, 0, 10};
        std::vector<float> o3((size_t) 8 * 10 * cpu::H, kSentinel);
        const bool r = strata::core::expert_helper_recompute_claimed(
            h, x.data(), ids.data(), 8, 10, claims.data(), (int64_t) claims.size(), false, layer, o3.data());
        std::vector<int32_t> uniq = {0, 10};
        std::vector<float> ref3((size_t) 8 * 10 * cpu::H, kSentinel);
        reference(pool, s2, layer, x.data(), ids.data(), 8, uniq.data(), 2, false, nullptr, ref3.data());
        check(r && std::memcmp(ref3.data(), o3.data(), ref3.size() * sizeof(float)) == 0,
              "a repeated claim computes the row exactly once");
    }
}

/// The window contract, refused rather than guessed; zero claims succeed with no work.
/// (These paths return BEFORE any quantization, so they run on every CPU the engine runs on.  The
///  layout is the NATIVE one: a layer the layout has must be a valid layer for the check to mean
///  what it says, and a canonical layout carries no per-layer format at all.)
void scenario_contract(cpu::ExpertPool& pool) {
    std::string err;
    if (!load_native_layout(err)) { check(false, "native layout for the contract cases"); return; }
    const int64_t n_tok = 8, k = 10;
    std::vector<int32_t> ids((size_t) n_tok * k, 3);
    FakeSource src;
    src.blobs.resize((size_t) 4 * cpu::NE);
    const auto x = make_x(n_tok, 5);
    struct Case { int64_t n_tok; int64_t k; std::vector<int32_t> claims; int64_t layer; };
    const std::vector<Case> cases = {
        {9, 10, {0}, 1},        // more than MAXT tokens
        {8, 9, {0}, 1},         // a routing width that is not 10
        {8, 10, {80}, 1},       // a claim outside the window
        {8, 10, {-1}, 1},       // a negative claim
        {8, 10, {0}, 4},        // a layer the layout does not have
    };
    for (const auto& c : cases) {
        ExpertDispatch f;
        f.pool = &pool;
        f.src = &src;
        f.n_expert = cpu::NE;
        f.job_of.assign((size_t) cpu::NE, -1);
        std::vector<float> o((size_t) 8 * 10 * cpu::H, kSentinel);
        const bool r = strata::core::expert_helper_recompute_claimed(
            f, x.data(), ids.data(), c.n_tok, c.k, c.claims.data(), (int64_t) c.claims.size(),
            false, c.layer, o.data());
        char msg[96];
        std::snprintf(msg, sizeof(msg), "contract refused: n_tok=%lld k=%lld layer=%lld claim[0]=%lld",
                      (long long) c.n_tok, (long long) c.k, (long long) c.layer, (long long) c.claims[0]);
        check(!r, msg);
        bool clean = true;
        for (float v : o) if (v != kSentinel) clean = false;
        check(clean, "  ...and the refused recompute wrote no row");
    }

    // zero claims: success, nothing to do (the begin claimed zero rows - the CPU served all)
    ExpertDispatch g;
    g.pool = &pool;
    g.src = &src;
    g.n_expert = cpu::NE;
    std::vector<float> o2((size_t) 8 * 10 * cpu::H, kSentinel);
    const bool z = strata::core::expert_helper_recompute_claimed(
        g, x.data(), ids.data(), 8, 10, nullptr, 0, false, 1, o2.data());
    check(z && !g.failed, "zero claims succeed with no work (the CPU served the window)");
    bool clean2 = true;
    for (float v : o2) if (v != kSentinel) clean2 = false;
    check(clean2, "zero claims wrote no row");
}

// ================= R5 P1: the prompt tier's MAXT=8 TILED same-layer fallback =================
// The design's bounded fallback: a kRowsP reply that left rows unanswered recomputes the deficit in
// tiles of <= cpu::MAXT tokens, each tile ONE exact call of the shipped window contract, each row
// copied to its OWN destination.  What is pinned here is what the design claims: the tiles partition
// the deficit (no row missing, no row twice, each at its own dst), every row is bitwise what the
// all-CPU path computes for it (the independent per-row `reference`), the budget is a STOP with the
// dispatch's failure latch untouched, and a blob the source cannot produce is the shipped function's
// own failure (d.failed latched, never a silent zero).
// Runs on BOTH pack shapes: the canonical Q2_0 layout (AVX-512's gate, the engine's own) and the
// NATIVE layout the engine serves on any AVX CPU - the tiling, the budget and the failure rule are
// the same code, so the native run is the one that executes on a host without AVX-512.  The caller
// loads the layout (expert_layout() is the process-wide table the fallback reads).
void scenario_prompt_tiled(cpu::ExpertPool& pool, const char* tag, int64_t layer, int32_t ebase, bool native,
                           const cpu::NativeFmt* fmt) {
    using strata::core::ExpertHelperPromptFallback;
    const int64_t k = 10;
    const int64_t n_sel = 256;                                   // the design's cap shape
    std::vector<int32_t> sel((size_t) n_sel);                    // the plan's tokens, ASCENDING
    int64_t max_tok = 0;
    for (int64_t i = 0; i < n_sel; ++i) { sel[(size_t) i] = (int32_t) (3 + i * 3); max_tok = sel[(size_t) i]; }
    ++max_tok;                                                   // the FULL layer's token count
    const auto x_sel = make_x(n_sel, 4242);                      // the tier's own pinned rows (ext_x)
    std::vector<int32_t> ids((size_t) max_tok * k);
    for (int64_t t = 0; t < max_tok; ++t)
        for (int64_t j = 0; j < k; ++j) ids[(size_t) t * k + (size_t) j] = (int32_t) (ebase + (t + j) % 4);
    FakeSource src;
    src.blobs.resize((size_t) 4 * cpu::NE);
    std::mt19937 rng(4);
    for (int32_t e = ebase; e < ebase + 4; ++e) {
        auto& blob = src.blobs[(size_t) layer * cpu::NE + (size_t) e];
        if (native) {   // the fixture's layer-0 format, quantized through ggml like scenario_native
            std::vector<float> w((size_t) cpu::H * cpu::FF);
            for (size_t i = 0; i < w.size(); ++i) w[i] = 0.02f * std::sin((float) i * 0.011f) + 1e-4f * (float) (i % 7);
            blob.assign(fmt->bytes, 0);
            quant_rows(fmt->gu_type, w.data(), blob.data(), cpu::FF, cpu::H);
            quant_rows(fmt->gu_type, w.data(), blob.data() + fmt->up_off, cpu::FF, cpu::H);
            quant_rows(fmt->d_type, w.data(), blob.data() + fmt->down_off, cpu::H, cpu::FF);
        } else {
            blob.resize(cpu::BLOB);
            make_s2_blob(blob.data(), rng);
        }
    }
    // the deficit: ONE unanswered row per selected token (the design's 256-row worst case), and the
    // tokens are NOT contiguous in the layer (a stride of 3), so no tile is a contiguous token run
    std::vector<int32_t> claims((size_t) n_sel);
    for (int64_t i = 0; i < n_sel; ++i) claims[(size_t) i] = (int32_t) sel[(size_t) i] * k + (int32_t) (i % k);
    // the destinations: 256 DISTINCT, scattered rows of a 1024-row out - a stride-251 walk, so every
    // row lands somewhere unrelated to its neighbours, exactly what the per-entry dst[c] copy is for
    const int64_t n_out = 1024;
    std::vector<float> out((size_t) n_out * cpu::H, kSentinel);
    std::vector<float*> dst((size_t) n_sel);
    std::vector<int32_t> rows_used((size_t) n_out, 0);
    for (int64_t c = 0; c < n_sel; ++c) {
        const int64_t r = (c * 251) % n_out;      // 251 is coprime with 1024: 256 distinct rows
        dst[(size_t) c] = out.data() + (size_t) r * cpu::H;
        ++rows_used[(size_t) r];
    }
    bool distinct = true;
    for (int64_t r = 0; r < n_out; ++r) if (rows_used[(size_t) r] > 1) distinct = false;
    std::printf("  ---- tiled prompt fallback: the %s layout ----\n", tag);
    check(distinct, "tiled: the 256 destinations are 256 distinct rows");

    ExpertDispatch d;
    d.pool = &pool;
    d.src = &src;
    d.n_expert = cpu::NE;
    d.job_of.assign((size_t) cpu::NE, -1);
    ExpertHelperPromptFallback fb;
    fb.start_layer();
    bool budget = true;
    const bool ok = strata::core::expert_helper_recompute_prompt_rows(
        d, x_sel.data(), sel.data(), n_sel, ids.data(), k, claims.data(), (int64_t) claims.size(),
        dst.data(), native, layer, /*max_rows_per_layer=*/256, /*max_rows_total=*/4096, fb, budget);
    check(ok, "tiled: the 256-row prompt deficit recomputes");
    check(!budget, "tiled: the budget was not reached (256 rows of the 256-row cap)");
    check(!d.failed, "tiled: no source failure");
    check(fb.rows_layer == 256 && fb.rows_total == 256,
          "tiled: every claimed row entered the budget counters once (32 tiles of 8 tokens)");

    // the partition: exactly the 256 destinations are written, the other 768 rows are untouched
    int64_t written = 0, untouched = 0;
    for (int64_t r = 0; r < n_out; ++r) {
        const bool is_dst = rows_used[(size_t) r] == 1;
        const bool sentinel = out[(size_t) r * cpu::H] == kSentinel;
        if (is_dst && sentinel) ++written;        // a destination left at the sentinel = MISSING
        if (!is_dst && !sentinel) ++untouched;    // a row written twice / written by accident
    }
    check(written == 0, "tiled: no destination is missing its row (the tiles cover the deficit)");
    check(untouched == 0, "tiled: no row outside the deficit was written (no duplicate, no spill)");
    bool finite = true;
    for (int64_t c = 0; c < n_sel; ++c)
        for (int64_t col = 0; col < cpu::H; ++col)
            if (!std::isfinite(dst[(size_t) c][(size_t) col])) finite = false;
    check(finite, "tiled: every recomputed row is finite");

    // bitwise: each tiled row equals the all-CPU row for that (token, expert) COMPUTED IN THE SAME
    // WINDOW SHAPE the fallback runs - one exact window per tile.  (Measured on this host: the
    // native multi-token kernel's rows drift by 1 ULP when the window's token grouping changes,
    // so a per-token 1-token window is NOT the bitwise baseline for an 8-token tile; the engine's
    // own all-CPU path runs the same ≤MAXT windows the tiles run, which is what this pins.)
    std::vector<float> ref_tile((size_t) n_sel * cpu::H, kSentinel);
    std::vector<float> ref_stage((size_t) cpu::MAXT * 10 * cpu::H, kSentinel);
    {
        int64_t at = 0;
        while (at < n_sel) {
            const int64_t nt = (n_sel - at < cpu::MAXT) ? (n_sel - at) : cpu::MAXT;
            std::vector<int32_t> ids_tile((size_t) nt * 10);
            std::vector<int32_t> cl_tile((size_t) nt);
            for (int64_t u = 0; u < nt; ++u)
                for (int64_t j = 0; j < 10; ++j)
                    ids_tile[(size_t) u * 10 + (size_t) j] = ids[(size_t) sel[(size_t) (at + u)] * k + (size_t) j];
            for (int64_t u = 0; u < nt; ++u)
                cl_tile[(size_t) u] = (int32_t) (u * 10 + claims[(size_t) (at + u)] % k);
            std::fill(ref_stage.begin(), ref_stage.end(), kSentinel);
            reference(pool, src, layer, x_sel.data() + (size_t) at * cpu::H, ids_tile.data(), nt,
                      cl_tile.data(), nt, native, fmt, ref_stage.data());
            for (int64_t u = 0; u < nt; ++u)
                std::memcpy(ref_tile.data() + (size_t) (at + u) * cpu::H,
                            ref_stage.data() + (size_t) cl_tile[(size_t) u] * cpu::H, (size_t) cpu::H * sizeof(float));
            at += nt;
        }
    }
    bool bitwise = true;
    int64_t mism = -1;
    for (int64_t c = 0; c < n_sel; ++c)
        if (std::memcmp(ref_tile.data() + (size_t) c * cpu::H, dst[(size_t) c], (size_t) cpu::H * sizeof(float)) != 0) {
            bitwise = false;
            mism = c;
            break;
        }
    check(bitwise, "tiled: every row is BITWISE the all-CPU row in its tile's window shape (all 256)");
    if (!bitwise) std::printf("      first mismatch at claim %lld\n", (long long) mism);

    // the budget is a STOP, not a failure: rows already served stay, d.failed stays clear
    {
        ExpertDispatch h;
        h.pool = &pool;
        h.src = &src;
        h.n_expert = cpu::NE;
        h.job_of.assign((size_t) cpu::NE, -1);
        std::vector<float> o((size_t) n_out * cpu::H, kSentinel);
        std::vector<float*> dd((size_t) n_sel);
        for (int64_t c = 0; c < n_sel; ++c) dd[(size_t) c] = o.data() + (size_t) ((c * 251) % n_out) * cpu::H;
        ExpertHelperPromptFallback fb2;
        fb2.start_layer();
        bool b2 = false;
        const bool r = strata::core::expert_helper_recompute_prompt_rows(
            h, x_sel.data(), sel.data(), n_sel, ids.data(), k, claims.data(), (int64_t) claims.size(),
            dd.data(), native, layer, /*max_rows_per_layer=*/100, 4096, fb2, b2);
        check(!r && b2, "budget: a layer over max_rows_per_layer stops the tiles (budget_exceeded, not a refusal)");
        check(!h.failed, "budget: d.failed stays CLEAR - the request keeps its local compute");
        check(fb2.rows_layer == 96 && fb2.rows_total == 96,
              "budget: whole tiles ran (12 x 8 = 96 rows), the 13th tile was not started");
        int64_t served = 0;
        for (int64_t c = 0; c < n_sel; ++c)
            if (dd[(size_t) c][0] != kSentinel) ++served;
        check(served == 96, "budget: the rows already served are COMPLETE rows, and only those");
        // the request-wide cap: the same rule on rows_total
        ExpertHelperPromptFallback fb3;
        fb3.start_layer();
        bool b3 = false;
        const bool r3 = strata::core::expert_helper_recompute_prompt_rows(
            h, x_sel.data(), sel.data(), n_sel, ids.data(), k, claims.data(), (int64_t) claims.size(),
            dd.data(), native, layer, 256, /*max_rows_total=*/40, fb3, b3);
        check(!r3 && b3 && !h.failed && fb3.rows_total == 40,
              "budget: the request-wide cap stops the tiles the same way (rows_total = 40, d.failed clear)");
    }

    // a blob the source cannot produce: the SHIPPED function's failure, inherited unchanged.
    // This fixtures' ids formula routes the claims in tiles (tile 1..2 = expert ebase+1, tile 3..4 =
    // ebase+3, ...), so the hole is placed on the expert the SECOND tile claims: the first tile
    // must complete, the second must fail at its blob, and the served count pin that.
    {
        FakeSource hole = src;
        hole.blobs[(size_t) layer * cpu::NE + (size_t) (ebase + 1)].clear();   // a hole the tiles hit
        ExpertDispatch h;
        h.pool = &pool;
        h.src = &hole;
        h.n_expert = cpu::NE;
        h.job_of.assign((size_t) cpu::NE, -1);
        std::vector<float> o((size_t) n_out * cpu::H, kSentinel);
        std::vector<float*> dd((size_t) n_sel);
        for (int64_t c = 0; c < n_sel; ++c) dd[(size_t) c] = o.data() + (size_t) ((c * 251) % n_out) * cpu::H;
        ExpertHelperPromptFallback fb4;
        fb4.start_layer();
        bool b4 = false;
        const bool r = strata::core::expert_helper_recompute_prompt_rows(
            h, x_sel.data(), sel.data(), n_sel, ids.data(), k, claims.data(), (int64_t) claims.size(),
            dd.data(), native, layer, 256, 4096, fb4, b4);
        check(!r && !b4, "a blob hole fails the recompute (it is not a budget stop)");
        check(h.failed, "a blob hole LATCHES d.failed, the shipped function's own rule (never a silent zero)");
        check(h.missing == 1, "the missing expert is counted once");
        check(h.fail != nullptr && std::strstr(h.fail, "blob") != nullptr, "the failure names the blob in d.fail");
        if (!(h.fail != nullptr && std::strstr(h.fail, "blob") != nullptr))
            std::printf("      blob-hole d.fail = %s (failed=%d missing=%lld)\n",
                        h.fail ? h.fail : "(null)", (int) h.failed, (long long) h.missing);
        int64_t served = 0;
        for (int64_t c = 0; c < n_sel; ++c)
            if (dd[(size_t) c][0] != kSentinel) ++served;
        // the tiles are 8 claims each (positions == claim order here): the first tile (claims 0..7,
        // expert ebase+3) ran COMPLETE, the second tile failed at its blob and wrote nothing.
        check(served == 8, "the tiles that ran keep their complete rows; nothing is half-written");
    }

    // the library's own contract, refused with the dispatch's failure latch
    {
        auto refuse = [&](const char* what, const int32_t* claims_p, int64_t n_c, const int32_t* sel_p,
                          int64_t n_s, int64_t kk) {
            ExpertDispatch h;
            h.pool = &pool;
            h.src = &src;
            h.n_expert = cpu::NE;
            h.job_of.assign((size_t) cpu::NE, -1);
            std::vector<float> o((size_t) n_out * cpu::H, kSentinel);
            std::vector<float*> dd((size_t) n_sel);
            for (int64_t c = 0; c < n_sel; ++c) dd[(size_t) c] = o.data() + (size_t) ((c * 251) % n_out) * cpu::H;
            ExpertHelperPromptFallback fb5;
            fb5.start_layer();
            bool b5 = false;
            const bool r = strata::core::expert_helper_recompute_prompt_rows(
                h, x_sel.data(), sel_p, n_s, ids.data(), kk, claims_p, n_c, dd.data(), native, layer, 256, 4096,
                fb5, b5);
            check(!r && h.failed && !b5, what);
            bool clean = true;
            for (float v : o) if (v != kSentinel) clean = false;
            check(clean, "  ...and the refused fallback wrote no row");
        };
        std::vector<int32_t> dup = claims;
        dup[5] = claims[4];                                        // one row claimed twice
        refuse("the tiled fallback refuses a duplicated claim", dup.data(), (int64_t) dup.size(), sel.data(), n_sel, 10);
        std::vector<int32_t> ghost((size_t) 1, 0);                 // token 0: not in sel, no activation row
        refuse("the tiled fallback refuses a token with no activation row in the tier's buffer",
               ghost.data(), 1, sel.data(), n_sel, 10);
        refuse("the tiled fallback refuses a routing width that is not 10", claims.data(),
               (int64_t) claims.size(), sel.data(), n_sel, 9);
        // zero claims: success, nothing to do (a reply that answered everything)
        ExpertDispatch h;
        h.pool = &pool;
        h.src = &src;
        h.n_expert = cpu::NE;
        h.job_of.assign((size_t) cpu::NE, -1);
        std::vector<float> o((size_t) n_out * cpu::H, kSentinel);
        ExpertHelperPromptFallback fb6;
        fb6.start_layer();
        bool b6 = false;
        const bool z = strata::core::expert_helper_recompute_prompt_rows(
            h, x_sel.data(), sel.data(), n_sel, ids.data(), k, nullptr, 0, nullptr, native, layer, 256, 4096,
            fb6, b6);
        check(z && !h.failed && !b6 && fb6.rows_total == 0, "an empty deficit succeeds with no work");
        bool clean6 = true;
        for (float v : o) if (v != kSentinel) clean6 = false;
        check(clean6, "an empty deficit wrote no row");
        // the stage is the library's OWN buffer, sized to the shipped window contract - and it is
        // allocated by the tile run, not by a call that had no work to do
        check(fb6.stage.empty(), "an empty deficit allocates no stage");
        check(fb.stage.size() == (size_t) strata::kernels::cpu::MAXT * 10 * strata::kernels::cpu::H,
              "the tile stage is MAXT*10 rows - the shipped window contract, not a widened one");
    }
}

}  // namespace

int main(int argc, char** argv) {
    (void) argc; (void) argv;
    if (!cpu::cpu_avx1_ok()) {
        std::printf("expert_helper_fallback_test: no AVX on this CPU, skipped\n");
        return 0;
    }
    std::printf("expert_helper_fallback_test:\n");
    cpu::ExpertPool pool(2, false);
    std::string err;
    // The canonical (non-native) Q2_0 pack's quantizer is the AVX-512 contract - the ENGINE's
    // own startup gate requires AVX-512 for it (cpu_require_expert_support), so the fallback's
    // non-native branch is only reachable there.  On an AVX2-only CPU the engine (and this
    // helper) serves native packs only: the native scenario is the one that runs, as #795 says.
    // The TILED PROMPT fallback runs on both shapes, so a host without AVX-512 still runs it (native).
    if (cpu::cpu_avx512_ok()) {
        scenario_canonical(pool);
        scenario_failures_canonical(pool);
        if (load_canonical(err)) scenario_prompt_tiled(pool, "canonical Q2_0", 2, 1, false, nullptr);
        else check(false, ("tiled: canonical layout load: " + err).c_str());
    } else {
        std::printf("  canonical Q2_0 scenarios need AVX-512 (the engine's own gate): skipped here\n");
    }
    scenario_native(pool);
    if (load_native_layout(err)) {
        // bind AFTER the load: expert_layout() is the process-wide table the fallback reads
        scenario_prompt_tiled(pool, "native (the fixtures' iq2_xs layer-0 format)", 0, 10, true,
                              &cpu::expert_layout().fmt[0]);
    } else {
        check(false, ("tiled: native layout load: " + err).c_str());
    }
    scenario_contract(pool);
    std::printf("expert_helper_fallback_test: %s\n", failures == 0 ? "PASS" : "FAIL");
    return failures == 0 ? 0 : 1;
}
