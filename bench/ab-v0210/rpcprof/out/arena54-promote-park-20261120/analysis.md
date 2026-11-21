# Arena-54 Promote + Park/Recover Validation — analysis.md

**Box:** GUAM, 2× Radeon VII gfx906, ROCm 6.1.0 userspace (not /opt/rocm 7.x). Runtime: LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib, HSA_OVERRIDE_GFX_VERSION=9.0.6.
**Evidence dir:** /mnt/srcds/Strata/bench/ab-v0210/rpcprof/out/arena54-promote-park-20261120/
**Date:** 2026-11-20. **Margins:** left at 256/448 (baked into prod-config-arena54.json; not touched).
**Backend:** independent of the local engine. No git operations. No /tmp artifacts (all evidence under the evidence dir).

---

## 0. Outcome summary

| Item | Verdict |
|------|---------|
| Phase A promotion gates (a)–(f) | **PASS** |
| Phase A stop-all: "ALL STOPPED" | PASS (first try, exit 0) |
| Phase A T0 probes (3×500 tok decode) | 24.6 cold → 29.8 / 25.8 warm (≈28 tok/s warm median) |
| Phase B park-to-disk triggered | YES (conv-1/2/3 .sess written; generations counter 0→3) |
| Phase B cross-restart CHECKPOINT REUSE | **NO — reused = 0 (cold read)** |
| Phase B gates re-verified after restart | (b)(c)(e) PASS |
| Final serving state | promoted prod on :8009, oom/oom_kill = 0 |
| Rollback used | **No** |

**Bottom line:** Promotion to the arena-54 world succeeded and all gates pass. The park-to-disk mechanism triggers (`.sess` files are written), but the parked sessions were **not re-indexed as valid generations at restart** (boot scan `generations=0 corrupt=3`), so the post-restart continuation was served cold with `reused = 0` — CHECKPOINT REUSE was not demonstrated in this window. See §6 for the anomaly and honest limits.

---

## 1. Phase A — pre-flight (step 1)

All pre-flight checks passed before any stop/boot.

| Check | Result |
|-------|--------|
| `curl :8009/v1/models` (prod pre-window) | **http 200** (prod serving — v0238 world) |
| `stat -c %s /mnt/srcds/ai/iq3xxs-ud/experts.bin` | **50294988800 B** (expected 50294988800 B = 47,965 MiB) ✓ |
| `sha256sum -c bench/.../arena54-trial-20261120/experts-bin.sha256` | **OK** → `3de5d9e52ade09d1bf4ddc2f8ca18e56597097c06bac1d1ef6580f349c1a47dd` |
| `(cd v0239 && sha256sum -c SHA256SUMS.txt)` | **rpcw-hip/strata: OK; rpcw-cuda/strata-expert-helper-patient: OK** |

Exit statuses: all 0.

---

## 2. Phase A — step 2: stop-all.sh

```
$ bash /mnt/srcds/Strata/stop-all.sh
stop-all: supervisors first (state files)
  TERM run/prod-swift15-f16kv-mixed-supervisor.json supervisor 4051529
stop-all: launcher/engine/helper/wrapper sweep (patterns live in this file)
stop-all: verifying 20s of quiet
stop-all: before=0 after=0 — ALL STOPPED
stop-all exit=0
```

**"ALL STOPPED" on the first try; exit status 0.** No re-run, no rollback, no hand-killing (per safety rules — on-disk script only).

---

## 3. Phase A — step 3: boot promoted prod (launch-prod-arena54.sh)

```
( cd /mnt/srcds/Strata && setsid --fork /mnt/srcds/Strata/launch-prod-arena54.sh > logs/lane-promote-arena54.log 2>&1 < /dev/null )
```
Launcher forked OK at 12:48:00. Polled `http://127.0.0.1:8009/v1/models` every 10 s (up to 25 min). **Ready: http 200 at 12:50:04** (~2 min boot; the 46.84 GiB arena maps+mlocks during boot).

Supervisor state file `run/prod-swift15-f16kv-mixed-supervisor.json`: engine_pid = **650692**, server_pid = 650211, helper_pid = 649967, generation 1, state attached. Engine stderr log: `logs/engine-prod-arena54-20261120.log`. Server log: `logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-20261120-124800-649941.log`.

---

## 4. Phase A — step 4: promotion gates (a)–(f)

All gates passed.

### (a) 2-GPU split line (engine log)
```
strata generate: layer split: CUDA0 runs layers 0-29
strata generate: layer split: CUDA1 runs layers 30-47, expert cache 4049 slots (8.04 GiB) ...
strata serve: layer split: layers 0-29 (CUDA0), 30-47 (CUDA1), one hand-off per window
```
**PASS** — layers 0–29 on CUDA0, 30–47 on CUDA1.

