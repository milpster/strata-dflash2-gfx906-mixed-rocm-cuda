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
// WHAT "==" MEANS, precisely, because one of the chain's four fields is not byte-claimable:
//   * The three GPU legs are byte-exact to EACH OTHER over all 36 bytes of every block, with no
//     tolerance. That is the ring's actual byte-exactness claim, and it holds by construction:
//     q8_1_store (iq_kernels.cu) reduces max and sum with the shfl_xor butterfly, and
//     native_quantize_q8_1_kernel (native_mmvq.cu) calls warp_max/warp_sum - the same butterfly, the
//     same offsets, so the two producers cannot disagree. A graph replay of the producer runs the
//     identical kernel, so it is byte-identical too.
//   * Against the CPU reference, amax, the scale d and every quantized value ARE provably
//     device-identical: fmaxf is order-independent, and d = amax / 127 and roundf(x / d) are single
//     correctly-rounded operations (--use_fast_math is off for the gfx906 build, CMakeLists.txt's
//     STRATA_HIP_GFX906 branch, so no leg takes the fast-division path #3948 describes). So block
//     bytes 0..1 (ds[0], the scale) and 4..35 (qs) are byte-exact here, as they are in the pins.
//   * The fp16 SUM field (ds[1], block bytes 2..3) is the ONE field this project does not claim
//     device bit-parity on, and says so where the quantizer is defined (expert_helper_socket.cpp's
//     note above quantize_q8_1_cpu; tests/core/expert_helper_socket_test.cpp's golden-bytes pin:
//     "amax/d/q ARE provably device-identical; the fp16 sum field is ULP-close, not proven
//     bit-equal"). The device reduces the block's sum with the 5-level butterfly tree
//     (((x0+x16)+(x8+x24))+...); quantize_q8_1_cpu accumulates x0+x1+...+x31. IEEE-754 addition is
//     not associative, so the two fp32 sums differ by a few ULP and, near a rounding boundary, land
//     on opposite halves. docs/MIXED_GPU_HELPER.md hardware item 4 is the acceptance test for the
//     field: ULP-scale deltas, not bit equality.
// So this binary asserts the sum field ULP-close against a bound derived from the block's own data
// (sum_slack below) and byte-exact everywhere else. A drift that big is a producer bug; a drift this
// small IS the reduction order, and is expected: on N(0,1) rows about one block in 1.3e3 flips, so a
// 16-case run over 5760 blocks normally shows a handful of them. The flips are data-driven, NOT a
// function of T - which is why the failing (T, orientation) pairs move between runs and why rotation
// neither causes nor masks them.
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

#include <cfloat>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <random>
#include <string>
#include <vector>

namespace k = strata::kernels;

static void ck(cudaError_t e) {
    if (e != cudaSuccess) { std::fprintf(stderr, "%s\n", cudaGetErrorString(e)); std::exit(2); }
}

