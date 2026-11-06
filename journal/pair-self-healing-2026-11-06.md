# Paired self-healing — 2026-11-06

## Authorization and boundaries

- User approved default paired supervision, engine/helper degradation detection,
  and isolated recovery tests across the six mixed-GPU production launchers.
- Follow-up: **do not kill production; fix its helper**. No live engine/server
  restart or production failure injection is authorized. Keep hardening tests
  CPU-only and isolated from production paths, sockets, and port 8009.
- Branch: `pair-self-heal`, based on the completed background-load-priority work.
- The deployed `v0210` engine, helper binary, server, and model settings stay
  unchanged. A later cutover requires separate approval.

## Findings

- Launchers currently supervise the pair only with `--recover`; their normal
  entry path bypasses recovery. The frozen supervisor detects helper/server death
  and helper-channel failure, but does not directly watch the engine process.
- The helper is absent and both original socket paths are absent. At 16:05 the
  RTX 3080 had 112 MiB used of 8192 MiB, with no helper compute process.
- Engine PID 233080 (started 15:33:51), server PID 2533023 remain live, serving
  swift-1.5-iq3_xxs-f16kv. Engine nice +1 remains active.
- Source inspection confirms helper connection happens during engine startup;
  failed connection leaves `helper_tier_p` unset for the entire run. The server
  only forwards helper socket options when starting the engine. No supported
  hot-attach control was found. Restarting a helper cannot retrofit the already
  running engine; do not claim it restored GPU expert computation.
- The helper's normal disconnect exit unbinds its own sockets. Its `--persist`
  reconnect path has the refill-budget issue already documented in the XXS
  journal, so the live rescue should use a fresh helper process per session.

## Non-disruptive helper rescue

- User was told the precise change before application: restore the original
  helper as a helper-only auto-restarting user service, at nice +1. No authority
  to signal, replace, or restart the engine/server is included.
- Entry point: `bash tools/mixed_gpu/restore-live-helper.sh`. It refuses existing
  socket paths and is idempotent when its service is active. It starts a transient
  `strata-swift15-helper.service` with the original pack, slot and fill budgets,
  both original sockets, and `Restart=always` (also covers a clean disconnect).
- This prepares the next natural engine start. The current CPU-fallback run is
  not expected to connect. A listener alone is not evidence of an active helper
  tier; require an actual engine connection and fill before claiming that.
- Caveat: a hard-killed helper can leave socket files. The rescue never removes
  unknown files; full owned-socket cleanup belongs to the pending pair hardening.
- This is transient runtime recovery, not boot persistence or full hardening.

Rescue validated:

- Service active/running, MainPID 817332, Restart=always, Nice=1, NRestarts=0.
- Both original Unix sockets are listening, owned by helper PID 817332; its log
  reports both listeners. It has not reported an engine connection or fill.
- Re-running the rescue entry point reports the existing active service and
  starts no duplicate.
- `/health` remains HTTP 200, loaded/ok. Engine 233080 and server 2533023 still
  exist with their original process names. A `ps` diagnostic timed out under
  host load, so direct `/proc/PID/comm` reads verified them instead.
- No signal was sent to the engine/server. Neither was restarted by this
  session. This helper-only rescue was subsequently removed, as recorded below.

### Correction: an unattached listener is not recovery

- User correctly rejected keeping an idle helper alive as self-healing. It
  restores no GPU computation for the engine already in permanent CPU fallback.
- Before stopping the rescue, verified the service still had PID 817332 with
  zero restarts, only two listener log lines, and both sockets in LISTEN state
  with no accepted engine connection.
- Stopped only `strata-swift15-helper.service`. It is now inactive/not-found,
  MainPID=0. `/health` remains HTTP 200, loaded/ok. Engine PID 233080 still has
  start token 1846487 and parent/server 2533023, at nice +1.
- Removed the uncommitted helper-only rescue script. No idle rescue service is
  part of the final solution. A supervisor may claim recovery only after helper
  attachment and positive fill, never merely because a listener is alive.
- The live CPU-fallback engine cannot be fixed non-disruptively by this design;
  enabling paired recovery still requires a separately approved engine restart.

## Hardening evidence plan

- Promote the existing supervisor into tracked local source, not the frozen
  deployment. Make ordinary production launches supervised, retain `--recover`
  compatibility, and provide an explicit diagnostic single-run mode.
- Use one lock for the shared port, preserve process-start ownership and exact
  socket-inode cleanup, keep bounded teardown and restart backoff.