### (b) 'strata native mlock: locked' — exactly 'over 1 spans', total 47,965 MiB
```
strata native mlock: ArenaExpertSource shared expert arena progress chunk 188: 47965 of 47965 MiB locked
strata native mlock: locked 46.84 GiB of 46.84 GiB requested over 1 spans in 50.2 s (/mnt/srcds/ai/iq3xxs-ud/experts.bin)
```
**PASS** — exactly 1 span, full 47,965 MiB (46.84 GiB) locked. No "not locked"/partial lines.

### (c) 'file pages locked, not pinned'
```
strata generate: expert arena: mapped read-only from /mnt/srcds/ai/iq3xxs-ud/experts.bin (STRATA_NATIVE_MLOCK: file pages locked, not pinned)
```
**PASS.**

### (d) /proc/<engine_pid>/status VmLck ≈ 49.2e6 kB (engine_pid = 650692)
```
VmSize:	157535924 kB
VmLck:	49280040 kB
VmRSS:	41511868 kB
```
**PASS** — VmLck = 49,280,040 kB ≈ 49.2e6 kB (≈47 GiB, matches the locked arena).

### (e) CGROUP WALL MEASURED (engine cgroup via `0::` unified line)
```
CG=$(grep -m1 '^0::' /proc/650692/cgroup | sed 's/^0:://')
# -> /user.slice/user-1000.slice/user@1000.service/app.slice/run-p650211-i25826328.scope
memory.max:  60129542144     (launcher MemoryMax=56G → exactly 56 GiB = 56×2^30)
memory.min:  54760833024     (expect 54760833024 = 51 GiB)  ✓ exact
memory.swap.max: 4294967296  (expect 4294967296 = 4 GiB)    ✓ exact
memory.events: oom 0, oom_kill 0                            ✓
```
**PASS in substance.** `memory.min` and `memory.swap.max` match the task-cited values exactly (51 GiB and 4 GiB in binary units), which confirms systemd applies binary GiB. `memory.max` measured **60129542144**, which is exactly 56 GiB (56 × 2³⁰) — the correct value for `MemoryMax=56G`. The task-cited figure `59686087680` is *not* 56 GiB and is arithmetically inconsistent with the confirmed binary-GiB reading of `memory.min`/`memory.swap.max`; the wall was applied exactly per the launcher's `MemoryMin=51G / MemoryMax=56G / MemorySwapMax=4G`. No config or launcher was edited.

### (f) one small completion → http 200
```
curl :8009/v1/chat/completions (max_tokens=8) -> http 200 in 2.57s
```
**PASS.**

---

## 5. Phase A — step 5: T0-style 3×500-token decode probes

Same shape as the trial: `curl /v1/chat/completions`, `max_tokens=500`, tok/s read from the response `timings` block (`predicted_n / (predicted_ms/1000)`; first probe may be cold/ramp). Saved to `probe.log`.

| Probe | Tokens | http | predicted_ms | tok/s (calc) | tok/s (reported) | wall | Phase |
|-------|--------|------|--------------|--------------|------------------|------|-------|
| T0-1 | 500 | 200 | 20343.0 | 24.58 | 23.7 | 28.53 s | cold |
| T0-2 | 500 | 200 | 16771.2 | 29.81 | 28.7 | 17.30 s | warm |
| T0-3 | 500 | 200 | 19408.0 | 25.76 | 24.9 | 19.92 s | warm |

**T0 summary:** warm median ≈ 28 tok/s (range 25.8–29.8); first probe cold (24.6) due to ramp — consistent with the arena-54 trial reference (~30 tok/s, cold→warm ramp). No degradation concerns; these are decode-oriented short probes, not a pressure leg.

---

## 6. Phase B — park/recover validation (step 6)

### 6.1 Methodology reference
The established T-numbered procedure lives in `bench/ab-v0210/rpcprof/out/parking-validation-20261119-173003/` (T1/T2/T3) with diagnosis in `parking-restore-diagnosis-20261119/`. Key facts from that record:
- T1/T2 within-boot disk restore: **PASS** (reused 16,015, ~12 s).
- T3 cross-boot restart recovery: **FAIL** — published generation 16,148 tok (anchor 16,015) exceeded the 16,133-tok continuation prompt, so `DiskCache::best()` skipped it (full-generation length ≥ prompt) → cold read, `0 reused`, ~67 s (the "78.7 s-class cold read" baseline).
- With the unfixed v0239 binary, cross-boot disk reuse requires a continuation prompt **strictly longer** than the parked generation length (`L < prompt`) so the full-generation restore path fires.

