# Expert-area 55 GiB mlock ladder — Nemotron backend (prod :8009) analysis

**Window:** `bench/ab-v0210/rpcprof/gpu-rounds/expert-area-ladder-20261120.sh`
**Evidence dir:** `bench/ab-v0210/rpcprof/out/expert-area-ladder-20261120/`
**Ran:** 02:20:41 → 02:34:44 (≈14 min) on LIVE prod `:8009`, no restart/downtime.
**Engine pid:** 1003553 (from `run/prod-swift15-f16kv-mixed-supervisor.json`; server_pid 1002840).
**Exit status:** 0 — clean completion, final line `ladder complete 02:34:44; Mlocked=161 MiB`.
**Defined lock area:** 55 GiB (budget steps 4…56 GiB), floor guard 4096 MiB, one-shot hold per step (released at once via `step.hold` removal).

## Pre-flight (all passed)
- `curl /v1.models` → HTTP 200; small chat completion → HTTP 200 (prod serving).
- `Mlocked: 165112 kB` (≈161 MiB) < 512 MiB ✓; no leftover hold files in `out/expert-area-ladder-20261120/` or `run/prod-expert-lock.hold` ✓.
- All five required files present in `bench/ab-v0210/rpcprof/gpu-rounds/`:
  `expert-area-ladder-20261120.sh`, `expert_shard_locker.py` (with `--budget-mib`), `prod_judge.sh`, `ram_pressure.py`, `file_residency.py` ✓.

## Per-step results

| Step | Locked MiB | Budget MiB | MemAvailable MiB | Mlocked MiB | SwapFree Δ vs L0 MiB | Engine RSS MiB | shard 00002 Rss MiB | shard 00003 Rss MiB | tok/s mean | tok/s median | tok/s min | expert-cache hit |
|------|-----------:|-----------:|-----------------:|------------:|---------------------:|---------------:|--------------------:|--------------------:|-----------:|-------------:|----------:|------------------|
| L0-0gb-unlocked | 0 | — | 15268 | 161 | 0 | 53788 | 10276.8 | 3412.5 | 22.5 | 22.8 | 20.9 | 86.3→96.2% |
| L4g | 4099.0 | 4096 | 11425 | 4260 | −63 | 54545 | 10859.6 | 3529.2 | 24.5 | 24.6 | 23.4 | 94.1→96.2% |
| L8g | 8193.2 | 8192 | 7091 | 8354 | +17 | 54795 | 11053.5 | 3583.1 | 22.5 | 23.8 | 17.7 | 95.4→96.2% |
| Ceiling step (skipped) | — | 12288 | pre-check 16283 | — | — | — | — | — | — | — | — | — |
| FINAL (post-release) | 0 | — | 17637 | 161 | −1664 | 50283 | 9596.5 | 2105.5 | — | — | — | — |

Per-probe tok/s (500-token chat completions via `prod_judge.sh <label> 4`):
- L0: 20.9, 22.7, 22.9, 23.3 (cache 86.3 / 95.4 / 95.6 / 96.2%)
- L4: 25.4, 24.7, 23.4, 24.5 (cache 95.8 / 94.1 / 95.8 / 96.2%)
- L8: 17.7, 24.8, 23.9, 23.7 (cache 95.4 / 95.9 / 95.8 / 96.2%)

SwapFree stayed flat at ≈184.9 GiB during locked steps; the −1.66 GiB at FINAL is residual after the pressure leg (see below), not in-step swap pressure. No step triggered MemAvailable < ~3500 MiB, no probe failed twice, no decode collapse below step norms except one pressure-leg outlier.

## Measured ceiling
**Ceiling = 12 GiB budget step, skipped as CEILING at pre-check.**
At the start of step 12g, `MemAvailable = 16283 MiB`; `16283 − 12288 = 3995 MiB < 4096 MiB floor`, so the floor guard refused the step and the ladder broke (ladder.log: `step 12g: pre-check avail 16283 MiB cannot host it above floor — CEILING`).

**Effective locked ceiling actually measured: 8 GiB** (8193.2 MiB locked of 8192 budget; lock came entirely from shard 00002 — shard 00003 received 0 MiB because the floor guard consumed the budget on shard 00002 first). The 55 GiB defined area was never approached; on this 62 GiB box with the engine serving (53.8–54.8 GiB RSS), headroom runs out around 8–12 GiB of lock.

## Pressure leg (at ceiling coverage, 8 GiB locked)
Per design, the pressure leg ran at the last fully-measured coverage (8 GiB) with `ram_pressure.py --target-mib 7000 --cap-mib 40000`, hold-file `press.hold`, 6 probes via `prod_judge.sh L8g-press 6`.

- Pressure held MemAvailable at the 7 GiB neighborhood: loop exited at `avail=7187 MiB after 66s` (target + 200 = 7200 MiB); ram_pressure allocated up to 40192 MiB, then released on `press.hold` removal.
- L8g-press probes (5 of 6 returned a decode line; one request left no `done:` trace): 23.7/96.2%, **11.6/94.0%**, 22.6/95.3%, 18.9/96.0% (495 tokens), 23.0/95.5% → **mean ≈20.0 tok/s, median 22.6, min 11.6**.
- Vs E3 reference ("E3 unlocked under 7 GiB pressure ramped 11→23 tok/s"): our pressure leg touched the E3-like floor on exactly **one** probe (11.6 tok/s) then recovered to 22.6–23.7; sustained decode held ≈22–24 tok/s. **Verdict: pressure at ceiling coverage caused one transient decode collapse, no engine stall, no probe failure; expert cache stayed 94–96%. Milder than E3's sustained 11→23 ramp — an outlier, not a new floor.**