- Watch the owned engine as well as helper/server; detect newly logged helper
  CPU fallback without confusing historical engine-log lines with this boot.
- CPU-only fixtures will exercise peer deaths, channel/fallback degradation,
  startup failure, duplicate ownership, cleanup and launcher-mode routing.
- Production virtualenv lacks pytest; use standard-library unittest without
  adding dependencies. All fixture artifacts stay under the project logs tree.
- No live crash test, restart, binary rebuild or model-quality benchmark is
  authorized by this work. No numerical speed claim is planned.

## Implementation review and verification budget

- Parent session owns all verification; no subagent is running on production.
- Claim 1, default paired ownership and safe diagnostic dispatch: intercepted
  executions of all six real launchers, shared-lock contention test, Bash syntax.
- Claim 2, actual attachment rather than an idle listener: CPU-only two-socket
  connect/fill exchange, positive helper fill plus current engine proxy ack;
  listener-only, no-fill, zero-fill and missing-ack negative cases.
- Claim 3, failure recovery and cleanup: kill owned helper/server/isolated engine,
  inject helper/engine channel failure and CPU fallback, verify replacement PIDs,
  bounded cleanup, socket identities, current-generation log handling and backoff.
- Claim 4, preserving load-favored priority: nice +1 on initial and recovered
  fixture peers. Observe live production separately; its existing server can
  automatically replace an engine outside this change's control.
- Minimum checks: one complete unittest run after code stabilizes, targeted
  reruns only for failures/changed boundaries, LSP diagnostics, Bash syntax and
  diff review. No GPU builds are needed: no backend source changes.
- Verified configured engine `exe` and `/proc/233080/exe` resolve to the same
  ELF binary, despite its displayed process name `strata-host`; executable
  identity monitoring does not need a guessed process-name match.
- LSP caught the startup process-token race in the draft; removed the unchecked
  optional subscript before tests. The tail now also handles replaced log files.
- Added early helper publication, positive-fill parsing, engine-side attachment
  acknowledgment, engine/channel monitoring, nice +1 inheritance and stopped
  state on interruption during backoff. Existing model/helper options and KSM
  bootstrap remain in place.

## Verification findings before final acceptance

- Initial suite passed 24 cases; after the publication/snapshot ownership race
  fix, 25 cases passed in 16.120 seconds. However the explicit final process
  audit found two orphaned CPU fixture helpers, PIDs 1250000 and 1299520. Those
  passing suites alone did **not** establish complete cleanup.
- Cause: stopping during startup could let a launcher fork after the last
  ancestry snapshot and then exit. An unobserved orphan was adopted by the host
  rather than remaining discoverable in the launcher's tree.
- Fix: the private Linux supervisor becomes a child subreaper; discovery and
  bounded escalation include its adopted descendants, never the supervisor
  itself. Adopted zombies are reaped; sockets bound during startup/teardown are
  included before inode-checked release. Every recovery-test teardown now audits
  for surviving fixture roles and socket files. Added immediate-startup stop and
  early-server-exit/isolated-engine regressions.

## Independently observed production restart

- During final read-only checks, old engine 233080 disappeared while server
  2533023 remained. `/health` briefly returned loaded=false (still HTTP 200).
- Server log: `the engine stopped unexpectedly (exit code -15). Unrecoverable
  native verification failure: verify: timed out at layer 46; its GPU waits were
  released and the GPU finished (#267)`, followed by `the engine had stopped ...
  starting it again`. Engine log records its new startup at **16:39:20**.
- New engine observed: PID 1300424, parent 2533023, nice 0. This was the existing
  server's engine-only recovery, not a restart requested by this session. It
  again logged helper-connect failure and whole-run CPU fallback. The earlier
  live nice +1 was lost on that automatic replacement; configurations remain
  unchanged. The new paired launcher will inherit nice +1 when deployed.
- User asked whether this session caused the restart; answered no with this
  evidence. User then instructed to continue and finish. No production restart,
  redeployment or live failure injection was performed or newly inferred as
  authorized.

## Final isolated verification

- `.venv/bin/python -m unittest discover -s tests/mixed_gpu -v`: **27 tests
  passed in 18.578 seconds**, Linux, Python 3.11.9, using only CPU subprocesses
  and private Unix sockets. Every recovery teardown also checks for surviving
  fixture processes and socket files.
- `.venv/bin/python tests/mixed_gpu/fixtures/mixed_pair_fake.py --check`: exit 0,
  no surviving fixtures. The two earlier leftovers were removed by its guarded
  `--cleanup` mode, restricted to the exact fixture, Python executable, UID,
  `logs/pair-test-*` environment and matching PID start token.
