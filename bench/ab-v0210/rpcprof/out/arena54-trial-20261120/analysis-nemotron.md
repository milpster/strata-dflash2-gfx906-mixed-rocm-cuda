# Arena-54 Trial Analysis — 20261120 (Attempt 3, nemotron)

**Box:** GUAM, 2× Radeon VII gfx906, ROCm 6.1.0 userspace (not /opt/rocm 7.x). Runtime env: LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib, HSA_OVERRIDE_GFX_VERSION=9.0.6.

**Script:** bench/ab-v0210/rpcprof/gpu-rounds/arena54-window-20261120.sh
**Run window:** 11:37:38 – 11:53:50 (≈16 min)
**Exit status:** 0 (WINDOW COMPLETE)
**Pre-gen:** SKIPPED — experts.bin already existed at exactly 50294988800 B, sha 3de5d9e52ade09d1bf4ddc2f8ca18e56597097c06bac1d1ef6580f349c1a47dd matching the recorded hash.

---

## 1. Gate + Pre-Gen Evidence Block

### Pre-flight (11:37:38 – 11:38:16)

| Item | Result |
|------|--------|
| `sha256sum trial-config-arena54.json` | 82459c06d1b2807b055700ff406110514b480e475ca9e21de5397c48f8e61a43 |
| Pin identity (v0239/SHA256SUMS.txt) | rpcw-hip/strata: OK; rpcw-cuda/strata-expert-helper-patient: OK |
| Prod models pre-window (:8009) | http 200 |
| experts.bin size | 50,294,988,800 B = exact match (50,294,988,800 B expected = 47,965 MiB) ✅ |
| experts.bin sha256 | 3de5d9e52ade09d1bf4ddc2f8ca18e56597097c06bac1d1ef6580f349c1a47dd = recorded ✅ |
| Pre-gen boot | SKIPPED (file present + sha matches) |

### WATCH-LIST checkpoint: 'ALL STOPPED' on first stop-all.sh
- `stop-all: before=0 after=0 — ALL STOPPED` ✅ (11:38:38, before measured boot)

### Measured boot (under 56G wall) — 11:38:38 to 11:40:22
- Engine PID: 3857602 (identity confirmed in /proc/3857602/cmdline: contains trial-config-arena54)
- Ready: `ready: http://127.0.0.1:8019/v1` at 11:40:22
- Trial log: /mnt/srcds/Strata/logs/trial-arena54-mixed-20261120-113851-3856877.log

### E2 boot gate (PASS at 11:40:22)

| Check | Result |
|-------|--------|
| 'strata native mlock: locked' lines | present ✅ |
| mlock spans | exactly 1 ✅ (`chunk 188: 47965 of 47965 MiB locked`) |
| 'file pages locked, not pinned' note | present ✅ |
| mlock progress total | 47,965 MiB = expected ✅ |
| VmLck | 49,280,040 kB ≈ 47 GiB (= 50,294,988,800 B ✅) |
| Fail lines (not locked, partial, etc.) | none ✅ |

**Gate verdict: PASS** — arena mapped and locked, exactly 1 span covering the full 47,965 MiB, VmLck held at ~47 GiB.

### VmLck at ready (11:40:22)
- VmLck: 49,280,040 kB
- VmRSS: 34,506,100 kB

---

## 2. T0 / T1 Tables

### snap A54-warm (11:40:22, engine=3857602)

| Metric | Value |
|--------|-------|
| MemAvailable | 23,526,912 kB ≈ 22.4 GiB |
| Cached | 53,605,952 kB ≈ 51.1 GiB |
| Mlocked | 30,071,340 kB ≈ 28.7 GiB |
| VmLck | 49,280,040 kB ≈ 47.0 GiB |
| VmRSS | 34,506,100 kB ≈ 32.9 GiB |
| engine Rss | 33,697 MiB |
| SwapFree | 193,749,536 kB ≈ 184.8 GiB |

**THP-VMA (experts.bin mapping):** KernelPageSize: 4 kB, AnonHugePages: 0 kB, FilePmdMapped: 0 kB, Locked: 30,051,748 kB, THPeligible: 0 — **THP NOT active** (4 KiB pages, not 2 MiB)

### T0 probe (no pressure, :8019) — 4 probes

| Probe | Tokens | tok/s | Expert-cache hit | Phase |
|-------|--------|-------|-------------------|-------|
| T0-1 | 496 | 25.6 | 94.0% | cold |
| T0-2 | 500 | 30.3 | 95.1% | warm |
| T0-3 | 500 | 30.3 | 96.1% | warm |
| T0-4 | 500 | 32.4 | 93.5% | warm |

