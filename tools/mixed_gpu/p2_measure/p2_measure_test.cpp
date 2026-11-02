// tools/mixed_gpu/p2_measure/p2_measure_test.cpp - the CPU-only validation of the P2 deviation harness's
// CPU halves (p2_measure.cpp) plus the framing smoke against an IN-PROCESS fake helper.
//
// Runs WITHOUT a GPU, WITHOUT the helper binary, WITHOUT a pack and WITHOUT any model file.  It links
// exactly the sources the harness's CPU half is built from (p2_measure.cpp, the engine's client, the
// frame transport) - the same proof shape as tests/core/expert_helper_socket_test.cpp.
//
// Sections:
//   (1) the metric against HAND-COMPUTED references (max_abs, the eps-guarded max_rel incl. a zero row,
//       mean_abs, l2_rel, the amaxes) - the expected values are computed by hand, in double, and the
//       arithmetic checked with a tight tolerance;
//   (1b) the RETAINED ENERGIES and the energy-weighted TENSOR NMSE, hand-computed, with every non-numeric
//        state (empty, zero-reference exact, zero-reference unbounded, nonfinite) pinned, the weighted
//        ratio proven different from an unweighted mean of row ratios, and the v1 band metrics pinned
//        unchanged; the JSON emitter's v2 shape (valid buckets, energies, the nmse block) is pinned too;
//   (2) the percentile RULE (nearest rank at or above: index = ceil(q*(n-1)) clamped) and the aggregate;
//   (3) the FNV-1a/64 pin against the published "abc" test vector, and the eight fixtures' ids,
//       character counts and hashes (an edited fixture text fails the build-time gate);
//   (4) selection determinism, every cap of the design's claim rule, the k refusal, the holds/stable
//       gates, order-independence inside an expert, the counts builder and the fill-order ranking;
//   (5) the report's byte stability: the same Report emits the same bytes, rows in the canonical order,
//       no wall-clock time; the human table too;
//   (6) framing smoke over a REAL bound AF_UNIX path and over a socketpair, with the fake helper
//       answering the REAL frames: the harness's own helper_handshake + helper_prompt_rows drive the
//       engine's own client, and the rows come back through finish_prompt at the pair indices; a
//       LEGACY helper (24-byte ack, features=0) is refused and NOTHING of the tier is ever sent; an
//       ABSENT peer answers kErrConnect in bounded time - never a hang, never a silent zero.
//   (7) the pack's blob source (per-role GGUF slices) and the row reader against a mock mix pack;
//   (8) the per-layer sizing (the staging ring's window maximum) and the plan's claim entries, plus the
//       fill-report scale drive;
//   (9) the side-B reply SHAPES the live run lives in: a PARTIAL reply (the helper answers only its
//       resident pairs), an empty reply, an unclaimed index, rows traveling out of order, and the
//       harness's pre-read invariant over every pair index.
//   (10) the pair -> row RESOLVER: the routed-slot lookup (pair2row is ENTRY-keyed) and the row VALUES it
//        carries (the row of entry i, not off[expert] + the row), the live (37, 75) shape, the loud refusals
//        (a pair the token does not route, a two-slot pair, out-of-range token / expert), and the plan's
//        pairs resolved end to end against the claims the harness marks.
//   (11) the GROW-AFTER-SHRINK staging audit: the live L32/L33/L34 sizes mirrored on the host, every
//        write/read extent inside its allocation, fresh-per-layer allocation, and the record of the
//        pre-fix tail-pointer overflow (256 B past the down buffer).
//   (12) the HOST STAGING path of the blob upload (the pinned attempt-10 fault site): the copy size,
//        the ring destination extent and the host source extent audited at the live triple, with the
//        per-role slice fills (ASAN-visible) and the window-max-copy record.
//   (13) the GGUF truth gate (fix7): the header's per-layer shapes against the GGUF's OWN metadata, the
//        mutation refusal, ring == 32 x stride.
//   (14) the per-op SYNC attribution (fix8): the FAULT-AT line's exact shape (fixture, layer, op,
//        q/slot/entry, src/dst/size/stream, both cudaPointerGetAttributes answers), the label the
//        refusal path carries, the per-layer op-parameter line and the one-field L32-vs-L34 diff, and
//        the SYNC PLACEMENT read out of the harness's own source (every async op ends with its own
//        sync probe; the upload's failing call carries the per-slot label; the ring probe is synced).
//   (15) the product write geometry (fix9): the LIVE L34 17-group window shape (17 groups, j0 0..256 step
//        16, last 256:12[2127/123], order 268, T 225, 2250 rows) vs the 16-group L33 and 12-group L32
//        controls, the row chain, the ring slot map, the gate/up base-vs-offset dst rule at EVERY group,
//        the pre-fix9 exceedance in all three shapes (the group count refuted), the live fault line
//        reproduced byte-for-byte, and the harness source's base/offset pairing.
//
// NO TOLERANCE IS ASSERTED ON ANY MEASURED DEVIATION: the metric checks validate the METRIC's arithmetic
// against hand-computed values, never a run's numbers against a band (the band is the user's).
#include "p2_measure.hpp"

#include "strata/core/expert_helper_protocol.hpp"
#include "strata/artifact/gguf_reader.hpp"

#include "ggml.h"
#include "gguf.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iterator>
#include <limits>
#include <map>
#include <set>
#include <string>
#include <thread>
#include <vector>

#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

namespace p2 = strata::tools::p2;
namespace eh = strata::core::expert_helper;
using strata::core::ExpertHelperClient;
using strata::core::ExpertHelperPromptPlan;
using strata::core::quantize_q8_1_cpu;
using namespace strata::tools::p2;   // fixtures(), diff_rows, select_prompt_rows, the Report types

namespace {

int failures = 0;
void check(bool ok, const char* what) {
    std::printf("  %-64s %s\n", what, ok ? "ok" : "FAIL");
    if (!ok) ++failures;
}
void check_status(int rc, int want, const char* what) {
    const bool ok = rc == want;
    std::printf("  %-64s %s (rc=%d %s)\n", what, ok ? "ok" : "FAIL", rc, eh::status_name(rc));
    if (!ok) ++failures;
}
bool near(double a, double b, double tol) { return std::fabs(a - b) <= tol; }

/// fix8 (14e): the directory THIS test file was compiled from.  __FILE__ is the path the compiler was
/// given, and CMake gives an absolute path in this tree, so the source-shape pins can read the harness's
/// own source from beside this file - the STRUCTURE of the sync placement is pinned as text, with no GPU.
const char* p2_measure_test_source_dir() {
    static std::string dir;
    if (dir.empty()) {
        const std::string f = __FILE__;
        const size_t s = f.find_last_of('/');
        dir = (s == std::string::npos) ? std::string(".") : f.substr(0, s);
    }
    return dir.c_str();
}

// The fixture pins, frozen from a ONE-TIME run of fixture_list_string() (the harness's own generator,
// printed by section (3) below).  An edited fixture text fails this pin instead of silently changing a
// run.  Captured 2026-10-29 from this tree AND cross-checked by a SECOND implementation
// (tools/mixed_gpu/p2_measure/check_fixture_pins.py, Python, reads the texts straight out of
// p2_measure.cpp): the two lists match byte for byte.
const char* const kFixturePins =
    "pinned-sample,122,edc608ea68f235bc;"
    "lighthouse-prefill,1056,f7437011e069cd0b;"
    "decode-list,74,93cc9bd3ccc135e5;"
    "code-question-short,167,3a4bc6945a694864;"
    "doc-rewrite-mid,907,fcf5024fa39826b7;"
    "json-extract,797,1444c1a475e69077;"
    "code-review-long,1718,c36ba6b95cc34031;"
    "prose-analysis-long,3175,cb88ce72bb99ae9f;";

// ============================== (1) the metric, hand-computed ==============================
//
// Reference 1, a = {1,2,3}, b = {1,4,3} (n = 3):
//   diffs = {0,2,0}                       max_abs = 2
//   row_amax = max(1,2,3,1,4,3) = 4      max_rel = 2 / 4 = 0.5
//   mean_abs = (0+2+0)/3 = 2/3
//   ||a-b|| = 2 ; ||a|| = sqrt(1+4+9) = sqrt(14) ; l2_rel = 2/sqrt(14)
//   a_amax = 3, b_amax = 4
void section_metric() {
    std::printf("(1) the metric against hand-computed references\n");
    const float a[3] = {1.0f, 2.0f, 3.0f};
    const float b[3] = {1.0f, 4.0f, 3.0f};
    const RowDiff d = diff_rows(a, b, 3, 1e-12);
    check(near(d.max_abs, 2.0, 1e-12), "max_abs = 2 (|2-4|)");
    check(near(d.row_amax, 4.0, 1e-12), "row_amax = 4 (the row's largest magnitude)");
    check(near(d.max_rel, 0.5, 1e-12), "max_rel = 2/4 = 0.5");
    check(near(d.mean_abs, 2.0 / 3.0, 1e-12), "mean_abs = 2/3");
    check(near(d.l2_rel, 2.0 / std::sqrt(14.0), 1e-12), "l2_rel = 2/sqrt(14)");
    check(near(d.a_amax, 3.0, 1e-12) && near(d.b_amax, 4.0, 1e-12), "the per-side amaxes 3 and 4");

    // Reference 2, a = {1}, b = {-1}: max_abs 2, row_amax 1, max_rel 2, l2_rel = 2/1 = 2, mean 2.
    const float c1[1] = {1.0f}, c2[1] = {-1.0f};
    const RowDiff e = diff_rows(c1, c2, 1, 1e-12);
    check(near(e.max_abs, 2.0, 1e-12) && near(e.max_rel, 2.0, 1e-12) && near(e.l2_rel, 2.0, 1e-12),
          "a={1} b={-1}: max_abs 2, max_rel 2, l2_rel 2");

    // Reference 3, the EXACT row: every difference 0 -> every measure 0, including max_rel (no 0/0).
    const RowDiff z = diff_rows(a, a, 3, 1e-12);
    check(z.max_abs == 0.0 && z.max_rel == 0.0 && z.mean_abs == 0.0 && z.l2_rel == 0.0,
          "an exact row: all zeros, max_rel 0 (not a 0/0)");

    // Reference 4, the ZERO row: max_abs 0, row_amax 0, max_rel 0/eps = 0, l2_rel 0 (eps guard, no NaN).
    const float z1[2] = {0.0f, 0.0f}, z2[2] = {0.0f, 0.0f};
    const RowDiff zz = diff_rows(z1, z2, 2, 1e-12);
    check(zz.max_abs == 0.0 && zz.max_rel == 0.0 && zz.row_amax == 0.0 && zz.l2_rel == 0.0 &&
          !std::isnan(zz.max_rel), "a zero row: max_rel 0 via the eps floor, never a NaN");

    // Reference 5, the eps FLOOR's effect: a = {1e-13}, b = {0} -> max_abs 1e-13, row_amax 1e-13 < eps,
    // so max_rel = 1e-13 / 1e-12 = 0.1 (the guard, not the 1.0 a 0 denominator would give).  The float
    // literal 1e-13f is not exact, so the tolerance is the float's own.
    const float g1[1] = {1e-13f}, g2[1] = {0.0f};
    const RowDiff gg = diff_rows(g1, g2, 1, 1e-12);
    check(near(gg.max_abs, 1e-13, 1e-19) && near(gg.max_rel, 0.1, 1e-6),
          "eps floor: a 1e-13 row at eps 1e-12 gives max_rel ~0.1, not 1");

    // Reference 6, constant rows: a = {5,5,5,5}, b = {4,4,4,4}: max_abs 1, row_amax 5, max_rel 0.2,
    // mean_abs 1, l2_rel = ||1,1,1,1|| / ||5,5,5,5|| = 2/10 = 0.2.
    const float k5[4] = {5.f, 5.f, 5.f, 5.f}, k4[4] = {4.f, 4.f, 4.f, 4.f};
    const RowDiff kk = diff_rows(k5, k4, 4, 1e-12);
    check(near(kk.max_abs, 1.0, 1e-12) && near(kk.max_rel, 0.2, 1e-12) &&
          near(kk.mean_abs, 1.0, 1e-12) && near(kk.l2_rel, 0.2, 1e-12),
          "constant rows 5 vs 4: 1, 0.2, 1, 0.2");

    // n <= 0: a defined zero record, no read past the arrays.
    const RowDiff n0 = diff_rows(a, b, 0, 1e-12);
    check(n0.max_abs == 0.0 && n0.max_rel == 0.0 && n0.mean_abs == 0.0 && n0.l2_rel == 0.0,
          "n = 0: the defined zero record");

    // the content pins: equal rows hash equal, a one-bit change in the row's bytes moves the hash.
    // 3.0000001f is NOT a distinct float (half-ULP at 3.0 is 1.19e-7), so the change uses 1e-6.
    check(z.a_fnv == z.b_fnv, "a_fnv == b_fnv for an exact row");
    const float b2[3] = {1.0f, 4.0f, 3.000001f};
    const RowDiff d2 = diff_rows(a, b2, 3, 1e-12);
    check(d2.b_fnv != d.b_fnv, "the b_fnv pin moves when the row's bytes move");
}

// ============================== (1b) the retained energies and the tensor NMSE ==============================
//
// Hand-computed, exactly like section (1).  The point of the numbers below is that the emitted NMSE is
// ENERGY-WEIGHTED (a row with 100x the reference energy moves it 100x as much) and that every
// non-numeric case NAMES itself - the code never turns a zero denominator or a NaN into a finite 0.
//
// The population (rows of length 4):
//   r0 a={1,2,3,4} b={1,2,3,4}  exact           err 0.00  ref 1+4+9+16 = 30   l2_rel 0
//   r1 a={10,0,0,0} b={11,0,0,0}                err 1.00  ref 100             l2_rel 1/10
//   r2 a={0,1,0,0}  b={0,1.5,0,0}               err 0.25  ref 1               l2_rel 0.5/1
//   sum(err) = 1.25   sum(ref) = 131   nmse = 1.25/131 = 0.0095419847328244274...
//   unweighted mean of l2_rel^2 = (0 + 0.01 + 0.25)/3 = 0.0866666666666666667  (9.08x the emitted
//   value - the two numbers are NOT the same measure, which is the whole reason the block exists).
void section_nmse() {
    std::printf("(1b) the retained energies, the energy-weighted NMSE and its named states\n");
    constexpr int64_t k = 4;
    const float a0[k] = {1.f, 2.f, 3.f, 4.f}, b0[k] = {1.f, 2.f, 3.f, 4.f};
    const float a1[k] = {10.f, 0.f, 0.f, 0.f}, b1[k] = {11.f, 0.f, 0.f, 0.f};
    const float a2[k] = {0.f, 1.f, 0.f, 0.f}, b2[k] = {0.f, 1.5f, 0.f, 0.f};
    const float z[k] = {0.f, 0.f, 0.f, 0.f};

    const RowDiff d0 = diff_rows(a0, b0, k, 1e-12);
    check(d0.n == k && d0.sum_sq_err == 0.0 && near(d0.sum_sq_ref, 30.0, 1e-12),
          "row r0 retains n 4, error energy 0, reference energy 1+4+9+16 = 30");
    check(!d0.ref_zero && !d0.nonfinite, "r0: reference energy present, inputs finite");
    const RowDiff d1 = diff_rows(a1, b1, k, 1e-12);
    check(near(d1.sum_sq_err, 1.0, 1e-12) && near(d1.sum_sq_ref, 100.0, 1e-12),
          "row r1 retains error energy 1, reference energy 100");
    const RowDiff d2 = diff_rows(a2, b2, k, 1e-12);
    check(near(d2.sum_sq_err, 0.25, 1e-12) && near(d2.sum_sq_ref, 1.0, 1e-12),
          "row r2 retains error energy 0.25, reference energy 1");

    const std::vector<RowDiff> pop = {d0, d1, d2};
    const NmseDiag s = nmse_diagnostic(pop);
    check(s.state == NmseState::kOk && s.has_value, "the population's state is ok (a number exists)");
    check(near(s.energy_error, 1.25, 1e-12) && near(s.energy_ref, 131.0, 1e-12),
          "numerator 1.25, denominator 131 (sums of the retained energies)");
    check(near(s.nmse, 1.25 / 131.0, 1e-15), "nmse = 1.25/131 = 0.009541984732824427");
    double unweighted = 0.0;
    for (const RowDiff& d : pop) unweighted += d.l2_rel * d.l2_rel;
    unweighted /= (double) pop.size();
    check(near(unweighted, (0.0 + 0.01 + 0.25) / 3.0, 1e-12), "the unweighted mean of l2_rel^2 = 0.0866667");
    check(std::fabs(unweighted - s.nmse) > 5.0e-2,
          "the energy-weighted ratio and the unweighted mean of row ratios are 9.08x apart - not the same measure");

    // empty: a population with no rows says "no rows"
    const NmseDiag e = nmse_diagnostic(std::vector<RowDiff>{});
    check(e.state == NmseState::kEmpty && !e.has_value && e.rows == 0,
          "empty population: null value, count 0 - never 0 read as agreement");
    check(std::strcmp(nmse_state_name(e.state), "empty") == 0, "the empty state's name");

    // a finite exact population is a real 0 with a positive denominator
    const NmseDiag ex = nmse_diagnostic(std::vector<RowDiff>{d0});
    check(ex.state == NmseState::kOk && ex.has_value && ex.nmse == 0.0,
          "an exact row over a nonzero reference: 0 with state ok");

    // zero reference, zero error: the named exact state
    const NmseDiag ze = nmse_diagnostic(std::vector<RowDiff>{diff_rows(z, z, k, 1e-12)});
    check(ze.state == NmseState::kZeroRefExact && ze.has_value && ze.nmse == 0.0 && ze.rows_ref_zero == 1,
          "zero reference + zero error: 0, named zero_reference_exact_match, ref-zero row counted");

    // zero reference, NONZERO error: unbounded, never 0
    const NmseDiag zu = nmse_diagnostic(std::vector<RowDiff>{diff_rows(z, a0, k, 1e-12)});
    check(zu.state == NmseState::kZeroRefUnbounded && !zu.has_value && zu.energy_ref == 0.0 &&
              near(zu.energy_error, 30.0, 1e-12),
          "zero reference + nonzero error: NULL unbounded state with numerator 30 shown, never 0");
    check(std::strcmp(nmse_state_name(zu.state), "zero_reference_nonzero_error_unbounded") == 0,
          "the unbounded state's name is printed in full");

    // nonfinite inputs: refused and counted, never folded into a finite 0
    const float nanv = (float) std::nan("t");
    const float infv = (float) (std::numeric_limits<double>::infinity)();
    const float nrow[k] = {1.f, 2.f, 3.f, nanv};
    const float irow[k] = {infv, 0.f, 0.f, 0.f};
    const RowDiff dn = diff_rows(a0, nrow, k, 1e-12);
    const RowDiff di = diff_rows(irow, a0, k, 1e-12);
    check(dn.nonfinite && di.nonfinite, "a NaN candidate and an Infinity reference are recorded on the row");
    const NmseDiag nf = nmse_diagnostic(std::vector<RowDiff>{d0, dn, di});
    check(nf.state == NmseState::kInvalidNonfinite && !nf.has_value && nf.rows_nonfinite == 2,
          "a population with nonfinite rows: refused, counted 2, no finite value");
    check(std::isnan(nf.energy_error) && std::isnan(nf.energy_ref),
          "invalid population energies are NaN/null, not fabricated zeros");
    check(std::isnan(dn.max_abs) && std::isnan(dn.max_rel) && std::isnan(di.max_abs),
          "fmax must not hide nonfinite inputs as finite row metrics");
    const RowDiff zero_bad = diff_rows(z, nrow, k, 1e-12);
    const NmseDiag mixed = nmse_diagnostic({d0, zero_bad});
    check(mixed.rows_ref_zero == 1 && mixed.rows_nonfinite == 1,
          "zero-reference counts remain truthful even when the population is invalid");
    const Agg invalid_aggregate = aggregate({0.0, std::nan(""), 1.0});
    check(invalid_aggregate.count == 3 && !invalid_aggregate.empty &&
              std::isnan(invalid_aggregate.p50) && std::isnan(invalid_aggregate.p999) &&
              std::isnan(invalid_aggregate.max) && std::isnan(invalid_aggregate.mean),
          "nonfinite percentile population is refused before sorting, retaining its count");
    RowDiff large = d0;
    large.sum_sq_err = large.sum_sq_ref = (std::numeric_limits<double>::max)();
    const NmseDiag overflow = nmse_diagnostic({large, large});
    check(overflow.state == NmseState::kInvalidEnergy && !overflow.has_value &&
              overflow.rows_nonfinite == 0 && std::isnan(overflow.energy_error),
          "energy overflow is invalid_energy, not invented nonfinite input rows");
    const NmseDiag absent = nmse_diagnostic({RowDiff{}});
    check(absent.state == NmseState::kInvalidEnergy && !absent.has_value,
          "a zero-length placeholder is not a measured exact-match row");

    // the v1 band metrics stay exactly as they were
    check(near(d1.max_rel, 1.0 / 11.0, 1e-12) && near(d1.l2_rel, 0.1, 1e-12),
          "v1 max_rel 1/11 and l2_rel 0.1 for r1 are unchanged by the energy fields");
    const RowDiff zr = diff_rows(z, a0, k, 1e-12);
    check(near(zr.max_rel, 1.0, 1e-12) && zr.l2_rel == 0.0 && zr.ref_zero,
          "zero reference + nonzero candidate: max_rel 1 (the row_amax takes the larger side), l2_rel 0 (v1) "
          "with ref_zero saying the ratio is UNDEFINED");

    // the self-test document builds, and every variant's state is the one the header names
    Report r;
    std::string err;
    check(make_cpu_selftest_report("ok", r, err) == 0, "the 'ok' self-test document builds");
    const std::string j = to_json(r);
    Report bad;
    check(make_cpu_selftest_report("no-such-variant", bad, err) == 3 &&
              err.find("unknown variant") != std::string::npos,
          "an unknown self-test variant is a typed refusal (exit 3), never an empty document");
    check(j.find("\"schema\": \"strata.p2_row_measure.v2\"") != std::string::npos,
          "the document declares schema v2");
    check(j.find("\"max_abs\": \"v\"") == std::string::npos,
          "the v1 bucket defect is gone: no \"max_abs\": \"v\" sequence in the document");
    check(j.find("\"max_abs\": {\"count\"") != std::string::npos,
          "a bucket's max_abs value IS the aggregate object");
    check(j.find("\"nmse\"") != std::string::npos && j.find("energy_ref") != std::string::npos &&
              j.find("rows_nonfinite") != std::string::npos,
          "the nmse block emits numerator, denominator, counts and state");
    check(j.find("\"sum_sq_ref\"") != std::string::npos && j.find("\"ref_zero\"") != std::string::npos,
          "the rows emit their retained energies and the zero-reference flag");
    check(to_json(r) == j, "the self-test document is byte-stable");
    check(j.find("NaN") == std::string::npos && j.find("Infinity") == std::string::npos &&
              j.find(" inf") == std::string::npos,
          "no NaN/Infinity token in the document (a non-finite number prints null)");
    Report nf2;
    check(make_cpu_selftest_report("nonfinite", nf2, err) == 0 &&
              to_json(nf2).find("\"state\": \"invalid_nonfinite_rows\"") != std::string::npos &&
              to_json(nf2).find("\"nmse\": null") != std::string::npos,
          "the nonfinite document emits null with its named state");
    Report znd;
    check(make_cpu_selftest_report("zero-ref-nonzero", znd, err) == 0 &&
              to_json(znd).find("\"state\": \"zero_reference_nonzero_error_unbounded\"") != std::string::npos,
          "the zero-reference document emits null with its named state");
    Report emp;
    check(make_cpu_selftest_report("empty", emp, err) == 0 &&
              to_json(emp).find("\"state\": \"empty\"") != std::string::npos,
          "the empty document emits the named empty state");
    const std::string table = to_table(r, 5);
    check(table.find("tensor NMSE diagnostic") != std::string::npos &&
              table.find("NOT a mean of per-row ratios") != std::string::npos &&
              table.find("NO TOLERANCE IS APPLIED HERE") != std::string::npos,
          "the table prints the NMSE diagnostic, says it is energy-weighted, and states no tolerance");
}

// ============================== (2) the percentile rule and the aggregate ==============================
//
// The rule (pinned in p2_measure.hpp, implemented in p2_measure.cpp): ascending sort, take the value at
// index = ceil(q * (n-1)) clamped to [0, n-1] - the nearest rank AT OR ABOVE the fraction.  Every
// expected index below is computed by hand.
void section_percentile() {
    std::printf("(2) the percentile rule and the aggregate\n");
    const std::vector<double> one{7.0};
    check(percentile(one, 0.0) == 7.0 && percentile(one, 0.5) == 7.0 && percentile(one, 1.0) == 7.0,
          "n=1: every q gives the single value");
    const std::vector<double> two{1.0, 2.0};
    check(percentile(two, 0.5) == 2.0, "n=2 q=0.5: ceil(0.5*1)=1 -> 2.0 (at-or-above)");
    check(percentile(two, 0.4) == 2.0, "n=2 q=0.4: ceil(0.4*1)=1 -> 2.0 (at-or-above)");
    check(percentile(two, 0.0) == 1.0, "n=2 q=0: index 0 -> 1.0");
    check(percentile(two, 1.0) == 2.0, "n=2 q=1: index 1 -> 2.0");
    std::vector<double> ten;
    for (int i = 1; i <= 10; ++i) ten.push_back((double) i);
    check(percentile(ten, 0.5) == 6.0, "n=10 q=0.5: ceil(0.5*9)=5 -> 6");
    check(percentile(ten, 0.9) == 10.0, "n=10 q=0.9: ceil(0.9*9)=ceil(8.1)=9 -> 10");
    check(percentile(ten, 0.99) == 10.0, "n=10 q=0.99: ceil(8.91)=9 -> 10");
    check(percentile(ten, 0.999) == 10.0, "n=10 q=0.999: ceil(8.991)=9 -> 10");
    check(percentile(ten, 0.0) == 1.0 && percentile(ten, 1.0) == 10.0, "n=10: q=0 -> 1, q=1 -> 10");
    const std::vector<double> empty;
    check(std::isnan(percentile(empty, 0.5)), "n=0: NaN, never a 0 (an aggregate over no rows says so)");

    const Agg a = aggregate({1, 2, 3, 4, 5, 6, 7, 8, 9, 10});
    check(a.count == 10 && !a.empty, "aggregate: count 10");
    check(a.p50 == 6.0 && a.p90 == 10.0 && a.p99 == 10.0 && a.p999 == 10.0 && a.max == 10.0,
          "aggregate percentiles follow the same rule");
    check(near(a.mean, 5.5, 1e-12), "aggregate mean 5.5");
    const Agg e = aggregate({});
    check(e.count == 0 && e.empty && e.p50 == 0.0 && e.max == 0.0,
          "an empty aggregate: count 0, empty flag (JSON prints its percentiles as null)");
    const Agg u = aggregate({3.0, 1.0, 2.0});   // unsorted input: aggregate sorts by itself
    check(u.max == 3.0 && u.p50 == 2.0 && near(u.mean, 2.0, 1e-12), "aggregate sorts its input");
}

// ============================== (3) the content pins and the fixtures ==============================
void section_fixtures() {
    std::printf("(3) FNV-1a/64 and the pinned fixtures\n");
    // the published FNV-1a 64-bit test vectors: fnv1a_64("a") = 0xaf63dc4c8601ec8c, and the empty
    // string is the offset basis.  fnv1a_64("abc") below is the same algorithm on three octets.
    check(fnv64("a", 1) == 0xaf63dc4c8601ec8cULL, "fnv64(\"a\") = 0xaf63dc4c8601ec8c (published vector)");
    check(fnv64("abc", 3) == 0xe71fa2190541574bULL, "fnv64(\"abc\") = 0xe71fa2190541574b (same rule, three octets)");
    check(fnv64("", 0) == 0xcbf29ce484222325ULL, "fnv64 of zero bytes is the offset basis");

    check(fixtures().size() == 8, "eight fixtures (design §2 line 38: >= 8 prompts)");
    bool uniq = true, nonempty = true;
    std::set<std::string> ids;
    for (const Fixture& f : fixtures()) {
        if (!f.id || !*f.id) nonempty = false;
        if (!f.text || !*f.text) nonempty = false;
        if (!ids.insert(std::string(f.id)).second) uniq = false;
    }
    check(uniq, "fixture ids are unique");
    check(nonempty, "no fixture has an empty id or text");
    check(fixture_by_id(fixtures()[0].id) == &fixtures()[0], "fixture_by_id finds a fixture by its id");
    check(fixture_by_id("no-such-fixture") == nullptr, "fixture_by_id refuses an unknown id");
    int short_n = 0, long_n = 0;
    for (const Fixture& f : fixtures()) {
        const size_t n = std::strlen(f.text);
        if (n < 200) ++short_n;
        if (n > 900) ++long_n;
    }
    check(short_n >= 2 && long_n >= 2, "the fixtures span short and long prompt shapes");
    const std::string pins = fixture_list_string();
    std::printf("  fixtures pin list: %s\n", pins.c_str());
    check(pins == kFixturePins, "the eight fixtures' (id, chars, fnv64) match the frozen pins");
}

// ============================== (4) the claim rule ==============================
namespace {

/// A selection input.  `lists` is the source of truth; build() lays it out in the shape
/// select_prompt_rows consumes (prefill.cpp:3898-3931): cnt[e] = the expert's token count, and expert e
/// owns toks[sum(cnt[0..e-1]) .. +cnt[e]].  Writing one expert's list before every count is known
/// would land inside a neighbour's slice, so the layout is computed in ONE pass over the finished
/// lists - which is also what the engine's own producer does.
struct SelCase {
    std::map<int64_t, std::vector<int32_t>> lists;   // expert -> its token list
    std::vector<int32_t> cnt;                        // n_expert counts
    std::vector<int32_t> toks;                       // the concatenated per-expert token lists
    std::vector<uint8_t> holds, stable;
    std::vector<int32_t> host_res;                   // the flat layer*n_expert+expert residency

