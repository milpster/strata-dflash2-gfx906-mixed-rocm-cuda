// tests/core/read_once_test.cpp - CPU checks for the round-17 read-once prefill window
// (include/strata/core/read_once.hpp): STRATA_READONCE_MIB / STRATA_PREFILL_REUSE_MIB parsing
// and precedence, the pin-guard reserve and the effective budget it arms with, and the
// request-scoped LRU itself - consult/admit/evict under a byte budget, the RAM guard, the
// counters the "read-once reuse" line reports, and the re-read collapse the design expects at
// the killer prefill's shape (adjacent chunk sweeps over a shared routed set).  Pure host
// arithmetic and RAM, no device, no files, no links.
#include "strata/core/read_once.hpp"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <functional>
#include <vector>

using namespace strata::core;

static int failures = 0;
static void check(bool ok, const char* what) {
    if (!ok) {
        std::printf("FAIL: %s\n", what);
        ++failures;
    }
}

int main() {
    // 1. The knob: absent, empty, unparsable, zero and negative are OFF (the default, today
    //    bit-for-bit); a plain value is itself; the alias works alone; the primary wins when both
    //    are set; absurd values clamp at 128 GiB.
    check(read_once_budget_mib(nullptr, nullptr) == 0, "no env: off");
    check(read_once_budget_mib("", "") == 0, "empty env: off");
    check(read_once_budget_mib("0", nullptr) == 0, "\"0\": off");
    check(read_once_budget_mib("-8", nullptr) == 0, "negative: off");
    check(read_once_budget_mib("garbage", nullptr) == 0, "garbage: off");
    check(read_once_budget_mib("512", nullptr) == 512, "\"512\": 512 MiB");
    check(read_once_budget_mib(nullptr, "10240") == 10240, "the journal's alias alone: 10 GiB");
    check(read_once_budget_mib("2048", "10240") == 2048, "the primary wins over the alias");
    check(read_once_budget_mib("999999999", nullptr) == 131072, "an absurd value clamps to 128 GiB");

    // 2. The reserve (the pin-guard's knob, default 2 GiB) and the effective budget: the knob off
    //    is off whatever the RAM; RAM under the reserve is off; else the smaller of the knob and
    //    the room above the reserve.
    check(read_once_reserve_bytes(nullptr) == (2ull << 30), "no reserve env: the 2 GiB default");
    check(read_once_reserve_bytes("4") == (4ull << 30), "STRATA_PIN_RESERVE_GIB=4: 4 GiB");
    check(read_once_reserve_bytes("0") == (2ull << 30), "\"0\" keeps the default (as the pin-guard reads it)");
    check(read_once_reserve_bytes("junk") == (2ull << 30), "garbage keeps the default");
    check(read_once_effective_budget(0, (8ull << 30), (2ull << 30)) == 0, "the knob off: off, whatever the RAM");
    check(read_once_effective_budget((4ull << 30), (2ull << 30), (2ull << 30)) == 0,
          "RAM under the reserve: off");
    check(read_once_effective_budget((4ull << 30), (3ull << 30), (2ull << 30)) == (1ull << 30),
          "a sliver of room: the room (3 - 2 GiB)");
    check(read_once_effective_budget((4ull << 30), (12ull << 30), (2ull << 30)) == (4ull << 30),
          "the knob fits: the knob");
    check(read_once_effective_budget((16ull << 30), (12ull << 30), (2ull << 30)) == (10ull << 30),
          "the room is smaller: the room (12 - 2 GiB)");

    // 3. The window off (budget 0 / never armed): find and admit are no-ops, nothing is held.
    {
        ReadOnceWindow w;
        uint8_t blob[8] = {1, 2, 3, 4, 5, 6, 7, 8};
        check(!w.armed(), "a window starts disarmed");
        check(w.find(0) == nullptr, "disarmed: find is a no-op");
        check(!w.admit(0, blob, sizeof blob, nullptr), "disarmed: admit is a no-op");
        w.arm(0);
        check(!w.armed(), "arm(0) keeps the window off");
    }

    // 4. Consult/admit/evict: a miss then a hit serves the SAME bytes from RAM; the budget evicts
    //    the least-recent entry; a hit refreshes recency; disarm frees wholesale and a re-arm
    //    starts empty while the counters keep accumulating.
    {
        ReadOnceWindow w;
        uint8_t a[4] = {1, 1, 1, 1}, b[4] = {2, 2, 2, 2}, c[4] = {3, 3, 3, 3};
        w.arm(8);   // two 4-byte images
        check(w.admit(10, a, 4, nullptr), "the first blob is admitted");
        check(w.admit(20, b, 4, nullptr), "the second fills the budget");
        check(w.held_bytes() == 8, "the window holds both");
        auto img = w.find(10);
        check(img != nullptr && std::memcmp(img.get(), a, 4) == 0, "the first blob hits with its bytes");
        img = w.find(20);
        check(img != nullptr && std::memcmp(img.get(), b, 4) == 0, "the second hits too");
        check(w.admit(30, c, 4, nullptr), "a third blob over budget: evicting the least-recent entry makes room");
        // the LRU back was 10 (the hit on 20 refreshed 20 last; 10 is the least recent); 10 went for 30
        ReadOnceWindow::Counters k = w.counters();
        check(k.evicted == 1 && k.evicted_bytes == 4, "the least-recent entry was evicted for it");
        check(w.find(10) == nullptr, "the evicted key is a miss again");
        check(w.find(20) != nullptr, "the most recently hit key survived");
        check(w.find(30) != nullptr, "the new key is in");
        check(w.held_bytes() == 8, "the budget is respected exactly");
        // a blob bigger than the whole budget: refused without touching anything
        check(!w.admit(40, a, 16, nullptr), "a blob over the whole budget is refused");
        k = w.counters();
        check(k.refused == 1 && k.evicted == 1, "the refusal evicted nothing");
        w.note_reads(5, 40);
        w.disarm();
        check(!w.armed() && w.held_bytes() == 0 && w.find(10) == nullptr,
              "disarm drops every image wholesale");
        w.arm(8);
        check(w.armed() && w.held_bytes() == 0 && w.find(10) == nullptr, "a re-arm starts empty");
        k = w.counters();
        check(k.hits == 4 && k.reads == 5 && k.read_bytes == 40, "the counters accumulate across arms");
    }

    // 5. The RAM guard (the pin-guard pattern): admission is stepwise, one blob at a time, only
    //    while the available RAM covers it above the reserve; a refusal is counted and the blob
    //    keeps today's read path.
    {
        ReadOnceWindow w;
        uint8_t blob[4] = {};
        w.arm(1ull << 30);
        int checks = 0;
        const std::function<bool(uint64_t)> room = [&](uint64_t need) {
            ++checks;
            return need <= 4 && checks <= 2;   // RAM for the first two admissions only
        };
        check(w.admit(1, blob, 4, room), "the first blob passes the guard");
        check(w.admit(2, blob, 4, room), "the second passes");
        check(!w.admit(3, blob, 4, room), "the third is refused by the guard");
        check(w.find(1) != nullptr && w.find(2) != nullptr && w.find(3) == nullptr,
              "the refused blob was not kept");
        check(w.counters().refused == 1, "the refusal is counted");
        check(checks == 3, "the guard ran once per admission (stepwise, not per batch)");
    }

    // 6. The killer shape (the design's expected collapse, scaled): 8 chunk sweeps over a routed
    //    set where each chunk after the first re-reads half of the previous chunk's experts, as
    //    the 142k prefill does (8 x ~22 GiB sweeps over a ~40 GiB unique set).  A window holding
    //    one chunk's set serves the shared half of every later sweep from RAM: reads fall from
    //    8 x 100 to 100 + 7 x 50, exactly the read-volume cut the ~600-700 tok/s target needs.
    {
        ReadOnceWindow w;
        constexpr int64_t per_chunk = 100, shared = 50, sweeps = 8, blob_bytes = 64;
        std::vector<uint8_t> blob((size_t) blob_bytes, 0xab);
        w.arm((uint64_t) (per_chunk * blob_bytes));   // one chunk's set
        int64_t reads = 0, hits = 0;
        for (int64_t s = 0; s < sweeps; ++s) {
            const int64_t first = s * shared;   // sweep s reads [s*shared, s*shared + per_chunk)
            for (int64_t at = first; at < first + per_chunk; ++at) {
                if (w.find(at) != nullptr) {
                    ++hits;
                    continue;
                }
                ++reads;
                w.note_reads(1, blob_bytes);
                check(w.admit(at, blob.data(), blob_bytes, nullptr), "in-budget admission");
            }
        }
        const int64_t want_reads = per_chunk + (sweeps - 1) * shared;
        check(reads == want_reads, "the re-read collapses to first-sweep + shared halves");
        check(hits == sweeps * per_chunk - want_reads, "everything else is served from RAM");
        const ReadOnceWindow::Counters k = w.counters();
        check(k.hits == (uint64_t) hits && k.reads == (uint64_t) reads,
              "the counters say what the walk saw (the meter's hits/reads)");
        check(k.read_bytes == (uint64_t) (want_reads * blob_bytes), "the read bytes are the misses only");
        check(k.evicted == (uint64_t) ((sweeps - 1) * shared),
              "each later sweep's admissions evict exactly its unshared half (the LRU tail)");
        check(w.held_bytes() == (uint64_t) (per_chunk * blob_bytes),
              "the window holds one chunk's set at every step (the budget is respected exactly)");
    }

    if (failures == 0) std::printf("read_once_test: all checks passed\n");
    return failures == 0 ? 0 : 1;
}
