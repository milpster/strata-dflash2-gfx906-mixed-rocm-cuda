# Persistent slowdown after the iGPU game exits — 2026-11-18

## Scope and state

The operator reported that Strata slowed while the iGPU game ran and did not recover after the game closed. The original game's launch/exit times are unknown. The passive investigator was cancelled before its report was written when the operator requested a backend restart. Parent then captured passive evidence and one explicitly authorized, synthetic generation before stopping production. No candidate deployment, cache clearing, swap policy change or injected memory pressure occurred.

Evidence root: `bench/ab-v0210/rpcprof/out/game-exit-debug-20261118-163135-1526550/`. Times below are local CET (`+01:00`). The original slow process has been stopped. Canonical v0238 generation 2 is verified serving, passive collectors are armed, and the operator has been told the next game may launch. Performance recovery is **not** established.

## Captured evidence

| Observation | Evidence / interpretation |
| --- | --- |
| Original live engine PID 10668, started 14:55:02 | `/proc/10668/exe` resolved to `v0238p0/rpcw-hip/strata`; SHA256 `b0ec0109ebc50c982512c371d81b2c15a8f72c70edf8a461a4ff10337da52f02`. `pre-request-processes.log`, supervisor JSON. The RAM-fix candidate was not running. |
| Engine `VmSwap: 3444280 kB` at 16:33:02 | About 3.28 GiB attributable to this process, not merely host-wide historical swap allocation. Engine RSS was `51130448 kB`. `pre-request-processes.log`. |
| 16:37:17 synthetic counting request: 57 prompt tokens, 256 generated | HTTP 200, curl exit 0, `finish_reason=length`; 44.923 s total. Engine reports 7.436 s prompt time and 37.475 s generation, **6.8 generated tokens/s**. `probe-request.json`, `probe-response.sse`, `probe-engine-window.log`. |
| Active paging during that request | `probe-vmstat.log` has interval samples, not just since-boot counters: up to `287948` KiB/s swap-in and `539424` KiB/s swap-out. Sampling covered approximately the first 29 s of the roughly 45 s request. These are host-wide rates; per-process attribution of each I/O burst is not established. |
| Engine swap remained/increased after request | At 16:38:02, engine `VmSwap: 3686012 kB` (about 3.52 GiB), RSS `51596568 kB`; host `MemAvailable` was 13709 MiB. `probe-result.log`. Apparent available memory did not imply a paging-free request. |
| Other captured state | Process status/stat/I/O/smaps, engine cgroup accounting, device clocks/busy/VRAM, NVIDIA helper telemetry, CPU frequencies and governors, recent NUL-stripped engine tail and exact request-log offsets. See the named pre-request and load-snapshot files. PSI pressure files are unavailable, not zero. GPU clocks are isolated snapshots, not a continuous time series. |
| Engine cache activity in the request window | Previous 24309-token conversation parked to disk; log reports an 822 MiB snapshot exceeding the 512 MiB retained-RAM ceiling. This establishes an operation in the window, not that parking or any reported advisory release caused the subsequent swapping. |
| Engine stopped at operator request | `restart-stop.log`: stop began 16:38:31; supervisor-aware stop completed with exit 0 and `ALL STOPPED` after the quiet gate. The operator also observed host swap usage fall after termination; no matching post-stop swap sample was captured before that observation. |

`probe-response.sse`'s curl `time_starttransfer=0.010519` measures arrival of HTTP/SSE output, **not first generated token**. Do not present it as generation TTFT. The tiny synthetic counting fixture is not comparable to earlier long-context champion throughput figures. No quality-equivalence or general throughput claim follows.

## Interpretation and limits

The engine demonstrably owned swapped anonymous memory, and the controlled request overlapped substantial ongoing host paging. That supports a paging/refault contribution to the slow state. It does not identify which engine allocation was swapped, prove that all paging came from Strata, or establish why the state persisted after the game exited.

Linux need not eagerly page all anonymous memory back in when another application exits. `MemAvailable` also includes reclaimable file cache. Those facts explain why nonzero swap and apparent available RAM can coexist; they do not alone explain the observed sustained under-load swap churn. The running engine's cgroup, file-backed working set, memory guard and competing processes still need time-correlated evidence. No diagnosis of a stuck GPU clock, memory leak, cap defect or cache bug is established.

Read-only policy check during the later restore: `vm.swappiness=100`, `vm.watermark_scale_factor=100`, `vm.watermark_boost_factor=15000`, `vm.min_free_kbytes=524288`. No value was changed, and this later observation is not proof of the original event's cause.

## Commands and ownership

- Passive snapshots used `free -m`, `vmstat -w 1 4`, `ps`, selected `/proc/<pid>/{status,stat,io,smaps_rollup,cgroup}`, cgroup files, sysfs clocks/power/busy state, bounded `nvidia-smi`, and SHA256 checks. Top-level snapshot commands exited 0; individual optional sysfs/tool failures, if any, remain in their raw logs rather than being treated as successful coverage.
- Request: `curl --silent --show-error --no-buffer --connect-timeout 10 --max-time 90 --dump-header "$OUT/probe-response.headers" http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' --data-binary "@$OUT/probe-request.json" --write-out '\nprobe_http_code=%{http_code} time_starttransfer=%{time_starttransfer} time_total=%{time_total}\n'`; curl exited 0. Concurrent `vmstat -w 1 30` and a five-second-delayed process/device snapshot completed; raw logs retain their output. Engine log offsets were 4096531 through 4097815.
- Stop: `bash bench/ab-v0210/rpcprof/gpu-rounds/champions_stop.sh`; exit 0 and `ALL STOPPED`.
- Restore: both canonical config and engine hashes were checked before invoking `bash bench/ab-v0210/rpcprof/gpu-rounds/champions_restore.sh --health --timeout 1200`. The supervisor retried generation 1 after a helper-channel close; **the helper-channel close is not a root-cause diagnosis**. The legacy readiness checker selected the first generation's log and remains waiting on that dead generation. Its eventual exit does not determine whether generation 2 serves. This script uses `launch-v0238.sh`, not a candidate launcher. New restore log files are added under the champion output directory by that existing script; frozen measurement files are not rewritten.

