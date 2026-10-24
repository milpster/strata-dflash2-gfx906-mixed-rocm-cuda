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

/// The drafter's snapshot descriptor (SavedConversation.dflash_meta): the EXACT host counters a
/// parked image captured, plus the layout identity they belong to. No nonce: geometry/ring/
/// capacity matching plus the live token extent is the validation surface.
struct DFlashSnapshotMeta {
    int64_t committed_end = -1;   ///< absolute confirmed draft context end (-1: absent)
    int64_t appended_through = -1;///< newest append end incl. rejected noise (the ring horizon)
    int64_t ring_cells = 0;       ///< physical ring cells (0: legacy full-capacity pools)
    int64_t window = 0;           ///< the geometry's symmetric SWA window
    int64_t capacity = 0;         ///< the logical --dflash-window capacity
};

/// A parked image restores the ring it was taken from: the absolute end it captured must sit inside
/// the retained span [captured_end - ring, captured_end] - deeper rewinds ask for overwritten
/// history and must re-prefill (the runtime reports, never silently reads stale rows).
/// `write_end` is the ring's newest APPEND end (rejected noise rows included), not the confirmed
/// context length: a cell survives only while no append past `c + ring` has landed.
inline bool dflash_ring_rewind_ok(int64_t resume_pos, int64_t write_end, int64_t rows,
                                  int64_t window, int64_t ring) {
    if (ring <= 0) return resume_pos <= write_end;
    if (resume_pos < 0 || resume_pos > write_end) return false;
    return dflash_ring_retained(resume_pos, rows, window, ring, write_end);
}

/// The drafter's host-side context state for the bounded ring (and the legacy ring-0 pools):
/// the absolute confirmed context end and the newest append end (speculative noise rows
/// included). ONE production model: the runtime's add_context/propose/rewind guards, the
/// snapshot descriptor, and tests/core/dflash_ring_test.cpp all drive THESE methods - the
/// semantics (gaps, confirmed coverage, retention, restore validation) are testable on pure CPU.
struct DFlashRingState {
    int64_t ring = 0;          ///< physical ring cells; 0 = legacy full-capacity pools
    int64_t window = 0;        ///< the geometry's symmetric SWA window (0 for legacy)
    int64_t max_rows = 0;      ///< widest block (proposals + anchor)
    int64_t capacity = 0;      ///< the logical --dflash-window capacity (docs/DFLASH.md)
    int64_t committed_end = 0; ///< end of the target-confirmed draft context (absolute)
    int64_t appended_through = 0; ///< newest append end, rejected noise included (the ring horizon)

    /// A prefill starting at 0 resets the state logically (the ring rows are re-appended as the
    /// new prompt advances; no slot zeroing is needed - every read follows this sequence's appends).
    void reset() { committed_end = appended_through = 0; }

    /// A context append [pos0, pos0+rows) is admissible: inside capacity, and NOT a gap past the
    /// confirmed end - an append whose window would read cells this sequence never wrote is refused.
    bool append_allowed(int64_t pos0, int64_t rows) const {
        return rows >= 1 && pos0 >= 0 && capacity > 0 && pos0 + rows <= capacity &&
               pos0 <= committed_end;
    }
    void note_append(int64_t pos0, int64_t rows) {
        if (pos0 + rows > committed_end) committed_end = pos0 + rows;
        if (pos0 + rows > appended_through) appended_through = pos0 + rows;
    }

    /// A propose at `pos` requires complete confirmed context before its anchor (pos <=
    /// committed_end: no proposing past uncommitted history, no bridging overwritten gaps) and a
    /// fully retained readable window - including the block's own rows, whose appends land as the
    /// forward runs. Legacy ring-0 pools keep their old (capacity-bounded) guards untouched.
    bool propose_allowed(int64_t pos, int64_t rows) const {
        if (rows < 1 || pos < 0 || capacity <= 0 || pos + rows > capacity) return false;
        if (ring <= 0) return true;
        if (pos > committed_end) return false;
        return dflash_ring_retained(pos, rows, window, ring,
                                    std::max(appended_through, pos + rows));
    }
    void note_propose(int64_t pos, int64_t rows) {
        if (pos + rows > appended_through) appended_through = pos + rows;
    }

    /// Resume/mount at `pos` (checkpoints, snapshot restores): the block [pos, pos+rows)'s window
    /// must be retained AND the resume must sit at confirmed history.
    bool context_retained(int64_t pos, int64_t rows) const {
        if (pos < 0 || rows < 1 || capacity <= 0 || pos + rows > capacity) return false;
        if (ring <= 0) return pos <= committed_end;
        return pos <= committed_end && dflash_ring_retained(pos, rows, window, ring,
                                                             appended_through);
    }

    /// A snapshot descriptor is restorable only against the drafter that took it: same physical
    /// ring/layout/capacity, and self-consistent counters (appended >= committed, both inside
    /// capacity). The image pools are slot order; the counters - NOT the live token length -
    /// define the horizon (the bonus token the target generates at restore is not draft context).
    bool restore_allowed(const DFlashSnapshotMeta& m) const {
        return m.ring_cells == ring && m.window == window && m.capacity == capacity &&
               m.committed_end >= 0 && m.appended_through >= m.committed_end &&
               m.committed_end <= capacity && m.appended_through <= capacity;
    }
    bool restore(const DFlashSnapshotMeta& m) {
        if (!restore_allowed(m)) return false;
        committed_end = m.committed_end;
        appended_through = m.appended_through;
        return true;
    }
};

}  // namespace strata::core