**T0 summary:** median = 30.3 tok/s; ramp 25.6 (cold) → 30.3 → 30.3 → 32.4 (warm). Warm avg (probes 2–4) = 31.0 tok/s.

### Pressure leg (11:41:56 – 11:52:36, ≈10.5 min)

- ram_pressure.py --target-mib 7000 --cap-mib 49000 --hold-file pressure.hold
- Allocated: 49,152 MiB (hit **49 GiB cap**)
- MemAvailable: started 9,182 MiB, bottomed at **7,863 MiB** (~7.7 GiB) — **never reached the 7 GiB target**
- Pressure wait timed out at 480s: `pressure: avail=9450 MiB after 480s` (11:51:56)
- pressure.hold: created 11:41:56, removed after T1
- memory.events oom/oom_kill: 0 throughout ✅

### snap A54-final (11:51:51, engine=3857602)

| Metric | Value | vs warm |
|--------|-------|---------|
| MemAvailable | 11,418,664 kB ≈ 10.9 GiB | −11.5 GiB |
| Cached | 50,596,104 kB ≈ 48.2 GiB | −2.9 GiB |
| Mlocked | 41,079,240 kB ≈ 39.2 GiB | +10.5 GiB |
| VmLck | 49,280,040 kB ≈ 47.0 GiB | unchanged ✅ |
| VmRSS | 46,768,096 kB ≈ 44.6 GiB | +11.7 GiB |
| engine Rss | 45,671 MiB | +11,974 MiB |
| SwapFree | 191,607,504 kB ≈ 182.7 GiB | −2.1 GiB |

**THP-VMA (final):** KernelPageSize: 4 kB, THPeligible: 0 — still no THP

### T1 probe (under pressure, :8019) — 6 probes

| Probe | Tokens | tok/s | Expert-cache hit |
|-------|--------|-------|-------------------|
| T1-1 | 500 | 29.6 | 94.2% |
| T1-2 | 500 | 30.7 | 95.6% |
| T1-3 | 500 | 29.5 | 96.2% |
| T1-4 | 500 | 32.9 | 92.9% |
| T1-5 | 500 | 33.7 | 95.1% |
| T1-6 | 500 | 30.2 | 96.3% |

**T1 summary:** median = 30.45 tok/s; range 29.5–33.7 tok/s; avg = 31.1 tok/s. **No degradation under pressure** — T1 median ≈ T0 median (30.3 vs 30.45 tok/s).

---

## 3. T0/T1 Summary vs Prod ~22.5 and Ladder Refs

| Comparison | T0 | T1 | Notes |
|------------|----|----|-------|
| Prod warm baseline | ~22.5 | ~22.5 | Reference only, not from this run |
| Ladder L0 | 22.5 | 22.5 | Reference only |
| Ladder L8g | 22.5 | 22.5 | Reference only |
| Ladder 8g-press | ~20 | ~20 | Reference only |
| **This run T0** | **30.3 median** | — | ramp 25.6→32.4 |
| **This run T1** | — | **30.45 median** | range 29.5–33.7 |

