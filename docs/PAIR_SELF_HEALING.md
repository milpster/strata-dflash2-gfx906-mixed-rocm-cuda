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
The current CPU-fallback engine cannot attach a newly started helper. Deploying
this recovery requires a separately approved restart of the current pair, using
the **same model launcher/configuration**, then verifying attachment, helper VRAM
fill, and a real completion. Do not run a cutover merely to check these edits.

The XXS cutover driver's persistent-helper test deliberately uses `--single-run`
in Stage B. Stage C replaces it with a fresh default-supervised pair and checks
the supervisor's attachment state before declaring success. It is a disruptive
model-cutover tool, not the deployment command for the current Swift 1.5 model.

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
