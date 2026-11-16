// tests/cuda/sampler_mapped_parity.cu - F1 (STRATA_SAMPLER_IN_GRAPH, include/strata/core/sampler_mapped.hpp):
// the captured in-graph head sampler against the host-driven sample_tokens family, token for token AND draw for
// draw.  Every window: random logits, history, SamplerParams and (for the judged modes) drafts and q rows; the
// reference is the engine's today-path - the out-of-graph sample_tokens / sample_tokens_spec /
// sample_tokens_proposal launches with the same device buffers - and the challenger is the captured graph
// (sample_stage_mapped + sample_tokens_mapped, part + merge) replayed with only the mapped words rewritten for
// the window: a FRESH counter (sp.counter = the window's pos0) on every replay, so a graph that baked the draws
// could not pass.  Two graph variants replay side by side: the production one (Shadow = false, the engine's
// exact code) and the test one (Shadow = true), whose per-row Philox taps `uout` must equal the probes of the
// draw positions the HOST computes itself (pos, pos | kSpecCtrResid, pos | kSpecCtrAccept) through the very
// Philox stream the sampler calls (`sample_philox_probe`) - and must stay -inf exactly where the decision
// consumed no draw.  Ties matter: the logits mix integers and halves, so the fold's and the selection's
// tie-rules are exercised, not just the arithmetic.  A mode-0 window's replay must move NOTHING: the captured
// staging and sampler are inert and m_out_ keeps what the window before it wrote.
//
// Needs a GPU (it captures and replays graphs): without one it exits 77 and ctest SKIPs it.  It is REGISTERED
// without being run in CI; the sweep is 3 vocabularies (48 < the 64-list width, one 4,096-block, two blocks
// with a partial tail) x 4 modes x T in {1,2,5,6,8}, 40 armed + 10 inert windows per combination.
#include "strata/kernels/sampler.hpp"
#include "strata/kernels/verify_kernels.hpp"
#include "strata/core/spec_prob.hpp"
#include "strata/core/spec_proposal.hpp"

#include <cuda_runtime.h>

#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

