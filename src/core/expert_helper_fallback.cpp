// src/core/expert_helper_fallback.cpp - R2 (opt-in CUDA expert helper): the request-tier CPU recompute.
//
// When the helper's FINISH fails mid-request, the rows it claimed at begin were left unwritten (the
// dispatch marked them kind 2 and zeroed them, exactly as it does for an in-process tier's rows).  This
// file recomputes EXACTLY the claimed (token, expert) rows on the CPU, and nothing else:
//
//   * the per-token activation is quantized by the SAME quantizer the dispatch's CPU path selects for
//     this pack and layer (act_quant_any / native_quant_act / act_quant_q8_1, see
//     src/core/expert_source.cpp:5010-5016) - quantized EVEN when the window had no CPU work at all;
//   * the jobs are assembled the way the CPU misses' jobs are assembled (src/core/expert_source.cpp's
//     job_of / jobs_multi pattern, blob from the same `d.src`), and run through the pool's own
//     run_split_multi / run_split_multi_native;
//   * d.job_of is reset for every touched expert, as every dispatch pass resets it.
//
// A recomputed row is what a CPU-served row always was - same source bytes, same kernels, no new
// dispatch semantics.  A blob the source cannot produce keeps the dispatch's latching failure path
// (d.failed + "the expert source could not produce a blob"): the fallback never invents a row.
//
// CPU-only by construction: this file calls the CPU pool and the CPU quantizers, no device call.  It
// is compiled into the same library as the dispatch; the proxy's finish seam (expert_source.cpp) is
// its only caller.
#include "strata/core/expert_source.hpp"

#include "strata/kernels/cpu/expert.hpp"
#include "strata/kernels/cpu/expert_layout.hpp"
#include "strata/kernels/cpu/native_expert.hpp"
#include "strata/kernels/cpu/pool.hpp"

#include <algorithm>
#include <cstring>

