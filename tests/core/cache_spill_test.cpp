// STRATA_CACHE_SPILL: the gate's parse, the card it picks, and the slots that card can take
// (header-only, no GPU).  The numbers are the 2026-11-10 mixed-GPU rig's own boot lines.
#include <cstdio>

#include "strata/core/cache_spill.hpp"

using strata::core::cache_spill::enabled;
using strata::core::cache_spill::pick_device;
using strata::core::cache_spill::slots_within;

static int failed = 0;
#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #x); ++failed; } } while (0)

int main() {
    // the gate: unset and "0" are today's start; "1" spills.  Anything else is not today's start.
    CHECK(!enabled(nullptr));
    CHECK(!enabled("0"));
    CHECK(enabled("1"));
    CHECK(enabled("2"));

    // the card: the LAST stage's device - the one with the fewest layers and the free VRAM.
    CHECK(pick_device({1}) == 1);                                    // --layer-split 44 on two VIIs
    CHECK(pick_device({1, 2}) == 2);                                 // a three-card split
    CHECK(pick_device({1, 2, 3}) == 3);
    CHECK(pick_device({}) == -1);                                    // no later stage: nothing to spill onto

    // the slots the tier can fill at the rig's own numbers.  Split 44: CUDA0 holds 3695 slots of 5.94 GiB
    // (1 724 416 B per slot, 256-aligned) and ends 404 MiB free; CUDA1 sits at 13.2/16.0 GiB, ~2.8 GiB
    // idle after its 4-layer stage cache.  The tier leaves the 512 MiB its own context and staging need.
    const int64_t blob = 1724416;                                    // 5.94 GiB / 3695 slots
    const int64_t idle = (int64_t) (2.8 * 1073741824.0);
    const int64_t spill = slots_within(idle, blob, 24576);
    CHECK(spill == 1432);
    CHECK(3695 + spill >= 5045);                                     // at least the split-32 cache back
    CHECK((spill * blob) >> 20 <= 2.8 * 1024 - 512);                 // it stays under the card's free minus 512 MiB

    // the guard binds before the pair count: a card with 400 MiB free fills nothing
    CHECK(slots_within(400ll << 20, blob, 24576) == 0);
    CHECK(slots_within(512ll << 20, blob, 24576) == 0);             // exactly the guard: no room left
    CHECK(slots_within(1024ll << 20, blob, 24576) == (1024 - 512) * 1048576 / blob);

    // the pair count binds the other way: 10 pairs fill 10 slots, not the whole card
    CHECK(slots_within(idle, blob, 10) == 10);
    CHECK(slots_within(idle, blob, 0) == 0);
    CHECK(slots_within(idle, 0, 10) == 0);
    CHECK(slots_within(0, blob, 10) == 0);

    if (failed) {
        std::fprintf(stderr, "%d check(s) failed\n", failed);
        return 1;
    }
    std::fprintf(stderr, "cache_spill_test: all checks passed\n");
    return 0;
}