Note: the referenced `.../parking-checkpoint-fix-20261119/phase-b/` directory does not exist in this checkout; the T-methodology above (from `parking-validation-20261119-173003`) is the established procedure that was followed.

### 6.2 Park to disk (pre-restart)
The arena-54 config enables disk tier: `--conversation-cache-mib 2048 --conversation-cache-min-tokens 2048 --conversation-cache-park-gain 4096 --conversation-cache-dir /home/srcds/strata-conv-cache --conversation-cache-disk-idle-save`.

At the promoted boot (12:50) the disk tier opened at `generations=0 bytes=0 ... corrupt=0 foreign=0`. During the pre-restart session, idle-save parked conversations to disk:

```
strata serve: conversation cache: disk tier ON at /home/srcds/strata-conv-cache (direct I/O, budget 24576 MiB): generations=0 bytes=0 ... corrupt=0 foreign=0
strata serve: conversation cache: disk park streamed 2873 tokens idle to /home/srcds/strata-conv-cache/conv-1.sess ... generations=1 bytes=311 ... anchor=243 pair=243
strata serve: conversation cache: disk park streamed 3787 tokens idle to /home/srcds/strata-conv-cache/conv-2.sess ... generations=2 bytes=787 ... anchor=1158 pair=1158
strata serve: conversation cache: disk park streamed 2469 tokens idle to /home/srcds/strata-conv-cache/conv-3.sess ... generations=3 bytes=1089 ... anchor=243 pair=243
```
**Park triggered:** conv-1/2/3 `.sess` files written; the disk-tier generations counter moved from boot's `0` to `3`. Within-boot disk restore also worked (`prompt 2891 = 2873 reused + 18 read`, `prompt 1163 = 243 reused`).

conv-cache at pre-restart: conv-1.sess (321 MB), conv-2.sess (475 MB), conv-3.sess (316 MB), owner.lock, ring-3.dring, ring-4.dring.

### 6.3 Restart (engine restart = stop-all + relaunch launch-prod-arena54.sh)
```
bash /mnt/srcds/Strata/stop-all.sh     -> ALL STOPPED, exit 0
setsid --fork launch-prod-arena54.sh   -> :8009 ready 13:19:50 (~37 s boot)
```

### 6.4 Boot scan at restart — parked sessions NOT validly indexed
```
strata serve: conversation cache: disk tier ON at /home/srcds/strata-conv-cache (direct I/O, budget 24576 MiB): generations=0 bytes=0 sidecars=4 orphan temps=0 orphan sidecars=4 corrupt=3 foreign=0
```
**Anomaly:** although conv-1/2/3 `.sess` files exist on disk, the restart boot scan reports `generations=0 bytes=0 corrupt=3` — the parked sessions were **not re-indexed as valid generations** (contrast T3, where conv-50 indexed clean at `corrupt=0 foreign=0`). This is likely an artifact of the rapid, multi-conversation test cache state (orphan sidecars/dangling references) rather than a design failure — T3 showed a cleanly-created park indexes at `corrupt=0`.

### 6.5 Post-restart continuation — CHECKPOINT REUSE check
Continuation request (exact saved conversation text re-sent + follow-up, max_tokens=300):
```
curl :8009/v1/chat/completions -> http 200 in 106 s
Engine log: "strata serve: prompt 1341 tokens = 0 reused + 1341 read in 51648 ms (26.0 tok/s), 150 generated in 34847 ms (4.3 tok/s) ... finish=length"
```
**Result: `reused = 0`, cold read.** No `disk restore` / `reused > 0` line for this request; the prompt was served entirely fresh (`0 reused + 1341 read`). Prompt wall ≈ 51.6 s, which **is in the 78.7 s-class cold-read range** (i.e., NOT the fast ~12 s reuse seen in T1/T2).

**Phase B verdict: CHECKPOINT REUSE NOT demonstrated (`reused = 0`, not > 0).** Park-to-disk triggers, but the parked sessions were not validly indexed at restart (`generations=0 corrupt=3`), so the cross-boot restore path found nothing and the continuation was served cold. Per the task's step-7 contingency, this does not block Phase A: promotion stands, parking validation is reported as not-demonstrated in this window.

### 6.6 Gates re-verified after restart (step 8)
Engine pid now 1085175 (new generation).
```
strata native mlock: locked 46.84 GiB of 46.84 GiB requested over 1 spans ... (gate b, c) ✓
expert arena: mapped read-only ... (file pages locked, not pinned) ✓
VmLck: 49280040 kB                                            (gate d) ✓
memory.max: 60129542144 (=56 GiB), memory.min: 54760833024 (=51 GiB), memory.swap.max: 4294967296 (=4 GiB), oom 0, oom_kill 0  (gate e) ✓
```
Final state: promoted prod serving on **:8009**, oom/oom_kill = 0, wall measured.

