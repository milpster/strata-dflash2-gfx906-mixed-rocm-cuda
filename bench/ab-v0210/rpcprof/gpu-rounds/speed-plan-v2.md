# Speed plan v2 — serving prefill program (2026-11-17, post-review)

Reviewed by independent oracle (ses_e15829d7effe). All review claims spot-verified at source.
Supersedes v1. Lanes parked by operator until unparked; nothing here executes before then.

## Facts (measured/settled)
- prompt_ms = 1338 + 3.74×pcie_fetch (R²=0.969) — COMMON CAUSE (slots drive both), not overlap debt.
- Fresh 10.3k prompt: ~12 150 streamed slots (22.3 GiB), fetch 1.15 ms/slot steady 1.82 GiB/s,
  GPU-side ~2.6 ms/slot ⇒ ~33 s of ~46 s wall is per-slot GPU cost (gather/dequant/launch, FLOPs ≈ 0).
- Overlap already on: whole-chunk readahead + issuer thread (prefill.cpp:4040, 4110); ring past saturation.
- MMQ=0 in prod is a MEASURED win, not a default: edf2af2489 "OFF +7% PP, +12% TG vs ON". MMQ-ON is dead.
- vram_free_mib ≈ 220 at boot — engine VRAM is effectively full; any VRAM growth needs a named shrink.
- Env single source: config env map (overrides launcher block).

## Phase 0 — decisive probe (2 boots, ~1 h)
0a. Zero-cost pre-check (existing logs): `prefill mmq:` line; STRATA_MANIFEST state.
0b. Boot 1: config env += STRATA_PREFILL_TIMING=1, STRATA_TRACE=1 → prefill_judge ×8.
    Parse `strata prefill timing:` table: wall vs GPU timeline; wait copy; host grouping; dequant; gemm.
0c. Boot 2: same + STRATA_PREFILL_ROUTED_STREAM=1 (depth 32) → prefill_judge ×8.
    TRACE prints routed-drop counts → converts A1 from hypothesis to measurement; the arm doubles as A1 pilot.
Branch: wall − timeline large ⇒ idle real (A gains weight); wall ≈ timeline ⇒ GPU-bound (B weight).

## Program A (env rounds)
- A1 ONLY: ROUTED_STREAM=1, depth sweep 16/32/64. Bit-identical by source contract
  (routed_stream.hpp: same ring slots, consumer walk unchanged, "same bits").
  Ceiling: header-measured drop 15% @ 8k-chunks (47% @ 1k) ⇒ ~1800 slots × 1.15 ms ≈ **2 s** expected,
  exact from Boot-2 drop line. Eligibility: keep ISSUER on, COPY_EVENT_EVERY=1, COPY_WAIT_DEDUPE off
  (they hold routed off), no --peer-device. Only pinned-arena copies droppable; stager-staged keep copies.
- DROPPED: A2 (Linux event overhead ~0 µs — 40 ms/prompt), A3 (32 IS the default; 16 optional undershoot).

## Program B
- B1 capacity placement — budgets stated separately (v1 mixed them):
  * Host RAM side: relief-hot-mib 15000→ raises RAM-resident packs; Δ ≤ MemAvailable headroom (~16 GiB
    at serve), and every GiB stolen from page cache starves the streamed blobs (self-defeating past a knee).
    Document complement_pin_limit_ behavior at target before flipping.
  * VRAM side: engine cache growth needs ΔVRAM ≤ (16 GiB − KV int8 − graph pools − arenas) — with
    vram_free 220 MiB that means naming a shrink first (kv/graph-pool candidate), else null.
  Both bit-identical placement. Honest expectation: single-digit seconds unless a real VRAM pairing exists.
- B2-REPLACEMENT — the 33 s bucket (precommitted lever):
  * STRATA_DEQUANT_SIDE=1 — ALREADY BUILT (gemm.cu:642; side-stream dequant; dedicated parity test
    gemm_dequant_side_parity.cu in repo). Run parity test (GPU, lanes unparked) → if bit-identical claim
    holds: env-only A/B, no KL needed. This is the first real GPU-overhead round.
  * If Phase 0 shows gemm (not dequant) dominates: recompile levers (MMQ_GROUP 16→, STAGE_GRP 32→) are
    constexpr — code change, separate cycle, tolerance-gated.

## Gates (corrected wording)
- Bit-identical-by-contract rounds (A1, B1, DEQUANT_SIDE-after-parity): no KL; A/B medians + decode guard.
- Any numerics-changing round: PRE-DECLARED tolerance (mean KL ≤ 0.001 + no-NaN + content sanity).
  KL=0.00000/bit-exact stays the champion-regression criterion, not the adoption gate for numerics levers.
- Decode guard ≥ 24 tok/s every round. B3 (helper instrumentation fix): own commit + own guard reading,
  never folded into a numerics round.

## Success bar (honest)
Phase 0 decisive; ≥1 gated adoption. Pre-Phase-0 priced candidates: A1 ~2 s + DEQUANT_SIDE (unpriced,
bucket is 33 s — could be large or zero) ⇒ −20% is stretch, not commitment. The 33 s bucket is the only
path to it; Phase 0's table prices where in it we aim.

## Wall-time
0: ~1 h · A1 sweep: ~30 min · B1 analysis now (read-only) + 1 boot if it survives math ·
DEQUANT_SIDE: parity run + A/B ~45 min.