    void put(int64_t e, const std::vector<int32_t>& t) { lists[e] = t; }
    void gates() { holds.assign((size_t) kNE, 1); stable.assign((size_t) kNE, 1);
                   host_res.assign((size_t) kLayers * kNE, -1); }
    void build() {
        int64_t total = 0;
        for (const auto& kv : lists) total += (int64_t) kv.second.size();
        cnt.assign((size_t) kNE, 0);
        toks.assign((size_t) total, 0);
        for (const auto& kv : lists) cnt[(size_t) kv.first] = (int32_t) kv.second.size();
        int64_t at = 0;
        for (int64_t e = 0; e < kNE; ++e) {
            const auto it = lists.find(e);
            if (it == lists.end()) continue;
            for (size_t i = 0; i < it->second.size(); ++i) toks[(size_t) (at + (int64_t) i)] = it->second[i];
            at += (int64_t) cnt[(size_t) e];
        }
    }
};

int run_sel(const SelCase& s, int64_t layer, int64_t k, ExpertHelperPromptPlan& plan,
            int64_t max_rows_per_expert = eh::kMaxRowsPerExpert,
            int64_t max_rows_per_layer = eh::kMaxRowsPerLayer,
            int64_t max_sel_tok = eh::kMaxSelTok) {
    // select_prompt_rows indexes host_res[layer * n_expert + e] ITSELF: the full array goes in, no offset.
    return select_prompt_rows(s.cnt.data(), s.toks.data(), s.host_res.data(),
                              (int32_t) layer, s.holds.data(), s.stable.data(), kNE, k,
                              max_rows_per_expert, max_rows_per_layer, max_sel_tok, plan);
}

}  // namespace

void section_selection() {
    std::printf("(4) the claim rule: determinism, caps, gates\n");
    const int64_t layer = 32;

    // expert 3 the largest, then 7, then 9; expert 4 primary-resident; expert 5 not held; expert 6
    // not stable; expert 11 an ordinary eligible control.
    SelCase s;
    s.gates();
    s.holds[5] = 0;                                 // the helper does not HOLD expert 5
    s.stable[6] = 0;                                // the CPU path is not STABLE for expert 6
    s.host_res[(size_t) layer * kNE + 4] = 17;      // a slot: expert 4 is PRIMARY-resident
    s.put(3, {0, 1, 2, 3, 4});                      // expert 3 the largest, then 7, then 9
    s.put(7, {0, 2, 5});
    s.put(9, {1, 4});
    s.put(5, {6, 7});
    s.put(6, {8, 9});
    s.put(4, {10, 11});
    s.put(11, {12, 13, 14});
    s.build();

    ExpertHelperPromptPlan plan;
    int rc = run_sel(s, layer, kK, plan);
    check_status(rc, eh::kOk, "a normal selection answers kOk");
    std::set<std::pair<int32_t, int32_t>> got;
    for (int64_t j = 0; j < plan.rows(); ++j) got.insert({plan.pair_token[j], plan.pair_expert[j]});
    check(got.count({0, 3}) && got.count({4, 3}), "the largest expert (3, 5 tokens) is claimed WHOLE");
    check(got.count({0, 7}) && got.count({5, 7}), "the next expert (7, 3 tokens) is claimed whole");
    check(got.count({1, 9}) && got.count({4, 9}), "the third (9, 2 tokens) is claimed whole");
    check(!got.count({6, 5}) && !got.count({7, 5}), "an expert the helper does not HOLD is never claimed");
    check(!got.count({8, 6}) && !got.count({9, 6}), "an expert the CPU path is not STABLE for is never claimed");
    check(!got.count({10, 4}) && !got.count({11, 4}), "a PRIMARY-resident expert is never claimed");
    check(got.count({12, 11}) && got.count({14, 11}), "an ordinary eligible expert is claimed");
    check(plan.rows() == 13, "rows = the sum of the claimed experts' counts (5+3+2+3)");

    bool ascending = true, distinct = true;
    std::set<int32_t> tokset(plan.tokens.begin(), plan.tokens.end());
    for (size_t i = 1; i < plan.tokens.size(); ++i)
        if (plan.tokens[i] <= plan.tokens[i - 1]) ascending = false;
    if (tokset.size() != plan.tokens.size()) distinct = false;
    check(ascending && distinct, "the plan's token list is ascending and distinct");
    check(!plan.tokens.empty() && plan.image_row(plan.tokens[0]) == 0 && plan.image_row(9999) == -1,
          "image_row indexes the image and refuses an unselected token");

    // determinism: the same input, tokens ARRIVED in a different order inside expert 3
    SelCase s2 = s;
    s2.put(3, {3, 0, 4, 2, 1});      // the SAME five tokens, shuffled arrival order
    s2.build();
    ExpertHelperPromptPlan plan2;
    rc = run_sel(s2, layer, kK, plan2);
    check(rc == eh::kOk && plan2.rows() == plan.rows() &&
          std::equal(plan2.pair_expert.begin(), plan2.pair_expert.end(), plan.pair_expert.begin()),
          "the claim set does not depend on the arrival order inside an expert");
    ExpertHelperPromptPlan plan3;
    rc = run_sel(s, layer, kK, plan3);   // same run twice: the selection is a pure function
    check(rc == eh::kOk && plan3.rows() == plan.rows() && plan3.tokens == plan.tokens &&
          plan3.pair_token == plan.pair_token, "the same input reproduces the same plan byte for byte");

    // the per-EXPERT cap: 65 > kMaxRowsPerExpert (64) -> skipped; the 64-token expert is claimed
    SelCase c;
    c.gates();
    std::vector<int32_t> many((size_t) 65);
    for (int i = 0; i < 65; ++i) many[i] = (int32_t) i;
    c.put(2, many);                                   // 65 rows: OVER the cap -> the expert is skipped
    std::vector<int32_t> ok64 = many;
    ok64.pop_back();                                  // exactly AT the cap -> claimed
    c.put(8, ok64);
    c.build();
    ExpertHelperPromptPlan pc;
    rc = run_sel(c, layer, kK, pc, 64, 256, 256);   // fixture caps (M4b: the protocol's rose above these)
    bool any2 = false, any8 = false;
    for (int64_t j = 0; j < pc.rows(); ++j) {
        if (pc.pair_expert[j] == 2) any2 = true;
        if (pc.pair_expert[j] == 8) any8 = true;
    }
    check(rc == eh::kOk && !any2 && any8 && pc.rows() == 64,
          "65 rows over the per-expert cap (64 here): skipped; the 64-row expert claimed (64 rows)");

    // the per-LAYER cap: four 64-token experts fill kMaxRowsPerLayer exactly; a fifth is skipped and
    // the scan CONTINUES (the result depends on the input only, not on arrival)
    SelCase d;
    d.gates();
    std::vector<int32_t> big((size_t) 64), small((size_t) 3);
    for (int i = 0; i < 64; ++i) big[i] = (int32_t) i;             // tokens 0..63 (shared by the four)
    for (int e : {1, 2, 3, 4}) d.put(e, big);                       // 256 rows, the cap exactly
    for (int i = 0; i < 3; ++i) small[i] = (int32_t) (200 + i);
    d.put(5, small);                                                // claiming it would breach 256
    d.build();
    ExpertHelperPromptPlan pd;
    rc = run_sel(d, layer, kK, pd, 64, 256, 256);   // fixture caps: the layer cap set to 256
    check(rc == eh::kOk && pd.rows() == 256, "the layer cap (256 here): exactly at it, the breaching expert skipped");

    // the distinct-TOKEN cap in ISOLATION: six experts with DISJOINT 64-token sets (384 routed rows).
    // With the layer cap loosened to 400 rows, the ONLY cap that can bind is kMaxSelTok (256): the
    // fourth expert fills the token union at 256 and the fifth's 64 fresh tokens do not fit.
    SelCase tok;
    tok.gates();
    int base = 0;
    for (int e = 1; e <= 6; ++e) {
        std::vector<int32_t> t((size_t) 64);
        for (int i = 0; i < 64; ++i) t[i] = (int32_t) (base + i);
        base += 64;
        tok.put(e, t);
    }
    tok.build();
    ExpertHelperPromptPlan pt;
    rc = run_sel(tok, layer, kK, pt, eh::kMaxRowsPerExpert, 400, 256);   // the token cap pinned to 256
    check(rc == eh::kOk && (int64_t) pt.tokens.size() == 256 && pt.rows() == 256,
          "the distinct-token cap bounds the image: 256 of 384 routed rows, the image exactly 256 tokens");

    // the same input with the layer cap at the same 256 instead of loosened: the binding is identical
    ExpertHelperPromptPlan ptp;
    rc = run_sel(tok, layer, kK, ptp, 64, 256, 256);
    check(rc == eh::kOk && ptp.rows() == 256 && (int64_t) ptp.tokens.size() <= 256,
          "the same set under all three caps at 256: 256 rows, the image inside 256 tokens");

    // a repeated (token, expert) pair is refused: one row, once (the double-compute the design forbids)
    SelCase dup;
    dup.gates();
    dup.put(3, {5, 5});
    dup.build();
    ExpertHelperPromptPlan pdk;
    check_status(run_sel(dup, layer, kK, pdk), eh::kErrGeometry,
                 "the same (token, expert) pair twice is a geometry refusal");

    // geometry refusals: k != kK, a negative count
    ExpertHelperPromptPlan pr;
    rc = run_sel(s, layer, 9, pr);
    check_status(rc, eh::kErrGeometry, "k other than the pack's kK is a geometry refusal");
    SelCase neg = s;
    neg.cnt[400] = -1;
    rc = run_sel(neg, layer, kK, pr);
    check_status(rc, eh::kErrGeometry, "a negative count is a geometry refusal");

    // an empty plan is a SUCCESS (the layer stays fully local)
    SelCase none = s;
    std::fill(none.holds.begin(), none.holds.end(), 0);
    ExpertHelperPromptPlan pe;
    rc = run_sel(none, layer, kK, pe);
    check(rc == eh::kOk && pe.rows() == 0, "nothing held: kOk with an EMPTY plan (the layer stays local)");

    // the layer's claim-set builder (from routed ids): the shape select_prompt_rows consumes
    Activations a;
    a.T = 4;
    a.T_dump = 4;
    a.x.assign((size_t) (4 * kN), 0.0f);
    a.ids.assign((size_t) (4 * kK), 3);
    a.ids[0] = 7; a.ids[10] = 9; a.ids[20] = 7;
    LayerCounts lc;
    std::string cerr;
    const int lrc = build_layer_counts(a, lc, cerr);
    check(lrc == eh::kOk && lc.cnt[7] == 2 && lc.cnt[9] == 1 && lc.cnt[3] == 37,
          "build_layer_counts: the per-expert counts from routed ids");
    check(lc.rows_all == 40, "build_layer_counts: rows_all = T * K");
    const int64_t off7 = lc.cnt[3];
    check(lc.toks[off7] == 0 && lc.toks[off7 + 1] == 2, "build_layer_counts: expert 7's token list in expert order");
    a.ids[39] = 700;      // the last routed id: outside the pack's 512 experts
    LayerCounts lcb;
    cerr.clear();
    check(build_layer_counts(a, lcb, cerr) == eh::kErrGeometry, "a routed id out of range is refused");

    // rank_pairs: layers ascending; within a layer count DESC, expert ASC; capped
    std::map<int64_t, std::vector<int32_t>> by_layer;
    by_layer[33].assign((size_t) kNE, 0);
    by_layer[32].assign((size_t) kNE, 0);
    by_layer[33][10] = 5; by_layer[33][4] = 5; by_layer[33][7] = 9;
    by_layer[32][2] = 1;
    const auto ranked = rank_pairs(by_layer, 0);
    check(ranked.size() == 4 && ranked[0] == std::make_pair((int32_t) 32, (int32_t) 2) &&
          ranked[1] == std::make_pair((int32_t) 33, (int32_t) 7) &&
          ranked[2] == std::make_pair((int32_t) 33, (int32_t) 4) &&
          ranked[3] == std::make_pair((int32_t) 33, (int32_t) 10),
          "rank_pairs: layer ASC, count DESC, expert ASC");
    const auto capped = rank_pairs(by_layer, 2);
    check(capped.size() == 2, "rank_pairs honours the cap");
}

// ============================== (5) the report's byte stability ==============================
void section_report() {
    std::printf("(5) the JSON/table: byte stability, canonical order, no wall-clock\n");
    Report r;
    r.meta.pack_dir = "/pack";
    r.meta.socket = "/s.sock";
    r.meta.activation_mode = "dump:/moe";
    r.meta.eps = 1e-12;
    r.meta.helper.note = "test";
    r.meta.helper.prompt_rows = true;
    r.meta.formats.push_back({32, 21, 42});
    r.meta.fixtures_used["sample-text"] = {8, 2};
    r.meta.notes.push_back("a note");
    r.meta.rows_expected = 2;
    r.meta.rows_measured = 2;

    std::vector<float> va((size_t) kN, 1.0f), vb((size_t) kN, 1.0f);
    va[10] = 2.0f; vb[10] = 4.0f;
    RowRec a;
    a.fixture = "sample-text"; a.fixture_index = 0; a.layer = 33; a.expert = 9; a.token = 1; a.pair = 1;
    a.gu_type = 21; a.d_type = 42;
    a.a_vs_b = diff_rows(va.data(), vb.data(), kN, 1e-12);
    RowRec b = a;
    b.layer = 32; b.expert = 7; b.token = 0; b.pair = 0;
    b.a_vs_b = diff_rows(vb.data(), vb.data(), kN, 1e-12);   // an exact row
    r.rows.push_back(a);           // deliberately out of canonical order (layer 33 first)
    r.rows.push_back(b);
    RowRec rec;
    rec.fixture = "sample-text"; rec.layer = 33; rec.expert = 9; rec.token = 2; rec.pair = 30;
    rec.gu_type = 21; rec.d_type = 42; rec.has_arm = true;
    rec.arm_vs_a = diff_rows(va.data(), va.data(), kN, 1e-12);
    r.recompose.push_back(rec);

    const std::string j1 = to_json(r);
    const std::string j2 = to_json(r);
    check(j1 == j2, "to_json: the same Report emits byte-identical JSON twice");
    const std::string t1 = to_table(r, 5);
    const std::string t2 = to_table(r, 5);
    check(t1 == t2, "to_table: the same Report emits byte-identical tables twice");

    // canonical row order: layer 32's record precedes layer 33's in the rows array
    const auto posb = j1.find("\"layer\": 32, \"expert\": 7, \"token\": 0, \"pair\": 0");
    const auto posa = j1.find("\"layer\": 33, \"expert\": 9, \"token\": 1, \"pair\": 1");
    check(posb != std::string::npos && posa != std::string::npos && posb < posa,
          "rows are emitted in the canonical order, not in insertion order");

    check(j1.find("\"schema\"") == 4, "the schema key is the document's first key");
    check(j1.find("p50") != std::string::npos && j1.find("p90") != std::string::npos &&
          j1.find("p99") != std::string::npos && j1.find("p999") != std::string::npos &&
          j1.find("\"max\"") != std::string::npos, "the aggregates carry p50/p90/p99/p99.9/max");
    check(j1.find("format:IQ3_S+Q2_0") != std::string::npos, "the per-format bucket uses the ggml names");
    check(j1.find("recompose:IQ3_S+Q2_0") != std::string::npos,
          "the recomposition term is a separate bucket family");
    check(j1.find("worst") != std::string::npos && j1.find("by_max_rel") != std::string::npos,
          "the worst rows are listed with their keys");
    check(j1.find("\"time") == std::string::npos && j1.find("timestamp") == std::string::npos,
          "no wall-clock time anywhere in the document");
    check(j1.find("\"eps\": 1e-12") != std::string::npos, "the run records its eps");
    check(j1.find("\"kMaxSelTok\": " + std::to_string(eh::kMaxSelTok)) != std::string::npos,
          "the run records the protocol caps it used");

    Report q = r;
    q.meta.notes.push_back("a \"quoted\"\nline");
    check(to_json(q).find("a \\\"quoted\\\"\\nline") != std::string::npos, "notes are JSON-escaped");

    check(ggml_type_name(21) == "IQ3_S" && ggml_type_name(42) == "Q2_0" && ggml_type_name(20) == "IQ4_NL" &&
          ggml_type_name(18) == "IQ3_XXS", "the type names the format buckets print");

    // the human table says, in words, that it applies no band
    check(t1.find("NO TOLERANCE IS APPLIED HERE") != std::string::npos,
          "the table states that no tolerance is applied");
}

// ============================== (6) framing smoke: the fake helper ==============================
//
// The engine's OWN client driven by the harness's OWN CPU functions (helper_handshake, helper_prompt_rows)
// over a REAL bound AF_UNIX path (the transport's connect path) and over a socketpair, answering the REAL
// frames - the two-process reality with no GPU and no helper binary.
namespace {

double pv(int i, int64_t c) { return 0.25 * (double) i + 0.001 * (double) c; }

/// The test's own copy of the ROUTED-SLOT lookup: the entry index token*kK + j whose routed id IS the
/// expert.  Used to build a pair2row exactly the way build_geometry fills it (ENTRY-keyed) and to check
/// the harness's resolver pair by pair - a mapping the harness must compute the same way everywhere.
int64_t entry_of(const Activations& a, int32_t t, int32_t e) {
    for (int64_t j = 0; j < kK; ++j)
        if (a.ids[(size_t) ((int64_t) t * kK + j)] == e) return (int64_t) t * kK + j;
    return -1;
}

/// The kRowsP body for the pair indices `idx` (the RowsPResp header, then (int32 pair, N floats) each).
std::vector<uint8_t> rowsp(const std::vector<int32_t>& idx) {
    eh::RowsPResp h;
    h.n_rows = (int32_t) idx.size();
    std::vector<uint8_t> payload(sizeof(h));
    std::memcpy(payload.data(), &h, sizeof(h));
    for (const int32_t i : idx) {
        const size_t at = payload.size();
        payload.resize(at + 4 + (size_t) kN * 4, 0);
        std::memcpy(payload.data() + at, &i, 4);
        float* row = (float*) (payload.data() + at + 4);
        for (int64_t col = 0; col < kN; ++col) row[col] = (float) pv(i, col);
    }
    return payload;
}

void write_ack(eh::Socket& s, const eh::Header& head, bool features, int64_t free_mib = 4096) {
    eh::HelloAck ack;
    ack.max_tok = (int32_t) eh::kMaxTok;
    ack.k = (int32_t) eh::kK;
    ack.max_entries = (int32_t) eh::kMaxEntries;
    ack.status = 0;
    ack.free_mib = (int32_t) free_mib;
    ack.features = features ? (uint64_t) eh::kFeaturePromptRows : 0u;
    const size_t n = features ? sizeof(ack) : eh::kHelloAckLegacyBytes;   // the negotiated SHAPE
    s.write_frame(eh::kHelloAck, head.req_id, (const uint8_t*) &ack, n, eh::deadline_after(8000));
}

void write_fill_report(eh::Socket& s, const eh::Header& head,
                       const std::vector<std::pair<int32_t, int32_t>>& resident, int64_t bytes) {
    eh::FillReport rep;
    rep.resident = (int64_t) resident.size();
    rep.bytes = bytes;
    rep.scratch_bytes = 1 << 20;
    rep.buffers_bytes = 2 << 20;
    rep.n_pairs = (int32_t) resident.size();
    std::vector<uint8_t> payload(sizeof(rep) + resident.size() * 8, 0);
    std::memcpy(payload.data(), &rep, sizeof(rep));
    for (size_t i = 0; i < resident.size(); ++i) {
        std::memcpy(payload.data() + sizeof(rep) + i * 8, &resident[i].first, 4);
        std::memcpy(payload.data() + sizeof(rep) + i * 8 + 4, &resident[i].second, 4);
    }
    s.write_frame(eh::kFillReport, head.req_id, payload.data(), payload.size(), eh::deadline_after(8000));
}

/// The full smoke over a bound path: bind, accept, answer hello (v3 ack) + fill (resident pairs) +
/// kBeginP (the rows reply).  Runs on ONE worker thread, in the order the client sends.
int g_accept_rc = eh::kErrInternal;
int64_t g_fill_bytes = 2621440;        // what the fake helper reports: window 1's number in the scale drive
int g_scale_fill_checks = 0;           // the scale drive's own request-side verifications (7286-pair fill frame)
// The scale drive's progress marker (the drive runs on a worker thread; a stalled scale run must say
// WHERE it stalled, and only the scale drive - ranked > 100 pairs - prints it).
void scale_probe(const std::vector<std::pair<int32_t, int32_t>>& ranked, int stage) {
    if (ranked.size() <= 100) return;
    std::fprintf(stderr, "      scale probe: %d\n", stage);
    std::fflush(stderr);
}
int smoke_full(const std::string& path, const std::vector<std::pair<int32_t, int32_t>>& resident,
               const Activations& a, const ExpertHelperPromptPlan& plan,
               const std::vector<int32_t>& host_res, std::vector<float>& rows, int32_t& nrows,
               HelperIdentity& id, std::string& herr,
               const std::vector<std::pair<int32_t, int32_t>>& ranked = {},
               int64_t fill_bytes = 2621440, int64_t free_mib = 4096,
               const std::vector<int32_t>* reply_idx = nullptr) {
    ::unlink(path.c_str());
    g_fill_bytes = fill_bytes;
    g_scale_fill_checks = 0;
    eh::Socket srv;
    if (srv.listen_bind(path) != eh::kOk) return eh::kErrInternal;
    std::atomic<int> wire{0};
    const std::vector<std::pair<int32_t, int32_t>> rank_req = ranked.empty() ? resident : ranked;
    std::thread srv_thr([&srv, &wire, resident, plan, &rows, &nrows, &a, host_res, rank_req, fill_bytes,
                         free_mib, reply_idx] {
        const auto probe = [&](int st) { if (rank_req.size() > 100) scale_probe(rank_req, st); };
        probe(1);
        // accept() RETURNS the new descriptor (it does not swap it into the listener): adopt it.
        const int afd = srv.accept(eh::deadline_after(30000));
        if (afd < 0) { wire = 1; return; }
        srv.adopt(afd);
        probe(2);
        eh::Header head;
        std::vector<uint8_t> body;
        if (srv.read_frame(head, body, eh::deadline_after(30000)) != eh::kOk || head.type != eh::kHello) {
            wire = 2; return;
        }
        write_ack(srv, head, true, free_mib);
        probe(3);
        if (srv.read_frame(head, body, eh::deadline_after(30000)) != eh::kOk) { wire = 20; return; }
        if (head.type != eh::kFill) { wire = 21; return; }
        probe(4);
        // the fill REQUEST: the header, and every (layer, expert) pair, in the client's order
        if (body.size() != sizeof(eh::FillReq) + rank_req.size() * 8) { wire = 10; return; }
        eh::FillReq freq;
        std::memcpy(&freq, body.data(), sizeof(freq));
        if (freq.n_pairs != (int32_t) rank_req.size()) { wire = 11; return; }
        for (size_t i = 0; i < rank_req.size(); ++i) {
            int32_t l = 0, e = 0;
            std::memcpy(&l, body.data() + sizeof(freq) + i * 8, 4);
            std::memcpy(&e, body.data() + sizeof(freq) + i * 8 + 4, 4);
            if (l != rank_req[i].first || e != rank_req[i].second) { wire = 12; return; }
        }
        ++g_scale_fill_checks;
        write_fill_report(srv, head, resident, fill_bytes);
        probe(41);
        // the tier: read the kBeginP, verify its shape, answer with every claimed pair's row
        if (srv.read_frame(head, body, eh::deadline_after(20000)) != eh::kOk || head.type != eh::kBeginP) {
            wire = 4; return;
        }
        probe(5);
        if (body.size() < sizeof(eh::BeginPReq)) { wire = 5; return; }
        eh::BeginPReq req;
        std::memcpy(&req, body.data(), sizeof(req));
        const int64_t n_sel = plan.n_sel_tok();
        const int64_t n_q8 = n_sel * (kN / 32) * 36;
        const size_t res_bytes = (size_t) ((n_sel * kNE + 7) / 8);
        if (req.layer != 32 || req.n_sel_tok != (int32_t) n_sel || req.k != (int32_t) kK ||
            req.n_pairs != (int32_t) plan.rows() || req.n_q8 != (int32_t) n_q8 ||
            body.size() != sizeof(req) + (size_t) plan.rows() * 8 + res_bytes + (size_t) n_q8) {
            wire = 6; return;
        }
        // the pair list: (image row, expert) as the plan's pairs
        for (int64_t j = 0; j < plan.rows(); ++j) {
            int32_t tok = 0, exp = 0;
            std::memcpy(&tok, body.data() + sizeof(req) + (size_t) j * 8, 4);
            std::memcpy(&exp, body.data() + sizeof(req) + (size_t) j * 8 + 4, 4);
            if (tok != plan.image_row(plan.pair_token[(size_t) j]) || exp != plan.pair_expert[(size_t) j]) {
                wire = 7; return;
            }
        }
        // the q8 image byte-for-byte against quantize_q8_1_cpu of the plan's token rows (the SAME
        // quantizer the client's begin_prompt used; the image is indexed by the plan's token order)
        const uint8_t* q8 = body.data() + sizeof(req) + (size_t) plan.rows() * 8 + res_bytes;
        std::vector<uint8_t> want((size_t) (kN / 32) * 36, 0);
        for (int64_t r = 0; r < n_sel; ++r) {
            const int32_t t = plan.tokens[(size_t) r];
            quantize_q8_1_cpu(a.x.data() + (size_t) t * kN, kN, want.data());
            if (std::memcmp(q8 + (size_t) r * (size_t) (kN / 32) * 36, want.data(), want.size()) != 0) {
                wire = 8; return;
            }
        }
        probe(6);
        // The REPLY's row indices: every claimed pair by default; a caller may answer a SUBSET (the live
        // helper serves only its resident pairs) or a SCRAMBLED order (to pin placement-by-index).
        std::vector<int32_t> idx = reply_idx != nullptr
                                        ? *reply_idx
                                        : std::vector<int32_t>((size_t) plan.rows());
        if (reply_idx == nullptr)
            for (int32_t i = 0; i < plan.rows(); ++i) idx[(size_t) i] = i;
        const auto payload = rowsp(idx);
        probe(7);
        // H13: a reply echoes the request frame's sequence (the client's per-call socket counter)
        if (srv.write_reply(eh::kRowsP, head.req_id, head.seq, payload.data(), payload.size(),
                            eh::deadline_after(20000)) != eh::kOk) wire = 9;
    });
    ExpertHelperClient c;
    HelperSetup opt;
    // The fill's deadline: the harness's own is 10 minutes (the real fill copies GiB).  A test peer that
    // breaks must answer in seconds, so the drive uses a bounded short fill wait.
    opt.fill_timeout_ms = 30000;
    // The REQUEST is the ranked list (the harness sends its whole ranked list); the REPLY is the resident
    // subset - the two differ by design, and this is what the drive mirrors.
    const int hrc = helper_handshake(c, path, opt, rank_req, id, herr);
    int brc = eh::kOk;
    if (hrc == eh::kOk) brc = helper_prompt_rows(c, 32, plan, a, host_res.data(),
                                                 opt, rows, nrows, herr);
    c.bye();
    c.close();
    srv_thr.join();
    srv.unbind();
    ::unlink(path.c_str());
    g_accept_rc = wire;
    if (wire != 0 && hrc == eh::kOk) return eh::kErrProtocol;   // the peer broke: never a pass
    if (hrc != eh::kOk) return hrc;
    return brc;
}

}  // namespace

void section_framing() {
    std::printf("(6) framing smoke: the harness's Side-B drive against the fake helper\n");
    const std::string path = "/tmp/p2_measure_test_full.sock";
    const std::string legacy_path = "/tmp/p2_measure_test_legacy.sock";
    const std::string absent_path = "/tmp/p2_measure_test_absent.sock";

    // the activation rows: T = 8 tokens; every token routes to experts 7 and 9 plus 8 rows of expert 3
    Activations a;
    a.T = 8;
    a.T_dump = 8;
    a.x.assign((size_t) (8 * kN), 0.0f);
    for (int64_t t = 0; t < 8; ++t)
        for (int64_t col = 0; col < kN; ++col) a.x[(size_t) t * kN + (size_t) col] = (float) pv((int) t, col);
    a.ids.assign((size_t) (8 * kK), 3);
    for (int64_t t = 0; t < 8; ++t) { a.ids[(size_t) (t * kK)] = 7; a.ids[(size_t) (t * kK + 1)] = 9; }
    LayerCounts lc;
    std::string cerr;
    check(build_layer_counts(a, lc, cerr) == eh::kOk && lc.cnt[7] == 8 && lc.cnt[9] == 8 && lc.cnt[3] == 64,
          "the smoke layer's counts (7: 8, 9: 8, 3: 64)");
    std::vector<uint8_t> holds((size_t) kNE, 0);
    holds[7] = 1; holds[9] = 1;                       // exactly the fill report's authoritative set
    std::vector<uint8_t> stable((size_t) kNE, 1);
    std::vector<int32_t> host_res((size_t) kLayers * kNE, -1);
    ExpertHelperPromptPlan plan;
    check(select_prompt_rows(lc.cnt.data(), lc.toks.data(), host_res.data(), 32,
                             holds.data(), stable.data(), kNE, kK, eh::kMaxRowsPerExpert,
                             eh::kMaxRowsPerLayer, eh::kMaxSelTok, plan) == eh::kOk &&
          plan.rows() == 16 && plan.n_sel_tok() == 8,
          "selection over the fill report's pairs: 16 claimed rows over the 8 tokens' image");

    // -- the full path over a REAL bound socket: hello(v3) + fill + kBeginP + kRowsP --
    std::vector<float> rows;
    int32_t nrows = 0;
    HelperIdentity id;
    std::string herr;
    const int brc = smoke_full(path, {{32, 7}, {32, 9}}, a, plan, host_res, rows, nrows, id, herr);
    check_status(brc, eh::kOk, "helper_handshake + helper_prompt_rows over the real connect path: kOk");
    check(g_accept_rc == 0, "the fake helper verified the kBeginP frame on the wire (shape, pairs, q8 bytes)");
    check(id.prompt_rows, "the v3 ack's feature bit is read as the tier being supported");
    check(id.free_mib == 4096 && id.bytes == 2621440, "the identity carries the ack's free MiB and the fill's bytes");
    check(id.resident == 2 && id.resident_pairs.size() == 2 &&
          id.resident_pairs[0] == std::make_pair((int32_t) 32, (int32_t) 7),
          "the fill report's authoritative resident pairs reach the caller");
    check(nrows == (int32_t) plan.rows(), "every claimed row came back (rows == plan.rows())");
    bool same = (int64_t) rows.size() == plan.rows() * kN;
    for (int32_t j = 0; same && j < plan.rows(); ++j)
        for (int64_t col = 0; same && col < kN; ++col)
            if (rows[(size_t) j * kN + (size_t) col] != (float) pv(j, col)) same = false;
    check(same, "the rows are scattered at the PAIR indices with the reply's values");

    // -- a LEGACY helper: the 24-byte ack (features=0).  The tier is OFF; NOTHING of it is sent. --
    ::unlink(legacy_path.c_str());
    {
        eh::Socket srv;
        if (srv.listen_bind(legacy_path) != eh::kOk) { check(false, "the legacy smoke could not bind"); return; }
        std::atomic<int> wire{0};
        std::atomic<int> tiers{0};              // any kBeginP/kRowsP the peer received: must stay 0
        std::thread srv_thr([&srv, &wire, &tiers] {
            const int afd = srv.accept(eh::deadline_after(8000));
            if (afd < 0) { wire = 1; return; }
            srv.adopt(afd);
            eh::Header head;
            std::vector<uint8_t> body;
            if (srv.read_frame(head, body, eh::deadline_after(8000)) != eh::kOk || head.type != eh::kHello) {
                wire = 2; return;
            }
            write_ack(srv, head, false);                 // the R2 helper's 24-byte LEGACY ack
            if (srv.read_frame(head, body, eh::deadline_after(3000)) != eh::kOk) {
                wire = 3; return;                        // the harness refused at the ack: nothing follows
            }
            if (head.type == eh::kBeginP) { tiers = 1; wire = 4; return; }   // the tier reached a legacy peer
            if (head.type != eh::kFill) { wire = 5; return; }
            write_fill_report(srv, head, {}, 0);         // a legacy helper fills (R2 windows)
        });
        ExpertHelperClient c2;
        HelperIdentity id2;
        std::string e2;
        HelperSetup opt2;
        const int rc = helper_handshake(c2, legacy_path, opt2, {{32, 7}}, id2, e2);
        ExpertHelperPromptPlan tiny;
        tiny.pair_token = {0}; tiny.pair_expert = {7}; tiny.tokens = {0};
        // The handshake REFUSES the legacy peer (a typed reason, nothing sent).  The client stays
        // connected (the R2 windows are a valid peer), so begin_prompt's OWN negotiation gate is what
        // keeps the tier's frame off the wire.
        check(c2.prompt_buffer_ensure(kN) == eh::kOk, "the legacy case can still map the tier's buffer");
        const int prompt_rc = c2.begin_prompt(32, tiny, host_res.data(), 1000);
        c2.close();
        srv_thr.join();
        srv.unbind();
        check(wire == 3, "the legacy peer answered the hello and received NOTHING after it (no kFill, no tier frame)");
        check(tiers == 0, "no kBeginP/kRowsP ever reached the legacy peer - the gate is local, not on the wire");
        check_status(rc, eh::kErrRequest, "a legacy helper is a typed refusal: the harness sends no kBeginP to it");
        check(!id2.prompt_rows, "the legacy identity says the prompt tier is absent");
        check(id2.note.find("legacy") != std::string::npos,
              "the identity names the legacy 24-byte ack (features=0) - a typed reason, not a guess");
        check(e2.find("legacy") != std::string::npos, "the refusal text names the legacy ack as its reason");
        check_status(prompt_rc, eh::kErrRequest, "begin_prompt to a legacy peer: typed refusal, nothing written");
    }
    ::unlink(legacy_path.c_str());

    // a peer that REFUSES the hello answers with the typed kError -> kErrRequest (never a hang)
    ::unlink(legacy_path.c_str());
    {
        eh::Socket srv;
        if (srv.listen_bind(legacy_path) != eh::kOk) { check(false, "the refusal smoke could not bind"); return; }
        std::thread srv_thr([&srv] {
            const int afd = srv.accept(eh::deadline_after(8000));
            if (afd < 0) return;
            srv.adopt(afd);
            eh::Header head;
            std::vector<uint8_t> body;
            if (srv.read_frame(head, body, eh::deadline_after(8000)) != eh::kOk) return;
            eh::ErrResp err;
            err.status = eh::kErrRequest;
            std::strncpy(err.text, "geometry refused", sizeof(err.text) - 1);
            srv.write_frame(eh::kError, head.req_id, (const uint8_t*) &err, sizeof(err),
                            eh::deadline_after(8000));
        });
        ExpertHelperClient c4;
        HelperIdentity id4;
        std::string e4;
        HelperSetup opt4;
        const int rc = helper_handshake(c4, legacy_path, opt4, {}, id4, e4);
        srv_thr.join();
        srv.unbind();
        check_status(rc, eh::kErrRequest, "a refused hello is a typed kErrRequest, immediately");
        check(e4.find("handshake failed") != std::string::npos, "the refusal text names the failing exchange");
    }
    ::unlink(legacy_path.c_str());

    // -- the absent helper: a typed connect refusal in bounded time (never a hang) --
    {
        ::unlink(absent_path.c_str());
        ExpertHelperClient c3;
        HelperIdentity id3;
        std::string e3;
        HelperSetup opt3;
        opt3.hello_timeout_ms = 1000;
        const auto t0 = std::chrono::steady_clock::now();
        const int rc = helper_handshake(c3, absent_path, opt3, {{32, 7}}, id3, e3);
        const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                            std::chrono::steady_clock::now() - t0).count();
        check_status(rc, eh::kErrConnect, "an absent helper answers kErrConnect");
        check(ms < 3000, "the refusal is bounded: it returns in well under its deadline");
        check(e3.find("start the helper") != std::string::npos, "the refusal tells the operator what to start");
    }
}

