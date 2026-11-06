# Local mixed-GPU pair recovery

The six local `launch-prod-*-mixed*.sh` launchers now use the tracked
`tools/mixed_gpu/pair_supervisor.py` by default. `--recover` is an alias for
the same behavior. `--single-run` explicitly disables supervision for diagnostics.
The frozen `v0210` source, GPU binaries, and model configurations are unchanged.

## What recovery means

A helper process or listening socket is not a working GPU tier. With a model
configuration supplied, the supervisor marks the pair `attached` only after:

- The owned server and configured engine process are alive.
- The helper reports a **positive** expert fill and the second-channel connection
  (for a two-socket launcher).
- This generation's engine log acknowledges a **positive** expert-helper proxy.

Engine, server, or helper death; helper-channel failure; logged helper absence,
claimed-row CPU fallback, or engine-side channel closure trigger a full owned
pair teardown and reload. An unfilled helper also reaches a startup deadline
and is retried. Recovery interrupts the failed request: callers must retry it.
There is no engine hot-attach implementation in this change.

Restarts use exponential backoff, initially 2 seconds and capped at 30 seconds.
An attached generation that lasted at least 300 seconds resets the failure count.
The startup deadline is 360 seconds; graceful teardown gets 15 seconds before
escalation. Cleanup must finish before another generation starts.

All six launchers share `run/mixed-supervisor.lock`, since all use port 8009.
Cleanup checks PID start tokens and removes only the same owned socket inodes
observed during that generation. Pre-existing ports or socket paths are refused,
not stolen. The dedicated Linux supervisor is a child subreaper, so children
orphaned during startup or shutdown remain owned and are reaped before a restart.
Launchers publish the helper before waiting for the server so an
early helper death can be retried without treating configuration refusals as
retryable failures. Recovered peers inherit a nice increment of +1, preserving
the approved slight loadend-inference CPU priority advantage.

## Observing the pair

Each launcher keeps its existing `run/*-supervisor.json` state path. States are
`starting`, `monitoring`, `attached`, `recovering`, `backoff`, `stopping`,
`stopped`, and `failed`. The record includes generation, peer PID/start tokens,
log paths, and the most recent restart reason. `engine_stderr_log` is the
configured engine log; the older `engine_log` field names the server wrapper log.

Only newly written complete engine-log lines count as this generation's evidence.
Historical failure and success lines do not establish current health. Log
replacement and truncation reset the tail. Helper logs are unique per launch.
This is process/log supervision, not a GPU-memory or kernel-health probe. A hung
but silent engine remains subject to the existing engine/server watchdogs.
`/health` being loaded/ok does **not** prove helper attachment.

## Deployment boundary

Editing the launcher does not supervise an already running unsupervised engine.
A CPU-fallback engine cannot attach a newly started helper. Deploying
this recovery requires a separately approved restart of the current pair, using
the **same model launcher/configuration**, then verifying attachment, helper VRAM
fill, and a real completion. Do not run a cutover merely to check these edits.

The XXS cutover driver's persistent-helper test deliberately uses `--single-run`
in Stage B. Stage C replaces it with a fresh default-supervised pair and checks
the supervisor's attachment state before declaring success. It is a disruptive
model-cutover tool, not the deployment command for the current Swift 1.5 model.

### Activated local service (2026-11-06)

The approved Swift 1.5 FP16-KV deployment runs in the user unit
`strata-mixed-pair.service`, whose command is the same Swift 1.5 launcher with
default supervision. Check it with:

```sh
systemctl --user status strata-mixed-pair.service
```

The service owns the whole pair's cgroup, including the engine's separate
session. It restarts the controller on failure after 2 seconds, with a limit of
3 starts per 60 seconds. `KillMode=mixed` gives the supervisor its cleanup window;
systemd's stop timeout is 30 seconds. Service output appends to
`logs/prod-pair-supervised.log`; peer logs and state retain their launcher paths.
Any authorized manual stop/restart must operate on this **whole unit**, not an
independent helper service. Do not use the older IQ3_S relaunch script to restart
this model. This is a transient user service, not a boot-enabled installation.

At activation, the helper filled 4,324 slots (6.61 GiB of expert weights), NVIDIA
reported 7,026 MiB allocated to it, and the engine acknowledged the same proxy.
Real API requests completed on the pair. These are deployment measurements on
the local RTX 3080 helper / Radeon VII engine PC, not performance comparisons.

### Patient helper replies (2026-11-06 evening)

The Nov 6 17:47 event: the engine's prompt lane froze ~90 s in a reclaim-driven
HIP queue suspension (amdgpu XDMA no-retry faults; the box was swapping ~100 MB/s
both ways), the helper's hardcoded 10 s kRowsP reply-write deadline closed the
second channel ("helper timeout"), the engine fell back to CPU rows for the
request, and the supervisor rebuilt the pair 3 times with all KV lost.

- `v0210/build-mixed-gpu/rpcw-cuda/strata-expert-helper-patient` is built from
  the same source plus one change (worktree branch `fix/helper-reply-patience`,
  commit `71ee8bd`): `STRATA_HELPER_REPLY_TIMEOUT_MS` (>= 100) overrides every
  DATA reply's write deadline (kRows, kRowsP, kDraftAck, kDraftReply, kHelloAck).
  Unset = the per-call defaults, byte for byte. A dead peer is POLLHUP and is
  detected at once at any deadline. One line in the helper log names the active
  override at startup. The original helper binary is untouched next to it.
- All six launchers start it with `STRATA_HELPER_REPLY_TIMEOUT_MS=600000`,
  matching the engine-side patience the configs already carried.
