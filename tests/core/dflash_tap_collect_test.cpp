// tests/core/dflash_tap_collect_test.cpp - GPU proof of the cross-device tap transport
// (include/strata/core/dflash_tap_collect.hpp) on a two-device box: synthetic producer
// planes per stage, the CPU-expected assembled plane, repeated rounds for slot reuse,
// and the fail-closed refusals.  Self-skips (77) below 2 devices.  Parent executes; the
// implementation lane only compiles it (docs/DFLASH2.md capture contract, split32).
#include <strata/core/dflash_tap_collect.hpp>
#include <strata/core/on_device.hpp>

#include <cuda_runtime.h>

#include <cstdio>
#include <string>
#include <vector>

using namespace strata::core;

namespace {

int failures = 0;

void check(bool ok, const char* what) {
    std::printf("%s %s\n", ok ? "ok  " : "FAIL", what);
    if (!ok) ++failures;
}

constexpr int kN = 2560;        // the draft's hidden width (the geometry's 2560)
constexpr int kF32Rows = 8;     // the verify window cap (kVerifyMaxT)
constexpr int kPfRows = 16;     // a small prefill chunk cap for the BF16 round

// The synthetic producer pattern: exact in FP32 (< 2^24) and BF16-representable.
float f32_val(int tap, int row, int col, int tag) { return (float) (tap * 100000 + row * 1000 + col + tag); }

/// Fill one device plane [n_taps][stride][N] FP32 with the pattern for its stage.
bool fill_f32(float* plane, int n_taps, int64_t stride, int tag) {
    std::vector<float> host((size_t) n_taps * stride * kN);
    for (int t = 0; t < n_taps; ++t)
        for (int64_t r = 0; r < stride; ++r)
            for (int c = 0; c < kN; ++c)
                host[(size_t) t * stride * kN + (size_t) r * kN + c] = f32_val(t, (int) r, c, tag);
    return cudaMemcpy(plane, host.data(), host.size() * 4, cudaMemcpyHostToDevice) == cudaSuccess;
}

}  // namespace

