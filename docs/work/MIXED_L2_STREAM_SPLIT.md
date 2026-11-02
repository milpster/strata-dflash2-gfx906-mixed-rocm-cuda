# L2: per-channel CUDA streams in the helper — measured, no gain

The helper served both channels (window/draft and prompt) under one device lock
(`gpu_mtx`) and one CUDA stream, so a frame on one channel queued behind the other
channel's in-flight frame even though the two tiers own separate staging buffers.
L2 split that: with `STRATA_HELPER_STREAMS=1` the prompt tier gets its own stream
and the lock becomes a reader/writer gate — the fill epoch is the only writer,
every served frame is a reader, so window/draft and prompt frames overlap on the
card. The env is opt-in and unset everywhere by default: with it unset, every code
path is exactly the old single-mutex, single-stream behaviour (commit `c8c46e14`;
harness arms `d8f41117`).

## What was measured

Window `l2w1` on 2026-11-02, 19:10–19:21 (`tools/mixed_gpu/slots_window.py
--l2-streams`): arms OFF/ON/OFF at the canonical 4200 slots / fill 6400 MiB, every
arm on the freshly built `rpcw-cuda-l2` helper (ctest `-R helper` 5/5; the HIP
engine binary byte-reused — no HIP source changed), only arm b's launcher
injecting `STRATA_HELPER_STREAMS=1`. Three matched long probes per arm; the
engine, prompts and request bytes identical across arms (request-SHA parity).

All three arms filled identically (4093 slots, 6.25 GiB) and served the prompt
channel identically (192 requests, 45,424 rows each). Zero helper fallbacks and
zero timeouts on every arm; control drift 1.003.

| measure | OFF arms (a, c) | ON arm (b) |
|---|---|---|
| decode median | 31.75 tok/s | 31.60 tok/s |
| on/off decode ratio | — | 0.995 |
| prefill read median | 44.6 s | 44.5 s |
| slow-prefill fraction (>120 s) | 0 | 0 |

**Verdict: no gain (L2_NO_GAIN).** The split is a clean parity — within noise of
the OFF arms — against a gate of ≥+5% decode. This matches the earlier L3
profile: at these batch-1 MoE shapes the shared lock was never the bottleneck
(head-of-line waits were already tiny), so removing it moves nothing.

## Status

- Not adopted; `STRATA_HELPER_STREAMS` stays unset in production and in every
  harness default. The OFF arms double as the behaviour-identity proof for the
  env-unset path of the new binary.
- The code stays in the tree behind the env gate (default-off, like
  `STRATA_RPC_PROFILE`); it is available if a future workload shows real
  cross-channel contention.
- Evidence: `build-mixed-gpu/cap1-validation/l2w1-status.json`,
  `l2w1-progress.log`, per-arm configs/launchers/manifests alongside.
  Production was restored after the window (fresh supervisor, smoke generation
  HTTP 200).
