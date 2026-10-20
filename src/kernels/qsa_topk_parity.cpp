// src/kernels/qsa_topk_parity.cpp - the QSA top-k the engine dispatches (qsa_block_topk) against the original kernel
// (qsa_block_topk_ref): the selected cells must be identical (GPU, synthetic, no model).
//
// A case is a context under a capacity (the engine's --max-context; max_blocks = capacity / 4 + 2, and without an
// active-block count the dispatch follows it), with consecutive queries at the context's end so that the tail block
// holds 0 to 3 cells.  Three score sets per case: continuous, 64 levels (many ties at the threshold) and all equal
// (the rule's ties go to the lowest index).  The dispatch is called as a decode window calls it (no block count) and as
// the prompt path does (with the call's largest block count).  The reference and the dispatched kernel are also timed.
//
// Usage: qsa_topk_parity --selftest
//        qsa_topk_parity CONTEXT [QUERIES=256] [REPS=10] [CAPACITY=CONTEXT] [COUNT=1]   (COUNT=0: no block count)
#include "strata/kernels/qsa.hpp"
#include "strata/kernels/qsa_select.hpp"

#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
#include <vector>

namespace k = strata::kernels;

namespace {
void ck(cudaError_t e, const char* w) {
    if (e != cudaSuccess) { std::fprintf(stderr, "%s: %s\n", w, cudaGetErrorString(e)); std::exit(2); }
}

bool run_case(int64_t ctx, int64_t nq, int reps, int64_t capacity, bool counted) {
    const k::QsaShapes s = k::qsa_real_shapes();
    const int64_t max_blocks = capacity / s.idx_block + 2, cap = k::qsa_selection_width(k::kTopkMaxCells, s);
    std::vector<int32_t> steps((size_t) (nq * k::kStepCount));
    for (int64_t i = 0; i < nq; ++i) {
        int32_t* st = steps.data() + i * k::kStepCount;
        const int64_t pos = ctx - nq + i;
        st[k::kStepPos] = (int32_t) pos;
        st[k::kStepNKv] = (int32_t) (pos + 1);
        st[k::kStepNBid] = (int32_t) ((pos + 1) / s.idx_block);
        st[k::kStepWidth] = (int32_t) k::qsa_selection_width(pos + 1, s);
    }
    std::mt19937 rng((uint32_t) (ctx * 31 + capacity));
    std::normal_distribution<float> nd(0.f, 1.f);
    std::vector<float> scores((size_t) (nq * max_blocks)), tied(scores.size()), equal(scores.size(), 0.25f);
    for (int64_t i = 0; i < nq; ++i) {
        const int32_t* st = steps.data() + i * k::kStepCount;
        float* row = scores.data() + i * max_blocks;
        for (int64_t b = 0; b < max_blocks; ++b) row[b] = nd(rng);
        // the tail block as the scorers leave it: far above the others when it holds cells
        if (st[k::kStepNKv] % s.idx_block != 0) row[st[k::kStepNBid]] += 1e9f;
        for (int64_t b = 0; b < max_blocks; ++b)
            tied[(size_t) (i * max_blocks + b)] = row[b] > 1e8f ? row[b] : std::floor(row[b] * 8.0f) / 8.0f;   // ~64 levels
    }
    int32_t *d_steps = nullptr, *ids_ref = nullptr, *ids_new = nullptr;
    float* d_scores = nullptr;
    ck(cudaMalloc(&d_steps, steps.size() * 4), "malloc");
    ck(cudaMalloc(&d_scores, scores.size() * 4), "malloc");
    ck(cudaMalloc(&ids_ref, (size_t) (nq * cap) * 4), "malloc");
    ck(cudaMalloc(&ids_new, (size_t) (nq * cap) * 4), "malloc");
    ck(cudaMemcpy(d_steps, steps.data(), steps.size() * 4, cudaMemcpyHostToDevice), "upload");
    const int64_t active = counted ? steps[(size_t) ((nq - 1) * k::kStepCount + k::kStepNBid)] + 1 : -1;
    std::vector<int32_t> a((size_t) (nq * cap)), b(a.size());
    auto same = [&](const std::vector<float>& sc) -> int64_t {
        ck(cudaMemcpy(d_scores, sc.data(), sc.size() * 4, cudaMemcpyHostToDevice), "upload");
        ck(cudaMemset(ids_ref, 0xff, a.size() * 4), "memset");
        ck(cudaMemset(ids_new, 0xff, a.size() * 4), "memset");
        k::qsa_block_topk_ref(d_scores, d_steps, nq, max_blocks, cap, s, ids_ref, nullptr);
        k::qsa_block_topk(d_scores, d_steps, nq, max_blocks, cap, s, ids_new, nullptr, active);
        ck(cudaDeviceSynchronize(), "sync");
        ck(cudaMemcpy(a.data(), ids_ref, a.size() * 4, cudaMemcpyDeviceToHost), "down");
        ck(cudaMemcpy(b.data(), ids_new, b.size() * 4, cudaMemcpyDeviceToHost), "down");
        int64_t n = 0;
        for (int64_t i = 0; i < nq; ++i) {
            const int64_t w = steps[(size_t) (i * k::kStepCount + k::kStepWidth)];
            n += std::equal(a.begin() + i * cap, a.begin() + i * cap + w, b.begin() + i * cap);
        }
        return n;
    };
    const int64_t same_tied = same(tied), same_equal = same(equal), same_cont = same(scores);   // d_scores: continuous
    cudaEvent_t e0, e1;
    ck(cudaEventCreate(&e0), "event");
    ck(cudaEventCreate(&e1), "event");
    auto timed = [&](auto f) {
        ck(cudaEventRecord(e0), "record");
        for (int r = 0; r < reps; ++r) f();
        ck(cudaEventRecord(e1), "record");
        ck(cudaEventSynchronize(e1), "time");
        float ms = 0;
        ck(cudaEventElapsedTime(&ms, e0, e1), "elapsed");
        return ms / (float) reps;
    };
    const float t_ref = timed([&] { k::qsa_block_topk_ref(d_scores, d_steps, nq, max_blocks, cap, s, ids_ref, nullptr); });
    const float t_new = timed([&] { k::qsa_block_topk(d_scores, d_steps, nq, max_blocks, cap, s, ids_new, nullptr, active); });
    cudaEventDestroy(e0); cudaEventDestroy(e1);
    cudaFree(d_steps); cudaFree(d_scores); cudaFree(ids_ref); cudaFree(ids_new);
    const bool ok = same_cont == nq && same_tied == nq && same_equal == nq;
    std::printf("%s context %lld under a capacity of %lld (%lld blocks), %lld queries %s: reference %.6f ms, "
                "dispatched %.6f ms (%.6fx); identical ids: continuous %lld/%lld, 64 levels %lld/%lld, equal scores "
                "%lld/%lld\n", ok ? "PASS" : "FAIL", (long long) ctx, (long long) capacity, (long long) max_blocks,
                (long long) nq, counted ? "with their block count" : "without a block count", t_ref, t_new, t_ref / t_new,
                (long long) same_cont, (long long) nq, (long long) same_tied,
                (long long) nq, (long long) same_equal, (long long) nq);
    return ok;
}

// Capture once, then replay with growing and restored device steps. The maximum
// n_bid deliberately moves away from the last row to exercise FIT17's whole-window guard.
bool guarded_graph_case(int64_t nq) {
    const auto s = k::qsa_real_shapes();
    const int64_t max_blocks = 262144 / s.idx_block + 2;
    const int64_t cap = k::qsa_selection_width(k::kTopkMaxCells, s);
    std::vector<int32_t> steps(nq * k::kStepCount), ref(nq * cap), got(nq * cap);
    std::vector<float> scores(nq * max_blocks);
    std::mt19937 rng(90617);
    for (auto& x : scores) x = float(int(rng() % 65) - 32) / 8.f;
    float* ds = nullptr; int32_t *dt = nullptr, *dr = nullptr, *dg = nullptr;
    ck(cudaMalloc(&ds, scores.size()*sizeof(float)), "scores");
    ck(cudaMalloc(&dt, steps.size()*sizeof(int32_t)), "steps");
    ck(cudaMalloc(&dr, ref.size()*sizeof(int32_t)), "reference");
    ck(cudaMalloc(&dg, got.size()*sizeof(int32_t)), "candidate");
    ck(cudaMemcpy(ds, scores.data(), scores.size()*sizeof(float), cudaMemcpyHostToDevice), "scores upload");
    cudaStream_t stream; ck(cudaStreamCreate(&stream), "stream");
    auto update = [&](int64_t ctx, bool reverse) {
        for (int64_t i = 0; i < nq; ++i) {
            const int64_t n = ctx - (reverse ? i : nq - 1 - i);
            auto* st = steps.data() + i*k::kStepCount;
            st[k::kStepPos] = int32_t(n-1); st[k::kStepNKv] = int32_t(n);
            st[k::kStepNBid] = int32_t(n/s.idx_block);
            st[k::kStepWidth] = int32_t(k::qsa_selection_width(n,s));
        }
        ck(cudaMemcpyAsync(dt, steps.data(), steps.size()*sizeof(int32_t), cudaMemcpyHostToDevice, stream), "steps upload");
    };
    update(24576,false);
    // Warm host-side device caches and option parsing before capture.
    k::qsa_block_topk(ds,dt,nq,max_blocks,cap,s,dg,stream,-1);
    ck(cudaStreamSynchronize(stream), "warmup");
    cudaGraph_t graph; cudaGraphExec_t exec;
    ck(cudaStreamBeginCapture(stream, cudaStreamCaptureModeThreadLocal), "capture begin");
    k::qsa_block_topk(ds,dt,nq,max_blocks,cap,s,dg,stream,-1);
    ck(cudaStreamEndCapture(stream,&graph), "capture end");
    ck(cudaGraphInstantiate(&exec,graph,nullptr,nullptr,0), "instantiate");
    bool ok = true;
    // Short/long boundary, exact 17-key capacity (n_bid+1=17408), overflow,
    // long route, and restores crossing both boundaries in the same graph.
    for (int64_t ctx : {4096,24575,24576,24577,69627,69628,69631,69632,69633,200000,262144,69628,24576,4096,24577}) {
        for (bool reverse : {false,true}) {
            update(ctx,reverse);
            ck(cudaMemsetAsync(dr,0xff,ref.size()*sizeof(int32_t),stream), "clear reference");
            ck(cudaMemsetAsync(dg,0xff,got.size()*sizeof(int32_t),stream), "clear candidate");
            k::qsa_block_topk_ref(ds,dt,nq,max_blocks,cap,s,dr,stream);
            ck(cudaGraphLaunch(exec,stream), "replay");
            ck(cudaMemcpyAsync(ref.data(),dr,ref.size()*sizeof(int32_t),cudaMemcpyDeviceToHost,stream), "read reference");
            ck(cudaMemcpyAsync(got.data(),dg,got.size()*sizeof(int32_t),cudaMemcpyDeviceToHost,stream), "read candidate");
            ck(cudaStreamSynchronize(stream), "replay sync");
            // Include padding sentinels: no arm may write outside selected width.
            if (ref != got) { ok = false; std::printf("FAIL guarded graph nq=%lld ctx=%lld reverse=%d\n",(long long)nq,(long long)ctx,int(reverse)); }
        }
    }
    ck(cudaGraphExecDestroy(exec), "exec destroy"); ck(cudaGraphDestroy(graph), "graph destroy");
    ck(cudaStreamDestroy(stream), "stream destroy");
    cudaFree(ds); cudaFree(dt); cudaFree(dr); cudaFree(dg);
    return ok;
}
}  // namespace