namespace {

#define CK(call)                                                                                         \
    do {                                                                                                 \
        const cudaError_t e_ = (call);                                                                    \
        if (e_ != cudaSuccess) {                                                                         \
            std::fprintf(stderr, "%s: %s\n", #call, cudaGetErrorString(e_));                             \
            return 1;                                                                                    \
        }                                                                                                \
    } while (0)

using strata::kernels::SamplerParams;
using strata::core::kSpecCtrAccept;
using strata::core::kSpecCtrResid;
using strata::core::kSpecQEntries;
using strata::core::kSpecQStride;
constexpr int kMaxT = strata::kernels::kVerifyMaxT;           // rows the captured tail is sized for
constexpr uint32_t kInfNegBits = 0xff800000u;                // -inf: the uout sentinel (0xFF-filled float)

// splitmix64 - enough randomness, fixed and bit-reproducible per window
struct Rng {
    uint64_t s;
    uint64_t next() {
        s += 0x9e3779b97f4a7c15ull;
        uint64_t z = s;
        z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ull;
        z = (z ^ (z >> 27)) * 0x94d049bb133111ebull;
        return z ^ (z >> 31);
    }
    uint32_t bits32() { return (uint32_t) (next() >> 32); }
    int range(int lo, int hi) { return lo + (int) (next() % (uint64_t) (hi - lo + 1)); }   // inclusive
    float uniform(float lo, float hi) { return lo + (hi - lo) * ((float) (next() >> 40) / 16777216.0f); }
};

uint32_t f2b(float f) { uint32_t u; std::memcpy(&u, &f, 4); return u; }

// the graph's per-T capture: stage -> part -> merge (production) and the Shadow twin, on the shared buffers
struct Fixture {
    // device
    float* dlogits = nullptr;
    int32_t* dhist = nullptr;
    int32_t* dspec = nullptr;     // the spec image: dtok region [0,kMaxT), q rows from kMaxT (verify.cpp's layout)
    int32_t* dctl = nullptr;      // device mode word [0], n_q [1], inert hist image from word 8
    SamplerParams* dsp = nullptr;
    int2* dscratch = nullptr;
    int* dout = nullptr;
    int* dout_ref = nullptr;      // the host-driven reference writes here
    float* duout = nullptr;       // the Shadow taps
    uint64_t* dcounters = nullptr;
    float* duprobe = nullptr;     // sample_philox_probe of the host-computed positions
    // mapped host (the per-window writes the replay reads)
    SamplerParams* hsp = nullptr;
    int32_t* hmode = nullptr;
    int32_t* hspec = nullptr;
    cudaGraphExec_t exec_prod = nullptr;
    cudaGraphExec_t exec_shadow = nullptr;
};

int fail(const char* what, int nv, int mode, int T, int w, int row, long want, long got) {
    std::fprintf(stderr, "MISMATCH %s nv=%d mode=%d T=%d window=%d row=%d want=%ld got=%ld\n", what, nv, mode, T, w,
                 row, want, got);
    return 1;
}

}  // namespace

int main() {
    int ndev = 0;
    if (cudaGetDeviceCount(&ndev) != cudaSuccess || ndev == 0) {
        std::printf("SKIP: no GPU - the mapped sampler parity needs a device\n");
        return 77;
    }

    constexpr int kHistLen = 6;
    const int kVs[] = {48, 2048, 6144};            // < list width, one block, two blocks (partial tail)
    const int kTs[] = {1, 2, 5, 6, 8};
    constexpr int kArmed = 40, kInert = 10;

    cudaStream_t cs;
    CK(cudaStreamCreateWithFlags(&cs, cudaStreamNonBlocking));

    for (const int nv : kVs) {
        for (const int T : kTs) {
            // ---- fixture: one capture per (nv, T), buffers at the widest row count
            Fixture f;
            CK(cudaMalloc(&f.dlogits, (size_t) kMaxT * nv * sizeof(float)));
            CK(cudaMalloc(&f.dhist, (size_t) kMaxT * kHistLen * sizeof(int32_t)));
            CK(cudaMalloc(&f.dspec, (size_t) kMaxT * (1 + kSpecQStride) * sizeof(int32_t)));
            CK(cudaMalloc(&f.dctl, 64));
            CK(cudaMalloc((void**) &f.dsp, sizeof(SamplerParams)));
            CK(cudaMalloc((void**) &f.dscratch, (size_t) kMaxT * ((nv + 4095) / 4096) * strata::kernels::kSampleSelMax *
                                                   sizeof(int2)));
            CK(cudaMalloc(&f.dout, (size_t) kMaxT * sizeof(int)));
            CK(cudaMalloc(&f.dout_ref, (size_t) kMaxT * sizeof(int)));
            CK(cudaMalloc(&f.duout, (size_t) kMaxT * 4 * sizeof(float)));
            CK(cudaMalloc(&f.dcounters, (size_t) kMaxT * 4 * sizeof(uint64_t)));
            CK(cudaMalloc(&f.duprobe, (size_t) kMaxT * 4 * sizeof(float)));
            CK(cudaHostAlloc((void**) &f.hsp, sizeof(SamplerParams) + 64, cudaHostAllocMapped));
            CK(cudaHostAlloc((void**) &f.hmode, 64 * sizeof(int32_t), cudaHostAllocMapped));
            CK(cudaHostAlloc((void**) &f.hspec, (size_t) kMaxT * (1 + kSpecQStride) * sizeof(int32_t),
                             cudaHostAllocMapped));
            CK(cudaMemset(f.dctl, 0, 64));

            const int32_t* hist_dev = f.dhist;
            // record the captured tail; the graph reads only pointers fixed now - contents are replay-time
            auto capture = [&](bool shadow, cudaGraphExec_t* out) -> int {
                cudaGraph_t g;
                CK(cudaStreamBeginCapture(cs, cudaStreamCaptureModeGlobal));
                strata::kernels::sample_stage_mapped(f.hsp, f.hspec, f.hmode, f.dsp, f.dspec, f.dctl, cs);
                strata::kernels::sample_tokens_mapped(f.dlogits, T, nv, f.dsp, hist_dev, kHistLen, f.dspec, f.dctl,
                                                      f.dscratch, f.dout, shadow ? f.duout : nullptr, cs);
                CK(cudaStreamEndCapture(cs, &g));
                CK(cudaGraphInstantiate(out, g, nullptr, nullptr, 0));
                CK(cudaGraphDestroy(g));
                return 0;
            };
            if (capture(false, &f.exec_prod) || capture(true, &f.exec_shadow)) return 1;

            std::vector<float> hlogits((size_t) T * nv);
            std::vector<int32_t> hhist((size_t) T * kHistLen);
            std::vector<int32_t> hdtok(kMaxT, -1);
            std::vector<int32_t> hq((size_t) kMaxT * kSpecQStride, 0);
            std::vector<uint64_t> hcounters((size_t) kMaxT * 4);
            std::vector<int> got_t(kMaxT), want_t(kMaxT);
            std::vector<float> got_u((size_t) kMaxT * 4);
            std::vector<float> probe_u((size_t) kMaxT * 4);

            for (int mi = 0; mi < 4; ++mi) {                 // 0 spec, 1 proposal, 2 sampled, 3 penalized-greedy
                for (int w = 0; w < kArmed + kInert; ++w) {
                    const bool inert = w >= kArmed;
                    Rng rng{0x1234567u + (uint64_t) nv * 1000003u + (uint64_t) T * 7919u + (uint64_t) mi * 104729u +
                                          (uint64_t) w};
                    const int mode = inert ? strata::kernels::kSampleModeNone
                                           : (mi == 0 ? strata::kernels::kSampleModeSpec
                                                       : mi == 1 ? strata::kernels::kSampleModeProposal
                                                                 : mi == 2 ? strata::kernels::kSampleModeSampled
                                                                           : strata::kernels::kSampleModeGreedy);
                    const bool judged = mode == strata::kernels::kSampleModeSpec ||
                                        mode == strata::kernels::kSampleModeProposal;

                    // ---- logits: three regimes, tie-heavy on purpose (integers and halves tie under penalties)
                    const int regime = rng.range(0, 2);
                    for (size_t i = 0; i < hlogits.size(); ++i) {
                        if (regime == 0) hlogits[i] = (float) rng.range(-6, 6);
                        else if (regime == 1) hlogits[i] = 0.5f * (float) rng.range(-24, 24);
                        else hlogits[i] = rng.uniform(-8.0f, 8.0f);
                    }
                    if (rng.range(0, 9) == 0)   // an all-dead row: every path answers through its -inf rules
                        for (int t = T - 1; t < T; ++t)
                            for (int v = 0; v < nv; ++v) hlogits[(size_t) t * nv + v] = -INFINITY;

                    // ---- history: in-vocab ids, repeats, out-of-vocab and negative ids (never candidates)
                    for (auto& h : hhist)
                        h = rng.range(0, 7) < 5 ? rng.range(0, nv - 1) : (rng.range(0, 1) ? -1 : rng.range(nv, nv + 7));

                    // ---- the window's SamplerParams
                    SamplerParams sp;
                    sp.seed = rng.next();
                    sp.counter = (uint64_t) w * 131u + 17u;
                    sp.temperature = (mode == strata::kernels::kSampleModeGreedy) ? 0.0f : rng.uniform(0.2f, 2.0f);
                    sp.greedy = mode == strata::kernels::kSampleModeGreedy;
                    sp.top_k = rng.range(0, 7) == 0 ? 0 : rng.range(1, 64);
                    sp.top_p = rng.range(0, 3) == 0 ? 1.0f : rng.uniform(0.3f, 1.0f);
                    sp.min_p = rng.range(0, 3) == 0 ? 0.0f : rng.uniform(0.0f, 0.3f);
                    sp.min_keep = rng.range(0, 3);
                    sp.penalty_last_n = sp.greedy ? rng.range(1, kHistLen)
                                                  : (rng.range(0, 2) == 0 ? 0 : rng.range(1, kHistLen + 4));
                    sp.penalty_repeat = rng.uniform(0.5f, 1.5f);
                    sp.penalty_freq = rng.uniform(0.0f, 1.0f);
                    sp.penalty_present = rng.uniform(0.0f, 1.0f);
                    sp.logit_bias = nullptr;
                    sp.gumbel = false;

                    // ---- drafts and q: judged modes only (the engine arms spec with 1 <= n_q <= T-1; the
                    // proposal's NULL tail n_q == T is legal for the family, so it gets some windows too)
                    int nq = 0;
                    if (judged) {
                        const bool nulltail = mi == 1 && rng.range(0, 5) == 0;
                        nq = T == 1 ? (nulltail ? 1 : 0) : (nulltail ? T : rng.range(1, T - 1));
                        for (int t = 0; t < kMaxT; ++t) hdtok[t] = -1;
                        for (int t = 0; t + 1 < T; ++t) {   // in-vocab, out-of-range and no-draft rows
                            const int roll = rng.range(0, 11);
                            hdtok[t] = roll < 8 ? rng.range(0, nv - 1) : (roll == 8 ? -1 : rng.range(nv, nv + 3));
                        }
                        std::fill(hq.begin(), hq.end(), 0);
                        for (int r = 0; r < nq && r < kMaxT; ++r) {
                            int32_t* qid = hq.data() + (size_t) r * kSpecQStride;
                            float* qpr = (float*) (qid + kSpecQEntries);
                            int left = rng.range(0, 12);    // a short row ends at the first -1
                            for (int i = 0; i < left; ++i) {
                                qid[i] = rng.range(0, nv - 1);
                                const int roll = rng.range(0, 9);
                                qpr[i] = roll < 6 ? rng.uniform(0.001f, 0.5f)
                                                  : roll == 7 ? 0.0f : INFINITY;   // zero and non-finite mu
                            }
                            for (int i = 0; i < kSpecQStride; ++i)
                                if (i >= left && i < kSpecQEntries) qid[i] = -1;
                        }
                    }

                    // ---- write this window's world: buffers, then the mapped words, then the fences
                    CK(cudaMemcpyAsync(f.dlogits, hlogits.data(), hlogits.size() * sizeof(float),
                                       cudaMemcpyHostToDevice, cs));
                    CK(cudaMemcpyAsync(f.dhist, hhist.data(), hhist.size() * sizeof(int32_t), cudaMemcpyHostToDevice,
                                       cs));
                    CK(cudaMemsetAsync(f.duout, 0xFF, (size_t) kMaxT * 4 * sizeof(float), cs));   // -inf taps
                    if (inert)   // so a captured kernel that is NOT inert shows up
                        CK(cudaMemsetAsync(f.dout, 0x5A, (size_t) kMaxT * sizeof(int), cs));
                    std::memcpy(f.hsp, &sp, sizeof sp);
                    if (judged) {
                        std::memcpy(f.hspec, hdtok.data(), (size_t) kMaxT * sizeof(int32_t));
                        std::memcpy(f.hspec + kMaxT, hq.data(), (size_t) nq * kSpecQStride * sizeof(int32_t));
                    }
                    f.hmode[0] = mode;
                    f.hmode[1] = nq;
                    std::atomic_thread_fence(std::memory_order_seq_cst);

                    // ---- challenger: the captured graph, production then Shadow, then the today-path reference
                    CK(cudaGraphLaunch(f.exec_prod, cs));
                    CK(cudaGraphLaunch(f.exec_shadow, cs));
                    if (!inert) {
                        const int32_t* ddtok = f.dspec;
                        const int32_t* dq = f.dspec + kMaxT;
                        bool okref = true;
                        if (mode == strata::kernels::kSampleModeSpec)
                            okref = strata::kernels::sample_tokens_spec(f.dlogits, T, nv, f.dhist, kHistLen, sp, ddtok,
                                                                        dq, nq, f.dout_ref, cs);
                        else if (mode == strata::kernels::kSampleModeProposal)
                            okref = strata::kernels::sample_tokens_proposal(f.dlogits, T, nv, f.dhist, kHistLen, sp,
                                                                            ddtok, dq, nq, f.dout_ref, cs);
                        else
                            strata::kernels::sample_tokens(f.dlogits, T, nv, f.dhist, kHistLen, sp, f.dout_ref, cs);
                        if (!okref) {
                            std::fprintf(stderr, "the reference sampler refused nv=%d mode=%d T=%d window=%d\n", nv,
                                         mode, T, w);
                            return 1;
                        }
                        // the draw positions the HOST expects the rows to consume, probed through the sampler's
                        // own Philox stream
                        for (int t = 0; t < kMaxT; ++t) {
                            const uint64_t pos = sp.counter + (uint64_t) t;
                            hcounters[(size_t) t * 4] = pos | kSpecCtrAccept;
                            hcounters[(size_t) t * 4 + 1] = pos | kSpecCtrResid;
                            hcounters[(size_t) t * 4 + 2] = pos;
                            hcounters[(size_t) t * 4 + 3] = pos;
                        }
                        CK(cudaMemcpyAsync(f.dcounters, hcounters.data(), hcounters.size() * sizeof(uint64_t),
                                           cudaMemcpyHostToDevice, cs));
                        strata::kernels::sample_philox_probe(sp.seed, f.dcounters, f.duprobe, kMaxT * 4, cs);
                    }
                    CK(cudaStreamSynchronize(cs));
                    CK(cudaMemcpy(got_t.data(), f.dout, (size_t) T * sizeof(int), cudaMemcpyDeviceToHost));
                    CK(cudaMemcpy(want_t.data(), f.dout_ref, (size_t) T * sizeof(int), cudaMemcpyDeviceToHost));
                    CK(cudaMemcpy(got_u.data(), f.duout, (size_t) kMaxT * 4 * sizeof(float), cudaMemcpyDeviceToHost));
                    CK(cudaMemcpy(probe_u.data(), f.duprobe, (size_t) kMaxT * 4 * sizeof(float), cudaMemcpyDeviceToHost));

                    // ---- assertions
                    for (int t = 0; t < T; ++t) {
                        if (inert) {
                            const int want = (int) 0x5A5A5A5A;
                            if (got_t[t] != want) return fail("inert-window-moved", nv, mode, T, w, t, want, got_t[t]);
                            continue;
                        }
                        if (got_t[t] != want_t[t])
                            return fail("token", nv, mode, T, w, t, want_t[t], got_t[t]);
                    }
                    if (inert) continue;

                    const uint32_t neginf = kInfNegBits;
                    auto tap_is = [&](int idx) {   // the tap equals the probe at the position this slot names
                        const float want = probe_u[idx];
                        if (std::memcmp(&want, &got_u[idx], 4) != 0)
                            return fail("draw", nv, mode, T, w, idx / 4, f2b(want), f2b(got_u[idx]));
                        return 0;
                    };
                    auto tap_sentinel = [&](int idx) {
                        if (f2b(got_u[idx]) != neginf)
                            return fail("draw-unexpected", nv, mode, T, w, idx / 4, neginf, f2b(got_u[idx]));
                        return 0;
                    };
                    if (mode == strata::kernels::kSampleModeSampled) {
                        for (int t = 0; t < T; ++t) {
                            // the tail's draw is pos; the probe wrote it at the +2 slot (plain position)
                            const float want = probe_u[(size_t) t * 4 + 2];
                            if (std::memcmp(&want, &got_u[t], 4) != 0)
                                return fail("draw-sampled", nv, mode, T, w, t, f2b(want), f2b(got_u[t]));
                        }
                    } else if (mode == strata::kernels::kSampleModeGreedy) {
                        for (int idx = 0; idx < kMaxT * 4; ++idx) {
                            int r = tap_sentinel(idx);
                            if (r) return r;
                        }
                    } else {
                        for (int t = 0; t < T; ++t) {
                            const int d = t + 1 < T ? hdtok[t] : -1;
                            const bool nulltail = mi == 1 && (t == T - 1) && (nq == T);
                            const int out = got_t[t];
                            const bool err = out == strata::core::proposal::kProposalSamplerErrorToken;
                            const bool plain = d < 0 && !nulltail;   // dec 0: the plain draw, no accept/residual
                            // accept draw: every lane-0 row with a draft reads u = pos | ACC (dec 3 included)
                            if (d >= 0) {
                                int r = tap_is((int) t * 4);
                                if (r) return r;
                            } else {
                                int r = tap_sentinel((int) t * 4);
                                if (r) return r;
                            }
                            // residual draw: dec 2 with mass - the output tells us (reject keeps no draft:
                            // out != d and not the error token), NullTail included (its "draft" is the -1)
                            const bool resid = !plain && !err && (out != d);
                            if (resid) {
                                int r = tap_is((int) t * 4 + 1);
                                if (r) return r;
                            } else {
                                int r = tap_sentinel((int) t * 4 + 1);
                                if (r) return r;
                            }
                            // plain draw: dec 0
                            if (plain) {
                                int r = tap_is((int) t * 4 + 2);
                                if (r) return r;
                            } else {
                                int r = tap_sentinel((int) t * 4 + 2);
                                if (r) return r;
                            }
                        }
                    }
                }
            }
            std::printf("nv=%5d T=%d: %d windows x 4 modes parity OK\n", nv, T, kArmed + kInert);
            std::fflush(stdout);

            CK(cudaGraphExecDestroy(f.exec_prod));
            CK(cudaGraphExecDestroy(f.exec_shadow));
            CK(cudaFree(f.dlogits));
            CK(cudaFree(f.dhist));
            CK(cudaFree(f.dspec));
            CK(cudaFree(f.dctl));
            CK(cudaFree(f.dsp));
            CK(cudaFree(f.dscratch));
            CK(cudaFree(f.dout));
            CK(cudaFree(f.dout_ref));
            CK(cudaFree(f.duout));
            CK(cudaFree(f.dcounters));
            CK(cudaFree(f.duprobe));
            CK(cudaFreeHost(f.hsp));
            CK(cudaFreeHost(f.hmode));
            CK(cudaFreeHost(f.hspec));
        }
    }
    CK(cudaStreamDestroy(cs));
    std::printf("PASS: sampler mapped-vs-host parity (graph-captured, fresh counter per replay)\n");
    return 0;
}