namespace {

constexpr size_t kBlockBytes = 36;        // sizeof(block_q8_1): half2 ds (d, sum) + int8 qs[32]
constexpr size_t kSumLo = 2, kSumHi = 3;  // ds[1], the sum field; ds[0] (the scale) is at 0..1
constexpr size_t kNoDiff = ~static_cast<size_t>(0);

/// binary16 -> float from the stored bits, exact for every finite half (checked against GCC's
/// _Float16 over all 65504 of them). Written out because this test's host TU sees the HIP compat
/// shim's cuda_fp16.h, where a host-side __half2float is not something to lean on. NaN's sign is
/// not preserved; #606 (q8_1_finite.hpp) clamps both ds halfs to a finite +-65504, so ds is never
/// NaN or inf here.
float f16_to_f32(uint16_t h) {
    const float sign = (h & 0x8000u) ? -1.0f : 1.0f;
    const uint32_t e = (h >> 10) & 0x1fu, m = h & 0x3ffu;
    if (e == 0x1fu) return sign * (m ? NAN : INFINITY);
    if (e == 0) return sign * (float) m * 0x1p-24f;   // +-0 and the subnormals
    return sign * std::ldexpf((float) (m + 0x400u), (int) e - 25);   // normals: (1024 + m) * 2^(e-25)
}

/// The gap between adjacent halfs at |v|. 2^-24 is both the subnormal spacing and the spacing at the
/// smallest normal, so the two branches meet exactly at 2^-14.
float f16_spacing(float v) {
    const float a = std::fabs(v);
    if (a < 0x1p-14f) return 0x1p-24f;
    return std::ldexpf(1.0f, std::ilogbf(a) - 10);
}

/// How far apart the two legs' fp16 sum fields may be. Each leg's fp32 sum is within
/// 31 * eps32 * sum|x| of the exact sum (Higham: a 32-term recursive sum; the 5-level butterfly does
/// the same 31 additions), and each fp16 store rounds by at most half a spacing, so the stored halfs
/// are within 62 * eps32 * sum|x| + one spacing of each other. The sum|x| term is what makes this a
/// bound rather than a guess: a block that cancels to near zero keeps an absolute fp32 error of
/// ~eps * sum|x| while its fp16 spacing collapses, so a fixed "within N ULP" rule cannot hold there
/// (measured: 195k of 200k deliberately near-cancelling blocks flip their sum field, and every one of
/// them is inside this bound; so is every one of the 150 flips in 200k N(0,1) blocks).
float sum_slack(double sum_abs, float a, float b) {
    const float fa = std::fabs(a), fb = std::fabs(b);
    return 62.0f * FLT_EPSILON * (float) sum_abs + f16_spacing(fa > fb ? fa : fb);
}

/// One GPU leg against quantize_q8_1_cpu. Block bytes 0..1 and 4..35 must be byte-identical; the
/// bytes 2..3 sum field must be inside sum_slack for its own block. Reports the first byte that must
/// be identical and is not (kNoDiff if none), how many blocks' sum field differs at all, and the
/// widest drift as a ratio of its own bound (>1 fails; a NaN drift is a failure, not a zero).
struct Leg {
    size_t hard_diff = kNoDiff;
    size_t sum_flips = 0;
    double worst = 0.0;
    bool ok() const { return hard_diff == kNoDiff && worst <= 1.0; }
};

Leg compare_leg(const std::vector<uint8_t>& gpu, const std::vector<uint8_t>& cpu,
                const std::vector<float>& x) {
    Leg r;
    for (size_t ib = 0; ib * kBlockBytes < gpu.size(); ++ib) {
        const uint8_t* g = gpu.data() + ib * kBlockBytes;
        const uint8_t* c = cpu.data() + ib * kBlockBytes;
        for (size_t b = 0; b < kBlockBytes; ++b) {
            if (b == kSumLo || b == kSumHi) continue;   // ULP-close by contract, checked below
            if (g[b] != c[b] && r.hard_diff == kNoDiff) r.hard_diff = ib * kBlockBytes + b;
        }
        const uint16_t gb = (uint16_t) g[kSumLo] | ((uint16_t) g[kSumHi] << 8);
        const uint16_t cb = (uint16_t) c[kSumLo] | ((uint16_t) c[kSumHi] << 8);
        if (gb == cb) continue;
        const float gv = f16_to_f32(gb), cv = f16_to_f32(cb);
        double sum_abs = 0.0;
        for (int i = 0; i < 32; ++i) sum_abs += std::fabs((double) x[ib * 32 + i]);
        ++r.sum_flips;
        const double ratio = std::fabs(gv - cv) / (double) sum_slack(sum_abs, gv, cv);
        if (!(ratio <= r.worst)) r.worst = ratio;   // negated so a NaN ratio registers as a failure
    }
    return r;
}

}  // namespace

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
    size_t cases = 0, bad = 0, flips = 0;
    double worst_ratio = 0.0;
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
            ck(cudaGraphLaunch(gx, s));
            ck(cudaGraphLaunch(gx, s));   // replayed twice: the second must rewrite the same bytes
            ck(cudaStreamSynchronize(s));
            cudaGraphExecDestroy(gx);
            cudaGraphDestroy(g);
            std::vector<uint8_t> rows(bytes), rows_g(bytes), nat(bytes);
            ck(cudaMemcpy(rows.data(), d_rows, bytes, cudaMemcpyDeviceToHost));
            ck(cudaMemcpy(rows_g.data(), d_rows_g, bytes, cudaMemcpyDeviceToHost));
            ck(cudaMemcpy(nat.data(), d_nat, bytes, cudaMemcpyDeviceToHost));
            const Leg lr = compare_leg(rows, cpu_ref, x);
            const Leg lg = compare_leg(rows_g, cpu_ref, x);
            const Leg ln = compare_leg(nat, cpu_ref, x);
            // the ring's own claim, with no tolerance anywhere: the producer, what the graph replays,
            // and the native pool reference are bit-identical to each other across all 36 bytes.
            const bool legs_identical = rows == rows_g && rows == nat;
            ++cases;
            flips += lr.sum_flips + lg.sum_flips + ln.sum_flips;
            for (const Leg& l : {lr, lg, ln}) worst_ratio = worst_ratio < l.worst ? l.worst : worst_ratio;
            if (lr.ok() && lg.ok() && ln.ok() && legs_identical) continue;
            const auto hard = [](const Leg& l) {
                return l.hard_diff == kNoDiff ? std::string("-") : std::to_string(l.hard_diff);
            };
            std::printf("FAIL T=%d %s: rows%s replay%s native%s%s (first hard diffs %s/%s/%s; "
                        "sum-field flips %zu/%zu/%zu, worst drift %.2fx its bound)\n",
                        t, rotated ? "rotated" : "plain", lr.ok() ? "==" : "!=", lg.ok() ? "==" : "!=",
                        ln.ok() ? "==" : "!=", legs_identical ? "" : " LEGS-NOT-IDENTICAL",
                        hard(lr).c_str(), hard(lg).c_str(), hard(ln).c_str(),
                        lr.sum_flips, lg.sum_flips, ln.sum_flips, worst_ratio);
            ++bad;
        }
    ck(cudaFree(dx));
    ck(cudaFree(d_rows));
    ck(cudaFree(d_rows_g));
    ck(cudaFree(d_nat));
    ck(cudaStreamDestroy(s));
    std::printf("xq_triple_parity: %zu cases (T 1..8, plain + rotated, direct + graph replay), %zu "
                "sum-field drifts within the fp32-order bound (worst %.2fx of it), %zu failures\n",
                cases, flips, worst_ratio, bad);
    return bad ? 1 : 0;
}