- One intermediate 27-case run had four **audit timeouts**, not recovery-case
  failures. The scanner read unrelated native processes' cmdlines, which can
  wait on a busy process's mmap lock. It now filters by the fixture's Python
  executable before reading arguments, and the final full run passed.
- LSP: no diagnostics in the supervisor, fixture, or tests. `git diff --check`
  passed. The suite syntax-checks all six launchers, the cutover driver and
  launcher interception guard. No GPU backend source or binary changed.
- Establishes default dispatch, peer/channel/fallback recovery, actual attach
  evidence, ownership isolation, early orphan cleanup, log generation handling,
  bounded teardown/backoff and inherited nice +1. Does not establish live GPU
  reload, model output equivalence, performance, or the disruptive XXS cutover.

## Explicit production activation approval

- Asked whether one controlled restart of the current Swift 1.5 engine/server/
  helper was authorized **after the tests passed**, explaining active-request
  interruption, same-model reload and the earlier no-restart restriction.
- User selected **Deploy with restart (Recommended)**: finish verification,
  restart the current pair under supervision and verify real helper attachment
  and GPU fill. This explicitly supersedes the no-restart boundary for this one
  deployment; no live fault injection or model/settings changes were approved.
- Verified current legacy launcher 2532650 and server 2533023 have the expected
  parent/group/session relationship and use the Swift 1.5 FP16-KV launcher/config.
  Do not execute `relaunch-pair-supervised.sh`: it hardcodes another model and
  delegates to broad process-name kills. Stop only the verified current owner.
- Deployment plan: commit this tested version; gracefully stop the verified
  legacy server and confirm its engine/launcher have exited; start the same
  launcher in a user service owning the **whole supervised pair**, not a helper-
  only service. Keep the original model/config/binaries and hostapp unchanged.
- Then require a fresh `attached` state, actual engine proxy and helper fill,
  RTX VRAM allocation, a real completion and nice +1. Record measured results;
  do not equate HTTP 200 or a listener with recovered helper computation.

## Production activation and acceptance

- Tested implementation committed as **2cec8ab** on `pair-self-heal` before
  stopping production. No subagents were running during the cutover.
- Sent TERM only to verified legacy server 2533023. The old launcher 2532650
  still had a same-model replacement server 1709445 (start token 2388181), later
  with engine 1710012. Its command line named the exact same Swift 1.5 config.
  Sent TERM to the verified legacy launcher as well. A later escalation attempt
  reported all three remaining legacy PIDs already absent: **no KILL signal was
  delivered**. No broad process-name kill or `stop-prod.sh` was used.
- Started `strata-mixed-pair.service` at **17:08:10 CET** through `systemd-run
  --user`, executing `/usr/bin/bash` and the same Swift 1.5 FP16-KV launcher with
  no diagnostic flag. Unit properties: `Restart=on-failure`, `RestartSec=2s`,
  `StartLimitIntervalSec=60s`, `StartLimitBurst=3`, `KillMode=mixed`,
  `TimeoutStopSec=30s`, working directory `/mnt/user/Strata`, stdout appended
  to `logs/prod-pair-supervised.log`, stderr inherited. It is a transient user
  service, not boot persistence. Invocation: 208715acaae046a1aee12eab3114fc46.
- The service owns the **whole** supervised pair, not an idle helper. Its cgroup
  was also verified in `/proc/1775942/cgroup` despite engine session isolation.
  KSM bootstrap still reports enabled; no model config, binary, hostapp affinity,
  or hostapp priority was changed.

### Observed live state

- Unit active/running; MainPID/supervisor **1775071**, NRestarts=0.
- Launcher **1775089**, helper **1775106**, server **1775394**, engine
  **1775942**. Supervisor generation **1**, state **attached**.
- Helper log: engine connection, successful finite-output warmup, **4,324 slots,
  6.61 GiB uploaded**, then second-channel engine connection. No mere listener
  was counted as success. Engine log line 2215 acknowledges `expert-helper
  proxy: 4324 experts ... 6.61 GiB (fill report)`.
- `nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory
  --format=csv,noheader`: helper **1775106 using 7,026 MiB**. This includes
  runtime buffers/context beyond the 6.61 GiB expert weights. The separate host store
  web helper allocation was left untouched.
- Helper, server and engine nice **+1**. A later aggregate of engine thread
  priorities reported **92 threads, all nice +1**. Hostapp priorities remain alone.
