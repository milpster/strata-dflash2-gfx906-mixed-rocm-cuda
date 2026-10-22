// tests/core/dflash_tap_collect_test.cpp - GPU proof of the cross-device tap transport
// (include/strata/core/dflash_tap_collect.hpp) on a two-device box: synthetic producer
// planes per stage, the CPU-expected assembled plane, repeated rounds for slot reuse,
// and the fail-closed refusals.  Self-skips (77) below 2 devices.  Parent executes; the
// implementation lane only compiles it (docs/DFLASH2.md capture contract, split32).
#include <strata/core/dflash_tap_collect.hpp>
#include <strata/core/on_device.hpp>

#include <cuda_runtime.h>

#include <array>
#include <chrono>
#include <cstdio>
#include <string>
#include <thread>
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
        // Review item 3: the public boundary validates type_bytes once.
        check(!tc.stage(9002, s0, 3, err) && err.find("type_bytes") != std::string::npos,
              "a type_bytes outside 2/4 is refused at the boundary");
        // Review item 3: assemble respects the producer's advertised rows - rows past them are a stale read.
        const DFlashTapCollector::Slice short0{0, plane0, kF32Rows, 2}, wide1{1, plane1, kF32Rows, 4};
        const DFlashTapCollector::Slice pair[2] = {short0, wide1};
        check(!tc.assemble(9003, pair, 2, 4, 4, err) && err.find("stale read") != std::string::npos,
              "assemble rows past a producer's advertised rows are refused (Slice.rows respected)");
        // Review item 3: a producer staged twice for the same chunk key is refused before consumption.
        check(tc.stage(9004, s0, 4, err) && !tc.stage(9004, s0, 4, err) && err.find("staged twice") != std::string::npos,
              "duplicate staging of the same tap for one chunk key is refused");
        // Review item 2: reset_staging drops bindings so a new conversation reusing the key is not
        // blocked by the aborted request's rows guard.
        tc.reset_staging();
        const DFlashTapCollector::Slice other_rows{0, plane0, kF32Rows, 3};
        check(tc.stage(9004, other_rows, 4, err), "after reset_staging the same key re-binds with new rows");
        tc.reset_staging();
    }

    // Review item 2 + L10 fix: the ACTUAL race - a producer thread stages chunk N+1 while
    // the drafter thread assembles chunk N (prefill.cpp:4948-4963 overlap).  Each chunk is
    // staged ONCE and assembled ONCE, consumed exactly where the real chain consumes it.
    // The drafter-local producer plane PING-PONGS (plane1 / plane1b) so the previous
    // chunk's D2D reads its own plane - the per-chunk plane lifetime of the real chain.
    // Tag discipline: planes carry tag r when chunk key_r is staged; assembling key_r must
    // leave tag r on the collector plane.
    {
        bool overlap_ok = true;
        const int kRounds = 8;
        float* plane1b = nullptr;
        {
            const OnDevice on(drafter);
            overlap_ok = cudaMalloc((void**) &plane1b, (size_t) 5 * kF32Rows * kN * 4) == cudaSuccess;
        }
        if (!overlap_ok) std::printf("FAIL overlap: drafter ping-pong plane allocation\n");
        std::vector<float> host((size_t) 5 * kF32Rows * kN), got((size_t) 5 * kF32Rows * kN);
        const auto fill_round = [&](int round) {   // fills THIS chunk's planes with its tag
            const int tag = round;
            for (int t = 0; t < 5; ++t)
                for (int r = 0; r < kF32Rows; ++r)
                    for (int c = 0; c < kN; ++c)
                        host[(size_t) t * kF32Rows * kN + (size_t) r * kN + c] = f32_val(t, r, c, tag);
            { const OnDevice on(dev0); (void) cudaMemcpy(plane0, host.data(), host.size() * 4, cudaMemcpyHostToDevice); }
            { const OnDevice on(drafter);
              (void) cudaMemcpy(round % 2 == 0 ? plane1 : plane1b, host.data(), host.size() * 4, cudaMemcpyHostToDevice); }
        };
        const auto verify_plane = [&](int tag) {
            if (cudaMemcpy(got.data(), tc.plane_f32(), got.size() * 4, cudaMemcpyDeviceToHost) != cudaSuccess)
                return false;
            for (int t = 0; t < 5; ++t)
                for (int r = 0; r < 4; ++r)
                    for (int c = 0; c < kN; c += 127)
                        if (got[(size_t) t * kF32Rows * kN + (size_t) r * kN + c] != f32_val(t, r, c, tag))
                            return false;
            return true;
        };
        const auto local_slice = [&](int round) { return DFlashTapCollector::Slice{1, round % 2 == 0 ? plane1 : plane1b, kF32Rows, 4}; };
        const auto pair_for = [&](int round) {
            return std::array<DFlashTapCollector::Slice, 2>{s0, local_slice(round)};
        };
        std::string thread_err;
        for (int round = 0; round < kRounds && overlap_ok; ++round) {
            const int64_t key = 2000 + (int64_t) round * 100;
            const int64_t prev = key - 100;
            fill_round(round);   // THIS chunk's tag: the previous chunk's ping-pong plane stays intact
            std::thread producer([&tc, &s0, &thread_err, key] {
                const OnDevice on(dev0);
                if (!tc.stage(key, s0, 4, thread_err)) thread_err = "stage(producer): " + thread_err;
            });
            {   // the drafter thread stages this chunk's local slice and assembles the PREVIOUS
                // one - staged last round, consumed exactly once here (the real overlap).
                const OnDevice on(drafter);
                overlap_ok = tc.stage(key, local_slice(round), 4, err);
                if (round > 0)
                    overlap_ok = overlap_ok && tc.assemble(prev, pair_for(round - 1).data(), 2, 4, 4, err) && verify_plane(round - 1);
            }
            producer.join();
            overlap_ok = overlap_ok && thread_err.empty();
            if (!overlap_ok) { std::printf("FAIL overlap round %d: %s / %s\n", round, err.c_str(), thread_err.c_str()); break; }
        }
        {   // the prefill tail: consume the last staged chunk exactly once
            const OnDevice on(drafter);
            overlap_ok = overlap_ok && tc.assemble(2000 + (int64_t) (kRounds - 1) * 100, pair_for(kRounds - 1).data(), 2, 4, 4, err)
                         && verify_plane(kRounds - 1);
        }
        check(overlap_ok, "overlapping producer staging + drafter assembly keep slots and planes consistent");
        { const OnDevice on(drafter); if (plane1b != nullptr) cudaFree(plane1b); }
    }

    // L10 cancellation-waiter fixture (real, on the enabled collector): park a producer in
    // backpressure (both slots STAGED), then reset_staging() - enabled() STAYS true, so
    // only the epoch token can reject the old request's producer.  The wake must return a
    // deterministic refusal, never stage the stale chunk, and never hang.
    {
        bool cancel_ok = true;
        cancel_ok = tc.stage(6000, s0, 4, err) && tc.stage(6100, s0, 4, err) && cancel_ok;
        const uint64_t epoch0 = tc.staging_epoch();
        std::string perr;
        bool pres = true;
        std::thread producer([&] {
            const OnDevice on(dev0);
            pres = tc.stage(6200, s0, 4, perr);   // both slots STAGED -> parks on cv_
        });
        std::this_thread::sleep_for(std::chrono::milliseconds(120));
        tc.reset_staging();   // epoch++ under mu_ + notify_all: the parked producer must reject
        producer.join();
        cancel_ok = cancel_ok && !pres && perr.find("cancelled") != std::string::npos;
        check(cancel_ok, "parked producer rejects after reset (epoch-guarded, collector still enabled)");
        check(tc.staging_epoch() != epoch0, "reset_staging increments the cancellation epoch");
        check(tc.stage(6300, s0, 4, err), "after the reset, a NEW request stages cleanly (slots cleared)");
    }

    // L14 consumer-ordering fixture (the dflash_w3 model window's "tap 0 was never
    // staged"): a THREE-stage plan - stage 0 AND stage 1 both on dev0 (two FOREIGN
    // producers; taps owned by a stage ON the drafter device never stage - assemble
    // copies those D2D from the owner's slice, and stage() skips them by
    // tap_device(t) == dev, dflash_tap_collect.cpp - parent-found in GPU window 4:
    // the earlier drafter-device stage 1 made the "partial" producer call a no-op and
    // assemble correctly fast-refused), stage 2 on the drafter (D2D taps).
    // The consumer (assemble) may arrive while a producer is still in flight: the
    // collector must WAIT (bounded, epoch-guarded), complete with the exact per-key
    // tags, and reject deterministically when reset_staging() lands mid-wait.
    {
        DFlashTapPlan plan3;
        const int lbs3[3] = {0, 16, 32}, les3[3] = {16, 32, 48}, devs3[3] = {dev0, dev0, drafter};
        if (!plan3.build(bounds, 5, lbs3, les3, devs3, 3, kN, err)) {
            std::printf("FAIL plan3: %s\n", err.c_str());
            ++failures;
        }
        DFlashTapCollector tc3;
        bool w_ok = plan3.n_taps == 5;
        if (w_ok) {
            const OnDevice on(drafter);
            w_ok = tc3.enable(plan3, drafter, kF32Rows, kPfRows, err);
        }
        check(w_ok, "L14: three-stage collector enables (2 staging producers + 1 D2D tap)");
        if (w_ok) {
            // tag 100 in every producer plane, rows 0..3 carry the chunk
            std::vector<float> host((size_t) 5 * kF32Rows * kN), got((size_t) 5 * kF32Rows * kN);
            const auto fill = [&](int tag) {
                for (int t = 0; t < 5; ++t)
                    for (int r = 0; r < kF32Rows; ++r)
                        for (int c = 0; c < kN; ++c)
                            host[(size_t) t * kF32Rows * kN + (size_t) r * kN + c] = f32_val(t, r, c, tag);
                { const OnDevice on(dev0); (void) cudaMemcpy(plane0, host.data(), host.size() * 4, cudaMemcpyHostToDevice); }
                { const OnDevice on(drafter); (void) cudaMemcpy(plane1, host.data(), host.size() * 4, cudaMemcpyHostToDevice); }
            };
            fill(100);
            const DFlashTapCollector::Slice sl0{0, plane0, kF32Rows, 4};
            const DFlashTapCollector::Slice sl1{1, plane1, kF32Rows, 4};
            const DFlashTapCollector::Slice sl2{2, plane1, kF32Rows, 4};
            const DFlashTapCollector::Slice all3[3] = {sl0, sl1, sl2};

            // (a) The proven failing shape with a healthy producer chain: the consumer
            //     arrives AFTER one producer staged and BEFORE the lagging one.
            //     assemble() must block, wake on the lagging stage(), and assemble the
            //     exact tag-100 plane - the per-key tag discipline is verified.
            bool a_ok = true;
            { const OnDevice on(drafter); a_ok = tc3.stage(7000, sl1, 4, err); }   // taps 1,2 staged (stage 1, foreign: partial - tap 0 still missing)
            std::string cerr;
            bool a_ran = false;
            std::this_thread::sleep_for(std::chrono::milliseconds(30));
            std::thread consumer([&] {
                const OnDevice on(drafter);
                a_ran = tc3.assemble(7000, all3, 3, 4, 4, cerr);   // parks: tap 0/1 unstaged
            });
            std::this_thread::sleep_for(std::chrono::milliseconds(170));
            { const OnDevice on(dev0); (void) tc3.stage(7000, sl0, 4, err); }      // the lagging producer
            consumer.join();
            {   // tag-100 everywhere: taps 0,1 from pinned staging, 2,3 staged, 4 D2D
                const OnDevice on(drafter);
                a_ok = a_ok && a_ran && cerr.empty() &&
                       cudaMemcpy(got.data(), tc3.plane_f32(), got.size() * 4, cudaMemcpyDeviceToHost) == cudaSuccess;
                for (int t = 0; t < 5 && a_ok; ++t)
                    for (int r = 0; r < 4 && a_ok; ++r)
                        for (int c = 0; c < kN && a_ok; c += 127)
                            a_ok = got[(size_t) t * kF32Rows * kN + (size_t) r * kN + c] == f32_val(t, r, c, 100);
            }
            check(a_ok, "L14: assemble blocks for the lagging producer, completes once staged, exact per-key tags");

            // (b) reset_staging() during a consumer's wait: deterministic epoch refusal,
            //     no hang, no plane touch (enabled() stays true - only the epoch rejects).
            bool b_ok = true;
            { const OnDevice on(drafter); b_ok = tc3.stage(7100, sl1, 4, err); }
            std::string berr("unset");
            bool b_ran = true;
            std::this_thread::sleep_for(std::chrono::milliseconds(30));
            std::thread consumer2([&] {
                const OnDevice on(drafter);
                b_ran = tc3.assemble(7100, all3, 3, 4, 4, berr);   // parks: taps 0/1 unstaged
            });
            std::this_thread::sleep_for(std::chrono::milliseconds(120));
            tc3.reset_staging();   // epoch++ + notify: the parked consumer must reject
            consumer2.join();
            b_ok = b_ok && !b_ran && berr.find("cancelled") != std::string::npos;
            check(b_ok, "L14: reset during a consumer's wait rejects deterministically (epoch-guarded, no hang)");
            tc3.reset_staging();

            // (c) NO producer staged at all: the refusal stays FAST (the fail-closed
            //     contract - a broken chain fails loudly, the consumer does not park).
            const auto t0 = std::chrono::steady_clock::now();
            bool fast = false;
            {
                const OnDevice on(drafter);
                std::string ferr;
                fast = !tc3.assemble(7200, all3, 3, 4, 4, ferr) && ferr.find("was never staged") != std::string::npos;
            }
            const auto ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
            check(fast && ms < 2000.0, "L14: zero staged flags for a key refuse immediately (no consumer park)");

            // (d) L15 null-plane contract (the dflash_w3 decode's "producer plane is
            //     null"): the slice of the stage that owns ONLY drafter-device taps
            //     (stage 2, tap 4) must not refuse staging when its verifier never
            //     carved a tap plane - it contributes nothing to staging.  The slice of
            //     a stage that owns FOREIGN taps with a null plane (stage 0, taps 0,1)
            //     keeps the loud refusal, and its taps stay unstaged so assemble also
            //     refuses (no null-source D2D, no stale-plane read).
            {
                const OnDevice on(drafter);
                const DFlashTapCollector::Slice null_local{2, nullptr, kF32Rows, 4};
                std::string nerr;
                check(tc3.stage(7300, null_local, 4, nerr) && nerr.empty(),
                      "L15: a null-plane slice owning ONLY drafter-device taps does not refuse staging");
                const DFlashTapCollector::Slice null_foreign{0, nullptr, kF32Rows, 4};
                nerr.clear();
                check(!tc3.stage(7300, null_foreign, 4, nerr) && nerr.find("producer plane is null") != std::string::npos,
                      "L15: a null-plane slice owning FOREIGN taps keeps the loud refusal");
                const DFlashTapCollector::Slice all_mix[3] = {null_foreign, sl1, null_local};
                nerr.clear();
                check(!tc3.assemble(7300, all_mix, 3, 4, 4, nerr) &&
                      (nerr.find("stale read") != std::string::npos || nerr.find("never staged") != std::string::npos),
                      "L15: assemble with the null-plane foreign producer refuses (no null-source copy, no stale read)");
                tc3.reset_staging();
            }
            tc3.disable();
        }
    }

    tc.disable();
    std::printf(failures == 0 ? "dflash_tap_collect_test: ALL PASS\n" : "dflash_tap_collect_test: %d FAILURES\n",
                failures);
    return failures == 0 ? 0 : 1;
}
