#!/usr/bin/env python3
"""CPU-only synthetic fixtures for the bounded-ring mapping in dflash2_stage_parity.py.

No device, no artifact: exercises the helpers the parity tool uses at the mandated deep
positions (pos 200000, ring 2112, window 2048, page 64, block 8) with fake context chunks
and fake pool rows, so the ring-wrap/contamination logic is testable while production
inference stays up.

  python tools/test_dflash2_stage_parity.py
"""
import importlib
import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
parity = importlib.import_module("dflash2_stage_parity")

W, P, K, RING, POS = 2048, 64, 8, 2112, 200000
failures = 0


def check(ok, what):
    global failures
    if not ok:
        print(f"FAIL: {what}")
        failures += 1


def refuses(fn, what):
    try:
        fn()
    except ValueError as e:
        print(f"refused ({what}): {e}")
        return
    check(False, f"{what} was not refused")


# meta.bin shapes: 5-field legacy stays ring 0; 6-field carries the ring; malformed refuses.
check(parity.unpack_meta(struct.pack("<5I", POS, K, 248057, 248077, P)) == (POS, K, 248057, 248077, P, 0),
      "legacy 5-field meta keeps the full-resident path (ring 0)")
check(parity.unpack_meta(struct.pack("<6I", POS, K, 248057, 248077, P, RING))[-1] == RING,
      "6-field meta carries the ring cells")
refuses(lambda: parity.unpack_meta(struct.pack("<3I", 1, 2, 3)), "meta with 3 fields")
refuses(lambda: parity.unpack_meta(b"\x00\x01\x02"), "meta with a non-multiple-of-4 size")

# Retained span boundary: a cell survives while its next congruent cell c+ring is unwritten,
# i.e. c >= pos+K-ring. At pos 200000: 200000+8-2112 = 197896; the noise writes' congruent
# cells [200000-2112, 200008-2112) = [197888, 197896) sit just below it (e.g. 200007-2112=197895).
lo = parity.ring_context_span(POS, K, RING)
check(lo == POS + K - RING == 197896, "retained context lower bound pos+K-ring = 197896")
check(parity.ring_context_span(POS, K, 0) == 0, "ring 0 keeps the legacy full span")

# The comparison span is the UNION of the visible attention window [pos-W+1, pos+K) and the
# retained context [pos+K-ring, pos): start = min(197953, 197896) = 197896, end 200008,
# length 2112 = ring (window 2048+K 8-1 = 2055 union the extra ring slack). The visible
# 2055-cell window span is a strict subset; never np.arange(pos+K) over the whole history.
cells = parity.ring_cells_span(POS, K, W, RING)
check(int(cells[0]) == POS + K - RING == 197896 and int(cells[-1]) == POS + K - 1,
      "union span starts at min(window start, retained bound) through the last block row")
check(len(cells) == POS + K - (POS + K - RING) == RING, "union span is exactly ring cells (2112)")
window_span = np.arange(POS - W + 1, POS + K)
check(len(window_span) == 2055 and window_span[0] >= cells[0] and window_span[-1] == cells[-1],
      "visible window span (2055 cells) is a subset of the union span")
check(len(parity.ring_cells_span(POS, K, 0, 0)) == POS + K, "legacy window 0 keeps arange(pos+K)")

# Pool rows: the same cell % ring the kernel applies, identity page table.
pt = np.arange(RING // P, dtype="<i4")
block = np.arange(POS, POS + K)
rows = parity.ring_pool_rows(pt, block, RING, P, 1)
check(len(set(rows.tolist())) == K, "the block's rows map to distinct slots (no wrap aliasing)")
check(all(r == c % RING for r, c in zip(rows.tolist(), block.tolist())), "rows are congruent mod ring")
check(parity.ring_pool_rows(pt, np.arange(5), 0, P, 1).tolist() == [0, 1, 2, 3, 4],
      "ring 0 keeps the identity mapping")
refuses(lambda: parity.ring_pool_rows(pt[:10], block, RING, P, 1), "short ring page table")
refuses(lambda: parity.ring_pool_rows(np.arange(5), block, 2110, P, 1), "ring not a page multiple")

# Contamination simulation with distinct tags: committed cells write value c, the block's
# noise rows [pos, pos+K) write -(c+1). The retained span must read committed values; cells
# below it must read NOISE (their rows were overwritten) - what the tool must exclude.
pool = np.zeros(RING, np.float32)
for c in range(lo - 2, POS):
    pool[c % RING] = c
for c in range(POS, POS + K):
    pool[c % RING] = -(c + 1)
check(all(pool[c % RING] == c for c in (lo, lo + 1, POS - 1)),
      "retained context cells read their own committed rows")
# Independent slot simulation of the EXACT boundary (no arithmetic literals trusted):
# the 8 cells just below lo have congruent writers inside the noise range [pos, pos+K);
# the 8 cells at lo have congruent writers past every write (>= pos+K) - self is newest.
contaminated = range(POS + K - RING - K, POS + K - RING)
check(all(c + RING in range(POS, POS + K) for c in contaminated),
      "the 8 cells below lo are congruent to noise writers (slot-simulation proof)")
check(all(pool[c % RING] < 0 for c in contaminated), "their slots carry the noise value")
check(all(c + RING >= POS + K for c in range(POS + K - RING, POS + K - RING + K))
      and all(pool[c % RING] == c for c in range(POS + K - RING, POS + K - RING + K)),
      "the 8 cells at lo have no congruent writer - their own committed value survives")
poisoned = POS + K - 1 - RING   # congruent to the last noise write (200007 - 2112)
check(pool[poisoned % RING] == -(POS + K) < 0,
      "a contaminated cell's row holds the noise writer's value (excluded from comparison)")
check(all(pool[c % RING] < 0 for c in range(POS, POS + K)), "the block's own rows are the noise writes")

if failures:
    print(f"test_dflash2_stage_parity: {failures} FAILURES")
    raise SystemExit(1)
print("test_dflash2_stage_parity: all fixtures passed")