namespace strata::core {

bool expert_helper_recompute_claimed(ExpertDispatch& d, const float* x_f, const int32_t* ids,
                                     int64_t n_tok, int64_t k, const int32_t* claimed, int64_t n_claimed,
                                     bool native, int64_t layer, float* out) {
    using namespace strata::kernels::cpu;
    const int64_t n = n_tok * k;
    if (n_claimed <= 0) return true;
    if (n_tok < 1 || n_tok > MAXT || k != 10 || n > MAXT * 10) {
        d.failed = true;
        d.fail = "the helper fallback needs a verify window of at most 8 tokens of 10 experts";
        d.fail_layer = layer;
        return false;
    }
    const auto& lay = expert_layout();
    if (layer < 0 || layer >= (int64_t) lay.fmt.size()) {
        d.failed = true;
        d.fail = "the helper fallback asked for a layer the pack layout does not have";
        d.fail_layer = layer;
        return false;
    }
    // The claimed rows must be in range and the window's activations exist for their tokens.
    for (int64_t c = 0; c < n_claimed; ++c)
        if (claimed[c] < 0 || claimed[c] >= n) return false;   // a claim outside the window: refuse, never a stray row

    // One activation per token that owns a claimed row, in the contract the dispatch's CPU path uses.
    const size_t need_act = (size_t) n_tok;
    if (d.act_multi.size() < need_act) d.act_multi.resize(need_act);
    if (native && d.nact_multi.size() < need_act * kNativeActBytes) d.nact_multi.resize(need_act * kNativeActBytes);
    std::vector<uint8_t> token_used((size_t) n_tok, 0);
    for (int64_t c = 0; c < n_claimed; ++c) token_used[(size_t) (claimed[c] / k)] = 1;
    for (int64_t t = 0; t < n_tok; ++t) {
        if (!token_used[(size_t) t]) continue;
        if (native && strata::kernels::cpu::q2_native_kernels(lay.fmt[(size_t) layer].gu_type))
            act_quant_any(x_f + (size_t) t * H, H, d.act_multi[(size_t) t]);
        else if (native)
            native_quant_act(lay.fmt[(size_t) layer], x_f + (size_t) t * H,
                             d.nact_multi.data() + (size_t) t * kNativeActBytes);
        else
            act_quant_q8_1(x_f + (size_t) t * H, H, d.act_multi[(size_t) t]);
    }

    // The jobs: the same assembly the CPU misses use - one job per distinct expert, every claimed
    // token routed to it, the blob from the same source.  (The zero-copy role route is the cold
    // path's optimization; the fallback takes the source's own blob, which is the same bytes.)
    if (d.jobs_multi.size() < (size_t) n_claimed) d.jobs_multi.resize((size_t) n_claimed);
    int njobs = 0;
    for (int64_t c = 0; c < n_claimed; ++c) {
        const int32_t i = claimed[c];
        const int64_t t = i / k;
        const int32_t e = ids[i];
        if (e < 0 || e >= d.n_expert) {
            d.failed = true;
            d.fail = "a routed expert id is out of range";
            d.fail_layer = layer;
            d.fail_expert = e;
            return false;
        }
        int16_t& jo = d.job_of[(size_t) e];
        if (jo < 0) {
            const uint8_t* b = d.src->blob(layer, e);
            if (b == nullptr) {
                d.failed = true;
                d.fail = "the expert source could not produce a blob";
                d.fail_layer = layer;
                d.fail_expert = e;
                ++d.missing;
                for (int64_t q = 0; q < njobs; ++q) d.jobs_multi[(size_t) q].nt = 0;
                for (int64_t q = 0; q < c; ++q)
                    if (ids[claimed[q]] >= 0 && ids[claimed[q]] < d.n_expert) d.job_of[(size_t) ids[claimed[q]]] = -1;
                return false;
            }
            jo = (int16_t) njobs++;
            ExpertJobMulti& nj = d.jobs_multi[(size_t) jo];
            nj.blob = b;
            nj.nt = 0;
            nj.fmt = nullptr;
        }
        ExpertJobMulti& jb = d.jobs_multi[(size_t) jo];
        if (jb.nt >= MAXT) continue;   // cannot happen (nt <= k per expert); a guard, not a policy
        jb.act[jb.nt] = &d.act_multi[(size_t) t];
        jb.nact[jb.nt] = native ? d.nact_multi.data() + (size_t) t * kNativeActBytes : nullptr;
        jb.out[jb.nt] = out + (size_t) i * H;
        ++jb.nt;
        ++d.multi_entries;
        ++d.cache_refused;   // the honest accounting: these rows were served by the CPU
    }
    if (njobs > 0) {
        if (native) d.pool->run_split_multi_native(lay.fmt[(size_t) layer], d.jobs_multi.data(), njobs);
        else d.pool->run_split_multi(d.jobs_multi.data(), njobs);
    }
    for (int64_t q = 0; q < njobs; ++q) d.jobs_multi[(size_t) q].nt = 0;
    for (int64_t c = 0; c < n_claimed; ++c)
        if (ids[claimed[c]] >= 0 && ids[claimed[c]] < d.n_expert) d.job_of[(size_t) ids[claimed[c]]] = -1;
    return true;
}

// R5 P1 (r5-three-gpu-design.md §2 "Same-layer fallback, bounded"): the MAXT=8 seam as a REUSABLE,
// EXACT-tile library path for the PROMPT tier's deficit.  The shipped function above refuses anything
// over MAXT tokens - that is the contract, and it stays.  A prompt layer claims up to 256 rows, so
// the deficit arrives as a set of TOKENS; the design's rule is to partition those tokens into tiles of
// at most MAXT and run ONE exact window call per tile.  Per-expert nt ≤ 8 follows from the tile size
// (a token routes to a given expert once), so the slot guard at :111 can never fire and no row is
// dropped by a guard that was never meant to be a policy.
//
// The tiles run SERIALLY on the caller's thread: the tile loop calls d.src->blob (the shipped
// function's own blob capture, :92) and the source is not thread-safe (prefill.cpp:3915-3920) - the
// design's serialization requirement, inherited unchanged, not invented here.
bool expert_helper_recompute_prompt_rows(ExpertDispatch& d, const float* x_sel, const int32_t* sel,
                                          int64_t n_sel, const int32_t* ids, int64_t k,
                                          const int32_t* claims, int64_t n_claims, float* const* dst,
                                          bool native, int64_t layer, int64_t max_rows_per_layer,
                                          int64_t max_rows_total, ExpertHelperPromptFallback& fb,
                                          bool& budget_exceeded) {
    using namespace strata::kernels::cpu;
    budget_exceeded = false;
    if (n_claims <= 0) return true;
    if (k != 10 || n_sel < 1 || x_sel == nullptr || sel == nullptr || ids == nullptr || dst == nullptr ||
        max_rows_per_layer < 1 || max_rows_total < 1) {
        d.failed = true;
        d.fail = "the prompt row fallback was called outside its contract";
        d.fail_layer = layer;
        return false;
    }

    // Map every claim onto its row in the tier's activation buffer: `sel` is the prompt plan's
    // ASCENDING token list and `x_sel` its rows (row r is the activation of token sel[r]), so a tile
    // is a contiguous slice of positions and the shipped window function's own indexing applies.
    struct TileRow {
        int32_t pos;    // the claim's token position in sel (its activation row)
        int32_t entry;  // the FULL-window entry index (t*k + j) the helper did not answer
        int32_t idx;    // the claim's index in the caller's arrays (dst[idx] is its destination)
    };
    std::vector<TileRow> rows((size_t) n_claims);
    for (int64_t c = 0; c < n_claims; ++c) {
        const int32_t i = claims[c];
        if (i < 0 || dst[c] == nullptr) {
            d.failed = true;
            d.fail = "a prompt row fallback claim is out of range";
            d.fail_layer = layer;
            return false;
        }
        const int32_t t = (int32_t) (i / k);
        int lo = 0, hi = (int) n_sel - 1, pos = -1;
        while (lo <= hi) {
            const int mid = (lo + hi) / 2;
            if (sel[(size_t) mid] == t) { pos = mid; break; }
            if (sel[(size_t) mid] < t) lo = mid + 1; else hi = mid - 1;
        }
        if (pos < 0) {
            d.failed = true;
            d.fail = "a prompt row fallback token has no activation row in the tier's buffer";
            d.fail_layer = layer;
            return false;
        }
        rows[(size_t) c] = {pos, i, (int32_t) c};
    }
    std::sort(rows.begin(), rows.end(), [](const TileRow& a, const TileRow& b) {
        if (a.pos != b.pos) return a.pos < b.pos;
        return a.entry < b.entry;
    });
    for (size_t c = 1; c < rows.size(); ++c)
        if (rows[c].entry == rows[c - 1].entry) {
            d.failed = true;
            d.fail = "the prompt row fallback lists one claimed row twice";
            d.fail_layer = layer;
            return false;
        }

    if (fb.stage.size() < (size_t) MAXT * 10 * H) fb.stage.assign((size_t) MAXT * 10 * H, 0.0f);
    std::vector<int32_t> ids_tile((size_t) MAXT * 10);
    std::vector<int32_t> claims_tile((size_t) MAXT * 10);

    size_t at = 0;
    while (at < rows.size()) {
        const int32_t pos0 = rows[at].pos;
        const int32_t pos_end = pos0 + MAXT;                    // exclusive: the tile is ≤ MAXT tokens
        size_t stop = at;
        while (stop < rows.size() && rows[stop].pos < pos_end) ++stop;
        const int64_t tile_rows = (int64_t) (stop - at);
        // The declared budget: a tile that would breach it is NOT run.  This is not a dispatch
        // failure - the request keeps every row it computes locally - so d.failed stays untouched.
        if (fb.rows_layer + tile_rows > max_rows_per_layer || fb.rows_total + tile_rows > max_rows_total) {
            budget_exceeded = true;
            std::fprintf(stderr, "expert-helper: the same-layer fallback reached its budget at layer %lld "
                                 "(layer %lld of %lld rows, request %lld of %lld); the prompt tier is off "
                                 "for the rest of this request\n",
                         (long long) layer, (long long) fb.rows_layer, (long long) max_rows_per_layer,
                         (long long) fb.rows_total, (long long) max_rows_total);
            return false;
        }
        int32_t pos_last = pos0;
        for (size_t q = at; q < stop; ++q) pos_last = rows[q].pos;
        const int64_t n_tok_tile = (int64_t) pos_last - pos0 + 1;   // ≤ MAXT by construction
        // The tile's window, in the shipped function's own shape: the tile tokens' k ids each, in
        // routing order, and the claims as tile-local entry indices.
        for (int64_t u = 0; u < n_tok_tile; ++u) {
            const int32_t t = sel[(size_t) (pos0 + u)];
            for (int64_t j = 0; j < 10; ++j)
                ids_tile[(size_t) u * 10 + (size_t) j] = ids[(size_t) t * (size_t) k + (size_t) j];
        }
        for (size_t q = at; q < stop; ++q)
            claims_tile[q - at] = (rows[q].pos - pos0) * 10 + (int32_t) (rows[q].entry % k);
        // ONE exact call of the shipped window contract.  A blob the source cannot produce is ITS
        // failure (d.failed with the source's message), exactly the CPU share's err at the join.
        if (!expert_helper_recompute_claimed(d, x_sel + (size_t) pos0 * (size_t) H, ids_tile.data(),
                                             n_tok_tile, 10, claims_tile.data(), tile_rows, native, layer,
                                             fb.stage.data()))
            return false;
        for (size_t q = at; q < stop; ++q) {
            const int32_t local = (rows[q].pos - pos0) * 10 + (int32_t) (rows[q].entry % k);
            std::memcpy(dst[rows[q].idx], fb.stage.data() + (size_t) local * (size_t) H, (size_t) H * sizeof(float));
        }
        fb.rows_layer += tile_rows;
        fb.rows_total += tile_rows;
        at = stop;
    }
    return true;
}

}  // namespace strata::core
