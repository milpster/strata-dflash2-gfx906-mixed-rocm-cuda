// tests/core/swapin_depth_test.cpp - CPU checks for the round-15 pack-file read window
// (include/strata/core/swapin_depth.hpp): STRATA_SWAPIN_DEPTH's parsing and clamps, the
// swapin_window resolution the stager applies verbatim (8-deep default equivalence against the
// pre-knob rules, the N-deep window, and the surgical precedence of STRATA_STAGER_BATCH and
// STRATA_STAGER_RING), and the batched read accounting a fake pack source sees behind that
// window - the same run arithmetic the stager's claim uses, the same pairs/batches meter the
// "adapt swap-in" line reports.  Pure host arithmetic, no device, no files, no links.
#include "strata/core/swapin_depth.hpp"

#include <cstdint>
#include <cstdio>

using namespace strata::core;

static int failures = 0;
static void check(bool ok, const char* what) {
    if (!ok) {
        std::printf("FAIL: %s\n", what);
        ++failures;
    }
}

/// The pre-knob rules the stager applied (prefill.cpp before round 15), spelled out here so the
/// default-equivalence walk below is a check against the OLD code, not a restatement of the new:
///   ring = STRATA_STAGER_RING given ? clamp(atoi, 2, 256) : (files ? 4 * threads : 16)
///   run  = min(clamp(STRATA_STAGER_BATCH given ? atoi : (files ? 8 : 1), 1, 32), ring)
static SwapinWindow today(const char* batch_env, const char* ring_env, bool files, int threads) {
    int ring = files ? 4 * threads : 16;
    if (ring_env != nullptr) {
        const long v = std::atol(ring_env);
        ring = (int) (v < 2 ? 2 : (v > 256 ? 256 : v));
    }
    long want = batch_env != nullptr ? std::atol(batch_env) : (files ? 8 : 1);
    if (want < 1) want = 1;
    if (want > 32) want = 32;
    const int run = want < ring ? (int) want : ring;
    return SwapinWindow{run, ring, false};
}