## Verified restoration and next-game capture

Parent independently followed the supervisor's current generation rather than the stale checker. Verification command exited 0: fresh `ready:` in the generation-2 wrapper log; `/proc/1707851/exe` resolves to the canonical v0238 binary and passes the pinned SHA256; `/v1/models` returns a nonempty model list; and an actual completion finishes with nonzero generated tokens. Evidence: `post-restart-supervisor.json`, `post-restart-identity.log`, `post-restart-models.json`, `post-restart-response.sse` and `post-restart-probe-summary.json`. The failed startup, current wrapper/helper logs and NUL-stripped restart engine window are copied into the evidence root; the old checker is not used as an acceptance verdict.

| Same synthetic request after restart, 16:47:42–16:48:14 | Result |
| --- | --- |
| Prompt / generated / reused tokens | 57 / 256 / 0, identical request bytes to the pre-restart probe |
| Generation / total wall time | 30.108 s / 32.420 s; **8.5 generated tokens/s**, still slow |
| Engine `VmSwap` immediately after | `186072 kB`, about 182 MiB, down markedly from the old process's roughly 3.52 GiB |
| Engine RSS / host available RAM immediately after | `41145088 kB` / 19516 MiB |
| Confounds / warning | Fresh-process cache/residency and graph-capture state differ. Engine logs warn of only 220 MiB free on one engine GPU. This is evidence to investigate, not permission to change precision, context, reserve or another production setting. |

Lower engine swap did **not** by itself restore normal throughput. This weakens a swap-allocation-only explanation; working-set refaults, device pressure and other remaining mechanisms are not ruled out by this one small fixture.

At 16:46:58, parent launched three bounded, live-streamed collectors using existing Linux tools: `vmstat -w -t 2 1801`; `LC_ALL=C S_TIME_FORMAT=ISO pidstat -h -r -u -d -p ALL -C 'strata-host|strata-expert-h' 2 1800`; and an explicit-path, NUL-stripped engine-log tail under `timeout 3600`. First output was inspected: host samples are timestamped and pidstat includes both current engine/helper PIDs. Collectors expire around **17:46:58 CET**; they do not silently run forever. Commands, shell handles, evidence paths and limits are in `capture-manifest.json`. No new monitor framework or source code was added.

The capture-ready snapshot was taken at 16:50:08; its command exited 1 after retaining process/cgroup data because `/proc/pressure/memory` does not exist. A narrow continuation exited 0 at 16:50:32, explicitly recording unavailable PSI, copying restart evidence and writing the capture-ready marker. Do not report pressure coverage from that failed optional read. `memory.high` and `memory.max` at the engine's immediate cgroup are both `max`; this does not establish all ancestor limits. `capture-ready-system.log`, `capture-ready-availability.log` and `capture-ready.log` preserve these facts.

Growing next-game streams and the stale checker's future result are excluded from the static evidence seal and retained locally. Operator-supplied game launch/exit markers and phase-boundary process/cgroup/device snapshots remain to be collected. Message receipt is not an invented exact game launch/exit time. No specialist is running during this capture.

Parent owns process operations and diagnostic requests. No specialist is authorized to stop or modify production. Unrelated GPU RAM-acceptance testing remains paused. Next: collect timestamped paging/fault data and bounded matching probes across an operator-marked game launch and exit, with explicit phase snapshots and no memory-policy change. Resume investigation only after reconciling that evidence; do not treat the user-authorized restart as a deployment or a proven fix.

## Operator deferral and open-work resumption

At the game-start confirmation, operator replied: “park for later, resume an agent to work on our open things”. The game experiment and its follow-up investigation are now **parked**. No launch/exit event was confirmed, so the existing collector window must not be labelled a before/during/after game experiment. Bounded passive collectors may naturally finish around 17:46:58 CET; later samples can include specialist inference and are not clean game-performance evidence. A future game run needs fresh phase markers and collector deadlines.

The original restore checker finally exited 1 at 17:02:23, specifically because the failed generation-1 wrapper log never became ready. Parent did not restart again in response. At 17:09:17, a fresh verification command exited 0: current PID 1707851 matches the canonical engine SHA, its current wrapper has readiness, and an actual completion returns `OK`, 2 tokens, `finish_reason=stop`. Evidence: `game-parked-pre-agent.log`, `pre-agent-response.json`, `game-experiment-parked.json` and the separate parking evidence seal. This establishes serving availability, not restored normal speed.

Parent resumed exactly one specialist, the previous harness explorer `ses_e11015949ffeBT4ejQcEbCrLxy`, on the open RAM-fix GPU acceptance preparation. It owns only `gpu-rounds/ram-parking-gpu-fixtures.md`: concrete source-backed pressure fixtures, matched manifest/cap/floor requirements, deepest-published-boundary assertions, minimal missing glue and CPU checks. It may also describe the now-evidenced generation-aware restore-check requirement. No endpoints, GPU work, pressure, process operations, source/config edits or deployment are authorized. Production stays up for its inference; parent owns reconciliation and any eventual GPU window.