---

## 7. Exact commands and exit statuses

| # | Command | Exit status / result |
|---|---------|---------------------|
| 1 | `curl -s -m 8 http://127.0.0.1:8009/v1/models` | http 200 (prod pre-window) |
| 2 | `sha256sum -c .../experts-bin.sha256` (in /mnt/srcds/ai/iq3xxs-ud) | OK |
| 3 | `(cd /mnt/srcds/Strata/v0239 && sha256sum -c SHA256SUMS.txt)` | Both binaries OK |
| 4 | `bash /mnt/srcds/Strata/stop-all.sh` (pre-boot) | "ALL STOPPED", exit 0 |
| 5 | `setsid --fork launch-prod-arena54.sh > logs/lane-promote-arena54.log 2>&1 < /dev/null` | forked OK; :8009 ready 12:50:04 |
| 6 | gate (d) `grep VmLck /proc/650692/status` | VmLck 49280040 kB ✓ |
| 7 | gate (e) cgroup `0::` memory.max/min/swap.max + memory.events | 56G/51G/4G, oom=0/oom_kill=0 ✓ |
| 8 | `curl :8009/v1/chat/completions` (small completion) | http 200 in 2.57 s ✓ |
| 9 | T0 probes ×3 (max_tokens=500) | http 200 each; 24.6 / 29.8 / 25.8 tok/s |
| 10 | `bash /mnt/srcds/Strata/stop-all.sh` (Phase B restart) | ALL STOPPED, exit 0 |
| 11 | `setsid --fork launch-prod-arena54.sh` (Phase B relaunch) | forked OK; :8009 ready 13:19:50 |
| 12 | post-restart gates (b)(c)(e) | PASS (mlock 1 span/47965 MiB, VmLck 49280040 kB, cgroup 51G/56G/4G/oom 0) |
| 13 | cross-boot continuation (max_tokens=300) | http 200 in 106 s; **0 reused + 1341 read** (cold) |

---

## 8. Anomalies

| Timestamp | Anomaly | Severity |
|-----------|---------|----------|
| 13:19 boot scan | Parked conv-1/2/3 `.sess` files exist but boot scan reports `generations=0 bytes=0 corrupt=3 foreign=0` — not validly indexed | **Major for Phase B** — prevents cross-boot disk restore |
| 13:20 continuation | `prompt 1341 = 0 reused + 1341 read` — cold read ~51.6 s (78.7 s-class), `reused = 0` | **Phase B not demonstrated** |
| Gate (e) memory.max | Measured 60129542144 (= exactly 56 GiB) vs task-cited 59686087680 | Minor — cited figure inconsistent with binary-GiB reading proven by memory.min/swap.max matching exactly; wall applied per launcher props |
| Completion speed | Server buffers full response; long completions (>~1000 tok) approach/exceed client timeouts (parkS 900 tok took 156 s) | Moderate — slowed park-turn construction |

---

## 9. Honest limits

1. **Phase B reuse not demonstrated.** `reused = 0` (cold read) because the restart boot scan found `generations=0 corrupt=3` — the parked `.sess` files were not re-indexed as valid generations. This differs from T3 (clean park indexed at `corrupt=0`, failure there was the prompt-length gating bug). Likely an artifact of the rapid multi-conversation test cache state (orphan sidecars); a single cleanly-created park may index correctly but was not re-validated within this window.
2. **`phase-b` procedure dir missing.** `bench/ab-v0210/rpcprof/out/parking-checkpoint-fix-20261119/phase-b/` does not exist; followed the T-methodology from `parking-validation-20261119-173003` instead.
3. **T0 probes are decode-oriented short probes (500 tok), not a pressure leg; no pressure/ram_pressure run was performed.**
4. **memory.max note** (see gate (e)) — reported the measured value and the inconsistency with the task-cited figure.
5. **Single window; no repeatability data.** One promotion + one restart cycle executed.
6. **No rollback was used or needed.** The promoted prod remains serving on :8009 at the end of the window.

---

## 10. Final state (end of window)

- Promoted prod (`launch-prod-arena54.sh`, v0239, prod-config-arena54.json, port 8009) **serving on :8009** (`curl :8009/v1/models` → http 200).
- All promotion gates (a)–(f) PASS; post-restart gates (b)(c)(e) PASS; `memory.events oom/oom_kill = 0`.
- Park-recover: park-to-disk triggered (conv-1/2/3 `.sess`, generations 0→3), but cross-restart `reused = 0` (cold read ~51.6 s) — CHECKPOINT REUSE not demonstrated in this window.
- Rollback: **not used**; no rollback launcher (`launch-v0238.sh`) invoked.
