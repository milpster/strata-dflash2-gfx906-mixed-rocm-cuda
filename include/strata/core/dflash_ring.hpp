// include/strata/core/dflash_ring.hpp - the bounded ring math for the official SWA DFlash2 draft pools.
//
// The official DFlash2 draft attends symmetric-locally (docs/DFLASH2.md: window 2048, head_dim 128).
// Its private K/V therefore only needs to retain the newest cells a query can read: the union of the
// context window [pos-window+1, pos) and the block's own rows [pos, pos+K). That union is
// window + K - 1 cells, so a physical ring of round64(window + max_rows - 1) cells covers every
// legal query, and a pure-CPU mapping (docs/DFLASH.md semantics live in the runtime) sizes pools,
// maps append cells, bounds read windows, clamps snapshot extents, and detects when a rewind asks
// for history the ring already overwrote.
//
// This header is pure C++ (no CUDA): the runtime's host decisions, the engine's snapshot clamps,
// and tests/core/dflash_ring_test.cpp all call THESE functions - the model is not duplicated.
#pragma once

#include <algorithm>
#include <cstdint>

namespace strata::core {

/// Cells the physical ring must hold: the union of a window's context cells and the newest block's
/// rows, rounded up to whole pages. `max_rows` is the widest block (rows, anchor included);
/// `pages` is the pool's page size. 0 means "no ring" (full-attention legacy capacity).
inline int64_t dflash_ring_capacity(int64_t window, int64_t max_rows, int64_t pages) {
    if (window <= 0 || max_rows < 1 || pages < 1) return 0;
    const int64_t union_cells = window + max_rows - 1;
    return (union_cells + pages - 1) / pages * pages;
}

/// The physical cell a logical (absolute) cell reads and writes: the ring identity, with 0 as the
/// legacy no-ring mapping.
inline int64_t dflash_ring_cell(int64_t abs_cell, int64_t ring) {
    return ring > 0 ? abs_cell % ring : abs_cell;
}

/// The first logical cell the block's queries can read (docs/DFLASH.md: positions keep their true
/// RoPE indices; the official attention sees the symmetric window, clamped at 0).
inline int64_t dflash_window_first(int64_t pos, int64_t window) {
    return window > 0 ? std::max<int64_t>(0, pos - window + 1) : 0;
}

/// Is every cell of the block [pos, pos+rows)'s readable window still retained in the ring?
/// Appends arrive at monotonically increasing absolute positions, so the newest append that
/// touched a ring row is the one at c + ring; a window cell `c` is stale exactly when an append
/// past `c + ring` landed, i.e. when `first < appended_through - ring`. This is the single
/// retention rule the runtime and the tests share.
inline bool dflash_ring_retained(int64_t pos, int64_t rows, int64_t window, int64_t ring,
                                 int64_t appended_through) {
    if (ring <= 0) return true;   // legacy full-capacity pools: every cell the guard admits is live
    if (rows < 1 || pos < 0) return false;
    const int64_t first = dflash_window_first(pos, window);
    return first >= appended_through - ring;
}

/// A snapshot carries the physical ring image (slot order), never the logical length: the transfer
/// extent clamps to the pool's physical cells.
inline int64_t dflash_snapshot_extent(int64_t live_cells, int64_t physical_cells) {
    return physical_cells > 0 ? std::min(live_cells, physical_cells) : live_cells;
}

/// A parked image restores the ring it was taken from: the absolute end it captured must sit inside
/// the retained span [captured_end - ring, captured_end] - deeper rewinds ask for overwritten
/// history and must re-prefill (the runtime reports, never silently reads stale rows).
inline bool dflash_ring_rewind_ok(int64_t resume_pos, int64_t captured_end, int64_t rows,
                                  int64_t window, int64_t ring) {
    if (ring <= 0) return resume_pos <= captured_end;
    if (resume_pos < 0 || resume_pos > captured_end) return false;
    return dflash_ring_retained(resume_pos, rows, window, ring, captured_end);
}

}  // namespace strata::core
