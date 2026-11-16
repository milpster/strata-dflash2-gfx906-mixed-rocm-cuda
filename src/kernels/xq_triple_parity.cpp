// src/kernels/xq_triple_parity.cpp - F2 (STRATA_HELPER_XQ_RING): the q8_1 activation-image parity
// chain the ring's byte-exactness rests on, asserted on THIS build:
//
//     quantize_q8_1_rows (the record_window producer) == native_quantize_q8_1 (the native pool
//     reference) == quantize_q8_1_cpu (the claim path's own quantizer)
//
// on random rows for every window width T = 1..8, on plain AND Hadamard-rotated rows (a folded
// layer's ring image is quantize(rot(x)), so the chain is asserted on rotated inputs too), and the
// rows leg additionally through a captured graph REPLAY - the producer runs inside the window graph,
// so its replay must write the same bytes.
//
// THE QFUSE LEG runs in the same ctest window as gr_parity --selftest, which pins the fused GR
// read's own q8_1 image (STRATA_QFUSE's producer for most images in the default config) to
// native_quantize_q8_1's bytes - together QFUSE == native == rows == cpu, the four-way chain the
// plan's definition of done names.  This binary is registered beside gr_parity for that reason.
//
// GPU, synthetic (no model).  Exits 77 when no device is present, so a host-only ctest run skips.
#include "strata/kernels/iq_kernels.hpp"
#include "strata/kernels/native_mmvq.hpp"
#include "strata/core/expert_helper_socket.hpp"
#include "strata/kernels/cpu/hadamard.hpp"

#include <cuda_runtime.h>

#include <cstdio>
#include <cstring>
#include <random>
#include <vector>

namespace k = strata::kernels;

static void ck(cudaError_t e) {
    if (e != cudaSuccess) { std::fprintf(stderr, "%s\n", cudaGetErrorString(e)); std::exit(2); }
}

int main() {
    int n_dev = 0;
    if (cudaGetDeviceCount(&n_dev) != cudaSuccess || n_dev <= 0) {
        std::printf("xq_triple_parity: no CUDA/HIP device - SKIP (77)\n");
        return 77;
    }
    const int64_t N = 2560;              // the model's n_embd: 80 q8_1 blocks x 36 B per row
    const size_t row_bytes = (size_t) (N / 32) * 36;
    cudaStream_t s;
    ck(cudaStreamCreate(&s));
    float* dx = nullptr;
    uint8_t *d_rows = nullptr, *d_rows_g = nullptr, *d_nat = nullptr;
    ck(cudaMalloc(&dx, 8 * (size_t) N * 4));
    ck(cudaMalloc(&d_rows, 8 * row_bytes));
    ck(cudaMalloc(&d_rows_g, 8 * row_bytes));
    ck(cudaMalloc(&d_nat, 8 * row_bytes));
    size_t cases = 0, bad = 0;
    for (int t = 1; t <= 8; ++t)
        for (int rotated = 0; rotated < 2; ++rotated) {
            std::mt19937 rng(7 + t * 131 + rotated * 65537);
            std::normal_distribution<float> nd(0.f, 1.f);
            std::vector<float> x((size_t) t * (size_t) N);
            for (auto& v : x) v = nd(rng);
            if (rotated)   // the folded layer's image is quantize(rot(x)) - hadamard.hpp's own transform
                strata::kernels::cpu::hadamard_rotate(x.data(), (int) (t * N), 256, nullptr);
            const size_t bytes = (size_t) t * row_bytes;
            std::vector<uint8_t> cpu_ref(bytes);
            strata::core::quantize_q8_1_cpu(x.data(), (int64_t) t * N, cpu_ref.data());
            ck(cudaMemcpyAsync(dx, x.data(), x.size() * 4, cudaMemcpyHostToDevice, s));
            ck(cudaMemsetAsync(d_rows, 0x5a, bytes, s));
            ck(cudaMemsetAsync(d_nat, 0xa5, bytes, s));
            // the direct legs: the record_window producer and the native pool reference
            k::quantize_q8_1_rows(dx, t, N, d_rows, s);
            k::native_quantize_q8_1(dx, d_nat, (int) N, t, s);
            ck(cudaStreamSynchronize(s));
            // the graph leg: the producer as the window graph replays it
            ck(cudaMemsetAsync(d_rows_g, 0x5a, bytes, s));
            cudaGraph_t g = nullptr;
            cudaGraphExec_t gx = nullptr;
            ck(cudaStreamBeginCapture(s, cudaStreamCaptureModeThreadLocal));
            k::quantize_q8_1_rows(dx, t, N, d_rows_g, s);
            ck(cudaStreamEndCapture(s, &g));
            ck(cudaGraphInstantiate(&gx, g, nullptr, nullptr, 0));
            ck(cudaGraphLaunch(qx, s));
            ck(cudaGraphLaunch(qx, s));   // replayed twice: the second must rewrite the same bytes
            ck(cudaStreamSynchronize(s));
            cudaGraphExecDestroy(qx);
            cudaGraphDestroy(g);
            std::vector<uint8_t> rows(bytes), rows_g(bytes), nat(bytes);
            ck(cudaMemcpy(rows.data(), d_rows, bytes, cudaMemcpyDeviceToHost));
            ck(cudaMemcpy(rows_g.data(), d_rows_g, bytes, cudaMemcpyDeviceToHost));
            ck(cudaMemcpy(nat.data(), d_nat, bytes, cudaMemcpyDeviceToHost));
            const bool ok_rows = rows == cpu_ref, ok_replay = rows_g == cpu_ref, ok_nat = nat == cpu_ref;
            if (!ok_rows || !ok_replay || !ok_nat) {
                const auto first_diff = [](const std::vector<uint8_t>& a, const std::vector<uint8_t>& b) {
                    size_t i = 0;
                    while (i < a.size() && a[i] == b[i]) ++i;
                    return i;
                };
                std::printf("FAIL T=%d %s: rows%s replay%s native%s (first diffs %zu/%zu/%zu)\n", t,
                            rotated ? "rotated" : "plain", ok_rows ? "==" : "!=", ok_replay ? "==" : "!=",
                            ok_nat ? "==" : "==", ok_rows ? 0 : first_diff(rows, cpu_ref),
                            ok_replay ? 0 : first_diff(rows_g, cpu_ref), ok_nat ? 0 : first_diff(nat, cpu_ref));
                ++bad;
            }
            ++cases;
        }
    ck(cudaFree(dx));
    ck(cudaFree(d_rows));
    ck(cudaFree(d_rows_g));
    ck(cudaFree(d_nat));
    ck(cudaStreamDestroy(s));
    std::printf("xq_triple_parity: %zu cases (T 1..8, plain + rotated, direct + graph replay), %zu failures\n",
                cases, bad);
    return bad ? 1 : 0;
}