- `/health`: HTTP 200, loaded=true, model **swift-1.5-iq3_xxs-f16kv**, max context
  262144, images enabled. Final service check still active/running with the same
  MainPID and zero service restarts; helper still at 7,026 MiB.

### Completion evidence and limits

- The dedicated `PAIR-OK` HTTP probe was **not confirmed**: the first tool call
  timed out while a large request was active; a longer shell probe was aborted
  by OpenCode with `Interaction cancelled because the location shut down`.
  Tools reconnected and the pair remained attached. Neither is claimed as a
  model failure or a successful dedicated probe. No further requests were
  injected into the busy server merely to duplicate existing evidence.
- `/metrics` subsequently confirms three real completed requests on this server,
  all `finish=stop`: **0a808842aa4a**, 46,471 prompt tokens / 467 output tokens;
  **820c5b3258a9**, 47,013 / 1,452; **89018bbaa707**, 48,541 / 418. The fresh server
  log independently records the first completion. This establishes working
  inference on the deployed attached pair, not just HTTP health or allocated RAM.
- At final API sample (1793982361.8685), the next 122,981-token request was
  reading, with 64,328 read and one queued. No interruption or extra parent
  restart was performed for that active request.
- CPU-only recovery injections passed; no live engine/helper crash injection
  was performed. No claim of GPU output equivalence, long-context stability or
  speedup is made. The earlier native verification timeout #267 is not claimed
  fixed internally; this change recovers the pair rather than leaving its
  replacement engine silently helper-less.

Activation and acceptance complete; operating instructions are in
`docs/PAIR_SELF_HEALING.md`. Stop/restart the full user unit for future authorized
maintenance; never revive the removed helper-only rescue as a substitute.

## Post-deployment sluggishness diagnosis (17:10-17:46, read-only)

User reported the server "stalling". Measurements, no changes made:

- **No recovery event occurred.** Pair stayed generation 1, `attached`,
  NRestarts=0 since 17:08:10. One request paused mid-read (63 s at
  15,196/124,390) then continued. The supervisor and the server watchdog both
  correctly did nothing: the engine stayed alive and kept reporting progress.
- **Root cause of the sustained slowness: continuous swap thrash.** Engine RSS
  ~49.9 GB (40 GB CPU experts + activations) plus the hostapp on 65 GB RAM.
  `vmstat` at 17:41-17:46: swap-in 43-140 MB/s and swap-out 38-126 MB/s
  simultaneously, 48.8 GB in swap, 2.4-2.8 GB free, block reads up to 590 MB/s.
  Engine itself had ~1.0 GB swapped out (`VmSwap`), `SwapCached` 38 GB.
- **Prefill degradation matches:** 122,981 tokens in 282.6 s (~435 tok/s, first
  read after boot) → 124,390 tokens in 704.1 s (~177 tok/s avg) → ~280 tok/s
  mean on the next read. Radeon VRAM nearly full (~29-61 MB free), so spills
  go to RAM and then swap.
- **Full re-reads amplify it.** The same client conversation (122,981 → 124,390
  tokens) got `reused: 0` both times: the 2,048 MiB conversation park budget
  cannot hold a ~123k-token fp16 KV, and full VRAM evicts it. Request
  d94a298dba32 spent 704 s in prefill to emit 81 tokens; requests serialize
  (queued=1; an earlier one waited 440 s for its first token).
- The 17:38:54 amdgpu powerplay errors coincide with my own `rocm-smi` sensor
  polling (common on gfx906) and are not proof of a GPU fault. The proven
  bottleneck is RAM/swap, not the GPU or the new supervisor.

## GPU-hiccup recovery traced end to end (17:47-17:51 event)

Trigger and generations (from `logs/prod-pair-supervised.log`, engine stderr,
helper log, kernel log):

- 17:46-47: prefill of a 124,910-token prompt froze at 15,176 tokens for 90+s.
  The helper's prompt tier logged `request failed: helper timeout` (limit
  `--expert-helper-timeout-ms 2000`), then `second channel ended`.
- Supervisor: `generation 1: helper channel ended; restart/refill in 2s` →
  gen 2 launcher 17:48:27.
