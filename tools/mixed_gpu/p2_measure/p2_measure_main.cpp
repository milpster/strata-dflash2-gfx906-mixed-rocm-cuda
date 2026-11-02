// tools/mixed_gpu/p2_measure/p2_measure_main.cpp - the parent-runnable P2 deviation harness (design §2
// lines 28-39, gate line 81): the SAME (token, expert) pairs of the REAL quantized blob, computed BOTH
// WAYS, measured, written out.  NO TOLERANCE, NO PASS/FAIL, NO BAND - the band is the user's
// (journal.md line 5) and the parent applies it AFTER the run.
//
// THE ONE DOCUMENTED COMMAND (a live window, after the helper is up on a device the engine will not use):
//
//   build-mixed-gpu/engine-cuda/strata-expert-helper \
//       --pack /mnt/user/ai/iq3xxs-15 --socket <run>/helper.sock --device 1 &
//   build-mixed-gpu/engine-cuda/p2_measure_harness \
//       --pack /mnt/user/ai/iq3xxs-15 --socket <run>/helper.sock \
//       --activations <run>/moe \
//       --out build-mixed-gpu/p2-measure-harness/measure.json \
//       [--experts ../Swift-…gguf] [--layers 32-47] [--fixtures all] [--max-tokens 2048] [--arm-groups]
//       [--eps 1e-12] [--slots 0] [--reserve-mib 512] [--max-fill-pairs 0] [--max-device-mib 4000]
//       [--rows-timeout-ms 120000] [--fill-timeout-ms 600000] [--summary-only] [--worst 20] [--device 0]
//
// THE BLOB FILE (the same file the helper maps, resolved the same way - p2_measure.hpp section 6):
// `--experts PATH` joins onto the pack dir exactly as the helper joins it, so the published packs'
// experts, which live in the model GGUF one level up, are named `--experts ../<name>.gguf` (the frozen
// acceptance driver's HELPER_EXPERTS, run_acceptance.py:70).  With no --experts the harness takes
// <pack>/experts.bin, and when a pack has none it resolves the GGUF named by native_experts.txt's
// header, beside the pack.  A GGUF-mapped pack has no packed blob region, so every (layer, expert) row
// is assembled per role at the helper's own offsets (helper_main.cpp:226-247) - not a second reading of
// the manifest.
//
// THE CAPTURE (the parent's part, before the harness): the engine's own opt-in dump (prefill.cpp:4010-4024),
// one file per (prompt, layer), named <fixture-id>.<layer>.bin in --activations DIR:
//
//   STRATA_DUMP_MOE_INPUT=<run>/moe/<fixture-id>.<layer>.bin STRATA_DUMP_MOE_LAYER=<layer>  ... one request
//
// The dump fires once per engine process, so the grid is one short request per (fixture, layer) with the
// fixture's pinned text (the texts and their FNV pins are in p2_measure.cpp and pinned by p2_measure_test).
// `--activations synthetic:SEED` is a deterministic FALLBACK the design marks INFERIOR (§2 line 35 asks for
// real rows); every output of a synthetic run says so in `activation_mode`.
//
// Side A - the primary's MMQ path, on THIS process's device: `mmq::quantize_scatter` (the producer a real
// prefill chunk runs, prefill.cpp:4131; STRATA_QUANT_GATHER=1 selects the gather form, mirroring prefill's
// own switch) + the 16-expert gather-group products exactly as prefill builds them (bounds prefill.cpp:
// 4135-4142, gather 4549-4567, products 4663-4686), on the blob bytes read from the pack.  It links the
// SAME library the engine links (strata_mmq) - no engine-source edit, no engine hook, no dispatch wiring:
// src/prefill/prefill.cpp and src/program/generate.cpp are untouched, and nothing here arms a tier.
// Side B - the helper's prompt-row path in the REAL `strata-expert-helper` process: the engine's OWN
// ExpertHelperClient (src/core/expert_helper_socket.cpp) over the REAL kBeginP/kRowsP frames; begin_prompt
// builds the frame and quantizes the activations with quantize_q8_1_cpu, finish_prompt validates the reply.
//
// The arm term (--arm-groups): the claimed pairs have NO arm MMQ row (the arm sends exactly those pairs to
// the helper), so the design's third deviation source (line 22, removal recomposes the 16-expert groups)
// is measured on the rows the ARM KEEPS LOCAL: the same routed pair's MMQ row with the layer's groups as
// they are today, and with the claimed pairs removed.  It is a separate record family (`recompose`), never
// mixed into the gate rows.
//
// Typed refusals, never a hang and never a silent zero: no helper socket, a LEGACY helper (features=0 -
// nothing is sent), a fill that fails, a pack that is not native / not 2560x640 / Hadamard-folded /
// NVFP4-tailed / without a grouped kernel / without an MMQ product, an activation file missing or short,
// a device-memory need above --max-device-mib (checked BEFORE the first cudaMalloc), a mid-run channel
// loss (the client's rule: no reconnect - the run stops with its records written).
#include "p2_measure.hpp"

#include "strata/core/expert_helper_socket.hpp"
#include "strata/artifact/gguf_reader.hpp"
#include "strata/kernels/cpu/expert_layout.hpp"
#include "strata/kernels/iq_kernels.hpp"
#include "strata/prefill/moe_mmq.hpp"

#include "ggml.h"

#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <functional>
#include <map>
#include <set>
#include <string>
#include <vector>

namespace mmq = strata::prefill::mmq;
namespace cpu = strata::kernels::cpu;
namespace eh = strata::core::expert_helper;
using strata::core::ExpertHelperClient;
using strata::core::ExpertHelperPromptPlan;
using namespace strata::tools::p2;

namespace {

// The group geometry and the activation tail come from p2_measure.hpp section 7b (single-sourced, and
// pinned by the CPU suite's section (11)); the harness allocates, memsets and writes ONLY through those.
constexpr int64_t MMQ_GROUP = kMmqGroup;   // prefill.cpp:1152
constexpr size_t MMQ_TAIL = kMmqTail;      // prefill.cpp:1156 (zeroed after a group's last expert)
constexpr int64_t kActTailRows = 16;       // mmq_act_tail_rows(): MMQ reads whole 16-row M tiles
static_assert(kActTailRows == 16 && MMQ_GROUP == 16 && MMQ_TAIL == 4096, "section 7b constants");

void usage() {
    std::printf(
        "p2_measure_harness --pack DIR --socket PATH --activations DIR|synthetic:SEED --out JSON\n"
        "  the helper-vs-MMQ prompt-row DEVIATION MEASUREMENT (r5-three-gpu-design.md §2, gate line 81).\n"
        "  NO tolerance and NO verdict anywhere: the band belongs to the user; the parent applies it after.\n"
        "\n"
        "  --pack DIR          the native pack dir (native_experts.txt), as the helper's --pack\n"
        "  --experts PATH      the blob file, with the HELPER's semantics: the value JOINS onto the pack\n"
        "                      dir (helper_main.cpp:708-712), so a relative PATH resolves against the pack\n"
        "                      (--experts ../model.gguf is the published layout; an absolute value joins\n"
        "                      too, exactly as the helper joins it).  Absent: <pack>/experts.bin, else the\n"
        "                      GGUF native_experts.txt's header names, beside the pack.  The bytes the\n"
        "                      harness reads are the helper's own offsets for the file it lands on.\n"
        "  --socket PATH       the helper's AF_UNIX path (start strata-expert-helper on it first)\n"
        "  --activations SRC   DIR of <fixture-id>.<layer>.bin dumps (the STRATA_DUMP_MOE_INPUT format),\n"
        "                      or synthetic:SEED (documented INFERIOR fallback: real rows are what §2 asks)\n"
        "  --out PATH          the machine-readable JSON; the human table goes to PATH.table.txt\n"
        "  --layers A-B        layer window to measure (default 32-47: the helper's window on the split32 box)\n"
        "  --fixtures LIST     'all' or comma-separated fixture ids (default all 8)\n"
        "  --max-tokens N      use only the first N token rows of each dump (0 = all; default 2048)\n"
        "  --arm-groups        also compute the arm's MMQ rows (claimed pairs removed) on the LOCAL rows\n"
        "  --eps X             the max_rel denominator floor, recorded per run (default 1e-12)\n"
        "  --slots N           the hello slot request (0 = the helper's budget decides)\n"
        "  --reserve-mib N     the helper's VRAM reserve rule (default 512)\n"
        "  --max-fill-pairs N  cap the ranked fill list (0 = every pair the run touches)\n"
        "  --max-device-mib N  refuse BEFORE any cudaMalloc when this run's device need exceeds N (4000)\n"
        "  --rows-timeout-ms N per-layer rows reply deadline (default 120000)\n"
        "  --fill-timeout-ms N the fill reply deadline (default 600000)\n"
        "  --summary-only      emit the aggregates and the worst rows, not every row record\n"
        "  --worst N           rows in the human table's worst list (default 20)\n"
        "  --device N          the CUDA device this harness computes on (default 0)\n");
}

struct Opts {
    std::string pack, socket, activations, out;
    std::string experts;                  // empty: <pack>/experts.bin, else the header's GGUF beside the pack
    int64_t layer_lo = kLayerLo, layer_hi = kLayerHi;
    std::string fixtures = "all";
    int64_t max_tokens = 2048;
    bool arm_groups = false;
    double eps = kEpsDefault;
    int64_t worst = 20;
    bool summary_only = false;
    uint64_t seed = 271828;
    bool synthetic = false;
    HelperSetup hs;
    int64_t max_fill_pairs = 0;
    int64_t max_device_mib = 4000;
    int64_t device = 0;
};

bool parse(int argc, char** argv, Opts& o, std::string& err) {
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        auto val = [&](std::string& v) -> bool {
            if (i + 1 >= argc) { err = a + " needs a value"; return false; }
            v = argv[++i];
            return true;
        };
        std::string v;
        if (a == "--pack") { if (!val(v)) return false; o.pack = v; }
        else if (a == "--experts") { if (!val(v)) return false; o.experts = v; }
        else if (a == "--socket") { if (!val(v)) return false; o.socket = v; }
        else if (a == "--activations") {
            if (!val(v)) return false;
            if (v.rfind("synthetic:", 0) == 0) { o.synthetic = true; o.seed = (uint64_t) std::atoll(v.c_str() + 10); }
            else o.activations = v;
        }
        else if (a == "--out") { if (!val(v)) return false; o.out = v; }
        else if (a == "--layers") {
            if (!val(v)) return false;
            const auto dash = v.find('-');
            if (dash == std::string::npos) { err = "--layers wants A-B"; return false; }
            o.layer_lo = std::atoll(v.substr(0, dash).c_str());
            o.layer_hi = std::atoll(v.substr(dash + 1).c_str());
        }
        else if (a == "--fixtures") { if (!val(v)) return false; o.fixtures = v; }
        else if (a == "--max-tokens") { if (!val(v)) return false; o.max_tokens = std::atoll(v.c_str()); }
        else if (a == "--arm-groups") o.arm_groups = true;
        else if (a == "--eps") { if (!val(v)) return false; o.eps = std::atof(v.c_str()); }
        else if (a == "--slots") { if (!val(v)) return false; o.hs.slots = std::atoll(v.c_str()); }
        else if (a == "--reserve-mib") { if (!val(v)) return false; o.hs.reserve_mib = std::atoll(v.c_str()); }
        else if (a == "--max-fill-pairs") { if (!val(v)) return false; o.max_fill_pairs = std::atoll(v.c_str()); }
        else if (a == "--max-device-mib") { if (!val(v)) return false; o.max_device_mib = std::atoll(v.c_str()); }
        else if (a == "--rows-timeout-ms") { if (!val(v)) return false; o.hs.rows_timeout_ms = std::atoll(v.c_str()); }
        else if (a == "--fill-timeout-ms") { if (!val(v)) return false; o.hs.fill_timeout_ms = std::atoll(v.c_str()); }
        else if (a == "--summary-only") o.summary_only = true;
        else if (a == "--worst") { if (!val(v)) return false; o.worst = std::atoll(v.c_str()); }
        else if (a == "--device") { if (!val(v)) return false; o.device = std::atoll(v.c_str()); }
        else if (a == "--help") { usage(); std::exit(0); }
        else { err = "unknown argument '" + a + "' (--help for the list)"; return false; }
    }
    if (o.pack.empty() || o.socket.empty() || o.out.empty() || (o.activations.empty() && !o.synthetic)) {
        err = "--pack, --socket, --activations and --out are all required";
        return false;
    }
    if (o.eps <= 0.0) { err = "--eps must be > 0"; return false; }
    if (o.layer_lo < kLayerLo || o.layer_hi > kLayerHi || o.layer_lo > o.layer_hi) {
        err = "--layers must be inside the helper's window 32-47";
        return false;
    }
    if (o.max_tokens < 0) { err = "--max-tokens must be >= 0"; return false; }
    if (!o.synthetic && o.max_tokens == 0) {
        err = "--max-tokens 0 (the whole dump) is refused: an 8192-token canonical dump needs ~1.3 GiB of "
             "device buffers for one layer's row set; pass an explicit --max-tokens (default 2048)";
        return false;
    }
    return true;
}