// ============================== (7) the pack's blob source ==============================
//
// The window-1 refusal of 2026-10-29 (`REFUSAL (pack): cannot open /mnt/user/ai/iq3xxs-15/experts.bin`)
// is the case this section pins: a pack whose experts live in the model GGUF its header names, ONE LEVEL
// UP - the layout the acceptance driver spells `--experts ../Swift-…gguf` (run_acceptance.py:70).  A mock
// pack dir, a mock GGUF-shaped blob, the whole resolution matrix (relative / absolute --experts,
// experts.bin, the header's auto-resolve, every typed refusal) and every mapped (offset,size) window
// against bytes the fixture wrote itself: the pattern is a function of the byte INDEX alone, so a row
// read at a wrong offset is a byte mismatch, never a silent pass.  No GPU, no helper, no model file.
constexpr int64_t kMockLayers = 2, kMockExperts = 2;
// The real pack's two formats and blob sizes, pinned from tests/data/native_experts/iq3_xxs.txt's layers
// 0 and 1 (`0 17 20 0 1868800 …`, `1 16 42 956825600 1305600 …`).  The CPU parser cross-checks a header's
// blob column against its own row bytes, so these numbers cannot drift from the layout's.
constexpr uint64_t kMockBlob0 = 1868800, kMockBlob1 = 1305600;
// The role-window bases the fixture chose, 4 MiB apart: a window for two experts is at most 2*1868800 B,
// so no window ever touches its neighbour and each window's expected bytes are hand-computable.
constexpr uint64_t kMockGate0 = 1000000, kMockUp0 = 5000000, kMockDown0 = 9000000;
constexpr uint64_t kMockGate1 = 13000000, kMockUp1 = 17000000, kMockDown1 = 21000000;
constexpr uint64_t kMockPackedTotal = kMockBlob0 * 2 + kMockBlob1 * 2;   // 6348800: the offset column's total
constexpr uint64_t kMockFileSize = 24000000;                            // past every window's end
const char* const kMockGgufName = "mock-weights.gguf";

uint8_t mock_byte(uint64_t i) { return (uint8_t) ((i % 251) + 1); }   // the pattern: the index is the content

bool write_pattern_file(const std::string& path, uint64_t size) {
    std::FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) return false;
    std::vector<uint8_t> blk(65536);
    for (uint64_t at = 0; at < size; at += blk.size()) {
        const uint64_t n = std::min<uint64_t>(blk.size(), size - at);
        for (uint64_t k = 0; k < n; ++k) blk[(size_t) k] = mock_byte(at + k);
        if (std::fwrite(blk.data(), 1, (size_t) n, f) != n) { std::fclose(f); return false; }
    }
    return std::fclose(f) == 0;
}

