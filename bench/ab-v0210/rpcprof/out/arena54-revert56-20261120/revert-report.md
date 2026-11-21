# Revert-restart report — arena54 56G/4G (GUAM, 2x Radeon VII gfx906 + RTX 3080 helper)

Date: 2026-11-20. Box: GUAM (post-reboot swap thrash; system swap ~9.8G used pre-flight). Mechanics only — no file edits, no git ops.

## Pre-flight
- `sha256sum launch-prod-arena54.sh` = `9c5cff8387442e67d6b96da322cc6d558be2764f2cfca36115531eb88bd41404` MATCH (expect: same)
- `sha256sum prod-config-arena54.json` = `c4a6bb3f391e514c05b9b0eeb5fb6484dafbe7c06639a57942b8f5f084bafd55` MATCH (expect: same; conv tier 1024 stays)
- `free -m`: Mem total 63644 used 55017 free 2147 avail 8626; Swap total 196607 used 9789
- `swapon --show`: /mnt/swap2 32G/3.1G pri2; /mnt/swap4 32G/3.3G pri2; /mnt/swap5 32G/3.2G pri2; /home/swap 32G/0B pri1; /home/swap4 32G/0B pri1; /home/swap3 32G/0B pri1
- `curl -s -m 5 http://127.0.0.1:8009/health` = `{"status":"ok",...,"loaded":true,"service":"strata"}` HTTP 200 exit 0 (engine serving, gen 2 mid-boot per briefing)

## Step 2 — stop-all.sh
Command: `bash /mnt/srcds/Strata/stop-all.sh`
Output:
```
stop-all: supervisors first (state files)
  TERM run/prod-swift15-f16kv-mixed-supervisor.json supervisor 54708
stop-all: launcher/engine/helper/wrapper sweep (patterns live in this file)
stop-all: verifying 20s of quiet
stop-all: before=0 after=0 — ALL STOPPED
```
Exit status: 0. Requirement `ALL STOPPED` + exit 0: MET.

## Step 3 — boot
Command: `setsid --fork bash /mnt/srcds/Strata/launch-prod-arena54.sh` (from /mnt/srcds/Strata), separate invocation.
Launcher log line: `Starting mixed backend on 127.0.0.1:8009; log: ...-141116-250800.log; helper: ...-141116-250800-helper.log`
Poll `curl -s -m 5 http://127.0.0.1:8009/v1/models` until HTTP 200:
- 14:11:16–14:12:52: 000 (retrying every 5 s)
- 14:12:57: 200
Boot wall time: 101.32 s (BOOT_START 1795180276.254 → BOOT_END 1795180377.575). Within 10 min cap. No speed claimed.

## Supervisor state (fresh boot)
`run/prod-swift15-f16kv-mixed-supervisor.json`:
```
{"supervisor_pid":250792,"launcher_pid":250800,"generation":1,"state":"attached",
 "helper_pid":250806,"server_pid":251055,
 "engine_log":".../prod-swift15-f16kv-dflash-w4-q4_0-mixed-20261120-141116-250800.log",
 "helper_log":"...-250800-helper.log","engine_stderr_log":"logs/engine-prod-arena54-20261120.log",
 "engine_pid":251533,"engine_start":"108592","attached_at":1795180353.3025832,
 "stage_affinity":{"state":"applied","moved":0,"cpus":"3,5,7,9,11,13,15"}}
```
engine_pid = 251533 (`strata-host`, STAT SNLl, PPID 251055).

## Gate table

