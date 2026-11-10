# Split-40 production restore and allocator investigation — 2026-11-10

## User-directed restore

The user requested split 40, the latest built engine and helper, and the production
conversation disk cache. This is an explicit operational choice, not a claim that
split 40 has passed a conclusive same-build short/long-context TG comparison.

- Branch: `allocator-placement`; pre-existing benchmark outputs and unrelated journal edits are untouched.
- Candidate source worktree: `/mnt/srcds/Strata-wt-helper-ipc`, clean at `894669db7`.
- Build evidence: `build-mixed-gpu/cache-spill-build.log` ends with both L3 builds green.
- Engine build mtime: 2026-11-10 18:58:17 +01:00.
- Helper build mtime: 2026-11-10 18:50:28 +01:00.
- Fixed production copies: `/mnt/srcds/Strata/v0216/build-mixed-gpu/{rpcw-hip/strata,rpcw-cuda/strata-expert-helper-patient}`.
- Engine SHA-256: `00691ac86aabb021b11bac784d10a1c6be6e064c12a8ec126876db82dde24efa`.
- Helper SHA-256: `0a340b99d550cfb7666203824929a90ca4951e8db2c0d7e2cdcad2ee5ab66ad6`.
- `cmp` verified both production copies against the source build artifacts.
- Engine runtime links resolve to ROCm 6.1 (`libhipblas.so.2`, `libamdhip64.so.6`) and the custom gfx906 `librocblas.so.4`.
- The existing v0215 Python serving wrapper remains in use. Only engine/helper binary paths and the layer split change.
- `STRATA_CACHE_SPILL=0` is explicit. No experimental cache-spill path is promoted.
- KV remains fp16. VII reserves remain 256/256 MiB. Helper launch arguments remain 4600 slots, fill 6796 MiB, reserve 300 MiB; actual effective fill/reserve must be read from the helper boot log.
- Production disk cache remains `/mnt/srcds/Strata/data/conversation-cache`, budget 24576 MiB. No scratch cache is substituted and no existing cache data is deleted.
- Previous config saved as `prod-config-swift15-f16kv-dflash-w4-q4_0-mixed.json.bak-20261110-before-split40-latest`; previous v0215 binaries remain available.

## Evidence reset for the investigation

Earlier conversation and journal entries contain contradictory GPU mappings and
unsupported performance explanations. Treat those explanations as hypotheses,
not established causes. In particular, the DFlash cross-GPU tap log is not proof
of an expert-spill bottleneck; expert-slot counts must distinguish primary,
later-stage, and helper residency; request contamination must be ruled out rather
than attributed to automatic disk replay without tracing request admission.

First audit the allocator, residency/dispatch paths, reserve precedence, and
actual physical GPU mapping from source. A read-only specialist may run only
after production serves successfully. GPU builds/tests are parent-owned and
start only after that specialist has reached a verified terminal state.

## Startup verified at 20:03–20:04 +01:00

- Supervisor PID 1374568 is attached to engine PID 1375327 and helper PID 1374628.
- `/proc/1375327/exe` and `/proc/1374628/exe` resolve to the v0216 release copies; both live-file SHA-256 hashes match the recorded build hashes above.
- The engine command line contains `--layer-split 40`, `--kv fp16`, and the original production disk-cache directory/budget/idle-save flags.
- `/health` reports `status: ok` and `loaded: true`.
- A bounded temperature-zero request completed at 20:03:33; response saved in `run/split40-latest-smoke.json`. It generated four reasoning tokens and hit its output limit, establishing serving readiness, not answer quality or comparative speed.
- Serving wrapper and pair supervision remain alive. The production launcher is detached from the transient tool shell.
- Runtime launch log: `logs/prod-split40-latest-20261110-launch.log`; current wrapper/helper logs are recorded in the supervisor JSON.

## Independent read-only audit dispatched

The dispatch of oracle session `ses_e39265907ffeBOcur6PrWcbEKK` failed with
`Agent not found: "oracle"`. It never performed the audit. Earlier statements
that the oracle was investigating were incorrect; no findings can be attributed
to it. A replacement has not been dispatched.

No comparative performance conclusion is claimed by this restore.

## Mid-generation stall investigation

The user reported a stall, not a process exit. The same engine, helper and
supervisor remained alive; supervisor generation stayed 1. No lifecycle operation,
GPU reset, configuration change, or synthetic model request was used in this
investigation. A diagnostic-tool cancellation saying "location shut down" did not
establish an actual service or OpenCode shutdown; that interpretation was withdrawn.

### Incident evidence

Immutable diagnostic snapshot: `run/midtg-stall-20261110-201842.MgzRPj/`.
It contains metrics, the relevant engine and wrapper logs, kernel messages,
supervisor state, cache-file timestamps, and post-incident memory/I/O counters.

- Request `bd69903a0f45` started at **20:14:28.368 +01:00**; 63,500 prompt tokens,
  60,840 reused, 581 generated; `finish=stop`, not an engine error or cancellation.