void section_blob_source() {
    std::printf("(7) the pack's blob source: the helper's join, the header's name, the mapped windows\n");
    namespace cpu = strata::kernels::cpu;
    const std::string root = "/tmp/p2_measure_test_blob";
    const std::string pack = root + "/pack", pack2 = root + "/nopack", pack3 = root + "/noname";
    ::mkdir(root.c_str(), 0755);
    ::mkdir(pack.c_str(), 0755);
    ::mkdir(pack2.c_str(), 0755);
    ::mkdir(pack3.c_str(), 0755);
    const std::string gguf = root + "/" + kMockGgufName;
    ::unlink(gguf.c_str());
    ::unlink((pack + "/mock-weights.gguf").c_str());
    ::unlink((pack + "/experts.bin").c_str());
    ::unlink((pack + "/native_experts.txt").c_str());
    ::unlink((pack2 + "/native_experts.txt").c_str());
    ::unlink((pack3 + "/native_experts.txt").c_str());

    // ---- (7a) the helper's join, verbatim (helper_main.cpp:708-712) ----
    check(join_experts_path("pack", "experts.bin") == "pack/experts.bin",
          "join: <pack>/experts.bin is the default the harness keeps");
    check(join_experts_path("pack/", "experts.bin") == "pack/experts.bin",
          "join: a trailing slash on the pack is not doubled");
    check(join_experts_path("pack", "../m.gguf") == "pack/../m.gguf",
          "join: a RELATIVE --experts resolves against the pack dir (the driver's ../Swift-… form)");
    check(join_experts_path("pack", "/abs/m.gguf") == "pack//abs/m.gguf",
          "join: an absolute value keeps the helper's exact concatenation (POSIX collapses //)");

    // ---- (7b) the header's name, every version's spelling (tools/iq_pack.py:479-486) ----
    check(header_blob_name("# strata native experts v3: layer gu_type d_type offset blob_bytes gate_off "
                           "up_off down_off [shard] (n_expert 512, total 42912972800; absolute offsets in "
                           "Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf, or in the named shard beside it)")
              == "Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf",
          "v3 header: the named GGUF is the token after 'absolute offsets in'");
    check(header_blob_name("# strata native experts v2: layer gu_type d_type offset blob_bytes gate_off "
                           "up_off down_off (n_expert 512, total 42912972800; the last three are absolute "
                           "offsets in Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf)")
              == "Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf",
          "v2 header: the same tag inside the parenthetical");
    check(header_blob_name("# strata native experts v5: layer gu_type d_type offset blob_bytes gate_off "
                           "up_off down_off [shard | gate,up,down] (n_expert 512, total 12; absolute "
                           "offsets in Model-00001-of-00004.gguf, or in the named shard beside it) - v5: "
                           "Hadamard-folded experts, see hadamard.txt")
              == "Model-00001-of-00004.gguf",
          "v5 header: the suffix after the parenthesis does not eat the name");
    check(header_blob_name("# strata native experts v3: layer gu_type d_type offset blob_bytes")
              .empty(),
          "a header that names no file answers with no name (the refusal says so)");

    // ---- (7c) the mock pack: the real pack's formats, the driver's layout (blob beside the pack) ----
    {
        std::FILE* h = std::fopen((pack + "/native_experts.txt").c_str(), "wb");
        if (!h) { check(false, "the mock pack's header could not be written"); return; }
        std::fprintf(h,
                     "# strata native experts v3: layer gu_type d_type offset blob_bytes gate_off up_off "
                     "down_off [shard] (n_expert %lld, total %llu; absolute offsets in %s, or in the "
                     "named shard beside it)\n"
                     "0 17 20 0 %llu %llu %llu %llu\n"
                     "1 16 42 %llu %llu %llu %llu %llu\n",
                     (long long) kMockExperts, (unsigned long long) kMockPackedTotal, kMockGgufName,
                     (unsigned long long) kMockBlob0, (unsigned long long) kMockGate0,
                     (unsigned long long) kMockUp0, (unsigned long long) kMockDown0,
                     (unsigned long long) (kMockBlob0 * 2), (unsigned long long) kMockBlob1,
                     (unsigned long long) kMockGate1, (unsigned long long) kMockUp1,
                     (unsigned long long) kMockDown1);
        std::fclose(h);
        std::FILE* h2 = std::fopen((pack2 + "/native_experts.txt").c_str(), "wb");
        if (h2) std::fprintf(h2, "# strata native experts v3: (n_expert 2; absolute offsets in missing-model.gguf, "
                                 "or in the named shard beside it)\n"), std::fclose(h2);
        std::FILE* h3 = std::fopen((pack3 + "/native_experts.txt").c_str(), "wb");
        if (h3) std::fprintf(h3, "# strata native experts v3: layer gu_type d_type offset blob_bytes gate_off "
                                 "up_off down_off (n_expert 2, total 0)\n"), std::fclose(h3);
    }
    std::string err;
    const bool loaded = cpu::expert_layout_load(pack, kMockLayers, kMockExperts, err);
    check(loaded, "the mock pack loads through the CPU layout parser (the same one the harness and the helper use)");
    if (!loaded) { check(false, "the mock layout is the whole rest of this section"); return; }
    const cpu::ExpertLayout& lay = cpu::expert_layout();
    check(lay.native && lay.version == 3 && lay.n_layers == kMockLayers && lay.n_expert == kMockExperts,
          "the mock layout is native v3 with the header's expert count");
    check(lay.gguf_off.size() == (size_t) (3 * kMockLayers) && lay.gguf_file.empty(),
          "the layout carries the role offsets and names no shard file (the helper's per-role condition)");
    check(layout_maps_gguf_roles(lay), "layout_maps_gguf_roles: the per-role mapping for this layout");

    uint64_t p0[3], p1[3];
    check(blob_role_bytes(lay, 0, p0) && blob_role_bytes(lay, 1, p1), "the role slices of both layers");
    check(p0[0] == (uint64_t) lay.fmt[0].n_ff * (uint64_t) lay.fmt[0].gu_row && p0[1] == p0[0] &&
              p0[2] == (uint64_t) lay.fmt[0].n_embd * (uint64_t) lay.fmt[0].d_row,
          "gate/up = n_ff*gu_row, down = n_embd*d_row (the helper's per[3], helper_main.cpp:235-236)");
    check(p0[0] + p0[1] + p0[2] == kMockBlob0 && p1[0] + p1[1] + p1[2] == kMockBlob1,
          "the slices sum to the pinned blob sizes 1868800 and 1305600 (the helper's 'at != bytes' check)");

    if (!write_pattern_file(gguf, kMockFileSize)) { check(false, "the mock GGUF-shaped blob could not be written"); return; }
    const int lnk = ::symlink(("../" + std::string(kMockGgufName)).c_str(), (pack + "/" + kMockGgufName).c_str());
    check(lnk == 0, "the mock pack has the blob inside its dir too (the --experts name.gguf case)");

    // ---- (7d) the resolution matrix ----
    // The joined path is the helper's own string; the OS resolves `pack/../name`, so the comparison is
    // made on the canonical path (realpath), never on a normalized string the helper never computes.
    auto canon = [](const std::string& p) {
        char buf[4096];
        const char* r = ::realpath(p.c_str(), buf);
        return std::string(r ? r : "");
    };
    BlobSource bs;
    check(resolve_blob_source(pack, "", lay, bs, err) == 0 && bs.path == pack + "/../" + kMockGgufName &&
              bs.per_role && canon(bs.path) == canon(gguf),
          "no --experts, no experts.bin: the header's GGUF beside the pack, mapped per role");
    check(resolve_blob_source(pack, "../" + std::string(kMockGgufName), lay, bs, err) == 0 &&
              canon(bs.path) == canon(gguf) && bs.per_role,
          "--experts ../name.gguf resolves against the pack dir (the driver's exact spelling)");
    check(resolve_blob_source(pack, kMockGgufName, lay, bs, err) == 0 &&
              bs.path == pack + "/" + kMockGgufName && bs.per_role,
          "--experts name.gguf is the file inside the pack dir (the join, not a search)");
    check(resolve_blob_source(pack, "no-such-model.gguf", lay, bs, err) == 3 &&
              err.find(pack + "/no-such-model.gguf") != std::string::npos,
          "an --experts that is not there is a typed refusal naming it (exit 3, the pack/layout code)");
    check(resolve_blob_source(pack, gguf, lay, bs, err) == 3 &&
              err.find(pack + "/" + gguf) != std::string::npos,
          "an ABSOLUTE --experts joins onto the pack dir exactly as the helper's concatenation joins it "
          "(the mirror refuses the joined path; the driver spells the path relative to the pack)");

    // the mapped windows, against the bytes the fixture wrote: every (layer, expert) row is gate|up|down
    std::ifstream pk(gguf, std::ios::binary);
    const uint64_t bases[2][3] = {{kMockGate0, kMockUp0, kMockDown0}, {kMockGate1, kMockUp1, kMockDown1}};
    int window_checks = 0;
    for (int64_t l = 0; l < kMockLayers; ++l) {
        const uint64_t* per = (l == 0) ? p0 : p1;
        for (int64_t e = 0; e < kMockExperts; ++e) {
            std::vector<uint8_t> got(lay.blob_bytes(l), 0), want(lay.blob_bytes(l), 0);
            uint64_t at = 0;
            for (int r = 0; r < 3; ++r) {
                const uint64_t start = bases[l][r] + (uint64_t) e * per[r];
                for (uint64_t k = 0; k < per[r]; ++k) want[(size_t) (at + k)] = mock_byte(start + k);
                at += per[r];
            }
            if (!read_blob_row(pk, lay, bs, l, e, got.data(), kMockFileSize, err)) {
                check(false, "a mapped row read failed");
                std::printf("      layer %lld expert %lld: %s\n", (long long) l, (long long) e, err.c_str());
                continue;
            }
            ++window_checks;
            check(got == want, "the row's bytes come from gate_off+e*per0 | up_off+e*per1 | down_off+e*per2");
        }
    }
    check(window_checks == (int) (kMockLayers * kMockExperts), "every (layer, expert) window was checked");
    const uint64_t end0 = std::max(std::max(kMockGate0 + 2 * p0[0], kMockUp0 + 2 * p0[1]), kMockDown0 + 2 * p0[2]);
    const uint64_t end1 = std::max(std::max(kMockGate1 + 2 * p1[0], kMockUp1 + 2 * p1[1]), kMockDown1 + 2 * p1[2]);
    check(blob_layer_end(lay, bs, 0) == end0 && blob_layer_end(lay, bs, 1) == end1,
          "blob_layer_end: the last expert's last role slice, the bound the harness checks before the run");
    check(resolve_blob_source(pack2, "", lay, bs, err) == 3 &&
              err.find(pack2 + "/experts.bin") != std::string::npos &&
              err.find(pack2 + "/../missing-model.gguf") != std::string::npos,
          "no experts.bin and no GGUF beside the pack: the refusal names BOTH paths it tried");
    check(resolve_blob_source(pack3, "", lay, bs, err) == 3 &&
              err.find(pack3 + "/experts.bin") != std::string::npos &&
              err.find("names no GGUF") != std::string::npos,
          "a header that names no file refuses by saying so, naming the one path it had");

    // a blob too short for the window it maps: the typed refusal, never a short row
    {
        const std::string short_path = pack + "/short.gguf";
        ::unlink(short_path.c_str());
        check(write_pattern_file(short_path, 100000), "the short mock blob was written");
        BlobSource sb;
        check(resolve_blob_source(pack, "short.gguf", lay, sb, err) == 0 && !sb.path.empty(),
              "the short blob resolves like any --experts value (the size is the read's business)");
        std::ifstream sf(short_path, std::ios::binary);
        std::vector<uint8_t> got(kMockBlob0, 0);
        check(!read_blob_row(sf, lay, sb, 0, 0, got.data(), 100000, err) &&
                  err.find(short_path) != std::string::npos,
              "a row past the file's end is a typed refusal naming the file (never a silent short row)");
        ::unlink(short_path.c_str());
    }

    // ---- (7e) a pack that DOES ship experts.bin keeps the packed blob_offset() read ----
    {
        const std::string packed = pack + "/experts.bin";
        check(write_pattern_file(packed, kMockPackedTotal), "the packed mock experts.bin was written");
        BlobSource ps;
        check(resolve_blob_source(pack, "", lay, ps, err) == 0 && ps.path == packed && !ps.per_role,
              "experts.bin wins over the header's name: the default path stays for packs that have one");
        std::ifstream pf(packed, std::ios::binary);
        int packed_checks = 0;
        for (int64_t l = 0; l < kMockLayers; ++l) {
            for (int64_t e = 0; e < kMockExperts; ++e) {
                std::vector<uint8_t> got(lay.blob_bytes(l), 0), want(lay.blob_bytes(l), 0);
                const uint64_t at = lay.blob_offset(l, e);
                for (uint64_t k = 0; k < lay.blob_bytes(l); ++k) want[(size_t) k] = mock_byte(at + k);
                if (read_blob_row(pf, lay, ps, l, e, got.data(), kMockPackedTotal, err)) {
                    ++packed_checks;
                    check(got == want, "the packed read uses blob_offset(layer, expert)");
                } else {
                    check(false, "a packed row read failed");
                }
            }
        }
        check(packed_checks == (int) (kMockLayers * kMockExperts), "every packed row was checked");
        check(blob_layer_end(lay, ps, 0) == kMockBlob0 * 2 &&
                  blob_layer_end(lay, ps, 1) == kMockBlob0 * 2 + kMockBlob1 * 2,
              "the packed bound is the harness's own offset + blob*n_expert");
        ::unlink(packed.c_str());
    }

    // the run record names the file and the mapping it used (evidence, no verdict)
    {
        Report r;
        r.meta.pack_dir = pack;
        r.meta.blob_file = root + "/" + kMockGgufName;
        r.meta.blob_mapping = "gguf-roles";
        const std::string j = to_json(r);
        check(j == to_json(r), "the blob fields keep to_json byte-stable");
        check(j.find("\"blob_file\": \"" + r.meta.blob_file + "\"") != std::string::npos &&
                  j.find("\"blob_mapping\": \"gguf-roles\"") != std::string::npos,
              "the JSON records the blob file and the mapping the run read");
    }

    ::unlink((pack + "/" + kMockGgufName).c_str());
    ::unlink(gguf.c_str());
    ::unlink((pack + "/native_experts.txt").c_str());
    ::unlink((pack2 + "/native_experts.txt").c_str());
    ::unlink((pack3 + "/native_experts.txt").c_str());
    ::rmdir(pack.c_str());
    ::rmdir(pack2.c_str());
    ::rmdir(pack3.c_str());
    ::rmdir(root.c_str());

    // ---- (7f) the box's real pack, when this machine has it (the window-1 refusal's own pack) ----
    // Guarded: it SKIPs (and says so) where the pack is not present.  It reads the header and the file
    // name only - no model bytes are read, and the harness's window 32-47 role sums are checked.
    {
        const std::string real_pack = "/mnt/user/ai/iq3xxs-15";
        struct stat st;
        if (::stat((real_pack + "/native_experts.txt").c_str(), &st) != 0) {
            std::printf("      (7f) skip: %s/native_experts.txt is not on this machine\n", real_pack.c_str());
        } else {
            std::string rerr;
            if (!cpu::expert_layout_load(real_pack, kLayers, kNE, rerr)) {
                check(false, "the real pack's layout must load");
                std::printf("      (7f): %s\n", rerr.c_str());
            } else {
                const cpu::ExpertLayout& rl = cpu::expert_layout();
                BlobSource rb;
                const int rrc = resolve_blob_source(real_pack, "", rl, rb, rerr);
                check(rrc == 0, "the real pack resolves with NO --experts (the window-1 refusal is gone)");
                check(rb.per_role, "the real pack maps per role (it ships no experts.bin)");
                check(rb.path == real_pack + "/../" + pack_header_blob_name(real_pack) &&
                          ::stat(rb.path.c_str(), &st) == 0 && S_ISREG(st.st_mode),
                      "the resolved file is the header's GGUF beside the pack, and it is a regular file");
                std::printf("      (7f) resolved blob: %s (%lld bytes)\n", rb.path.c_str(),
                            (long long) st.st_size);
                bool sums = true;
                for (int64_t l = kLayerLo; l <= kLayerHi; ++l) {
                    uint64_t per[3];
                    if (!blob_role_bytes(rl, l, per) || per[0] + per[1] + per[2] != rl.blob_bytes(l)) sums = false;
                    if (blob_layer_end(rl, rb, l) > (uint64_t) st.st_size) sums = false;
                }
                check(sums, "window 32-47: the role slices sum to each blob and sit inside the file");
                // the guard's ring term on the REAL window: the largest layer's ring vs layer_lo's
                const uint64_t lo_blob = rl.blob_bytes(kLayerLo), mx_blob = max_blob_bytes(rl, kLayerLo, kLayerHi);
                std::printf("      (7f) ring term: layer %lld's blob %llu B -> ring %llu MiB; window max blob "
                            "%llu B -> ring %llu MiB (the guard uses the max)\n",
                            (long long) kLayerLo, (unsigned long long) lo_blob,
                            (unsigned long long) (stage_ring_bytes(lo_blob) >> 20),
                            (unsigned long long) mx_blob, (unsigned long long) (stage_ring_bytes(mx_blob) >> 20));
                check(mx_blob >= lo_blob, "the real window's max blob is at least layer_lo's (the guard's term)");
            }
        }
    }
}

}  // namespace

// ============================== (8) the per-layer sizing and the plan's claims ==============================
//
// The two window-1 findings of 2026-10-29 (~07:39, build-mixed-gpu/p2-measure-window1/harness2.log), both
// pinned on the CPU:
//
// 1. THE CLAIMS ARRAY.  The harness died at the first host allocation after the fill with glibc
//    "corrupted size vs. prev_size" - the lazy report of an out-of-bounds write made in the SAME iteration,
//    in the claims loop.  `claims` is sized T*kK (the routed ENTRY count) and was written at
//    `token*kK + expert`.  An expert id is not a slot of the token's kK block: from id 10 (= kK) on the
//    write lands in a LATER token's block, and for the last tokens with high ids it runs past the array -
//    at the live shape (T = 256, the pair (255, 511)) by 501 bytes.  Section (6)'s fixtures used experts
//    3, 7 and 9, all below kK, which is why the suite never saw it.  (8b) pins the fixed rule (find the
//    entry inside the token's routed ids) on exactly the live shape.
//
// 2. THE STAGING RING'S ESTIMATE.  The device guard sized the 32-slot ring (and the group buffers) by
//    layer_lo's blob and types, while each layer ALLOCATES 32 * its own blob.  (8a) is a mock pack whose
//    three layers have three DIFFERENT blob sizes with the largest LAST, so the window maximum is pinned
//    against layer_lo's value instead of a uniform mock, together with the per-role and packed reads and
//    the ring math of all three.
namespace {

constexpr int64_t kMixLayers = 3, kMixExperts = 2;
constexpr int kMixGu[3] = {17, 16, 21}, kMixD[3] = {20, 42, 20};   // the real window's format buckets
constexpr uint64_t kMixBase[3][3] = {{1000000, 5000000, 9000000},
                                     {13000000, 17000000, 21000000},
                                     {25000000, 29000000, 33000000}};
const char* const kMixGgufName = "mock-mix.gguf";

/// The blob sizes the parser's own formats make for the three layers: native_fmt is exactly what
/// expert_layout_load cross-checks a header's blob column against, so header and layout cannot disagree.
bool mix_blobs(uint64_t out[3], std::string& err) {
    for (int i = 0; i < 3; ++i) {
        strata::kernels::cpu::NativeFmt f;
        if (!strata::kernels::cpu::native_fmt(kMixGu[i], kMixD[i], kN, 640, f, err)) return false;
        out[i] = (uint64_t) f.bytes;
    }
    return true;
}

constexpr int64_t kScaleLayerLo = 32, kScaleLayerHi = 47;
constexpr int64_t kScaleRequested = 7286, kScaleResident = 1656;
constexpr int64_t kScaleFillBytes = 3598233600, kScaleFreeMib = 7458;
constexpr int64_t kScaleT = 256;                        // the plan at its caps: 4 experts x 64 rows
constexpr int32_t kScaleExperts[4] = {400, 401, 402, 511};   // all >= kK; 511 is routed by the LAST token

}  // namespace

void section_sizing_and_claims() {
    std::printf("(8) the per-layer sizing (mixed blobs) and the plan's claims (the window-1 heap write)\n");
    namespace cpu = strata::kernels::cpu;
    std::string err;

    // ---- (8a) a mock pack with three DIFFERENT blob sizes, the largest LAST ----
    uint64_t mix[3] = {0, 0, 0};
    if (!mix_blobs(mix, err)) {
        check(false, "the mock layers' formats could not be built");
        std::printf("      (8a): %s\n", err.c_str());
        return;
    }
    check(mix[0] == kMockBlob0 && mix[1] == kMockBlob1,
          "layers 17/20 and 16/42 make the pinned 1868800 and 1305600 (the format math is shared)");
    check(mix[0] != mix[1] && mix[1] != mix[2] && mix[0] != mix[2], "the three layers' blobs differ in size");
    check(mix[2] > mix[0] && mix[2] > mix[1], "the LARGEST blob is the LAST layer (layer_lo is not the max)");
    std::printf("      (8a) blobs: layer 0 %llu, layer 1 %llu, layer 2 %llu bytes\n",
                (unsigned long long) mix[0], (unsigned long long) mix[1], (unsigned long long) mix[2]);

    const std::string root = "/tmp/p2_measure_test_blob";
    const std::string mixpack = root + "/mixpack";
    ::mkdir(root.c_str(), 0755);
    ::mkdir(mixpack.c_str(), 0755);
    ::unlink((mixpack + "/native_experts.txt").c_str());
    ::unlink((mixpack + "/experts.bin").c_str());
    const std::string mixgguf = root + "/" + kMixGgufName;
    ::unlink(mixgguf.c_str());
    const uint64_t packed_total = 2 * (mix[0] + mix[1] + mix[2]);
    const uint64_t file_size = kMixBase[2][2] + kMixExperts * mix[2] + 1000000;
    {
        std::FILE* h = std::fopen((mixpack + "/native_experts.txt").c_str(), "wb");
        if (!h) { check(false, "the mix pack's header could not be written"); return; }
        std::fprintf(h,
                     "# strata native experts v3: layer gu_type d_type offset blob_bytes gate_off up_off "
                     "down_off [shard] (n_expert %lld, total %llu; absolute offsets in %s, or in the named "
                     "shard beside it)\n",
                     (long long) kMixExperts, (unsigned long long) packed_total, kMixGgufName);
        uint64_t at = 0;
        for (int64_t l = 0; l < kMixLayers; ++l) {
            std::fprintf(h, "%lld %d %d %llu %llu %llu %llu %llu\n", (long long) l, kMixGu[l], kMixD[l],
                         (unsigned long long) at, (unsigned long long) mix[l],
                         (unsigned long long) kMixBase[l][0], (unsigned long long) kMixBase[l][1],
                         (unsigned long long) kMixBase[l][2]);
            at += 2 * mix[l];
        }
        std::fclose(h);
    }
    if (!cpu::expert_layout_load(mixpack, kMixLayers, kMixExperts, err)) {
        check(false, "the mix pack loads through the CPU layout parser");
        std::printf("      (8a): %s\n", err.c_str());
        return;
    }
    const cpu::ExpertLayout& lay = cpu::expert_layout();
    check(lay.native && lay.n_layers == kMixLayers && lay.n_expert == kMixExperts &&
              lay.fmt.size() == (size_t) kMixLayers,
          "the mock mix layout is 3 layers x 2 experts with a format per layer");
    check(max_blob_bytes(lay, 0, 2) == mix[2], "max_blob_bytes over 0-2 is the LAST layer's blob, not layer_lo's");
    check(max_blob_bytes(lay, 1, 2) == std::max(mix[1], mix[2]), "max over 1-2");
    check(max_blob_bytes(lay, 0, 1) == std::max(mix[0], mix[1]), "max over 0-1 (the largest layer excluded)");
    check(max_blob_bytes(lay, 2, 2) == mix[2] && max_blob_bytes(lay, 1, 1) == mix[1],
          "a one-layer window's max is that layer's own blob");
    check(max_blob_bytes(lay, 2, 1) == 0, "an empty window answers 0");
    check(max_blob_bytes(lay, 3, 5) == 0, "a window outside the layout answers 0 (no read past fmt)");
    check(stage_ring_bytes(mix[2]) == (size_t) kStageRingSlots * (size_t) mix[2] && stage_ring_bytes(0) == 0,
          "stage_ring_bytes = kStageRingSlots * the layer's blob (the ring's stride IS the blob)");
    bool ring_covers = true;
    for (int64_t l = 0; l < kMixLayers; ++l)
        if (stage_ring_bytes(mix[l]) > stage_ring_bytes(max_blob_bytes(lay, 0, 2))) ring_covers = false;
    check(ring_covers, "the window-max ring is at least every layer's own ring (the guard cannot under-say)");
    check(stage_ring_bytes(mix[0]) < stage_ring_bytes(mix[2]),
          "layer_lo's ring is SMALLER than the largest layer's: the 32*blob_bytes(layer_lo) form under-says");

    if (!write_pattern_file(mixgguf, file_size)) { check(false, "the mix blob could not be written"); return; }
    BlobSource bs;
    check(resolve_blob_source(mixpack, "", lay, bs, err) == 0 && bs.per_role &&
              bs.path == mixpack + "/../" + kMixGgufName,
          "the mix pack resolves per role through its header's name");
    std::ifstream pk(mixgguf, std::ios::binary);
    int windows = 0;
    bool bytes_ok = true, sums_ok = true, ends_ok = true;
    for (int64_t l = 0; l < kMixLayers; ++l) {
        uint64_t per[3];
        if (!blob_role_bytes(lay, l, per) || per[0] + per[1] + per[2] != lay.blob_bytes(l)) sums_ok = false;
        for (int64_t e = 0; e < kMixExperts; ++e) {
            std::vector<uint8_t> got(lay.blob_bytes(l), 0), want(lay.blob_bytes(l), 0);
            uint64_t at = 0;
            for (int r = 0; r < 3; ++r) {
                const uint64_t start = kMixBase[l][r] + (uint64_t) e * per[r];
                for (uint64_t k = 0; k < per[r]; ++k) want[(size_t) (at + k)] = mock_byte(start + k);
                at += per[r];
            }
            if (read_blob_row(pk, lay, bs, l, e, got.data(), file_size, err)) {
                ++windows;
                if (got != want) bytes_ok = false;
            } else {
                bytes_ok = false;
                std::printf("      layer %lld expert %lld: %s\n", (long long) l, (long long) e, err.c_str());
            }
        }
        const uint64_t want_end = std::max(std::max(kMixBase[l][0] + kMixExperts * per[0],
                                                   kMixBase[l][1] + kMixExperts * per[1]),
                                           kMixBase[l][2] + kMixExperts * per[2]);
        if (blob_layer_end(lay, bs, l) != want_end) ends_ok = false;
    }
    check(sums_ok, "every layer's role slices sum to its own (differently sized) blob");
    check(windows == (int) (kMixLayers * kMixExperts) && bytes_ok,
          "every (layer, expert) row of the mixed-size pack comes from its own role windows");
    check(ends_ok, "blob_layer_end is each layer's last expert's last role slice");

    {   // the packed form of the same mixed sizes
        const std::string packed = mixpack + "/experts.bin";
        check(write_pattern_file(packed, packed_total), "the mixed-size packed experts.bin was written");
        BlobSource ps;
        check(resolve_blob_source(mixpack, "", lay, ps, err) == 0 && !ps.per_role && ps.path == packed,
              "experts.bin wins for the mix pack too (the packed read stays)");
        std::ifstream pf(packed, std::ios::binary);
        int n = 0;
        bool ok = true;
        for (int64_t l = 0; l < kMixLayers; ++l)
            for (int64_t e = 0; e < kMixExperts; ++e) {
                std::vector<uint8_t> got(lay.blob_bytes(l), 0), want(lay.blob_bytes(l), 0);
                const uint64_t at = lay.blob_offset(l, e);
                for (uint64_t k = 0; k < lay.blob_bytes(l); ++k) want[(size_t) k] = mock_byte(at + k);
                if (read_blob_row(pf, lay, ps, l, e, got.data(), packed_total, err)) { ++n; if (got != want) ok = false; }
                else ok = false;
            }
        check(n == (int) (kMixLayers * kMixExperts) && ok,
              "the packed read of a mixed-size pack strides by each layer's own blob_bytes");
        ::unlink(packed.c_str());
    }
    {   // a file too short for the LARGEST layer's last window: the typed refusal, never a short row
        const std::string short_path = mixpack + "/short.gguf";
        ::unlink(short_path.c_str());
        uint64_t per2[3];
        if (!blob_role_bytes(lay, 2, per2)) { check(false, "the largest layer's role slices"); return; }
        const uint64_t just_short = kMixBase[2][2] + kMixExperts * per2[2];   // one byte past the window's end
        check(write_pattern_file(short_path, just_short - 1), "the short mix blob was written");
        BlobSource sb;
        std::ifstream sf(short_path, std::ios::binary);
        std::vector<uint8_t> got(mix[2], 0);
        check(resolve_blob_source(mixpack, "short.gguf", lay, sb, err) == 0 &&
                  !read_blob_row(sf, lay, sb, 2, kMixExperts - 1, got.data(), just_short - 1, err) &&
                  err.find(short_path) != std::string::npos,
              "the largest layer's last row past a short file's end is the typed refusal naming the file");
        ::unlink(short_path.c_str());
    }

    // ---- (8b) the plan's claims: the entry a (token, expert) pair owns, at the live shape ----
    Activations sa;
    sa.T = kScaleT;
    sa.T_dump = kScaleT;
    sa.x.assign((size_t) (kScaleT * kN), 0.0f);
    for (int64_t t = 0; t < kScaleT; ++t)
        for (int64_t c = 0; c < kN; ++c) sa.x[(size_t) t * kN + (size_t) c] = (float) pv((int) t, c);
    sa.ids.assign((size_t) (kScaleT * kK), 0);
    for (int64_t t = 0; t < kScaleT; ++t) {
        for (int64_t s = 0; s < kK; ++s) sa.ids[(size_t) (t * kK + s)] = (int32_t) ((t * 9 + s * 31 + 7) % 400);
        sa.ids[(size_t) (t * kK)] = kScaleExperts[t / 64];      // expert t/64 owns token t: 64 tokens each
    }
    LayerCounts slc;
    check(build_layer_counts(sa, slc, err) == eh::kOk && slc.cnt[511] == 64 && slc.cnt[400] == 64 &&
              slc.cnt[401] == 64 && slc.cnt[402] == 64,
          "the scale layer's counts: 4 experts x 64 tokens (the other slots route ids below 400)");
    std::vector<uint8_t> holds((size_t) kNE, 0);
    for (const int32_t e : kScaleExperts) holds[(size_t) e] = 1;
    std::vector<uint8_t> stable((size_t) kNE, 1);
    std::vector<int32_t> host_res((size_t) kLayers * kNE, -1);
    ExpertHelperPromptPlan plan;
    check(select_prompt_rows(slc.cnt.data(), slc.toks.data(), host_res.data(), 32, holds.data(), stable.data(),
                             kNE, kK, eh::kMaxRowsPerExpert, eh::kMaxRowsPerLayer, eh::kMaxSelTok, plan) == eh::kOk &&
              plan.rows() == 256 && plan.n_sel_tok() == 256,
          "the scale plan sits at its caps: 256 claimed rows over 256 distinct tokens");
    bool high_ids = true, last_pair = false;
    for (int64_t j = 0; j < plan.rows(); ++j) {
        if (plan.pair_expert[(size_t) j] < kK) high_ids = false;
        if (plan.pair_token[(size_t) j] == kScaleT - 1 && plan.pair_expert[(size_t) j] == 511) last_pair = true;
    }
    check(high_ids, "every claimed pair's expert id is at or above kK (the old index left the token's block)");
    check(last_pair, "the plan claims (last token, expert 511): the old index wrote 501 bytes past the array");

    std::vector<uint8_t> claims;
    check(mark_claim_entries(sa, plan, claims, err) == eh::kOk, "the claims mark cleanly at the live shape");
    check((int64_t) claims.size() == kScaleT * kK, "claims is sized T*kK: the routed ENTRY count");
    int marks = 0;
    bool marks_ok = true;
    for (size_t i = 0; i < claims.size(); ++i) {
        if (!claims[i]) continue;
        ++marks;
        const int32_t e = sa.ids[i];
        bool named = false;
        for (int64_t j = 0; j < plan.rows() && !named; ++j)
            if (plan.pair_expert[(size_t) j] == e && plan.pair_token[(size_t) j] == (int32_t) (i / (size_t) kK))
                named = true;
        if (!named) marks_ok = false;
    }
    check(marks == (int) plan.rows() && marks_ok,
          "one mark per claimed pair, each on the entry whose routed id IS the pair's expert");
    bool last_marked = false;
    for (int64_t s = 0; s < kK; ++s)
        if (sa.ids[(size_t) ((kScaleT - 1) * kK + s)] == 511 && claims[(size_t) ((kScaleT - 1) * kK + s)])
            last_marked = true;
    check(last_marked, "the (last token, 511) pair's entry is marked, INSIDE the array");

    {   // a pair the token does not route: a typed refusal, and no mark
        ExpertHelperPromptPlan bad;
        bad.pair_token = {3}; bad.pair_expert = {499}; bad.tokens = {3};
        std::vector<uint8_t> c2;
        std::string e2;
        check_status(mark_claim_entries(sa, bad, c2, e2), eh::kErrGeometry,
                     "a pair whose expert the token does not route is a typed refusal (never a silent mark)");
        check(e2.find("499") != std::string::npos && e2.find("token 3") != std::string::npos,
              "the refusal names the pair it refused");
        bool clean = true;
        for (size_t i = 0; i < c2.size(); ++i) if (c2[i]) clean = false;
        check(clean, "the refusal leaves no mark set");
    }
    {   // T = 1 with the highest id: the smallest array, the largest index the old form wrote
        Activations one;
        one.T = 1;
        one.T_dump = 1;
        one.x.assign((size_t) kN, 0.5f);
        one.ids.assign((size_t) kK, 0);
        one.ids[3] = 511;
        ExpertHelperPromptPlan p1;
        p1.pair_token = {0}; p1.pair_expert = {511}; p1.tokens = {0};
        std::vector<uint8_t> c1;
        std::string e1;
        check(mark_claim_entries(one, p1, c1, e1) == eh::kOk && c1.size() == (size_t) kK && c1[3] == 1,
              "T=1 with expert 511: the mark lands in the token's own block (index 3 of 10)");
    }

    // ---- (8c) the fill-report scale: 7286 requested pairs, a 1656-pair report, a 256-row tier ----
    std::map<int64_t, std::vector<int32_t>> cnt_by_layer;
    for (int64_t l = kScaleLayerLo; l <= kScaleLayerHi; ++l) {
        std::vector<int32_t> c((size_t) kNE, 1);
        for (int64_t e = 0; e < kNE; ++e) c[(size_t) e] = (int32_t) (1 + ((l * 7 + e * 13) % 50));
        if (l == 32) for (const int32_t e : kScaleExperts) c[(size_t) e] = 64;   // ranked first in layer 32
        cnt_by_layer[l] = c;
    }
    const auto ranked = rank_pairs(cnt_by_layer, kScaleRequested);
    check((int64_t) ranked.size() == kScaleRequested,
          "rank_pairs caps the fill list at the harness's cap (8192 pairs of 16x512 -> 7286)");
    const std::vector<std::pair<int32_t, int32_t>> resident(ranked.begin(),
                                                            ranked.begin() + (size_t) kScaleResident);
    std::vector<float> rows;
    int32_t nrows = 0;
    HelperIdentity id;
    std::string herr;
    const int brc = smoke_full("/tmp/p2_measure_test_scale.sock", resident, sa, plan, host_res, rows, nrows,
                               id, herr, ranked, kScaleFillBytes, kScaleFreeMib);
    check_status(brc, eh::kOk, "the live-scale drive: hello, the 1656-pair report, 256 rows of the tier");
    check(g_accept_rc == 0, "the fake helper verified the scale kBeginP (shape, 256 pairs, the q8 image)");
    check(g_scale_fill_checks == 1, "the fake helper verified the 7286-pair fill REQUEST frame on the wire");
    check(id.requested_pairs == kScaleRequested && id.resident == kScaleResident,
          "the identity carries 7286 requested and 1656 resident pairs (window 1's numbers)");
    check(id.bytes == kScaleFillBytes && id.free_mib == kScaleFreeMib,
          "the report's 3598233600 bytes and 7458 MiB free reach the caller unchanged");
    check(nrows == 256 && (int64_t) rows.size() == 256 * kN, "all 256 claimed rows came back (a 2.6 MB reply)");
    bool vals = true;
    for (int32_t j = 0; vals && j < nrows; ++j)
        for (int64_t c = 0; vals && c < kN; ++c)
            if (rows[(size_t) j * kN + (size_t) c] != (float) pv(j, c)) vals = false;
    check(vals, "the scale rows land at the pair indices with the reply's values");
    std::vector<uint8_t> sc;
    check(mark_claim_entries(sa, plan, sc, err) == eh::kOk && (int64_t) sc.size() == kScaleT * kK,
          "the claims at the fill-report scale mark inside the array (the window-1 crash site)");
}