int main() {
    int ndev = 0;
    cudaGetDeviceCount(&ndev);
    if (ndev < 2) {
        std::printf("dflash_tap_collect_test: needs 2 GPUs (found %d) - SKIP\n", ndev);
        return 77;
    }
    const int dev0 = 0, drafter = 1;
    std::string err;

    // split32 ownership: GPU0 captures boundaries 6,16,26; the drafter's GPU1 captures 36,46.
    DFlashTapPlan plan;
    const int bounds[5] = {6, 16, 26, 36, 46};
    const int lbs[2] = {0, 32}, les[2] = {32, 48}, devs[2] = {dev0, drafter};
    if (!plan.build(bounds, 5, lbs, les, devs, 2, kN, err)) {
        std::printf("FAIL plan: %s\n", err.c_str());
        return 1;
    }

    DFlashTapCollector tc;
    {
        const OnDevice on(drafter);
        check(tc.enable(plan, drafter, kF32Rows, kPfRows, err), "collector enables on the drafter's device");
    }
    if (failures) return 1;

    // Producer planes on their own devices (the verifier tap planes' shape).
    float *plane0 = nullptr, *plane1 = nullptr;
    const size_t plane_bytes = (size_t) 5 * kF32Rows * kN * 4;
    {
        const OnDevice on(dev0);
        check(cudaMalloc((void**) &plane0, plane_bytes) == cudaSuccess, "producer plane CUDA0 allocates");
        check(fill_f32(plane0, 5, kF32Rows, 0), "producer plane CUDA0 filled (tag 0)");
    }
    {
        const OnDevice on(drafter);
        check(cudaMalloc((void**) &plane1, plane_bytes) == cudaSuccess, "producer plane CUDA1 allocates");
        check(fill_f32(plane1, 5, kF32Rows, 1), "producer plane CUDA1 filled (tag 1)");
    }
    if (failures) return 1;

    // One verify window: stage the compact slices, assemble, compare against the CPU model.
    const DFlashTapCollector::Slice s0{0, plane0, kF32Rows, 4}, s1{1, plane1, kF32Rows, 4};
    const DFlashTapCollector::Slice both[2] = {s0, s1};
    for (int round = 0; round < 4; ++round) {   // repeated copies exercise the slot ring + events
        const int64_t key = 1000 + round * kF32Rows;   // the window's absolute position
        {
            const OnDevice on(drafter);
            if (!tc.stage(key, s0, 4, err) || !tc.stage(key, s1, 4, err)) {
                std::printf("FAIL round %d stage: %s\n", round, err.c_str());
                ++failures;
                break;
            }
            if (!tc.assemble(key, both, 2, 4, 4, err)) {
                std::printf("FAIL round %d assemble: %s\n", round, err.c_str());
                ++failures;
                break;
            }
        }
        std::vector<float> got((size_t) 5 * kF32Rows * kN);
        {
            const OnDevice on(drafter);
            if (cudaMemcpy(got.data(), tc.plane_f32(), got.size() * 4, cudaMemcpyDeviceToHost) != cudaSuccess) {
                std::printf("FAIL round %d readback\n", round);
                ++failures;
                break;
            }
        }
        bool ok = true;
        for (int t = 0; t < 5 && ok; ++t)
            for (int r = 0; r < 4 && ok; ++r)
                for (int c = 0; c < kN && ok; c += 127)   // sampled columns: the pattern is positional
                    ok = got[(size_t) t * kF32Rows * kN + (size_t) r * kN + c] == f32_val(t, r, c, t < 3 ? 0 : 1);
        check(ok, "round: assembled plane matches the per-stage CPU model in tap order");
        if (!ok) break;
    }

    // BF16 prompt round: the prefill kind transports 3 GPU0 taps and D2Ds the GPU1 slice.
    {
        const size_t pf_bytes = (size_t) 5 * kPfRows * kN * 2;
        uint16_t *pf0 = nullptr, *pf1 = nullptr;
        const auto fill_bf16 = [&](uint16_t* plane, uint16_t* scratch, int tag) {
            for (int t = 0; t < 5; ++t)
                for (int r = 0; r < kPfRows; ++r)
                    for (int c = 0; c < kN; ++c)
                        scratch[(size_t) t * kPfRows * kN + (size_t) r * kN + c] =
                            (uint16_t) ((t * 64 + r) * 4 + tag);   // exact in BF16 (multiples of 4)
            return cudaMemcpy(plane, scratch, pf_bytes, cudaMemcpyHostToDevice) == cudaSuccess;
        };
        std::vector<uint16_t> scratch((size_t) 5 * kPfRows * kN);
        bool alloc_ok = false;
        {
            const OnDevice on(dev0);
            alloc_ok = cudaMalloc((void**) &pf0, pf_bytes) == cudaSuccess && fill_bf16(pf0, scratch.data(), 0);
        }
        {
            const OnDevice on(drafter);
            alloc_ok = alloc_ok && cudaMalloc((void**) &pf1, pf_bytes) == cudaSuccess && fill_bf16(pf1, scratch.data(), 1);
        }
        check(alloc_ok, "BF16 producer planes allocate and fill");
        if (!alloc_ok) return 1;
        const DFlashTapCollector::Slice p0{0, pf0, kPfRows, 6}, p1{1, pf1, kPfRows, 6};
        const DFlashTapCollector::Slice pair[2] = {p0, p1};
        bool round_ok = false;
        {
            const OnDevice on(drafter);
            round_ok = tc.stage(500, p0, 2, err) && tc.stage(500, p1, 2, err) && tc.assemble(500, pair, 2, 6, 2, err);
            std::vector<uint16_t> got((size_t) 5 * kPfRows * kN);
            round_ok = round_ok && cudaMemcpy(got.data(), tc.plane_bf16(), got.size() * 2, cudaMemcpyDeviceToHost) == cudaSuccess;
            for (int t = 0; t < 5 && round_ok; ++t)
                for (int r = 0; r < 6 && round_ok; ++r)
                    for (int c = 0; c < kN && round_ok; c += 511)
                        round_ok = got[(size_t) t * kPfRows * kN + (size_t) r * kN + c] ==
                                   (uint16_t) ((t * 64 + r) * 4 + (t < 3 ? 0 : 1));
        }
        check(round_ok, "BF16 prompt chunk: rows 0..5 of every tap arrive on the drafter's card");
    }

    // Fail-closed refusals: a missing producer and an over-long row count never touch the plane.
    {
        const OnDevice on(drafter);
        const DFlashTapCollector::Slice only1{1, plane1, kF32Rows, 4};
        err.clear();
        const DFlashTapCollector::Slice one[1] = {only1};
        check(!tc.assemble(9000, one, 1, 4, 4, err) && err.find("producer slices") != std::string::npos,
              "assemble with a missing producer stage refuses (no stale-plane read)");
        const DFlashTapCollector::Slice too_long{0, plane0, kF32Rows, kF32Rows + 1};
        check(!tc.stage(9001, too_long, 4, err), "rows beyond the plane capacity are refused");
    }

    tc.disable();
    std::printf(failures == 0 ? "dflash_tap_collect_test: ALL PASS\n" : "dflash_tap_collect_test: %d FAILURES\n",
                failures);
    return failures == 0 ? 0 : 1;
}