int fixture_index_of(const std::string& id) {
    for (size_t i = 0; i < fixtures().size(); ++i)
        if (id == fixtures()[i].id) return (int) i;
    return -1;
}

std::vector<std::string> chosen_fixtures(const std::string& spec, std::string& err) {
    std::vector<std::string> ids;
    if (spec == "all") {
        for (const Fixture& f : fixtures()) ids.push_back(f.id);
        return ids;
    }
    size_t p = 0;
    while (p < spec.size()) {
        size_t q = spec.find(',', p);
        if (q == std::string::npos) q = spec.size();
        std::string id = spec.substr(p, q - p);
        const auto s = id.find_first_not_of(" \t");
        const auto e = id.find_last_not_of(" \t");
        id = (s == std::string::npos) ? std::string() : id.substr(s, e - s + 1);
        if (!fixture_by_id(id)) { err = "no fixture named '" + id + "' (--fixtures all lists them)"; return {}; }
        ids.push_back(id);
        p = q + 1;
    }
    return ids;
}

bool ck(cudaError_t e, const char* what, std::string& err) {
    if (e == cudaSuccess) return true;
    err = std::string(what) + ": " + cudaGetErrorString(e);
    return false;
}

/// Write both outputs: the machine-readable JSON to --out, the human table to --out.table.txt.
/// A failure prints the typed reason (never a silent partial document: the JSON is written first).
void emit_report(const Report& rep, const Opts& o) {
    {
        std::ofstream j(o.out, std::ios::binary);
        if (!j) std::fprintf(stderr, "p2_measure_harness: REFUSAL (output): cannot write %s\n", o.out.c_str());
        else { j << to_json(rep); j.flush(); if (!j) std::fprintf(stderr, "p2_measure_harness: REFUSAL (output): write failed for %s\n", o.out.c_str()); }
    }
    const std::string tpath = o.out + ".table.txt";
    std::ofstream t(tpath, std::ios::binary);
    if (!t) { std::fprintf(stderr, "p2_measure_harness: REFUSAL (output): cannot write %s\n", tpath.c_str()); return; }
    t << to_table(rep, o.worst);
    t.flush();
    if (!t) std::fprintf(stderr, "p2_measure_harness: REFUSAL (output): write failed for %s\n", tpath.c_str());
}

// ============================== Side A: the MMQ path, on the device ==============================
//
// EXACTLY the sequence a real prefill chunk runs for this layer, over the layer's FULL routed row set
// (the composition a real chunk runs today), or with the claimed pairs removed (--arm-groups: the
// composition the arm runs).  Mirrored line by line:
//   producer ......... prefill.cpp:4115-4131 (quantize_scatter over slot/src; STRATA_QUANT_GATHER=1: the
//                      gather form quantize(x, src, ...) - the scatter kernel writes at slot[t*k+k] with
//                      no skip for removed entries, so the removed-entry shape is only expressible through
//                      src, exactly as prefill's own gather switch does it)
//   off/slot/src ..... prefill.cpp:3954-3976 (expert-id-order offsets, the inverse walk)
//   order ............ prefill.cpp:3979-3982 (cnt > 0, ascending expert id)
//   bounds ........... prefill.cpp:4135-4142 (absolute per order entry + rows_local, group-relative blocks)
//   group gather ..... prefill.cpp:4528-4567 (gather_native_group over the staged blobs, MMQ_TAIL zeroed
//                      after the group's last gathered expert; the per-expert fallback when not aligned)
//   products ......... prefill.cpp:4635-4686 (Product gate/up, swiglu(interleaved = !native), the
//                      intermediate re-quantized to q8_1, Product down)
// The staged walk's ring (prefill.cpp STAGE slots, groups of 16) is a 32-slot device ring here: a group's
// copy is enqueued before its gather, and slot reuse only ever follows an already-enqueued gather read.

struct DevMem {
    std::vector<void*> p;
    ~DevMem() { for (void* q : p) if (q) cudaFree(q); }
    void* take(size_t bytes, std::string& err) {
        void* q = nullptr;
        if (!ck(cudaMalloc(&q, bytes ? bytes : 16), "cudaMalloc", err)) return nullptr;
        p.push_back(q);
        return q;
    }
};

struct MmqRows {
    std::vector<float> dm;             // rows_local * N floats, the expert-order down rows (Dm)
    std::vector<int32_t> pair2row;     // T*K, ENTRY-keyed: the Dm ROW of routed entry i (slot[i]), -1 if dropped
    std::vector<int32_t> order;        // the MMQ group order: cnt > 0 experts, ascending id
    std::vector<int32_t> cnt;          // n_expert counts THIS run uses
    int64_t rows_local = 0;
    int64_t nr_max = 0;                // the most rows one group computes
};

/// The rows this run keeps, in the expert-order layout the MMQ consumes (the prefill.cpp:3954-3976 walk,
/// with the claimed entries skipped).  `claims` (T*K bytes, 1 = the pair goes to the helper) is null for
/// the default composition.
bool build_geometry(const Activations& act, const uint8_t* claims, MmqRows& g, std::string& err) {
    const int64_t T = act.T, nent = T * kK;
    g.cnt.assign((size_t) kNE, 0);
    for (int64_t i = 0; i < nent; ++i) {
        if (claims != nullptr && claims[(size_t) i]) continue;
        const int32_t e = act.ids[(size_t) i];
        if (e < 0 || e >= (int32_t) kNE) { err = "routed id out of range"; return false; }
        ++g.cnt[(size_t) e];
    }
    std::vector<int64_t> off((size_t) kNE + 1, 0);
    int64_t r = 0;
    for (int64_t e = 0; e < kNE; ++e) { off[(size_t) e] = r; r += g.cnt[(size_t) e]; }
    off[(size_t) kNE] = r;
    g.rows_local = r;
    if (g.rows_local <= 0) { err = "this run keeps no rows"; return false; }
    g.order.clear();
    for (int64_t e = 0; e < kNE; ++e) if (g.cnt[(size_t) e] > 0) g.order.push_back((int32_t) e);
    const size_t n = g.order.size(), ng = (n + MMQ_GROUP - 1) / MMQ_GROUP;
    g.nr_max = 0;
    for (size_t gg = 0; gg < ng; ++gg) {
        const size_t j0 = gg * MMQ_GROUP;
        const size_t j1 = std::min(n, j0 + MMQ_GROUP) - 1;
        g.nr_max = std::max<int64_t>(g.nr_max, off[(size_t) g.order[j1] + 1] - off[(size_t) g.order[j0]]);
    }
    // slot/src: the same inverse walk prefill builds (slot[i] = the row of entry i, src[row] = its token)
    std::vector<int32_t> slot((size_t) nent, -1), src((size_t) g.rows_local, 0);
    std::vector<int64_t> fill(off.begin(), off.end() - 1);
    for (int64_t i = 0; i < nent; ++i) {
        if (claims != nullptr && claims[(size_t) i]) continue;
        const int32_t e = act.ids[(size_t) i];
        const int32_t p = (int32_t) fill[(size_t) e]++;
        slot[(size_t) i] = p;
        src[(size_t) p] = (int32_t) (i / kK);
    }
    g.pair2row.assign((size_t) nent, -1);
    // slot[i] IS the row (fill starts at off[expert], so p = off[expert] + the expert's ordinal).  Adding
    // off[expert] a second time inflates every row by its expert's offset - up to 2 * rows_local - which is
    // what put "side A row 3810" past a 2250-row buffer (the 2026-10-29 live crash, attempt 8): the gate
    // loop and the arm block read dm at these indices.  The row of entry i is slot[i], nothing more.
    for (int64_t i = 0; i < nent; ++i)
        g.pair2row[(size_t) i] = slot[(size_t) i];
    return true;
}

struct MmqDevice {                   // the device-side staging, sized by the layer's row counts
    void* x = nullptr;              // T * N floats
    void* xq = nullptr;             // q8_bytes(rows_local + 16, N)  (the tail: the 16-row M tile past row n-1)
    void* gu = nullptr;             // rows_local * 1280 floats
    void* dm = nullptr;             // rows_local * N floats
    void* h = nullptr;              // nr_max * 640 floats
    void* hq = nullptr;             // q8_bytes(nr_max + 16, 640)
    void* grp_gu = nullptr;         // mmq_group_gu_bytes(mmq_gub_l, over_gu)
    void* grp_d = nullptr;          // mmq_group_d_bytes(mmq_db_l, over_d)
    void* grp_tail = nullptr;       // mmq_tail_words_bytes(): the NVFP4 tail words, THEIR OWN buffer
    void* stage = nullptr;          // kStageRingSlots blobs of the layer's blob size
    void* bounds = nullptr;         // n + 1 + ng * (MMQ_GROUP + 1) int32
    void* ids = nullptr;            // rows_local int32 (identity)
    void* slot = nullptr;           // T*K int32
    void* src = nullptr;            // rows_local int32
};

