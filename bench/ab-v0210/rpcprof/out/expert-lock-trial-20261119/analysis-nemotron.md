# Expert-page lock trial — analysis (Nemotron session)

**Trial:** expert-lock-trial-20261119 (live prod :8009, no downtime, no engine change)
**Evidence dir:** bench/ab-v0210/rpcprof/out/expert-lock-trial-20261119/
**Model:** Qwen3.8-Flash-Next-UD-IQ3_XXS (shards 00002 + 00003 of 00004)
**Engine pid:** 3538773  **Helper pid:** 3537319  **Pressure target:** 4500 MiB MemAvailable
**Probes:** prod_judge.sh, 6 requests × 500 tokens per leg
**Leg order:** A-base-nolock → B-lock-nopress → C-lock-press → D-nolock-press → FINAL snap → trial complete
**Trial wall:** 2026-11-19 22:17:16 → 22:44:53 (~27 min; within the 30–50 min estimate)
**Verdict summary:** decisive pressure comparison (C vs D) NOT actionable — neither pressure phase reached the 4500 MiB MemAvailable target (ram_pressure hit its 24 GiB allocation cap both times). Lock alone (B vs A) shows no significant cost. No refault signature observable in D because no pressure was applied.

## 1. Per-leg results (probe.log: "LABEL: N tokens, decode X tok/s, expert cache Y% hit")

| Leg | Lock | Pressure effective? | Req | Tokens | Decode tok/s | Expert cache hit % |
|-----|------|---------------------|-----|--------|--------------|-------------------|
| A-base-nolock | off | n/a (none) | 1 | 500 | 13.1 | 92.5 |
| A-base-nolock | off | n/a (none) | 2 | 500 | 15.6 | 95.4 |
| A-base-nolock | off | n/a (none) | 3 | 500 | 18.9 | 95.7 |
| A-base-nolock | off | n/a (none) | 4 | 500 | 18.5 | 94.8 |
| A-base-nolock | off | n/a (none) | 5 | 500 | 20.0 | 95.8 |
| A-base-nolock | off | n/a (none) | 6 | 500 | 21.2 | 95.2 |
| B-lock-nopress | on | n/a (none) | 1 | 481 | 13.8 | 96.2 |
| B-lock-nopress | on | n/a (none) | 2 | 500 | 11.7 | 96.2 |
| B-lock-nopress | on | n/a (none) | 3 | 500 | 16.4 | 93.8 |
| B-lock-nopress | on | n/a (none) | 4 | 500 | 19.1 | 95.4 |
| B-lock-nopress | on | n/a (none) | 5 | 500 | 19.3 | 93.6 |
| B-lock-nopress | on | n/a (none) | 6 | 500 | 22.6 | 95.7 |
| C-lock-press | on | NO (target missed, ~9556 MiB) | 1 | 500 | 20.9 | 95.3 |
| C-lock-press | on | NO (target missed, ~9556 MiB) | 2 | 500 | 22.5 | 93.4 |
| C-lock-press | on | NO (target missed, ~9556 MiB) | 3 | 500 | 23.0 | 95.4 |
| C-lock-press | on | NO (target missed, ~9556 MiB) | 4 | 500 | 23.1 | 95.8 |
| C-lock-press | on | NO (target missed, ~9556 MiB) | 5 | 500 | 23.1 | 92.9 |
| C-lock-press | on | NO (target missed, ~9556 MiB) | 6 | 500 | 23.5 | 94.7 |
| D-nolock-press | off | NO (target missed, ~18.4 GiB) | 1 | 500 | 18.6 | 95.8 |
| D-nolock-press | off | NO (target missed, ~18.4 GiB) | 2 | 500 | 20.4 | 95.4 |
| D-nolock-press | off | NO (target missed, ~18.4 GiB) | 3 | 500 | 22.1 | 95.4 |
| D-nolock-press | off | NO (target missed, ~18.4 GiB) | 4 | 470 | 21.5 | 92.7 |
| D-nolock-press | off | NO (target missed, ~18.4 GiB) | 5 | 500 | 24.4 | 95.5 |
| D-nolock-press | off | NO (target missed, ~18.4 GiB) | 6 | 500 | 23.4 | 95.8 |

