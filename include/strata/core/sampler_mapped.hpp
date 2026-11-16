// include/strata/core/sampler_mapped.hpp - F1: the gate and the host contract of the in-graph head sampler.
//
// Today (gate absent) a sampled, penalized or spec-judged request's head sampling runs on the HOST after the
// window graph: `Verifier::run` launches the sample_tokens family out-of-graph and pays a second
// cudaStreamSynchronize, because a captured sampler would bake its parameters and replay the same draws forever.
// With the gate on, the window graph's tail stages this window's sampling inputs out of mapped host memory into
// device copies (`kernels::sample_stage_mapped`) and one mode-selected sampler (`kernels::sample_tokens_mapped`,
// the split sampler's own part + merge launches) samples from them, selecting the body on the device-read mode
// word - `sample_tokens`' split sampler, the `sample_tokens_spec` / `sample_tokens_proposal` rejection rules, or
// the penalized-greedy argmax.  Nothing is baked that changes per window: T and n_vocab are capture-time
// constants the graphs already key on; params, mode, n_q and spec rows - and the Philox counter, which travels
// as pos0 inside the staged params, so a replay draws exactly the draws the host launch would - are replay-time
// mapped reads, and the history rows are the `hist_d_` copy F8's head stage lands earlier in the same replay.
//
// The decision set is one function, `sampler_in_graph()`, and the capture (`record_window`) and the per-window
// writes (`run` -> the mapped words) consult it with the same inputs; the mode word keeps every captured launch
// inert when the host path owns the window (a measure leg, a logit_bias request, or any refused staging), so a
// captured graph and an un-armed window cannot disagree.  The design and the bit-exactness argument are in
// src/kernels/cuda/sampler.cu; `sampler_mapped_parity` pins them.
//
// Arming includes the PENALIZED-GREEDY request (hist_d_ != nullptr with temperature 0): the mode word covers it,
// and a temperature-0 argmax is deterministic, so its tokens are identical by construction.  Plain greedy
// (hist_d_ == nullptr) never arms - it is served by the captured argmax already.  Fallback rule: any condition
// unmet -> today's host path, byte for byte.
#pragma once

#include <cstdlib>

namespace strata::core {

/// STRATA_SAMPLER_IN_GRAPH, default OFF: absent or "0" keeps the host sampler and its second sync exactly as
/// they were (the graphs of such a build do not even contain the sampler's tail); "1" captures the staging and
/// the mode-selected sampler into the window graph's tail and skips the host launch and sync on the armed windows.
/// Read ONCE per translation unit (the static below), like F8's penalty_hist_mapped(): every decision that has
/// to agree - allocating the staging buffers in init(), capturing the tail in record_window, and filling the
/// mapped words in run() - lives in verify.cpp, so a captured graph and a staged window cannot disagree about
/// whether this engine samples in the graph.
inline bool sampler_in_graph() {
    static const bool on = [] {
        const char* v = std::getenv("STRATA_SAMPLER_IN_GRAPH");
        return v != nullptr && std::atoi(v) != 0;
    }();
    return on;
}

}  // namespace strata::core