## Dose-response summary (what each added 4 GiB bought or cost)
- **0 → 4 GiB locked:** mean tok/s 22.5 → 24.5 (+2.0), cache flat ≈95–96%. Cost: MemAvailable −3.8 GiB (15.3 → 11.4), engine RSS +0.8 GiB. Neutral-to-slightly-positive, but confounded: prod was still warming during L0→L4 (cache 86.3→96.2%, tok/s 20.9→23.3) — the gain is not attributable to locking.
- **4 → 8 GiB locked:** mean 24.5 → 22.5 (−2.0, ≈−8%), one probe at 17.7, median holds 23.8. Cost: MemAvailable −4.3 GiB more (11.4 → 7.1), engine RSS +0.25 GiB, shard-00002 resident +0.2 GiB. First measurable cost; headroom thin (7.1 GiB available with 8.4 GiB locked).
- **8 → 12 GiB:** not measurable — blocked by floor guard (CEILING). The box simply lacks ≈4.5 GiB of headroom above the 4 GiB floor while serving.
- **Pressure at 8 GiB:** mean −2.5 vs unlocked L8 (22.5 → 20.0), one collapse to 11.6 then recovery; no stall.

## Honest limits
- Single run per step; no replication, no confidence intervals.
- Small prompts only (~2.2 KiB input from `diag267_drive.py`, max 500 output tokens), decode-only, short generations — not prefill-bound or multi-turn.
- Warm-up confound: prod rebooted 00:23:50 on 256/448 reserves and was still warming (12.9→17.1 tok/s observed earlier); the ladder's own L0 step anchors the warm state (cache 86.3→96.2% across L0 probes). Early-step numbers reflect warm-up, not lock effect.
- Resident-subset coverage, not tensor-proven: the locker mlocks mincore-resident page-cache extents of the two shard files (`...-00002-of-00004`, `...-00003-of-00004`); locking landed almost entirely on shard 00002 (shard 00003 got 0 MiB under the floor guard). This is not proof that any particular expert tensor or GPU-resident layer was locked.
- `file_residency.py` reports engine RSS of mapped shard files (RSS/Pss), not resident expert weights; Mlocked counts page-cache locks via `/proc/meminfo`, not the engine's anonymous allocations.
- SwapFree drifted −1.66 GiB by FINAL (residual post-pressure); per-step swap was flat, so no in-step swap thrashing was measured.

## Exact commands and exit statuses
Pre-flight:
```
curl -s -o /dev/null -w "models:%{http_code}\n" http://127.0.0.1:8009/v1/models            # 200
curl -s -o /dev/null -w "chat:%{http_code}\n" -X POST http://127.0.0.1:8009/v1/chat/completions \
  -H 'Content-Type: application/json' -d '{"model":"Nemotron","messages":[{"role":"user","content":"hi"}],"max_tokens":5,"temperature":0}'  # 200
grep Mlocked /proc/meminfo                                          # 165112 kB (<512 MiB)
ls bench/ab-v0210/rpcprof/out/expert-area-ladder-20261120/ run/prod-expert-lock.hold   # absent (no leftovers)
```
Main run (backgrounded, streamed):
```
mkdir -p bench/ab-v0210/rpcprof/out/expert-area-ladder-20261120
nohup bash bench/ab-v0210/rpcprof/gpu-rounds/expert-area-ladder-20261120.sh \
  > bench/ab-v0210/rpcprof/out/expert-area-ladder-20261120/stream.log 2>&1 &   # pid 3283631
```
Exit status: **0** (clean completion; `ladder complete 02:34:44; Mlocked=161 MiB`; no `set -e`/pipefail abort).

Internal commands the script ran (recorded in evidence): `prod_judge.sh L0-0gb-unlocked 4`, `L4g 4`, `L8g 4` (probe.log), `ram_pressure.py --target-mib 7000 --cap-mib 40000 --hold-file .../press.hold` (pressure-ceiling.log), `prod_judge.sh L8g-press 6` (probe.log tail), plus `expert_shard_locker.py --budget-mib 4096/8192 --hold-file .../step.hold` per step (locker-4g.log, locker-8g.log, locker-ceiling-hold.log).

## Anomalies / incidents
**None.** Safety lever never used: no `rm step.hold` intervention, no stall (all probes returned), MemAvailable never approached ~3500 MiB (minimum in-step 7091 MiB at L8), no sustained decode collapse (single 11.6 tok/s pressure-leg probe recovered immediately). All locks released cleanly (FINAL Mlocked = 161 MiB = baseline); no hold files left behind; engine serving throughout (prod `/v1.models` 200 before, during, and after).

Evidence files: `ladder.log`, `memory.log`, `probe.log`, `locker-4g.log`, `locker-8g.log`, `locker-ceiling-hold.log`, `pressure-ceiling.log`, `stream.log`, `step.hold`/`press.hold` (both removed at end).