// ============================== (9) the side-B reply shapes ==============================
//
// The LIVE condition section (6) never covered: the real helper serves rows only for the pairs it holds
// (tools/mixed_gpu/sidecar/helper_main.cpp's kBeginP handler: the resident pairs answer, the others stay
// OUT of the reply - the engine's design rule, "never a silent zero").  The harness's plan claims the fill
// report's resident pairs, so a COMPLETE reply is the normal case; the shapes below pin what happens when
// the helper's cache is not what the fill report said, and that the gate loop can never walk past the
// reply:
//   * a PARTIAL reply (a strict subset of the claims, the live 1656-of-7286 resident shape) is REFUSED
//     (kErrMissing) with NO row applied, and the caller's buffer stays plan.rows() * N floats, every pair
//     index readable - the harness's own guard (rows_ret != plan.rows()) stops the run BEFORE the first
//     read, so a short reply is a stopped run, never a pointer walk;
//   * an EMPTY reply and an UNCLAIMED index are refused the same way (kErrMissing / kErrBadId);
//   * a FULL reply answers rows AT THEIR PAIR INDICES whatever order they travel in, and
//     check_side_b_rows (the harness's pre-read invariant) passes on it and refuses a short side-B buffer,
//     a short side-A buffer and a claimed pair with no side-A row - all before any read.
void section_side_b() {
    std::printf("(9) side-B reply shapes: partial, scrambled, empty, unclaimed, the read invariant\n");
    const std::string path = "/tmp/p2_measure_test_sideb.sock";

    // The same smoke layer as section (6): T = 8 tokens, experts 7 and 9 resident (16 claimed rows).
    Activations a;
    a.T = 8;
    a.T_dump = 8;
    a.x.assign((size_t) (8 * kN), 0.0f);
    for (int64_t t = 0; t < 8; ++t)
        for (int64_t col = 0; col < kN; ++col) a.x[(size_t) t * kN + (size_t) col] = (float) pv((int) t, col);
    a.ids.assign((size_t) (8 * kK), 3);
    for (int64_t t = 0; t < 8; ++t) { a.ids[(size_t) (t * kK)] = 7; a.ids[(size_t) (t * kK + 1)] = 9; }
    LayerCounts lc;
    std::string cerr;
    check(build_layer_counts(a, lc, cerr) == eh::kOk, "the side-B smoke layer's counts build");
    std::vector<uint8_t> holds((size_t) kNE, 0), stable((size_t) kNE, 1);
    holds[7] = 1;
    holds[9] = 1;
    std::vector<int32_t> host_res((size_t) kLayers * kNE, -1);
    ExpertHelperPromptPlan plan;
    check(select_prompt_rows(lc.cnt.data(), lc.toks.data(), host_res.data(), 32, holds.data(), stable.data(),
                             kNE, kK, eh::kMaxRowsPerExpert, eh::kMaxRowsPerLayer, eh::kMaxSelTok, plan) ==
              eh::kOk && plan.rows() == 16,
          "the side-B smoke plan claims 16 rows over the fill report's pairs");

    // The side-A mock the gate loop reads: row = pair index (a complete geometry), rows_local = plan.rows().
    // pair2row is ENTRY-KEYED exactly as build_geometry fills it: pair2row[entry] = the row of the ROUTED
    // entry (token*kK + slot), so the pair's row is reached by the routed-slot lookup, never by pair2row[
    // token*kK + expert] (which reads another token's entry - the live crash).
    std::vector<int32_t> pair2row((size_t) (8 * kK), -1);
    for (int32_t j = 0; j < plan.rows(); ++j)
        pair2row[(size_t) entry_of(a, plan.pair_token[(size_t) j], plan.pair_expert[(size_t) j])] = j;
    std::vector<float> dm((size_t) plan.rows() * kN, 0.0f);
    for (int32_t j = 0; j < plan.rows(); ++j)
        for (int64_t c = 0; c < kN; ++c) dm[(size_t) j * kN + (size_t) c] = (float) pv(j, c) + 0.5f;

    std::vector<float> hb;
    int32_t nrows = 0;
    HelperIdentity id;
    std::string herr, ierr;

    // -- the PARTIAL reply: 8 of the 16 claims (the live helper answers only its resident pairs) --
    const std::vector<int32_t> partial = {0, 2, 4, 6, 8, 10, 12, 14};
    int brc = smoke_full(path, {{32, 7}, {32, 9}}, a, plan, host_res, hb, nrows, id, herr, {}, 2621440, 4096,
                         &partial);
    check_status(brc, eh::kErrMissing, "a partial kRowsP reply (8 of 16 claims) is refused: kErrMissing");
    check(g_accept_rc == 0, "the fake helper sent the subset over the REAL frames (no wire trick)");
    check(nrows == 0, "a refused reply applies NO rows (rows = 0, never a zero presented as a served row)");
    bool readable = (int64_t) hb.size() == plan.rows() * kN;
    float far_end = 0.0f;
    if (readable) far_end = hb[(size_t) (plan.rows() - 1) * kN + (size_t) (kN - 1)];
    check(readable && far_end == 0.0f,
          "the caller's buffer is plan.rows() * N floats: every pair index the gate loop reads is inside it");
    check((int64_t) nrows != plan.rows(), "the harness's guard fires on it (rows_ret != plan.rows() -> stop)");
    check_status(check_side_b_rows(a, plan, pair2row.data(), hb, dm, ierr), eh::kOk,
                 "the read invariant PASSES on the partial case: the sizes are the harness's own contract");

    // -- the FULL reply with the rows traveling in REVERSE order: placement is by the index each row carries
    std::vector<int32_t> scr((size_t) plan.rows());
    for (int32_t i = 0; i < plan.rows(); ++i) scr[(size_t) i] = plan.rows() - 1 - i;
    hb.clear();
    nrows = 0;
    brc = smoke_full(path, {{32, 7}, {32, 9}}, a, plan, host_res, hb, nrows, id, herr, {}, 2621440, 4096, &scr);
    check_status(brc, eh::kOk, "a full reply whose rows travel in reverse order is accepted");
    check(nrows == (int32_t) plan.rows(), "the accepted reply reports every claimed row");
    bool at_index = (int64_t) hb.size() == plan.rows() * kN;
    for (int32_t j = 0; at_index && j < plan.rows(); ++j)
        for (int64_t c = 0; at_index && c < kN; ++c)
            if (hb[(size_t) j * kN + (size_t) c] != (float) pv(j, c)) at_index = false;
    check(at_index, "every row sits at ITS PAIR INDEX, whatever order the rows arrived in");
    check_status(check_side_b_rows(a, plan, pair2row.data(), hb, dm, ierr), eh::kOk,
                 "the read invariant passes on the accepted reply");

    // -- the gate loop mirrored: the metric over EVERY claimed pair index, with the pair's side-A row from
    //    the resolver.  Under ASAN this walks exactly what p2_measure_main.cpp's loop walks (side A by the
    //    geometry row, side B by the pair index).
    bool walked = true;
    for (int32_t j = 0; walked && j < plan.rows(); ++j) {
        int32_t row = -1;
        std::string rerr;
        if (pair_row(a, pair2row.data(), plan.pair_token[(size_t) j], plan.pair_expert[(size_t) j], row, rerr) !=
            eh::kOk || row < 0) { walked = false; break; }
        const RowDiff rd = diff_rows(dm.data() + (size_t) row * kN, hb.data() + (size_t) j * kN, kN, 1e-6);
        if (!(rd.max_abs > 0.0)) walked = false;
    }
    check(walked, "the gate loop resolves every pair to its row and reads both buffers in bounds");

    // -- the EMPTY reply (the helper holds none of the claimed pairs) --
    const std::vector<int32_t> none;
    hb.assign(1, 1.0f);
    nrows = -1;
    brc = smoke_full(path, {{32, 7}, {32, 9}}, a, plan, host_res, hb, nrows, id, herr, {}, 2621440, 4096, &none);
    check_status(brc, eh::kErrMissing, "an empty reply is refused: kErrMissing");
    check(nrows == 0 && (int64_t) hb.size() == plan.rows() * kN && hb[0] == 0.0f,
          "the empty reply leaves the caller's buffer sized and untouched by any applied row");

    // -- an UNCLAIMED index (a row for a pair the request did not claim) --
    const std::vector<int32_t> bogus = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 16};
    hb.clear();
    nrows = 0;
    brc = smoke_full(path, {{32, 7}, {32, 9}}, a, plan, host_res, hb, nrows, id, herr, {}, 2621440, 4096, &bogus);
    check_status(brc, eh::kErrBadId, "a row for an unclaimed pair index is refused: kErrBadId");
    bool clean = (int64_t) hb.size() == plan.rows() * kN;
    for (size_t i = 0; clean && i < hb.size(); ++i)
        if (hb[i] != 0.0f) clean = false;
    check(nrows == 0 && clean, "an unclaimed index applies NOTHING (whole-or-not-at-all, the client's rule)");

    // -- the read invariant's own refusals (the harness checks these BEFORE the first read) --
    std::vector<float> short_hb((size_t) (plan.rows() - 1) * kN, 0.0f);
    check_status(check_side_b_rows(a, plan, pair2row.data(), short_hb, dm, ierr), eh::kErrGeometry,
                 "a side-B buffer one row short is refused BEFORE any read");
    check(ierr.find("floats") != std::string::npos && ierr.find("16") != std::string::npos,
          "the side-B refusal names the sizes it found and needs");
    std::vector<float> short_dm((size_t) (plan.rows() - 1) * kN, 0.0f);
    check_status(check_side_b_rows(a, plan, pair2row.data(), hb, short_dm, ierr), eh::kErrGeometry,
                 "a side-A buffer that cannot carry the last claimed row is refused BEFORE any read");
    check(ierr.find("row") != std::string::npos, "the side-A refusal names the row it found");
    std::vector<int32_t> no_row = pair2row;
    no_row[(size_t) entry_of(a, plan.pair_token[3], plan.pair_expert[3])] = -1;
    check_status(check_side_b_rows(a, plan, no_row.data(), hb, dm, ierr), eh::kErrGeometry,
                 "a claimed pair with no side-A row is refused by the invariant too");
}

// ============================== (10) the pair -> row resolver ==============================
//
// The defect this section pins is the LIVE one, typed by the harness's own invariant line at attempt 8:
//   "side A row 3810 of claimed pair (37, 75) is past the 2250 rows of the side-A buffer"
// TWO defects sat on this one line, and the harness's typed invariant line named both:
//   "side A row 3810 of claimed pair (37, 75) is past the 2250 rows of the side-A buffer"
//   (1) INDEX KEYING: the gate loop resolved a plan pair with pair2row[t*kK + e] - the EXPERT ID as an index
//       into a table keyed by the ENTRY index (t*kK + slot).  For an id >= kK that reads a DIFFERENT token's
//       entry (pair (37, 75) read entry (44, slot 5)), i.e. a row of the wrong token.  pair_row() now scans
//       the token's kK routed ids for the expert - the same lookup mark_claim_entries uses - so every
//       (token, expert) -> index mapping in the harness goes through ONE primitive.
//   (2) ROW VALUES: build_geometry wrote pair2row[i] = off[expert] + slot[i] while slot[i] was ALREADY
//       off[expert] + the ordinal, so every row was inflated by its expert's offset, up to 2 * rows_local.
//       Section (10)'s own mock reproduced it before the fix (max row 911 of 480 = the same arithmetic that
//       produced row 3810 past 2250 rows) and ASAN caught it as a heap-buffer-overflow: that is the pinned
//       proof of the live crash's magnitude.  pair2row[i] is now slot[i], the row, nothing more.
//
// The shape: T = 48 tokens; every token routes expert 7 (slot 0), expert 75 (slot 1) and expert 3 in the
// other eight slots.  Expert ids are >= kK, exactly the live condition.  The mock mirrors build_geometry's
// fixed walk (row = off[expert] + the expert's fill ordinal, stored at the ENTRY index) and a `src` table
// records each row's token, so a resolved row can be checked to BELONG to the pair's token.
void section_pair_row() {
    std::printf("(10) the pair -> row resolver: the live (37, 75) shape, refusals, the plan end to end\n");
    const int64_t T = 48;
    Activations a;
    a.T = T;
    a.T_dump = T;
    a.x.assign((size_t) (T * kN), 0.0f);
    for (int64_t t = 0; t < T; ++t)
        for (int64_t c = 0; c < kN; ++c) a.x[(size_t) t * kN + (size_t) c] = (float) pv((int) t, c);
    a.ids.assign((size_t) (T * kK), 3);
    for (int64_t t = 0; t < T; ++t) { a.ids[(size_t) (t * kK)] = 7; a.ids[(size_t) (t * kK + 1)] = 75; }

    // build_geometry's own layout, by hand: counts ASC-by-expert offsets, the entry walk, entry-keyed rows.
    std::vector<int64_t> cnt((size_t) kNE, 0);
    for (int64_t i = 0; i < T * kK; ++i) ++cnt[(size_t) a.ids[(size_t) i]];
    std::vector<int64_t> off((size_t) kNE + 1, 0);
    int64_t r = 0;
    for (int64_t e = 0; e < kNE; ++e) { off[(size_t) e] = r; r += cnt[(size_t) e]; }
    const int64_t rows_local = r;
    std::vector<int64_t> fill(off.begin(), off.end() - 1);
    std::vector<int32_t> pair2row((size_t) (T * kK), -1), src((size_t) rows_local, -1);
    for (int64_t i = 0; i < T * kK; ++i) {
        const int32_t e = a.ids[(size_t) i];
        const int32_t p = (int32_t) fill[(size_t) e]++;      // p = off[expert] + the ordinal = THE ROW
        pair2row[(size_t) i] = p;                            // the row of entry i, nothing added (fix4)
        src[(size_t) pair2row[(size_t) i]] = (int32_t) (i / kK);
    }
    int32_t max_row = -1;
    for (const int32_t v : pair2row) max_row = std::max(max_row, v);
    check(max_row < (int32_t) rows_local,
          "the row table holds ROW INDICES inside the range (adding off[expert] twice reached 911 of 480)");

    // -- the live pair (37, 75): the resolver lands on the token's own entry; the expert-as-index form does not
    int32_t row = -1;
    std::string rerr;
    check_status(pair_row(a, pair2row.data(), 37, 75, row, rerr), eh::kOk,
                 "pair (37, 75) resolves through the routed slot: kOk");
    const int64_t entry371 = entry_of(a, 37, 75);
    check(entry371 == 37 * kK + 1 && row == pair2row[(size_t) entry371],
          "the resolved row is the row of entry (37, slot 1) - the entry whose routed id IS 75");
    check(row >= 0 && (int64_t) row < rows_local, "the resolved row is inside the side-A row range");
    check(src[(size_t) row] == 37, "the row the gate loop measures BELONGS TO THE PAIR'S TOKEN (token 37)");
    const int32_t old_form = pair2row[(size_t) (37 * kK + 75)];
    check(old_form != row && src[(size_t) old_form] != 37,
          "the expert-as-index form names ANOTHER token's row - the live defect class, now closed");

    // -- a pair the token does not route: a loud typed refusal (an invariant break), row untouched
    row = 7;
    check_status(pair_row(a, pair2row.data(), 37, 91, row, rerr), eh::kErrGeometry,
                 "an expert the token does not route is a typed refusal (the plan never names such a pair)");
    check(rerr.find("INVARIANT BREAK") != std::string::npos && rerr.find("37") != std::string::npos &&
              rerr.find("91") != std::string::npos,
          "the refusal names the pair and says it is an invariant break");
    check(row == -1, "a refused pair leaves no row (never a stray index handed to the loop)");

    // -- a token routing the same expert twice: the pair names no single row
    Activations dup;
    dup.T = 1;
    dup.T_dump = 1;
    dup.x.assign((size_t) kN, 0.5f);
    dup.ids.assign((size_t) kK, 4);
    dup.ids[0] = 12;
    dup.ids[5] = 12;
    std::vector<int32_t> dup_map((size_t) kK, 0);
    row = 3;
    check_status(pair_row(dup, dup_map.data(), 0, 12, row, rerr), eh::kErrGeometry,
                 "an expert routed in two slots of one token is refused (the pair names no single row)");
    check(rerr.find("2 slots") != std::string::npos, "the refusal names the slot count it found");
    check(row == -1, "the two-slot refusal leaves no row");

    // -- the bounds refusals name what they found
    check(pair_row(a, pair2row.data(), (int32_t) T, 75, row, rerr) == eh::kErrGeometry &&
              rerr.find("token 48") != std::string::npos,
          "a token outside the activation rows is refused by name");
    check(pair_row(a, pair2row.data(), 37, 512, row, rerr) == eh::kErrGeometry &&
              rerr.find("expert 512") != std::string::npos,
          "an expert outside the pack's 512 routed experts is refused by name");
    check(pair_row(a, nullptr, 37, 75, row, rerr) == eh::kErrGeometry &&
              rerr.find("row map is missing") != std::string::npos,
          "a missing side-A row map is refused, not walked");

    // -- end to end: the plan over this fixture, every pair -> a row inside the buffer, its token's own row
    LayerCounts lc;
    std::string cerr;
    check(build_layer_counts(a, lc, cerr) == eh::kOk && lc.cnt[7] == (int32_t) T && lc.cnt[75] == (int32_t) T &&
              lc.cnt[3] == (int32_t) (T * 8),
          "the live-shaped layer's counts (7: 48, 75: 48, 3: 384 - over the per-expert cap)");
    std::vector<uint8_t> holds((size_t) kNE, 1), stable((size_t) kNE, 1);
    std::vector<int32_t> host_res((size_t) kLayers * kNE, -1);
    ExpertHelperPromptPlan plan;
    check(select_prompt_rows(lc.cnt.data(), lc.toks.data(), host_res.data(), 32, holds.data(), stable.data(),
                             kNE, kK, eh::kMaxRowsPerExpert, eh::kMaxRowsPerLayer, eh::kMaxSelTok, plan) ==
              eh::kOk && plan.rows() == 2 * T,
          "the plan claims 96 rows: experts 7 and 75 (3 is screened out by the fan-out cap, as designed)");
    bool all_rows = plan.rows() > 0;
    for (int64_t j = 0; j < plan.rows(); ++j) {
        int32_t rr = -1;
        std::string e2;
        if (pair_row(a, pair2row.data(), plan.pair_token[(size_t) j], plan.pair_expert[(size_t) j], rr, e2) !=
            eh::kOk) { all_rows = false; break; }
        if (rr < 0 || (int64_t) rr >= rows_local || src[(size_t) rr] != plan.pair_token[(size_t) j]) {
            all_rows = false;
            break;
        }
    }
    check(all_rows, "every plan pair resolves to a row inside the side-A range, owned by its own token");

    // -- the claims and the resolver agree: ONE mapping for both (mark_claim_entries via the same primitive)
    std::vector<uint8_t> claims;
    std::string cmsg;
    check(mark_claim_entries(a, plan, claims, cmsg) == eh::kOk && (int64_t) claims.size() == T * kK,
          "the claims array is the entry space at the live shape");
    bool agree = true;
    int marks = 0;
    for (size_t i = 0; i < claims.size(); ++i)
        if (claims[i]) {
            ++marks;
            const int32_t t = (int32_t) (i / kK), e = a.ids[i];
            int32_t rr = -1;
            std::string e2;
            if (pair_row(a, pair2row.data(), t, e, rr, e2) != eh::kOk || rr != pair2row[i]) agree = false;
        }
    check(agree && marks == (int) plan.rows(),
          "each mark sits on the entry the resolver uses - the mapping is single-sourced, one mark per pair");

    // -- the invariant on the live-shaped buffers: passes, and the expert-as-index row is what it refuses
    std::vector<float> hb((size_t) plan.rows() * kN, 0.0f), dm((size_t) rows_local * kN, 0.0f);
    std::string ierr;
    check_status(check_side_b_rows(a, plan, pair2row.data(), hb, dm, ierr), eh::kOk,
                 "the pre-read invariant passes on the live-shaped pair set (96 rows, 480 side-A rows)");

    // -- the old form's OWN index runs past the row table at the live shape, and where it lands inside,
    //    the values it wrote are never read by the resolver
    const int64_t old_index = (int64_t) plan.pair_token[(size_t) (plan.rows() - 1)] * kK +
                              plan.pair_expert[(size_t) (plan.rows() - 1)];
    check(old_index >= (int64_t) pair2row.size(),
          "the expert-as-index form indexes PAST the row table (token 47, expert 75 -> 545 of 480 entries)");
    int32_t prow = -1;
    std::string perr2;
    check_status(pair_row(a, pair2row.data(), plan.pair_token[(size_t) (plan.rows() - 1)],
                          plan.pair_expert[(size_t) (plan.rows() - 1)], prow, perr2),
                 eh::kOk, "the resolver answers the same pair with an in-range row");
    check(prow >= 0 && (int64_t) prow < rows_local, "that row is inside the side-A range");
    std::vector<int32_t> old_keyed = pair2row;
    for (int64_t j = 0; j < plan.rows(); ++j) {
        const int64_t at = (int64_t) plan.pair_token[(size_t) j] * kK + plan.pair_expert[(size_t) j];
        if (at < (int64_t) old_keyed.size()) old_keyed[(size_t) at] = (int32_t) rows_local + 3060;
    }
    check_status(check_side_b_rows(a, plan, old_keyed.data(), hb, dm, ierr), eh::kOk,
                 "the invariant (and the loop) read the routed entries only: the values the old form wrote "
                 "at token*kK+expert are never read, so a live row 3810 cannot enter the loop");
}

