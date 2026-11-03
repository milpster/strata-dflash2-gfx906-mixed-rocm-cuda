// tools/mixed_gpu/p2_measure/p2_measure.hpp - the helper-vs-MMQ prompt-row DEVIATION MEASUREMENT harness.
//
// WHAT THIS IS (r5-three-gpu-design.md §2 lines 28-39 and line 81, the P2 gate measurement): for the SAME
// (token, expert) pairs of a REAL quantized blob, compute the row BOTH WAYS and report how far apart they
// are:
//
//   Side A - the primary's MMQ path: `mmq::quantize_scatter`/`mmq::quantize` (the ggml-derived q8_1 producer,
//            ids-gathered, slot scatter) + the 16-expert gather-group MMQ products (gate/up, swiglu, the
//            intermediate re-quantized to q8_1, down), on the real blob bytes - the sequence a real prefill
//            chunk runs (prefill.cpp:4129-4131, 4549-4567, 4663-4686).  Lives in p2_measure_main.cpp: it is
//            the CUDA side of the harness.
//   Side B - the helper's prompt-row path: `quantize_q8_1_cpu` bytes (expert_helper_socket.cpp:90) carried in
//            the REAL P1 kBeginP frame to the REAL `strata-expert-helper` binary, answered by
//            `native_expert_grouped` on the same blob (helper_main.cpp:482-602), read back by `finish_prompt`.
//            Lives HERE (p2_measure.cpp), CPU-only: it drives the engine's own client over the socket.
//
// NO TOLERANCE, NO PASS/FAIL, NO BAND.  The harness measures and reports.  The acceptance band belongs to the
// user and is applied by the parent AFTER the run (the design's line 39: "no invented band").  Nothing in
// these files compares a number against a threshold, and nothing in them can print a verdict.
//
// CPU-only by construction in THIS file and its .cpp: the metric, fixture, selection, socket, JSON and table
// code calls no device function (Side A is deliberately the other translation unit), so the CPU validation
// test compiles, runs and pins all of it on a machine with no GPU in sight.
#pragma once

#include "strata/core/expert_helper_socket.hpp"
#include "strata/kernels/cpu/expert_layout.hpp"

#include <cstdint>
#include <istream>
#include <map>
#include <string>
#include <utility>
#include <vector>

