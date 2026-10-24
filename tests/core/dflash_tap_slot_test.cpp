// tests/core/dflash_tap_slot_test.cpp - CPU proof of the collector's slot bookkeeping
// lifecycle (the real DFlashTapCollector::acquire, dflash_tap_collect.hpp): key binding,
// oldest-eviction, staged/rows reset, and the reset_staging contract that keeps a NEW
// conversation's pos0 keys from inheriting an aborted request's state.  ev stays
// nullptr, so no CUDA call is ever made - the test is GPU-free (links strata_engine
// only for the collector symbol).  Parent review 2026-10-21 item 2/3.
#include <strata/core/dflash_tap_collect.hpp>

#include <chrono>
#include <cstdio>
#include <string>
#include <thread>

using strata::core::DFlashTapCollector;

namespace {

int failures = 0;

void check(bool ok, const char* what) {
    std::printf("%s %s\n", ok ? "ok  " : "FAIL", what);
    if (!ok) ++failures;
}

using Slot = DFlashTapCollector::Slot;

}  // namespace

int main() {
    std::string err;
    Slot slots[DFlashTapCollector::kSlots];

    // 1. Fresh slots: the first key binds slot 0, the second slot 1.
    int a = DFlashTapCollector::acquire(slots, 100, err);
    int b = DFlashTapCollector::acquire(slots, 200, err);
    check(a == 0 && b == 1 && slots[0].key == 100 && slots[1].key == 200,
          "two fresh slots bind keys 100 and 200 in order");
    check(slots[0].rows == -1 && slots[1].rows == -1, "a new binding starts with rows=-1 (no geometry guard yet)");

    // 2. An existing key re-binds its slot: same index, staged/rows reset for the new round.
    slots[1].rows = 4096;
    for (int t = 0; t < strata::core::kDFlashMaxTaps; ++t) slots[1].staged[t] = true;
    int c = DFlashTapCollector::acquire(slots, 200, err);
    check(c == 1, "an existing key wins its slot (pipeline: a slice re-stages the same chunk key)");
    check(slots[1].rows == 4096, "key-hitting acquire does NOT clear the in-request rows guard (consistency check stays)");

    // 3. L10 lifecycle: an assembled slot (staged flags cleared = RETIRED) is reused;
    //    the key-hitting live binding stays untouched.
    slots[0].rows = -1;   // key 100 was "assembled": flags cleared, rows released by retire
    int d = DFlashTapCollector::acquire(slots, 300, err);
    check(d == 0 && slots[0].key == 300, "a RETIRED slot (no live staging) is reused; the live binding stays");
    check(slots[0].rows == -1, "a reused slot resets rows=-1");
    bool flags_clear = true;
    for (int t = 0; t < strata::core::kDFlashMaxTaps; ++t) flags_clear &= !slots[0].staged[t];
    check(flags_clear, "a reused slot starts with every staged flag clear (no stale tap leaks into the new chunk)");
    check(slots[1].key == 200 && slots[1].staged[0], "the live (STAGED) binding is NOT a reuse victim");

    // 4. UNCONSUMED staging is NEVER evicted (the window-1 GPU failure: staging N+2
    //    destroyed unconsumed N).  Both slots STAGED: acquire refuses with backpressure
    //    (the GPU stage() waits; the CPU bookkeeping refuses deterministically).
    slots[0].staged[0] = true;   // key 300 now holds unconsumed staging
    int e = DFlashTapCollector::acquire(slots, 400, err);
    check(e == -1 && err.find("backpressure") != std::string::npos,
          "both slots STAGED: acquire REFUSES - unconsumed staging is never evicted");
    check(slots[0].key == 300 && slots[1].key == 200 && slots[1].staged[0],
          "the refusal changes nothing: both live bindings and their staged flags stand");
    for (int t = 0; t < strata::core::kDFlashMaxTaps; ++t) slots[1].staged[t] = false;   // key 200 retires
    int f = DFlashTapCollector::acquire(slots, 400, err);
    check(f == 1 && slots[1].key == 400 && slots[0].key == 300, "after a retire, the oldest retired slot binds the new key");
    int dup = DFlashTapCollector::acquire(slots, 300, err);
    check(dup == 0 && slots[0].key == 300, "an existing key finds its binding (unique, never re-binds elsewhere)");

    // 5. The reset contract (aborted request -> new conversation, same pos0 keys):
    //    clear keys/rows/flags, then the same keys bind fresh slots with rows=-1.
    for (int i = 0; i < DFlashTapCollector::kSlots; ++i) {
        slots[i].key = -1; slots[i].rows = -1;
        for (int t = 0; t < strata::core::kDFlashMaxTaps; ++t) slots[i].staged[t] = false;
    }
    int g = DFlashTapCollector::acquire(slots, 0, err);     // new conversation, chunk pos0 = 0
    int h = DFlashTapCollector::acquire(slots, 2048, err);
    check(g == 0 && h == 1 && slots[0].rows == -1 && slots[1].rows == -1,
          "after a reset, the same keys bind fresh with no inherited rows guard (no false 'rows disagree')");

    // 6. Cancellation contract (L10 review): a producer parked in backpressure on the REAL
    //    acquire path must REJECT after reset_staging() - the epoch token captured under
    //    mu_ at wait entry changes, so an old request's producer never stages into a new
    //    request.  The collector is NOT enabled here (no CUDA call happens); the GPU
    //    cancellation-waiter fixture in dflash_tap_collect_test covers the enabled case.
    {
        DFlashTapCollector tc;
        auto& kf = tc.kind_for_test(4);
        std::string e2;
        int s0 = DFlashTapCollector::acquire(kf.slots, 100, e2);
        int s1 = DFlashTapCollector::acquire(kf.slots, 200, e2);
        kf.slots[s0].staged[0] = true; kf.slots[s1].staged[0] = true;   // both STAGED
        const uint64_t epoch0 = tc.staging_epoch();
        std::string perr;
        bool pres = true;
        std::thread producer([&] { pres = tc.acquire_producer(4, 300, perr); });
        std::this_thread::sleep_for(std::chrono::milliseconds(80));   // let it park
        tc.reset_staging();   // new epoch + notify: the parked producer must reject
        producer.join();
        check(!pres && perr.find("cancelled") != std::string::npos,
              "reset during backpressure: pending producer REJECTS deterministically (no hang)");
        check(tc.staging_epoch() != epoch0, "reset_staging increments the cancellation epoch");
        check(kf.slots[0].key < 0 && kf.slots[1].key < 0,
              "the rejected producer staged nothing: slots stay cleared after the reset");
    }

    // 7. L14 consumer-binding protection (the dflash_w3 model window: assemble() bound
    //    a fresh slot for a key whose cross-device producers had not staged; acquire()
    //    must never bind a new chunk's key OVER the consumer's binding, and the key's
    //    producers must land ON it - the flags the consumer's bounded wait reads).
    {
        DFlashTapCollector tc;
        auto& kf = tc.kind_for_test(2);
        std::string e3;
        // The consumer (assemble) binds chunk key 0 on a fresh collector.
        int ca = DFlashTapCollector::acquire(kf.slots, 0, e3);
        kf.slots[ca].awaiting = true;                       // the consumer is waiting for this key's flags
        int pb = DFlashTapCollector::acquire(kf.slots, 0, e3);
        check(ca == 0 && pb == 0, "the key's producers acquire the consumer's binding (staging lands where the consumer waits)");
        int pc = DFlashTapCollector::acquire(kf.slots, 2048, e3);
        check(pc == 1 && kf.slots[ca].awaiting, "the other chunk binds the free slot; the awaiting binding is untouched");
        kf.slots[pc].staged[0] = true;   // parent-found fixture bug (2026-10-22): a REAL in-flight producer
        kf.slots[pc].rows = 4;           // chunk has staged flags - bound-but-unstaged is RETIRED by the
                                         // collector's own lifecycle and rightly reusable. Mark it STAGED
                                         // so the third chunk below truly faces two live bindings.
        int pd = DFlashTapCollector::acquire(kf.slots, 4096, e3);
        check(pd == -1 && e3.find("backpressure") != std::string::npos,
              "an awaiting consumer binding is NOT a reuse victim: a third chunk refuses with backpressure");
        check(kf.slots[ca].key == 0 && kf.slots[1].key == 2048, "the refusal keeps both bindings standing");
        // The consumer's chunk retires (assemble completed after its producers staged).
        kf.slots[ca].awaiting = false;
        kf.slots[ca].staged[0] = true; kf.slots[ca].rows = 4;   // staged, then assembled: retire
        kf.slots[ca].rows = -1;
        for (int t = 0; t < strata::core::kDFlashMaxTaps; ++t) kf.slots[ca].staged[t] = false;
        int pe = DFlashTapCollector::acquire(kf.slots, 4096, e3);
        check(pe == 0 && kf.slots[0].key == 4096 && !kf.slots[0].awaiting,
              "after the consumer's retire the slot re-enters the reuse ring (oldest retired)");
    }

    std::printf(failures == 0 ? "dflash_tap_slot_test: ALL PASS\n" : "dflash_tap_slot_test: %d FAILURES\n",
                failures == 0 ? 0 : failures);
    return failures == 0 ? 0 : 1;
}