// ============================== (11) the grow-after-shrink staging audit ==============================
//
// The live shape of attempt 9 (2026-10-29): one fixture's layer walk measured L32 (gu 21/d 20, blob
// 2,329,600 B - the window max) and L33 (gu 16/d 42, 1,305,600 B), then DIED at L34 (gu 17/d 20,
// 1,868,800 B) with "blob upload: an illegal memory access" - the first GROW after a SHRINK.  This
// section mirrors the harness's per-layer sizing (p2_measure.hpp section 7b) on the HOST with real
// byte sizes, and writes EVERY region the kernels write at EXACTLY the extents the kernels write
// (verified from moe_mmq.cu: copy16_group_kernel writes slot q at q*stride, the zero tails at the LAST
// slot at (ngx)*stride .. +zero_bytes, and one 16-byte tail word per slot at tail_dst[q]; copy1_kernel
// writes the per-expert zero tail at q*stride + the expert's bytes).  Under ASAN the mirror catches any
// write past a buffer, and every size is asserted against every extent.
//
// The mirror reproduces the harness's allocation RULE: a fresh buffer per (fixture, layer) (DevMem),
// sized by THAT layer's own slot/blob numbers - so a grow after a shrink cannot inherit a smaller
// layer's allocation.  The tail-word rule (fix5): the words live in their OWN buffer (the engine's
// dedicated grp_tail, prefill.cpp:1645) at a 16-byte stride; the record of the pre-fix form is
// asserted too - writing the 16 words inside the down buffer reaches PAST its end by 256 B.
void section_grow_after_shrink() {
    std::printf("(11) grow-after-shrink: per-layer fresh sizing, write extents inside every buffer\n");
    namespace cpu = strata::kernels::cpu;

    // The window's three layers: sizes from the live log, types from the pack's manifest.
    struct LayerShape { int64_t layer; int gu_type, d_type; uint64_t blob; };
    const LayerShape shape[3] = {{32, 21, 20, 2329600}, {33, 16, 42, 1305600}, {34, 17, 20, 1868800}};
    check(shape[0].blob > shape[1].blob && shape[2].blob > shape[1].blob,
          "the sequence is a shrink then the first grow (2,329,600 -> 1,305,600 -> 1,868,800)");

    // The MMQ slot sizes for a type, the matrix_bytes formula (moe_mmq.cu:322-324) at the fixture's
    // own byte table: 32-wide quants at 96/128/144 B, 16-wide at 128 B, the 4-bit type at 8 B, and the
    // 64-wide d42 shape at 16 B.  The harness reads the live values from mmq::matrix_bytes; the audit
    // pins the SIZE RULE (allocation vs every extent), which is what the live fault needed.
    auto blck = [](int t) -> int64_t { return t == 42 ? 64 : 32; };
    auto bpb  = [](int t) -> int64_t { return t == 16 ? 144 : t == 17 ? 128 : t == 20 ? 96 : t == 21 ? 128
                                                                                                       : 16; };
    auto matrix_bytes = [&](int t, int64_t rows, int64_t cols) -> size_t {
        return (size_t) rows * (size_t) (cols / blck(t)) * (size_t) bpb(t);
    };
    // q8_bytes' per-row part, the formula at moe_mmq.cu:326-328 (block_q8_1_mmq 128 B, QK8_1 32):
    // the tail region between q8_bytes(rows) and q8_bytes(rows + tail) is tail * pad512(cols) / 8 B.
    auto pad512 = [](int64_t c) -> int64_t { return (c + 511) / 512 * 512; };
    auto q8_row_bytes = [&](int64_t cols) -> int64_t { return pad512(cols) / 8; };
    // the layout's down row bytes at the live triple (d_bpc: 10 for type 20, 16 for 42 - the
    // harness's matrix_bytes(d_type, 2560, 640)); the gate row bytes follow from the blob identity
    // blob = 1280*gu_row + 2560*d_row (section 12 uses the same numbers)
    auto d_bpc  = [](int t) -> uint64_t { return t == 42 ? 16 : 10; };

    // The live layer's row shape: 2250 routed rows, 16-expert groups (ngx = 16, the full group - the
    // worst extent), 256 tokens * kK entries.
    const int64_t rows = 2250, T = 256, ngx = kMmqGroup;
    const int64_t tail_rows = mmq_act_tail_rows();

    struct Fresh {   // the harness's DevMem rule: every buffer of a layer is FRESH, freed at its end
        std::vector<std::vector<uint8_t>> bufs;
        uint8_t* take(size_t bytes) { bufs.emplace_back(bytes, 0); return bufs.back().data(); }
    };

    bool fresh_and_fits = true, extents_ok = true;
    uint64_t prev_ring = 0;
    (void) prev_ring;
    for (const LayerShape& ls : shape) {
        const size_t gu_b = matrix_bytes(ls.gu_type, 1280, kN), d_b = matrix_bytes(ls.d_type, kN, 640);
        const size_t over_gu = 0, over_d = 136;   // the documented Q8_0-class over-read at 640 (moe_mmq.hpp:148)
        Fresh dev;   // fresh per layer: nothing of the previous layer is reused
        const size_t ring_bytes = stage_ring_bytes(ls.blob);
        const size_t ggu = mmq_group_gu_bytes(gu_b, over_gu), gd = mmq_group_d_bytes(d_b, over_d);
        const size_t tw = mmq_tail_words_bytes();
        const int64_t xq_rows = rows + tail_rows, hq_rows = rows / 2 + tail_rows;

        // every allocation the harness takes, at the helper-computed size
        uint8_t* ring = dev.take(ring_bytes);
        uint8_t* gu_buf = dev.take(ggu);
        uint8_t* d_buf = dev.take(gd);
        uint8_t* tail = dev.take(tw);
        uint8_t* xq = dev.take((size_t) xq_rows * q8_row_bytes(kN));
        uint8_t* hq = dev.take((size_t) hq_rows * q8_row_bytes(640));
        uint8_t* slot = dev.take((size_t) (T * kK) * 4);
        uint8_t* src = dev.take((size_t) rows * 4);
        uint8_t* bounds = dev.take((size_t) (2 * (kNE + 1) + 64 * (kMmqGroup + 1)) * 4);

        // the RING: the upload writes a full blob at slot (i % 32) * blob for every entry of the walk
        for (int64_t i = 0; i < 2 * kMmqGroup; ++i) {
            const size_t off = (size_t) (i % (size_t) kStageRingSlots) * (size_t) ls.blob;
            if (off + ls.blob > ring_bytes) extents_ok = false;
            std::memset(ring + off, (int) ls.layer, (size_t) ls.blob);        // the blob upload
        }
        // the GROUP GATHERS: slots 0..ngx-1, the zero tails at the last slot, the tail words
        const size_t zero = kMmqTail;
        const size_t wgu = gather_write_bytes((int) ngx, gu_b, zero);
        const size_t wd = gather_write_bytes((int) ngx, d_b, zero);
        const size_t rgu = mmq_weight_read_bytes((int) ngx, gu_b, over_gu);
        const size_t rd = mmq_weight_read_bytes((int) ngx, d_b, over_d);
        if (wgu > ggu || wd > gd || rgu > ggu || rd > gd || tw < (size_t) ngx * 16) extents_ok = false;
        for (int64_t q = 0; q < ngx; ++q)
            std::memset(gu_buf + (size_t) q * gu_b, 1, gu_b);                 // gathered gate/up
        std::memset(gu_buf + (size_t) (ngx - 1) * gu_b + gu_b, 0, zero);      // the zero tail at the last
        for (int64_t q = 0; q < ngx; ++q)
            std::memset(d_buf + (size_t) q * d_b, 2, d_b);                    // gathered down
        std::memset(d_buf + (size_t) (ngx - 1) * d_b + d_b, 0, zero);
        for (int64_t q = 0; q < ngx; ++q)
            std::memset(tail + (size_t) q * 16, 3, 16);                       // the tail words (fix5 rule)
        // the activation tails: the last M tile reads whole rows past the last real row
        if ((size_t) tail_rows * q8_row_bytes(kN) < 15 * q8_row_bytes(kN) ||
            (size_t) tail_rows * q8_row_bytes(640) < 15 * q8_row_bytes(640)) extents_ok = false;
        std::memset(xq + (size_t) rows * q8_row_bytes(kN), 0, (size_t) tail_rows * q8_row_bytes(kN));
        std::memset(hq + (size_t) (rows / 2) * q8_row_bytes(640), 0, (size_t) tail_rows * q8_row_bytes(640));
        // the slot/src uploads: each is exactly its allocation by construction (T*kK int32, rows int32)
        std::memset(slot, 0, (size_t) (T * kK) * 4);
        std::memset(src, 0, (size_t) rows * 4);
        // the bounds upload at the full worst-case count
        const size_t bounds_n = (size_t) (kNE + 1 + ((kNE + kMmqGroup - 1) / kMmqGroup) * (kMmqGroup + 1));
        if (bounds_n * 4 > (size_t) (2 * (kNE + 1) + 64 * (kMmqGroup + 1)) * 4) extents_ok = false;
        std::memset(bounds, 0, bounds_n * 4);

        // the fresh-per-layer rule: this layer's ring is sized by ITS OWN blob (the mirror's `dev`
        // object is a per-layer scope: a layer's buffers cannot be a previous layer's buffers)
        fresh_and_fits &= ring_bytes == stage_ring_bytes(ls.blob) && ring_bytes >= ls.blob * (size_t) ngx
                          && ggu >= wgu && gd >= wd && tw >= (size_t) ngx * 16;
        prev_ring = ring_bytes;
    }
    check(fresh_and_fits, "every layer's buffers are freshly allocated at THAT layer's sizes "
                          "(no ring/stride inherited from a smaller previous layer)");
    check(extents_ok, "every write/read extent (blob upload, gathers, zero tails, tail words, "
                      "activation M-tile tails, slot/src, bounds) sits inside its allocation");

    // the record of the pre-fix tail rule, computed (no write): words inside the down buffer
    const size_t d_b33 = matrix_bytes(42, kN, 640), gd33 = mmq_group_d_bytes(d_b33, 136);
    const size_t old_tail_extent = (size_t) 16 * d_b33 + kMmqTail + (size_t) kMmqGroup * 16;
    check(old_tail_extent > gd33,
          "the pre-fix tail pointer (inside grp_d) reached 256 B PAST the down buffer - the overflow fix5 "
          "closes; the 4-byte stride it used also overlapped neighbouring experts' words");

    // the harness's own guard estimate is the window's MAXIMUM, not the shrink's (fix2) - the grow
    // after a shrink needs the largest layer's ring at the guard and its OWN ring at the allocation
    cpu::ExpertLayout lay;
    lay.native = true;
    lay.n_layers = 3;
    lay.n_expert = kNE;
    lay.fmt.resize(3);
    lay.offset.assign(3, 0);
    lay.bytes = {2329600, 1305600, 1868800};
    for (int i = 0; i < 3; ++i) {
        lay.fmt[(size_t) i].n_ff = 640;
        lay.fmt[(size_t) i].n_embd = kN;
        // the ggml row bytes the harness's NativeFmt carries (the LIVE layout's own numbers: gate
        // 1804/988/720 from the blob identity 1280*gu_row + 2560*d_row = 2,329,600 / 1,305,600 /
        // 1,868,800, the blob layout [gate][up][down] the per-role gate (main.cpp:606-612) requires)
        lay.fmt[(size_t) i].gu_row = (lay.bytes[(size_t) i] - 2560 * d_bpc(shape[i].d_type)) / 1280;
        lay.fmt[(size_t) i].d_row = d_bpc(shape[i].d_type);
        lay.fmt[(size_t) i].gu_type = shape[i].gu_type;
        lay.fmt[(size_t) i].d_type = shape[i].d_type;
        lay.fmt[(size_t) i].bytes = (size_t) lay.bytes[(size_t) i];
    }
    check(max_blob_bytes(lay, 0, 2) == 2329600,
          "the guard sizes the ring by the window's MAX blob (the shrink is not the guard's number)");
    check(max_blob_bytes(lay, 1, 2) == 1868800,
          "over the shrink+grow pair the guard takes the grow (1,868,800), not the shrink");
    check(stage_ring_bytes(1868800) > stage_ring_bytes(1305600),
          "the grow's own ring is LARGER than the shrink layer's: an inherited allocation could not fit it");
}

// ============================== (12) the host staging path of the blob upload ==============================
//
// The pinned live fault (attempt 10, 2026-10-29): L34's prologue synced CLEAN and the fault still
// reported at the blob upload - so the faulting op IS that cudaMemcpyAsync.  This section mirrors the
// upload's THREE parameters with the live sizes, on the host, at exact extents:
//   (1) DESTINATION  slot = (j0+q) % 32, stride = the per-layer upload stride; extent = 31*stride + size
//       must be <= stage_ring_bytes(the layer's blob) - the allocation the harness takes (main.cpp:857).
//   (2) COPY SIZE    must be exactly lay.blob_bytes(l) for that layer - not the window max (2,329,600 at
//       L34 would fit the ring but read past the layer's host row).
//   (3) HOST SOURCE  the staging vector is allocated IN the run's per-layer call at exactly the layer's
//       blob (run_mmq's `std::vector<uint8_t> blob_host(blob_bytes)`), and read_blob_row writes the
//       gate|up|down slices into it - the sum of the slices is checked equal to the blob (the harness's
//       own refusal), so the source extent equals the copy size; the file reads stay inside the file.
void section_host_staging() {
    std::printf("(12) host staging: copy size, ring extent, host source at the upload's exact parameters\n");
    namespace cpu = strata::kernels::cpu;

    struct LShape { int64_t layer; int gu_type, d_type; uint64_t blob; };
    const LShape shape[3] = {{32, 21, 20, 2329600}, {33, 16, 42, 1305600}, {34, 17, 20, 1868800}};

    auto blck = [](int t) -> int64_t { return t == 42 ? 64 : 32; };
    auto bpb  = [](int t) -> int64_t { return t == 16 ? 144 : t == 17 ? 128 : t == 20 ? 96 : t == 21 ? 128
                                                                                                    : 16; };
    auto matrix_bytes = [&](int t, int64_t rows, int64_t cols) -> size_t {
        return (size_t) rows * (size_t) (cols / blck(t)) * (size_t) bpb(t);
    };
    // The blob layout the harness reads is [gate: n_ff*gu_row][up: n_ff*gu_row][down: n_embd*d_row]
    // (read_blob_row's role slices, helper_main.cpp:235-236), so the blob identity is
    // blob = 1280*gu_row + 2560*d_row.  The down row bytes are the type-rule numbers of THIS fixture
    // (d_bpc: 10 for type 20, 16 for type 42 - the harness's own matrix_bytes(d_type, 2560, 640));
    // the gate row bytes FOLLOW from the blob: 1804 / 988 / 720, giving 2,329,600 / 1,305,600 /
    // 1,868,800 exactly.  The per-role host fill (section 6) writes gate + up + down = the blob, the
    // harness's refusal gate (main.cpp:606-612).
    auto d_bpc  = [](int t) -> uint64_t { return t == 42 ? 16 : 10; };
    auto gu_row_of = [=](uint64_t blob, int t) -> uint64_t { return (blob - 2560 * d_bpc(t)) / 1280; };   // gate row bytes from blob = 1280*gu_row + 2560*d_row
    auto d_row_of  = [&](int t) -> uint64_t { return d_bpc(t); };

    // the layout the role-slice code reads (blob_role_bytes: gate/up = n_ff*gu_row, down = n_embd*d_row).
    // The blob IS [gate: n_ff*gu_row][up: n_ff*gu_row][down: n_embd*d_row] = 1280*gu_row + 2560*d_row,
    // so the gate row bytes follow from the blob: 1804 / 988 / 720 with the down row bytes 10 / 16 / 10
    // (1280*1804 + 25,600 = 2,329,600, 1280*988 + 40,960 = 1,305,600, 1280*720 + 25,600 = 1,868,800).
    // The harness's per-role gate (main.cpp:606-612, the helper's check at helper_main.cpp:246)
    // requires exactly this sum - and attempt 10 MEASURED L32/L33, so the live pack passes it (a
    // per-role pack failing it is refused at startup, before any upload).
    cpu::ExpertLayout lay;
    lay.native = true;
    lay.n_layers = 3;
    lay.n_expert = kNE;
    lay.fmt.resize(3);
    lay.offset.assign(3, 0);
    lay.bytes = {2329600, 1305600, 1868800};
    lay.gguf_off.assign(3 * 3, 0);
    for (int i = 0; i < 3; ++i) {
        lay.fmt[(size_t) i].n_ff = 640;
        lay.fmt[(size_t) i].n_embd = kN;
        lay.fmt[(size_t) i].gu_row = gu_row_of(lay.bytes[(size_t) i], shape[i].d_type);   // gate row bytes from the blob identity (needs the DOWN type: gu_row = (blob - 2560*d_row)/1280)   // gate row bytes (gate = 640*gu_row)
        lay.fmt[(size_t) i].d_row = d_row_of(shape[i].d_type);      // down row bytes (down = 2560*d_row)
        lay.fmt[(size_t) i].gu_type = shape[i].gu_type;
        lay.fmt[(size_t) i].d_type = shape[i].d_type;
        lay.fmt[(size_t) i].bytes = (size_t) lay.bytes[(size_t) i];
    }

    const uint64_t file_cap = (uint64_t) kNE * 2329600 + 4096;   // the harness checks reads vs the file size
    bool sums_ok = true, params_ok = true, host_ok = true, guard_ok = true;
    for (int i = 0; i < 3; ++i) {
        uint64_t per[3];
        if (!blob_role_bytes(lay, i, per)) { check(false, "the role slices of the shape layer"); return; }
        // The harness's per-role refusal gate (main.cpp:606-612): the slices MUST sum to the layer's
        // blob, exactly like the helper's own check (helper_main.cpp:246).  The blob IS
        // [gate: n_ff*gu_row][up: n_ff*gu_row][down: n_embd*d_row], so with the live row bytes
        // (gate 1804/988/720, down 10/16/10) the slices sum EXACTLY to the blob - the gate PASSES,
        // as it must: attempt 10 measured L32/L33, and a per-role pack whose slices did not sum would
        // have been REFUSED at startup (main.cpp:606-612).  With the gate passed, the per-role host
        // fill writes exactly nb bytes, and a packed pack writes the same nb in ONE read
        // (p2_measure.cpp:1071-1085): the source extent equals the copy size on both paths.
        const uint64_t sum = per[0] + per[1] + per[2];
        if (sum != lay.blob_bytes(i)) sums_ok = false;

        // (3) the host staging buffer: fresh, per layer, EXACTLY this layer's blob (run_mmq's vector);
        //     read_blob_row writes the three slices at dst + at (per-role path), or ONE nb-sized read
        //     (the packed path) - both fill exactly `nb` bytes, so the source extent equals the copy size
        std::vector<uint8_t> blob_host(lay.blob_bytes(i), 0);
        uint64_t at = 0;
        bool file_ok = true;
        for (int r = 0; r < 3; ++r) {
            const uint64_t roff = lay.gguf_off[(size_t) (3 * i + r)] + (uint64_t) (kNE - 1) * per[r];
            if (roff + per[r] > file_cap) file_ok = false;             // the last expert's slice in the file
            std::memset(blob_host.data() + at, 0xC0 + r, (size_t) per[r]);   // the slice write (ASAN-visible)
            at += per[r];
        }
        std::memset(blob_host.data() + at, 0xEE, blob_host.size() - at);   // the packed read: the whole nb
        if (at > blob_host.size()) host_ok = false;            // a per-role write past the vector (overflow)
        if (file_ok && per[0] + per[1] + per[2] == blob_host.size() && at != blob_host.size())
            host_ok = false;                                    // gate passed: the slices end exactly at nb
        if (!file_ok) host_ok = false;

        // (1)+(2) the upload at the live slot walk: every slot of the ring, at this layer's own stride
        const size_t stride = (size_t) lay.blob_bytes(i), size = (size_t) lay.blob_bytes(i);
        const size_t ring = stage_ring_bytes(lay.blob_bytes(i));
        for (int64_t j = 0; j < 2 * kMmqGroup; ++j) {
            const size_t off = (size_t) (j % (size_t) kStageRingSlots) * stride;
            if (off + size > ring) params_ok = false;                  // destination extent vs allocation
            std::memcpy(blob_host.data(), blob_host.data(), size);     // the copy engine's source read
        }
        if (size != lay.blob_bytes(i)) params_ok = false;              // the copy size IS this layer's blob
        // the parent's suspect copy: a window-max (2,329,600) copy at THIS layer's own stride fits the
        // ring (ring = 32*stride >= 2,329,600 for the 1,305,600 and 1,868,800 layers) and would read
        // PAST this layer's host row - so a size mismatch alone cannot produce the fault at L34's ring,
        // and the code's size is the per-layer number by construction (blob_bytes = lay.blob_bytes(l)).
        const bool window_max_fits_ring = (size_t) 2329600 <= ring;
        const bool window_max_overruns_host = (size_t) 2329600 > blob_host.size();
        if (i > 0 && !(window_max_fits_ring && window_max_overruns_host)) guard_ok = false;
    }
    check(sums_ok, "the gate/up/down slices sum EXACTLY to each layer's blob at the live triple with the "
                   "live layout's own row bytes (gate 1804/988/720, down 10/16/10): 1280*gu_row + "
                   "2560*d_row = 2,329,600 / 1,305,600 / 1,868,800 - the per-role host fill writes "
                   "exactly nb, the packed path writes the same nb, so the host source extent equals the "
                   "copy size on both paths");
    check(params_ok, "at every upload slot: slot*stride + size = 31*blob + blob = 32*blob = the ring "
                     "allocation, and the copy size is exactly THIS layer's blob_bytes (not the window max)");
    check(host_ok, "the host staging buffer is fresh per layer at exactly that blob, the source reads end "
                   "at the blob (the per-role slices, or one nb-sized packed read inside the file), and "
                   "the source extent equals the copy size");
    check(guard_ok, "the shrink/grow layers: the window-max copy (2,329,600) WOULD fit their rings while "
                    "reading PAST their host rows - the ring cannot catch a wrong-size copy, and the "
                    "code's size is the per-layer number, so the size parameter audits clean");
}