| # | Gate | Expected | Observed | Verdict |
|---|------|----------|----------|---------|
| version | engine exe | v0239 | `/mnt/srcds/Strata/v0239/rpcw-hip/strata` (fresh boot `[strata] 2026-11-20 14:11:18 engine started: ... v0239 ...`) | PASS |
| (a) | 2-GPU split | `layer split across 2 GPUs`; `CUDA0 runs layers 0-29`; `CUDA1 runs layers 30-47` | Fresh boot section (from line 773 `14:11:18 engine started`): `strata generate: layer split across 2 GPUs: CUDA0, then CUDA1 (split 30)`; `strata generate: layer split: CUDA0 runs layers 0-29`; `strata generate: layer split: CUDA1 runs layers 30-47, expert cache 4049 slots (8.04 GiB)... slot 0 verified` | PASS |
| (b) | mlock | `over 1 spans`; `locked 46.84 GiB of 46.84 GiB requested`; `47965 of 47965 MiB`; `file pages locked, not pinned` | Fresh boot: `strata native mlock: locked 46.84 GiB of 46.84 GiB requested over 1 spans in 50.2 s (/mnt/srcds/ai/iq3xxs-ud/experts.bin)`; `... ArenaExpertSource shared expert arena progress chunk 188: 47965 of 47965 MiB locked`; `strata generate: expert arena: mapped read-only from .../experts.bin (STRATA_NATIVE_MLOCK: file pages locked, not pinned)` | PASS |
| (c) | /proc/pid/status | engine_pid from state file; VmLck ≈ 49,280,040 kB (accept 49.0–49.4e6); record VmRSS | PID 251533 alive. `VmLck: 49280040 kB` (exact match); `VmRSS: 52028600 kB`; `VmSize: 157534520 kB`; `VmHWM: 52462540 kB` | PASS |
| (d) | conv cache | RAM tier / disk tier ON, budget 24576; record disk-tier scan counters line verbatim | `strata serve: conversation cache: RAM ceiling 256 MiB ...`; `strata serve: conversation cache: disk tier ON at /home/srcds/strata-conv-cache (direct I/O, budget 24576 MiB): generations=0 bytes=0 sidecars=0 orphan temps=0 orphan sidecars=0 corrupt=0 foreign=0` (verbatim fresh-boot disk-tier scan counters line) | PASS |
| (e) | cgroup scope | `grep '^0::' /proc/251533/cgroup` → `0::/user.slice/user-1000.slice/user@1000.service/app.slice/run-p251055-i224210.scope`; memory.max=60129542144; memory.min=54760833024; memory.swap.max=4294967296; record memory.current (≥~48 GiB hint; re-read once if low) and memory.swap.current (<4 GiB) | Scope: `0::/user.slice/user-1000.slice/user@1000.service/app.slice/run-p251055-i224210.scope`. memory.max = `60129542144` (exact = 56×2^30). memory.min = `54760833024` (exact). memory.swap.max = `4294967296` (exact = 4×2^30). memory.current read#1 = `48334159872` (~45.0 GiB); read#2 after 60 s = `47534514176` (~44.28 GiB) — below ~48 GiB hint, both reads recorded. memory.swap.current read#1 = `2207723520` (~2.06 GiB); read#2 after 60 s = `3431325696` (~3.19 GiB) — well under 4 GiB max, rising, recorded. | PASS (exact max/min/swap.max; current+swap.current recorded both reads) |
| (f) | memory.events | oom=0, oom_kill=0; record sock_throttled if nonzero | `low 0 high 0 max 0 oom 0 oom_kill 0 oom_group_kill 0 sock_throttled 19` | PASS (oom/oom_kill 0; sock_throttled=19 recorded) |
| (g) | liveness | one small completion max_tokens ~32 → HTTP 200; wall time recorded (liveness only, not a speed datapoint) | `POST /v1/chat/completions {"model":"ud-iq3_xxs",...,"max_tokens":32}` → HTTP 200; live wall 15.25 s; 16 completion tokens (finish_reason length). Not reported as speed. | PASS |

## Cgroup numbers vs expected
| file | expected | observed | match |
|------|----------|----------|-------|
| memory.max | 60129542144 | 60129542144 | exact |
| memory.min | 54760833024 | 54760833024 | exact |
| memory.swap.max | 4294967296 | 4294967296 | exact |
| memory.current | ≥~48 GiB hint | 48334159872 (r1) / 47534514176 (r2, +60s) | below hint; both recorded |
| memory.swap.current | < 4 GiB | 2207723520 (r1) / 3431325696 (r2) | under max; rising |

## Anomalies
- memory.current (~44.3–45.0 GiB) is below the ≥~48 GiB post-lock hint and below memory.min (51 GiB); VmLck (47 GiB locked) exceeds memory.current — cross-metric inconsistency, recorded, no OOM.
- memory.swap.current rose from ~2.06 GiB to ~3.19 GiB between the two reads (still under the 4 GiB max); engine not re-swapping to the cap but trending up.
- sock_throttled = 19 (nonzero) while oom/oom_kill = 0.
- Box recovering from swap thrash; first responses slow (boot 101 s; health/curl sluggish) — recorded, not diagnosed.

## Gen-1 death lines (~14:05)
Source: `logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-20261120-135732-54731.log` (gen-1 wrapper log), `tr -d '\0'`, tail ~40 + abort/exit/stall/timeout grep. Verbatim:
```
[strata] reading the prompt: 56 tokens, 47 s so far
[strata] reading the prompt: 56 tokens, 57 s so far
[strata] done: 0 tokens in 72 s (no speed) (cancel, cancel=True)
[strata] thinking capped at 16 of max_tokens 32 to leave room for the answer (budget 55000)
[strata] reading the prompt: 56 tokens, 10 s so far
[strata] reading the prompt: 56 tokens, 20 s so far
[strata] reading the prompt: 56 tokens, 30 s so far
[strata] reading the prompt: 56 tokens, 40 s so far
[strata] the engine stopped unexpectedly (exit code 1). Unrecoverable native verification failure: verify: timed out at layer 0; its GPU waits were released but the GPU did not finish within 5 s (#267) The next request starts the engine again. Its log: /mnt/srcds/Strata/logs/engine-prod-arena54-20261120.log
[strata] done: 0 tokens in 47 s (no speed) (error, cancel=False)
[strata] stopped
Traceback (most recent call last):
  File "/mnt/srcds/Strata/v0215/serve/server.py", line 10637, in _main
    time.sleep(1)
    ...
  File "/mnt/srcds/Strata/v0215/serve/lifeline.py", line 327, in handler
    request_stop(reason)
  ...
KeyboardInterrupt
```
Key death line: `the engine stopped unexpectedly (exit code 1). Unrecoverable native verification failure: verify: timed out at layer 0; its GPU waits were released but the GPU did not finish within 5 s (#267)`. No further analysis per instructions.

## Final state
Prod serving under 56G/4G cgroup on :8009. All gates (a)–(g) PASS. No rollback performed (no failure). Report only — no files changed, no git ops.
