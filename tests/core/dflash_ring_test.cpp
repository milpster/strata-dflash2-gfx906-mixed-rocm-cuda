// tests/core/dflash_ring_test.cpp - CPU checks for the bounded SWA DFlash2 ring (docs/DFLASH.md
// capacity semantics, docs/DFLASH2.md official geometry). These call the production helpers in
// include/strata/core/dflash_ring.hpp - the mapping model lives there once, not here.
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
    check(RING == 2112, "official union 2048+8-1 rounds to 2112 cells (33 pages)");
    check(dflash_ring_capacity(2048, 4, 64) == 2080, "block 4 rows rounds 2051 up to 2080");
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
            // Union uniqueness: window + block never exceeds the ring, so a query's readable
            // window [first, pos+rows) maps injectively for the newest ring's span.
            const int64_t first = dflash_window_first(pos, W);
            const int64_t span = pos + rows - first;
            check(span <= W + rows - 1 && span <= RING, "readable span fits the ring union");
            // RoPE positions stay absolute: the ring mapping touches cells, never positions.
            check(dflash_window_first(pos, W) == (pos < W ? 0 : pos - W + 1),
                  "window start uses the absolute position, not the ring cell");
            check(dflash_ring_cell(pos, 0) == pos, "ring 0 keeps the legacy identity mapping");
        }
    }

    // Retention: appends arrive monotonically; a block reads only retained history.
    for (int64_t pos : positions) {
        const int64_t through = pos + ROWS;   // the block's own appends landed
        check(dflash_ring_retained(pos, ROWS, W, RING, through),
              "an in-place propose reads retained history");
        check(dflash_ring_retained(pos, 1, W, RING, through), "a 1-row block is retained");
    }
    // Deep rewind past the ring: the resume's window reaches cells a newer append overwrote.
    check(dflash_ring_rewind_ok(200000, 200000, ROWS, W, RING), "resume at the captured end");
    check(dflash_ring_rewind_ok(200000 - W + 1, 200000, 1, W, RING),
          "rewind to the oldest window start is retained");
    check(!dflash_ring_rewind_ok(200000 - RING, 200000, 1, W, RING),
          "rewind past the ring's newest span is rejected (fresh prefill)");
    check(!dflash_ring_rewind_ok(200001, 200000, 1, W, RING), "rewind past the captured end is rejected");
    check(dflash_ring_rewind_ok(500, 200000, 1, W, 0), "ring 0 accepts any prefix rewind (legacy)");

    // Rejections the runtime mirrors: zero/negative rows, gaps.
    check(!dflash_ring_retained(100, 0, W, RING, 108), "zero rows are rejected");
    check(!dflash_ring_retained(-1, 4, W, RING, 3), "negative positions are rejected");
    check(dflash_ring_retained(0, ROWS, W, RING, RING), "a prefill at 0 covering the whole ring is live");
    check(!dflash_ring_retained(0, ROWS, W, RING, RING + 1),
          "after a wrap, a prefill starting at 0 asks for overwritten history");

    // Snapshot clamps: the image is the physical ring, never the logical length.
    check(dflash_snapshot_extent(200000, RING) == RING, "park clamps the dflash image to the ring");
    check(dflash_snapshot_extent(100, RING) == 100, "a short context parks whole");
    check(dflash_snapshot_extent(200000, 0) == 200000, "ring 0 keeps the legacy full extent");

    // All/partial/zero re-append against a slot-indexed pool: noise rows land at the block's
    // cells; the committed prefix re-appends the SAME cells (positional overwrite); a later
    // full commit overwrites exactly the stale noise rows the next window would read.
    {
        std::vector<int64_t> pool((size_t) RING, -1);
        auto append = [&](int64_t pos0, int64_t rows) {
            for (int64_t r = 0; r < rows; ++r) pool[(size_t) dflash_ring_cell(pos0 + r, RING)] = pos0 + r;
        };
        append(200000, ROWS);                       // propose writes the block (noise included)
        append(200000, 3);                          // partial commit re-appends the accepted prefix
        for (int64_t r = 0; r < 3; ++r)
            check(pool[(size_t) dflash_ring_cell(200000 + r, RING)] == 200000 + r,
                  "committed prefix re-appends its own cells");
        for (int64_t r = 3; r < ROWS; ++r)
            check(pool[(size_t) dflash_ring_cell(200000 + r, RING)] == 200000 + r,
                  "the re-append overwrote the same absolute cells");
        append(200003, ROWS);                       // next propose at the commit point
        check(dflash_ring_retained(200003, ROWS, W, RING, 200003 + ROWS),
              "propose after the commit reads retained (overwritten) history");
        append(200011, ROWS);                       // and so on: the ring only keeps the newest writers
        bool congruent = true;
        for (int64_t s = 0; s < RING; ++s)
            if (pool[(size_t) s] >= 0 && pool[(size_t) s] % RING != s) congruent = false;
        check(congruent, "every slot holds a congruent absolute cell (positional addressing)");
    }

    if (failures) {
        std::printf("dflash_ring_test: %d FAILURES\n", failures);
        return 1;
    }
    std::printf("dflash_ring_test: all checks passed\n");
    return 0;
}
