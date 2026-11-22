// tools/mixed_gpu/p2_measure/p2_measure.cpp - the CPU-only half of the P2 deviation harness.
//
// Fixtures, the metric, the percentiles, the activation-dump reader, the layer's claim set, the REAL helper
// prompt-row drive (the engine's own ExpertHelperClient over the real transport) and the report writer.  No
// device call in this file: Side A (the MMQ producer and its group products) lives in p2_measure_main.cpp,
// which is what makes this file compilable, runnable and pinnable on a host with no GPU (the CPU test links
// exactly these sources plus the client and the frame transport).
//
// NO TOLERANCE, NO VERDICT: nothing here compares a measured number against a limit.
#include "p2_measure.hpp"

#include "strata/core/expert_helper_protocol.hpp"
#include "strata/artifact/gguf_reader.hpp"
#include "ggml.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <limits>
#include <random>
#include <iomanip>
#include <sstream>

#include <sys/stat.h>
#include <sys/types.h>

namespace strata {
namespace tools {
namespace p2 {

namespace eh = core::expert_helper;

bool cpu_activation_to_helper(int type, const std::vector<uint8_t>& raw, int64_t n,
                              std::vector<uint8_t>& wire, std::vector<float>& reconstructed,
                              std::string& err) {
    const int block = type == GGML_TYPE_Q8_K ? 256 : type == GGML_TYPE_Q8_0 ? 32 : 0;
    const size_t bytes = block == 256 ? 292 : 34;
    if (!block || n <= 0 || n % block || ggml_type_size((ggml_type) type) != bytes ||
        (uint64_t) (n / block) != raw.size() / bytes || raw.size() % bytes) {
        err = "CPU activation transfer requires an exact audited Q8_K/Q8_0 image";
        return false;
    }
    wire.assign((size_t) n / 32 * 36, 0);
    reconstructed.resize((size_t) n);
    for (int64_t i = 0; i < n; i += 32) {
        const uint8_t* src = raw.data() + (size_t) (i / block) * bytes;
        float d;
        if (block == 256) std::memcpy(&d, src, 4);
        else {
            ggml_fp16_t dh;
            std::memcpy(&dh, src, 2);
            d = ggml_fp16_to_fp32(dh);
        }
        const ggml_fp16_t dh = ggml_fp32_to_fp16(d);
        if (!std::isfinite(d) || !std::isfinite(ggml_fp16_to_fp32(dh))) {
            err = "CPU activation scale cannot be represented by the helper";
            return false;
        }
        const int8_t* qs = (const int8_t*) (src + (block == 256 ? 4 : 2) + i % block);
        uint8_t* dst = wire.data() + (size_t) (i / 32) * 36;
        int sum = 0;
        for (int j = 0; j < 32; ++j) {
            reconstructed[(size_t) i + j] = d * qs[j];
            sum += qs[j];
        }
        const ggml_fp16_t sh = ggml_fp32_to_fp16(d * sum);
        if (!std::isfinite(ggml_fp16_to_fp32(sh))) {
            err = "CPU activation sum cannot be represented by the helper";
            return false;
        }
        std::memcpy(dst, &dh, 2);
        std::memcpy(dst + 2, &sh, 2);
        std::memcpy(dst + 4, qs, 32);
    }
    return true;
}

bool decode_mmq_d4(const std::vector<uint8_t>& raw, int64_t rows, int64_t cols,
                   std::vector<float>& scales, std::vector<int8_t>& qs, std::string& err) {
    constexpr size_t bytes = 4 * sizeof(float) + 128;
    if (rows <= 0 || cols <= 0 || cols % 128 ||
        (uint64_t) rows > std::numeric_limits<size_t>::max() / (uint64_t) cols ||
        (uint64_t) rows > raw.size() / bytes / (uint64_t) (cols / 128)) {
        err = "MMQ D4 image is short or has invalid/overflowed geometry";
        return false;
    }
    scales.resize((size_t) rows * (size_t) cols / 32);
    qs.resize((size_t) rows * (size_t) cols);
    for (int64_t r = 0; r < rows; ++r) {
        for (int64_t c = 0; c < cols; c += 32) {
            const size_t block = (size_t) (c / 128) * (size_t) rows + (size_t) r;
            const uint8_t* src = raw.data() + block * bytes;
            const size_t logical = (size_t) r * (size_t) cols + (size_t) c;
            std::memcpy(&scales[logical / 32], src + (size_t) ((c % 128) / 32) * sizeof(float), sizeof(float));
            std::memcpy(qs.data() + logical, src + 4 * sizeof(float) + (size_t) (c % 128), 32);
        }
    }
    return true;
}

bool diff_q8_input(const float* x, int64_t n, const float* primary_scales,
                   const int8_t* primary_qs, Q8InputDiff& out, std::string& err) {
    out = Q8InputDiff{};
    if (n <= 0 || n % 32 || !x || !primary_scales || !primary_qs) {
        err = "q8 producer comparison needs nonempty whole blocks and all three inputs";
        return false;
    }
    for (int64_t i = 0; i < n; ++i) {
        if (!std::isfinite(x[i]) || !std::isfinite(primary_scales[i / 32])) {
            err = "q8 producer comparison refuses nonfinite input/scales";
            return false;
        }
    }
    std::vector<uint8_t> helper((size_t) (n / 32) * 36);
    core::quantize_q8_1_cpu(x, n, helper.data());
    out.blocks = n / 32;
    for (int64_t b = 0; b < out.blocks; ++b) {
        ggml_fp16_t half;
        std::memcpy(&half, helper.data() + (size_t) b * 36, sizeof(half));
        const float hs = ggml_fp16_to_fp32(half), ps = primary_scales[b];
        if (std::memcmp(&ps, &hs, sizeof(float))) {
            ++out.scale_different;
            if (out.first_scale_block < 0) {
                out.first_scale_block = b;
                out.first_primary_scale = ps;
                out.first_helper_scale = hs;
            }
        }
        if (ggml_fp32_to_fp16(ps) != half) ++out.scale_after_half_different;
        out.max_scale_abs = std::max(out.max_scale_abs, std::fabs((double) ps - hs));
        bool different = false;
        for (int j = 0; j < 32; ++j) {
            int8_t hq;
            std::memcpy(&hq, helper.data() + (size_t) b * 36 + 4 + j, 1);
            const int64_t i = b * 32 + j;
            const int pq = primary_qs[i];
            if (pq != hq) {
                ++out.integers_different;
                different = true;
                if (out.first_integer < 0) {
                    out.first_integer = i;
                    out.first_primary_integer = pq;
                    out.first_helper_integer = hq;
                }
            }
            out.max_reconstructed_abs = std::max(out.max_reconstructed_abs,
                                                 std::fabs((double) ps * pq - (double) hs * hq));
        }
        if (different) ++out.integer_blocks_different;
    }
    return true;
}

std::string q8_input_diff_json(const Q8InputDiff& d) {
    std::ostringstream s;
    s << std::setprecision(17)
      << "{\"blocks\":" << d.blocks << ",\"scale_different\":" << d.scale_different
      << ",\"scale_after_half_different\":" << d.scale_after_half_different
      << ",\"integers_different\":" << d.integers_different
      << ",\"integer_blocks_different\":" << d.integer_blocks_different
      << ",\"first_scale_block\":" << d.first_scale_block
      << ",\"first_primary_scale\":" << d.first_primary_scale
      << ",\"first_helper_scale\":" << d.first_helper_scale
      << ",\"first_integer\":" << d.first_integer
      << ",\"first_primary_integer\":" << d.first_primary_integer
      << ",\"first_helper_integer\":" << d.first_helper_integer
      << ",\"max_scale_abs\":" << d.max_scale_abs
      << ",\"max_reconstructed_abs\":" << d.max_reconstructed_abs << "}";
    return s.str();
}

// ============================== the metric ==============================

uint64_t fnv64(const void* data, size_t bytes) {
    const uint8_t* p = (const uint8_t*) data;
    uint64_t h = 0xcbf29ce484222325ULL;   // the FNV-1a 64-bit offset basis 14695981039346656037
    for (size_t i = 0; i < bytes; ++i) {
        h ^= (uint64_t) p[i];
        h *= 1099511628211ULL;
    }
    return h;
}

RowDiff diff_rows(const float* a, const float* b, int64_t n, double eps) {
    RowDiff d;
    if (n <= 0) return d;
    d.n = n;                              // the length the energies cover
    double sum = 0.0, sq = 0.0, na = 0.0;
    for (int64_t i = 0; i < n; ++i) {
        const double av = (double) a[i], bv = (double) b[i];
        // A NaN/Infinity in the row is RECORDED, and the row is never reported as a finite agreement.
        if (!std::isfinite(av) || !std::isfinite(bv)) d.nonfinite = true;
        const double diff = std::fabs(av - bv);
        const double aa = std::fabs(av), bb = std::fabs(bv);
        d.max_abs = std::fmax(d.max_abs, diff);
        d.row_amax = std::fmax(d.row_amax, std::fmax(aa, bb));
        d.a_amax = std::fmax(d.a_amax, aa);
        d.b_amax = std::fmax(d.b_amax, bb);
        sum += diff;
        sq += diff * diff;
        na += av * av;
    }
    d.mean_abs = sum / (double) n;
    d.sum_sq_err = sq;                    // retained: the row's error energy (raw double accumulation)
    d.sum_sq_ref = na;                    // retained: the row's reference energy, reference = side A
    d.ref_zero = (na == 0.0);             // no reference energy: the relative measures below are UNDEFINED
    const double amax = std::fmax(d.row_amax, eps);   // the eps guard: a zero row gives 0/eps = 0, never 0/0
    d.max_rel = d.max_abs / amax;
    const double l2 = std::sqrt(sq), nrm = std::sqrt(na);
    // v1's value, kept EXACTLY: a zero-reference row reports 0 here.  `ref_zero` above is what says that
    // 0 is an UNDEFINED ratio, not an exact row.  No band metric is redefined by this change.
    d.l2_rel = nrm > 0.0 ? l2 / nrm : 0.0;
    d.a_fnv = fnv64(a, (size_t) n * sizeof(float));
    d.b_fnv = fnv64(b, (size_t) n * sizeof(float));
    if (d.nonfinite) {
        // fmax can hide a NaN input. No derived metric from an invalid row may look like agreement.
        d.max_abs = d.row_amax = d.max_rel = d.mean_abs = d.l2_rel = d.a_amax = d.b_amax = std::nan("");
    }
    return d;
}

// ---- the energy-weighted tensor NMSE over a POPULATION of rows (diagnostic, no threshold, no verdict) ----

const char* nmse_state_name(NmseState s) {
    switch (s) {
        case NmseState::kEmpty: return "empty";
        case NmseState::kOk: return "ok";
        case NmseState::kZeroRefExact: return "zero_reference_exact_match";
        case NmseState::kZeroRefUnbounded: return "zero_reference_nonzero_error_unbounded";
        case NmseState::kInvalidNonfinite: return "invalid_nonfinite_rows";
        case NmseState::kInvalidEnergy: return "invalid_energy";
    }
    return "invalid_state";
}

NmseDiag nmse_diagnostic(const std::vector<RowDiff>& population) {
    NmseDiag s;
    s.rows = (int64_t) population.size();
    // Nonfinite rows are counted FIRST and refuse the whole population: their energies are NaN, so any
    // partial sum would be a made-up finite number.
    for (const RowDiff& d : population) {
        if (d.nonfinite) ++s.rows_nonfinite;
        if (d.ref_zero) ++s.rows_ref_zero;
    }
    s.state = NmseState::kEmpty;
    s.has_value = false;
    s.nmse = 0.0;
    if (population.empty()) return s;                       // count 0, null: an empty population says "no rows"
    if (s.rows_nonfinite > 0) {                             // never silently finite, never 0
        s.state = NmseState::kInvalidNonfinite;
        s.energy_error = s.energy_ref = std::nan("");
        return s;
    }
    for (const RowDiff& d : population) {
        if (d.n <= 0 || !std::isfinite(d.sum_sq_err) || !std::isfinite(d.sum_sq_ref) ||
            d.sum_sq_err < 0.0 || d.sum_sq_ref < 0.0 || d.ref_zero != (d.sum_sq_ref == 0.0)) {
            s.state = NmseState::kInvalidEnergy;
            s.energy_error = s.energy_ref = std::nan("");
            return s;
        }
        s.energy_error += d.sum_sq_err;
        s.energy_ref += d.sum_sq_ref;
    }
    if (!std::isfinite(s.energy_error) || !std::isfinite(s.energy_ref)) {
        s.state = NmseState::kInvalidEnergy;               // overflow is not a count of nonfinite inputs
        s.energy_error = s.energy_ref = std::nan("");
        return s;
    }
    if (s.energy_ref == 0.0) {
        if (s.energy_error == 0.0) {                        // every row is a zero row: a named 0
            s.state = NmseState::kZeroRefExact;
            s.has_value = true;
            s.nmse = 0.0;
        } else {                                            // zero reference, nonzero candidate: UNBOUNDED
            s.state = NmseState::kZeroRefUnbounded;         // null, never 0, never a large finite number
        }
        return s;
    }
    s.nmse = s.energy_error / s.energy_ref;
    if (!std::isfinite(s.nmse)) {
        s.state = NmseState::kInvalidEnergy;
        s.energy_error = s.energy_ref = std::nan("");
        return s;
    }
    s.state = NmseState::kOk;
    s.has_value = true;
    return s;
}

double percentile(const std::vector<double>& sorted_ascending, double q) {
    const size_t n = sorted_ascending.size();
    if (n == 0) return std::nan("");
    double f = q * (double) (n - 1);
    if (f < 0.0) f = 0.0;
    size_t idx = (size_t) std::ceil(f);
    if (idx >= n) idx = n - 1;
    return sorted_ascending[idx];
}

Agg aggregate(std::vector<double> v) {
    Agg a;
    a.count = (int64_t) v.size();
    a.empty = v.empty();
    if (v.empty()) return a;
    if (std::any_of(v.begin(), v.end(), [](double value) { return !std::isfinite(value); })) {
        // Reject before sorting: NaNs do not define the ordering a percentile needs.
        a.p50 = a.p90 = a.p99 = a.p999 = a.max = a.mean = std::nan("");
        return a;
    }
    std::sort(v.begin(), v.end());
    a.p50 = percentile(v, 0.50);
    a.p90 = percentile(v, 0.90);
    a.p99 = percentile(v, 0.99);
    a.p999 = percentile(v, 0.999);
    a.max = v.back();
    double s = 0.0;
    for (double x : v) s += x;
    a.mean = s / (double) v.size();
    return a;
}

// ============================== the fixtures ==============================
//
// Eight REAL prompt texts, pinned here.  The first three are the acceptance driver's pinned texts
// (tools/mixed_gpu/run_acceptance.py:82-86): the SAMPLE_PROMPT whose request bytes the design pins as
// SHA256 93d4b9ca..., the prefill-shape text (a repeated block), the decode-shape text.  The other five are
// varied realistic texts of clearly different lengths - short, mid, long, very long; prose, code, JSON - so a
// run spans prompt shapes, as design §2 line 38 asks.  The harness measures the activations the ENGINE
// produced for these texts (the parent's dumps), so the texts are the run's contract: the JSON records each
// fixture's character count and FNV-1a/64, and the CPU test pins them, so an edited text fails the test
// instead of silently changing a run.
namespace {

const char* const kPinnedSample =
    "Explain how a moire pattern forms when two grids overlap. Write five short paragraphs, with no headings. "
    "Do not use tools.";

const char* const kLighthouseRepetitive =
    "Every evening a lighthouse keeper climbs the hundred steps, trims the lamp, and writes the weather in a "
    "ledger. Every evening a lighthouse keeper climbs the hundred steps, trims the lamp, and writes the weather "
    "in a ledger. Every evening a lighthouse keeper climbs the hundred steps, trims the lamp, and writes the "
    "weather in a ledger. Every evening a lighthouse keeper climbs the hundred steps, trims the lamp, and writes "
    "the weather in a ledger. Every evening a lighthouse keeper climbs the hundred steps, trims the lamp, and "
    "writes the weather in a ledger. Every evening a lighthouse keeper climbs the hundred steps, trims the lamp, "
    "and writes the weather in a ledger. Every evening a lighthouse keeper climbs the hundred steps, trims the "
    "lamp, and writes the weather in a ledger. Every evening a lighthouse keeper climbs the hundred steps, trims "
    "the lamp, and writes the weather in a ledger. Every evening a lighthouse keeper climbs the hundred steps, "
    "trims the lamp, and writes the weather in a ledger. In one sentence, summarize what the keeper does.";

const char* const kDecodeList =
    "List the integers from 1 to 200, each on its own line, with no commentary.";

const char* const kCodeShort =
    "Write a C function that parses a hex string into a uint64 and rejects anything that is not hex. No "
    "allocations, no locale calls, and say which errno each failure sets.";

const char* const kDocRewrite =
    "Rewrite the following pages of our operator manual so that a person with no background in GPUs can follow "
    "them. Keep every measured number exactly as written, keep the order of the steps, and mark every statement "
    "that is not backed by a measurement. Do not add new claims.\n\n"
    "1. The engine reads each routed expert blob once per prompt chunk, in groups of sixteen experts.\n"
    "2. A group's products are gate/up, the SwiGLU of its rows, the intermediate re-quantized for the down "
    "projection, and the down projection.\n"
    "3. The activations are rounded to q8_1 blocks of thirty-two values with an fp16 scale and an fp16 sum.\n"
    "4. A row the helper computes arrives unweighted; the combine applies the router's weight once.\n"
    "5. The tier is armed by an environment variable, and a default run never enters it.\n"
    "6. If the helper dies mid-request, the rows it claimed are recomputed on the CPU in tiles of at most eight "
    "tokens.\n";

const char* const kJsonExtract =
    "From the JSON below, extract the build id, the device name, the free memory in MiB, and every layer index "
    "whose resident count is greater than 100. Answer with one line per field, name and value, nothing else.\n\n"
    "{\"build_id\":\"f83039d31a\",\"device\":\"NVIDIA GeForce RTX 3060\",\"vram_mib\":12288,\"free_mib\":790,"
    "\"resident\":{\"32\":141,\"33\":98,\"34\":207,\"35\":64,\"36\":112,\"37\":150,\"38\":88,\"39\":301,"
    "\"40\":176,\"41\":59,\"42\":212,\"43\":133,\"44\":96,\"45\":244,\"46\":71,\"47\":188},"
    "\"windows\":[{\"stage\":0,\"layers\":\"0-31\",\"tokens\":8192},{\"stage\":1,\"layers\":\"32-47\","
    "\"tokens\":8192}],\"helper\":{\"socket\":\"/tmp/helper.sock\",\"slots\":4780,\"bytes\":9362079744,"
    "\"features\":[\"prompt_rows\"]},\"refusals\":[],\"counters\":{\"rows_claimed\":256,\"rows_returned\":256,"
    "\"rows_local_recompute\":0},\"timings_ms\":{\"begin\":1.83,\"wait\":14.42,\"combine\":3.11}}\n";

const char* const kCodeReview =
    "Please review this code for correctness bugs only, not style. The function reads one layer's routed "
    "expert ids, counts them per expert, and builds the claim set for a helper tier. Report every bug you find "
    "with the line it is on, and say what input makes it fire. If you believe a case is unreachable, say so and "
    "name the invariant that makes it unreachable.\n\n"
    "```c\n"
    "int select_rows(const int32_t * cnt, const int32_t * toks, const int32_t * host_res, int64_t layer,\n"
    "                const uint8_t * holds, const uint8_t * stable, int64_t n_expert, int64_t k,\n"
    "                int64_t cap_per_expert, int64_t cap_layer, int64_t cap_tok, Plan * out) {\n"
    "    out->rows = 0;\n"
    "    int64_t used = 0;\n"
    "    for (int64_t e = 0; e < n_expert; ++e) {\n"
    "        if (cnt[e] == 0) continue;\n"
    "        if (host_res[layer * n_expert + e] >= 0) continue;\n"
    "        if (!holds[e] || !stable[e]) continue;\n"
    "        if (cnt[e] > cap_per_expert) continue;\n"
    "        if (out->rows + cnt[e] > cap_layer) continue;\n"
    "        for (int64_t i = 0; i < cnt[e]; ++i) {\n"
    "            out->tok[used++] = toks[i];\n"
    "        }\n"
    "        out->rows += cnt[e];\n"
    "    }\n"
    "    return 0;\n"
    "}\n"
    "```\n\n"
    "Context: the token lists for the experts are concatenated in expert order, so expert e owns the entries "
    "starting at the sum of the counts of the experts below it. The tier claims an expert whole or not at all, "
    "because a split claim would leave the same row computed twice. The reply is validated against the claims, "
    "so a pair that appears twice in the claim list is a row the engine would add twice. The distinct-token "
    "ceiling is separate from the row ceiling and is checked on the union of the claimed tokens. The function "
    "above is not the shipped one; it is a reduction of it for review.\n";

const char* const kProseLong =
    "Write a careful account, for a reader who is not a specialist, of what changes numerically when a "
    "mixture-of-experts model computes some of a layer's routed rows on a second graphics card with a different "
    "multiplication kernel than the first card uses.\n\n"
    "Cover, in this order, and keep the distinction between what is measured and what is inferred visible on "
    "every page:\n\n"
    "First, the two activation quantizers. Both produce q8_1: blocks of thirty-two values with a scale and a "
    "sum. One is a device kernel whose scale is the block's largest magnitude divided by one hundred "
    "twenty-seven, whose quantized integers are the rounded quotients, and whose sum is reduced across the "
    "block by a butterfly of shuffle steps. The other runs on the host, with the same scale rule and the same "
    "rounding, and a left-to-right sum. The scales and the integers are provably identical, because a maximum "
    "and a rounding depend only on the values. The sums are not provably identical: floating-point addition is "
    "not associative, and the two orders are different trees. What the difference is worth, in the last bits of "
    "an fp16 field, is a measurement, not a deduction.\n\n"
    "Second, the products. On the first card the expert weights stay quantized and the multiplications run on "
    "integer tensor cores over gathered groups of sixteen experts, so the rounding of one row's tile depends on "
    "which other experts share its group. On the second card the same expert weights are read by a grouped "
    "kernel built for decode windows, whose groups are the claimed rows only. The two kernels are both "
    "faithful to the weights; they are not the same arithmetic, and no parity evidence for one of them "
    "transfers to the other at prompt scale.\n\n"
    "Third, the consequence for generation. A row that enters the weighted combine differs by a little; the "
    "weighted sum of ten rows differs by a little more; the residual stream carries that difference into the "
    "next layer, and a prompt of thousands of tokens multiplies the number of opportunities for a sampled token "
    "to land on the other side of a close comparison. This is the reason a deviation measured at row level is "
    "not the same question as a prompt reproducing its tokens, and why the design asks for both.\n\n"
    "Fourth, honesty about what we do not know. Nobody has measured this. The claim that prompt bits will move "
    "is arithmetic, not measurement; the claim about how far a sampled token moves is a guess, not "
    "measurement. A band for accepting the difference belongs to the person who owns the model's accuracy, and "
    "must be agreed before the measurement, not chosen after it. If the measurement shows a cost the owner will "
    "not accept, the honest answer is that the rows stay where they are computed today. Say that plainly, and do "
    "not write a workaround that hides the difference: a fallback mask, a re-sampling of rows, a quiet rounding "
    "of the reply to make the comparison look better, are all ways of turning a measurement into a fiction.\n\n"
    "Write it in plain words, in short sentences, with no headings, and give every number you use the hardware "
    "it was measured on. Where a fact comes from reading code rather than from a measurement, say so in the "
    "sentence that states it.";

}  // namespace

const std::vector<Fixture>& fixtures() {
    static const std::vector<Fixture> f = {
        {"pinned-sample", "the acceptance driver's pinned sample request text, verbatim (the design's pinned workload)",
         kPinnedSample},
        {"lighthouse-prefill", "the acceptance driver's prefill-shape text: a repeated block (long, repetitive)",
         kLighthouseRepetitive},
        {"decode-list", "the acceptance driver's decode-shape text: a numbered-list request (short)", kDecodeList},
        {"code-question-short", "a short coding question (a few lines)", kCodeShort},
        {"doc-rewrite-mid", "a mid-length documentation rewrite with a numbered list", kDocRewrite},
        {"json-extract", "a prompt carrying a JSON document to extract fields from (mid, dense in digits)",
         kJsonExtract},
        {"code-review-long", "a realistic long code-review prompt with a C function pasted in", kCodeReview},
        {"prose-analysis-long", "a long prose/analysis prompt (several thousand characters)", kProseLong},
    };
    return f;
}

const Fixture* fixture_by_id(const std::string& id) {
    for (const Fixture& f : fixtures())
        if (id == f.id) return &f;
    return nullptr;
}

std::string fixture_list_string() {
    std::string s;
    for (const Fixture& f : fixtures()) {
        const size_t n = std::strlen(f.text);
        char buf[160];
        std::snprintf(buf, sizeof buf, "%s,%zu,%016llx;", f.id, n, (unsigned long long) fnv64(f.text, n));
        s += buf;
    }
    return s;
}

// ============================== activation dumps ==============================

bool load_moe_dump(const std::string& path, int64_t max_tokens, Activations& out, std::string& err) {
    std::ifstream f(path, std::ios::binary);
    if (!f) { err = "cannot open the activation dump " + path; return false; }
    int64_t hdr[3] = {0, 0, 0};
    f.read((char*) hdr, sizeof hdr);
    if (!f) { err = "activation dump " + path + " is shorter than its 24-byte header"; return false; }
    const int64_t T = hdr[0], N = hdr[1], K = hdr[2];
    if (N != kN || K != kK) {
        err = "activation dump " + path + " is N=" + std::to_string(N) + " K=" + std::to_string(K) +
              "; the harness measures the pack's N=" + std::to_string(kN) + " K=" + std::to_string(kK);
        return false;
    }
    if (T < 1) { err = "activation dump " + path + " has no tokens"; return false; }
    if (T > (int64_t) 1 << 22) { err = "activation dump " + path + " claims " + std::to_string(T) + " tokens";
        return false; }
    std::vector<float> x((size_t) (T * N));
    std::vector<int32_t> ids((size_t) (T * K));
    f.read((char*) x.data(), (std::streamsize) (x.size() * sizeof(float)));
    if (!f) {
        err = "activation dump " + path + " is short of its " + std::to_string(T * N) + " activation floats";
        return false;
    }
    f.read((char*) ids.data(), (std::streamsize) (ids.size() * sizeof(int32_t)));
    if (!f) {
        err = "activation dump " + path + " is short of its " + std::to_string(T * K) + " routed ids";
        return false;
    }
    out.T_dump = T;
    out.T = (max_tokens > 0 && max_tokens < T) ? max_tokens : T;
    out.x.assign(x.begin(), x.begin() + (size_t) (out.T * N));
    out.ids.assign(ids.begin(), ids.begin() + (size_t) (out.T * K));
    out.synthetic = false;
    out.source = path + (out.T < T ? (" [truncated to the first " + std::to_string(out.T) + " of " +
                                      std::to_string(T) + " tokens by --max-tokens]") : "");
    return true;
}

void synth_activations(uint64_t seed, int fixture_index, int64_t layer, int64_t T, Activations& out) {
    // The documented INFERIOR fallback (design §2 line 35 asks for REAL rows from STRATA_DUMP_MOE_INPUT): a
    // deterministic generator keyed by (seed, fixture, layer) so a run reproduces, shaped like a residual
    // stream (a per-row magnitude, a dense body, a few large outlier columns - the shape the q8_1 block scale
    // is sensitive to).  Every output that contains synthetic rows says so.
    const uint64_t s = seed * 1000003ULL + (uint64_t) fixture_index * 97ULL + (uint64_t) layer;
    std::mt19937_64 rng(s);
    std::normal_distribution<float> nd(0.0f, 1.0f);
    std::uniform_real_distribution<float> mag(0.1f, 10.0f);
    std::uniform_real_distribution<float> pos(0.0f, 1.0f);
    out.T = T;
    out.T_dump = T;
    out.x.assign((size_t) (T * kN), 0.0f);
    out.ids.assign((size_t) (T * kK), 0);
    for (int64_t t = 0; t < T; ++t) {
        const float m = mag(rng);
        float* row = out.x.data() + (size_t) t * kN;
        for (int64_t c = 0; c < kN; ++c) row[c] = nd(rng) * m;
        const int n_out = 2 + (int) (pos(rng) * 6.0);
        for (int i = 0; i < n_out; ++i) {
            const int64_t c = (int64_t) (pos(rng) * (float) kN) % kN;
            row[c] = (pos(rng) < 0.5f ? -1.0f : 1.0f) * m * (8.0f + pos(rng) * 24.0f);
        }
    }
    // Routing: k distinct experts per token, spread over the layer's 512, deterministic per (seed, fixture, layer).
    for (int64_t t = 0; t < T; ++t) {
        int32_t* idrow = out.ids.data() + (size_t) t * kK;
        for (int64_t j = 0; j < kK; ++j) {
            int32_t e = (int32_t) ((rng() >> 8) % (uint64_t) kNE);
            for (int64_t q = 0; q < j; ++q) {
                if (idrow[q] == e) {
                    e = (e + 1) % (int32_t) kNE;
                    q = -1;                          // re-check the whole row against the moved id
                }
            }
            idrow[j] = e;
        }
    }
    out.synthetic = true;
    out.source = "synthetic:seed=" + std::to_string(seed) + " fixture=" + std::to_string(fixture_index) +
                 " layer=" + std::to_string(layer);
}

// ============================== the layer's claim set ==============================

int build_layer_counts(const Activations& act, LayerCounts& out, std::string& err) {
    if (act.T < 1) { err = "no activation rows"; return eh::kErrGeometry; }
    out.cnt.assign((size_t) kNE, 0);
    for (int64_t i = 0; i < act.T * kK; ++i) {
        const int32_t e = act.ids[(size_t) i];
        if (e < 0 || e >= (int32_t) kNE) {
            err = "routed id out of range at entry " + std::to_string(i) + ": " + std::to_string(e);
            return eh::kErrGeometry;
        }
        ++out.cnt[(size_t) e];
    }
    out.rows_all = act.T * kK;
    out.toks.assign((size_t) out.rows_all, 0);
    std::vector<int64_t> off((size_t) kNE + 1, 0);
    for (int64_t e = 0; e < kNE; ++e) off[(size_t) e + 1] = off[(size_t) e] + out.cnt[(size_t) e];
    std::vector<int64_t> fill(off.begin(), off.end() - 1);
    for (int64_t i = 0; i < act.T * kK; ++i) {
        const int32_t e = act.ids[(size_t) i];
        out.toks[(size_t) fill[(size_t) e]++] = (int32_t) (i / kK);
    }
    return eh::kOk;
}

std::vector<std::pair<int32_t, int32_t>> rank_pairs(const std::map<int64_t, std::vector<int32_t>>& cnt_by_layer,
                                                     int64_t cap) {
    // ONE ranked fill list over the run's layers: layers ascending; within a layer, count DESC then expert ASC
    // (the CPU share's sorted-by-count shape, prefill.cpp:3899-3911).  The helper fills in THIS order and the
    // fill report is what makes the resident set authoritative.
    std::vector<std::pair<int32_t, int32_t>> out;
    for (const auto& kv : cnt_by_layer) {
        const int64_t layer = kv.first;
        std::vector<int64_t> e;
        for (int64_t q = 0; q < (int64_t) kv.second.size(); ++q)
            if (kv.second[(size_t) q] > 0) e.push_back(q);
        const std::vector<int32_t>& cnt = kv.second;
        std::sort(e.begin(), e.end(), [&](int64_t a, int64_t b) {
            if (cnt[(size_t) a] != cnt[(size_t) b]) return cnt[(size_t) a] > cnt[(size_t) b];
            return a < b;
        });
        for (int64_t q : e) out.push_back({(int32_t) layer, (int32_t) q});
    }
    if (cap > 0 && (int64_t) out.size() > cap) out.resize((size_t) cap);
    return out;
}

// ============================== Side B: the real helper prompt-row path ==============================

int helper_handshake(core::ExpertHelperClient& c, const std::string& socket, const HelperSetup& opt,
                     const std::vector<std::pair<int32_t, int32_t>>& ranked, HelperIdentity& id,
                     std::string& err) {
    const int rc0 = c.connect(socket, opt.hello_timeout_ms);
    if (rc0 != eh::kOk) {
        err = "the helper socket is not usable (" + std::string(eh::status_name(rc0)) + "): " + socket +
              " - start the helper (strata-expert-helper --pack DIR --socket PATH) before this harness";
        return rc0;
    }
    // The claimed bitmap is all zero: the harness owns no stage caches, so the helper may fill any pair of its
    // window.  Geometry: the pack's fixed MoE shape and the split32 box's helper window 32..47.
    int64_t free_mib = 0;
    const int rc = c.hello(kLayers, kNE, kN, kFF, kLayerLo, kLayerHi, opt.slots, opt.reserve_mib,
                           std::vector<uint8_t>((size_t) kLayers * kNE, 0), opt.hello_timeout_ms, free_mib);
    if (rc != eh::kOk) {
        err = "the handshake failed (" + std::string(eh::status_name(rc)) +
              "); the helper answers geometry for the pack's 48 x 512 x 2560 x 640 and refuses a geometry "
              "it has no grouped kernel for";
        return rc;
    }
    id.free_mib = free_mib;
    id.prompt_rows = c.prompt_rows_supported();
    if (!id.prompt_rows) {
        // The design's negotiation: a 24-byte legacy ack carries features = 0, and begin_prompt refuses LOCALLY
        // without sending anything.  The harness says so and stops: no frame is sent, nothing hangs.
        err = "the helper is LEGACY: its HelloAck carried features=0, so it serves no kBeginP/kRowsP tier. "
              "Rebuild strata-expert-helper from this tree; the harness sends no prompt-row frame to a legacy peer.";
        id.note = "legacy 24-byte ack (features=0)";
        return eh::kErrRequest;
    }
    id.note = "HelloAck carried kFeaturePromptRows";
    id.requested_pairs = (int64_t) ranked.size();
    std::vector<std::pair<int32_t, int32_t>> resident;
    int64_t bytes = 0;
    const int rcf = c.fill_report(ranked, opt.fill_timeout_ms, resident, bytes);
    if (rcf != eh::kOk) {
        err = "the fill failed (" + std::string(eh::status_name(rcf)) +
              "); the helper fills its window's pairs within the card's budget and reports what fit";
        return rcf;
    }
    id.resident = (int64_t) resident.size();
    id.resident_pairs = resident;   // the fill report is what makes the resident set AUTHORITATIVE
    id.bytes = bytes;
    return eh::kOk;
}

int helper_prompt_rows(core::ExpertHelperClient& c, int32_t layer, const core::ExpertHelperPromptPlan& plan,
                       const Activations& act, const int32_t* host_res, const HelperSetup& opt,
                       std::vector<float>& out, int32_t& rows, std::string& err) {
    rows = 0;
    const int64_t rows_n = plan.rows(), n_sel = plan.n_sel_tok();
    if (rows_n < 1) { err = "the plan claims no rows"; return eh::kErrGeometry; }
    const int rcb = c.prompt_buffer_ensure(kN);
    if (rcb != eh::kOk) {
        err = "the tier's pinned activation buffer failed (" + std::string(eh::status_name(rcb)) +
              "); the design's policy is: tier off for the request, printed notice, full local compute";
        return rcb;
    }
    // The q8 image row order is the plan's ASCENDING token list; copy those rows into the tier's OWN buffer
    // (row r = plan.tokens[r], the order begin_prompt quantizes in).
    float* xb = const_cast<float*>(c.prompt_buffer());
    for (int64_t r = 0; r < n_sel; ++r) {
        const int32_t t = plan.tokens[(size_t) r];
        if (t < 0 || t >= (int32_t) act.T) {
            err = "the plan names token " + std::to_string(t) + " outside the activation rows";
            return eh::kErrGeometry;
        }
        std::memcpy(xb + (size_t) r * (size_t) kN, act.x.data() + (size_t) t * (size_t) kN,
                    (size_t) kN * sizeof(float));
    }
    const int rcbp = c.begin_prompt(layer, plan, host_res, opt.begin_timeout_ms);
    if (rcbp != eh::kOk) {
        err = "begin_prompt refused (" + std::string(eh::status_name(rcbp)) + ") at layer " + std::to_string(layer);
        return rcbp;
    }
    std::vector<int32_t> claims((size_t) rows_n);
    for (int64_t i = 0; i < rows_n; ++i) claims[(size_t) i] = (int32_t) i;
    out.assign((size_t) rows_n * (size_t) kN, 0.0f);
    const int rc = c.finish_prompt(out.data(), kN, claims, opt.rows_timeout_ms, rows);
    if (rc != eh::kOk) {
        err = "finish_prompt refused the reply (" + std::string(eh::status_name(rc)) + ") at layer " +
              std::to_string(layer) + " for " + std::to_string(rows_n) + " claims";
        out.assign((size_t) rows_n * (size_t) kN, 0.0f);   // never a silent zero left readable
        return rc;
    }
    return eh::kOk;
}

namespace {
/// The primitive behind EVERY (token, expert) -> entry mapping in this harness: the ROUTED-SLOT lookup -
/// the entry index `token*kK + j` whose routed id IS the expert.  The expert id is never an index into an
/// entry-keyed table: `table[token*kK + expert]` reads another token's entry as soon as the id is >= kK,
/// which is the class of both live crashes of 2026-10-29 (the claims overrun, and pair (37, 75) -> entry
/// (44, slot 5)'s row 3810, ~39 MB past a 2250-row buffer).
int pair_entry_lookup(const Activations& act, int32_t token, int32_t expert, int64_t& entry,
                      std::string& why) {
    entry = -1;
    if (token < 0 || token >= (int32_t) act.T) {
        why = "token " + std::to_string(token) + " is outside the activation rows (T = " + std::to_string(act.T) + ")";
        return eh::kErrGeometry;
    }
    if (expert < 0 || expert >= (int32_t) kNE) {
        why = "expert " + std::to_string(expert) + " is outside the pack's routed experts (512)";
        return eh::kErrGeometry;
    }
    const int64_t base = (int64_t) token * kK;
    int slots = 0;
    int64_t at = -1;
    for (int64_t j = 0; j < kK; ++j)
        if (act.ids[(size_t) (base + j)] == expert) { ++slots; at = base + j; }
    if (slots == 0) { why = "token " + std::to_string(token) + " routes no such expert (" + std::to_string(expert) + ")"; return eh::kErrGeometry; }
    if (slots > 1) {
        why = "token " + std::to_string(token) + " routes expert " + std::to_string(expert) + " in " +
              std::to_string(slots) + " slots, so the pair names no single entry";
        return eh::kErrGeometry;
    }
    entry = at;
    return eh::kOk;
}
}  // namespace

int pair_row(const Activations& act, const int32_t* pair2row, int32_t token, int32_t expert,
             int32_t& row, std::string& err) {
    row = -1;
    err.clear();
    if (pair2row == nullptr) { err = "the side-A row map is missing"; return eh::kErrGeometry; }
    int64_t entry = -1;
    if (pair_entry_lookup(act, token, expert, entry, err) != eh::kOk) {
        err = "INVARIANT BREAK: " + err + ", so the pair (" + std::to_string(token) + ", " +
              std::to_string(expert) + ") has no entry - the plan's claim rule only names routed pairs";
        return eh::kErrGeometry;
    }
    row = pair2row[(size_t) entry];   // -1 = the entry is dropped from this run's rows (caller's refusal)
    return eh::kOk;
}

int check_side_b_rows(const Activations& act, const core::ExpertHelperPromptPlan& plan,
                      const int32_t* pair2row, const std::vector<float>& hb, const std::vector<float>& dm,
                      std::string& err) {
    err.clear();
    const int64_t rows_n = plan.rows();
    if (rows_n < 1) { err = "the plan claims no rows"; return eh::kErrGeometry; }
    if (pair2row == nullptr) { err = "the side-A row map is missing"; return eh::kErrGeometry; }
    // Side B: the reply's rows are addressed BY PAIR INDEX (finish_prompt scatters each row at the index
    // it carried), so the buffer must be plan.rows() * N floats - the size the loop reads.  A reply that
    // answered fewer claims never reaches this function (helper_prompt_rows refuses it), so a short buffer
    // here means the sizes and the addressing have drifted apart: refuse, name both numbers, read nothing.
    const int64_t want = rows_n * kN;
    if ((int64_t) hb.size() != want) {
        err = "side B row buffer is " + std::to_string(hb.size()) + " floats for " + std::to_string(rows_n) +
              " claimed rows (" + std::to_string(want) + " floats needed at the pair indices)";
        return eh::kErrGeometry;
    }
    // Side A: the claimed pair's row index comes from the routed-slot lookup (pair_row), and the loop reads
    // row * N + N floats.  A negative index is the pair having no MMQ row (the caller's own refusal), an
    // index past the buffer is the same class of defect as the side-B one: refuse before the read.
    for (int64_t j = 0; j < rows_n; ++j) {
        const int32_t t = plan.pair_token[(size_t) j], e = plan.pair_expert[(size_t) j];
        int32_t row = -1;
        std::string perr;
        if (pair_row(act, pair2row, t, e, row, perr) != eh::kOk) {
            err = "claimed pair (" + std::to_string(t) + ", " + std::to_string(e) + ") at index " +
                  std::to_string(j) + ": " + perr;
            return eh::kErrGeometry;
        }
        if (row < 0) {
            err = "claimed pair (" + std::to_string(t) + ", " + std::to_string(e) + ") at index " +
                  std::to_string(j) + " has no side-A row";
            return eh::kErrGeometry;
        }
        if ((int64_t) row + 1 > ((int64_t) dm.size() / kN)) {
            err = "side A row " + std::to_string(row) + " of claimed pair (" + std::to_string(t) + ", " +
                  std::to_string(e) + ") is past the " + std::to_string(dm.size() / kN) +
                  " rows of the side-A buffer";
            return eh::kErrGeometry;
        }
    }
    return eh::kOk;
}

// ============================== the report ==============================

std::string ggml_type_name(int32_t t) {
    switch (t) {
        case 0: case 2: return "Q4_0";
        case 6: return "Q5_0";
        case 7: return "Q5_1";
        case 8: return "Q8_0";
        case 12: return "Q4_K";
        case 13: return "Q5_K";
        case 14: return "Q6_K";
        case 16: return "IQ2_XXS";
        case 17: return "IQ2_XS";
        case 18: return "IQ3_XXS";
        case 19: return "IQ1_S";
        case 20: return "IQ4_NL";
        case 21: return "IQ3_S";
        case 22: return "IQ2_S";
        case 23: return "IQ4_XS";
        case 29: return "IQ1_M";
        case 39: return "MXFP4";
        case 40: return "NVFP4";
        case 42: return "Q2_0";
        default: return "type" + std::to_string(t);
    }
}

std::string json_escape(const std::string& s) {
    std::string o;
    for (char ch : s) {
        switch (ch) {
            case '"': o += "\\\""; break;
            case '\\': o += "\\\\"; break;
            case '\n': o += "\\n"; break;
            case '\r': o += "\\r"; break;
            case '\t': o += "\\t"; break;
            default:
                if ((unsigned char) ch < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof buf, "\\u%04x", (unsigned) (unsigned char) ch);
                    o += buf;
                } else o += ch;
        }
    }
    return o;
}

void sort_rows(Report& r) {
    std::stable_sort(r.rows.begin(), r.rows.end(), [](const RowRec& a, const RowRec& b) {
        if (a.fixture_index != b.fixture_index) return a.fixture_index < b.fixture_index;
        if (a.layer != b.layer) return a.layer < b.layer;
        return a.pair < b.pair;
    });
}

namespace {

std::string num(double v) {
    char buf[40];
    // NaN and +/-Infinity are NOT JSON numbers: v1 emitted `inf`, which a strict parser rejects.  A
    // non-finite measurement prints as null - the row's own `nonfinite` flag and the NMSE state say what
    // it means, and a null is never read as a 0.
    if (!std::isfinite(v)) return "null";
    std::snprintf(buf, sizeof buf, "%.12g", v);
    return buf;
}

std::string hex64(uint64_t v) {
    char buf[20];
    std::snprintf(buf, sizeof buf, "%016llx", (unsigned long long) v);
    return buf;
}

void agg_json_body(std::string& o, const Agg& a) {
    o += "{\"count\": " + std::to_string(a.count);
    o += ", \"p50\": " + num(a.p50);
    o += ", \"p90\": " + num(a.p90);
    o += ", \"p99\": " + num(a.p99);
    o += ", \"p999\": " + num(a.p999);
    o += ", \"max\": " + num(a.max);
    o += ", \"mean\": " + num(a.mean);
    o += "}";
}

void agg_json(std::string& o, const char* key, const Agg& a) {
    o += "\"";
    o += key;
    o += "\": ";
    agg_json_body(o, a);
}

std::string row_key(const RowRec& r) {
    return r.fixture + "/L" + std::to_string(r.layer) + "/e" + std::to_string(r.expert) + "/t" +
           std::to_string(r.token) + "/p" + std::to_string(r.pair);
}

/// ONE population's energy-weighted tensor NMSE, as JSON.  A non-numeric state emits `"nmse": null`
/// with its name - never 0, and never a number that reads like agreement.
std::string nmse_json(const char* population, const char* reference, const NmseDiag& s) {
    std::string o;
    o += "{\"population\": \"" + json_escape(population) + "\"";
    o += ", \"reference\": \"" + json_escape(reference) + "\"";
    o += ", \"state\": \"" + std::string(nmse_state_name(s.state)) + "\"";
    o += ", \"rows\": " + std::to_string(s.rows);
    o += ", \"rows_ref_zero\": " + std::to_string(s.rows_ref_zero);
    o += ", \"rows_nonfinite\": " + std::to_string(s.rows_nonfinite);
    o += ", \"energy_error\": " + num(s.energy_error);
    o += ", \"energy_ref\": " + num(s.energy_ref);
    o += ", \"nmse\": " + (s.has_value ? num(s.nmse) : std::string("null"));
    o += "}";
    return o;
}

std::string diff_json(const RowDiff& d) {
    std::string o;
    o += "{\"max_abs\": " + num(d.max_abs);
    o += ", \"row_amax\": " + num(d.row_amax);
    o += ", \"max_rel\": " + num(d.max_rel);
    o += ", \"mean_abs\": " + num(d.mean_abs);
    o += ", \"l2_rel\": " + num(d.l2_rel);
    o += ", \"a_amax\": " + num(d.a_amax);
    o += ", \"b_amax\": " + num(d.b_amax);
    o += ", \"a_fnv\": \"" + hex64(d.a_fnv) + "\"";
    o += ", \"b_fnv\": \"" + hex64(d.b_fnv) + "\"";
    // schema v2, additive: the retained energies and the row's state.  A NaN energy is null, never 0.
    o += ", \"n\": " + std::to_string(d.n);
    o += ", \"sum_sq_err\": " + num(d.sum_sq_err);
    o += ", \"sum_sq_ref\": " + num(d.sum_sq_ref);
    o += ", \"ref_zero\": " + std::string(d.ref_zero ? "true" : "false");
    o += ", \"nonfinite\": " + std::string(d.nonfinite ? "true" : "false");
    o += "}";
    return o;
}

struct TopRow {
    std::string key;
    double value = 0.0;
};
bool by_value_then_key(const TopRow& a, const TopRow& b) {
    const bool a_finite = std::isfinite(a.value), b_finite = std::isfinite(b.value);
    if (a_finite != b_finite) return !a_finite;  // invalid rows stay visible, ordered deterministically
    if (!a_finite) return a.key < b.key;
    if (a.value != b.value) return a.value > b.value;
    return a.key < b.key;
}

}  // namespace

void collect_buckets(const Report& r, Buckets& b) {
    for (const RowRec& rr : r.rows) {
        const std::string fmt = ggml_type_name(rr.gu_type) + "+" + ggml_type_name(rr.d_type);
        b.ab["format:" + fmt].push_back(rr.a_vs_b.max_abs);
        b.rel["format:" + fmt].push_back(rr.a_vs_b.max_rel);
        b.ab["layer:" + std::to_string(rr.layer)].push_back(rr.a_vs_b.max_abs);
        b.rel["layer:" + std::to_string(rr.layer)].push_back(rr.a_vs_b.max_rel);
        b.ab["fixture:" + rr.fixture].push_back(rr.a_vs_b.max_abs);
        b.rel["fixture:" + rr.fixture].push_back(rr.a_vs_b.max_rel);
    }
    // The recomposition term (design line 22) is a separate bucket family: rows the arm keeps local.
    for (const RowRec& rr : r.recompose) {
        const std::string fmt = ggml_type_name(rr.gu_type) + "+" + ggml_type_name(rr.d_type);
        b.ab["recompose:" + fmt].push_back(rr.arm_vs_a.max_abs);
        b.rel["recompose:" + fmt].push_back(rr.arm_vs_a.max_rel);
        b.ab["recompose-layer:" + std::to_string(rr.layer)].push_back(rr.arm_vs_a.max_abs);
        b.rel["recompose-layer:" + std::to_string(rr.layer)].push_back(rr.arm_vs_a.max_rel);
    }
}

std::string to_json(const Report& r) {
    Report s = r;
    sort_rows(s);
    std::string o;
    o += "{\n";
    o += "  \"schema\": \"" + std::string(kReportSchemaV2) + "\",\n";
    o += "  \"harness_version\": \"" + json_escape(s.meta.harness_version) + "\",\n";
    o += "  \"argv\": \"" + json_escape(s.meta.argv_joined) + "\",\n";
    o += "  \"pack_dir\": \"" + json_escape(s.meta.pack_dir) + "\",\n";
    o += "  \"blob_file\": \"" + json_escape(s.meta.blob_file) + "\",\n";
    o += "  \"blob_mapping\": \"" + json_escape(s.meta.blob_mapping) + "\",\n";
    o += "  \"helper_socket\": \"" + json_escape(s.meta.socket) + "\",\n";
    o += "  \"activation_mode\": \"" + json_escape(s.meta.activation_mode) + "\",\n";
    o += "  \"eps\": " + num(s.meta.eps) + ",\n";
    o += "  \"layer_window\": [" + std::to_string(s.meta.layer_lo) + ", " + std::to_string(s.meta.layer_hi) + "],\n";
    o += "  \"max_tokens\": " + std::to_string(s.meta.max_tokens) + ",\n";
    o += "  \"arm_groups\": " + std::string(s.meta.arm_groups ? "true" : "false") + ",\n";
    o += "  \"summary_only\": " + std::string(s.meta.summary_only ? "true" : "false") + ",\n";
    o += "  \"caps\": {\"kMaxSelTok\": " + std::to_string(eh::kMaxSelTok) + ", \"kMaxRowsPerLayer\": " +
         std::to_string(eh::kMaxRowsPerLayer) + ", \"kMaxRowsPerExpert\": " + std::to_string(eh::kMaxRowsPerExpert) +
         ", \"kMaxPairs\": " + std::to_string(eh::kMaxPairs) + "},\n";
    o += "  \"helper\": {\"free_mib\": " + std::to_string(s.meta.helper.free_mib) + ", \"resident_pairs\": " +
         std::to_string(s.meta.helper.resident) + ", \"bytes\": " + std::to_string(s.meta.helper.bytes) +
         ", \"requested_pairs\": " + std::to_string(s.meta.helper.requested_pairs) + ", \"prompt_rows\": " +
         std::string(s.meta.helper.prompt_rows ? "true" : "false") + ", \"note\": \"" +
         json_escape(s.meta.helper.note) + "\"},\n";
    o += "  \"formats\": [";
    for (size_t i = 0; i < s.meta.formats.size(); ++i) {
        if (i) o += ", ";
        o += "{\"layer\": " + std::to_string(s.meta.formats[i].layer) + ", \"gu_type\": " +
             std::to_string(s.meta.formats[i].gu_type) + ", \"gu\": \"" + ggml_type_name(s.meta.formats[i].gu_type) +
             "\", \"d_type\": " + std::to_string(s.meta.formats[i].d_type) + ", \"d\": \"" +
             ggml_type_name(s.meta.formats[i].d_type) + "\"}";
    }
    o += "],\n";
    o += "  \"staging\": [";   // fix7: the per-layer shape truth this run resolved (header vs GGUF)
    for (size_t i = 0; i < s.meta.staging.size(); ++i) {
        if (i) o += ", ";
        o += "\"" + json_escape(s.meta.staging[i]) + "\"";
    }
    o += "],\n";
    o += "  \"fixtures\": [";
    {
        size_t i = 0;
        for (const Fixture& f : fixtures()) {
            const auto it = s.meta.fixtures_used.find(f.id);
            if (it == s.meta.fixtures_used.end()) continue;
            if (i++) o += ", ";
            const size_t n = std::strlen(f.text);
            o += "{\"id\": \"" + std::string(f.id) + "\", \"chars\": " + std::to_string(n) +
                 ", \"fnv64\": \"" + hex64(fnv64(f.text, n)) + "\", \"tokens\": " +
                 std::to_string(it->second.first) + ", \"rows\": " + std::to_string(it->second.second) + "}";
        }
    }
    o += "],\n";
    o += "  \"counts\": {\"rows_expected\": " + std::to_string(s.meta.rows_expected) + ", \"rows_measured\": " +
         std::to_string(s.meta.rows_measured) + ", \"rows_skipped\": " + std::to_string(s.meta.rows_skipped) +
         ", \"rows_retained\": " + std::to_string(s.meta.summary_only ? (int64_t) 0 : (int64_t) s.rows.size()) +
         ", \"recompose_retained\": " + std::to_string(s.meta.summary_only ? (int64_t) 0 : (int64_t) s.recompose.size()) +
         "},\n";

    Buckets b;
    collect_buckets(s, b);
    std::vector<double> all_ab, all_rel, rec_ab, rec_rel;
    for (const RowRec& rr : s.rows) {
        all_ab.push_back(rr.a_vs_b.max_abs);
        all_rel.push_back(rr.a_vs_b.max_rel);
    }
    for (const RowRec& rr : s.recompose) {
        rec_ab.push_back(rr.arm_vs_a.max_abs);
        rec_rel.push_back(rr.arm_vs_a.max_rel);
    }
    o += "  \"aggregate\": {\n    \"mmq_vs_helper\": {";
    agg_json(o, "max_abs", aggregate(all_ab));
    o += ",\n    ";
    agg_json(o, "max_rel", aggregate(all_rel));
    o += "},\n    \"mmq_arm_vs_mmq\": {";
    agg_json(o, "max_abs", aggregate(rec_ab));
    o += ",\n    ";
    agg_json(o, "max_rel", aggregate(rec_rel));
    o += "}\n  },\n";

    {
        // The energy-weighted tensor NMSE of the SAME two populations the aggregate above describes.  This
        // is sum(energy)/sum(energy) over the rows, NOT a mean of the per-row l2_rel, and no threshold.
        std::vector<RowDiff> pop_ab, pop_arm;
        pop_ab.reserve(s.rows.size());
        for (const RowRec& rr : s.rows) pop_ab.push_back(rr.a_vs_b);
        pop_arm.reserve(s.recompose.size());
        for (const RowRec& rr : s.recompose) pop_arm.push_back(rr.arm_vs_a);
        o += "  \"nmse\": {\n";
        o += "    \"definition\": \"" + std::string(kNmseDefinition) + "\",\n";
        o += "    \"states\": \"empty | ok | zero_reference_exact_match | "
             "zero_reference_nonzero_error_unbounded | invalid_nonfinite_rows | invalid_energy\",\n";
        o += "    \"note\": \"diagnostic only - no threshold, no verdict; a null nmse is a named "
             "non-numeric state, never agreement\",\n";
        o += "    \"mmq_vs_helper\": " +
             nmse_json("rows[].a_vs_b", "side a = the MMQ group product (Side A)", nmse_diagnostic(pop_ab)) +
             ",\n";
        o += "    \"mmq_arm_vs_mmq\": " + nmse_json("recompose[].arm_vs_a", "side a = the arm's MMQ row",
                                                    nmse_diagnostic(pop_arm)) + "\n";
        o += "  },\n";
    }

    o += "  \"buckets\": [";
    {
        size_t i = 0;
        for (const auto& kv : b.ab) {
            if (i++) o += ", ";
            o += "{\"key\": \"" + json_escape(kv.first) + "\", \"max_abs\": ";
            agg_json_body(o, aggregate(kv.second));
            o += ", \"max_rel\": ";
            agg_json_body(o, aggregate(b.rel[kv.first]));
            o += "}";
        }
    }
    o += "],\n";

    std::vector<TopRow> by_rel, by_abs;
    for (const RowRec& rr : s.rows) {
        by_rel.push_back({row_key(rr), rr.a_vs_b.max_rel});
        by_abs.push_back({row_key(rr), rr.a_vs_b.max_abs});
    }
    std::stable_sort(by_rel.begin(), by_rel.end(), by_value_then_key);
    std::stable_sort(by_abs.begin(), by_abs.end(), by_value_then_key);
    o += "  \"worst\": {\"by_max_rel\": [";
    for (size_t i = 0; i < by_rel.size() && i < 20; ++i) {
        if (i) o += ", ";
        o += "{\"key\": \"" + json_escape(by_rel[i].key) + "\", \"value\": " + num(by_rel[i].value) + "}";
    }
    o += "], \"by_max_abs\": [";
    for (size_t i = 0; i < by_abs.size() && i < 20; ++i) {
        if (i) o += ", ";
        o += "{\"key\": \"" + json_escape(by_abs[i].key) + "\", \"value\": " + num(by_abs[i].value) + "}";
    }
    o += "]},\n";

    o += "  \"notes\": [";
    for (size_t i = 0; i < s.meta.notes.size(); ++i) {
        if (i) o += ", ";
        o += "\"" + json_escape(s.meta.notes[i]) + "\"";
    }
    o += "]";

    if (!s.meta.summary_only) {
        o += ",\n  \"rows\": [";
        for (size_t i = 0; i < s.rows.size(); ++i) {
            const RowRec& rr = s.rows[i];
            o += (i ? ",\n     " : "\n     ");
            o += "{\"fixture\": \"" + json_escape(rr.fixture) + "\", \"fixture_index\": " +
                 std::to_string(rr.fixture_index) + ", \"layer\": " + std::to_string(rr.layer) +
                 ", \"expert\": " + std::to_string(rr.expert) + ", \"token\": " + std::to_string(rr.token) +
                 ", \"pair\": " + std::to_string(rr.pair) + ", \"gu_type\": " + std::to_string(rr.gu_type) +
                 ", \"d_type\": " + std::to_string(rr.d_type) + ", \"has_arm\": " +
                 std::string(rr.has_arm ? "true" : "false") + ", \"a_vs_b\": " + diff_json(rr.a_vs_b) +
                 ", \"arm_vs_a\": " + diff_json(rr.arm_vs_a) + "}";
        }
        o += "\n  ],\n  \"recompose\": [";
        for (size_t i = 0; i < s.recompose.size(); ++i) {
            const RowRec& rr = s.recompose[i];
            o += (i ? ",\n     " : "\n     ");
            o += "{\"fixture\": \"" + json_escape(rr.fixture) + "\", \"fixture_index\": " +
                 std::to_string(rr.fixture_index) + ", \"layer\": " + std::to_string(rr.layer) +
                 ", \"expert\": " + std::to_string(rr.expert) + ", \"token\": " + std::to_string(rr.token) +
                 ", \"pair\": " + std::to_string(rr.pair) + ", \"gu_type\": " + std::to_string(rr.gu_type) +
                 ", \"d_type\": " + std::to_string(rr.d_type) + ", \"arm_vs_a\": " + diff_json(rr.arm_vs_a) + "}";
        }
        o += "\n  ]";
    }
    o += "\n}\n";
    return o;
}

std::string to_table(const Report& r, int64_t worst_n) {
    Report s = r;
    sort_rows(s);
    std::string o;
    char line[512];
    std::snprintf(line, sizeof line, "# helper-vs-MMQ prompt rows - %s\n# pack %s\n# activations %s\n",
                  s.meta.harness_version.c_str(), s.meta.pack_dir.c_str(), s.meta.activation_mode.c_str());
    o += line;
    std::snprintf(line, sizeof line,
                  "# eps %s | layers [%lld,%lld] | max_tokens %lld | arm_groups %s | summary_only %s\n",
                  num(s.meta.eps).c_str(), (long long) s.meta.layer_lo, (long long) s.meta.layer_hi,
                  (long long) s.meta.max_tokens, s.meta.arm_groups ? "yes" : "no",
                  s.meta.summary_only ? "yes" : "no");
    o += line;
    std::snprintf(line, sizeof line,
                  "# helper: resident %lld of %lld requested pairs, %lld bytes, free %lld MiB, prompt_rows %s\n",
                  (long long) s.meta.helper.resident, (long long) s.meta.helper.requested_pairs,
                  (long long) s.meta.helper.bytes, (long long) s.meta.helper.free_mib,
                  s.meta.helper.prompt_rows ? "yes" : "NO");
    o += line;
    o += "# NO TOLERANCE IS APPLIED HERE: the band belongs to the user and the parent applies it after the run.\n\n";

    std::vector<double> ab, rel, abR, relR;
    for (const RowRec& rr : s.rows) {
        ab.push_back(rr.a_vs_b.max_abs);
        rel.push_back(rr.a_vs_b.max_rel);
    }
    // The arm term is the group recomposition on rows the arm keeps local (design line 22).
    for (const RowRec& rr : s.recompose) {
        abR.push_back(rr.arm_vs_a.max_abs);
        relR.push_back(rr.arm_vs_a.max_rel);
    }
    std::snprintf(line, sizeof line, "%-22s %8s %12s %12s %12s %12s %12s %12s\n", "metric", "rows", "p50",
                  "p90", "p99", "p99.9", "max", "mean");
    o += line;
    auto row = [&](const char* name, const Agg& a) {
        std::snprintf(line, sizeof line, "%-22s %8lld %12s %12s %12s %12s %12s %12s\n", name,
                      (long long) a.count, num(a.p50).c_str(), num(a.p90).c_str(), num(a.p99).c_str(),
                      num(a.p999).c_str(), num(a.max).c_str(), num(a.mean).c_str());
        o += line;
    };
    row("mmq vs helper abs", aggregate(ab));
    row("mmq vs helper rel", aggregate(rel));
    if (!abR.empty()) {
        row("recompose abs", aggregate(abR));
        row("recompose rel", aggregate(relR));
    }

    // The energy-weighted tensor NMSE of the same two populations: numerator, denominator, counts and the
    // named state.  A non-numeric state prints `-`, never a 0 that reads like a pass.
    std::vector<RowDiff> pop_ab, pop_arm;
    for (const RowRec& rr : s.rows) pop_ab.push_back(rr.a_vs_b);
    for (const RowRec& rr : s.recompose) pop_arm.push_back(rr.arm_vs_a);
    o += "\n# tensor NMSE diagnostic = sum(error energy)/sum(reference energy), reference = side a\n";
    o += "# energy-weighted over the population, NOT a mean of per-row ratios; no threshold is applied\n";
    std::snprintf(line, sizeof line, "%-22s %8s %8s %8s %16s %16s %14s %s\n", "population", "rows",
                  "ref0", "nonfin", "energy_error", "energy_ref", "nmse", "state");
    o += line;
    auto nmse_row = [&](const char* name, const NmseDiag& d) {
        std::snprintf(line, sizeof line, "%-22s %8lld %8lld %8lld %16s %16s %14s %s\n", name,
                      (long long) d.rows, (long long) d.rows_ref_zero, (long long) d.rows_nonfinite,
                      num(d.energy_error).c_str(), num(d.energy_ref).c_str(),
                      d.has_value ? num(d.nmse).c_str() : "-", nmse_state_name(d.state));
        o += line;
    };
    nmse_row("mmq vs helper", nmse_diagnostic(pop_ab));
    nmse_row("mmq arm vs mmq", nmse_diagnostic(pop_arm));

    Buckets b;
    collect_buckets(s, b);
    o += "\n# per format (mmq vs helper)\n";
    std::snprintf(line, sizeof line, "%-22s %8s %12s %12s %12s %12s\n", "format", "rows", "p90 abs", "p99 abs",
                  "p99 rel", "max rel");
    o += line;
    for (const auto& kv : b.ab) {
        if (kv.first.rfind("format:", 0) != 0) continue;
        const Agg a = aggregate(kv.second), e = aggregate(b.rel[kv.first]);
        std::snprintf(line, sizeof line, "%-22s %8lld %12s %12s %12s %12s\n", kv.first.c_str() + 7,
                      (long long) a.count, num(a.p90).c_str(), num(a.p99).c_str(), num(e.p99).c_str(),
                      num(e.max).c_str());
        o += line;
    }
    o += "\n# per layer (mmq vs helper)\n";
    std::snprintf(line, sizeof line, "%-22s %8s %12s %12s %12s %12s\n", "layer", "rows", "p90 abs", "p99 abs",
                  "p99 rel", "max rel");
    o += line;
    for (const auto& kv : b.ab) {
        if (kv.first.rfind("layer:", 0) != 0) continue;
        const Agg a = aggregate(kv.second), e = aggregate(b.rel[kv.first]);
        std::snprintf(line, sizeof line, "%-22s %8lld %12s %12s %12s %12s\n", kv.first.c_str(),
                      (long long) a.count, num(a.p90).c_str(), num(a.p99).c_str(), num(e.p99).c_str(),
                      num(e.max).c_str());
        o += line;
    }
    o += "\n# worst rows by max_rel (row key = fixture/L<layer>/e<expert>/t<token>/p<pair>)\n";
    std::vector<TopRow> by_rel;
    for (const RowRec& rr : s.rows) by_rel.push_back({row_key(rr), rr.a_vs_b.max_rel});
    std::stable_sort(by_rel.begin(), by_rel.end(), by_value_then_key);
    std::snprintf(line, sizeof line, "%-56s %14s\n", "row", "max_rel");
    o += line;
    for (size_t i = 0; i < by_rel.size() && (int64_t) i < worst_n; ++i) {
        std::snprintf(line, sizeof line, "%-56s %14s\n", by_rel[i].key.c_str(), num(by_rel[i].value).c_str());
        o += line;
    }
    if (!s.meta.notes.empty()) {
        o += "\n# notes\n";
        for (const std::string& n : s.meta.notes) o += "  " + n + "\n";
    }
    return o;
}

// ============================== 5b. the CPU self-test document ==============================
//
// Deterministic synthetic rows with HAND-COMPUTABLE energies (see the arithmetic in the comments below and
// its mirror in tests/terminal/test_p2_report_json.py).  No GPU, no helper, no pack, no model file, no
// wall-clock time.  This document exists so a STRICT JSON PARSER - not a substring check - can validate
// the emitter, and so the NMSE states are exercised end to end.  It is never written over a run's evidence.
namespace {

void st_meta(Report& r, const char* variant) {
    r.meta.harness_version = "p2-row-measure-selftest-1";
    r.meta.pack_dir = "/selftest/no-pack";
    r.meta.socket = "/selftest/no-socket";
    r.meta.activation_mode = std::string("selftest:") + variant;
    r.meta.blob_file = "/selftest/no-blob";
    r.meta.blob_mapping = "selftest";
    r.meta.argv_joined = std::string("--selftest ") + variant;
    r.meta.eps = kEpsDefault;
    r.meta.layer_lo = 32;
    r.meta.layer_hi = 47;
    r.meta.rows_expected = (int64_t) r.rows.size();
    r.meta.rows_measured = (int64_t) r.rows.size();
    r.meta.rows_skipped = 0;
    r.meta.helper.note = "selftest: no helper was contacted";
    r.meta.helper.prompt_rows = true;
    r.meta.notes.push_back(kSelfTestNote);
    r.meta.notes.push_back(std::string("variant: ") + variant);
    r.meta.notes.push_back("no tolerance is applied and no verdict is expressible in this document");
}

void st_format(Report& r, int32_t layer, int32_t gu, int32_t d) {
    const FormatInfo f{layer, gu, d};
    r.meta.formats.push_back(f);
}

/// One self-test row: the fixture/layer/format keys plus the two rows' floats.  `n` is the row length.
void st_row(std::vector<RowRec>& into, const char* fixture, int64_t fixture_index, int32_t layer,
            int32_t expert, int32_t token, int32_t pair, int32_t gu, int32_t d, const float* a,
            const float* b, int64_t n, bool arm_pair) {
    RowRec rr;
    rr.fixture = fixture;
    rr.fixture_index = fixture_index;
    rr.layer = layer;
    rr.expert = expert;
    rr.token = token;
    rr.pair = pair;
    rr.gu_type = gu;
    rr.d_type = d;
    rr.has_arm = arm_pair;
    if (arm_pair) rr.arm_vs_a = diff_rows(a, b, n, kEpsDefault);
    else rr.a_vs_b = diff_rows(a, b, n, kEpsDefault);
    into.push_back(rr);
}

}  // namespace

int make_cpu_selftest_report(const std::string& variant, Report& out, std::string& err) {
    out = Report();
    // n = 4 rows keep every energy below computable by hand in one line of arithmetic.
    constexpr int64_t k = 4;
    const float z0[k] = {0.0f, 0.0f, 0.0f, 0.0f};
    if (variant == "ok") {
        // Reference energies deliberately UNEQUAL (30, 100, 1) so the energy-weighted ratio and an
        // unweighted mean of per-row ratios cannot agree:
        //   r0 a={1,2,3,4} b={1,2,3,4}  exact: err 0        ref 1+4+9+16 = 30
        //   r1 a={10,0,0,0} b={11,0,0,0}                   err 1        ref 100
        //   r2 a={0,1,0,0}  b={0,1.5,0,0}                  err 0.25     ref 1
        // sum(err) = 1.25 ; sum(ref) = 131 ; nmse = 1.25/131 = 0.009541984732824427...
        // unweighted mean of l2_rel^2 = (0 + 0.01 + 0.25)/3 = 0.08666666666666667 (DIFFERENT, by design).
        const float a0[k] = {1.f, 2.f, 3.f, 4.f}, b0[k] = {1.f, 2.f, 3.f, 4.f};
        const float a1[k] = {10.f, 0.f, 0.f, 0.f}, b1[k] = {11.f, 0.f, 0.f, 0.f};
        const float a2[k] = {0.f, 1.f, 0.f, 0.f}, b2[k] = {0.f, 1.5f, 0.f, 0.f};
        st_row(out.rows, "pinned-sample", 0, 32, 7, 0, 0, 21, 42, a0, b0, k, false);
        st_row(out.rows, "pinned-sample", 0, 33, 9, 1, 1, 21, 42, a1, b1, k, false);
        st_row(out.rows, "decode-list", 2, 34, 3, 0, 0, 17, 20, a2, b2, k, false);
        // the arm population (rows the arm keeps local): a={2,0,0,0} b={2,0,0,1} -> err 1, ref 4, nmse 0.25
        const float a3[k] = {2.f, 0.f, 0.f, 0.f}, b3[k] = {2.f, 0.f, 0.f, 1.f};
        st_row(out.recompose, "pinned-sample", 0, 32, 11, 2, 32, 21, 42, a3, b3, k, true);
    } else if (variant == "empty") {
        // an unmeasured comparison: NO rows in either population.
    } else if (variant == "zero-ref-nonzero") {
        // zero reference, NONZERO candidate: the ratio is unbounded, so the value is null - never 0,
        // and max_rel is 1 for every row (row_amax takes the larger side, the eps floor never divides).
        const float c0[k] = {1.f, 2.f, 3.f, 4.f}, c1[k] = {0.f, 5.f, 0.f, 0.f};
        st_row(out.rows, "pinned-sample", 0, 32, 7, 0, 0, 21, 42, z0, c0, k, false);
        st_row(out.rows, "decode-list", 2, 33, 9, 1, 1, 17, 20, z0, c1, k, false);
    } else if (variant == "zero-ref-exact") {
        // every row is all zeros on both sides: 0/0, a NAMED exact-zero state.
        st_row(out.rows, "pinned-sample", 0, 32, 7, 0, 0, 21, 42, z0, z0, k, false);
        st_row(out.rows, "decode-list", 2, 33, 9, 1, 1, 17, 20, z0, z0, k, false);
    } else if (variant == "nonfinite") {
        // one row exact and finite, one row with a NaN candidate, one row with an Infinity reference:
        // the population REFUSES the ratio and counts the rows.
        const float a0[k] = {1.f, 2.f, 3.f, 4.f};
        const float nanv = std::nan("selftest");
        const float infv = (std::numeric_limits<double>::infinity)();
        const float n0[k] = {1.f, 2.f, 3.f, nanv};
        const float i0[k] = {infv, 0.f, 0.f, 0.f};
        st_row(out.rows, "pinned-sample", 0, 32, 7, 0, 0, 21, 42, a0, a0, k, false);
        st_row(out.rows, "pinned-sample", 0, 33, 9, 1, 1, 21, 42, a0, n0, k, false);
        st_row(out.rows, "decode-list", 2, 34, 3, 0, 2, 17, 20, i0, a0, k, false);
        out.meta.notes.push_back("selftest: nonfinite inputs, the ratio must be refused, never reported 0");
    } else {
        err = "selftest: unknown variant '" + variant + "' (ok|empty|zero-ref-nonzero|zero-ref-exact|"
              "nonfinite)";
        return 3;
    }
    // The declaration must match the actual rows, including variants with different layer/format pairs.
    for (const auto* population : {&out.rows, &out.recompose}) {
        for (const RowRec& rr : *population) {
            const bool present = std::any_of(out.meta.formats.begin(), out.meta.formats.end(),
                [&](const FormatInfo& f) { return f.layer == rr.layer && f.gu_type == rr.gu_type &&
                                                 f.d_type == rr.d_type; });
            if (!present) st_format(out, rr.layer, rr.gu_type, rr.d_type);
        }
    }
    // fixtures_used: per fixture, (tokens this self-test uses, gate rows retained for that fixture)
    for (const RowRec& rr : out.rows) {
        auto it = out.meta.fixtures_used.find(rr.fixture);
        if (it == out.meta.fixtures_used.end())
            out.meta.fixtures_used[rr.fixture] = {1, 1};
        else
            it->second.second += 1;
    }
    st_meta(out, variant.c_str());
    return 0;
}

// ============================== 6. the pack's blob source ==============================
//
// Mirrors the CUDA helper, file by file and offset by offset - see the header's section 6 for the three
// facts and where each comes from.  Nothing here invents an interpretation: the join, the header name and
// the per-role slices are the helper's own (helper_main.cpp:708-712, tools/iq_pack.py:479-486,
// helper_main.cpp:226-247).

namespace {

/// A path the harness can read as a blob file: a regular file with bytes in it (a directory, a missing
/// file and an empty file are all "not there" - the same verdict the helper's ::open/fstat makes).
bool regular_readable(const std::string& path) {
    struct stat st;
    if (::stat(path.c_str(), &st) != 0) return false;
    return S_ISREG(st.st_mode) && st.st_size > 0;
}

}  // namespace

std::string join_experts_path(const std::string& pack_dir, const std::string& experts) {
    std::string p = pack_dir;
    if (!p.empty() && p.back() != '/') p += "/";
    p += experts;
    return p;
}

std::string header_blob_name(const std::string& header_line) {
    static const char tag[] = "absolute offsets in ";
    const size_t at = header_line.find(tag);
    if (at == std::string::npos) return std::string();
    std::string name;
    for (size_t i = at + sizeof(tag) - 1; i < header_line.size(); ++i) {
        const char c = header_line[i];
        if (c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == ',' || c == ')') break;
        name.push_back(c);
    }
    return name;
}

std::string pack_header_blob_name(const std::string& pack_dir) {
    std::ifstream in(pack_dir + "/native_experts.txt");
    if (!in) return std::string();
    std::string line;
    while (std::getline(in, line)) {
        const std::string name = header_blob_name(line);
        if (!name.empty()) return name;
    }
    return std::string();
}

bool layout_maps_gguf_roles(const kernels::cpu::ExpertLayout& lay) {
    // The helper's condition verbatim (helper_main.cpp:226): per-role slices, no per-layer shard named.
    return !lay.gguf_off.empty() && lay.gguf_off.size() == (size_t) (3 * lay.n_layers) && lay.gguf_file.empty();
}

int resolve_blob_source(const std::string& pack_dir, const std::string& experts_arg,
                        const kernels::cpu::ExpertLayout& lay, BlobSource& out, std::string& err) {
    std::vector<std::string> tried;
    if (!experts_arg.empty()) {
        out.path = join_experts_path(pack_dir, experts_arg);
        tried.push_back(out.path);
        if (regular_readable(out.path)) {
            out.per_role = layout_maps_gguf_roles(lay);   // the helper maps the roles for this layout
            return 0;
        }
        err = "cannot open the blob named by --experts: " + out.path;
        return 3;
    }
    const std::string packed = join_experts_path(pack_dir, "experts.bin");
    tried.push_back(packed);
    if (regular_readable(packed)) {
        out.path = packed;
        out.per_role = false;                              // a packed blob keeps blob_offset()
        return 0;
    }
    const std::string name = pack_header_blob_name(pack_dir);
    if (!name.empty()) {
        const std::string gguf = join_experts_path(pack_dir, "../" + name);
        tried.push_back(gguf);
        if (regular_readable(gguf)) {
            out.path = gguf;
            out.per_role = layout_maps_gguf_roles(lay);
            return 0;
        }
    }
    std::string list;
    for (size_t i = 0; i < tried.size(); ++i) list += (i ? "   and   " : "") + tried[i];
    err = "no expert blob file: tried " + list +
          (name.empty() ? "  (native_experts.txt names no GGUF file, so there is nothing beside the "
                          "pack to try: pass --experts PATH, the helper's own spelling)"
                        : "  (native_experts.txt names " + name + " beside the pack; pass --experts PATH "
                          "to name the file the helper maps)");
    return 3;
}

bool blob_role_bytes(const kernels::cpu::ExpertLayout& lay, int64_t layer, uint64_t per[3]) {
    if (layer < 0 || (size_t) layer >= lay.fmt.size()) return false;
    const auto& pf = lay.fmt[(size_t) layer];
    // The helper's `per[3]` (helper_main.cpp:235-236): gate, up, down - and its native_expert_layout's
    // gu_row/d_row are the same ggml row bytes the layout's NativeFmt carries (native_fmt's f.gu_row /
    // f.d_row, and f.up_off == n_ff*gu_row, f.down_off == 2*f.up_off: the same formulas).
    per[0] = (uint64_t) pf.n_ff * (uint64_t) pf.gu_row;
    per[1] = (uint64_t) pf.n_ff * (uint64_t) pf.gu_row;
    per[2] = (uint64_t) pf.n_embd * (uint64_t) pf.d_row;
    return true;
}

uint64_t blob_layer_end(const kernels::cpu::ExpertLayout& lay, const BlobSource& src, int64_t layer) {
    const int64_t ne = lay.n_expert > 0 ? lay.n_expert : kNE;
    if (layer < 0 || (size_t) layer >= lay.fmt.size() || ne < 1) return 0;
    if (!src.per_role)
        return lay.blob_offset(layer, 0) + lay.blob_bytes(layer) * (uint64_t) ne;
    uint64_t per[3];
    if (!blob_role_bytes(lay, layer, per)) return 0;
    uint64_t end = 0;
    for (int r = 0; r < 3; ++r) {
        if (lay.gguf_off.size() < (size_t) (3 * layer + 3)) return 0;
        const uint64_t at = lay.gguf_off[(size_t) (3 * layer + r)] + (uint64_t) (ne - 1) * per[r] + per[r];
        if (at > end) end = at;
    }
    return end;
}

bool read_blob_row(std::istream& f, const kernels::cpu::ExpertLayout& lay, const BlobSource& src,
                   int64_t layer, int64_t expert, uint8_t* dst, uint64_t end, std::string& err) {
    if (layer < 0 || (size_t) layer >= lay.fmt.size()) {
        err = "layer " + std::to_string(layer) + " is outside the pack's layout";
        return false;
    }
    if (expert < 0) {
        err = "expert " + std::to_string(expert) + " is negative";
        return false;
    }
    const uint64_t nb = lay.blob_bytes(layer);
    if (!src.per_role) {
        const uint64_t at = lay.blob_offset(layer, (uint64_t) expert);
        if (at + nb > end) {
            err = "the blob of layer " + std::to_string(layer) + " expert " + std::to_string(expert) +
                  " at offset " + std::to_string(at) + " runs past the end of " + src.path;
            return false;
        }
        f.seekg((std::streamoff) at);
        if (!f.read((char*) dst, (std::streamsize) nb)) {
            err = "reading the blob of layer " + std::to_string(layer) + " expert " + std::to_string(expert) +
                  " at offset " + std::to_string(at) + " of " + src.path;
            return false;
        }
        return true;
    }
    uint64_t per[3];
    if (!blob_role_bytes(lay, layer, per)) {
        err = "layer " + std::to_string(layer) + " has no formats for its role slices";
        return false;
    }
    if (per[0] + per[1] + per[2] != nb) {   // the helper's own check (helper_main.cpp:246)
        err = "the role slices of layer " + std::to_string(layer) + " sum to " + std::to_string(per[0] + per[1] + per[2]) +
              " B, not its blob of " + std::to_string(nb) + " B (an NVFP4-tailed pack: outside this harness)";
        return false;
    }
    uint64_t at = 0;
    for (int r = 0; r < 3; ++r) {
        if (lay.gguf_off.size() < (size_t) (3 * layer + 3)) {
            err = "layer " + std::to_string(layer) + " has no GGUF offsets for role " + std::to_string(r);
            return false;
        }
        const uint64_t roff = lay.gguf_off[(size_t) (3 * layer + r)] + (uint64_t) expert * per[r];
        if (roff + per[r] > end) {
            err = "role " + std::to_string(r) + " of layer " + std::to_string(layer) + " expert " +
                  std::to_string(expert) + " at offset " + std::to_string(roff) + " runs past the end of " + src.path;
            return false;
        }
        f.seekg((std::streamoff) roff);
        if (!f.read((char*) (dst + at), (std::streamsize) per[r])) {
            err = "reading role " + std::to_string(r) + " of layer " + std::to_string(layer) + " expert " +
                  std::to_string(expert) + " at offset " + std::to_string(roff) + " of " + src.path;
            return false;
        }
        at += per[r];
    }
    return true;
}

// ============================== 7. the run's per-layer sizing and the plan's claims ==============================

size_t stage_ring_bytes(uint64_t blob_bytes) { return (size_t) ((uint64_t) kStageRingSlots * blob_bytes); }

uint64_t max_blob_bytes(const kernels::cpu::ExpertLayout& lay, int64_t lo, int64_t hi) {
    uint64_t mx = 0;
    for (int64_t l = lo; l <= hi; ++l)
        if (l >= 0 && (size_t) l < lay.fmt.size()) mx = std::max(mx, lay.blob_bytes(l));
    return mx;
}

// ---- 7b. the per-layer MMQ buffer sizes and the extents the ops reach (fix5) ----

size_t mmq_group_gu_bytes(size_t mmq_gub, size_t over_read) {
    return (size_t) kMmqGroup * mmq_gub + std::max(kMmqTail, over_read);
}

size_t mmq_group_d_bytes(size_t mmq_db, size_t over_read) {
    return (size_t) kMmqGroup * mmq_db + std::max(kMmqTail, over_read);
}

size_t mmq_tail_words_bytes() { return (size_t) kMmqGroup * 16u; }

size_t gather_write_bytes(int ngx, size_t slot_bytes, size_t zero_bytes) {
    return (size_t) (ngx > 0 ? ngx : 0) * slot_bytes + zero_bytes;
}

size_t mmq_weight_read_bytes(int ngx, size_t slot_bytes, size_t over_read) {
    return (size_t) (ngx > 0 ? ngx : 0) * slot_bytes + over_read;
}

int64_t mmq_act_tail_rows() { return 16; }

// ---- 7c. the per-op SYNC attribution instruments (fix8) ----
//
// The STRINGS only; the SYNC that produces them is in p2_measure_main.cpp.  They are here so the CPU suite
// (section 14) pins the exact shape of every line a live fault prints, with no GPU in sight.

std::string op_label(const char* op, long long q, long long slot, long long entry) {
    char buf[128];
    if (entry < 0) std::snprintf(buf, sizeof buf, "%s", op);
    else std::snprintf(buf, sizeof buf, "%s q%lld slot%lld entry%lld", op, q, slot, entry);
    return std::string(buf);
}

const char* cuda_memory_type_name(int type) {
    switch (type) {
        case 0: return "unregistered-host";   // cudaMemoryTypeUnregistered: ordinary malloc'd host memory
        case 1: return "host";                // cudaMemoryTypeHost: registered/pinned host memory
        case 2: return "device";              // cudaMemoryTypeDevice: cudaMalloc'd device memory
        case 3: return "managed";             // cudaMemoryTypeManaged: cudaMallocManaged
        default: break;
    }
    static std::string unknown = "unknown(0)";
    unknown = "unknown(" + std::to_string(type) + ")";
    return unknown.c_str();
}

std::string format_fault_line(const char* fixture, int64_t layer, const OpProbe& p, const char* err) {
    const std::string lbl = op_label(p.op, p.q, p.slot, p.entry);
    char head[512];
    std::snprintf(head, sizeof head, "FAULT-AT %s L%lld %s: %s | src %p dst %p n %llu stream %p",
                  (fixture && *fixture) ? fixture : "?", (long long) layer, lbl.c_str(),
                  (err && *err) ? err : "?", (const void*) p.src, (const void*) p.dst,
                  (unsigned long long) p.bytes, (const void*) p.stream);
    std::string out = head;
    if (p.src != nullptr) {
        out += " src-attr ";
        out += (p.src_attr && *p.src_attr) ? p.src_attr : "?";
        out += "(";
        out += (p.src_rc && *p.src_rc) ? p.src_rc : "?";
        out += ")";
    }
    if (p.dst != nullptr) {
        out += " dst-attr ";
        out += (p.dst_attr && *p.dst_attr) ? p.dst_attr : "?";
        out += "(";
        out += (p.dst_rc && *p.dst_rc) ? p.dst_rc : "?";
        out += ")";
    }
    if (p.w_last > 0) {   // fix9: a product launch's REAL write span (section 7d) - the pointer printed
                          // above is IN-bounds of its buffer; the kernel writes at ids[col] rows from it
        char w[128];
        std::snprintf(w, sizeof w, " write-rows [%lld,%lld) alloc %llu",
                      (long long) p.w_first, (long long) p.w_last, (unsigned long long) p.alloc);
        out += w;
    }
    return out;
}

std::string format_op_summary(const char* fixture, const OpWindow& w) {
    long long qmax = 0;
    for (const auto& g : w.wins) qmax = std::max(qmax, g.second);
    char head[512];
    std::snprintf(head, sizeof head, "OPSUM %s L%lld T %lld entries %lld rows %lld order %lld groups %lld "
                                 "qcount-max %lld nr_max %lld blob %llu ring %llu gu %llu down %llu stream %p "
                                 "ring-base %p win",
                  (fixture && *fixture) ? fixture : "?", (long long) w.layer, (long long) w.T,
                  (long long) w.entries, (long long) w.rows, (long long) w.order_n, (long long) w.groups,
                  qmax, (long long) w.nr_max, (unsigned long long) w.blob, (unsigned long long) w.ring,
                  w.gu_bytes, w.down_bytes, (const void*) w.stream, (const void*) w.ring_base);
    std::string out = head;
    for (size_t i = 0; i < w.wins.size(); ++i) {
        char g[96];
        std::snprintf(g, sizeof g, " %lld:%lld[%lld/%lld]", w.wins[i].first, w.wins[i].second,
                      i < w.r0.size() ? w.r0[i] : -1, i < w.nr.size() ? w.nr[i] : -1);
        out += g;
    }
    return out;
}

int mark_claim_entries(const Activations& act, const core::ExpertHelperPromptPlan& plan,
                       std::vector<uint8_t>& claims, std::string& err) {
    const int64_t nent = act.T * kK;
    claims.assign((size_t) (nent > 0 ? nent : 1), 0);          // the ENTRY index space
    for (int64_t j = 0; j < plan.rows(); ++j) {
        const int32_t t = plan.pair_token[(size_t) j], e = plan.pair_expert[(size_t) j];
        if (t < 0 || t >= (int32_t) act.T || e < 0 || e >= (int32_t) kNE) {
            err = "pair " + std::to_string(j) + " of the plan names token " + std::to_string(t) +
                  " expert " + std::to_string(e) + ", outside the activation set";
            return eh::kErrGeometry;
        }
        // ONE mapping for every (token, expert) pair in this harness: the routed-slot lookup
        // (pair_entry_lookup).  claims[t*kK + e] would mark a DIFFERENT token's entry whenever the expert
        // id is >= kK - the first live crash of 2026-10-29 (byte 3061 of a 2560-byte array).
        int64_t entry = -1;
        std::string why;
        if (pair_entry_lookup(act, t, e, entry, why) != eh::kOk) {
            err = "pair " + std::to_string(j) + " of the plan claims (token " + std::to_string(t) +
                  ", expert " + std::to_string(e) + "), but " + why + " - the plan and the routed ids do "
                  "not describe the same layer";
            return eh::kErrGeometry;
        }
        claims[(size_t) entry] = 1;                            // entry < nent, by construction
    }
    return eh::kOk;
}

// ---- 6c. the GGUF truth gate (fix7) ----------------------------------------------------------
//
// native_experts.txt is the per-layer shape table BOTH the engine's loader and the harness resolve
// from: the loader keys every line by its layer index and refuses a line whose blob disagrees with
// its own format geometry (expert_layout.cpp), so the table is index-keyed and self-consistent.
// What no loader sees is the table's AGREEMENT WITH THE MODEL.  The 2026-10-29 live anomaly
// (attempt 11 STAGE lines): the pack's L33 line names 21/42 (blob 1,868,800 B) and L34's 21/20
// (2,329,600 B), while the GGUF's own tensor metadata says L33 is the 16/42 shape (1,305,600 B)
// and L34 the 17/20 shape (1,868,800 B): the packer wrote the per-layer type columns keyed on the
// GGUF tensor ENCOUNTER order, not on the layer index, and the loader's self-consistency check
// cannot catch it (its geometry math is fed the same wrong types).  A harness running such a pack
// reads the WRONG byte ranges (every L33/L34 record is numerically invalid) and the live crash
// follows from the same corruption.  The engine's own expert source enforces the model's truth
// before it loads (`check_experts_gguf`, src/core/expert_source.cpp:5514); this gate is the same
// contract for the harness, one typed line per inspected layer, refusing on the first divergence.
bool check_gguf_truth(const std::string& native_gguf, const kernels::cpu::ExpertLayout& lay,
                      int64_t lo, int64_t hi, std::vector<LayerTruth>& out, std::string& err) {
    static const char* roles[3] = {"gate", "up", "down"};
    if (lo > hi) {
        err = "the layer window is empty";
        return false;
    }
    if (!lay.native) {
        err = "the layout is not a native (IQ) pack: no per-layer GGUF truth to check";
        return false;
    }
    if (!lay.gguf_off.empty() && lay.gguf_off.size() != (size_t) (3 * lay.n_layers)) {
        err = "native_experts.txt has GGUF offsets for " + std::to_string(lay.gguf_off.size() / 3) +
              " layers, the layout has " + std::to_string(lay.n_layers);
        return false;
    }
    if (!lay.gguf_file.empty()) {
        err = "the header names per-layer shard files (a v4 pack): the harness gate reads the --experts "
              "file only - run the check per shard (the engine's check_experts_gguf covers all shards)";
        return false;
    }
    const strata::GgufFile gg(native_gguf);
    const uint64_t n_exp = (uint64_t) lay.n_expert;
    for (int64_t l = lo; l <= hi; ++l) {
        if (l < 0 || l >= lay.n_layers) {
            err = "layer " + std::to_string(l) + " is outside the layout";
            return false;
        }
        const kernels::cpu::NativeFmt& f = lay.fmt[(size_t) l];
        const uint64_t blob = lay.blob_bytes(l);
        const uint64_t per[3] = {(uint64_t) f.up_off, (uint64_t) f.up_off, blob - (uint64_t) f.down_off};
        LayerTruth t;
        t.layer = l;
        t.gu_type = f.gu_type;
        t.d_type = f.d_type;
        t.header_blob = blob;
        uint64_t gguf_blob = 0;
        for (int r = 0; r < 3; ++r) {
            const std::string name = "blk." + std::to_string(l) + ".ffn_" + roles[r] + "_exps.weight";
            const strata::TensorInfo* ti = gg.find(name);
            const int want_type = r < 2 ? f.gu_type : f.d_type;
            if (ti == nullptr) {
                err = "layer " + std::to_string(l) + ": " + name + " is not in " + native_gguf +
                      " - the header names offsets in it, the tensor is not there";
                return false;
            }
            const uint64_t total = strata::tensor_payload_bytes(*ti);
            if (total == 0 || total % n_exp) {
                err = "layer " + std::to_string(l) + ": " + name + " of " + native_gguf + " has " +
                      std::to_string(total) + " B, not a whole " + std::to_string(n_exp) + "-expert stack";
                return false;
            }
            const uint64_t per_e = total / n_exp;
            if ((int) ti->type != want_type) {
                err = "layer " + std::to_string(l) + ": " + name + " of " + native_gguf + " is type " +
                      std::to_string(ti->type) + " (" + ti->type_name() + "), the header's line says type " +
                      std::to_string(want_type) + " (header: " + std::to_string(f.gu_type) + "/" +
                      std::to_string(f.d_type) + " blob " + std::to_string(blob) + " B)";
                return false;
            }
            if (ti->shape.size() < 3 || ti->shape[0] != (uint64_t) (r < 2 ? f.n_embd : f.n_ff) ||
                ti->shape[1] != (uint64_t) (r < 2 ? f.n_ff : f.n_embd) || ti->shape[2] != n_exp) {
                std::string dims;
                for (size_t i = 0; i < ti->shape.size(); ++i) {
                    if (i) dims += ", ";
                    dims += std::to_string(ti->shape[i]);
                }
                err = "layer " + std::to_string(l) + ": " + name + " of " + native_gguf + " is dims [" +
                      dims + "], the header's line wants [" + std::to_string(r < 2 ? f.n_embd : f.n_ff) +
                      ", " + std::to_string(r < 2 ? f.n_ff : f.n_embd) + ", " + std::to_string(n_exp) + "]";
                return false;
            }
            if (per_e != per[r]) {
                err = "layer " + std::to_string(l) + ": " + name + " of " + native_gguf + " holds " +
                      std::to_string(per_e) + " B per expert, the header's line says " + std::to_string(per[r]) +
                      " (header: " + std::to_string(f.gu_type) + "/" + std::to_string(f.d_type) + " blob " +
                      std::to_string(blob) + " B; gguf: " + std::to_string(ti->type) + "-typed stack " +
                      std::to_string(total) + " B)";
                return false;
            }
            if (!lay.gguf_off.empty() && gg.data_start() + ti->offset != lay.gguf_off[(size_t) (3 * l + r)]) {
                err = "layer " + std::to_string(l) + ": " + name + " of " + native_gguf + " starts at byte " +
                      std::to_string(gg.data_start() + ti->offset) + ", the header's line says " +
                      std::to_string(lay.gguf_off[(size_t) (3 * l + r)]);
                return false;
            }
            if (r == 0) {
                t.gguf_gu_type = (int) ti->type;
                gguf_blob += 2 * per_e;    // gate and up are equal-shaped (the role check above)
            }
            if (r == 2) {
                t.gguf_d_type = (int) ti->type;
                gguf_blob += per_e;
            }
        }
        t.gguf_blob = gguf_blob;
        if (gguf_blob != blob) {
            err = "layer " + std::to_string(l) + ": the GGUF's tensors make a " + std::to_string(gguf_blob) +
                  " B blob, the header's line says " + std::to_string(blob) + " B";
            return false;
        }
        out.push_back(t);
    }
    return true;
}

// ---- 7d. the MMQ product's dst write geometry (fix9) --------------------------------------------
//
// The kernel's addressing is `dst[ids_dst[col]*ld_dst + i]` with col from expert_bounds - see the section
// 7d comment in p2_measure.hpp.  These are the numbers the harness sizes and asserts with; the CPU suite
// (section 15) pins them for the live L34 17-group shape and its L33/L32 controls.

long long product_write_first_row(const ProductGeom& g) {
    return g.absolute_bounds ? g.r0 : 0;
}

long long product_write_last_row(const ProductGeom& g) {
    return product_write_first_row(g) + (g.nr > 0 ? g.nr : 0);
}

size_t product_write_bytes_from_dst(const ProductGeom& g) {
    return (size_t) product_write_last_row(g) * (size_t) (g.ld_dst > 0 ? g.ld_dst : 0) * sizeof(float);
}

bool product_write_fits(const ProductGeom& g, long long dst_rows) {
    const long long avail = dst_rows - g.dst_row_offset;      // the rows from the pointer to the buffer end
    return product_write_last_row(g) <= (avail > 0 ? avail : 0);
}

size_t mmq_gu_buffer_bytes(int64_t rows) {
    return (size_t) (rows > 0 ? rows : 0) * (size_t) (2 * kFF) * sizeof(float);     // ld_dst = 1280
}

size_t mmq_dm_buffer_bytes(int64_t rows) {
    return (size_t) (rows > 0 ? rows : 0) * (size_t) kN * sizeof(float);
}

std::string product_write_refusal(const char* which, bool absolute_bounds, long long j0, long long r0,
                                  long long nr, long long ld_dst, long long dst_row_offset,
                                  long long dst_rows, unsigned long long alloc_bytes) {
    const ProductGeom g{true, absolute_bounds, r0, nr, ld_dst, dst_row_offset};
    const long long avail = (dst_rows > dst_row_offset) ? (dst_rows - dst_row_offset) : 0;
    const long long last = product_write_last_row(g);
    const unsigned long long reach = (unsigned long long) product_write_bytes_from_dst(g);
    const unsigned long long room = (unsigned long long) avail * (unsigned long long) ld_dst * sizeof(float);
    char buf[512];
    std::snprintf(buf, sizeof buf,
                  "the %s product's dst write: group j0=%lld writes rows [%lld,%lld) from a dst pointer "
                  "%lld rows into a %lld-row buffer (%llu B), %s bounds - runs %llu B past the end",
                  (which && *which) ? which : "?", (long long) j0, (long long) product_write_first_row(g),
                  (long long) last, (long long) dst_row_offset, (long long) dst_rows, alloc_bytes,
                  absolute_bounds ? "absolute" : "group-relative",
                  (reach > room) ? (reach - room) : 0ULL);
    return std::string(buf);
}

}  // namespace p2
}  // namespace tools
}  // namespace strata