namespace strata {
namespace tools {
namespace p2 {

// The pack's fixed MoE geometry (strata::kernels::cpu::expert.hpp:36-38, the helper's own H/FF/NE).
inline constexpr int64_t kN = 2560;        // n_embd
inline constexpr int64_t kFF = 640;        // n_ff
inline constexpr int64_t kNE = 512;        // routed experts per layer
inline constexpr int64_t kK = 10;          // routing width (expert_helper::kK)
inline constexpr int64_t kLayers = 48;
inline constexpr int64_t kLayerLo = 32;    // the helper's window on the split32 box (design §2, generate.cpp:7102-7144)
inline constexpr int64_t kLayerHi = 47;

// ============================== 1. the metric ==============================
//
// The two numbers the design asks for (line 81: "per-row max-abs/max-rel"), per (prompt, layer, format,
// expert, token) pair, plus the fields a reader needs to judge a row:
//
//   max_abs  = max_i |a_i - b_i|
//   row_amax = max_i max(|a_i|, |b_i|)                      (the row's own largest magnitude)
//   max_rel  = max_abs / max(row_amax, eps)                 (the design's "/ row amax, eps-guarded")
//            = 0 when the row is EXACTLY equal, and the eps floor keeps a zero row at 0, never a 0/0.
//   mean_abs, l2_rel and the two amaxes are context, not new criteria: l2_rel = ||a-b|| / ||a|| when
//            ||a|| > 0, and 0 when ||a|| == 0 (a zero-reference row has NO defined ratio - see 1b).
//
// eps is a parameter of the RUN (recorded in the JSON), never a hidden constant: the same run with a
// different eps reproduces every max_abs and re-derives every max_rel.
inline constexpr double kEpsDefault = 1e-12;
//
// ---- 1b. the retained energies and what the zero-reference row IS (NMSE evidence, 2026-11-01) ----
//
// `diff_rows` accumulates two sums in plain double and RETAINS both, so a reader can rebuild the
// tensor-level ratio of any population of rows without re-reading the rows' floats:
//
//   sum_sq_err = sum_i (a_i - b_i)^2          (the row's error energy)
//   sum_sq_ref = sum_i a_i^2                  (the row's REFERENCE energy - the reference is side A)
//   n          = the row length the sums cover (0 for the n <= 0 record)
//
// and it RETAINS the row's state, so a zero in a ratio never reads as agreement:
//
//   ref_zero   = (sum_sq_ref == 0).  A row with no reference energy has NO defined relative measure.
//   nonfinite  = the row carried a NaN or an Infinity.  The ratios a nonfinite row produces are NOT
//                reported as finite: the row is counted, and every aggregate over a population that
//                contains it is REFUSED (a named state, never a silent 0 - see NmseDiag).
//
// THE ZERO-REFERENCE CASES, EXACTLY (this is the code, not a description of it):
//   max_rel: the denominator is max(row_amax, eps) and row_amax takes the largest magnitude of BOTH
//            sides, so a zero-reference, nonzero-candidate row gives max_abs/max(max_abs, eps): 1 at or
//            above eps, clamped below eps. A zero row gives 0.
//   l2_rel : THE CODE returns 0 when sum_sq_ref == 0 (`nrm > 0 ? l2/nrm : 0`).  It is NOT the eps clamp
//            this header's v1 text claimed.  The numeric value is kept EXACTLY as v1 emitted it - a
//            band metric is not redefined here - and the row's `ref_zero` flag says what that 0 means:
//            an UNDEFINED ratio, not an exact row.  A reader who needs the ratio must use the NMSE
//            block below, which names the state instead of returning 0.
//
// NO TOLERANCE IS ATTACHED TO ANY OF THESE: they are measurements and states, not criteria.

struct RowDiff {
    double max_abs = 0.0;
    double row_amax = 0.0;
    double max_rel = 0.0;
    double mean_abs = 0.0;
    double l2_rel = 0.0;
    double a_amax = 0.0, b_amax = 0.0;
    uint64_t a_fnv = 0, b_fnv = 0;   // FNV-1a/64 over the row's float bytes: a content pin, not a hash standard
    // ---- additive (schema v2): the retained energies and the row's state ----
    int64_t n = 0;                   // the row length the energies cover
    double sum_sq_err = 0.0;         // sum_i (a_i-b_i)^2, the raw double accumulation
    double sum_sq_ref = 0.0;         // sum_i a_i^2 (reference = side A)
    bool ref_zero = true;            // sum_sq_ref == 0: the row has no reference energy
    bool nonfinite = false;          // a NaN/Infinity was in the row's inputs
};

RowDiff diff_rows(const float* a, const float* b, int64_t n, double eps = kEpsDefault);
uint64_t fnv64(const void* data, size_t bytes);

// ---- 1c. TENSOR NMSE over a POPULATION of rows (diagnostic, not a criterion) ----
//
// NMSE here means ONE ratio over the whole population, never a mean of per-row ratios:
//
//   nmse = ( sum over rows of sum_sq_err ) / ( sum over rows of sum_sq_ref )
//
// It is ENERGY-WEIGHTED by construction: a row with 100x the reference energy moves the number 100x as
// much as a row with 1x.  An unweighted mean of l2_rel^2 is a DIFFERENT number and is not computed here.
// The reference side is side A of each RowDiff (a_vs_b: A is the MMQ row; arm_vs_a: A is the arm's MMQ
// row), which the emitted block states in words.
//
// EVERY non-numeric case names itself and emits a NULL value.  There is NO threshold, NO comparison and
// NO verdict anywhere in this code:
//   empty                     no rows in the population: null, count 0 (never 0 = agreement)
//   ok                        denominator > 0 and every row finite: the ratio
//   zero_reference_exact      denominator == 0 AND numerator == 0 (every row is a zero row): 0, named
//   zero_reference_unbounded denominator == 0 AND numerator > 0 (zero reference, nonzero candidate):
//                             NULL - unbounded, never 0, never a large finite number
//   invalid_nonfinite        at least one row carried a NaN/Infinity: NULL, with the count of such rows
//                             (the energies of a nonfinite row are NaN, so a partial sum is refused)
//   invalid_energy           invalid/missing row energies or population overflow: NULL, without inventing
//                             nonfinite input rows. Invalid population energies also emit null, not 0.
inline constexpr const char* kNmseDefinition =
    "sum over the population of (a_i - b_i)^2, divided by sum over the population of a_i^2; "
    "reference side a; energy-weighted, not a mean of per-row ratios; no threshold applied";

enum class NmseState { kEmpty, kOk, kZeroRefExact, kZeroRefUnbounded, kInvalidNonfinite, kInvalidEnergy };
const char* nmse_state_name(NmseState s);   // the exact strings above

struct NmseDiag {
    NmseState state = NmseState::kEmpty;
    bool has_value = false;        // false: the value must be emitted as null, never as 0
    int64_t rows = 0;              // rows in the population
    int64_t rows_ref_zero = 0;     // rows with no reference energy
    int64_t rows_nonfinite = 0;    // rows carrying a NaN/Infinity
    double energy_error = 0.0;     // the numerator; NaN/null for an invalid population, never a fake 0
    double energy_ref = 0.0;       // the denominator; NaN/null for an invalid population
    double nmse = 0.0;             // meaningful only when has_value
};
NmseDiag nmse_diagnostic(const std::vector<RowDiff>& population);

/// PERCENTILE, EXACTLY THIS RULE (pinned by the CPU test, so a reader can reproduce a number by hand):
/// sort ascending, take index = ceil(q * (n - 1)) clamped to [0, n-1] - the nearest rank AT OR ABOVE the
/// fraction.  n = 0 returns NaN (an aggregate over no rows says "no rows", never 0).
double percentile(const std::vector<double>& sorted_ascending, double q);

struct Agg {
    int64_t count = 0;
    double p50 = 0.0, p90 = 0.0, p99 = 0.0, p999 = 0.0, max = 0.0, mean = 0.0;
    bool empty = true;
};
Agg aggregate(std::vector<double> v);   // takes the values, sorts them itself

// ============================== 2. the fixtures ==============================
//
// Eight REAL prompt texts, pinned in source (the acceptance driver's pinned sample text plus seven varied
// realistic texts of clearly different lengths, design §2 line 38's ">=8 prompts").  The harness measures the
// activations the ENGINE produced for these texts (the parent's dumps), so the texts are the run's contract:
// the JSON records each fixture's id, character count and FNV-1a/64 of its UTF-8 bytes, and the CPU test pins
// all of them, so a fixture edited in source fails the build-time gate instead of silently changing a run.

struct Fixture {
    const char* id;
    const char* what;     // what the text is for (length class, shape)
    const char* text;
};
const std::vector<Fixture>& fixtures();                 // 8, in a fixed order
const Fixture* fixture_by_id(const std::string& id);
std::string fixture_list_string();                      // "id,len,fnv" per fixture, joined by ';'

// A fixture's activation rows for one layer (the dump's T rows of N floats + T*K ids).
struct Activations {
    int64_t T = 0;                       // tokens this run uses (<= the dump's T, see --max-tokens)
    int64_t T_dump = 0;                  // the dump's own token count
    std::vector<float> x;                // T * N floats, row-major (the MoE input `mixed`)
    std::vector<int32_t> ids;            // T * K routed expert ids, routing order
    std::string source;                  // "<file>" for a dump, "synthetic:<seed>" for the synthetic mode
    bool synthetic = false;
};

/// Read one `STRATA_DUMP_MOE_INPUT` file: {T, N, K} int64, T*N float32, T*K int32 (prefill.cpp:4010-4024).
/// A typed refusal (never a crash) for: unreadable file, short file, N/K other than the pack's geometry,
/// a token count the harness cannot take.  Truncates to the first `max_tokens` rows (0 = all) and says so
/// in `out.source` is left to the caller; `T_dump` always records the dump's own count.
bool load_moe_dump(const std::string& path, int64_t max_tokens, Activations& out, std::string& err);
/// Deterministic synthetic rows (a documented FALLBACK, marked INFERIOR in every output it appears in):
/// the same generator reproduces the same bytes for (seed, fixture index, layer, T).
void synth_activations(uint64_t seed, int fixture_index, int64_t layer, int64_t T, Activations& out);

// Producer-only diagnostic: compare the actual MMQ D4 scales/integers, decoded into
// logical input order by the GPU driver, with the helper's ACTUAL host quantizer on
// the same floats. No tolerance, verdict or claim about complete expert products.
struct Q8InputDiff {
    int64_t blocks = 0, scale_different = 0, scale_after_half_different = 0;
    int64_t integers_different = 0, integer_blocks_different = 0;
    int64_t first_scale_block = -1, first_integer = -1;
    float first_primary_scale = 0, first_helper_scale = 0;
    int first_primary_integer = 0, first_helper_integer = 0;
    double max_scale_abs = 0, max_reconstructed_abs = 0;
};
// Pinned ggml block_q8_1_mmq D4 layout: four float scales then 128 int8s,
// physical block (column/128)*rows + row. Refuses short/overflowed geometry.
bool decode_mmq_d4(const std::vector<uint8_t>& raw, int64_t rows, int64_t cols,
                   std::vector<float>& scales, std::vector<int8_t>& qs, std::string& err);
bool diff_q8_input(const float* x, int64_t n, const float* primary_scales,
                   const int8_t* primary_qs, Q8InputDiff& out, std::string& err);
std::string q8_input_diff_json(const Q8InputDiff& d);

// Diagnostic counterfactual only: preserve CPU Q8_K/Q8_0 integers and scales in
// the helper's 36-byte Q8_1 wire blocks. Q8_K scales necessarily round to half.
// Also return the original CPU reconstruction, before that half conversion.
bool cpu_activation_to_helper(int type, const std::vector<uint8_t>& raw, int64_t n,
                              std::vector<uint8_t>& wire, std::vector<float>& reconstructed,
                              std::string& err);

// ============================== 3. the layer's claim set ==============================
//
// The design's selection is `select_prompt_rows` over the layer's per-expert token lists.  This builds its
// INPUTS in the shape the CPU share builds them (prefill.cpp:3898-3931: the concatenated per-expert token
// lists in EXPERT order) from the dump's ids, and calls the client's own selection - the harness NEVER
// re-implements the claim rule.

struct LayerCounts {
    std::vector<int32_t> cnt;            // n_expert counts, from the ids
    std::vector<int32_t> toks;           // the per-expert token lists concatenated in expert order
    int64_t rows_all = 0;                // T * K (every routed pair of this layer)
};
int build_layer_counts(const Activations& act, LayerCounts& out, std::string& err);   // 0 = ok, else named

/// The ranked fill order the helper gets: (layer, expert) pairs with cnt > 0, ordered by the run's
/// fixture/layer set, count DESC then expert ASC within a layer (the CPU path's shape), capped.
std::vector<std::pair<int32_t, int32_t>> rank_pairs(
    const std::map<int64_t, std::vector<int32_t>>& cnt_by_layer, int64_t cap);

// ============================== 4. Side B: the real helper prompt-row path ==============================
//
// Drives the engine's OWN client (ExpertHelperClient) against the real helper binary.  Nothing here
// re-implements framing, quantization or validation: begin_prompt builds the frame (and the q8 image, by
// quantize_q8_1_cpu) and finish_prompt validates the reply, exactly as the engine will call them.

struct HelperSetup {
    int64_t slots = 0;                   // 0: the helper's budget decides
    int64_t reserve_mib = 512;
    int64_t hello_timeout_ms = 5000;
    int64_t fill_timeout_ms = 600000;    // the fill copies GiB: the client's own long bounded wait
    int64_t begin_timeout_ms = 5000;
    int64_t rows_timeout_ms = 120000;    // one layer's bounded rows wait
};

struct HelperIdentity {
    int64_t free_mib = 0;
    int64_t resident = 0;                // what the fill report says the helper holds
    int64_t bytes = 0;
    int64_t requested_pairs = 0;
    bool prompt_rows = false;            // HelloAck.features carried kFeaturePromptRows
    std::string note;                    // "legacy 24-byte ack (features=0)" etc.
    std::vector<std::pair<int32_t, int32_t>> resident_pairs;   // the fill report's authoritative (layer, expert)
};

/// hello + kFill + kFillReport.  Refuses with the transport's named status; a typed reason in `err` for
/// the two cases this harness must never sit on: a legacy helper (no feature bit) and an absent one.
int helper_handshake(core::ExpertHelperClient& c, const std::string& socket, const HelperSetup& opt,
                     const std::vector<std::pair<int32_t, int32_t>>& ranked, HelperIdentity& id,
                     std::string& err);

/// One layer's Side-B rows: copy the plan's selected tokens' rows into the tier's OWN pinned buffer (the
/// order begin_prompt quantizes in), begin_prompt, finish_prompt.  `out` is plan.rows() * N floats at the
/// PAIR indices.  A typed refusal for every failure, and it NEVER leaves a row uninitialised.
int helper_prompt_rows(core::ExpertHelperClient& c, int32_t layer, const core::ExpertHelperPromptPlan& plan,
                       const Activations& act, const int32_t* host_res, const HelperSetup& opt,
                       std::vector<float>& out, int32_t& rows, std::string& err);

/// A plan pair (token, expert) -> its Side-A row, THE SINGLE SOURCE of that mapping in this harness.
/// `pair2row` is keyed by the ENTRY index i = token*kK + slot (build_geometry fills it from the routing
/// walk), so a pair's row is pair2row[token*kK + j] for the slot j whose routed id IS the expert
/// (act.ids[token*kK + j] == expert).  Indexing that table by the EXPERT id reads some other token's
/// entry - that is the class of the 2026-10-29 live crash: pair (37, 75) read entry (44, slot 5)'s row
/// 3810, ~39 MB past a 2250-row buffer.  Returns kOk with row = -1 when the routed pair was dropped from
/// this run's rows (the caller's own "no MMQ row" refusal), and a typed refusal naming the pair when the
/// token does not route the expert at all: the plan's claim rule only names routed pairs, so that refusal
/// is an invariant break and says so.
int pair_row(const Activations& act, const int32_t* pair2row, int32_t token, int32_t expert,
             int32_t& row, std::string& err);

/// The side-B READ invariant, to be checked BEFORE the first row is read (the harness calls it at the top
/// of its gate-row loop).  `hb` (the reply's rows, from helper_prompt_rows) must carry a full row at EVERY
/// pair index - hb.size() == plan.rows() * N - and `dm` (Side A's rows) must carry a full row at every
/// claimed pair's side-A row index - pair2row[t*k + j] >= 0 and (row + 1) * N <= dm.size(), the row found
/// through pair_row (the routed-slot lookup, never the expert id as an index).  A violation is a typed
/// refusal naming both sizes: the harness never reads past a reply, and the CPU test drives this function
/// with the live-shaped partial reply, a short `hb` and a short `dm`.
int check_side_b_rows(const Activations& act, const core::ExpertHelperPromptPlan& plan,
                      const int32_t* pair2row, const std::vector<float>& hb, const std::vector<float>& dm,
                      std::string& err);

// ============================== 5. the report ==============================
//
// Schema-stable JSON: a fixed key order, %.12g floats, rows in a canonical order, and NO wall-clock time in
// the document.  The same inputs produce byte-identical bytes; the CPU test proves that.
//
// SCHEMA v2 (2026-11-01, additive - the v1 keys and their numbers are unchanged in shape and meaning):
//   "schema": "strata.p2_row_measure.v2"
//   buckets[i].max_abs / .max_rel are now the aggregate OBJECT (v1 emitted `"max_abs": "v": {...}`, which
//     is not JSON at all - the malformed v1 documents are kept unchanged and read by the audit's legacy path)
//   every row diff gains "n", "sum_sq_err", "sum_sq_ref", "ref_zero", "nonfinite" (the retained energies)
//   a new top-level "nmse" block with the two comparison populations' energy-weighted ratio and its state
//   NaN and +/-Infinity always emit `null`, never a number (v1 printed `inf`, which is not JSON at all);
//   a non-numeric NMSE state emits `null` with its name.
// A strict parser (Python `json.loads`, NaN/Infinity rejected) is the regression gate: the CPU test emits
// this document and the Python test parses it, so an emitter defect can no longer pass a substring check.

/// ONE measured row comparison, keyed as the design's contract says: (prompt, layer, format, expert, token).
///
///   a_vs_b   - the GATE measurement: Side A (the MMQ producer + the MMQ group product, in the group
///              composition a real chunk runs today) against Side B (the helper's `native_expert_grouped` row
///              over quantize_q8_1_cpu bytes).  This is line 81's "per-row max-abs/max-rel".
///   arm_vs_a - only with --arm-groups, and only on rows the ARM keeps LOCAL: the claimed pairs are exactly
///              the pairs the arm moves to the helper, so no arm MMQ row exists for them.  This measures the
///              third deviation source the design names (line 22, "removing experts recomposes 16-expert MMQ
///              groups, so co-grouped local rows round differently"): the same local row, computed with the
///              layer's groups as they are today and with the claimed pairs removed.
struct RowRec {
    std::string fixture;                 // the fixture id
    int64_t fixture_index = 0;
    int32_t layer = 0, expert = 0, token = 0, pair = 0;
    int32_t gu_type = -1, d_type = -1;   // the layer's formats: the design's "per format" key
    RowDiff a_vs_b;                      // Side A (today's MMQ group composition) vs Side B (the helper)
    bool has_arm = false;                // --arm-groups computed this row's arm MMQ row too
    RowDiff arm_vs_a;                    // the arm's MMQ row vs today's MMQ row (the recomposition term)
};

struct FormatInfo { int32_t layer = 0, gu_type = -1, d_type = -1; };

struct ReportMeta {
    std::string harness_version = "p2-row-measure-1";
    std::string pack_dir, socket, activation_mode;
    std::string blob_file;               ///< the blob file the harness read (the file the helper maps)
    std::string blob_mapping;            ///< "experts.bin" (packed) | "gguf-roles" (the per-role slices)
    std::string argv_joined;
    int64_t slots = 0, reserve_mib = 0;
    double eps = kEpsDefault;
    int32_t layer_lo = kLayerLo, layer_hi = kLayerHi;
    int64_t max_tokens = 0;
    bool arm_groups = false;
    bool summary_only = false;
    HelperIdentity helper;
    std::vector<FormatInfo> formats;              // the layers this run measured
    std::vector<std::string> staging;             // fix7: per-layer shape truth, "header ... gguf ..." (the record stamp)
    std::map<std::string, std::pair<int64_t, int64_t>> fixtures_used;   // id -> (tokens, rows)
    std::vector<std::string> notes;               // run notes (capture gaps, truncations, synthetic mode)
    int64_t rows_expected = 0, rows_measured = 0, rows_skipped = 0;
};

struct Report {
    ReportMeta meta;
    std::vector<RowRec> rows;           // the gate rows: the plan's claimed pairs, MMQ vs helper
    std::vector<RowRec> recompose;      // --arm-groups: rows the ARM keeps local, its MMQ row vs today's
};

/// The canonical sort (fixture index, layer, pair) - the harness emits rows in this order, always.
void sort_rows(Report& r);
std::string json_escape(const std::string& s);
/// Fixed schema-v2 JSON only. Malformed legacy emission is not part of the report API.
std::string to_json(const Report& r);
std::string to_table(const Report& r, int64_t worst_n);

/// ---- 5b. the CPU SELF-TEST document (schema v2, 2026-11-01) ----
///
/// A deterministic, GPU-free, pack-free, helper-free `Report` whose rows are FIXED synthetic floats with
/// HAND-COMPUTABLE energies, built so a STRICT JSON PARSER (tools/mixed_gpu/p2_measure/p2_report_audit.py
/// and tests/terminal/test_p2_report_json.py) can parse the harness's own document and check the emitted
/// NMSE against values computed independently in Python.  It is a SELF-TEST of the EMITTER, never a
/// run's measurement: every note in it says so, and it is never written over a run's raw evidence.
///
/// The variants, each a distinct NMSE state, all emitted by the SAME serializer:
///   "ok"                 finite rows with UNEQUAL reference energies: a number, energy-weighted
///   "empty"             no rows at all: null value, count 0 (never 0 = agreement)
///   "zero-ref-nonzero"  every row has zero reference and nonzero candidate: null, named unbounded
///   "zero-ref-exact"    every row is all zeros: 0, named
///   "nonfinite"         one row carries a NaN and one an Infinity: null, named, counted
/// 0 = built, 3 = typed refusal in `err` (an unknown variant).
inline constexpr const char* kSelfTestNote = "CPU self-test document: synthetic rows, no measurement";
int make_cpu_selftest_report(const std::string& variant, Report& out, std::string& err);
inline constexpr const char* kReportSchemaV2 = "strata.p2_row_measure.v2";

/// The aggregates the design names (line 81): p50/p90/p99/p99.9/max per metric, per format and per layer,
/// plus the worst rows with their keys.  Computed from `rows` - no thresholds, no verdicts.
struct Buckets {
    std::map<std::string, std::vector<double>> ab, rel;   // key -> values
};
void collect_buckets(const Report& r, Buckets& b);
std::string ggml_type_name(int32_t t);

// ============================== 6. the pack's blob source ==============================
//
// The harness measures the SAME bytes the helper serves, so it must read the SAME file the helper maps,
// with the SAME mapping.  Three facts, each mirrored from the CUDA helper (tools/mixed_gpu/sidecar/),
// never reinterpreted here:
//
// 1. WHICH FILE.  The helper concatenates its `--experts` value onto the pack dir
//    (helper_main.cpp:708-712: `p = pack; if (p.back() != '/') p += "/"; p += experts_file`), so a
//    RELATIVE value resolves against the PACK DIR - the published packs keep their experts in the model
//    GGUF one level up, and the frozen acceptance driver passes exactly that
//    (run_acceptance.py:70 `HELPER_EXPERTS = "../Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf"`).
//    The join is a concatenation, not a search: an ABSOLUTE value joins too (`<pack>//abs/name`), which
//    is what the helper does with it - the harness mirrors that and refuses the joined path when it is
//    not a file.  The default stays `experts.bin`, i.e. `<pack>/experts.bin`, which is what a pack with
//    a packed blob ships.  The harness takes the same `--experts` and joins it the same way.
//
// 2. THE NAME, when --experts is absent.  native_experts.txt's header names the file its offsets index
//    into: v2 "… the last three are absolute offsets in NAME.gguf)", v3/v4/v5 "… absolute offsets in
//    NAME.gguf, or in the named shard beside it)".  With no --experts and no experts.bin, the harness
//    resolves `<pack>/../NAME.gguf` from that name (tools/iq_pack.py:479-486 writes the header, the
//    name is one whitespace-free token).  Refusal is typed and names EVERY path it tried.
//
// 3. THE MAPPING.  A GGUF-mapped pack has NO packed blob region: the `offset` column describes an
//    experts.bin layout such a pack does not ship, so `blob_offset()` lands on unrelated bytes (the
//    helper's H14 proof: 191/2560 NaN rows there).  A (layer, expert) row is therefore assembled per
//    role, exactly as the helper's fill does (helper_main.cpp:226-247): role r at
//    `gguf_off[3*layer + r] + expert * per[r]`, roles ordered gate|up|down, with
//    `per = {n_ff*gu_row, n_ff*gu_row, n_embd*d_row}` - the same row bytes the layout table's own
//    NativeFmt carries, and the sum checked against the layer's blob_bytes exactly as the helper checks
//    `at != bytes`.  The helper takes that branch when `gguf_off` is 3 x n_layers and NO per-layer shard
//    file is named; a pack that DOES ship experts.bin keeps the packed `blob_offset` read.
struct BlobSource {
    std::string path;        ///< the file the harness reads: the file the helper maps
    bool per_role = false;   ///< true: the GGUF per-role assembly; false: the packed experts.bin offsets
};

/// The helper's own join: the pack dir + '/' + `experts` (a RELATIVE path resolves against the pack; an
/// absolute value joins the same way - the concatenation, exactly as the helper builds its path).
std::string join_experts_path(const std::string& pack_dir, const std::string& experts);

/// The GGUF file name a native_experts.txt HEADER line names for its offsets ("" when it names none).
std::string header_blob_name(const std::string& header_line);

/// The name native_experts.txt's header names, read from `<pack_dir>/native_experts.txt` ("" when the
/// file or the name is missing).  The first header line that names a file wins.
std::string pack_header_blob_name(const std::string& pack_dir);

/// Which mapping this layout implies (the helper's condition verbatim: gguf_off is 3 x n_layers and no
/// per-layer shard file is named).
bool layout_maps_gguf_roles(const kernels::cpu::ExpertLayout& lay);

/// The harness's blob source, with the helper's semantics: `--experts` if given (that exact file, a
/// typed refusal if it is not there), else `<pack>/experts.bin`, else `<pack>/../<header name>`.
/// 0 = `out` resolved; 3 = typed refusal in `err`, naming every path tried (the harness's exit 3).
int resolve_blob_source(const std::string& pack_dir, const std::string& experts_arg,
                        const kernels::cpu::ExpertLayout& lay, BlobSource& out, std::string& err);

/// One (layer, expert) row's BYTES, read the way the helper's fill reads them: the packed blob at
/// `blob_offset(layer, expert)`, or the gate|up|down slices concatenated.  `dst` takes
/// `lay.blob_bytes(layer)` bytes.  Typed false (never a short row) for a layer the layout does not
/// have, a per-role layer whose slices do not sum to the blob, a read that fails, or a row past `end`.
bool read_blob_row(std::istream& f, const kernels::cpu::ExpertLayout& lay, const BlobSource& src,
                   int64_t layer, int64_t expert, uint8_t* dst, uint64_t end, std::string& err);

/// The role slice sizes of one layer (gate, up, down) - the helper's `per[3]`, from the layout's own
/// NativeFmt row bytes.  False when the layer has no formats (the slice sizes are then unusable).
bool blob_role_bytes(const kernels::cpu::ExpertLayout& lay, int64_t layer, uint64_t per[3]);

/// The LAST byte (one past the final byte) the layer's last expert touches, in the file the harness
/// reads - the bound a mapped pack must not run past (the packed form's check is the harness's own
/// `blob_offset(l,0) + blob_bytes(l)*n_expert`).
uint64_t blob_layer_end(const kernels::cpu::ExpertLayout& lay, const BlobSource& src, int64_t layer);

// ---- 6c. the GGUF truth gate (fix7) ----
//
// The pack's native_experts.txt IS the per-layer shape table both the engine's loader and this harness
// resolve from (index-keyed, self-consistent: the loader refuses a line whose blob disagrees with its
// own format geometry).  What the loader CANNOT see is the table's AGREEMENT WITH THE MODEL: a header
// line can be internally consistent and still name the wrong ggml types for its layer, exactly the
// 2026-10-29 live anomaly (the pack's L33 line reads 21/42 = 1,868,800 B and L34's 21/20 = 2,329,600 B,
// while the GGUF's own tensor metadata for L33 is the 16/42 shape = 1,305,600 B and for L34 the 17/20
// shape = 1,868,800 B; the packer wrote the per-layer columns keyed on encounter order, not layer
// index).  A harness running such a pack reads the WRONG byte ranges (the numerics are invalid) and is
// what the live fault is pinned to.  The engine's own loader path enforces this equality
// (`check_experts_gguf`, src/core/expert_source.cpp); this gate is that same check for the harness,
// per layer, TYPED: it prints `header: ... gguf: ...` for every layer it inspects and refuses the run
// naming the first divergence.  The GGUF's tensor metadata is the source of record.
struct LayerTruth {
    int64_t layer = 0;
    int gu_type = -1, d_type = -1;        // the header's types
    uint64_t header_blob = 0;             // the header's blob_bytes
    int gguf_gu_type = -1, gguf_d_type = -1;   // the GGUF's gate/gate/down tensor types
    uint64_t gguf_blob = 0;               // 2*gate tensor bytes + down tensor bytes (per expert)
};

/// Checks [lo, hi] of a GGUF-mapped layout against the GGUF's own tensor metadata (name, type, shape,
/// per-expert bytes, expert-0 offset per role), appending one LayerTruth per layer to `out` for EVERY
/// inspected layer - equal or not.  A layout whose header names per-layer shards (v4), a GGUF that
/// cannot be opened, a missing role tensor, any type/shape/bytes/offset divergence: false with a typed
/// `err` naming the layer, the tensor, the header's numbers and the GGUF's.
bool check_gguf_truth(const std::string& native_gguf, const kernels::cpu::ExpertLayout& lay,
                      int64_t lo, int64_t hi, std::vector<LayerTruth>& out, std::string& err);

// ============================== 7. the run's per-layer sizing and the plan's claims ==============================
//
// Both are here, and not in the harness's main, so the CPU suite pins them: every number below is a
// per-layer value the harness must size its buffers by, and the claims array is the indexed array the
// arm's geometry walk reads.  The staging ring is the 32-slot device ring of section 7's comment in
// p2_measure_main.cpp; a ring's stride is the LAYER's blob size, so a run whose window mixes blob
// sizes needs the ring of the LARGEST layer in the estimate (the guard) and of EACH layer in the
// allocation.  Sizing the guard by one layer's blob is the 2026-10-29 window-1 defect: it under-says
// the need (a false refusal is exit 4, an under-say is no protection).
constexpr int kStageRingSlots = 32;

/// One layer's staging ring: kStageRingSlots blobs of `blob_bytes` (the ring's stride IS the blob).
size_t stage_ring_bytes(uint64_t blob_bytes);

/// The largest `lay.blob_bytes(l)` over l in [lo, hi], 0 when the window is empty or outside the
/// layout.  The device-memory guard sizes the ring, and the group buffers, by the window's MAXIMUM.
uint64_t max_blob_bytes(const kernels::cpu::ExpertLayout& lay, int64_t lo, int64_t hi);

// ---- 7b. the per-layer MMQ buffer sizes and the extents the ops reach (fix5, single-sourced) ----
//
// The harness allocates EVERY device buffer fresh per (fixture, layer) (DevMem, p2_measure_main.cpp), so
// each layer's writes are bounded by that layer's own numbers: L32 2,329,600 B (gu 21/d 20), L33
// 1,305,600 (gu 16/d 42), L34 1,868,800 (gu 17/d 20) - a grow after a shrink inside one fixture's walk.
// These functions ARE the allocation sizes, and the byte extents the gather, the MMQ launches and the
// activation tiles reach; the CPU suite (section 11) compares them for a real grow-after-shrink
// sequence, and the harness allocates, memsets and writes ONLY through them.
constexpr int64_t kMmqGroup = 16;      // MMQ_GROUP, prefill.cpp:1152
constexpr size_t  kMmqTail  = 4096;    // MMQ_TAIL,  prefill.cpp:1156 (zeroed after a group's last expert)

/// The group's gate/up buffer: `kMmqGroup` slots of `mmq_gub` + the zeroed tail region after the LAST
/// slot.  `over_read` is mmq::overread_bytes(gu_type, kN): the K tile's over-read past the last expert's
/// bytes (prefill.cpp:1242-1246 tracks the same number for the resident slots' guard) - the tail region
/// covers it, so the allocation is max(kMmqTail, over_read) wide there.
size_t mmq_group_gu_bytes(size_t mmq_gub, size_t over_read);

/// The group's down buffer: the same rule at `mmq_db` and mmq::overread_bytes(d_type, 640).
size_t mmq_group_d_bytes(size_t mmq_db, size_t over_read);

/// The NVFP4 tail words, ONE 16-byte word per group slot: `kMmqGroup * 16` bytes, in a buffer of their
/// OWN.  gather_native_group's kernel writes tail_dst[q] (16 B) for every slot q of the group, and
/// gather_native writes one word at tail_dst; the engine keeps them in a dedicated `grp_tail` of
/// MMQ_GROUP * 4 floats (prefill.cpp:1645) with a stride of 16 bytes per expert (the MMQ kernel reads
/// tails[4*q], moe_mmq.cu:158).  A tail_dst INSIDE the down buffer writes past its end - by up to
/// kMmqGroup * 16 = 256 B - and a 4-byte stride overlaps the words of neighbouring experts.
size_t mmq_tail_words_bytes();

/// The last byte the gathered writes reach in a group buffer of slot stride `slot_bytes`: the last slot's
/// bytes, then the zeroed tail of `zero_bytes` (the kernels write the zero region right after the last
/// slot: copy16_group_kernel at (q+1)*stride, copy1_kernel at q*stride + the expert's bytes).
size_t gather_write_bytes(int ngx, size_t slot_bytes, size_t zero_bytes);

/// The last byte the MMQ's weight reads reach in a group buffer: the last expert's slot + its K tile's
/// over-read (`over_read`).
size_t mmq_weight_read_bytes(int ngx, size_t slot_bytes, size_t over_read);

/// The rows the harness must keep PAST the last real activation row: MMQ reads whole 16-row M tiles, so
/// a row count that is not a multiple of 16 has its last tile read up to 15 rows past the last row.
/// The harness's own tail was 8 rows (the comment at p2_measure_main.cpp:91 took it from the 4096 B
/// MMQ_TAIL convention), which covers only a 16-aligned row count - 16 rows covers any row count.
int64_t mmq_act_tail_rows();

// ---- 7c. the per-op SYNC attribution instruments (fix8) ----
//
// A device fault (cudaErrorIllegalAddress and its family) is ASYNCHRONOUS and STICKY: the driver records
// it when the GPU finishes the faulting op, and it SURFACES at the FIRST CUDA API call made after that -
// i.e. at the first SYNC, not at the op that caused it.  That is the whole shape of the live L34 fault
// (attempts 10-12): `REFUSAL (side A): pinned-sample @ layer 34: blob upload: an illegal memory access`.
// The call that REPORTED it is the q=0 `cudaMemcpyAsync` of L34's first group; the ops that ran between
// the last sync (the prologue's, the only one) and that call are the ring probe, the x upload, the
// activation quantizer and the bounds upload - and the fix5 checkpoints cannot tell them apart, because
// `cudaGetLastError()` WITHOUT a sync can only pop an error the driver has ALREADY recorded, so an
// unsurfaced async fault stays invisible to it.  The fix7 per-slot label sat on the checkpoint AFTER the
// upload, so the upload's own failing call printed its fixed literal and the label never ran.
//
// The instruments therefore SYNC after every op and print the op's OWN parameters when it faults.  The
// strings are HERE, in the CPU-only half, so the CPU suite pins their exact shape without a GPU.
struct OpProbe {                  // the parameters of ONE op of the run
    const char* op = "";          // "blob upload" | "act quantize" | "bounds upload" | "gather group" | ...
    long long q = -1;             // the expert's index INSIDE the group (blob uploads; -1 = no parameter)
    long long slot = -1;          // the ring slot the upload wrote: (j0+q) % kStageRingSlots
    long long entry = -1;         // the group-order entry index j0+q
    const void* src = nullptr;    // the copy's source (the host row); null for a kernel
    const void* dst = nullptr;    // the copy's destination (device); null for a kernel
    unsigned long long bytes = 0; // the copy's size
    const void* stream = nullptr; // the stream the op ran on
    const char* src_attr = "";    // cudaPointerGetAttributes(src)'s type: unregistered-host|host|device|managed
    const char* dst_attr = "";
    const char* src_rc = "";      // the attribute CALL's own result, named (a failing call is evidence too)
    const char* dst_rc = "";
    // fix9: for a PRODUCT launch, the rows its kernel writes in the dst buffer, counted FROM THE POINTER
    // the launch was given (section 7d), and that buffer's size.  `w_last == 0` means the op is not a
    // product and the fault line keeps fix8's exact shape.
    long long w_first = 0;        // first written row (from the pointer)
    long long w_last = 0;         // one past the last written row
    unsigned long long alloc = 0; // the dst allocation's bytes, from the pointer's base
    const char* fault = "";       // the op's OWN sync result, named ("" when the op completed clean)
};

/// The op label the harness prints for ONE op: "blob upload q0 slot0 entry0" when the op carries its
/// parameters, the bare op name when it does not (entry < 0).  THE SAME LABEL goes into the refusal path's
/// error string, so a refusal names the expert, the slot and the entry - not a fixed literal.
std::string op_label(const char* op, long long q, long long slot, long long entry);

/// cudaMemoryType's name, by its int value (this file takes the number, not the CUDA header):
/// 0 unregistered-host, 1 host, 2 device, 3 managed, anything else "unknown(N)".
const char* cuda_memory_type_name(int type);

/// The typed per-op fault line, printed at the op's OWN sync:
///   FAULT-AT <fixture> L<layer> <op label>: <error> | src P dst P n N stream P
///            src-attr T(RC) dst-attr T(RC)
/// One line, stderr.  `fixture`/`layer` name the (fixture, layer) the op belongs to.
std::string format_fault_line(const char* fixture, int64_t layer, const OpProbe& p, const char* err);

/// The layer's per-op parameter set, printed ONCE per (fixture, layer) so a layer that measures clean and
/// a layer that faults with IDENTICAL types and blob size can be diffed in one place (the live L32 vs L34
/// pair: both 21/20, both 2,329,600 B, both a 74,547,200 B ring - so the differing dimension is in the
/// geometry: the row counts, the group windows, the offsets, the stream).
struct OpWindow {
    int64_t layer = 0, T = 0, entries = 0, rows = 0, order_n = 0, groups = 0, nr_max = 0;
    uint64_t blob = 0, ring = 0;
    unsigned long long gu_bytes = 0, down_bytes = 0;      // the layer's per-expert MMQ slot sizes
    const void* stream = nullptr;
    const void* ring_base = nullptr;                       // the ring's slot 0
    std::vector<std::pair<long long, long long>> wins;     // per group: (j0, q-count)
    std::vector<long long> r0;                             // per group: its first row
    std::vector<long long> nr;                             // per group: the rows it computes
};
std::string format_op_summary(const char* fixture, const OpWindow& w);

// ---- 7d. the MMQ product's dst write geometry (fix9) ----
//
// The MMQ kernel with `ids_dst` set writes at `dst[ids_dst[col] * ld_dst + i]` where `col` is the ABSOLUTE
// column taken from `expert_bounds` (mmq.cuh:1036-1059 in the main path, 1153 in the stream-k path, 1375 in
// the fixup kernel: `ids_dst_shared[j] = ids_dst[col_low + jt*J + j]`, then `write_back` at
// `dst[ids_dst[j]*stride + i]`).  So for a launch whose bounds are the CHUNK's row indices (the gate/up
// product, bounds = `bounds + j0`) and whose ids are the identity, the dst pointer MUST be the buffer's
// BASE, and the write spans rows [r0, r0+nr) of it; a pointer offset by r0 rows lands the group's products
// at row r0 + col, i.e. r0 rows past their place, and the group runs off the buffer as soon as
// r0 + nr > rows.  For a launch whose bounds are the GROUP's (the down product, bounds =
// `bounds + n + 1 + gg*(MMQ_GROUP+1)`, columns 0..nr), the offset pointer `base + r0*ld` is right and the
// write spans [0, nr) of it.  The engine follows exactly this (prefill.cpp:4666 `gu.dst = m.GU` with
// absolute bounds; :4682 `dn.dst = m.Dm + r0 * N` with group-relative bounds).
//
// This is the 2026-10-30 live fault: the harness's gate/up site passed `d.gu + r0*1280` WITH absolute
// bounds, so group j0=176 (r0 1498, nr 137) wrote to gu + (1498+137)*5120 = 16,040,960 B of an
// 11,520,000 B buffer - unmapped VA, an illegal access at its own sync.  The group count is NOT the
// mechanism: L32 (12 groups) overshoots MORE at its last group (r0 2247 -> gu + 23,019,520) and only
// escaped a fault because the overshoot stayed inside the mapped VA of the buffers allocated after gu.
struct ProductGeom {
    bool valid = false;
    bool absolute_bounds = true;    // true: expert_bounds are the CHUNK's rows (gate/up); false: the group's (down)
    long long r0 = 0;               // the group's first row of the chunk
    long long nr = 0;               // the group's row count
    long long ld_dst = 0;           // the dst row stride, in floats
    long long dst_row_offset = 0;   // the rows the dst POINTER sits past its buffer's base (0 for gate/up)
};

/// The rows the kernel writes, counted from the pointer given to the launch: absolute bounds ->
/// [r0, r0+nr) (ids[absolute col] = col); group-relative -> [0, nr).
long long product_write_first_row(const ProductGeom& g);
long long product_write_last_row(const ProductGeom& g);        // one past the last written row

/// The last byte the write reaches, measured from that pointer: last_row * ld_dst * sizeof(float).
size_t product_write_bytes_from_dst(const ProductGeom& g);

/// The write fits a dst buffer of `dst_rows` rows, given the pointer's offset into it.
bool product_write_fits(const ProductGeom& g, long long dst_rows);

/// The gate/up and down output buffers, rows rows wide (prefill.cpp:1242 GU = ne*K*4, Dm = ne*N*4): the
/// harness allocates ONLY through these, and the fit check compares the write against them.
size_t mmq_gu_buffer_bytes(int64_t rows);
size_t mmq_dm_buffer_bytes(int64_t rows);

/// The typed refusal of a product launch whose write would run past its buffer - the harness refuses such
/// a launch on the CPU (exit 4) and never hands it to the device.  The line names the group, the rows the
/// kernel writes FROM THE POINTER it was given, the pointer's offset into the buffer, the buffer's rows
/// and bytes, and how far past the end the write runs.
std::string product_write_refusal(const char* which, bool absolute_bounds, long long j0, long long r0,
                                  long long nr, long long ld_dst, long long dst_row_offset,
                                  long long dst_rows, unsigned long long alloc_bytes);

/// The plan's claims over the layer's routed ENTRIES: `claims` is sized act.T*kK here (the entry index
/// space build_geometry and prefill.cpp:3954-3976 walk), and the entry a (token, expert) pair claims is
/// the entry `i` in [token*kK, token*kK+kK) whose routed id IS that expert - it is NOT `token*kK + expert`,
/// which is an entry of a DIFFERENT token as soon as the expert id reaches kK, and runs off the end of
/// the array for the last tokens' high ids (the 2026-10-29 window-1 heap corruption, glibc "corrupted
/// size vs. prev_size").  Refuses (typed) for a pair whose expert is not routed by its token.
int mark_claim_entries(const Activations& act, const core::ExpertHelperPromptPlan& plan,
                       std::vector<uint8_t>& claims, std::string& err);

}  // namespace p2
}  // namespace tools
}  // namespace strata