/// cudaStreamSynchronize + cudaGetLastError + the typed FAULT-AT line for ONE op of main's OWN prologue
/// (the ring probe).  `out`, when given, takes the filled probe (with `fault`) and the printer stays quiet:
/// the caller prints the line through the run's checkpoint, which knows the fixture id.
/// A sticky device fault surfaces at the FIRST API call after it - so the sync must be the OP'S OWN,
/// taken immediately after it, which is exactly what this function does.
bool sync_op(cudaStream_t cs, const char* fid, int64_t layer, const char* op, long long q, long long slot,
             long long entry, const void* src, const void* dst, unsigned long long bytes, std::string& err,
             OpProbe* out = nullptr) {
    OpProbe pr;
    pr.op = op; pr.q = q; pr.slot = slot; pr.entry = entry;
    pr.src = src; pr.dst = dst; pr.bytes = bytes; pr.stream = (const void*) cs;
    cudaPointerAttributes at{};
    if (src != nullptr) {
        const cudaError_t rc = cudaPointerGetAttributes(&at, src);
        pr.src_rc = cudaGetErrorString(rc);
        pr.src_attr = cuda_memory_type_name(rc == cudaSuccess ? (int) at.type : -1);
    }
    if (dst != nullptr) {
        const cudaError_t rc = cudaPointerGetAttributes(&at, dst);
        pr.dst_rc = cudaGetErrorString(rc);
        pr.dst_attr = cuda_memory_type_name(rc == cudaSuccess ? (int) at.type : -1);
    }
    const cudaError_t se = cudaStreamSynchronize(cs);      // SYNC: the op's completion IS its verdict
    const cudaError_t le = cudaGetLastError();
    const cudaError_t e = (se != cudaSuccess) ? se : le;
    if (e == cudaSuccess) {
        if (out != nullptr) *out = pr;
        return true;
    }
    pr.fault = cudaGetErrorString(e);
    err = op_label(op, q, slot, entry) + ": " + pr.fault + " (at this op's own sync)";
    if (out != nullptr) {
        *out = pr;                                          // the caller prints it, with the fixture id
    } else {
        std::fprintf(stderr, "p2_measure_harness: %s\n", format_fault_line(fid, layer, pr, pr.fault).c_str());
    }
    return false;
}

/// One layer's MMQ run on the device.  The exact prefill sequence; `claims` null = the full row set.
/// `off` is the geometry's expert row offsets (the same table build_geometry walked), `layer` the layer
/// whose blobs the gather reads.  `ckpt` is the run's checkpoint.
///
/// fix8, the point of the whole function: EVERY async op of this run ends with its OWN
/// cudaStreamSynchronize + cudaGetLastError (sync_op), and the result is handed to `ckpt` with the op's
/// parameters.  A device fault is asynchronous and sticky: it is recorded when the GPU finishes the
/// faulting op and SURFACES at the first CUDA API call after that.  The fix5 checkpoints called
/// cudaGetLastError WITHOUT a sync, so they could only pop an error the driver had already recorded - an
/// unsurfaced async fault passed every one of them in silence, and the live L34 fault surfaced later, at
/// the q=0 blob upload, which is where it was REPORTED and not where it was CAUSED.  With a sync after
/// every op, the first call to see the fault is the sync of the op that caused it.  Latency is not a
/// concern here: this is a measurement harness, not a serving path.
bool run_mmq(int64_t layer, const Activations& act, const MmqRows& g, const std::vector<int64_t>& off,
             const cpu::NativeFmt& f, size_t mmq_gub, size_t mmq_db, size_t blob_bytes, std::ifstream& pk,
             const BlobSource& blob, uint64_t pack_bytes,
             mmq::Context& ctx, MmqDevice& d, cudaStream_t cs, std::vector<float>& out_dm, std::string& err,
             const std::function<bool(const char*, const OpProbe&)>& ckpt) {
    const int64_t T = act.T, rows = g.rows_local;
    const size_t n = g.order.size(), ng = (n + MMQ_GROUP - 1) / MMQ_GROUP;
    // the per-op probe: sync this op, name it, hand the parameters to the run's checkpoint.  False = the
    // op faulted at its own sync; the run stops with the SAME label in its refusal.
    // fix9: `wgeom`/`walloc` carry the last PRODUCT launch's write geometry (section 7d) into the fault
    // line, because a product's copy-style parameters are all in-bounds and tell nothing: the kernel
    // writes at ids[col] rows from the pointer, so the pointer line shows an INTENT inside the buffer
    // while the write is outside it.  Non-product ops clear it and keep fix8's line shape.
    ProductGeom wgeom{};
    unsigned long long walloc = 0;
    auto sync_probe = [&](const char* op, long long q, long long slot, long long entry,
                          const void* src, const void* dst, unsigned long long bytes) -> bool {
        OpProbe pr;
        const bool clean = sync_op(cs, nullptr, layer, op, q, slot, entry, src, dst, bytes, err, &pr);
        pr.w_first = wgeom.valid ? product_write_first_row(wgeom) : 0;
        pr.w_last  = wgeom.valid ? product_write_last_row(wgeom)  : 0;
        pr.alloc   = wgeom.valid ? walloc : 0;
        if (!clean) {
            (void) ckpt(op_label(op, q, slot, entry).c_str(), pr);      // prints FAULT-AT with the fixture
            return false;
        }
        return ckpt(op, pr);                                             // the clean path prints nothing
    };
    if (!ck(cudaMemcpyAsync(d.x, act.x.data(), (size_t) (T * kN) * sizeof(float), cudaMemcpyHostToDevice, cs),
            "x upload", err)) return false;
    if (!sync_probe("x upload", -1, -1, -1, act.x.data(), d.x, (size_t) (T * kN) * sizeof(float)))
        return false;
    // the producer: q8_1 activations in expert order (prefill.cpp:4115-4131)
    const bool gather_form = [] {
        static const bool on = std::getenv("STRATA_QUANT_GATHER") != nullptr;
        return on;
    }();
    if (gather_form)
        mmq::quantize((const float*) d.x, (const int32_t*) d.src, d.xq, f.gu_type, kN, kN, rows, cs, nullptr);
    else
        mmq::quantize_scatter((const float*) d.x, (const int32_t*) d.slot, (const int32_t*) d.src, d.xq,
                              f.gu_type, kN, kN, T, (int) kK, cs);
    if (const cudaError_t e = cudaGetLastError(); e != cudaSuccess) {
        err = std::string("the activation quantizer: ") + cudaGetErrorString(e);
        return false;
    }
    if (!sync_probe("act quantize", -1, -1, -1, d.x, d.xq, mmq::q8_bytes(rows, kN))) return false;
    // the bounds (prefill.cpp:4135-4142): absolute per order entry + rows_local, group-relative blocks
    std::vector<int32_t> bounds(n + 1 + ng * (MMQ_GROUP + 1));
    for (size_t j = 0; j < n; ++j) bounds[j] = (int32_t) off[(size_t) g.order[j]];
    bounds[n] = (int32_t) rows;
    for (size_t gg = 0; gg < ng; ++gg)
        for (size_t i = 0; i <= MMQ_GROUP; ++i)
            bounds[n + 1 + gg * (MMQ_GROUP + 1) + i] =
                bounds[std::min(n, gg * MMQ_GROUP + i)] - bounds[gg * MMQ_GROUP];
    if (!ck(cudaMemcpyAsync(d.bounds, bounds.data(), bounds.size() * sizeof(int32_t), cudaMemcpyHostToDevice, cs),
            "bounds upload", err)) return false;
    if (!sync_probe("bounds upload", -1, -1, -1, bounds.data(), d.bounds,
                    (unsigned long long) (bounds.size() * sizeof(int32_t)))) return false;
    // the staged walk: the group's 16 blobs H2D into the ring, then ONE gather launch (prefill.cpp:4528-4567)
    std::vector<uint8_t> blob_host(blob_bytes);
    for (size_t gg = 0; gg < ng; ++gg) {
        const size_t j0 = gg * MMQ_GROUP, j1 = std::min(n, j0 + MMQ_GROUP);
        const int ngx = (int) (j1 - j0);
        mmq::GatherGroup group;
        for (int q = 0; q < ngx; ++q) {
            const int32_t e = g.order[j0 + (size_t) q];
            // the row's bytes, at the helper's own offsets: the packed blob's blob_offset(), or the
            // gate|up|down slices of a GGUF-mapped pack (p2_measure.hpp section 6, helper_main.cpp:226-247)
            if (!read_blob_row(pk, cpu::expert_layout(), blob, layer, (int64_t) e, blob_host.data(),
                               pack_bytes, err)) {
                err = "layer " + std::to_string(layer) + " expert " + std::to_string(e) + ": " + err;
                return false;
            }
            uint8_t* dst = (uint8_t*) d.stage + (size_t) ((j0 + (size_t) q) % (size_t) kStageRingSlots) * blob_bytes;
            // fix6 audit of this call's three parameters (the pinned attempt-10 fault site):
            //   DESTINATION: slot index < 32, stride == size == the per-layer blob_bytes, so the extent is
            //     31*blob + blob = 32*blob = stage_ring_bytes(blob_bytes) = the allocation (main.cpp:857).
            //   COPY SIZE: the run_mmq parameter blob_bytes, passed from the call site as
            //     lay.blob_bytes(layer) - THIS layer's number, not the window max, not a cached value.
            //   HOST SOURCE: blob_host is std::vector<uint8_t>(blob_bytes) allocated IN THIS run_mmq call
            //     (per layer, fresh) and read_blob_row writes exactly the role slices (their sum is
            //     checked equal to nb = this layer's blob) - the source extent == the copy size.
            // All three audit clean in code; the typed STAGE line in the prologue prints the numbers.
            // fix8, the label fix: the per-slot label goes INTO the call that can fail, so a refusal
            // printed AT the upload names the expert, the slot and the entry.  fix7 put the label on the
            // checkpoint AFTER this call - and this call is the one that fails when a sticky fault
            // surfaces here (a sticky fault returns at the first API call after it), so the label never
            // ran and attempts 10-12 printed the bare literal "blob upload".
            const size_t slot_idx = (size_t) ((j0 + (size_t) q) % (size_t) kStageRingSlots);
            const std::string lbl = op_label("blob upload", (long long) q, (long long) slot_idx,
                                             (long long) (j0 + (size_t) q));
            if (!ck(cudaMemcpyAsync(dst, blob_host.data(), blob_bytes, cudaMemcpyHostToDevice, cs),
                    lbl.c_str(), err)) return false;
            // the op's OWN sync, immediately after it: this is the call that SEES a fault this op caused
            // (fix5's cudaGetLastError-only checkpoints could not see an unsurfaced async fault)
            if (!sync_probe("blob upload", (long long) q, (long long) slot_idx, (long long) (j0 + (size_t) q),
                            blob_host.data(), dst, (unsigned long long) blob_bytes)) return false;
            group.blob[q] = dst;
        }
        group.n = ngx;
        const size_t r0 = (size_t) bounds[j0];
        const int64_t nr = (int64_t) bounds[std::min(n, j0 + ngx)] - (int64_t) r0;
        int64_t maxr = 0;
        for (int q = 0; q < ngx; ++q) maxr = std::max<int64_t>(maxr, g.cnt[(size_t) g.order[j0 + (size_t) q]]);
        const bool grouped = mmq::gather_native_group(group, f.up_off, mmq_gub / 2, f.down_off, mmq_db,
                                                      d.grp_gu, mmq_gub, d.grp_d, mmq_db, cs, f.tail_off,
                                                      // the tail words go in their OWN buffer (mmq_tail_words_bytes,
                                                      // section 7b) at the engine's 16-byte stride: writing them at
                                                      // grp_d + 16*mmq_db + MMQ_TAIL puts up to 16*16 = 256 bytes of
                                                      // kernel writes PAST that allocation's end (the engine keeps
                                                      // them in a dedicated grp_tail, prefill.cpp:1645)
                                                      f.tail_off ? d.grp_tail : nullptr,
                                                      MMQ_TAIL);
        if (!sync_probe("gather group", -1, -1, (long long) j0, group.blob[0], d.grp_gu,
                        (unsigned long long) gather_write_bytes(ngx, mmq_gub, MMQ_TAIL))) return false;
        if (!grouped) {   // not 16-byte aligned: one gather per expert (prefill.cpp:4552-4558)
            for (int q = 0; q < ngx; ++q) {
                const uint8_t* b = group.blob[q];
                mmq::gather_native(b, b + f.up_off, mmq_gub / 2, b + f.down_off, mmq_db,
                                   (uint8_t*) d.grp_gu + (size_t) q * mmq_gub,
                                   (uint8_t*) d.grp_d + (size_t) q * mmq_db, cs,
                                   f.tail_off ? b + f.tail_off : nullptr,
                                   // 16 bytes per expert, the engine's stride (m.grp_tail + i*4 floats,
                                   // prefill.cpp:4557; the MMQ kernel reads tails[4*q], moe_mmq.cu:158) - a
                                   // 4-byte stride overlapped the neighbours' words
                                   f.tail_off ? (uint8_t*) d.grp_tail + 16 * (size_t) q : nullptr,
                                   q + 1 == ngx ? MMQ_TAIL : 0);
            }
            if (!sync_probe("gather per-expert", -1, -1, (long long) j0, group.blob[0], d.grp_gu,
                            (unsigned long long) gather_write_bytes(ngx, mmq_gub, MMQ_TAIL))) return false;
        }
        // the group's products (prefill.cpp:4663-4686)
        // fix9 THE FIX, the gate/up product's dst base: its bounds are ABSOLUTE (`bounds + j0`, the
        // CHUNK's row indices) and ids is the identity, and the MMQ kernel writes at
        // `dst[ids_dst[col_low + jt*J + j]*ld_dst + i]` with col from expert_bounds (mmq.cuh:1036-1059,
        // 1153, 1375) - so its dst must be the buffer's BASE, exactly as the engine passes it
        // (prefill.cpp:4666 `gu.dst = m.GU`).  This site passed `d.gu + r0*1280`, the DOWN site's
        // convention (whose bounds ARE group-relative), so the group's products landed r0 rows past
        // their place: row r0 + col for col in [r0, r0+nr).  Every group with r0 > 0 wrote outside the
        // gate/up buffer as soon as r0 + nr exceeded its rows - the live L34 fault at group j0=176
        // (r0 1498, nr 137: the write reached gu + 16,040,960 of an 11,520,000 B buffer) is that
        // overrun landing in UNMAPPED VA; L32/L33 escaped only because their (larger) overshoot,
        // 23,019,520 B at L32's last group (r0 2247), stayed in the mapped VA of the buffers allocated
        // after gu, so it corrupted silently.  The read side of the same bug: swiglu takes the group's
        // OWN row span (gu + r0*1280), which under the old call held the products of rows [0, nr) -
        // so the pre-fix9 side-A numbers of every group past group 0 are INVALID (group 0, r0 0, is
        // correct).  The fit check below refuses such a launch on the CPU (typed, exit 4) instead of
        // handing it to the device.
        mmq::Product gu;
        gu.w = d.grp_gu; gu.type = f.gu_type; gu.w_rows = 1280; gu.w_cols = kN; gu.expert_bytes = mmq_gub;
        gu.n = ngx; gu.xq = d.xq; gu.bounds = (int32_t*) d.bounds + j0; gu.ids = (int32_t*) d.ids;
        gu.total_rows = rows; gu.max_rows = maxr; gu.dst = (float*) d.gu; gu.ld_dst = 1280;
        wgeom = ProductGeom{true, true, (long long) r0, (long long) nr, 1280, 0};
        walloc = (unsigned long long) mmq_gu_buffer_bytes(rows);
        if (!product_write_fits(wgeom, rows)) {
            wgeom = ProductGeom{};
            err = product_write_refusal("gate/up", true, (long long) j0, (long long) r0, (long long) nr,
                                        1280, 0, (long long) rows, walloc);
            return false;
        }
        ctx.run(gu, cs);
        if (!sync_probe("gu product", -1, -1, (long long) j0, d.grp_gu, gu.dst,
                        (unsigned long long) product_write_bytes_from_dst(wgeom))) return false;
        wgeom = ProductGeom{};
        mmq::swiglu((const float*) d.gu + r0 * 1280, (float*) d.h, nr, 640, !cpu::expert_layout().native, cs);
        if (!sync_probe("swiglu", -1, -1, (long long) j0, gu.dst, d.h,
                        (unsigned long long) (nr * 640 * (int64_t) sizeof(float)))) return false;
        mmq::quantize((const float*) d.h, nullptr, d.hq, f.d_type, 640, 640, nr, cs, nullptr);
        if (!sync_probe("h quantize", -1, -1, (long long) j0, d.h, d.hq,
                        (unsigned long long) mmq::q8_bytes(nr, 640))) return false;
        mmq::Product dn;
        dn.w = d.grp_d; dn.type = f.d_type; dn.w_rows = kN; dn.w_cols = 640; dn.expert_bytes = mmq_db;
        dn.n = ngx; dn.xq = d.hq;
        dn.bounds = (int32_t*) d.bounds + n + 1 + gg * (MMQ_GROUP + 1);
        dn.ids = (int32_t*) d.ids; dn.total_rows = nr; dn.max_rows = maxr;
        dn.dst = (float*) d.dm + r0 * kN; dn.ld_dst = kN;
        // fix9: the down product's convention IS the offset pointer (its bounds are the group's, columns
        // 0..nr, ids[c] = c), so the write spans [0, nr) from a pointer r0 rows into the dm buffer:
        // group-relative bounds + base + r0*ld, the engine's own pairing (prefill.cpp:4682).  The same
        // fit check guards it, so an offset/bounds mismatch on this site refuses on the CPU too.
        wgeom = ProductGeom{true, false, (long long) r0, (long long) nr, (long long) kN, (long long) r0};
        walloc = (unsigned long long) mmq_dm_buffer_bytes(rows);
        if (!product_write_fits(wgeom, rows)) {
            wgeom = ProductGeom{};
            err = product_write_refusal("down", false, (long long) j0, (long long) r0, (long long) nr,
                                        (long long) kN, (long long) r0, (long long) rows, walloc);
            return false;
        }
        ctx.run(dn, cs);
        if (!sync_probe("dn product", -1, -1, (long long) j0, d.grp_d, dn.dst,
                        (unsigned long long) product_write_bytes_from_dst(wgeom))) return false;
        wgeom = ProductGeom{};
        if (const cudaError_t e = cudaGetLastError(); e != cudaSuccess) {
            err = std::string("the MMQ launch: ") + cudaGetErrorString(e);
            return false;
        }
    }
    if (!ck(cudaStreamSynchronize(cs), "layer run", err)) return false;
    if (!sync_probe("layer sync", -1, -1, -1, nullptr, nullptr, 0)) return false;
    out_dm.assign((size_t) (rows * kN), 0.0f);
    if (!ck(cudaMemcpy(out_dm.data(), d.dm, out_dm.size() * sizeof(float), cudaMemcpyDeviceToHost),
            "dm readback", err)) return false;
    if (!sync_probe("dm readback", -1, -1, -1, d.dm, out_dm.data(),
                    (unsigned long long) (out_dm.size() * sizeof(float)))) return false;
    return true;
}

}  // namespace

