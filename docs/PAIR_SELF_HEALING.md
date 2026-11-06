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
- All six launchers start it with `STRATA_HELPER_REPLY_TIMEOUT_MS=180000`
  (3 min, twice the observed 90 s stall).
- The Swift 1.5 config also sets `STRATA_PREFILL_HELPER_TIMEOUT_MS=180000` (the
  engine's prompt-row read bound; hardcoded default 10000) and raises
  `expert_helper_timeout` 2000 -> 10000 (the per-window bound - a late helper is
  a CPU window, never a stall).
- Effect: a transient engine stall up to 3 minutes no longer closes a channel,
  triggers the CPU fallback, or costs the KV; a real peer death is still
  detected instantly and the supervisor rebuilds the pair.
- Verified GPU-free: the override line prints only when set, is silent when
  unset; `expert_helper_socket_test` passes; the 27-test `tests/mixed_gpu` suite
  passes; the config JSON validates. Not yet verified on the live GPU pair.

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
