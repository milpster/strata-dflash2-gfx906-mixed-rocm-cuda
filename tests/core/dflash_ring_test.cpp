// tests/core/dflash_ring_test.cpp - CPU checks for the bounded SWA DFlash2 ring (docs/DFLASH.md
// capacity semantics, docs/DFLASH2.md official geometry). These call the production helpers in
// include/strata/core/dflash_ring.hpp - the mapping AND state model live there once, not here.
//
// No device work: pure host arithmetic over the official window 2048, page 64, widest block
// 8 rows (7 proposals + anchor), five pools of 8 KV heads x 128 FP16.
#include "strata/core/dflash_ring.hpp"

#include <cstdio>
#include <cstdint>
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
    const int64_t W = 2048, P = 64, ROWS = 8;
    const int64_t RING = dflash_ring_capacity(W, ROWS, P);
    check(RING == 2112, "official union 2048+8-1=2111 rounds up to 2112 cells (33 pages)");
    // 2048+4-1 = 2051: the page-multiple round-up is 2112; 2080 is 32.5 pages (not a multiple).
    check(dflash_ring_capacity(2048, 4, 64) == 2112, "block 4 rows rounds 2051 up to 2112 (2080 is not page-multiple)");
    check(dflash_ring_capacity(2048, 1, 64) == 2048, "1-row block: union 2048 is exactly 32 pages");
    check(dflash_ring_capacity(0, 8, 64) == 0, "no window: no ring (legacy capacity path)");
    // The exact bounded pool: five layers, 8 KV heads, 128 FP16, K and V.
    const uint64_t pools = 5ull * (uint64_t) RING * 8ull * 128ull * 2ull * 2ull;
    check(pools == 43253760ull, "five FP16 pools at 2112 cells are exactly 43,253,760 bytes");

    // Mapping over more than two ring wraps, at the mandated absolute positions.
    const int64_t positions[] = {0, 2047, 2048, 2049, 65536, 200000, 262144};
    for (int64_t pos : positions) {
        for (int64_t rows = 1; rows <= ROWS; ++rows) {
            std::vector<int64_t> seen((size_t) RING, -1);
            bool unique = true;
            for (int64_t r = 0; r < rows; ++r) {
                const int64_t pc = dflash_ring_cell(pos + r, RING);
                if (pc < 0 || pc >= RING) unique = false;
                else if (seen[(size_t) pc] >= 0) unique = false;
                else seen[(size_t) pc] = pos + r;
            }
            check(unique, "block rows map to distinct ring cells (no wrap aliasing)");
            const int64_t first = dflash_window_first(pos, W);
            const int64_t span = pos + rows - first;
            check(span <= W + rows - 1 && span <= RING, "readable span fits the ring union");
            check(dflash_ring_cell(pos, 0) == pos, "ring 0 keeps the legacy identity mapping");
        }
    }

    // Retention boundary is the exact rule first >= write_end - ring (write_end = newest append
    // end incl. noise). At write_end 200000 the OLDEST resumable position is
    // 200000 - RING + W - 1 = 199935 (a 65-token rewind); 199934 is stale.
    {
        const int64_t T = 200000;
        const int64_t oldest = T - RING + W - 1;
        check(oldest == 199935, "oldest resumable position at write_end 200000 is 199935");
        check(dflash_ring_rewind_ok(oldest, T, 1, W, RING), "the oldest retained resume passes");
        check(!dflash_ring_rewind_ok(oldest - 1, T, 1, W, RING),
              "one position deeper reaches cells a newer append overwrote (fresh prefill)");
        check(dflash_ring_rewind_ok(T, T, ROWS, W, RING), "resume at the captured end");
        check(!dflash_ring_rewind_ok(T + 1, T, 1, W, RING), "resume past the captured end is rejected");
        check(dflash_ring_rewind_ok(500, 200000, 1, W, 0), "ring 0 accepts any prefix rewind (legacy)");
        // With the widest block, the horizon (write_end incl. noise) shifts the boundary:
        // propose's own rows extend it; committed-only retention is rows-independent.
        DFlashRingState st;
        st.ring = RING; st.window = W; st.max_rows = ROWS; st.capacity = 262144;
        st.committed_end = T; st.appended_through = T;   // everything confirmed, no noise
        check(st.context_retained(oldest, ROWS), "clean horizon: the boundary resume retains the wide block");
        check(!st.context_retained(oldest - 1, ROWS), "clean horizon: one deeper is stale");
        st.appended_through = T + ROWS;                   // the last propose's noise rows landed
        check(st.context_retained(T, ROWS), "propose at the confirmed end after noise retains its window");
        check(!st.context_retained(oldest, ROWS), "the noise horizon pushes the oldest resume by +rows");
        check(st.context_retained(oldest + ROWS, ROWS),
              "the shifted boundary write_end - ring + W - 1 resumes cleanly");
    }

    // Rejections the runtime mirrors: zero/negative rows.
    check(!dflash_ring_retained(100, 0, W, RING, 108), "zero rows are rejected");
    check(!dflash_ring_retained(-1, 4, W, RING, 3), "negative positions are rejected");
    check(dflash_ring_retained(0, ROWS, W, RING, RING), "a prefill at 0 covering the whole ring is live");
    check(!dflash_ring_retained(0, ROWS, W, RING, RING + 1),
          "after a wrap, a prefill starting at 0 asks for overwritten history");

    // Snapshot clamps: the image is the physical ring, never the logical length.
    check(dflash_snapshot_extent(200000, RING) == RING, "park clamps the dflash image to the ring");
    check(dflash_snapshot_extent(100, RING) == 100, "a short context parks whole");
    check(dflash_snapshot_extent(200000, 0) == 200000, "ring 0 keeps the legacy full extent");

    // ---- DFlashRingState: production state semantics on pure CPU.
    {
        DFlashRingState st;
        st.ring = RING; st.window = W; st.max_rows = ROWS; st.capacity = 262144;
        // Empty context: proposing at a deep position reads uninitialized slots - refused.
        check(st.committed_end == 0 && st.appended_through == 0, "a fresh drafter has an empty horizon");
        check(!st.propose_allowed(200000, ROWS), "propose with no confirmed context is refused");
        check(!st.append_allowed(200000, 4), "an append that skips past the confirmed end is a gap");
        check(st.append_allowed(0, 4), "append at 0 is admissible on an empty context");
        st.note_append(0, 4);
        check(st.committed_end == 4 && st.appended_through == 4, "note_append advances both counters");
        check(st.append_allowed(4, 4), "contiguous appends are admissible (after the first landed)");
        st.note_append(4, 4);
        check(st.committed_end == 8, "adjacent append stays contiguous");
        check(st.append_allowed(8, 4), "append at the confirmed end continues the context");
        st.note_append(8, 4);
        check(st.committed_end == 12, "coverage tracks the full confirmed span (4+4+4)");
        check(!st.append_allowed(20, 4), "an append past the confirmed end is a gap");

        // Prefill the whole 200000 context logically (counters only), then the verify loop with
        // TAG-distinct rows: noise and committed are distinguishable, so contamination is caught.
        st.reset();
        st.note_append(0, 200000);
        check(st.propose_allowed(200000, ROWS), "propose after full confirmed context is allowed");
        check(!st.propose_allowed(200001, ROWS), "propose past the confirmed end is refused");

        std::vector<int> pool((size_t) RING, 0);   // 0 empty, 1 COMMITTED, 2 NOISE
        auto append = [&](int64_t pos0, int64_t rows, int tag) {
            for (int64_t r = 0; r < rows; ++r) pool[(size_t) dflash_ring_cell(pos0 + r, RING)] = tag;
        };
        append(0, 200000, 1);   // materialize the prefilled rows: readable cells must carry
                                // COMMITTED tags, not the untouched-slot default 0
        // The exhaustive loop mirrors the engine: prefill committed, then at each cycle the
        // propose writes NOISE rows [pos, pos+K), verify commits a accepted proposals + the
        // generated anchor token (a+1 rows, 1..K) as COMMITTED (re-appending the SAME cells),
        // and the next cycle's readable window [pos'-W+1, pos'+K) must contain no NOISE below
        // pos' (contamination check).
        int64_t pos = 200000;
        bool clean = true, tag_clean = true;
        for (int cycle = 0; cycle < 40 && pos < 200140; ++cycle) {
            const int64_t K = ROWS;
            if (!st.propose_allowed(pos, K)) { clean = false; break; }
            st.note_propose(pos, K);
            append(pos, K, 2);                                   // the block's rows (noise included)
            // Engine reality: verify commits a accepted proposals + 1 generated token = a+1
            // rows (the anchor), never zero. a cycles 0..7 so t spans 1..8.
            const int64_t t = 1 + cycle % K;
            append(pos, t, 1);
            st.note_append(pos, t);
            pos += t;
            const int64_t first = dflash_window_first(pos, W);
            for (int64_t c = first; c < pos; c += 128)          // sampled readable cells < pos
                if (pool[(size_t) dflash_ring_cell(c, RING)] != 1) tag_clean = false;
        }
        check(clean, "the engine loop's proposes stay inside confirmed coverage");
        check(tag_clean, "readable context cells below the anchor are never NOISE (contamination guard)");
        std::vector<int64_t> cells((size_t) RING, -1);
        for (int64_t c = 200000; c < pos; ++c) cells[(size_t) dflash_ring_cell(c, RING)] = c;
        bool congruent = true;
        for (int64_t s = 0; s < RING; ++s)
            if (cells[(size_t) s] >= 0 && dflash_ring_cell(cells[(size_t) s], RING) != s) congruent = false;
        check(congruent, "every stored cell sits in its congruent slot (positional addressing)");

        // Restore semantics: the counters come from the descriptor, not the live length.
        DFlashSnapshotMeta m{200000, 200012, RING, W, 262144};
        check(st.restore_allowed(m), "a matching descriptor with noise horizon is restorable");
        check(!st.restore_allowed(DFlashSnapshotMeta{200012, 200000, RING, W, 262144}),
              "appended < committed is refused");
        check(!st.restore_allowed(DFlashSnapshotMeta{200000, 200012, 1056, W, 262144}),
              "a descriptor from a foreign ring layout is refused");
        check(!st.restore_allowed(DFlashSnapshotMeta{-1, -1, RING, W, 262144}),
              "an absent descriptor is refused");
        check(!st.restore_allowed(DFlashSnapshotMeta{300000, 300008, RING, W, 262144}),
              "counters beyond capacity are refused");
        DFlashRingState fresh;
        fresh.ring = RING; fresh.window = W; fresh.max_rows = ROWS; fresh.capacity = 262144;
        check(fresh.restore(m), "restore persists the EXACT counters");
        check(fresh.committed_end == 200000 && fresh.appended_through == 200012,
              "the horizon keeps speculative writes beyond the confirm (bonus token stays outside)");
        check(fresh.context_retained(200000, ROWS), "restore at the confirmed end retains its window");
        check(!fresh.context_retained(200001, ROWS), "a resume past the confirmed end is refused");
        // The same geometry with a corrupted end (committed past what the image can back) must
        // fail at the propose/retention boundary, not read stale rows.
        fresh.appended_through = 400000;
        check(!fresh.context_retained(200000, ROWS),
              "a corrupted horizon marker rejects the restore's own window (no stale read)");
    }

    if (failures) {
        std::printf("dflash_ring_test: %d FAILURES\n", failures);
        return 1;
    }
    std::printf("dflash_ring_test: all checks passed\n");
    return 0;
}