Per-leg summary (mean/median/min/max tok/s over 6 requests; expert-cache mean):

| Leg | Mean tok/s | Median | Min | Max | Expert cache mean % |
|-----|------------|--------|-----|-----|---------------------|
| A-base-nolock | 17.9 | 18.7 | 13.1 | 21.2 | 94.9 |
| B-lock-nopress | 17.2 | 17.8 | 11.7 | 22.6 | 95.2 |
| C-lock-press | 22.7 | 23.1 | 20.9 | 23.5 | 94.6 |
| D-nolock-press | 21.7 | 21.8 | 18.6 | 24.4 | 95.1 |

Note: C and D numbers are not a pressure comparison — both legs ran without effective memory pressure (§4). C vs D here is effectively locked-vs-unlocked with warm cache; the ~1 tok/s edge to C is within leg-to-leg variance (A 17.9 → B 17.2 → C 22.7).

## 2. Memory snapshots (per-leg `snap` from memory.log; MiB)

| Leg | Time | MemAvailable | Mlocked | SwapFree | Shard 00002 Rss | Shard 00003 Rss | Matched Rss total |
|-----|------|--------------|---------|----------|-----------------|-----------------|-------------------|
| A-base-nolock | 22:17:16 | 12367 | 161 | 186645 | 4080.5 | 1489.4 | 5607.2 |
| B-lock-nopress | 22:20:46 | 4090 | 8943 | 186926 | 6254.0 | 2373.7 | 8664.1 |
| C-lock-press | 22:30:58 | 9556 | 8713 | 184047 | 7314.2 | 2467.8 | 9818.5 |
| D-nolock-press | 22:41:41 | 18454 | 161 | 184065 | 6083.9 | 1334.4 | 7452.4 |
| FINAL | 22:44:53 | 17067 | 161 | 185068 | 7163.8 | 2419.2 | 9617.0 |

Deltas: B−A: MemAvailable −8277 MiB (lock cost), Mlocked +8782 MiB, matched Rss +3057 MiB (resident growth during probing/generation). D−C: MemAvailable +8898 MiB (unlock released ~8.7 GiB), Mlocked −8552 MiB, matched Rss −2366 MiB (pages evicted after unlock).

## 3. Locker (expert_shard_locker.py, files S2+S3, floor 4096 MiB MemAvailable)

- Shard 00002: 46.52 GiB mapped, 13452 resident extents = 7709.9 MiB → locked **7709.9/7709.9 MiB (100%)**
- Shard 00003: 4.53 GiB mapped, 3249 resident extents = 2470.3 MiB → locked **1071.7/2470.3 MiB (~43%)**
- **FLOOR line present in locker.log:** "FLOOR: MemAvailable 4096 MiB - next 2 MiB < floor 4096; stopping partial (file_locked=... MiB)" — lock stopped on shard 00003 at the MemAvailable floor; shard 00003 only partially locked.
- LOCKED-SETTLED: total=8781.6 MiB, MemAvailable=4096 MiB, Mlocked=8942 MiB → trial continued (floor stop is by design, not a trial abort).
- Lock cost: ~8.8 GiB Mlocked; MemAvailable at B snap 4090 MiB (essentially at floor).
- **Anomaly flagged:** shard 00003 partial coverage due to floor — C-leg lock protection covers shard 00002 fully and only ~43% of shard 00003 resident pages.

## 4. Pressure phases (ram_pressure.py, target 4500 MiB MemAvailable, cap 24576 MiB)