int main() {
    // 1. STRATA_SWAPIN_DEPTH parsing: absent, empty, "0", negative and garbage are the default 8
    //    (today's exact scheduling); a plain value is itself; 512 is the clamp.
    check(swapin_depth(nullptr) == 8, "no env: the depth is the 8 default");
    check(swapin_depth("") == 8, "empty env: the depth is the 8 default");
    check(swapin_depth("0") == 8, "\"0\": the depth is the 8 default");
    check(swapin_depth("-64") == 8, "negative: the depth is the 8 default");
    check(swapin_depth("garbage") == 8, "garbage env: the depth is the 8 default");
    check(swapin_depth("8") == 8, "\"8\": the default, spelled out");
    check(swapin_depth("1") == 1, "\"1\": the shallowest window");
    check(swapin_depth("32") == 32, "\"32\": a suggested value");
    check(swapin_depth("64") == 64, "\"64\": a suggested value");
    check(swapin_depth("128") == 128, "\"128\": a suggested value");
    check(swapin_depth("512") == 512, "\"512\": the top of the clamp");
    check(swapin_depth("513") == 512, "one past the top clamps to 512");
    check(swapin_depth("99999999") == 512, "an absurd value clamps to 512");

    // 2. Default equivalence: with the depth knob ABSENT, the window is today's number pair for
    //    every combination of the other knobs, both profiles and the thread counts 1..32 - the
    //    default scheduling is bit-for-bit and nothing notes a change.
    const char* batch_envs[] = {nullptr, "", "0", "1", "8", "16", "31", "32", "33", "200", "-4", "garbage"};
    const char* ring_envs[] = {nullptr, "", "0", "2", "16", "64", "256", "257", "-8", "junk"};
    for (int files = 0; files <= 1; ++files)
        for (int threads = 1; threads <= 32; threads += 31)
            for (const char* bv : batch_envs)
                for (const char* rv : ring_envs) {
                    const SwapinWindow w = swapin_window(nullptr, bv, rv, files != 0, threads);
                    const SwapinWindow t = today(bv, rv, files != 0, threads);
                    check(w.run == t.run && w.ring == t.ring && !w.note,
                          "no depth knob: the window is today's, whatever the other knobs say");
                }

    // 3. The window with the knob: the run AND the ring widen together (a run never wraps onto its
    //    own unfinished buffers), the ring stays at least 4*threads, and the note fires.
    check(swapin_window("64", nullptr, nullptr, true, 32).run == 64, "depth 64: 64-blob runs");
    check(swapin_window("64", nullptr, nullptr, true, 32).ring == 128, "depth 64 at 32 threads: the ring stays 128");
    check(swapin_window("64", nullptr, nullptr, true, 4).ring == 64, "depth 64 at 4 threads: the ring widens to 64");
    check(swapin_window("64", nullptr, nullptr, true, 4).run == 64, "depth 64 at 4 threads: the run is not ring-capped");
    check(swapin_window("128", nullptr, nullptr, true, 2).run == 128 && swapin_window("128", nullptr, nullptr, true, 2).ring == 128,
          "depth 128 at 2 threads: 128-deep window");
    check(swapin_window("512", nullptr, nullptr, true, 32).run == 512 && swapin_window("512", nullptr, nullptr, true, 32).ring == 512,
          "depth 512: the top of the clamp, ring and run");
    check(swapin_window("64", nullptr, nullptr, true, 32).note, "a widened window notes itself");
    check(!swapin_window("8", nullptr, nullptr, true, 32).note, "the default spelled out changes nothing to note");

    // 4. Surgical precedence: the older knobs keep the sides they own.
    check(swapin_window("128", "1", nullptr, true, 32).run == 1, "an explicit STRATA_STAGER_BATCH=1 wins the run size (the A/B arm)");
    check(swapin_window("128", "1", nullptr, true, 32).ring == 128, "the depth still owns the ring");
    check(swapin_window("128", nullptr, "16", true, 32).ring == 16, "an explicit STRATA_STAGER_RING=16 wins the ring");
    check(swapin_window("128", nullptr, "16", true, 32).run == 16, "the explicit ring caps the run (no buffer wrap)");
    check(swapin_window("128", "200", nullptr, true, 32).run == 128, "the depth lifts the 32 run cap; the ring still bounds an explicit batch");
    check(swapin_window("128", nullptr, nullptr, false, 32).run == 1 && swapin_window("128", nullptr, nullptr, false, 32).ring == 16,
          "the RAM profile is untouched: run 1, ring 16, no note");
    check(!swapin_window("128", nullptr, nullptr, false, 32).note, "no note off the pack-file profile");

    // 5. The batched read a fake pack source sees behind the window: runs of at most `run` jobs
    //    claimed in order (the stager's claim arithmetic), each run served as ONE serialized
    //    read_blobs_batch - so the meter's pairs/batches is the depth the "adapt swap-in" line
    //    reports.  Default 8 is the measured flat-~1-GiB/s shape; 64/128 cut the batch count by
    //    the window factor; skipped jobs (a run the routing emptied) never reach the reader.
    struct FakeSource {                     // the relief's serialized file batch, one run at a time
        int64_t pairs = 0, batches = 0, max_run = 0;
        void read_blobs_batch(int64_t k) {
            pairs += k;
            batches += 1;
            if (k > max_run) max_run = k;
        }
    };
    const auto walk = [](int64_t run, int64_t n_jobs, int64_t skip_every, FakeSource& s) {
        for (int64_t j = 0; j < n_jobs;) {
            const int64_t k = run < n_jobs - j ? run : n_jobs - j;   // Stager::claim: min(want, n - j)
            int64_t live = 0;
            for (int64_t i = j; i < j + k; ++i)                       // the run's skipped jobs are not read
                if (!(skip_every > 0 && i % skip_every == 0)) ++live;
            if (live > 0) s.read_blobs_batch(live);
            j += k;
        }
    };
    for (int64_t depth : {8, 32, 64, 128, 512}) {
        FakeSource s;
        walk(depth, 1000, 0, s);
        char what[96];
        std::snprintf(what, sizeof what, "depth %lld: every job is read exactly once", (long long) depth);
        check(s.pairs == 1000, what);
        std::snprintf(what, sizeof what, "depth %lld: the batches are ceil(1000/depth)", (long long) depth);
        check(s.batches == (1000 + depth - 1) / depth, what);
        std::snprintf(what, sizeof what, "depth %lld: no run exceeds the window", (long long) depth);
        check(s.max_run <= depth, what);
        std::snprintf(what, sizeof what, "depth %lld: the meter's pairs/batch IS the window", (long long) depth);
        check(s.batches * depth - s.pairs < depth, what);   // only the last run may be short
    }
    {
        FakeSource s;
        walk(8, 1000, 0, s);
        check(s.pairs / s.batches == 8, "the default window meters 8 swaps per batch (the measured shape)");
        FakeSource deep;
        walk(64, 1000, 0, deep);
        check(deep.pairs == 1000 && deep.batches == 16,
              "a 64-deep window serves the same 1000 pairs in 16 batches (the last run short)");
    }
    {
        FakeSource s;
        walk(128, 1000, 7, s);            // every 7th job skipped, as the layer-major routing can
        check(s.pairs == 1000 - (1000 + 6) / 7, "skipped jobs are never read");
        check(s.max_run <= 128, "skips only shrink runs, never past the window");
    }

    // 6. The deep window's RAM bound: only a batch past the per-thread scratch cap (64 MiB - the
    //    default 8-pair run is ~16 MiB, the biggest non-depth run 32 pairs ~64 MiB) takes the ONE
    //    shared reused buffer, so a deep run's staging never multiplies by the stager's thread
    //    count (32 threads x a retained 256 MiB scratch each would be gigabytes).
    check(!swapin_shared_window(8 * 2 << 20), "the default 8-pair run (~16 MiB): the thread scratch, exactly as before");
    check(!swapin_shared_window(32 * 2 << 20), "a 32-pair run (~64 MiB): still the thread scratch");
    check(swapin_shared_window(33 * 2 << 20), "one pair past the cap: the shared window");
    check(swapin_shared_window(64 * 2 << 20), "a 64-pair run (~128 MiB): the shared window");
    check(swapin_shared_window(128 * 2 << 20), "a 128-pair run (~256 MiB): ONE shared buffer, reused");

    if (failures == 0) std::printf("swapin_depth_test: all checks passed\n");
    return failures == 0 ? 0 : 1;
}