int main(int argc, char** argv) {
    if (argc == 2 && std::strcmp(argv[1], "--gfx906-guarded") == 0) {
        bool ok = true;
        for (int nq : {1,2,6,8}) ok = guarded_graph_case(nq) && ok;
        // The uncounted >8 window, prompt/count, and capacity beyond reg66
        // must continue to use current-main fallback paths.
        ok = run_case(69632,9,1,262144,false) && ok;
        ok = run_case(69632,8,1,262144,true) && ok;
        ok = run_case(300001,8,1,524288,false) && ok;
        std::printf("guarded/FIT17 graph parity: %s\n",ok ? "PASS" : "FAIL");
        return ok ? 0 : 1;
    }

    if (argc > 1 && std::strcmp(argv[1], "--selftest") == 0) {
        // {context, capacity}: both sides of the register kernel's reach (33,792 blocks = 135,168 cells), short
        // contexts under a long capacity (fewer blocks than the kernel's threads; a prompt batch below the dispatch's
        // 4,608 blocks), and the long contexts themselves
        const int64_t cases[][2] = {{9000, 9000},    {135160, 135160}, {135176, 135176}, {2100, 262144},
                                    {9000, 262144},  {32768, 262144},  {262144, 262144}, {300001, 524288},
                                    {524288, 524288}};
        bool ok = true;
        for (const auto& c : cases) {
            ok = run_case(c[0], 8, 3, c[1], false) && ok;     // a decode window's call
            ok = run_case(c[0], 64, 3, c[1], true) && ok;     // the prompt path's
        }
        std::printf("%s\n", ok ? "qsa_topk_parity: PASS" : "qsa_topk_parity: FAIL");
        return ok ? 0 : 1;
    }
    if (argc < 2) {
        std::fprintf(stderr, "usage: qsa_topk_parity --selftest | CONTEXT [QUERIES=256] [REPS=10] [CAPACITY=CONTEXT] "
                             "[COUNT=1]\n");
        return 2;
    }
    const int64_t ctx = std::atoll(argv[1]);
    const int64_t nq = argc > 2 ? std::atoll(argv[2]) : 256;
    const int reps = argc > 3 ? std::atoi(argv[3]) : 10;
    const int64_t capacity = argc > 4 ? std::atoll(argv[4]) : ctx;
    if (ctx < nq || nq <= 0 || reps <= 0 || capacity < ctx) {
        std::fprintf(stderr, "qsa_topk_parity: CONTEXT >= QUERIES > 0, REPS > 0, CAPACITY >= CONTEXT\n");
        return 2;
    }
    return run_case(ctx, nq, reps, capacity, argc <= 5 || std::atoi(argv[5]) != 0) ? 0 : 1;
}
