# RAM-relief night — iq3xxs/iq3s occupancy push (2026-11-11, COMPLETE)

Goal: highest safe occupancy (helper + engine reserves) for iq3xxs (split 38) and
iq3s (split 36), spill ON/OFF, each gated by 250k infill + QA. Winners → prod.
Outcome: both promoted and serving/verified.

## Engine commits (branch helper-ipc-diet)

- `089b6efcf` allocator placement fix → v0217 (gate no longer tanks PP; tier fills correctly)
- `d82068115` spill-tier pairs join `--expert-helper-ram-relief` → v0218
- `38d91a17c` stage-cache pairs join the relief → v0219 (fix3)
- `ae71a6577` refill a lent slot from the pack file when its host copy was dropped (fix4)
  - v0219 final pins: engine ee1f39cd…526b, helper 1748c716…c30f (both helper binaries identical)

## Helper static border (budget = free − reserve − scratch − buffers; budget binds)

iq3xxs (free-at-hello ~7496 MiB, blob ≈1.608 MiB/pair): reserve 512→4130,
256→4335, 128→4405, 0→4484-4507 slots. Border = reserve 0, fill 7400, slots 5000.
iq3s (blob ≈1.86 MiB/pair): border mirrors 3858 slots at the same budget.

## Stage relief: works at boot, dead for serving (for now)

With `--adapt-swaps 0` the relief fires: 13832 pairs / 23533 MiB total
(helper 4504 = 7348 + stage 9328 = 16185 MiB), engine RSS 37.45 GiB (from ~48.7),
sys available 14 GiB (from ~2-3). BUT adapt-off collapses sustained prefill:
fresh 80k legs 220.7 → 78.5 / 74.5 tok/s (3×), infill250k aborted at 147k/247k
tokens @ 72.4 tok/s. Cause: the static no-eviction cache means later fresh prompts
borrow slots and refill from pack files at ~52 ms/slot, every leg (adaptive swaps
were load-bearing). The 16 GiB lever needs pack-file swap-in refills under
adaptive — future engine work, not tonight.

Regression found+fixed along the way: fix3 400'd every FRESH request
("refilling a lent slot failed:" with empty err) — `blob_stable` returns nullptr
for dropped pairs and refill_issue returned false without setting err; lent slots
always hold a stage-cache pair, exactly what 38d91a17c dropped. ae71a6577 falls
back to the pack-file copy (decode's own path) and populates every err path.
Reuse-heavy requests survived (idle-save verification, 105640 reused) which
is why the boot looked healthy.

## Matrix — iq3xxs (split 38, v0219, adaptive ON unless noted, helper border)

| arm | reserves | VRAM free | 8k tok/s | 80k tok/s | TG gen | infill250k | verdict |
|---|---|---|---|---|---|---|---|
| M1b adapt-off+stage relief | 256/256 | — | 222 | 220.7→78.5/74.5 | — | FAIL 72.4 | dead for serving |
| M1a | 256/256 | 404 MiB | 239-242 | 344.7/314.3/319.6 | 44.3/40.5 | 625.1 PASS | safe (=proven v0216 margin) |
| M2a spill ON | 256/256 | — | 234-236 | 320.8-322.8 | 40.0/38.0 | 616.6 PASS | no win, TG worse |
| **M3a** | **128/128** | **278 MiB** | **241.8-244.4** | **346.8/349.5/348.2** | 42.0/38.8 | **623.7 PASS** | **CHAMPION** |
| M4a | 64/64 | 212→**37 MiB** | 236-239 | 347.8-351.6 | 39.0/38.7 | 625.8 | gate FAIL (#2065 zone) |

Proven v0216 prod margin = 404 MiB free (days of serving, same LOW warnings).
M4a bought only +0.7% 80k over M3a right into the driver-paging zone — M3a is
the knee. Caveat on record: 278 MiB is beyond the proven point; watch for stalls.

## Matrix — iq3s (split 36, v0219, adaptive ON, helper border 3858)

| arm | reserves | 8k tok/s | 80k tok/s | TG | infill250k | verdict |
|---|---|---|---|---|---|---|
| **I1a** | **256/256** | **235.4-240.2** | **318.0-320.1** | 37.1/42.5 | **451.2 PASS** | **CHAMPION** |
| I2a spill ON | 256/256 | 226.6-231.3 | 304.8-305.7 | 39.0/27.6 | 411.5 PASS | loses everywhere |
| I3a | 128/128 | 235.3-241.5 | 321.1×3 | 37.0/38.1 | 417.0 PASS | −8% infill, not worth it |

iq3s engine RSS 55.5 GiB, helper 7.6, sys available 9 GiB (bigger blobs than iq3xxs).
Spill verdict final: dead on both models under adaptive (tier rows cost PCIe hops;
only 51-61 pairs even join under adaptive at these splits).

## QA / reproducibility gate (M3a)

- Prefill-leg SHAs (invariant-8-token prefix): MATCH across restart ✓
- Fresh single-request temp-0 generation: deterministic 3/3 identical (M3a-QA boot
  and again on the promoted prod boot) ✓
- warm/tg legs are multi-request conversation continuations: SHAs vary with
  checkpoint/park timing (21cccfb1 vs 08cf1843 warm; pre-existing spec-decode
  timing behavior, mechanism untouched tonight). SHA gate applies to prefill legs;
  warm/tg judged by judge-lines only.
- "cancelled first infill attempt" artifact: first 250k request sometimes reads
  246937/246942 then logs (cancelled) with a second completing — seen on M1a and
  M3a; completed run is the gate evidence. Worth a look someday, not a blocker.

## Promotions (both verified at boot)

- swift15 (iq3xxs): exe v0219, split 38 (search-derived), reserves 128/128,
  engine `--expert-helper-slots 5000 --expert-helper-reserve-mib 0`,
  launcher helper `--slots 5000 --reserve-mib 0 --fill-mib 7400`.
  Boot verified: 4507 pairs / 7348 MiB relief, adaptive ON, 278 MiB free, probes deterministic.
- iq3s: exe v0219, split 36, reserves 256/256 (unchanged), same helper border
  triple (mirrors 3858). Verified by the I1a lane itself.
- Conversation/prompt disk caching unchanged everywhere: RAM tier 4096 / disk
  24576 / idle-save / park-gain 4096 / `--prompt-cache-spread`; disk tier healthy
  all night (250k KV parks in ~10 s, corrupt=0, budget intact, orphan sidecars ≤1
  per boot window).

## RAM summary per promoted config

- iq3xxs prod: engine ~48.2-48.7 GiB RSS + helper ~7.9 → sys available ~14-15 GiB
- iq3s prod: engine ~55.5 GiB RSS + helper ~7.6 → sys available ~9 GiB
- The additional ~16 GiB (stage relief) stays unrealized until adaptive-compatible
  pack-file swap-in refills exist.

## Artifacts

- lanes: bench/ab-v0210/rpcprof/out/ab9-matrix-m{1,1a,2a,3a,4a}-*, ab9-qa-m3a-restart-*,
  ab9-iq3s-i{1a,2a,3a}-*
- trial configs: trial-m{1,1a,2a,3a,4a}.json, trial-i{1a,2a,3a}.json (+ pre-adapt variants)
- launcher: launch-trial-iq3s.sh (iq3s pack, border-parameterized)
- run_lane.sh (standing infill250k leg), ram_map.sh, extract_judge.py