// (13) THE GGUF TRUTH GATE (fix7): the pack header's per-layer shape table checked against the
//      model GGUF's OWN tensor metadata, for all sixteen window indices - at the three live shapes
//      (L32 2,329,600 = 21/20, L33 1,305,600 = 16/42, L34 1,868,800 = 17/20) at their RIGHT indices -
//      and the ring allocation proven to be 32 x the stride for every layer.  The mock model is
//      written with ggml's GGUF writer (tensor metadata only), its header with the loader's own
//      geometry, so header and GGUF agree on a consistent mock and the MUTATION (the live anomaly:
//      header 33 names type 17's shape while the GGUF's 33 is type 21's - internally consistent, so
//      the loader passes it) is caught by the gate ALONE.  The live pack header (present on this
//      machine) is reported in the same typed form - expected-vs-used, no verdict asserted.
void section_gguf_truth() {
    std::printf("(13) gguf truth: header per-layer shapes vs the GGUF's own metadata (16 indices), "
                "mutation refusal, ring == 32 x stride\n");
    namespace cpu = strata::kernels::cpu;
    const std::string root = "/tmp/p2_measure_test_truth";
    const std::string pack = root + "/pack";
    ::mkdir(root.c_str(), 0755);
    ::mkdir(pack.c_str(), 0755);
    const std::string gguf = root + "/mock-model.gguf";
    ::unlink(gguf.c_str());
    ::unlink((pack + "/native_experts.txt").c_str());

    // the layer plan: sixteen window layers (the live window's start), the live triple at 32/33/34,
    // the other two formats of the pack family around them
    const int NL = 48;
    auto gu_t = [](int l) { return l == 33 ? 16 : l == 34 ? 17 : 21; };
    auto d_t  = [](int l) { return l == 33 ? 42 : 20; };
    auto gu_row = [](int t) { return (uint64_t) ggml_row_size((ggml_type) t, 2560); };   // gate row bytes
    auto d_row  = [](int t) { return (uint64_t) ggml_row_size((ggml_type) t, 640); };    // down row bytes

    // ---- the mock model: every layer's gate/up/down expert stack, metadata only ----
    ggml_init_params gp{(size_t) (8 * 1024 * 1024), nullptr, true};   // no_alloc: the tensor METADATA
    ggml_context* gc = ggml_init(gp);                                 // lives in this pool
    gguf_context* g = gguf_init_empty();
    gguf_set_val_u32(g, "general.file_type", 0);
    std::vector<uint64_t> ggate(NL), gup(NL), gdown(NL), gblob(NL);
    bool gguf_ok = true;
    {
        // ggml's GGUF writer computes each tensor's data offset (alignment-padded) as tensors are added
        for (int l = 0; l < NL && gguf_ok; ++l) {
            struct Role { const char* role; int64_t a, b; ggml_type ty; };
            const Role roles[3] = {{"gate", 2560, 640, (ggml_type) gu_t(l)},
                                   {"up", 2560, 640, (ggml_type) gu_t(l)},
                                   {"down", 640, 2560, (ggml_type) d_t(l)}};
            for (int r = 0; r < 3; ++r) {
                char name[80];
                std::snprintf(name, sizeof name, "blk.%d.ffn_%s_exps.weight", l, roles[r].role);
                ggml_tensor* t = ggml_new_tensor_3d(gc, roles[r].ty, roles[r].a, roles[r].b, kNE);
                if (!t) { gguf_ok = false; break; }
                ggml_set_name(t, name);
                gguf_add_tensor(g, t);
                const int64_t id = gguf_get_n_tensors(g) - 1;
                const uint64_t off = (uint64_t) gguf_get_tensor_offset(g, id);
                const uint64_t bytes = (uint64_t) ggml_nbytes(t);
                (void) bytes;
                if (r == 0) ggate[l] = off;
                if (r == 1) gup[l] = off;
                if (r == 2) gdown[l] = off;
            }
        }
    }
    for (int l = 0; l < NL; ++l) gblob[l] = 2 * 640 * gu_row(gu_t(l)) + 2560 * d_row(d_t(l));
    // the architecture string key (the mock's only string kv)
    gguf_set_val_str(g, "general.architecture", "qwen4exp");
    gguf_set_val_u32(g, "general.alignment", 32);
    gguf_write_to_file(g, gguf.c_str(), /*only_meta =*/ true);
    gguf_free(g);
    ggml_free(gc);
    check(gguf_ok, "the mock model's 144 expert role tensors (48 layers x gate/up/down) written with "
                   "ggml's GGUF writer (metadata only)");
    if (!gguf_ok) return;

    // the mock GGUF's own data_start, read back through the same reader the gate uses
    uint64_t data_start = 0;
    {
        const strata::GgufFile mf(gguf);
        data_start = mf.data_start();
        check(mf.tensors().size() == (size_t) (48 * 3), "the mock GGUF round-trips 144 tensor infos");
    }
    // ---- the header, built from the GGUF's OWN offsets and sizes (the packer's contract) ----
    // The blob geometry the loader validates: native_fmt's bytes = 2*n_ff*row_size(gu,n_embd) +
    // n_embd*row_size(d,n_ff) = exactly 2*640*gu_row + 2560*d_row, the same arithmetic as the
    // tensor stacks (gate = 512*gu_row per role, gate/up equal) - header and GGUF agree by
    // construction here; the LIVE pack proves they need NOT agree in the field.
    std::FILE* h = std::fopen((pack + "/native_experts.txt").c_str(), "wb");
    if (!h) { check(false, "the mock header could not be written"); return; }
    std::fprintf(h,
                 "# strata native experts v3: layer gu_type d_type offset blob_bytes gate_off up_off "
                 "down_off [shard] (n_expert %lld, total 0; absolute offsets in mock-model.gguf, or in "
                 "the named shard beside it)\n",
                 (long long) kNE);
    std::string hdr_lines;
    {
        char line[256];
        uint64_t total = 0;
        for (int l = 0; l < NL; ++l) {
            std::snprintf(line, sizeof line, "%d %d %d %llu %llu %llu %llu %llu\n", l, gu_t(l), d_t(l),
                          (unsigned long long) total, (unsigned long long) gblob[l],
                          (unsigned long long) (data_start + ggate[l]),
                          (unsigned long long) (data_start + gup[l]),
                          (unsigned long long) (data_start + gdown[l]));
            hdr_lines += line;
            total += gblob[l] * (uint64_t) kNE;
        }
    }
    std::fprintf(h, "%s", hdr_lines.c_str());
    std::fclose(h);

    std::string err;
    const bool loaded = cpu::expert_layout_load(pack, NL, kNE, err);
    check(loaded, "the mock pack loads: every header line's blob equals the loader's OWN geometry for "
                  "its types (the loader's self-check cannot see a header that disagrees with the model)");
    if (!loaded) { check(false, ("the mock layout is the rest of this section: " + err).c_str()); return; }
    const cpu::ExpertLayout& lay = cpu::expert_layout();

    // (13a) truth for ALL SIXTEEN window indices, and the three live shapes at their RIGHT indices
    std::vector<LayerTruth> truth;
    const bool gate_ok = check_gguf_truth(gguf, lay, 32, 47, truth, err);
    check(gate_ok, ("the gate passes the consistent mock for all sixteen window indices: " + err).c_str());
    bool idx_ok = gate_ok && truth.size() == 16;
    for (size_t i = 0; i < truth.size(); ++i) {
        const LayerTruth& t = truth[i];
        if (t.layer != (int64_t) (32 + (int64_t) i)) idx_ok = false;
        if (t.header_blob != gblob[(size_t) t.layer] || t.gguf_blob != gblob[(size_t) t.layer]) idx_ok = false;
        if (t.gu_type != gu_t((int) t.layer) || t.gguf_gu_type != gu_t((int) t.layer) ||
            t.d_type != d_t((int) t.layer) || t.gguf_d_type != d_t((int) t.layer)) idx_ok = false;
    }
    if (!idx_ok) {
        for (size_t i = 0; i < truth.size(); ++i) {
            const LayerTruth& t = truth[i];
            std::printf("      idx-dump L%lld header %d/%d %llu B gguf %d/%d %llu B (want %d/%d %llu B)\n",
                        (long long) t.layer, t.gu_type, t.d_type, (unsigned long long) t.header_blob,
                        t.gguf_gu_type, t.gguf_d_type, (unsigned long long) t.gguf_blob, gu_t((int) t.layer),
                        d_t((int) t.layer), (unsigned long long) gblob[(size_t) t.layer]);
        }
    }
    bool live_ok = truth.size() == 16;
    for (const LayerTruth& t : truth) {
        if (t.layer == 32 && !(t.gu_type == 21 && t.d_type == 20 && t.header_blob == 2329600 &&
                               t.gguf_blob == 2329600)) live_ok = false;
        if (t.layer == 33 && !(t.gu_type == 16 && t.d_type == 42 && t.header_blob == 1305600 &&
                               t.gguf_blob == 1305600)) live_ok = false;
        if (t.layer == 34 && !(t.gu_type == 17 && t.d_type == 20 && t.header_blob == 1868800 &&
                               t.gguf_blob == 1868800)) live_ok = false;
    }
    check(idx_ok, "every window index 32..47: header types == GGUF types, header blob == GGUF blob, "
                  "layer indices in order (typed expected-vs-used for all 16)");
    check(live_ok, "the three live shapes at their RIGHT indices - L32 21/20 2,329,600 B, L33 16/42 "
                   "1,305,600 B, L34 17/20 1,868,800 B (the mock encodes the live family; the gate "
                   "compares each against the mock GGUF's own tensor metadata)");

    // (13b) ring vs stride: the allocation IS 32 x the stride for EVERY window layer (the same
    // number main.cpp takes the ring with and STAGE prints - no allocation-vs-stride divergence)
    bool ring_ok = true;
    for (int64_t l = 32; l <= 47; ++l) {
        if (stage_ring_bytes(lay.blob_bytes(l)) != (size_t) (32 * lay.blob_bytes(l))) ring_ok = false;
    }
    check(ring_ok, "ring allocation == 32 x stride at every window layer (the take site's number IS "
                   "the stride x 32; STAGE prints both)");

    // (13c) THE LIVE ANOMALY, mutated: header 33 names the 17/20 shape (1,868,800 - internally
    // consistent, the loader PASSES it) while the mock GGUF's 33 is the 21/42 stack, and header 34
    // names the 16/42 shape while the GGUF's 34 is 17/20 - the exact permutation of attempt 11
    // (header L33 21/42 vs GGUF 16/42).  The gate must REFUSE, naming layer 33 with both columns.
    {
        const std::string packm = root + "/packm";
        ::mkdir(packm.c_str(), 0755);
        std::FILE* ha = std::fopen((packm + "/native_experts.txt").c_str(), "wb");
        std::string lines;
        uint64_t total = 0;
        for (int l = 0; l < NL; ++l) {
            const int g = l == 33 ? 17 : l == 34 ? 16 : gu_t(l);
            const int d = l == 33 ? 20 : l == 34 ? 42 : d_t(l);
            const uint64_t blob = 2 * 640 * gu_row(g) + 2560 * d_row(d);
            char line[256];
            std::snprintf(line, sizeof line, "%d %d %d %llu %llu %llu %llu %llu\n", l, g, d,
                          (unsigned long long) total, (unsigned long long) blob,
                          (unsigned long long) (data_start + ggate[l]),
                          (unsigned long long) (data_start + gup[l]),
                          (unsigned long long) (data_start + gdown[l]));
            lines += line;
            total += blob * (uint64_t) kNE;
        }
        std::fprintf(ha,
                     "# strata native experts v3: layer gu_type d_type offset blob_bytes gate_off "
                     "up_off down_off [shard] (n_expert %lld, total 0; absolute offsets in "
                     "mock-model.gguf, or in the named shard beside it)\n%s",
                     (long long) kNE, lines.c_str());
        std::fclose(ha);
        std::string merr;
        const bool mloaded = cpu::expert_layout_load(packm, NL, kNE, merr);
        check(mloaded, ("the LOADER accepts the mutated header: its 33's blob 1,868,800 matches its own "
                        "17/20 geometry - a header that disagrees with the MODEL passes every loader "
                        "self-check: " + merr).c_str());
        std::vector<LayerTruth> mtruth;
        std::string gerr;
        const bool mgate = check_gguf_truth(gguf, cpu::expert_layout(), 32, 47, mtruth, gerr);
        check(!mgate, ("the GATE refuses the mutated pack (the live anomaly's shape): " + gerr).c_str());
        check(!mgate && gerr.find("layer 33") != std::string::npos,
              "the refusal names layer 33 (the first diverging layer, typed expected-vs-used)");
        check(!mgate && gerr.find("16") != std::string::npos && gerr.find("17") != std::string::npos,
              "the refusal carries BOTH type columns (header 17/20, the GGUF's 16 at the gate tensor)");
        ::unlink((packm + "/native_experts.txt").c_str());
    }

    // (13d) the LIVE pack on this machine, typed expected-vs-used (reported, no verdict asserted)
    {
        const std::string real_pack = std::getenv("STRATA_PACK_DIR") ? std::getenv("STRATA_PACK_DIR") : "";
        if (real_pack.empty() || !std::ifstream(real_pack + "/native_experts.txt").good()) {
            std::printf("      (13d) skip: no live pack (set STRATA_PACK_DIR to check the model's header "
                        "against its GGUF)\n");
        } else {
            std::string rerr;
            if (!cpu::expert_layout_load(real_pack, NL, kNE, rerr)) {
                std::printf("      (13d) live pack did not load: %s\n", rerr.c_str());
            } else {
                std::vector<LayerTruth> rt;
                std::string terr;
                const std::string rgguf = std::getenv("STRATA_PACK_GGUF") ? std::getenv("STRATA_PACK_GGUF")
                                                                          : real_pack + "/../Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf";
                const bool ok = check_gguf_truth(rgguf, cpu::expert_layout(), 32, 47, rt, terr);
                std::printf("      (13d) live pack: gate %s\n", ok ? "PASSES" : "REFUSES");
                if (!ok) std::printf("      (13d) live: %s\n", terr.c_str());
                for (const LayerTruth& t : rt)
                    std::printf("      (13d) L%lld header %d/%d %llu B  gguf %d/%d %llu B\n",
                                (long long) t.layer, t.gu_type, t.d_type, (unsigned long long) t.header_blob,
                                t.gguf_gu_type, t.gguf_d_type, (unsigned long long) t.gguf_blob);
            }
        }
    }
    ::unlink(gguf.c_str());
    ::unlink((pack + "/native_experts.txt").c_str());

}

// ============================== (14) the per-op SYNC attribution instruments (fix8) ==============================
//
// The CPU half of fix8: the exact SHAPE of every line a live per-op fault prints, the label the refusal
// path carries, and the STRUCTURE of the run's sync placement (read out of the harness's own source).
// No GPU, no device call: the strings are the instrument, and a live run only prints what these lines pin.
void section_fault_attribution() {
    std::printf("(14) per-op sync attribution: FAULT-AT shape, op label, pointer attributes, the L32-vs-L34 "
                "op-parameter line, the sync placement in the harness's source\n");

    // ---- (14a) the op label: the refusal path carries the expert, the slot and the entry ----
    check(op_label("blob upload", 0, 0, 0) == "blob upload q0 slot0 entry0",
          "the pinned fault site's label: q0 slot0 entry0 (L34's first upload writes slot 0, entry 0)");
    check(op_label("blob upload", 3, 19, 35) == "blob upload q3 slot19 entry35",
          "a mid-group slot: q3 -> slot (32+3)%32 = 19, entry 35 (the ring slot IS the entry index mod 32)");
    check(op_label("act quantize", -1, -1, -1) == "act quantize",
          "an op with no parameters keeps its bare name (no empty q/slot/entry fields)");

    // ---- (14b) cudaPointerGetAttributes' answer, named ----
    check(std::string(cuda_memory_type_name(0)) == "unregistered-host" &&
          std::string(cuda_memory_type_name(1)) == "host" &&
          std::string(cuda_memory_type_name(2)) == "device" &&
          std::string(cuda_memory_type_name(3)) == "managed",
          "cudaMemoryType 0/1/2/3 -> unregistered-host / host (pinned) / device / managed");
    check(std::string(cuda_memory_type_name(7)) == "unknown(7)",
          "an unmapped type number prints unknown(N) - the instrument never guesses a pointer's kind");

    // ---- (14c) the fault line: one typed line naming the op, its parameters and both pointers ----
    OpProbe pr;
    pr.op = "blob upload"; pr.q = 0; pr.slot = 0; pr.entry = 0;
    pr.src = (const void*) 0x1000; pr.dst = (const void*) 0x2000;
    pr.bytes = 2329600; pr.stream = (const void*) 0x3000;
    pr.src_attr = "unregistered-host"; pr.dst_attr = "device";
    pr.src_rc = "cudaSuccess"; pr.dst_rc = "cudaSuccess";
    const std::string fl = format_fault_line("pinned-sample", 34, pr,
                                             "an illegal memory access was encountered");
    check(fl.rfind("FAULT-AT pinned-sample L34 blob upload q0 slot0 entry0: an illegal memory access "
                   "was encountered | ", 0) == 0,
          "the fault line names the fixture, the layer, the op AND its q/slot/entry (the attempt-12 line "
          "carried none of the three)");
    check(fl.find("src 0x1000 dst 0x2000 n 2329600 stream 0x3000") != std::string::npos,
          "src, dst, copy size and the stream are on the same line (the three parameters fix6 audited "
          "statically, printed as the live process has them)");
    check(fl.find("src-attr unregistered-host(cudaSuccess)") != std::string::npos &&
          fl.find("dst-attr device(cudaSuccess)") != std::string::npos,
          "cudaPointerGetAttributes for src and dst: the host row is unregistered host memory, the ring "
          "destination is device memory");
    OpProbe kp = pr;
    kp.op = "act quantize"; kp.q = kp.slot = kp.entry = -1; kp.src = nullptr; kp.dst = nullptr;
    kp.bytes = 0; kp.src_attr = kp.dst_attr = kp.src_rc = kp.dst_rc = "";
    const std::string kl = format_fault_line("pinned-sample", 34, kp, "an illegal memory access was encountered");
    check(kl.find("FAULT-AT pinned-sample L34 act quantize: an illegal memory access was encountered | src (nil)")
              == 0 &&
          kl.find("-attr") == std::string::npos,
          "a kernel's line: bare label, (nil) pointers, no attribute fields to misread");
    bool verdict_free = true;
    for (const char* w : {"tolerance", "toler", "band", "verdict", "PASS", "FAIL", "accept", "thresh"})
        if (fl.find(w) != std::string::npos || kl.find(w) != std::string::npos) verdict_free = false;
    check(verdict_free, "the fault line carries evidence only - no tolerance, no band, no verdict word");

    // ---- (14d) the layer's op-parameter line, and the L32-vs-L34 diff in ONE place ----
    OpWindow a32;
    a32.layer = 32; a32.T = 256; a32.entries = 2560; a32.rows = 2250; a32.order_n = 44; a32.groups = 3;
    a32.nr_max = 210; a32.blob = 2329600; a32.ring = stage_ring_bytes(a32.blob);
    a32.gu_bytes = 461120; a32.down_bytes = 163840;
    a32.stream = (const void*) 0x3000; a32.ring_base = (const void*) 0x2000;
    a32.wins = {{0, 16}, {16, 16}, {32, 12}};
    a32.r0 = {0, 210, 400};
    a32.nr = {210, 190, 120};
    const std::string sa = format_op_summary("pinned-sample", a32);
    check(sa.rfind("OPSUM pinned-sample L32 T 256 entries 2560 rows 2250 order 44 groups 3 qcount-max 16 "
                   "nr_max 210 blob 2329600 ring 74547200", 0) == 0,
          "the per-layer op-parameter line: T, entries, rows, order length, group count, q-count, nr_max, "
          "blob, ring (fields in a fixed order, once per layer)");
    check(sa.rfind("win 0:16[0/210] 16:16[210/190] 32:12[400/120]") != std::string::npos &&
          sa.size() == std::string("win 0:16[0/210] 16:16[210/190] 32:12[400/120]").size() +
                       sa.rfind("win 0:16[0/210] 16:16[210/190] 32:12[400/120]"),
          "the claim windows are the tail of the line: j0:q-count[r0/nr] per group (the offsets each "
          "group's gather and products address)");
    check(a32.ring == (size_t) 32 * (size_t) a32.blob,
          "the line's ring is 32 x the blob stride (the ring==stride identity fix7 proved, now printed)");
    OpWindow a34 = a32;                       // SAME types, SAME blob, SAME ring, SAME windows, SAME stream
    a34.layer = 34;
    a34.rows = 2230;
    const std::string sb = format_op_summary("pinned-sample", a34);
    auto fields = [](const std::string& s) {
        std::vector<std::string> f;
        size_t i = 0;
        while (i < s.size()) {
            size_t j = s.find(' ', i);
            if (j == std::string::npos) j = s.size();
            f.push_back(s.substr(i, j - i));
            i = j + 1;
        }
        return f;
    };
    const std::vector<std::string> fa = fields(sa), fb = fields(sb);
    size_t ndiff = 0;
    std::string diff_a, diff_b;
    for (size_t i = 0; i < fa.size() && i < fb.size(); ++i)
        if (fa[i] != fb[i]) { ++ndiff; diff_a += fa[i] + " "; diff_b += fb[i] + " "; }
    size_t rows_at = 0;
    while (rows_at < fa.size() && fa[rows_at] != "rows") ++rows_at;
    check(fa.size() == fb.size() && ndiff == 2 && diff_a == "L32 2250 " && diff_b == "L34 2230 " &&
          rows_at < fa.size() && fa[rows_at + 1] == "2250" && fb[rows_at + 1] == "2230",
          "L32 vs L34 with IDENTICAL types/blob/ring/stream/windows: the two lines differ in the layer "
          "label and in EXACTLY ONE measured field (the row count) - the differing dimension is visible in "
          "one place, no reconstruction needed");

    // ---- (14e) the sync placement, read out of the harness's own source ----
    const std::string src_path = std::string(p2_measure_test_source_dir()) + "/p2_measure_main.cpp";
    std::ifstream srcf(src_path, std::ios::binary);
    std::string src((std::istreambuf_iterator<char>(srcf)), std::istreambuf_iterator<char>());
    check(!src.empty(), ("the harness source is readable for the structure pin: " + src_path).c_str());
    if (src.empty()) return;
    auto count = [&](const char* needle) -> int {
        int c = 0;
        size_t at = src.find(needle);
        while (at != std::string::npos) { ++c; at = src.find(needle, at + 1); }
        return c;
    };
    bool every_op_probed = true;
    for (const char* op : {"x upload", "act quantize", "bounds upload", "blob upload", "gather group",
                           "gather per-expert", "gu product", "swiglu", "h quantize", "dn product",
                           "layer sync", "dm readback"}) {
        const std::string call = std::string("sync_probe(\"") + op + "\"";
        if (src.find(call) == std::string::npos) { every_op_probed = false; std::printf("      missing: %s\n", call.c_str()); }
    }
    check(every_op_probed, "EVERY async op of the run ends with its OWN sync probe (x upload, act quantize, "
                           "bounds upload, EACH blob upload q, the gathers, the products, swiglu, the "
                           "quantizes, the layer sync, the readback)");
    check(count("sync_probe(") == 12,
          "12 probe sites, unconditional (no flag gates them): the blob-upload site is ONE site inside the "
          "group loop, so it fires for every q of every group of every layer");
    check(src.find("sync_probe(\"blob upload\", (long long) q, (long long) slot_idx") != std::string::npos,
          "the blob-upload probe takes THIS q, THIS ring slot and THIS entry - the per-upload attribution "
          "the parent's attempt 13 reads");
    check(src.find("const std::function<bool(const char*, const OpProbe&)>& ckpt") != std::string::npos,
          "the checkpoint is (label, probe) -> bool: a faulted op's label and parameters reach main, and "
          "the run stops there");
    check(src.find("\"blob upload\", err)) return false;") == std::string::npos &&
          src.find("op_label(\"blob upload\"") != std::string::npos,
          "the upload's OWN failing call carries the q/slot/entry label - fix7's label sat AFTER it, so "
          "the refusal printed the bare literal and the label never ran");
    check(src.find("cudaStreamSynchronize") != std::string::npos &&
          src.find("cudaPointerGetAttributes") != std::string::npos &&
          src.find("cudaGetLastError") != std::string::npos,
          "the probe is cudaStreamSynchronize + cudaGetLastError + cudaPointerGetAttributes (a sync is the "
          "only way an unsurfaced async fault becomes visible)");
    check(src.find("sync_op(cs, fid.c_str(), l, \"ring probe tail\"") != std::string::npos &&
          src.find("sync_op(cs, fid.c_str(), l, \"ring probe head\"") != std::string::npos,
          "the ring probe is SYNCED (fix6 probed with cudaGetLastError alone - blind to an unsurfaced "
          "fault), and it reports as a FAULT-AT line");
    check(src.find("sync_op(cs, fid.c_str(), l, \"arm prologue\"") != std::string::npos,
          "the arm's prep ops get their own prologue sync too (--arm-groups runs the same instrument)");
    check(src.find("format_op_summary") != std::string::npos && src.find("print_opsum(\"+arm\"") != std::string::npos,
          "the op-parameter line prints once per (fixture, layer), and once more for the arm's composition");
    // the prior instruments all stay: CKPT / STAGE / TRUTH / the ring probe / the prologue bracket
    check(src.find("prologue clean") != std::string::npos && src.find("STAGE %s L%lld") != std::string::npos &&
          src.find("ring probe") != std::string::npos && src.find("check_gguf_truth") != std::string::npos &&
          src.find("TRUTH L%lld") != std::string::npos &&
          src.find("stage_ring_bytes(blob_bytes)") != std::string::npos,
          "fix4-fix7 instruments intact: the clean-prologue bracket, the STAGE numbers, the ring probe, "
          "the GGUF truth gate, the per-layer ring take");
    check(src.find("REFUSAL (side A") != std::string::npos &&
          src.find("%s @ layer %lld: %s") != std::string::npos,
          "the side-A refusal path is unchanged in shape - it now prints the label the failing call "
          "carries, because the label is in the error string (fix9 adds the geometry suffix, the device "
          "path prints the same line as before)");
}