- Gen 2's engine (17:48:30) refused to start: `cudaMalloc ... failed (out of
  memory): 170 MiB of 16368 MiB free` — the just-exited gen-1 engine's VRAM
  was still held by the driver. `generation 2: engine exited; restart in 4s` →
  gen 3 at 17:48:40.
- Gen 3's engine loaded and then hit `amdgpu ... no-retry page fault ...
  Faulty UTCL2 client ID: XDMA` bursts at 17:50:01 (host pages 0x7f9bfe8cxxxx,
  PERMISSION_FAULTS) and exited. `generation 3: engine exited; restart in 8s`
  → gen 4 at 17:50:13, attached 17:51:12. Backoff doubled 2→4→8 s as designed.
- **Temperature is not the evidenced cause**: post-event Radeon VII junction
  33-34 C, RTX 45 C; no thermal events in the kernel log. The XDMA faults are
  host-page permission faults during peer-to-peer DMA while the box was
  swapping ~100 MB/s both ways with 1-2 GB free — memory pressure unmapped or
  reclaimed pages the GPU was DMA-ing through. The 17:38:54 powerplay spam
  coincided with rocm-smi polling.

Why recovery takes minutes, measured:

1. Trigger→attached ≈ **3.5 min** = teardown + 3 cold rebuilds. Each rebuild:
   helper refills 6.61 GiB over PCIe, engine reloads the 39.97 GiB expert
   arena through a thrashing page cache (1.23 GiB/s vs 1.85 fresh), expert
   cache fill 6.2 s, 8 verify captures, warmup.
2. **VRAM release lag** after engine exit cost a whole generation (gen 2's
   cudaMalloc refusal). The supervisor does not wait for the driver to free
   the dead engine's VRAM before relaunching.
3. **Every restart zeroes all KV**: post-recovery requests re-read 51,576 and
   125,766 tokens with reused=0; a 125,954-token read then ran ~420 tok/s
   (5 min) with **3 requests queued**. Parking the ~124k-token conversation
   needs a ~3.9 GiB snapshot vs the 2,048 MiB budget (`skip parking ... exceeds
   available budget`), so big sessions always pay full re-reads. This dominates
   the user-visible recovery (10-30 min), not the 3.5 min rebuild.
4. The engine never self-heals a dead helper channel: it falls back to CPU rows
   for the rest of the request; only pair replacement restores it.

Remedies (proposals only, none applied): more RAM or a smaller resident expert
pack; avoid 123k-context client sessions while background-load; optionally raise
`--conversation-cache-mib` / lower `--kv-resident` via an approved restart.
Lowering `engine_silence_s` would not help: the engine is slow, not hung, and a
restart would add a full reload and kill the queue.

New options from this trace (none applied): (a) supervisor waits for VRAM
release before relaunch (saves gen-2-style refusals); (b) consider a longer
`--expert-helper-timeout-ms` so transient stalls close no channel; both need
approval and testing first.

## Patient helper replies implemented (user-approved same day)

User: "longer helper timeout sounds good. we dont want a fallback. if it breaks
in any way it is to fix itself."

Found the decisive timer was NOT `--expert-helper-timeout-ms` (that is the
engine's per-window bound; late = CPU window, never a stall) and not the
helper's `--timeout`/`--begin-timeout` (already ~infinite in the launcher). It
was the helper's HARDCODED reply-write deadlines (1 s empty reply, 10 s
kRows/kRowsP/kDraft, in `tools/mixed_gpu/sidecar/helper_main.cpp`): the frozen
engine stopped draining its socket and the helper closed the second channel.
The engine side also has `STRATA_PREFILL_HELPER_TIMEOUT_MS` (default 10000) for
its prompt-row read.

Changes (worktree branch `fix/helper-reply-patience`, commit `71ee8bd`):
`STRATA_HELPER_REPLY_TIMEOUT_MS` (>= 100) overrides every data-reply write
deadline at once; unset keeps each default byte for byte; one startup log line
names the override. Built `strata-expert-helper` in the worktree's rpcw-cuda
dir and copied it to `v0210/.../strata-expert-helper-patient` (original
untouched; md5 fccdb64... vs cf97b6a...).

Main repo: all six launchers use the patient helper with
`STRATA_HELPER_REPLY_TIMEOUT_MS=180000`; the Swift 1.5 config gains
`STRATA_PREFILL_HELPER_TIMEOUT_MS=180000` in `env` and `expert_helper_timeout`
2000 -> 10000.

Verification so far: build OK; override prints only when set (GPU-free run with
`--device 99`); `expert_helper_socket_test` passed (6.23 s); 27/27
`tests/mixed_gpu` passed; config JSON validates. NOT yet live: activation needs
one `systemctl --user restart strata-mixed-pair.service`, then the helper log
must show the override line, fill, proxy ack and a completion.
