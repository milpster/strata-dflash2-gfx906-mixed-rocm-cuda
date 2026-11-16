// include/strata/kernels/sampler.hpp - the sampler chain, host-callable (P2.S2).
//
//     penalties -> top_k -> top_p -> min_p -> temperature -> pick
//
// llama.cpp's default chain (issue #53): ONE penalties stage, first, and temperature AFTER the truncation filters;
// top_p cuts before min_p.  `src/kernels/cuda/sampler.cu` has the details and `sampler_parity` pins them.
#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>

// int2, as src/kernels/cuda/sampler.cu's launches take it - the runtime header's, never a local stand-in when
// the header is reachable: a TU that includes this one and then <cuda_runtime.h> (many do) would otherwise
// collide with it.  The stand-in is only for pure host TUs of a GPU build (no toolchain headers on the path).
#if defined(__CUDACC__) || defined(__HIPCC__) || __has_include(<hip/hip_runtime.h>) || \
    (__has_include(<cuda_runtime.h>) && !defined(STRATA_HIP_GFX906))
#include <cuda_runtime.h>
#else
struct int2 { int x, y; };  // the GPU-less host unit's view of the same vector pair (CUDA's int2, bit for bit)
#endif

namespace strata::kernels {

struct SamplerParams {
    int top_k = 20;              // sampled path: 1..64 as given; 0 (off) or more than 64 keep the widest list, 64
    float top_p = 0.95f;         // 1.0 disables the filter
    float min_p = 0.0f;          // 0 disables; keeps tokens with p >= min_p * p_max (a prefix of the top_k list)
    float temperature = 1.0f;    // <= 0 means greedy
    int min_keep = 1;            // top_p keeps at least this many
    int penalty_last_n = 0;      // 0 disables; the window over `history` to count occurrences in
    float penalty_repeat = 1.0f;
    float penalty_freq = 0.0f;
    float penalty_present = 0.0f;
    uint64_t seed = 0;           // drives Philox, which is counter-based on (seed, token index)
    uint64_t counter = 0;        // absolute draw index of row 0; advance across decode calls
    bool greedy = false;
    const float* logit_bias = nullptr; // optional device array [n_vocab], added BEFORE penalties/filters; -inf bans
    bool gumbel = false;         // STRATA_SPEC_GUMBEL=1: Gumbel-max pick keyed by (seed, counter, token id) - see sampler.cu
};

// logits (n_tokens, n_vocab) -> one sampled token id per row in `out`.
//
// **`logits` AND `out` ARE DEVICE POINTERS.**  This is a CUDA kernel launch, not a host function, and nothing
// in the parameter names or the types says so - `const float* logits` reads exactly like a host buffer.
// Measured: passing host memory for `logits` faults inside the kernel with an ILLEGAL MEMORY ACCESS that is
// reported by whatever synchronising call happens NEXT, which will be somewhere else entirely and will name a
// buffer that has nothing to do with it.  A sticky async fault does not know where it came from.
//
// `history` is (n_tokens, history_len) int32 - ONE ROW PER TOKEN ROW, at a stride of `history_len` - the most
// recent tokens that row's pick follows, with any unused slots set to -1; only the last `p.penalty_last_n` of
// each row are counted, and ids outside [0, n_vocab) are ignored.  Pass nullptr and 0 when no penalties apply.
// A verify window's rows need DIFFERENT histories: row t follows the window's drafts 1..t (`penalty_rows`).
void sample_tokens(const float* logits, int n_tokens, int n_vocab, const int* history, int history_len,
                   const SamplerParams& p, int* out, void* stream);

// The greedy pick (no penalties) on a thread-block cluster of 8 CTAs per row (sm_90+, CUDA; S19): the same token as
// sample_tokens' one-block argmax, which takes it unless STRATA_ARGMAX_MULTI=0.  False (nothing launched) where it
// cannot run: HIP, a card or a build below sm_90.  Device pointers; capturable.
bool sample_greedy_cluster(const float* logits, int n_tokens, int n_vocab, int* out, void* stream);

// ---- COUPLED DRAFT SAMPLING (include/strata/core/coupled_draft.hpp, STRATA_SPEC_COUPLED=1).  Device pointers
// throughout; every per-request / per-round value comes from device memory, so the calls can be captured.
//
// The scratch `coupled_draft_sample` needs for `nv` logits (0: too wide for the split merge, coupled cannot run).
size_t coupled_draft_scratch_bytes(int nv);
// A round's inputs: `*params` <- `*mapped_params`, ring[cap - h, cap) <- mapped_hist[cap - h, cap) with h =
// coupled_hist_len(penalty_last_n, cap).  `mapped_*` are mapped host memory.
void coupled_draft_stage(const SamplerParams* mapped_params, const int32_t* mapped_hist, SamplerParams* params,
                         int32_t* ring, int cap, void* stream);
// Draft j of the chain from ONE row of `nv` logits (modified in place: the penalties): the target's chain with
// `*params`, the penalty window ring[cap + j - h, cap + j), Philox counter coupled_draft_counter(step_rec[0]).
// `sub_to_id` / `id_to_sub` map the draft head's subset to token ids and back (null: the whole vocabulary,
// `id_vocab` ids).  Writes the token id to *out_id, its probability under the final distribution to *out_prob, and
// the id to ring[cap + j].
void coupled_draft_sample(float* logits, int nv, const int32_t* sub_to_id, const int32_t* id_to_sub, int id_vocab,
                          const SamplerParams* params, int32_t* ring, int cap, int j, const int32_t* step_rec,
                          void* scratch, int32_t* out_id, float* out_prob, void* stream);

// ---- PROBABILISTIC DRAFT ACCEPTANCE (include/strata/core/spec_prob.hpp, STRATA_SPEC_PROB=1).  Device pointers.
// The drafter's last step in this mode: as coupled_draft_sample, but the draft is drawn from the chain's distribution
// q with the drafter's own Philox stream, and q's (id, probability) list goes to qrows + j * kSpecQStride (the
// drafter's mapped host memory; host-readable once the stream has synced).  *out_prob is q's top probability, or the
// drawn token's own when gate_pick (what --spec-min-p gates a further draft on).
void spec_draft_sample(float* logits, int nv, const int32_t* sub_to_id, const int32_t* id_to_sub, int id_vocab,
                       const SamplerParams* params, int32_t* ring, int cap, int j, const int32_t* step_rec,
                       void* scratch, int32_t* out_id, float* out_prob, int32_t* qrows, int gate_pick, float tscale, void* stream);
// The verify window's head under rejection sampling: row t (< n_tokens - 1) judges the draft dtok[t] (n_tokens - 1
// ids, device); rows < n_q have the drafter's list at qbuf + t * kSpecQStride (device), the others are point masses.
// out[t] = the draft when kept, the residual sample when not, a plain sample on the last row; `p.counter` is the
// window's first position, as sample_tokens.  False (nothing launched) where the split sampler cannot run - the
// caller then calls sample_tokens (exact match).
bool sample_tokens_spec(const float* logits, int n_tokens, int n_vocab, const int* history, int history_len,
                        const SamplerParams& p, const int32_t* dtok, const int32_t* qbuf, int n_q, int* out,
                        void* stream);
// Explicit gated measure: rows<n_q use mu; n_q==n_tokens means a NULL tail.
// n_q == 0 permits null qbuf: point-only rows, or plain sampling for T1.
// Same device-buffer layout as sample_tokens_spec. False is a hard failure:
// work may have been queued; synchronize before releasing buffers. No plain-p fallback.
bool sample_tokens_proposal(const float* logits, int n_tokens, int n_vocab, const int* history, int history_len,
                        const SamplerParams& p, const int32_t* dtok, const int32_t* qbuf, int n_q, int* out,
                        void* stream);

// ---- F1: THE IN-GRAPH HEAD SAMPLER (include/strata/core/sampler_mapped.hpp, STRATA_SAMPLER_IN_GRAPH).  The window
// graph stages this window's sampling inputs out of mapped host memory into device copies, then ONE mode-selected
// sampler runs from those: `sample_stage_mapped_kernel`, `sample_mapped_part_kernel` and `sample_mapped_merge_kernel`
// in src/kernels/cuda/sampler.cu carry the design and the bit-exactness argument.  The mapped_* pointers are mapped
// host memory (host-visible, device-addressable); every other pointer is device memory.  Capturable; nothing reads
// the environment between capture and replay.
//
// The mode word (mapped_mode[0], staged to the device at each window's launch): 0 none - the launches are inert,
// the host sampler owns the window (the fallback path); 1 spec - `sample_tokens_spec`'s rejection rule; 2 proposal
// - `sample_tokens_proposal`'s; 3 sampled - `sample_tokens`' split sampler; 4 penalized greedy - `sample_tokens`'s
// temperature-0 argmax over the penalties.  mapped_mode[1] is n_q for the judged modes.
// The spec buffer is the window's draft ids (dtok) in a fixed kVerifyMaxT-word region followed by n_q q rows of
// kSpecQStride (the drafter's lists, spec_prob.hpp's layout) - the layout sample_tokens_spec's device buffer
// already uses.  The penalty history is NOT staged: the sampler reads the same `hist_d_` rows the host sampler
// reads, landed by F8's head copy earlier in the same replay.  SamplerParams.counter travels in the staged struct:
// the host writes the window's pos0, so a replay draws Philox(seed, pos0 + t) - the same draws the out-of-graph
// launch takes today.
inline constexpr int32_t kSampleModeNone = 0;
inline constexpr int32_t kSampleModeSpec = 1;
inline constexpr int32_t kSampleModeProposal = 2;
inline constexpr int32_t kSampleModeSampled = 3;
inline constexpr int32_t kSampleModeGreedy = 4;

/// The sampler's split geometry (sampler.cu's internal kSplitBlockSpan / kSelMax): the vocabulary's
/// capture-time block count, 0 when the vocabulary is past the widest capture the split sampler takes.  The
/// verifier sizes its scratch with this and `sample_tokens_mapped_scratch_entries` before capturing.
inline constexpr int kSampleSplitBlockSpan = 4096;   // logits per part block, as sampler_split_part_kernel
inline constexpr int kSampleSelMax = 64;             // the widest shortlist, as sampler_kernel's KMAX
inline constexpr int kSampleSplitMaxBlocks = 64;     // lists the merge holds: n_vocab <= 262,144
inline int sample_split_blocks(int n_vocab) {
    const int nb = (n_vocab + kSampleSplitBlockSpan - 1) / kSampleSplitBlockSpan;
    return nb > kSampleSplitMaxBlocks ? 0 : nb;
}
inline size_t sample_tokens_mapped_scratch_entries(int n_tokens, int n_vocab) {
    const int nb = sample_split_blocks(n_vocab);
    return nb == 0 ? 0 : (size_t) n_tokens * (size_t) nb * (size_t) kSampleSelMax;
}

/// The staging launch, recorded at the head of the sampler's captured tail: params, mode/n_q and (judged modes)
/// the kVerifyMaxT + n_q * kSpecQStride spec words from mapped into the device copies `sample_tokens_mapped`
/// reads.  A mode-0 window copies nothing (the mode gate is read at replay).
void sample_stage_mapped(const SamplerParams* mapped_params, const int32_t* mapped_spec, const int32_t* mapped_mode,
                         SamplerParams* params, int32_t* spec, int32_t* mode, void* stream);
/// The mode-selected captured sampler: two launches recorded exactly as the split sampler records itself - the
/// 4,096-logit parts of every row, then one warp per row running the body the device-read mode word names
/// (sampler.cu describes the merge's sampled / judged / penalized-greedy tails).  `scratch` holds
/// kVerifyMaxT x n_blocks x kSelMax int2 (Verifier-owned, never freed while a graph can replay).  `hist`/`hist_len`
/// are the history rows the host sampler itself takes (`hist_d_`).  `uout` (tests; pass nullptr in the engine)
/// taps each row's Philox draws - sampled mode one float per row, judged modes three.
/// No-op for n_vocab past the split sampler's widest capture (the caller then keeps the host path).
void sample_tokens_mapped(const float* logits, int n_tokens, int n_vocab, const SamplerParams* params,
                          const int32_t* hist, int hist_len, const int32_t* spec, const int32_t* mode, int2* scratch,
                          int* out, float* uout, void* stream);
/// The Philox stream itself (the sampler_mapped_parity test only; nullptr stream = default): out[i] =
/// philox_uniform(seed, counters[i]) on the device - the same function the sampler calls, so the test can pin
/// the Shadow taps against draw positions it computes itself on the host. All pointers device memory.
void sample_philox_probe(uint64_t seed, const uint64_t* counters, float* out, int n, void* stream);

// #4590: which tokens the repeat / frequency / presence penalties count.  By default only the current reply: the
// tokens the answer has written (the window starts at the last prompt token, the one the first decode step feeds in).
// The prompt, including earlier assistant turns in it, is not counted, so a number or a quoted answer in the new
// task is not penalised just for being there.  STRATA_PENALTY_PROMPT=1 restores the old window, the last
// penalty_last_n tokens of prompt + reply.  Any other value (or none) is the default.
inline bool penalty_counts_prompt(const char* env) { return env != nullptr && env[0] == '1'; }
// The first index of the session's consumed tokens (prompt, then reply) the penalty window may read, given how
// many were consumed when the request's decode began (the prompt without its last token).
inline size_t penalty_window_from(size_t consumed_at_decode_start, bool counts_prompt) {
    return counts_prompt ? 0 : consumed_at_decode_start;
}

// The penalty-history rows of a verify window, on the host: row t of `out` (T rows of `h` slots) is the last `h`
// tokens of `tail[0..n_tail)` followed by `window[0..t]`, most recent LAST, -1 in the unused front slots.
// `tail` is what the state consumed before the window, `window[0]` the fed-back token and `window[1..]` the
// drafts: row t is exactly the history plain decode counts when it picks the token after `window[t]`, so drafting
// cannot change which tokens are penalised.  Row 0 alone is the single row the engine staged before 0.1.19.
inline void penalty_rows(const int32_t* tail, int64_t n_tail, const int32_t* window, int T, int h, int32_t* out) {
    for (int t = 0; t < T; ++t) {
        int32_t* row = out + (size_t) t * (size_t) h;
        const int64_t avail = n_tail + t + 1;                      // tail + window[0..t]
        const int take = (int) std::min<int64_t>(h, avail);
        std::fill(row, row + (h - take), -1);
        for (int j = 0; j < take; ++j) {
            const int64_t i = avail - take + j;                     // index into (tail, window)
            row[h - take + j] = i < n_tail ? tail[i] : window[i - n_tail];
        }
    }
}

/// The penalty-history rows of a BATCH window, on the host.  Row t of the window feeds `tokens[t]` into slot `rows[t]`; its row of `out` (h slots, most
/// recent LAST, -1 in unused front slots) is the last `h` tokens of that slot's reply tail (`tails[slot]`, `n_tails[slot]` tokens: what the slot's
/// session consumed since the reply began, the fed-back head not yet included) followed by the tokens fed so far by THIS window's rows of the same slot
/// up to and including row t (a slot may hold several rows: MTP drafts).  Exactly what `penalty_rows` gives a solo window, per slot.
inline void slot_penalty_rows(const int* rows, const int32_t* tokens, int S, const int32_t* const* tails, const int64_t* n_tails, int h, int32_t* out) {
    for (int t = 0; t < S; ++t) {
        int32_t* row = out + (size_t) t * (size_t) h;
        const int slot = rows[t];
        const int32_t* tail = tails[slot];
        const int64_t n_tail = tail != nullptr ? n_tails[slot] : 0;
        int64_t in_window = 0;                                     // this window's rows of the slot up to t
        for (int u = 0; u <= t; ++u) in_window += rows[u] == slot;
        const int64_t avail = n_tail + in_window;
        const int take = (int) std::min<int64_t>(h, avail);
        std::fill(row, row + (h - take), -1);
        for (int j = 0; j < take; ++j) {
            const int64_t i = avail - take + j;                     // index into (tail, the slot's window tokens)
            if (i < n_tail) { row[h - take + j] = tail[i]; continue; }
            int64_t k = i - n_tail, seen = 0;
            for (int u = 0; u <= t; ++u)
                if (rows[u] == slot) { if (seen == k) { row[h - take + j] = tokens[u]; break; } ++seen; }
        }
    }
}

}  // namespace strata::kernels