- Engine prefill: **14,528.2 ms**; decode: **22,373.1 ms**, **26.0 tok/s**.
- Wrapper duration: **266.6 s**. The native log attributes **229,666.5 ms** to the
  idle disk park of 64,080 committed tokens (`conv-11.sess`).
- The file is **2,797,311,812 bytes**. Its birth time is **20:15:05.474645438**,
  last-write time **20:18:54.920831337**, rename/change time **20:18:54.924831375**.
  Estimated decode end is 20:15:05.269. Thus planning reached temporary-file
  creation almost immediately, the long interval precedes the final write, and
  the final rename follows that write by about 4 ms. A long final fsync/rename
  alone cannot explain the interval; the missing time lies in streaming sources
  and/or writes. No per-block timing was retained for this incident.
- At **20:18:42**, inside that interval, the kernel reported an AMD
  `amdgpu_amdkfd_restore_userptr_worker` blocked for over 120 seconds on a mutex.
  Its likely owner was `svm_range_restore_work`, blocked through
  `amdgpu_hmm_range_get_pages -> hmm_range_fault_locked -> handle_mm_fault ->
  do_swap_page -> folio_wait_bit_common`. This directly establishes a GPU host
  memory-restore wait on swapped pages, not a GPU reset or hardware failure.
- Cache files and the active swap files (`/mnt/swap2`, `/mnt/swap4`, `/mnt/swap5`)
  share `/dev/nvme0n1p2`. This can couple write and page-in latency, but does not
  by itself establish storage saturation or causal direction.
- Post-incident only: engine RSS approximately 46.6 GiB, anonymous PSS 39.4 GiB,
  swap 1.92 GiB; total system swap approximately 6.22 GiB. Available RAM was about
  12.3 GiB and sampled swap activity had ceased. These are NOT at-stall readings.
- That request reports helper timeouts **0**, fallbacks **0**. The nearby
  296,438-token context-limit HTTP 400 is a separate rejected request and does
  not explain the completed request's pause.

### Source trace and remaining uncertainty

Source tree: `/mnt/srcds/Strata-wt-helper-ipc`.

- `src/program/generate.cpp:21840-21848` emits/flushed token lines during decoding;
  `:22133` stops decode timing; `:22222-22226` performs idle disk saving;
  `:22401-22410` emits/flushed DONE only afterward.
- `src/program/generate.cpp:13340-13387` plans the save, including GPU quiescence
  and full 40 MiB drafter-sidecar reads. File birth timing rules out this planning
  being the bulk of this incident's 229-second delay.
- `src/core/conversation_file.cpp:1833-1987` reads source fragments, hashes them,
  writes bounded blocks, then flushes/publishes. `FileSink` at `:569-641` uses one
  16 MiB buffer, not a whole-target-KV snapshot. Its source loop includes ordinary
  checkpoint host memory as well as GPU K/V copies, so "disk park" is not a
  measurement of physical disk time alone.
- `src/core/conversation_snapshot.cpp:258-266,517-535` reads K/V via
  `conversation_detail::copy_nonblocking`; `src/core/conversation_copy.hpp:140-152`
  uses cudaMemcpyAsync and a stream synchronization before returning each read.
- Actual serving wrapper `v0215/serve/server.py:2932-2976` yields token lines,
  treats DCACHE lines as heartbeats, and returns only on DONE. `:5831-5863`
  labels a busy request that has emitted tokens as "generating", including the
  post-decode save. This explains the apparent TG stall without denying the
  user's observation.

**Strongest lead, not a proven complete root cause:** a host-memory restore/page-in
stall blocked the streaming save, possibly coupled to swap on the same NVMe.
Without an at-stall engine stack and DCACHE byte/phase history we cannot distinguish
a waiting GPU copy, a faulting host checkpoint read, or a blocked write precisely.
Do not attribute this to split 40, low free VRAM, or a specific allocation yet.

`tools/mixed_gpu/watch_stall.py` is a bounded, read-only recurrence recorder. It
GETs existing metrics, records process fault/I/O/wait counters plus memory and
disk counters, and captures thread waits after ten seconds without observed
progress. It never submits a model request, signals any process, writes to the
conversation cache, or changes configuration. Raw disk progress is retained
without treating a stale "done" phase as this request's activity.

Validation passed: Python compilation, CLI help, a three-second real read-only
capture (`run/stall-watch-20261110-203559-838541`, three idle samples with the
original engine/helper PIDs and no metrics/state errors), and a one-second
connection-refused capture (`run/stall-watch-20261110-203629-770455`). These do not
validate stall detection against a real recurrence. `/proc/pressure` is unavailable
on this kernel, so the recorder uses vmstat counters rather than promising PSI.

A ten-minute passive capture is prepared at
`run/stall-watch-20261110-midtg-1/`. It may observe normal traffic but must not
create traffic to provoke a result. A quiet capture cannot confirm or refute the
stall's proposed mechanism.