| Phase | Start | Reached target (MemAvailable ≤ ~4650 MiB)? | Outcome |
|-------|-------|--------------------------------------------|---------|
| pressure.log (locked) | 22:23:57 | NO | Allocated to 24 GiB cap; MemAvailable bottomed ~8016 MiB; script logged "pressure reached MemAvailable=9556 MiB after 421s" (420s timeout, not target); then released |
| pressure2.log (unlocked) | 22:34:40 | NO | Allocated to 24 GiB cap; MemAvailable bottomed ~6837 MiB; released; script timed out at ~22:41 and started D with MemAvailable ~18.4 GiB |

Root cause: this box has ~19 GiB of free/reclaimable RAM; a 24 GiB anonymous allocation cannot drive MemAvailable down to 4.5 GiB, so the 420s timeout fires before the target. Both "pressure" legs ran at ~9.6 GiB / ~18.4 GiB MemAvailable respectively — no pressure applied.

## 5. Key comparisons

- **Decisive pair C (locked + pressure) vs D (unlocked + pressure) at the same 4500 MiB target: cannot be evaluated** — neither pressure phase reached target. No pressure → no refault scenario → the lock's protective value under pressure is unmeasured by this trial.
- **Refault signature in D (early requests slow, later recovering): not observed** — D's requests run steady 18.6–24.4 tok/s with no slow-start pattern, consistent with no pressure being applied.
- **C lacks refault:** trivially true (no pressure), not evidence of lock protection.
- **B vs A (no-pressure sanity):** B mean 17.2 vs A 17.9 tok/s (~4%); cache hit 95.2 vs 94.9%. Lock alone has no significant cost or benefit; within noise.
- **C vs D (locked vs unlocked, both no-effective-pressure):** 22.7 vs 21.7 tok/s — nearly identical; any edge is confounded by leg timing/warm cache and not attributable to the lock.

## 6. Lock cost / benefit

- MiB locked: 8781.6 MiB total settled (7709.9 shard 00002 + 1071.7 shard 00003).
- MemAvailable cost at B snap: ~8277 MiB (12367 → 4090 MiB), i.e. essentially the floor.
- Mlocked delta: +8782 MiB (161 → 8943 MiB).
- Floor never breached: the locker self-stopped at MemAvailable=4096 MiB (floor); during C the snap shows 9556 MiB (pressure phase had released); no FLOOR abort, no "locker FAILED to settle".

## 7. Honest limits

- Single run, no repeats; one pressure target (4500 MiB) that was never achieved.
- Small probe prompts (500 tokens each); results may not scale to long contexts.
- Locking is resident-subset (mincore/mlock over file extents); tensor-level expert-only coverage inferred from the serving path — not proven by this trial.
- No quality/perplexity measurement; tok/s only.
- Shard 00003 only ~43% locked (floor stop) — any C-leg protection claim is partial for that shard.
- Comparison C vs D compromised by the pressure target miss; conclusions about "lock protects under pressure" cannot be drawn.

## 8. Anomalies / errors detected

| Time | Source | Description | Disposition |
|------|--------|-------------|-------------|
| 22:20 | locker.log | FLOOR guard hit on shard 00003; partial lock 1071.7/2470.3 MiB (~43%) | Trial continued; coverage gap noted in §3 |
| 22:23 | probe.log | B-lock-nopress request 1 generated 481 tokens (not 500) | Minor; likely early stop/EOS |
| 22:25 | pressure.log | Pressure (locked) never reached 4500 MiB MemAvailable; hit 24 GiB allocation cap (min ~8016 MiB), script timed out at 421s with 9556 MiB | **Major: C-leg ran without effective pressure** |
| 22:36 | pressure2.log | Pressure (unlocked) identical failure: cap at 24 GiB, min ~6837 MiB, timed out | **Major: D-leg ran without effective pressure; decisive C-vs-D comparison cannot be made** |
| 22:43 | probe.log | D-nolock-press request 4 generated 470 tokens (not 500) | Minor; likely early stop/EOS |

No "locker FAILED to settle", no stall (>10 min no file growth), no error text seen; trial reached "=== trial complete 22:44:53 ===" cleanly.