// ==========================================================================================================

int main(int argc, char** argv) {
    std::string err;
    Opts o;
    if (argc > 1 && std::strcmp(argv[1], "--help") == 0) { usage(); return 0; }
    if (!parse(argc, argv, o, err)) {
        std::fprintf(stderr, "p2_measure_harness: REFUSAL (arguments): %s\n", err.c_str());
        return 2;
    }
    const std::vector<std::string> fix = chosen_fixtures(o.fixtures, err);
    if (fix.empty()) {
        std::fprintf(stderr, "p2_measure_harness: REFUSAL (fixtures): %s\n", err.c_str());
        return 2;
    }
    std::string joined;
    for (int i = 1; i < argc; ++i) joined += (i > 1 ? " " : "") + std::string(argv[i]);

    Report rep;
    rep.meta.argv_joined = joined;
    rep.meta.pack_dir = o.pack;
    rep.meta.socket = o.socket;
    rep.meta.eps = o.eps;
    rep.meta.layer_lo = (int32_t) o.layer_lo;
    rep.meta.layer_hi = (int32_t) o.layer_hi;
    rep.meta.max_tokens = o.max_tokens;
    rep.meta.arm_groups = o.arm_groups;
    rep.meta.summary_only = o.summary_only;
    rep.meta.slots = o.hs.slots;
    rep.meta.reserve_mib = o.hs.reserve_mib;
    rep.meta.activation_mode = o.synthetic
        ? ("synthetic:seed=" + std::to_string(o.seed) +
           "  [INFERIOR: design §2 line 35 asks for the engine's real STRATA_DUMP_MOE_INPUT rows]")
        : ("dump:" + o.activations);
    if (o.synthetic)
        rep.meta.notes.push_back("synthetic activations: NOT real prefill rows.  The design's fixture is the "
                                 "engine's STRATA_DUMP_MOE_INPUT dump; numbers from this mode are a labelled "
                                 "smoke test, not the gate measurement.");

    // ---- the pack's layout (the same table the helper loads) ----
    if (!cpu::expert_layout_load(o.pack, kLayers, kNE, err)) {
        std::fprintf(stderr, "p2_measure_harness: REFUSAL (layout): %s\n", err.c_str());
        return 3;
    }
    const auto& lay = cpu::expert_layout();
    if (!lay.native) {
        std::fprintf(stderr, "p2_measure_harness: REFUSAL (layout): the pack is not a native (IQ) expert "
                             "pack (no native_experts.txt) - the helper serves no other pack\n");
        return 3;
    }
    for (int64_t l = o.layer_lo; l <= o.layer_hi; ++l) {
        const auto& ft = lay.fmt[(size_t) l];
        if (ft.n_embd != kN || ft.n_ff != kFF) {
            std::fprintf(stderr, "p2_measure_harness: REFUSAL (layout): layer %lld is %lldx%lld, not 2560x640\n",
                         (long long) l, (long long) ft.n_embd, (long long) ft.n_ff);
            return 3;
        }
        if (ft.had_gu || ft.had_d) {
            std::fprintf(stderr, "p2_measure_harness: REFUSAL (layout): layer %lld is Hadamard-folded; the "
                                 "helper's scope is the non-folded path\n", (long long) l);
            return 3;
        }
        if (ft.tail_off) {
            std::fprintf(stderr, "p2_measure_harness: REFUSAL (layout): layer %lld carries NVFP4 tails; the "
                                 "canonical pack's window does not, and the harness mirrors the non-tail "
                                 "sequence only\n", (long long) l);
            return 3;
        }
        if (!strata::kernels::native_expert_supported(ft.gu_type, ft.d_type, ft.n_embd, ft.n_ff)) {
            std::fprintf(stderr, "p2_measure_harness: REFUSAL (layout): layer %lld has no grouped kernel for "
                                 "its gate/up + down types (the helper refuses it too)\n", (long long) l);
            return 3;
        }
        if (!mmq::built() || !mmq::supported(ft.gu_type) || !mmq::supported(ft.d_type)) {
            std::fprintf(stderr, "p2_measure_harness: REFUSAL (mmq): this build has no MMQ product for layer "
                                 "%lld's types %d/%d - Side A cannot be computed\n",
                         (long long) l, ft.gu_type, ft.d_type);
            return 3;
        }
        if (mmq::fp4_activations(ft.gu_type) || mmq::fp4_activations(ft.d_type)) {
            std::fprintf(stderr, "p2_measure_harness: REFUSAL (mmq): layer %lld would run the NVFP4 w4a4 "
                                 "activation mode; this harness mirrors the q8_1 producer only\n", (long long) l);
            return 3;
        }
        rep.meta.formats.push_back({(int32_t) l, ft.gu_type, ft.d_type});
    }

    // ---- the CUDA device ----
    int n_dev = 0;
    if (cudaGetDeviceCount(&n_dev) != cudaSuccess || o.device >= n_dev) {
        cudaGetLastError();
        std::fprintf(stderr, "p2_measure_harness: REFUSAL (device): no CUDA device %lld (nvidia-smi; the "
                             "helper runs on a device the engine does not use)\n", (long long) o.device);
        return 4;
    }
    std::string derr;
    if (!ck(cudaSetDevice((int) o.device), "cudaSetDevice", derr) ||
        !ck(cudaFree(nullptr), "context init", derr)) {
        std::fprintf(stderr, "p2_measure_harness: REFUSAL (device): %s\n", derr.c_str());
        return 4;
    }
    cudaStream_t cs = nullptr;
    if (!ck(cudaStreamCreateWithFlags(&cs, cudaStreamNonBlocking), "stream", derr)) {
        std::fprintf(stderr, "p2_measure_harness: REFUSAL (device): %s\n", derr.c_str());
        return 4;
    }

    // ---- the pack's blobs (the same file the helper maps, resolved the helper's way) ----
    BlobSource blob;
    if (const int brc = resolve_blob_source(o.pack, o.experts, lay, blob, err)) {
        std::fprintf(stderr, "p2_measure_harness: REFUSAL (pack): %s\n", err.c_str());
        return brc;   // 3: the pack/layout code, unchanged
    }
    rep.meta.blob_file = blob.path;
    rep.meta.blob_mapping = blob.per_role ? "gguf-roles" : "experts.bin";
    if (blob.per_role) {
        // The helper's own per-role rule, checked BEFORE the run: the three slices must sum to the
        // layer's blob (an NVFP4-tailed pack does not, and this harness refuses tails anyway).
        for (int64_t l = o.layer_lo; l <= o.layer_hi; ++l) {
            uint64_t per[3];
            if (!blob_role_bytes(lay, l, per)) {
                std::fprintf(stderr, "p2_measure_harness: REFUSAL (pack): layer %lld has no role slices\n",
                             (long long) l);
                return 3;
            }
            if (per[0] + per[1] + per[2] != lay.blob_bytes(l)) {
                std::fprintf(stderr, "p2_measure_harness: REFUSAL (pack): layer %lld's role slices sum to "
                                     "%llu B, not its blob of %llu B\n", (long long) l,
                             (unsigned long long) (per[0] + per[1] + per[2]),
                             (unsigned long long) lay.blob_bytes(l));
                return 3;
            }
        }
    }
    // ---- the GGUF truth gate (fix7) ----
    // The pack's native_experts.txt is the per-layer shape table EVERY resolution here uses (the
    // loader keys it by layer index and self-checks each line's geometry).  It cannot check the
    // table's agreement with the MODEL.  The 2026-10-29 live anomaly (attempt 11 STAGE lines): the
    // pack's L33 line names 21/42 (1,868,800 B) and L34's 21/20 (2,329,600 B) while the GGUF's own
    // tensor metadata says L33 is the 16/42 shape (1,305,600 B) and L34 the 17/20 shape
    // (1,868,800 B) - the packer wrote the per-layer columns keyed on encounter order.  A run on
    // such a pack measures WRONG byte ranges; the engine refuses the same pack at load
    // (check_experts_gguf).  This gate refuses it HERE, and stamps the per-layer truth
    // header-vs-gguf into the record so pre-fix records are distinguishable.
    std::map<int64_t, std::string> staged_truth;
    std::map<int64_t, uint64_t> staged_gguf_blob;   // fix8: the GGUF's OWN per-layer blob, for the STAGE line
    if (blob.per_role) {
        std::vector<strata::tools::p2::LayerTruth> truth;
        std::string terr;
        if (!strata::tools::p2::check_gguf_truth(blob.path, lay, o.layer_lo, o.layer_hi, truth, terr)) {
            std::fprintf(stderr, "p2_measure_harness: REFUSAL (gguf truth): %s\n", terr.c_str());
            return 3;
        }
        for (const auto& tv : truth) {
            std::fprintf(stderr, "p2_measure_harness: TRUTH L%lld header %d/%d blob %llu B  gguf %d/%d "
                                 "blob %llu B\n",
                         (long long) tv.layer, tv.gu_type, tv.d_type, (unsigned long long) tv.header_blob,
                         tv.gguf_gu_type, tv.gguf_d_type, (unsigned long long) tv.gguf_blob);
            staged_truth[tv.layer] = "types " + std::to_string(tv.gu_type) + "/" + std::to_string(tv.d_type) +
                                     " header-blob " + std::to_string(tv.header_blob) + " gguf-blob " +
                                     std::to_string(tv.gguf_blob);
            staged_gguf_blob[tv.layer] = tv.gguf_blob;   // fix8: the STAGE line's gguf-blob field
            rep.meta.staging.push_back("L" + std::to_string(tv.layer) + " header " + std::to_string(tv.gu_type) +
                                       "/" + std::to_string(tv.d_type) + " " + std::to_string(tv.header_blob) +
                                       " B  gguf " + std::to_string(tv.gguf_gu_type) + "/" +
                                       std::to_string(tv.gguf_d_type) + " " + std::to_string(tv.gguf_blob) + " B");
        }
    } else {
        rep.meta.notes.push_back("packed pack (experts.bin): no GGUF tensor metadata to check - the header's "
                                 "per-layer table is the only shape source");
    }
    std::ifstream pk(blob.path, std::ios::binary);
    if (!pk) {
        std::fprintf(stderr, "p2_measure_harness: REFUSAL (pack): cannot open %s\n", blob.path.c_str());
        return 3;
    }
    pk.seekg(0, std::ios::end);
    const int64_t pack_bytes = (int64_t) pk.tellg();
    pk.seekg(0, std::ios::beg);

    // ---- pass 1: every activation set, the counts, the ranked fill list ----
    std::map<std::string, Activations> act;
    std::map<std::string, LayerCounts> counts;
    std::map<int64_t, std::vector<int32_t>> cnt_sum_by_layer;
    for (const std::string& fid : fix) {
        const int fi = fixture_index_of(fid);
        for (int64_t l = o.layer_lo; l <= o.layer_hi; ++l) {
            Activations a;
            if (o.synthetic) {
                synth_activations(o.seed, fi, l, o.max_tokens > 0 ? std::min<int64_t>(o.max_tokens, 256) : 256, a);
            } else {
                const std::string path = o.activations + "/" + fid + "." + std::to_string(l) + ".bin";
                std::string aerr;
                if (!load_moe_dump(path, o.max_tokens, a, aerr)) {
                    rep.meta.notes.push_back("skip " + fid + " L" + std::to_string(l) + ": " + aerr +
                                             "  [capture it with STRATA_DUMP_MOE_INPUT=" + path +
                                             " STRATA_DUMP_MOE_LAYER=" + std::to_string(l) + " on one request]");
                    continue;
                }
            }
            LayerCounts lc;
            std::string cerr;
            if (build_layer_counts(a, lc, cerr) != eh::kOk) {
                std::fprintf(stderr, "p2_measure_harness: REFUSAL (activations): %s @ layer %lld: %s\n",
                             fid.c_str(), (long long) l, cerr.c_str());
                return 5;
            }
            rep.meta.fixtures_used[fid] = {a.T, 0};
            act[fid + "@" + std::to_string(l)] = std::move(a);
            counts[fid + "@" + std::to_string(l)] = std::move(lc);
            auto& s = cnt_sum_by_layer[l];
            if (s.empty()) s.assign((size_t) kNE, 0);
            const auto& c = counts[fid + "@" + std::to_string(l)].cnt;
            for (int64_t e = 0; e < kNE; ++e) s[(size_t) e] += c[(size_t) e];
        }
    }
    if (act.empty()) {
        std::fprintf(stderr, "p2_measure_harness: REFUSAL (activations): no activation set was readable. "
                             "Pass --activations DIR of <fixture-id>.<layer>.bin dumps (the capture recipe is "
                             "in this file's header) or --activations synthetic:SEED for a labelled smoke run.\n");
        return 5;
    }
    const auto ranked = rank_pairs(cnt_sum_by_layer, o.max_fill_pairs);

    // ---- the device-memory guard, BEFORE the first cudaMalloc of a layer run ----
    // The per-layer footprint: the activation array, the q8_1 rows (+ the 4096 B MMQ may read past the
    // last row), gate/up, down, swiglu, the intermediate's q8_1, the group buffers (+ MMQ_TAIL), the
    // 32-blob staging ring, the bounds, slot/src and the identity rows.  Doubled with --arm-groups.
    int64_t max_T = 0;
    for (const auto& kv : act) max_T = std::max(max_T, kv.second.T);
    const int64_t max_rows = max_T * kK;
    // EVERY per-layer number in this estimate is the window's MAXIMUM, not layer_lo's.  The layer loop
    // allocates each layer's group buffers and staging ring by THAT layer's formats and blob size, so a
    // window whose layers differ (the real pack's gate/up types are 16,17,18,21,22 across 32-47) needs the
    // biggest one here: a per-layer estimate sized by layer_lo under-says the footprint (window-1 defect
    // 2026-10-29: 32 * blob_bytes(layer_lo) = 32 * 2,329,600 B for the ring, while layers 32-47 allocate
    // 32 * their own blob).  The allocation sites stay per-layer (p2_measure_main.cpp: the DevMem block).
    int32_t gu_mx = lay.fmt[(size_t) o.layer_lo].gu_type, d_mx = lay.fmt[(size_t) o.layer_lo].d_type;
    uint64_t blob_mx = max_blob_bytes(lay, o.layer_lo, o.layer_hi);
    for (int64_t l = o.layer_lo; l <= o.layer_hi && (size_t) l < lay.fmt.size(); ++l) {
        const auto& f = lay.fmt[(size_t) l];
        if (mmq::matrix_bytes(f.gu_type, 1280, kN) > mmq::matrix_bytes(gu_mx, 1280, kN)) gu_mx = f.gu_type;
        if (mmq::matrix_bytes(f.d_type, kN, 640) > mmq::matrix_bytes(d_mx, kN, 640)) d_mx = f.d_type;
    }
    const size_t mmq_gub = mmq::matrix_bytes(gu_mx, 1280, kN);
    const size_t mmq_db = mmq::matrix_bytes(d_mx, kN, 640);
    // the group buffers' tail region covers each type's K-tile over-read (section 7b): the estimate takes
    // the window's MAXIMUM over-read, exactly as it takes the window's maximum slot and blob
    size_t over_mx = 0;
    for (int64_t l = o.layer_lo; l <= o.layer_hi && (size_t) l < lay.fmt.size(); ++l) {
        const auto& f = lay.fmt[(size_t) l];
        over_mx = std::max(over_mx, std::max(mmq::overread_bytes(f.gu_type, kN),
                                             mmq::overread_bytes(f.d_type, 640)));
    }
    size_t need = (size_t) (max_T * kN * 4)                                   // x
                + mmq::q8_bytes(max_rows + kActTailRows, kN)                 // xq (+ the 16-row M tile)
                + (size_t) (max_rows * 1280 * 4) + (size_t) (max_rows * kN * 4)   // GU, Dm
                + (size_t) (max_rows * 640 * 4) + mmq::q8_bytes(max_rows + kActTailRows, 640)   // H, Hq
                + mmq_group_gu_bytes(mmq_gub, over_mx) + mmq_group_d_bytes(mmq_db, over_mx)   // groups
                + mmq_tail_words_bytes()                                     // the NVFP4 tail words
                + stage_ring_bytes(blob_mx)                                  // the staging ring (window max)
                + (size_t) (2 * kNE + 32) * 4 + (size_t) (2 * max_rows + max_T * kK) * 4 + 4096;
    if (o.arm_groups) need *= 2;
    const int64_t need_mib = (int64_t) (need >> 20);
    if (need_mib > o.max_device_mib) {
        std::fprintf(stderr, "p2_measure_harness: REFUSAL (device memory): this run needs ~%lld MiB of "
                             "device buffers at T<=%lld, above --max-device-mib %lld. Lower --max-tokens "
                             "or raise --max-device-mib.\n",
                     (long long) need_mib, (long long) max_T, (long long) o.max_device_mib);
        return 4;
    }
    std::fprintf(stderr, "p2_measure_harness: blob %s (%lld bytes, %s), layers %lld-%lld, %zu fixtures, "
                         "activations %s, device need ~%lld MiB (cap %lld)\n",
                 blob.path.c_str(), (long long) pack_bytes, blob.per_role ? "per-role GGUF slices" : "packed blobs",
                 (long long) o.layer_lo, (long long) o.layer_hi,
                 fix.size(), rep.meta.activation_mode.c_str(), (long long) need_mib,
                 (long long) o.max_device_mib);

    // ---- the helper: hello, fill, and the resident set (the design's authoritative holds) ----
    ExpertHelperClient client;
    HelperIdentity id;
    std::string herr;
    const int hrc = helper_handshake(client, o.socket, o.hs, ranked, id, herr);
    rep.meta.helper = id;
    if (hrc != eh::kOk) {
        std::fprintf(stderr, "p2_measure_harness: REFUSAL (helper): %s\n", herr.c_str());
        return 6;
    }
    std::fprintf(stderr, "p2_measure_harness: helper resident %lld of %lld requested pairs (%lld bytes, "
                         "%lld MiB free), %s\n",
                 (long long) id.resident, (long long) id.requested_pairs, (long long) id.bytes,
                 (long long) id.free_mib, id.note.c_str());
    // holds[] comes ONLY from the authoritative fill report (the engine's rule: the report mirrors
    // holds()/resident() for the rest of the run).  host_res is ALL -1: the harness owns no stage caches,
    // so no pair is primary-resident; stable[] is ALL 1: the harness reads the blobs from a mapped
    // read-only file, so same-layer fallback is always possible (the CPU path's blob_stable rule).
    std::map<int64_t, std::vector<uint8_t>> holds;
    for (int64_t l = o.layer_lo; l <= o.layer_hi; ++l) holds[l].assign((size_t) kNE, 0);
    for (const auto& pr : id.resident_pairs)
        if (pr.first >= o.layer_lo && pr.first <= o.layer_hi)
            holds[(size_t) pr.first][(size_t) pr.second] = 1;
    std::vector<uint8_t> stable((size_t) kNE, 1);
    std::vector<int32_t> host_res((size_t) kLayers * kNE, -1);
    if ((int64_t) id.resident == 0)
        std::fprintf(stderr, "p2_measure_harness: NOTE: the helper filled 0 pairs - every layer's plan is "
                             "empty and the run measures nothing. Raise the helper's budget.\n");

    mmq::Context ctx;

    // ---- the run: fixture-major, then layer (the design's (prompt, layer, format, expert, token) key) ----
    int64_t total_rows = 0;
    for (const std::string& fid : fix) {
        const int fi = fixture_index_of(fid);
        for (int64_t l = o.layer_lo; l <= o.layer_hi; ++l) {
            const std::string key = fid + "@" + std::to_string(l);
            const auto it = act.find(key);
            if (it == act.end()) continue;                 // missing dump: noted in pass 1
            const Activations& a = it->second;
            const LayerCounts& lc = counts[key];
            const auto& ft = lay.fmt[(size_t) l];

            ExpertHelperPromptPlan plan;
            // select_prompt_rows and begin_prompt index host_res[layer * n_expert + e] THEMSELVES: the
            // full array goes in at every call (host_res is all -1 here: the harness owns no stage caches).
            const int src = select_prompt_rows(lc.cnt.data(), lc.toks.data(),
                                               host_res.data(), (int32_t) l,
                                               holds[(size_t) l].data(), stable.data(), (int64_t) kNE, kK,
                                               eh::kMaxRowsPerExpert, eh::kMaxRowsPerLayer, eh::kMaxSelTok,
                                               plan);
            if (src != eh::kOk) {
                std::fprintf(stderr, "p2_measure_harness: REFUSAL (selection): %s @ layer %lld: %s\n",
                             fid.c_str(), (long long) l, eh::status_name(src));
                return 7;
            }
            if (plan.rows() < 1) {
                rep.meta.notes.push_back(fid + " L" + std::to_string(l) +
                                         ": empty plan (no resident non-resident expert fits the caps) - "
                                         "the layer stays fully local");
                continue;
            }
            rep.meta.rows_expected += plan.rows();

            // the claims over the layer's routed ENTRIES (p2_measure.hpp section 7: the entry a pair claims
            // is found in the token's routed ids, not computed from the expert id)
            std::vector<uint8_t> claims;
            std::string cerr2;
            if (mark_claim_entries(a, plan, claims, cerr2) != eh::kOk) {
                std::fprintf(stderr, "p2_measure_harness: REFUSAL (plan): %s @ layer %lld: %s\n",
                             fid.c_str(), (long long) l, cerr2.c_str());
                return 7;
            }
            std::set<std::pair<int32_t, int32_t>> seen;
            for (int64_t j = 0; j < plan.rows(); ++j) {
                const int32_t t = plan.pair_token[(size_t) j], e = plan.pair_expert[(size_t) j];
                if (!seen.insert({t, e}).second) {
                    std::fprintf(stderr, "p2_measure_harness: REFUSAL (plan): duplicate (token, expert) "
                                         "pair (%d, %d) at %s @ layer %lld\n", (int) t, (int) e,
                                 fid.c_str(), (long long) l);
                    return 7;
                }
            }

            const size_t mmq_gub_l = mmq::matrix_bytes(ft.gu_type, 1280, kN);
            const size_t mmq_db_l = mmq::matrix_bytes(ft.d_type, kN, 640);
            const size_t blob_bytes = (size_t) lay.blob_bytes(l);
            if ((int64_t) blob_layer_end(lay, blob, l) > pack_bytes) {
                std::fprintf(stderr, "p2_measure_harness: REFUSAL (pack): layer %lld's blobs run past the "
                                     "end of %s (%lld bytes)\n", (long long) l, blob.path.c_str(),
                             (long long) pack_bytes);
                return 3;
            }

            MmqRows g, ga;
            std::string gerr;
            if (!build_geometry(a, nullptr, g, gerr)) {
                std::fprintf(stderr, "p2_measure_harness: REFUSAL (geometry): %s @ layer %lld: %s\n",
                             fid.c_str(), (long long) l, gerr.c_str());
                return 7;
            }
            if (o.arm_groups && !build_geometry(a, claims.data(), ga, gerr)) {
                std::fprintf(stderr, "p2_measure_harness: REFUSAL (geometry, arm): %s @ layer %lld: %s\n",
                             fid.c_str(), (long long) l, gerr.c_str());
                return 7;
            }

            // the device buffers for this (fixture, layer).  EVERY size is THIS layer's own (the grow-
            // after-shrink rule: L32 2,329,600 B -> L33 1,305,600 -> L34 1,868,800), computed through the
            // section 7b helpers the CPU suite pins (p2_measure.hpp); DevMem is fresh per layer, so no
            // buffer of a previous layer is reused, and no allocation is keyed by another layer.
            DevMem mem;
            std::string merr;
            MmqDevice d;
            const int64_t rows = g.rows_local;
            const int64_t nr_max = std::max<int64_t>(g.nr_max, o.arm_groups ? ga.nr_max : 0);
            // the K tile's over-read past the last expert's bytes in each group buffer (the same number
            // prefill.cpp:1242-1246 tracks for the resident slots' guard): the group tail region covers it
            const size_t over_gu = mmq::overread_bytes(ft.gu_type, kN);
            const size_t over_d = mmq::overread_bytes(ft.d_type, 640);
            const size_t grp_gu_bytes = mmq_group_gu_bytes(mmq_gub_l, over_gu);
            const size_t grp_d_bytes = mmq_group_d_bytes(mmq_db_l, over_d);
            d.x = mem.take((size_t) (a.T * kN) * sizeof(float), merr);
            d.xq = mem.take(mmq::q8_bytes(rows + kActTailRows, kN), merr);
            d.gu = mem.take(mmq_gu_buffer_bytes(rows), merr);
            d.dm = mem.take(mmq_dm_buffer_bytes(rows), merr);
            d.h = mem.take((size_t) (nr_max * 640) * sizeof(float), merr);
            d.hq = mem.take(mmq::q8_bytes(nr_max + kActTailRows, 640), merr);
            d.grp_gu = mem.take(grp_gu_bytes, merr);
            d.grp_d = mem.take(grp_d_bytes, merr);
            d.grp_tail = mem.take(mmq_tail_words_bytes(), merr);   // the NVFP4 tail words: their own buffer
            d.stage = mem.take(stage_ring_bytes(blob_bytes), merr);   // the ring IS 32 of THIS layer's blob
            d.bounds = mem.take((size_t) (2 * (kNE + 1) + 64 * (MMQ_GROUP + 1)) * 4, merr);
            d.ids = mem.take((size_t) std::max<int64_t>(rows, nr_max) * sizeof(int32_t), merr);
            d.slot = mem.take((size_t) (a.T * kK) * sizeof(int32_t), merr);
            d.src = mem.take((size_t) rows * sizeof(int32_t), merr);
            if (!merr.empty() || !d.x || !d.xq || !d.gu || !d.dm || !d.h || !d.hq || d.grp_gu == nullptr ||
                d.grp_d == nullptr || d.grp_tail == nullptr || !d.stage || !d.bounds || !d.ids || !d.slot ||
                !d.src) {
                std::fprintf(stderr, "p2_measure_harness: REFUSAL (device buffers): %s @ layer %lld: %s\n",
                             fid.c_str(), (long long) l, merr.c_str());
                return 4;
            }
            // zero-fill the regions MMQ's tile reads may touch: the activation tails, the group tails
            ck(cudaMemsetAsync((uint8_t*) d.xq + mmq::q8_bytes(rows, kN), 0,
                               mmq::q8_bytes(rows + kActTailRows, kN) - mmq::q8_bytes(rows, kN), cs),
               "xq tail", merr);
            ck(cudaMemsetAsync((uint8_t*) d.hq + mmq::q8_bytes(nr_max, 640), 0,
                               mmq::q8_bytes(nr_max + kActTailRows, 640) - mmq::q8_bytes(nr_max, 640), cs),
               "hq tail", merr);
            ck(cudaMemsetAsync(d.grp_gu, 0, grp_gu_bytes, cs), "grp gu", merr);
            ck(cudaMemsetAsync(d.grp_d, 0, grp_d_bytes, cs), "grp d", merr);
            ck(cudaMemsetAsync(d.grp_tail, 0, mmq_tail_words_bytes(), cs), "grp tail", merr);
            ck(cudaMemsetAsync(d.dm, 0, (size_t) (rows * kN) * sizeof(float), cs), "dm", merr);
            mmq::iota((int32_t*) d.ids, std::max<int64_t>(rows, nr_max), cs);
            // slot/src uploads (the geometry's inverse walk)
            {
                std::vector<int32_t> slot((size_t) (a.T * kK), -1), src((size_t) rows, 0);
                std::vector<int64_t> off((size_t) kNE + 1, 0);
                int64_t r = 0;
                for (int64_t e = 0; e < kNE; ++e) { off[(size_t) e] = r; r += g.cnt[(size_t) e]; }
                std::vector<int64_t> fill(off.begin(), off.end() - 1);
                for (int64_t i = 0; i < a.T * kK; ++i) {
                    const int32_t e = a.ids[(size_t) i];
                    const int32_t p = (int32_t) fill[(size_t) e]++;
                    slot[(size_t) i] = p;
                    src[(size_t) p] = (int32_t) (i / kK);
                }
                ck(cudaMemcpyAsync(d.slot, slot.data(), slot.size() * sizeof(int32_t),
                                   cudaMemcpyHostToDevice, cs), "slot upload", merr);
                ck(cudaMemcpyAsync(d.src, src.data(), src.size() * sizeof(int32_t),
                                   cudaMemcpyHostToDevice, cs), "src upload", merr);
            }
            if (const cudaError_t e = cudaGetLastError(); e != cudaSuccess) {
                std::fprintf(stderr, "p2_measure_harness: REFUSAL (device prep): %s @ layer %lld: %s\n",
                             fid.c_str(), (long long) l, cudaGetErrorString(e));
                return 4;
            }
            // the layer-boundary checkpoint BEFORE the run: a fault recorded by any prologue op (a
            // memset, iota, the slot/src uploads) is caught HERE, with the prologue named - not at the
            // first op of the MMQ run (the 2026-10-29 live fault was reported at "blob upload" and the
            // faulting op was unknown).  SYNC = a full flush: the price is one wall-clock sync per layer.
            if (!ck(cudaStreamSynchronize(cs), "prologue sync", err)) {
                std::fprintf(stderr, "p2_measure_harness: CKPT %s L%lld prologue sync: %s\n",
                             fid.c_str(), (long long) l, err.c_str());
                return 8;
            }
            std::fprintf(stderr, "p2_measure_harness: CKPT %s L%lld prologue clean\n",
                         fid.c_str(), (long long) l);

            // fix6 staging evidence + probe, printed for EVERY (fixture, layer) because the pinned live
            // fault (attempt 10, L34) fires at the FIRST blob upload after a clean prologue: the static
            // audit of that cudaMemcpyAsync's three parameters is clean (the destination slot*stride+size
            // = 32*blob = the ring's own size; the size is THIS layer's lay.blob_bytes(l); the host
            // source is a vector allocated at exactly that size in run_mmq), so the run must hand us the
            // numbers and the ring's first touch must be bracketed on its own.
            //   the numbers: slot = (j0+q) % 32 for the FIRST upload (j0 = q = 0), stride = the per-layer
            //   upload stride, size = the copy size, cap = the host vector's size, ring = the allocation.
            std::fprintf(stderr, "p2_measure_harness: STAGE %s L%lld ring slot 0 stride %llu size %llu "
                                 "host-cap %llu ring %llu types %d/%d header-blob %llu gguf-blob %llu\n",
                         fid.c_str(), (long long) l,
                         (unsigned long long) blob_bytes, (unsigned long long) blob_bytes,
                         (unsigned long long) blob_bytes, (unsigned long long) stage_ring_bytes(blob_bytes),
                         (int) ft.gu_type, (int) ft.d_type,
                         (unsigned long long) lay.blob_bytes(l),
                         (unsigned long long) staged_gguf_blob.count(l) ? staged_gguf_blob.at(l) : 0ULL);
            // the ring probe: the ring is the ONLY device buffer of the layer the prologue never touches,
            // so its first touch IS the first blob upload.  Write the ring's LAST byte (and, at a tiny
            // blob, its first) as a probe op, then SYNC each probe op: if a freshly cudaMalloc'ed ring
            // faults on its first touch, the FAULT-AT line names the probe op with the address and the
            // pointer attributes, and the upload is exonerated - a device-side allocation fault, not a
            // harness sizing mismatch.  fix6 probed with cudaGetLastError ONLY, which cannot see an
            // unsurfaced async fault: the probe's own sync is what makes the probe able to answer.
            {
                const size_t ring_bytes = stage_ring_bytes(blob_bytes);
                uint8_t* const probe_tail = (uint8_t*) d.stage + ring_bytes - 1;
                uint8_t* const probe_head = (uint8_t*) d.stage;
                ck(cudaMemsetAsync(probe_tail, 0xA5, 1, cs), "ring probe", merr);
                if (!sync_op(cs, fid.c_str(), l, "ring probe tail", -1, 31, -1, nullptr, probe_tail, 1,
                             merr)) return 8;
                ck(cudaMemsetAsync(probe_head, 0xA5, 1, cs), "ring probe head", merr);
                if (!sync_op(cs, fid.c_str(), l, "ring probe head", -1, 0, -1, nullptr, probe_head, 1,
                             merr)) return 8;
            }

            // fix8 (iv): the layer's per-op parameter set, ONE line per (fixture, layer).  L32 measures
            // clean and L34 faults with IDENTICAL types (21/20) and IDENTICAL blob (2,329,600 B) and ring
            // (74,547,200 B), so the dimension that differs is in the GEOMETRY: the token count, the
            // routed entries, the rows the run keeps, the order length, the group count and windows
            // (j0 : q-count [r0/nr]), nr_max, the MMQ slot sizes, the stream.  Both layers print the same
            // fields in the same order, so the differing one is visible in one place.
            std::vector<int64_t> off_main((size_t) kNE + 1, 0);
            {
                int64_t r = 0;
                for (int64_t e = 0; e < kNE; ++e) { off_main[(size_t) e] = r; r += g.cnt[(size_t) e]; }
                off_main[(size_t) kNE] = r;
            }
            auto print_opsum = [&](const char* tag, const MmqRows& r, const std::vector<int64_t>& o) {
                OpWindow ow;
                ow.layer = l; ow.T = a.T; ow.entries = a.T * kK; ow.rows = r.rows_local;
                ow.order_n = (int64_t) r.order.size();
                ow.groups = (int64_t) ((ow.order_n + MMQ_GROUP - 1) / MMQ_GROUP);
                ow.nr_max = r.nr_max;
                ow.blob = blob_bytes; ow.ring = stage_ring_bytes(blob_bytes);
                ow.gu_bytes = mmq_gub_l; ow.down_bytes = mmq_db_l;
                ow.stream = (const void*) cs; ow.ring_base = d.stage;
                for (int64_t gg = 0; gg < ow.groups; ++gg) {
                    const size_t j0 = (size_t) (gg * MMQ_GROUP);
                    const size_t j1 = std::min((size_t) ow.order_n, j0 + MMQ_GROUP);
                    const long long rr0 = (long long) o[(size_t) r.order[j0]];
                    const long long end_row = (j1 < (size_t) ow.order_n)
                                                  ? (long long) o[(size_t) r.order[j1]]
                                                  : (long long) r.rows_local;
                    ow.wins.push_back({(long long) j0, (long long) (j1 - j0)});
                    ow.r0.push_back(rr0);
                    ow.nr.push_back(end_row - rr0);   // the same nr run_mmq computes from bounds[]
                }
                const std::string fid_tag = std::string(fid) + tag;
                std::fprintf(stderr, "p2_measure_harness: %s\n",
                             format_op_summary(fid_tag.c_str(), ow).c_str());
            };
            print_opsum("", g, off_main);

            // The run's checkpoint (fix5's label, fix8's sync): run_mmq SYNCs after EVERY op and hands the
            // op's parameters here, so a fault is printed AS THE OP THAT CAUSED IT, with its pointers,
            // copy size, stream and both pointers' cudaPointerGetAttributes answers.  The old checkpoint
            // called cudaGetLastError with no sync, so it could only pop an ALREADY-RECORDED error - an
            // unsurfaced async fault crossed every one of them and surfaced later, at an unrelated call
            // (the live L34 fault's "blob upload").  The clean path prints nothing; the prologue line
            // above stays as the window's clean bracket.
            auto ck_run = [&](const char* op, const OpProbe& pr) -> bool {
                if (pr.fault != nullptr && *pr.fault != '\0') {
                    std::fprintf(stderr, "p2_measure_harness: %s\n",
                                 format_fault_line(fid.c_str(), l, pr, pr.fault).c_str());
                    return false;
                }
                (void) op;
                return true;
            };

            std::vector<float> dm_main;
            if (!run_mmq(l, a, g, off_main, ft, mmq_gub_l, mmq_db_l, blob_bytes, pk, blob,
                         (uint64_t) pack_bytes, ctx, d, cs, dm_main,
                         merr, ck_run)) {
                // a refused product geometry is a CPU-side refusal (exit 4), NOT a device fault (exit 8)
                const bool geom = merr.rfind("the gate/up product's dst write", 0) == 0 ||
                                  merr.rfind("the down product's dst write", 0) == 0;
                std::fprintf(stderr, "p2_measure_harness: REFUSAL (side A%s): %s @ layer %lld: %s\n",
                             geom ? " geometry" : "", fid.c_str(), (long long) l, merr.c_str());
                return geom ? 4 : 8;
            }

            // the arm's composition (--arm-groups): the same buffers rebuilt for the reduced row set
            std::vector<float> dm_arm;
            if (o.arm_groups) {
                MmqDevice da;
                const int64_t rows_a = ga.rows_local;
                da.x = d.x; da.xq = mem.take(mmq::q8_bytes(rows_a + kActTailRows, kN), merr);
                da.gu = mem.take(mmq_gu_buffer_bytes(rows_a), merr);
                da.dm = mem.take(mmq_dm_buffer_bytes(rows_a), merr);
                da.h = mem.take((size_t) (ga.nr_max * 640) * sizeof(float), merr);
                da.hq = mem.take(mmq::q8_bytes(ga.nr_max + kActTailRows, 640), merr);
                da.grp_gu = d.grp_gu; da.grp_d = d.grp_d; da.grp_tail = d.grp_tail; da.stage = d.stage;
                da.bounds = mem.take((size_t) (kNE + 1 + 64 * (MMQ_GROUP + 1)) * 4, merr);
                da.ids = mem.take((size_t) std::max<int64_t>(rows_a, ga.nr_max) * sizeof(int32_t), merr);
                da.slot = d.slot; da.src = mem.take((size_t) rows_a * sizeof(int32_t), merr);
                ck(cudaMemsetAsync((uint8_t*) da.xq + mmq::q8_bytes(rows_a, kN), 0,
                                   mmq::q8_bytes(rows_a + kActTailRows, kN) - mmq::q8_bytes(rows_a, kN), cs),
                   "arm xq tail", merr);
                ck(cudaMemsetAsync(da.hq, 0, mmq::q8_bytes(ga.nr_max + kActTailRows, 640), cs),
                   "arm hq", merr);
                ck(cudaMemsetAsync(da.dm, 0, (size_t) (rows_a * kN) * sizeof(float), cs), "arm dm", merr);
                ck(cudaMemsetAsync(da.grp_gu, 0, grp_gu_bytes, cs), "arm grp gu", merr);
                ck(cudaMemsetAsync(da.grp_d, 0, grp_d_bytes, cs), "arm grp d", merr);
                mmq::iota((int32_t*) da.ids, std::max<int64_t>(rows_a, ga.nr_max), cs);
                {
                    std::vector<int32_t> slot((size_t) (a.T * kK), -1), src((size_t) rows_a, 0);
                    std::vector<int64_t> off((size_t) kNE + 1, 0);
                    int64_t r = 0;
                    for (int64_t e = 0; e < kNE; ++e) { off[(size_t) e] = r; r += ga.cnt[(size_t) e]; }
                    std::vector<int64_t> fill(off.begin(), off.end() - 1);
                    for (int64_t i = 0; i < a.T * kK; ++i) {
                        if (claims[(size_t) i]) continue;
                        const int32_t e = a.ids[(size_t) i];
                        const int32_t p = (int32_t) fill[(size_t) e]++;
                        slot[(size_t) i] = p;
                        src[(size_t) p] = (int32_t) (i / kK);
                    }
                    ck(cudaMemcpyAsync(da.slot, slot.data(), slot.size() * sizeof(int32_t),
                                       cudaMemcpyHostToDevice, cs), "arm slot upload", merr);
                    ck(cudaMemcpyAsync(da.src, src.data(), src.size() * sizeof(int32_t),
                                       cudaMemcpyHostToDevice, cs), "arm src upload", merr);
                }
                if (const cudaError_t e = cudaGetLastError(); e != cudaSuccess) {
                    std::fprintf(stderr, "p2_measure_harness: REFUSAL (arm prep): %s @ layer %lld: %s\n",
                                 fid.c_str(), (long long) l, cudaGetErrorString(e));
                    return 4;
                }
                // fix8: the ARM's prologue SYNC - its prep ops (memsets, iota, the slot/src uploads) are
                // async too, so a fault one of them caused is attributed at the arm's prologue, with a
                // FAULT-AT line, and not at the arm's first MMQ op.
                if (!sync_op(cs, fid.c_str(), l, "arm prologue", -1, -1, -1, nullptr, nullptr, 0, merr))
                    return 8;
                std::vector<int64_t> off_arm((size_t) kNE + 1, 0);
                {
                    int64_t r = 0;
                    for (int64_t e = 0; e < kNE; ++e) { off_arm[(size_t) e] = r; r += ga.cnt[(size_t) e]; }
                    off_arm[(size_t) kNE] = r;
                }
                print_opsum("+arm", ga, off_arm);
                if (!run_mmq(l, a, ga, off_arm, ft, mmq_gub_l, mmq_db_l, blob_bytes, pk, blob,
                             (uint64_t) pack_bytes, ctx, da, cs,
                             dm_arm, merr, ck_run)) {
                    const bool geom = merr.rfind("the gate/up product's dst write", 0) == 0 ||
                                      merr.rfind("the down product's dst write", 0) == 0;
                    std::fprintf(stderr, "p2_measure_harness: REFUSAL (side A, arm%s): %s @ layer %lld: %s\n",
                                 geom ? " geometry" : "", fid.c_str(), (long long) l, merr.c_str());
                    return geom ? 4 : 8;
                }
            }

            // Side B: the REAL helper prompt-row path over the REAL frames
            std::vector<float> hb;
            int32_t rows_ret = 0;
            std::string berr;
            const int brc = helper_prompt_rows(client, (int32_t) l, plan, a,
                                               host_res.data(), o.hs, hb, rows_ret, berr);
            if (brc != eh::kOk) {
                // The client's rule: a reply violation CLOSES the channel (no reconnect exists), so the
                // run stops here.  Records measured so far are written; nothing is left as a silent zero.
                rep.meta.notes.push_back("side B stopped " + fid + " L" + std::to_string(l) + ": " + berr);
                rep.meta.rows_skipped = rep.meta.rows_expected - total_rows;
                client.close();
                emit_report(rep, o);
                std::fprintf(stderr, "p2_measure_harness: STOPPED (side B): %s\n", berr.c_str());
                return 9;
            }
            if ((int64_t) rows_ret != plan.rows()) {
                rep.meta.notes.push_back("side B row deficit " + fid + " L" + std::to_string(l) + ": " +
                                          std::to_string(rows_ret) + " of " + std::to_string(plan.rows()));
                rep.meta.rows_skipped = rep.meta.rows_expected - total_rows;
                client.close();
                emit_report(rep, o);
                std::fprintf(stderr, "p2_measure_harness: STOPPED (side B): row deficit at %s L%lld\n",
                             fid.c_str(), (long long) l);
                return 9;
            }

            // The read invariant, checked BEFORE the first read: the loop below addresses both buffers by
            // index (side A by the geometry row, side B by the pair index), so a buffer shorter than the
            // loop reads is a refusal, never a stray pointer walk.  The printed sizes and addresses are the
            // evidence a live run leaves behind: an ASAN report's faulting address is comparable to them.
            std::string inverr;
            if (check_side_b_rows(a, plan, g.pair2row.data(), hb, dm_main, inverr) != eh::kOk) {
                rep.meta.notes.push_back("side B invariant " + fid + " L" + std::to_string(l) + ": " + inverr);
                rep.meta.rows_skipped = rep.meta.rows_expected - total_rows;
                client.close();
                emit_report(rep, o);
                std::fprintf(stderr, "p2_measure_harness: STOPPED (side B invariant) %s L%lld: %s\n",
                             fid.c_str(), (long long) l, inverr.c_str());
                return 9;
            }
            std::fprintf(stderr, "p2_measure_harness: side B gate %s L%lld: %lld rows, hb %zu floats @ %p, "
                                 "dm %zu floats @ %p\n", fid.c_str(), (long long) l, (long long) plan.rows(),
                         hb.size(), (const void*) hb.data(), dm_main.size(), (const void*) dm_main.data());

            // the gate rows: the claimed pairs, Side A (today's group composition) vs Side B (the helper).
            // The pair's side-A row comes from pair_row - the routed-slot lookup - because pair2row is keyed
            // by the ENTRY index (token*kK + slot): g.pair2row[t*kK + e] reads another token's entry whenever
            // the expert id is >= kK, which is what walked the loop ~39 MB past a 2250-row buffer (the live
            // crash of 2026-10-29, pair (37, 75) -> entry (44, slot 5)'s row 3810).
            for (int64_t j = 0; j < plan.rows(); ++j) {
                const int32_t t = plan.pair_token[(size_t) j], e = plan.pair_expert[(size_t) j];
                int32_t row = -1;
                std::string rerr;
                if (pair_row(a, g.pair2row.data(), t, e, row, rerr) != eh::kOk) {
                    std::fprintf(stderr, "p2_measure_harness: REFUSAL (side A mapping): %s @ layer %lld: %s\n",
                                 fid.c_str(), (long long) l, rerr.c_str());
                    return 7;
                }
                if (row < 0) {
                    std::fprintf(stderr, "p2_measure_harness: REFUSAL (side A): claimed pair (%d, %d) of "
                                         "%s @ layer %lld has no MMQ row\n", (int) t, (int) e,
                                 fid.c_str(), (long long) l);
                    return 7;
                }
                RowRec rr;
                rr.fixture = fid;
                rr.fixture_index = fi;
                rr.layer = (int32_t) l;
                rr.expert = e;
                rr.token = t;
                rr.pair = (int32_t) j;
                rr.gu_type = ft.gu_type;
                rr.d_type = ft.d_type;
                rr.a_vs_b = diff_rows(dm_main.data() + (size_t) row * kN,
                                      hb.data() + (size_t) j * kN, kN, o.eps);
                rep.rows.push_back(std::move(rr));
                ++total_rows;
            }
            rep.meta.fixtures_used[fid].second += plan.rows();

            // the recomposition term: rows the ARM keeps LOCAL whose expert left the group it shared
            // with the claimed rows (the design's line 22 - co-grouped local rows round differently)
            if (o.arm_groups) {
                std::set<int32_t> claimed_experts;
                for (int64_t j = 0; j < plan.rows(); ++j) claimed_experts.insert(plan.pair_expert[(size_t) j]);
                for (int64_t i = 0; i < a.T * kK; ++i) {
                    if (claims[(size_t) i]) continue;
                    const int32_t e = a.ids[(size_t) i];
                    if (!claimed_experts.count(e)) continue;
                    const int32_t row = g.pair2row[(size_t) i];
                    const int32_t rowa = ga.pair2row[(size_t) i];
                    if (row < 0 || rowa < 0) continue;
                    RowRec rr;
                    rr.fixture = fid;
                    rr.fixture_index = fi;
                    rr.layer = (int32_t) l;
                    rr.expert = e;
                    rr.token = (int32_t) (i / kK);
                    rr.pair = (int32_t) i;
                    rr.gu_type = ft.gu_type;
                    rr.d_type = ft.d_type;
                    rr.has_arm = true;
                    rr.arm_vs_a = diff_rows(dm_arm.data() + (size_t) rowa * kN,
                                            dm_main.data() + (size_t) row * kN, kN, o.eps);
                    rep.recompose.push_back(std::move(rr));
                }
            }
            std::fprintf(stderr, "p2_measure_harness: %s L%lld: %lld claimed rows measured "
                                 "(%lld local of %lld routed), %lld recompose records\n",
                         fid.c_str(), (long long) l, (long long) plan.rows(), (long long) ga.rows_local,
                         (long long) (a.T * kK), o.arm_groups ? (long long) rep.recompose.size() : (long long) 0);
        }
    }
    rep.meta.rows_measured = total_rows;
    rep.meta.rows_skipped = rep.meta.rows_expected - total_rows;
    client.bye();
    client.close();
    emit_report(rep, o);
    std::fprintf(stderr, "p2_measure_harness: done: %lld gate rows, %lld recompose records -> %s and %s.table.txt\n",
                 (long long) rep.meta.rows_measured, (long long) rep.recompose.size(), o.out.c_str(),
                 o.out.c_str());
    return 0;
}
