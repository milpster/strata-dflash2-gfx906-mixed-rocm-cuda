# Short-prompt PP lane — S-session results (source lane "123": refill-overlap + arena-populate)

Date: 2026-11-05. Engine: build-v0210/rpcw-hip/strata sha f5fb0738218485a2 (shortpp-src:
STRATA_REFILL_OVERLAP c44c6d07 + STRATA_WINDOW_POPULATE 03d40f8e, both default OFF; W1a/W1b
relief lineage). Helper: rpcw-cuda afbf19f76e39ac16. Pack: IQ3_S GSQ-RCO, port 8019, one
engine boot per arm, probe = fixed prompts x 3 rounds + TG leg (unique markers, max_tokens 8).
Host: 62 GiB RAM, VII x2 + 3080, prod DOWN during the session, no background jobs running, pack page
cache warmed (MemAvailable 55.7 GiB before arm 1).

## Legs (client walls, seconds; best-of-round spread shown as r0/r1/r2)

| arm | flags | S214 (~214 tok) | S1k (~620 tok) | S4k (~3.9k tok) | S18k (~10.1k tok) | TG128 |
|-----|-------|-----------------|----------------|-----------------|-------------------|-------|
| S0 control | both off | 5.67/2.88/2.60 | 6.43/6.15/6.40 | 15.73/16.05/15.98 | 40.65/48.72/45.95 | 16.55 |
| S1 | +REFILL_OVERLAP | 5.68/2.87/2.58 | 6.41/6.21/6.42 | 15.66/15.95/15.97 | 38.84/41.17/**241.82** | 73.94 |
| S2 | +WINDOW_POPULATE | **215.79**/3.26/2.70 | 9.97/6.25/6.51 | 21.24/16.11/18.85 | 52.01/**134.91**/55.74 | 25.71 |
| S3 | both | 5.80/2.95/2.61 | 6.50/6.24/6.41 | 15.85/16.02/19.04 | **268.80/159.20/162.63** | 79.60 |
| S4 | both + --short-read 256 | 3.16/3.04/2.92 | 9.42/6.28/16.49 | 78.42/26.11/116.46 | FAILED (HTTP 400 mid-leg) | — |
| SP | +PF_STEP_SYNC (profile) | 9.21/2.90/2.62 | 71.01/6.22/6.39 | 20.94/38.56/24.32 | 71.83/540.56/530.47 | 11.63 |

Reference (T-lane, pre-W1 binary, same harness family): 185tok 2.6-2.9 / 620tok 6.2-6.4 /
2.3k 15.8-16.2 / 10k 38.9. A/B2 (V5 binary 331c2e89, 14:45-15:00): S1k 6.22-6.47, S4k
16.22-20.79, S18k 39.03 (r1).

## Verdicts

- **S0 (control, default path): healthy everywhere.** Byte-identical-default confirmed in
  behavior: S1k/S4k sit exactly on the T-lane/V5 numbers; S18k 40-49 s vs 38.9-39 s T-lane
  (same class; engine-trace on, slightly larger probe content). The default path is NOT
  regressed by the two commits — the afternoon's "3-5x regression" was host memory pressure
  (see post-mortem), not code.
- **S1 (REFILL_OVERLAP): no win, one loss.** Short/mid identical to control. The final
  refill it hides (0.7 s at 620 tok, 5.8-7.5 s at 10k) is post-read cost; with max_tokens 8
  the wall is read-dominated, so the hiding buys ~nothing measurable. S18k r2 blew up to
  241.8 s (read alone 214.4 s in the engine log) — later rounds degrade.
- **S2 (WINDOW_POPULATE): first-request poison + big-prompt instability.** The very first
  chat request after boot took 215.8 s (the per-pair pack-file refills of dropped pairs at
  request end — the 52 ms/slot class). Later chat legs fine. S18k rounds unstable.
- **S3 (both flags): worst arm.** S18k 159-269 s every round; engine log shows the
  accumulation: refill 29.9 s (control 5.8), checkpoint saves 23.2 s and 49.6 s — the latter
  on a 620-token prompt (control 0.1-3.9 s). Something the two features build up across
  rounds (registered pin ranges / arena writes are the candidates) inflates exactly the
  serial steps the big-prompt path walks.
- **S4 (poison test): poison confirmed.** With --short-read 256 the chat legs are fine
  (2.9-3.2 s) but S4k degrades (26-116 s) and the S18k leg dies with the known blob-loss
  HTTP 400 (helper 10 s prompt-tier deadline closing the channel under pathological
  slowness). Routing short prompts through the window path is not usable in this form.
