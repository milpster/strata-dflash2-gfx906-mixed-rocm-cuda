// tests/core/dflash_tap_slot_test.cpp - CPU proof of the collector's slot bookkeeping
// lifecycle (the real DFlashTapCollector::acquire, dflash_tap_collect.hpp): key binding,
// oldest-eviction, staged/rows reset, and the reset_staging contract that keeps a NEW
// conversation's pos0 keys from inheriting an aborted request's state.  ev stays
// nullptr, so no CUDA call is ever made - the test is GPU-free (links strata_engine
// only for the collector symbol).  Parent review 2026-10-21 item 2/3.
#include <strata/core/dflash_tap_collect.hpp>

#include <cstdio>
#include <string>

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

    // 3. All bound: the OLDEST key is evicted, fully reset before reuse.
    int d = DFlashTapCollector::acquire(slots, 300, err);
    check(d == 0 && slots[0].key == 300, "all bound: the oldest key (100) is evicted, not the newest");
    check(slots[0].rows == -1, "an evicted slot resets rows=-1");
    bool flags_clear = true;
    for (int t = 0; t < strata::core::kDFlashMaxTaps; ++t) flags_clear &= !slots[0].staged[t];
    check(flags_clear, "an evicted slot resets every staged flag (no stale tap leaks into the new chunk)");
    check(slots[1].key == 200, "the eviction takes exactly one victim: the live binding stays");

    // 4. Eviction order follows the keys (positions grow): after evicting 100 and 200,
    //    slot 0 must be 300 then 400, slot 1 re-binds 500 last.
    int e = DFlashTapCollector::acquire(slots, 400, err);   // evicts 200 (oldest live)
    check(e == 1 && slots[1].key == 400 && slots[0].key == 300, "eviction follows key age (200 evicted before 300)");
    int f = DFlashTapCollector::acquire(slots, 500, err);   // evicts 300
    check(f == 0 && slots[0].key == 500 && slots[1].key == 400, "eviction follows key age (300 evicted before 400)");

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

    std::printf(failures == 0 ? "dflash_tap_slot_test: ALL PASS\n" : "dflash_tap_slot_test: %d FAILURES\n",
                failures == 0 ? 0 : failures);
    return failures == 0 ? 0 : 1;
}
