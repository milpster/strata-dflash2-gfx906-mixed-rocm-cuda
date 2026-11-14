# PP speedup analysis — 2026-11-13 (post arm campaign, prod on v0233 champion)

Data: five arm logs (shadow/stripe2/f32/drafter/fillraise) + prod log; all legs relief-mode.

## 1. The 16k fresh-read "anomaly" is cold-expert-read disk time

Every arm's fresh 16k leg reads 60–108 s for ~16k tokens (149–266 tok/s) while its own
swap-in file reads account for only ~13 s (~12k swaps, ~23.8 GiB, 1.05–1.13 ms/slot).
The rest is the **prefill's expert reads served from files**: 12,997 pairs dropped by
ram-relief + RAM pressure → ~19 GiB read per fresh context. The disk streams 1–2 GB/s
(operator-measured), so the ~150–300 MB/s effective rate is NOT bandwidth: the dropped
pair's serve path is a serialized per-pair chain (pack pread + cudaHostRegister +
device alias + pin-table insert, one pair at a time on the prefill thread, QD≈1;
refill_pair/refill_recorded_pairs in src/core/expert_source.cpp) — latency-bound at
~2–5 ms/pair × 13k pairs. The 64k/180k legs run 436–578 tok/s because the 16k leg
already touched every pair once; they are compute/helper-bound.

Cross-arm read-rate variance (149→265.9) with near-identical swap-in work = page-cache
temperature, not config. This closes the "arm4 SPEC_MIN_P=0.4 is a PP lever" reading
(its 64k/180k PP were *below* arm 1's) — cache artifact.

## 2. Ranked levers

1. **Full expert residency (the parked policy)** — the #1 PP lever, not just a TG fix.
   Removes the 47–95 s cold-read span per fresh context entirely; fresh 16k should move
   from 149–266 toward the warm-bound 400–600 class. Blocked on the init-hang lane
   (journal 18k evidence: one busy thread, zero I/O, never finishes).
2. **Killer-shape regression 620 → 466.7** (v0231 T1 full-fresh 180k in 290.6 s vs the
   R3A-era full-read killer). Shape definitions differ (arm 1's 180k was 49k-reused:
   578.7). Re-measure the full-fresh 995 KB shape once residency lands; if a gap
   remains, it is code/config, not disk.
3. **Phase-timer on the prompt tier** — only if (2) still shows a gap post-residency;
   the W-lane doc already showed the prompt lock is not the wall (quant deletion moved
   the 8k wall −0.8%).

## 3. Dead ends (measured or derived, do not spend arms here)

- `STRATA_HELPER_Q8_IMAGE`: deletes the engine CPU quant (44.9% of the prompt lock) but
  the wall is insensitive (−0.8%) — the prompt tier is not lock-bound. (helper-quant-sync-diet.md Q3)
- Swap-in depth: already 64; per-slot floor 1.05–1.13 ms stable across arms.
- Spec-min-p as a PP knob: cache-temperature artifact (see §1).
- Fill-mib raise: measured +103 slots, hit rate flat (arm 5).

## 4. Secondary lane (optional, after residency)

The dropped-pair serve path itself is serialization-bound (per-pair pread + register +
alias on the prefill thread). Batching it the way the swap-in path batches at depth 64,
or overlapping reads with compute across layers, would make any residual file service
graceful (cold boots, relief fallback). Moot for prod once residency lands — park it.

## 5. Sequence

Land the residency init fix → restart prod without relief flags → re-run the killer
shape + a lean panel → compare fresh-16k and killer-180k against this table.