- The engine's prompt-row read bound `STRATA_PREFILL_HELPER_TIMEOUT_MS` was
  ALREADY set in every prod config (600000 here and in the other f16kv/ud
  configs, 120000 in the two dflash ones) - the missing half was always the
  helper's hardcoded write deadline. All seven configs also raise
  `expert_helper_timeout` 2000 -> 10000 (the per-window bound - a late helper
  is a CPU window, never a stall).
- Effect: a transient engine stall up to 10 minutes no longer closes a channel,
  triggers the CPU fallback, or costs the KV; a real peer death is still
  detected instantly and the supervisor rebuilds the pair.
- Verified GPU-free: the override line prints only when set, is silent when
  unset; `expert_helper_socket_test` passes; the 27-test `tests/mixed_gpu` suite
  passes; the config JSON validates. Verified live on Nov 6 evening: through a
  ~7-minute mid-request GPU wedge (19:54-20:01) the helper channel stayed open,
  no CPU-fallback line appeared, and the pair held `attached` generation 1. The
  restart that finally came was the server's own STOP watchdog (`engine_silence_s`
  300) after the client canceled - about 12 minutes end to end. That slowness is
  the next problem, not the helper's.

### Stage-thread affinity at attach (2026-11-06 evening)

The engine pins its `strata-stage` threads to CPU 0 at every start, and the hostapp
shares that core (66% host wait was measured on Oct 27 while background-load). A runtime
move survives only until the next engine start, and the supervisor now restarts
the engine on every recovery - so the move had to become part of startup.

- `pair_supervisor.py run` gains an opt-in `--stage-cpus CPUS`. After the pair
  turns `attached`, and again for every later generation, the supervisor runs
  the existing `staging_affinity.py apply` for the discovered engine PID: only
  threads named `strata-stage` whose mask is exactly CPU 0 move, snapshot-first,
  exactly the approved trial from the coexistence journal.
- Every application writes a rollback snapshot beside the state file
  (`run/<state-stem>-affinity-<engine-pid>-<start>.json`); roll back with
  `staging_affinity.py restore --snapshot <file>`. Snapshots accumulate one per
  engine incarnation.
- Results land in the state JSON under `stage_affinity` (`applied`/`failed`,
  moved count, snapshot path). A failure never restarts the pair by itself; the
  supervisor tries for 30 monitor intervals, records the outcome, and moves on.
  Both startup races are retried, not failed: the engine renames itself
  `strata-host` only after early serve startup (first seen live on Nov 6 night:
  attach fired while comm was still `strata`), and the staging threads can spawn
  late. The fixture reproduces the rename delay.
- All six supervised launchers pass `--stage-cpus 3,5,7,9,11,13,15`. Without the
  flag the supervisor is byte-for-byte the old behavior. `--single-run` (the
  diagnostic path, no supervisor) does not apply affinity.
- Verified by the fixture tests: the fake engine now names itself `strata-host`
  and carries a CPU-0 `strata-stage` thread; the suite (29 tests) checks the
  move at attach, the fresh move after an engine kill, and the CPU-0 refusal.

### Stalled-request recovery (2026-11-06 night)

The Nov 6 evening wedge (19:54): mid-read of a 189k-token prompt the GPU
wedged - the read counter froze at 64,773 of 188,981, the server's 10 s ticker
printed the same count for ~50 s and then went silent, both compute GPUs sat at
0% utilization, and every process stayed alive with the helper channel open.
Nothing fired: no death, no channel close, no degradation line. The patient
helper held the pair `attached` for the whole wedge (its first live pass). What
finally recovered was the server's own STOP watchdog (`engine_silence_s` 300) -
but only after the client canceled, ~12 minutes end to end, and the request was
dead regardless.

- `pair_supervisor.py run --stall-timeout SECONDS` (opt-in, 0 = old behavior;
  all six launchers pass 300) closes that blind spot. The server's read ticker
  (`[strata] reading the prompt: N of M tokens`) is the only per-request
  heartbeat: the tracker starts on the first tick, advances only when the
  N-of-M pair CHANGES, and clears when the engine log reports the request done
  (`strata serve: prompt ... tokens`). Idle (no request in flight) has no
  tracker and can never fire.
- Attached + no changed tick for 300 s -> the reason is
  `prompt read stalled at N of M tokens ...` and the normal generation restart
  follows (SIGTERM, escalation to SIGKILL for a wedged engine, backoff, refill).
- Why 300 s and not sooner: host contention (swap, a hostapp loading) can stall a
  read for minutes and then auto-recover - an early restart would throw away a
  pair that was about to heal. Five minutes of frozen heartbeat separates the
  two; it also beats the ~12 min client-dependent server path.
- Limits: this covers the read phase, the one phase with a heartbeat. A wedge
  during decode (no per-request heartbeat in either log) is not detected; the
  server's `engine_silence_s` remains the backstop there. GPU utilization is
  deliberately not consulted. `--single-run` runs without it.
- Verified by three fixture tests: a frozen ticker restarts with the reason; a
  completed request clears the tracker (idle never restarts, a later new stall
  still does); a progressing ticker never restarts. Suite is 32 tests.

## CPU-only regression tests

```sh
.venv/bin/python -m unittest discover -s tests/mixed_gpu -v
```

Tests use standard-library subprocesses and Unix sockets under `logs/pair-test-*`.
They never start a GPU binary, stop production, or bind port 8009. Launcher tests
intercept `exec`, force an occupied-port refusal, and disable KSM before invoking
the real scripts. These tests establish supervisor decisions and ownership
cleanup, not successful model reload, GPU correctness, or inference performance.
Each fixture teardown audits for surviving fixture roles and socket files. A
read-only global leftover check is also available:

```sh
.venv/bin/python tests/mixed_gpu/fixtures/mixed_pair_fake.py --check
```