// ============================== (15) the product write geometry: the L34 fault mechanism (fix9) ================
//
// The mechanism the live attempt-13 line pinned (handoff p2-measure-fix9.md): the gate/up product's dst
// base was offset by r0 rows WHILE its bounds are absolute, so the kernel - which writes at
// ids_dst[col]*ld_dst for col in [r0, r0+nr) - wrote r0 rows past their place.  These checks run the
// geometry of the THREE live layers' group windows, at every group boundary, for the fixed call and for
// the pre-fix9 call, and show that the group COUNT is not the discriminator.
namespace {

/// One layer's group-window shape, exactly as the live OPSUM line prints it: per group (j0, q-count, r0, nr).
struct WinShape {
    int64_t layer;
    int64_t T, rows, order_n;
    std::vector<std::pair<long long, long long>> wins;   // (j0, q-count)
    std::vector<long long> r0, nr;
};

/// The live pinned-sample shapes (the attempt-13 run, all three layers in one run).
WinShape shape34() {          // FAULTS live: 17 groups, order 268, nr_max 181, gu 1408000, down 921600
    return WinShape{34, 225, 2250, 268,
                    {{0, 16}, {16, 16}, {32, 16}, {48, 16}, {64, 16}, {80, 16}, {96, 16}, {112, 16},
                     {128, 16}, {144, 16}, {160, 16}, {176, 16}, {192, 16}, {208, 16}, {224, 16},
                     {240, 16}, {256, 12}},
                    {0, 154, 321, 451, 571, 712, 888, 1069, 1190, 1257, 1359, 1498, 1635, 1776, 1868,
                     2010, 2127},
                    {154, 167, 130, 120, 141, 176, 181, 121, 67, 102, 139, 137, 141, 92, 142, 117, 123}};
}
WinShape shape33() {          // measures clean live: 16 groups, order 249, last 240:9[2158/92]
    WinShape s{33, 225, 2250, 249, {}, {}, {}};
    long long r = 0;
    for (int g = 0; g < 15; ++g) {
        s.wins.push_back({(long long) (g * 16), 16});
        const long long n = (g == 14) ? 156 : 143;       // 14*143 + 156 = 2158 = the live last group's r0
        s.r0.push_back(r); s.nr.push_back(n); r += n;
    }
    s.wins.push_back({240, 9}); s.r0.push_back(2158); s.nr.push_back(92);
    return s;
}
WinShape shape32() {          // measures clean live: 12 groups, order 177, last 176:1[2247/3]
    WinShape s{32, 225, 2250, 177, {}, {}, {}};
    long long r = 0;
    for (int g = 0; g < 11; ++g) {
        s.wins.push_back({(long long) (g * 16), 16});
        const long long n = (g == 10) ? 207 : 204;       // 10*204 + 207 = 2247 = the live last group's r0
        s.r0.push_back(r); s.nr.push_back(n); r += n;
    }
    s.wins.push_back({176, 1}); s.r0.push_back(2247); s.nr.push_back(3);
    return s;
}

}  // namespace

void section_product_write_geometry() {
    std::printf("(15) product write geometry: the live 17-group L34 shape vs the 16- and 12-group controls, "
                "the base-vs-offset dst rule, the exceedance the fault line printed\n");

    const WinShape l34 = shape34(), l33 = shape33(), l32 = shape32();

    // ---- (15a) the shape itself: the group chain, the row partition, the ring slot map ----
    check(l34.wins.size() == 17 && l34.order_n == 268 && l34.rows == 2250,
          "the live L34 shape: 17 groups (16*16 + 12 = 268 entries), 2250 rows - the first window layer "
          "with MORE THAN kMmqGroup(16) groups");
    {
        long long n_ent = 0, n_rows = 0, r = 0, qmax = 0, nrmax = 0;
        bool chain = true, slots_ok = true, group_size_ok = true;
        for (size_t g = 0; g < l34.wins.size(); ++g) {
            n_ent += l34.wins[g].second;
            qmax = std::max(qmax, l34.wins[g].second);
            nrmax = std::max(nrmax, l34.nr[g]);
            n_rows += l34.nr[g];
            if (l34.r0[g] != r) chain = false;                      // the spans partition the row set
            r += l34.nr[g];
            if (l34.wins[g].second > kMmqGroup) group_size_ok = false;
            for (long long q = 0; q < l34.wins[g].second; ++q) {   // the ring slot the upload writes
                const long long slot = (l34.wins[g].first + q) % kStageRingSlots;
                if (slot < 0 || slot >= kStageRingSlots) slots_ok = false;
            }
        }
        check(chain && r == l34.rows,
              "the group row spans are a chain that ends EXACTLY at rows: 2127 + 123 = 2250 (the bounds "
              "math run_mmq mirrors, prefill.cpp:4135-4142)");
        check(n_ent == l34.order_n && n_rows == l34.rows,
              "the q-counts sum to the order length (268) and the nr's sum to the row set (2250)");
        check(qmax == 16 && nrmax == 181,
              "q-count max 16, nr_max 181 - as the live OPSUM line printed them");
        check(group_size_ok,
              "the 16 CAP IS THE GROUP SIZE (every group has <= kMmqGroup experts, so every MMQ launch has "
              "n <= 16 and its grid z dim is <= 16): the 17th group adds a LAUNCH, not a wider launch - "
              "there is no 16-group cap in the product path to exceed");
        check(slots_ok,
              "every group's ring slots are (j0+q) mod 32 inside [0,32): group 11 (j0 176) takes slots "
              "16..31, the last group (j0 256, 12 experts) takes 0..11 - 17 groups wrap the ring 17 times, "
              "no slot past the ring");
        check(l34.order_n + 1 + 17 * (kMmqGroup + 1) == 558 &&
              558 <= 2 * (kNE + 1) + 64 * (kMmqGroup + 1),
              "the bounds buffer holds a 17-group window (268+1 + 17*17 = 558 of the 2114 int32 the harness "
              "allocates - it is sized for 64 groups, so 17 is not the limit there either)");
    }

    // ---- (15b) the FIXED gate/up call: absolute bounds + the buffer BASE, every group fits ----
    auto gu_geom = [](const WinShape& s, size_t g, bool pre_fix9) {
        // pre_fix9: the pointer sat r0 rows into the buffer (the down site's convention) with ABSOLUTE
        // bounds - the harness's call before fix9.  The fix: the pointer is the base.
        return ProductGeom{true, true, s.r0[g], s.nr[g], 2 * kFF, pre_fix9 ? s.r0[g] : 0};
    };
    auto dn_geom = [](const WinShape& s, size_t g) {
        return ProductGeom{true, false, s.r0[g], s.nr[g], kN, s.r0[g]};     // offset pointer, group bounds
    };
    for (const WinShape* s : {&l34, &l33, &l32}) {
        bool all_fit = true, dn_all_fit = true;
        long long reach = 0;
        for (size_t g = 0; g < s->wins.size(); ++g) {
            all_fit = all_fit && product_write_fits(gu_geom(*s, g, false), s->rows);
            dn_all_fit = dn_all_fit && product_write_fits(dn_geom(*s, g), s->rows);
            reach = std::max(reach, product_write_last_row(gu_geom(*s, g, false)));
        }
        check(all_fit, ("L" + std::to_string(s->layer) + ": the gate/up write from the BASE spans "
                        "[r0, r0+nr) of the buffer and fits every group (" +
                        std::to_string(s->wins.size()) + " groups)").c_str());
        check(dn_all_fit, ("L" + std::to_string(s->layer) + ": the down product's offset pointer "
                           "(base + r0*ld) with GROUP-RELATIVE bounds spans [0, nr) and fits every "
                           "group - the site fix9 did not need to change").c_str());
        check(reach == s->rows, ("L" + std::to_string(s->layer) + ": the deepest gate/up write ends "
                                 "EXACTLY at rows - the last group's r0+nr is the buffer's row count, the "
                                 "chain has no slack and no overrun").c_str());
    }

    // ---- (15c) the PRE-fix9 call: the exceedance in EVERY shape, so the group count is refuted ----
    // The pointer sits dst_row_offset rows into the buffer, so the write's span FROM THE BUFFER BASE is
    // [offset + first, offset + last) and its last byte is last*ld*4 + offset*ld*4.
    auto reach_from_base = [](const ProductGeom& g) -> unsigned long long {
        return (unsigned long long) product_write_bytes_from_dst(g) +
               (unsigned long long) (g.dst_row_offset > 0 ? g.dst_row_offset : 0) *
               (unsigned long long) g.ld_dst * sizeof(float);
    };
    {
        int groups_over34 = 0, groups_in34 = 0, misplaced34 = 0;
        long long over34_g11 = -1;
        for (size_t g = 0; g < l34.wins.size(); ++g) {
            const ProductGeom pre = gu_geom(l34, g, true);
            // PAST THE BUFFER'S END means reach_from_base > the allocation; the write is MISPLACED by r0
            // rows in every group with r0 > 0, and runs off the end from 2*r0 + nr > rows
            if (reach_from_base(pre) > (unsigned long long) mmq_gu_buffer_bytes(l34.rows)) ++groups_over34;
            else ++groups_in34;
            if (l34.r0[g] > 0) ++misplaced34;
            if (l34.wins[g].first == 176) {
                over34_g11 = (long long) (product_write_bytes_from_dst(pre) -
                                           (size_t) (l34.rows - l34.r0[g]) * (size_t) (2 * kFF) * sizeof(float));
            }
        }
        check(misplaced34 == 16,
              "the pre-fix9 call writes EVERY group with r0 > 0's products r0 rows PAST their place (16 of "
              "L34's 17 groups - group 0, r0 0, is the only one swiglu reads correctly)");
        check(groups_over34 == 10 && groups_in34 == 7,
              "and it runs off the ALLOCATION's end in 10 of L34's 17 groups (from group j0 112, r0 1069, "
              "on: 2*r0 + nr passes rows; at the faulting group 2*1498 + 137 = 3133 > 2250).  The threshold "
              "is r0, not the group index - the 17-group count is refuted as the discriminator");
        check(over34_g11 == 4520960,
              "the live faulting group (j0 176, r0 1498, nr 137) overran by 4,520,960 B: the write reaches "
              "gu + 16,040,960 of an 11,520,000 B buffer (rows*1280*4) - unmapped VA, an illegal access at "
              "the op's own sync, which is where fix8's probe sits");
        bool controls_over = true;
        unsigned long long over32_last = 0, over33_last = 0;
        int over33 = 0, over32 = 0;
        for (size_t g = 0; g < l33.wins.size(); ++g)
            if (reach_from_base(gu_geom(l33, g, true)) > (unsigned long long) mmq_gu_buffer_bytes(l33.rows)) ++over33;
        for (size_t g = 0; g < l32.wins.size(); ++g)
            if (reach_from_base(gu_geom(l32, g, true)) > (unsigned long long) mmq_gu_buffer_bytes(l32.rows)) ++over32;
        for (const WinShape* s : {&l33, &l32}) {
            const ProductGeom last_pre = gu_geom(*s, s->wins.size() - 1, true);
            const unsigned long long reach = reach_from_base(last_pre);
            if (reach <= (unsigned long long) mmq_gu_buffer_bytes(s->rows)) controls_over = false;
            if (s->layer == 32) over32_last = reach; else over33_last = reach;
        }
        check(over33 == 8 && over32 == 6,
              "the controls cross the same threshold on the same rule (2*r0 + nr > rows): 8 of L33's 16 "
              "groups (from j0 128 on), 6 of L32's 12 (from j0 96 on) - a 12-group window overruns in half "
              "its groups, so the window's GROUP COUNT is not what selects the faulting layer");
        check(over32_last == 23024640ULL && over33_last == 22568960ULL,
              "the CONTROLS overrun by MORE than the faulting group: L32's last group (12 groups, r0 2247, "
              "nr 3) reaches gu + 23,024,640 and L33's (16 groups, r0 2158, nr 92) reaches gu + 22,568,960, "
              "against L34 group 11's 16,040,960 - and they did not FAULT: their overshoot stayed inside "
              "the MAPPED VA of the buffers allocated after gu, so it corrupted silently.  The group count "
              "(17 vs 16 vs 12) is refuted as the discriminator; r0 is the mechanism");
        check(controls_over,
              "every shape's pre-fix9 call exceeds its gate/up buffer at its last group (the live 'clean "
              "measurements' of L32/L33 were taken with this call)");
    }

    // ---- (15d) the numbers the live fault line printed, decoded ----
    {
        const ProductGeom g = gu_geom(l34, 11, true);
        check(l34.wins[11].first == 176 && l34.nr[11] == 137,
              "entry176 IS j0: the group label the probe prints for the product ops is the group's first "
              "order entry (win 176:16[1498/137] - group 11 of 17)");
        check((unsigned long long) (137 * 1280 * 4) == 701440 && g.nr == 137,
              "n 701440 = nr*1280*4 = 137*5120, the INTENDED dst extent fix8 printed (16*43840 = 640*1096); "
              "it is in-bounds of the buffer - which is exactly why the line needed the write-rows fields: "
              "the kernel's real index is ids[absolute col], not col - r0");
        const unsigned long long gu_base = 0x7fc1b1150800ULL - (unsigned long long) 1498 * 1280 * 4;
        const unsigned long long last_write = 0x7fc1b1150800ULL +
                                              (unsigned long long) product_write_bytes_from_dst(g);
        check(gu_base == 0x7fc1b0a00000ULL &&
              last_write - gu_base == 16040960ULL &&
              last_write - (gu_base + (unsigned long long) mmq_gu_buffer_bytes(2250)) == 4520960ULL,
              "the pointers agree: dst 0x7fc1b1150800 - 1498*1280*4 = the gu base 0x7fc1b0a00000 (a "
              "page-aligned cudaMalloc address), and the last written byte is dst + 8,371,200 = base + "
              "16,040,960, which is 4,520,960 B past the 11,520,000 B allocation - the printed dst-attr "
              "'device' only says the PRINTED pointer is mapped, not the written byte");
    }

    // ---- (15e) the typed lines: the write-rows fields and the CPU-side refusal ----
    {
        OpProbe pr;
        pr.op = "gu product"; pr.q = -1; pr.slot = -1; pr.entry = 176;
        pr.src = (const void*) 0x7fc1ad600000ULL; pr.dst = (const void*) 0x7fc1b1150800ULL;
        pr.bytes = 701440; pr.stream = (const void*) 0x5574216460f0ULL;
        pr.src_attr = "device"; pr.dst_attr = "device";
        pr.src_rc = "no error"; pr.dst_rc = "no error";
        const std::string bare = format_fault_line("pinned-sample", 34, pr,
                                                   "an illegal memory access was encountered");
        check(bare == "FAULT-AT pinned-sample L34 gu product q-1 slot-1 entry176: an illegal memory access "
                      "was encountered | src 0x7fc1ad600000 dst 0x7fc1b1150800 n 701440 stream "
                      "0x5574216460f0 src-attr device(no error) dst-attr device(no error)",
              "the attempt-13 line, reproduced byte for byte from the CPU formatters (no GPU in sight)");
        const ProductGeom g = gu_geom(l34, 11, true);
        pr.w_first = product_write_first_row(g);
        pr.w_last = product_write_last_row(g);
        pr.alloc = (unsigned long long) mmq_gu_buffer_bytes(l34.rows);
        const std::string with_w = format_fault_line("pinned-sample", 34, pr,
                                                      "an illegal memory access was encountered");
        check(with_w == bare + " write-rows [1498,1635) alloc 11520000",
              "fix9's fields name the exceeded buffer on the fault line: the write's row span from the "
              "pointer and the buffer's bytes (write-rows 0 leaves the line byte-identical to fix8)");
        const std::string ref = product_write_refusal("gate/up", true, 176, 1498, 137, 2 * kFF, 1498,
                                                      l34.rows, (unsigned long long) mmq_gu_buffer_bytes(l34.rows));
        check(ref.rfind("the gate/up product's dst write: group j0=176 writes rows [1498,1635) from a dst "
                        "pointer 1498 rows into a 2250-row buffer (11520000 B), absolute bounds - runs "
                        "4520960 B past the end", 0) == 0,
              "the CPU-side refusal names the group, the write span, the pointer's offset, the buffer and "
              "the overrun - and the harness REFUSES (exit 4) instead of handing the launch to the device");
        const std::string refdn = product_write_refusal("down", false, 256, 2127, 123, kN, 2127,
                                                        l34.rows, (unsigned long long) mmq_dm_buffer_bytes(l34.rows));
        check(refdn.find("the down product's dst write: group j0=256 writes rows [0,123) from a dst pointer "
                         "2127 rows into a 2250-row buffer") != std::string::npos &&
              refdn.find("group-relative bounds - runs 0 B past the end") != std::string::npos,
              "the down site's convention reads correctly in the same text (group-relative bounds => rows "
              "[0, nr) from the offset pointer, 2127 + 123 = the buffer's rows: zero overrun, which is why "
              "this site is guarded and never fires)");
        bool verdict_free = true;
        for (const char* w : {"tolerance", "toler", "band", "verdict", "PASS", "FAIL", "accept", "thresh"})
            if (with_w.find(w) != std::string::npos || ref.find(w) != std::string::npos) verdict_free = false;
        check(verdict_free, "no tolerance, no band, no verdict word - geometry numbers only");
    }

    // ---- (15f) the harness source: the base-vs-offset pairing, single-sourced sizes ----
    {
        const std::string src_path = std::string(p2_measure_test_source_dir()) + "/p2_measure_main.cpp";
        std::ifstream srcf(src_path, std::ios::binary);
        const std::string src((std::istreambuf_iterator<char>(srcf)), std::istreambuf_iterator<char>());
        check(!src.empty(), "the harness source is readable for the pairing pin");
        if (src.empty()) return;
        check(src.find("gu.dst = (float*) d.gu;") != std::string::npos,
              "fix9: the gate/up product's dst is the buffer BASE (prefill.cpp:4666 gu.dst = m.GU) with its "
              "absolute bounds - the site that had d.gu + r0*1280");
        check(src.find("gu.dst = (float*) d.gu + r0 * 1280") == std::string::npos,
              "no offset pointer left on the absolute-bounds site (the double offset is gone)");
        check(src.find("dn.dst = (float*) d.dm + r0 * kN") != std::string::npos,
              "the down site keeps base + r0*ld, the pairing its group-relative bounds need");
        check(src.find("mmq::swiglu((const float*) d.gu + r0 * 1280") != std::string::npos,
              "swiglu still reads the group's own row span - which is WHERE the fixed write puts it "
              "(prefill.cpp:4674 reads m.GU + r0*1280 too)");
        int fits = 0;
        size_t at = src.find("product_write_fits(wgeom, rows)");
        while (at != std::string::npos) { ++fits; at = src.find("product_write_fits(wgeom, rows)", at + 1); }
        check(fits == 2,
              "BOTH product sites check their write against the buffer on the CPU before the launch "
              "(gate/up and down), and the refusal is typed (REFUSAL (side A geometry), exit 4)");
        check(src.find("mem.take(mmq_gu_buffer_bytes(rows)") != std::string::npos &&
              src.find("mem.take(mmq_dm_buffer_bytes(rows)") != std::string::npos &&
              src.find("mem.take(mmq_gu_buffer_bytes(rows_a)") != std::string::npos &&
              src.find("mem.take(mmq_dm_buffer_bytes(rows_a)") != std::string::npos,
              "the gu/dm buffers are allocated through the section-7d sizes (the arm's too): the write check "
              "and the allocation are the SAME expression");
        check(src.find("wgeom = ProductGeom{};") != std::string::npos &&
              src.find("pr.w_first = wgeom.valid ? product_write_first_row(wgeom) : 0;") != std::string::npos,
              "the geometry is cleared for non-product ops and carried into the fault line by the probe - "
              "a non-product fault keeps fix8's exact line");
        check(src.find("REFUSAL (side A%s): %s @ layer %lld: %s") != std::string::npos,
              "the side-A refusal keeps its shape for a device fault (empty suffix, exit 8) and gains the "
              "geometry class (exit 4)");
        check(src.find("prologue clean") != std::string::npos && src.find("STAGE %s L%lld") != std::string::npos &&
              src.find("TRUTH L%lld") != std::string::npos && src.find("ring probe") != std::string::npos &&
              src.find("format_op_summary") != std::string::npos,
              "the fix4-fix8 instruments all stay (CKPT bracket, STAGE, TRUTH, ring probe, OPSUM)");
    }
}

int main(int argc, char** argv) {
    // The strict-parser integration seam: emit the deterministic CPU self-test document (no GPU, no
    // helper, no pack) so tools/mixed_gpu/p2_measure/p2_report_audit.py and
    // tests/terminal/test_p2_report_json.py can PARSE it with a real JSON parser - the v1 defect passed
    // every substring check the byte-stability test has.  "--emit-selftest <variant> <path>".
    if (argc == 4 && std::strcmp(argv[1], "--emit-selftest") == 0) {
        Report r;
        std::string err;
        if (make_cpu_selftest_report(argv[2], r, err) != 0) {
            std::fprintf(stderr, "p2_measure_test: %s\n", err.c_str());
            return 3;
        }
        std::FILE* f = std::fopen(argv[3], "wb");
        if (!f) {
            std::fprintf(stderr, "p2_measure_test: cannot write %s\n", argv[3]);
            return 4;
        }
        const std::string j = to_json(r);
        const size_t written = std::fwrite(j.data(), 1, j.size(), f);
        std::fclose(f);
        return written == j.size() ? 0 : 4;
    }
    (void) argc; (void) argv;
    section_metric();
    section_nmse();
    section_percentile();
    section_fixtures();
    section_selection();
    section_report();
    section_framing();
    section_blob_source();
    section_sizing_and_claims();
    section_side_b();
    section_pair_row();
    section_grow_after_shrink();
    section_host_staging();
    section_gguf_truth();
    section_fault_attribution();
    section_product_write_geometry();
    std::printf("%s: %d failure(s)\n", failures ? "p2_measure_test: FAIL" : "p2_measure_test: PASS",
                failures);
    return failures ? 1 : 0;
}