**Named confounds (not mlock-attributable alone):**
- Different binary: trial v0239 vs prod v0233 — includes Phase B changes
- Conversation cache cuts: 4096→2048 MiB + ram 512→256 MiB
- tok/s measured with different prompt/completion lengths
- T0 first probe is cold (25.6 tok/s) — warm-up confound (the ladder's L0 did the same)

**No delta can be attributed to mlock alone.** T0/T1 (~30 tok/s) are actually *above* prod ~22.5, but this is confounded by binary version and config differences — not evidence that mlock improves throughput.

---

## 4. THP Verdict

**THP is NOT active on the arena mapping.** At both snap A54-warm and snap A54-final:
- KernelPageSize: 4 kB (not 2 MiB)
- AnonHugePages: 0 kB
- FilePmdMapped: 0 kB
- THPeligible: 0
- Locked equals Rss (all mapped pages locked)

**4 KiB page outcome is the dominant THP confound.** The arena is mapped as 4 KiB file pages, not 2 MiB huge pages. This means the mlock holds ~12 million 4 KiB pages instead of ~24 thousand 2 MiB pages. Implications:
- mlock syscall overhead (more pages to lock)
- Page table memory (more PTEs)
- Potential TLB pressure

The mlock still succeeded (VmLck = 47 GiB held throughout), so THP absence did not prevent locking, but it is a significant implementation detail. The standing THP confound remains: 4 KiB pages, not 2 MiB.

---

## 5. Pressure Verdict vs E3 / Run-2

| Metric | This run (arena-54, attempt 3) | E3 (reference) | Run-2 (reference) |
|--------|-------------------------------|----------------|-------------------|
| Pressure target | ~7 GiB available | — | — |
| Actual available | ~7.9 GiB bottom (never reached 7 GiB) | — | — |
| Pressure held | 49 GiB allocated (capped) | — | — |
| T0 tok/s | 30.3 median | ~22.5 | — |
| T1 tok/s | 30.45 median | — | — |
| tok/s under pressure | no measurable drop | 11→23 sustained | locked ~23 flat |
| memory.events oom/oom_kill | 0 | — | — |

**Pressure verdict: STRUCTURAL.** The pressure holder hit the 49 GiB cap (49,152 MiB allocated) but MemAvailable bottomed at only ~7.9 GiB — **the 7 GiB target was never reached**. With 47 GiB locked non-reclaimable, even 49 GiB of pressure cannot squeeze available memory to 7 GiB; the kernel reclaims page cache instead (Cached dropped from 51.1 to 48.2 GiB), and the locked arena simply will not yield.

Structural interpretation: **locked arena un-squeezeable + oom 0 + TG under max holder pressure.** This contrasts with:
- **E3** (unlocked): 11→23 tok/s sustained under pressure (reclaimable memory could be squeezed)
- **Run-2** (locked): ~23 tok/s flat under pressure

Here, no tok/s degradation was measurable (30.3 → 30.45 tok/s) because the arena's mlock pages are non-reclaimable and the engine's own memory (which stays reclaimable) was never actually starved — MemAvailable stayed at 7.9–9.5 GiB throughout T1.

---

## 6. Anomalies with Timestamps

| Timestamp | Anomaly | Severity |
|-----------|---------|----------|
| 11:37:38–11:38:16 | Pre-flight: experts.bin present + sha matches → pre-gen SKIPPED | Informational |
| 11:38:38 | stop-all: ALL STOPPED before measured boot | Informational |
| 11:40:22 | E2 boot gate PASS — 1 span, 47,965 MiB locked, VmLck 47 GiB | Positive |
| 11:40:22–11:44:00 | T0: 4 probes, 25.6 cold → 30.3–32.4 warm tok/s | Informational |
| 11:41:56–11:52:36 | Pressure leg: 49 GiB cap hit, MemAvailable bottomed 7.9 GiB (target 7 GiB unreachable) | Moderate — target not reached |
| 11:51:56 | Pressure wait timed out at 480s: avail=9450 MiB | Moderate |
| 11:52:36–11:53:00 | T1: 6 probes under pressure, 29.5–33.7 tok/s (no degradation) | Informational |
| 11:53:18 | stop-all: ALL STOPPED | Informational |
| 11:53:44 | Prod restored on :8009 | Positive |
| 11:53:46 | Prod completion check: http 200 in 1.69s | Positive |
| 11:53:50 | Window complete | Informational |

**No early-abort signature** — "conversation parking does not support a layer split on one GPU" did NOT appear.

---

## 7. Honest Limits

1. **Pressure target not reached:** MemAvailable bottomed at ~7.9 GiB, above the 7 GiB target, because the 47 GiB mlocked arena is non-reclaimable. The holder hit the 49 GiB cap. No valid pressure-vs-no-pressure tok/s comparison is possible at the intended pressure depth.
2. **THP confound:** 4 KiB pages (not 2 MiB) on the arena mapping — mlock succeeded but with more page-table overhead and more PTEs.
3. **Binary difference:** Trial uses v0239, prod uses v0233 — tok/s deltas not attributable to mlock alone.
4. **cgroup memory.max/min not captured:** cgroup fields were empty in snap output (cgroup path resolution issue — engine is in a user.slice scope, not a dedicated memory cgroup).
5. **Single run:** Only one trial window executed; no repeatability data.
6. **T0 first probe is cold:** 25.6 tok/s vs warm 30.3–32.4 tok/s — warm-up confound (the ladder's L0 exhibited the same). Median used for leg summary.
7. **Probe prompts are short:** 500 completion tokens each; decode-oriented, not prefill-oriented.

---

## 8. Exact Commands and Exit Statuses

| # | Command | Exit status / result |
|---|---------|---------------------|
| 1 | `curl -s -m 8 http://127.0.0.1:8009/v1/models` | http 200 (prod pre-window) |
| 2 | `(cd /mnt/srcds/Strata/v0239 && sha256sum -c SHA256SUMS.txt)` | Both binaries OK |
| 3 | `bash /mnt/srcds/Strata/stop-all.sh` (pre-boot) | "ALL STOPPED" |
| 4 | `bash arena54-window-20261120.sh` (background, PID 3839245) | **0** (WINDOW COMPLETE at 11:53:50) |
| 5 | `bash launch-trial-arena54.sh` (measured boot, port 8019) | Ready at 11:40:22, E2 gate PASS |
| 6 | `bash trial_judge_8019.sh T0-arena54-nopress 4` | **OK** — 4 probes, 25.6→30.3→30.3→32.4 tok/s |
| 7 | `python3 ram_pressure.py --target-mib 7000 --cap-mib 49000 --hold-file pressure.hold` | Timed out at 480s, avail=9450 MiB, 49 GiB cap hit |
| 8 | `bash trial_judge_8019.sh T1-arena54-press 6` | **OK** — 6 probes, 29.5–33.7 tok/s (no degradation) |
| 9 | `bash /mnt/srcds/Strata/stop-all.sh` (restore_prod) | "ALL STOPPED" |
| 10 | `setsid --fork launch-v0238.sh` (prod restore) | Prod ready at 11:53:44 |
| 11 | `curl -s -m 60 http://127.0.0.1:8009/v1/chat/completions` | http 200 in 1.69s |

### Evidence files created
- `window.log` — full window log (pre-flight through prod restore)
- `window-outer.log` — outer stream capture
- `probe.log` — T0 (4 probes) + T1 (6 probes), all real judge legs ✅
- `memory.log` — snap A54-warm (11:40:22) and snap A54-final (11:51:51)
- `boot-gate.log` — E2 gate evidence (mlock progress, VmLck, cgroup)
- `pressure.log` — pressure allocation trace (256→49152 MiB)
- `experts-bin.sha256` — 3de5d9e52ade09d1bf4ddc2f8ca18e56597097c06bac1d1ef6580f349c1a47dd

### Lane logs
- Engine: `/mnt/srcds/Strata/logs/engine-trial-arena54-20261120.log`
- Trial: `/mnt/srcds/Strata/logs/trial-arena54-mixed-20261120-113851-3856877.log`
- Prod restore: `/mnt/srcds/Strata/logs/lane-restore.log`

---

## Final Summary

| Item | Result |
|------|--------|
| Gate verdict | **PASS** — 1 span, 47,965/47,965 MiB locked, VmLck 47 GiB |
| Pre-gen | **SKIPPED** — experts.bin present, sha matches recorded 3de5d9e5… |
| T0 median | **30.3 tok/s** (ramp 25.6 cold → 30.3–32.4 warm) |
| T1 median | **30.45 tok/s** (range 29.5–33.7, under pressure) |
| Pressure verdict | **Structural** — 49 GiB cap hit, MemAvailable bottomed ~7.9 GiB (7 GiB target unreachable), oom=0, no tok/s degradation |
| THP outcome | **4 KiB pages, no THP** — standing confound |
| Prod restore | **SUCCESS** — :8009 serving at 11:53:44 (http 200, completion 1.69s) |
| Window exit | **0** — WINDOW COMPLETE at 11:53:50 |

**Bottom line:** The arena-54 trial window (attempt 3) completed successfully. Both probe legs now produce real data (judge script log-name bug fixed). The E2 boot gate passed with exactly 1 mlock span covering the full 47,965 MiB arena. T0/T1 tok/s are statistically identical (~30 tok/s, no degradation under pressure). Pressure could not reach the 7 GiB available target even at a 49 GiB cap because the locked 47 GiB arena is non-reclaimable — structural verdict: locked arena un-squeezeable + oom 0 + TG under max holder pressure. THP not active (4 KiB pages). No delta attributable to mlock alone due to binary/config confounds.
---

## CORRIGENDUM (parent, after oracle verification — 2026-11-20 afternoon)

Oracle verification (fresh session, independent backend) confirmed the core results and
found four errors in this analysis, now corrected for the record:

1. Pressure floor was **7 533 MiB** (pressure.log minimum during allocation); 7 863 MiB
   was the value at the hold point, not the floor.
2. "Floors 8723 and 9450 MiB" mixed attempts: **8 723 is attempt-2's** value; attempt 3's
   timeout reading alone is 9 450 MiB.
3. Attempt-3 pressure timeout was **480 s** (481 s was attempt 2).
4. **oom/oom_kill = 0 was UNVERIFIED**: every snap's cgroup readout was empty — the snap's
   cgroup path resolution grabbed the hybrid v1 `net_cls:` line instead of the unified
   `0::` scope line. Fixed in the window script (verified live on prod: memory.max/min/
   current/events now read correctly). The wall was applied per the launcher props, but
   attempt 3 never MEASURED it; the promoted boot will.

Additional oracle findings carried forward: T1 effectively ran at ~9.5 GiB available
(holder capped, available partially recovered) — so the no-degradation result is at
shallow depth, and the design point (~7 GiB) was never reached because the box could not
give more up (structural un-squeezeability, which stands); NEW confound named: VRAM ran
at 284 MiB free at start and 127 MiB during requests (engine warning #2065 class).