- **SP (profile): the S18k read cost is three layers, not an intercept.** With per-layer
  step sync on a 10.1k-token read: layer 6 = 212.5 s, layer 21 = 45.9 s, layer 41 = 70.9 s;
  the other 44 layers ~0.7-2.2 s each. The big-prompt wall is dominated by a few layers'
  non-resident expert fetches (dropped pairs served from pack files / PCIe), not a uniform
  per-layer cost. Any future big-prompt PP work should start at those layers' residency,
  not at generic overlap.

## Decision

**Do not promote.** Both features stay default OFF; prod remains on the V5-validated
binaries (engine 331c2e89d00dd4f1 / helper afbf19f76e39ac16), unchanged by this session.
No quality-yardstick run: there is no winner to gate (both feature arms lose or tie; the
control merely matches the incumbent).

## Sessions 1-2 post-mortem (why the first two sessions looked like a code regression)

Sessions 1 (13:39) and 2 (13:39-14:25) and the A/B1 rerun (14:25-14:43) measured S1k at
8.6-29.2 s on the new binary while A/B2 (14:45-15:00) measured 6.2-6.5 s on the old binary
— a clean-looking binary attribution that was wrong. A/B3 (16:15-16:30, load over, memory
settled) ran THREE cells — the regression binary itself, a fresh f4e0bc3d build, a fresh
HEAD build — and all three measured S1k 6.17-6.47 s / TG 8.9 s. The "regression" was host
memory pressure during the background-load era (62 GiB box; prod pair ~47 GiB; background jobs + compile
on top; 24 GiB swapped at the worst point — the same pressure that stall-cycled prod
15:19-15:37). Banked lesson: on this 62 GiB host, bench arms are only valid with
MemAvailable > ~30 GiB and no swap thrash; the harness RAM gate (exit 3 below 30 GiB) is
the guard, and arms from pressured eras are void.

## Follow-ups (not ordered)

- Accumulation pathology of S1/S3 across rounds (pin-range/checkpoint growth) — needs a
  source read before any retry of these features.
- The three hot layers (6/21/41) of the 10k-token read — residency gap, measurable target.
- Helper 10 s prompt-tier deadline → blob-loss 400 under pathological slowness (S4, and
  session-1's mid-S18k death) — robustness candidate.

## A/B4 — STRATA_PROMPT_STAGE_MEMO (2026-11-05 19:04-19:30): DO NOT PROMOTE, both arms failed

Binary: build-memo/rpcw-hip/strata (sha16 95059fae184f6953) = shortpp-src c85cda34
+ 375183eb (note_dropped_read dedup) + b8470d07 (STRATA_PROMPT_STAGE_MEMO, prefill.cpp +105/-8).
Legs S214/S1k/S4k/S18k x3, same helper (4600 slots / 7000 MiB), ab3 swap-window skeleton.

| arm | S214 | S1k | S4k | S18k | end |
|-----|------|-----|-----|------|-----|
| M0 (flags OFF) | **107.4**/2.9/2.6 | 6.45/6.23/6.45 | 400 after 35 s | - | probe abort |
| M1 (MEMO=1) | 5.7/2.9/2.6 | 6.45/6.18/**9.0** | **113.2/22.5/88.3** | **60.1/242.9**/400 at 174 s | probe abort |
| S0-r1 baseline (f5fb0738) | 2.6 | 6.15-6.43 | 15.7-16.1 | 40.7-48.7 | ok |

The 400 is the server mapping an engine error: `prefill: the helper fallback lost a blob`.

- M0 fails FLAGS-OFF => the regression is in the default path of b8470d07 (its only file is
  prefill.cpp), not in the opt-in memo path. The "byte-identical default" claim did not hold.
- M1 adds slowness that scales with prompt size (S4k 1.4-7x baseline, S18k r1 243 s): the memo
  path itself is also wrong, not just slow.
- First-leg anomalies: M0 S214 r0 107.4 s (later legs 2.6-2.9) - first request after fill.
- 375183eb (dedup) is gated to the WINDOW_POPULATE recording path, not exercised here; its diff
  re-read looks clean. Suspect remains b8470d07 alone.
- Zero crash signatures in either engine log; teardowns clean. Restores: bench binary f5fb0738
  sha-verified back; prod relaunched supervised gen-1, health 200.

Follow-up: post-mortem b8470d07's prefill.cpp diff (helper-fallback blob handoff vs the new
staging code), fix forward or revert; HEAD of shortpp-src currently carries the regression
(prod unaffected: it runs the V5 binaries). Re-bench only after a flags-off S4k/S18k parity leg
passes on the fixed binary.
