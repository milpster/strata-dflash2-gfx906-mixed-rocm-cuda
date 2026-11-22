# Plan: move the Strata deployment to upstream 0.2.18, keep the serving lane alive

Date: 2026-11-22. Author: planning subagent (read-only research, on the local engine). Status: reviewed and approved 2026-11-22 — two review passes by the parent; the fixes below were applied by the parent after the author's session could not land edits. Nothing here has been run.
Every count below was measured with `git` on 2026-11-22 in the worktree `/mnt/srcds/Strata-wt-merge-ipc` (branch `merge-helper-ipc-diet`, HEAD `564e1dbbea`) against `origin/main` = `6e38f588a6` (release 0.2.18, engine 0.1.64, fetched 2026-11-22). Anything not measured on a machine run is marked **unconfirmed (static analysis)**.

## 1. Goal

We want the engine, server and terminal code this box builds and serves to come from upstream release 0.2.18 (`v0.2.18`, tip `6e38f588a6`), while keeping the four fork-only serving features we depend on: the mixed-GPU expert-helper lane (helper-ipc-diet), the pool-admission suite, the DFlash2 runtime, and the per-card Foresight-swap VRAM clamp (`564e1dbbea`). The current prod (`127.0.0.1:8009`, engine sha `4b8251e9` in `run/fsclamp-bin`, helper `9d16742d`) keeps serving through the whole effort. Nothing gets promoted without an operator-approved window, armed files, and same-day A/B legs.

## 2. Current state vs target state

| Item | Current state | Target state |
|---|---|---|
| Upstream position | 2,489 commits behind `origin/main` (measured 2026-11-22; merge-base `54828c7efb`, dated 2026-11-06) | Even with tag `v0.2.18` (`6e38f588a6`) |
| Fork position | 6,608 commits ahead (736 of them merge commits; 1,271 first-parent) | No commit-carry-over; lane re-applied as a small patch set |
| Repo layout | old top level: `src/ include/ serve/ sycl/ metal/ apps/ strata.py` | new (0.2.16 move, commit `4058aa782b`, 2026-11-17): `engine/ terminal/ app/ docs/ tools/` |
| Engine | fork tree, engine 0.1.57-era lineage | upstream 0.2.18 engine (0.1.64) + fork-only lane files |
| Serving | prod `:8009` via `launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh`, server `$ROOT/v0215/serve/server.py` (ROOT=`/mnt/srcds/Strata`) | unchanged prod; new stack armed as a fresh `v0NNN` tree, promoted only by the operator |
| Fork-only features | helper lane, pool suite, DFlash2, FS clamp — all live | re-ported and proven on the new tree before any promotion |
| Build recipe | ROCm 6.1.0 userspace, gfx906, xnack rocBLAS (AGENTS.md) | unchanged |

## 3. Evidence gathered (all 2026-11-22, commands shown)

**Counts.** `git rev-list --count origin/main..HEAD` → 6,608 ahead; `HEAD..origin/main` → 2,489 behind. Ahead commits touching `src/` 2,411, `include/` 985, `sycl/` 307, `serve/` 575, `metal/` 0.

**History duplication.** Among the ahead commits that touch engine paths there are 2,710 unique subjects; 2,368 of them also appear in `origin/main` by subject (`comm -12` on `git log --format=%s` lists). Only **342 subjects are not found upstream**. The fork's "6,608 ahead" is mostly upstream work re-parented with different SHAs, not fork invention. The fork engine already contains nearly all upstream work up to the 2026-11-06 merge-base.

**Path mapping (verified against both trees).** `git ls-tree`: `src/` (481 files) → `engine/src/` (614 upstream), `include/` (245) → `engine/include/` (303), `sycl/` → `engine/sycl/`, `metal/` → `engine/metal/`, `serve/server.py` → `terminal/serve/server.py`, `strata.py` + `strata_cli/` → `terminal/`. Example both sides: `src/core/expert_source.cpp` → `engine/src/core/expert_source.cpp`.

**Fork-only engine surface is bounded.** Mapping every fork `src/`+`include/` path into the new layout and comparing against `engine/src`+`engine/include` at `origin/main`: **124 files exist only in the fork**. The lane-critical ones: `core/expert_helper_{candidates,protocol,reserve,socket}` (+`expert_helper_fallback.cpp`) — the helper-IPC lane; `core/{ram_pool,pool_expert_store,pool_expert_cache,pool_expert_source_adapter}` — the pool suite; `dflash*` (`dflash.hpp`, `dflash_runtime.cpp`, `dflash_ring.hpp`, `dflash_tap_*`, `kernels/dflash_inco.*`, `spec/dflash_policy`) — the fork DFlash2 runtime; plus `dsv41/ds4_pool.hpp`.

The full port surface is larger than 124: mapping `sycl/`→`engine/sycl/` and `serve/`→`terminal/serve/` too, then `LC_ALL=C comm -23` of the mapped fork path list against `origin/main`'s `engine/src`+`engine/include`+`engine/sycl`+`terminal/serve` (measured 2026-11-22, re-run under two upstream scopes): **140 files exist only in the fork** — 124 + `sycl/` 6 + `serve/` 10; `metal/` 0 ahead. (An earlier review count of 168 included 28 `metal/` mapping artifacts: fork paths with zero fork commits behind them, orphaned by upstream's restructure of `engine/metal/`. §7 takes upstream's metal wholesale, so they are not port surface. Reconciled 2026-11-22: **140 is the port surface**.) Phase 1 ports every listed file; upstream's `engine/sycl/` and `terminal/serve/` files are NOT taken wholesale (decision: port, not replace; the risk is re-implementing a fork-only serve/sycl behavior — §6 covers the check).

**Hard spots on shared files (two-dot content diffs).** `expert_source.cpp`: fork +1,556/−56 lines vs merge-base, upstream +594/−54; fork vs upstream tip differ by +1,606/−646 (`git diff --stat origin/main:engine/src/core/expert_source.cpp HEAD:src/core/expert_source.cpp`, reproduced twice 2026-11-22, raw +/- line counts agree; a review pass measured +1,571/−639 with `grep -c "^+[^+]"`, which undercounts empty added lines — reviewer method error, reconciled) on a ~8,000-line file. `serve/server.py`: upstream rewrote it (+1,324/−2,941 since merge-base: `git diff --stat 54828c7efb:serve/server.py origin/main:terminal/serve/server.py`, reproduced; a review pass measured +1,296/−2,583 with the same undercounting grep — reviewer method error, reconciled); the fork changed it +217/−14. These two files must be hand-merged, not patch-applied.

**Fork-only behaviors re-confirmed at the upstream tip.** `git grep origin/main`: STRATA_FS_SLOTS is still parsed with `std::atoi` in `foresight_swap_from_env` — `getenv` at `engine/src/core/expert_source.cpp:7128`, `std::atoi` into one scalar `slots` for all cards at :7129 (`expert_nibble.hpp:290` is only the env-lever help list, not the parse); `foresight_swap.hpp` has no `card_slots` (no per-card comma list — ours is "2,8"), and there is no VRAM clamp; the clamp from `564e1dbbea` lives in `src/core/expert_source.cpp` (+37/−3). Note: upstream 0.2.18 *does* ship its own `dflash2.*` runtime and a `STRATA_HAVE_DFLASH2` flag (`engine/cmake/strata_gpu_engine.cmake`) — the flag name collides with ours but gates upstream's runtime, not the fork's. Treat as a semantics-collision risk, not a win.

**What 0.2.18 actually changed.** 299 upstream commits touch `engine/src|include|sycl` since the merge-base. Sampling the HIP/MoE/expert/foresight keyword list: the wave is the Hy4-Flash family (`engine/src/hy4flash/` runner, RDNA2/3/4 WMMA on gfx103x/11xx/12xx, sm_120/121, Xe2, Metal). No gfx906-named change appeared in that sampled list. Upstream's `AdaptiveExpertCache` and CPU/SSD expert tiering are conceptually adjacent to our pool suite but are a different implementation (unconfirmed static analysis — no boot-level comparison run).

**Deployment stack (read, not run).** The launcher starts `$WORK/serve/server.py` with `WORK="$ROOT/v0215"` — i.e. `/mnt/srcds/Strata/v0215/serve/server.py`; sockets `run/prod-iq3xxs-ud-mixed.sock`/-2; refuses if `:8009` is occupied; requires `${STRATA_DFLASH2_DIR:-/mnt/srcds/dflash2}/artifacts/inco-dflash2-q4_0.gguf`. Versioned trees `v0210`…`v0239` exist. Harnesses present: `bench/ab-v0210/rpcprof/knob-leg.sh`, `ctx-threshold.sh`, `park-restore-window.sh`; pinned binaries in `run/fsclamp-bin/{rpcw-hip,rpcw-cuda}` with `SHA256SUMS`. Oracle figures (3,058-token judge, 239,663-token leg) appear in `bench/ab-v0210/` records (`shortpp/out/T1b-r1-judge.txt`, `out/V7-r1.json`, `out/DF3-r1-io.csv` and siblings).

## 4. Recommended strategy: (b) fresh upstream checkout + port the lane

**Do (b): start from tag `v0.2.18` in a new worktree, port the full fork-only engine surface (140 files measured — 124 from `src/`+`include/`, 6 from `sycl/`, 10 from `serve/`) plus the hand-merged shared-file deltas and the deployment stack, build offline, prove with the existing harnesses, and arm — never rebase, never promote yourself.**

Why not (a) rebase onto `engine/src`: 6,608 commits including 736 merge commits would be rewritten, and 2,368 of 2,710 engine subjects duplicate upstream already — a rebase moves nothing and risks the whole tree for zero content gain.

Why not (c) stay forked and cherry-pick: it keeps today's risk of upstream drift, and it never reaches the stated goal of 0.2.18. If the port fails validation twice, fall back to (c) as the contingency, not the plan.

Why (b) is bounded and safe: the fork-only surface is 140 engine files (measured; the review's 168 was 28 `metal/` mapping artifacts, reconciled — §3) with ≈34 lane-critical, plus 2 hard files (`expert_source.cpp`, `server.py`); everything else is new code upstream-side that we inherit for free. The serving lane is untouched until the operator opens a window.

## 5. Phases

Each phase names what runs, what gets built, what proves it, and what could break. Prod stays up in every phase. All builds are fixer-lane only: no boots and no GPU work until Phase 4, and only in an operator-approved window.

**Phase 0 — freeze and create the port tree (no build, no boot).**
```
git -C /mnt/srcds/Strata worktree add /mnt/srcds/Strata-wt-0218 -b port-0218 v0.2.18
```
Confirm the tree: `ls /mnt/srcds/Strata-wt-0218/engine/src/core/expert_source.cpp`.
Proves: tree identity only. Could break: nothing live.

**Phase 1 — file-level port (source only).**
Copy all fork-only files that the §3 command lists (140 measured; the checklist is the command output, not a hand count) to their new-layout paths, including the 6 `sycl/` and 10 `serve/` files. Then carry the fork's edits on shared engine files as a reviewable patch series:
```
git -C /mnt/srcds/Strata-wt-merge-ipc diff 54828c7efb..HEAD -- \
  src/core/foresight_swap.hpp include/strata/core/foresight_swap.hpp \
  include/strata/core/{conversation_buffer,conversation_cache,conversation_file,expert_cache,expert_source,mtp,read_once,session_save_reclaim}.hpp \
  > /mnt/srcds/Strata-wt-0218/port-hint-engine.patch   # reference only; hand-apply to engine/ paths
```
Hand-merge the two hard files (`engine/src/core/expert_source.cpp`: helper hooks, pool seams, per-card `card_slots`, the `564e1dbbea` clamp; `terminal/serve/server.py`: re-implement the fork's +217-line mixed-lane edits against upstream's rewrite — sockets, helper flags, boot lines). Wire the fork files into `engine/CMakeLists.txt` / `engine/cmake/strata_gpu_engine.cmake`; resolve the DFlash2 flag-name collision by renaming the fork flag (e.g. `STRATA_HAVE_FORK_DFLASH2`) so upstream's own `STRATA_HAVE_DFLASH2` stays untouched.
Proves: none yet; Phase 2/3 gate it. Could break: silent loss of a shared-file hunk — every fork-only behavior gets a named check in §6.

**Phase 2 — gfx906 build (fixer lane, CPU only).**
```
PATH=/opt/rocm-6.1.0/bin:$PATH cmake -S . -B build-906 -G Ninja -DSTRATA_HIP_GFX906=ON \
  -DCMAKE_HIP_ARCHITECTURES=gfx906 -DCMAKE_HIP_COMPILER=/opt/rocm-6.1.0/llvm/bin/clang++ \
  -DCMAKE_PREFIX_PATH=/opt/rocm-6.1.0 -DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF \
  -DSTRATA_HAVE_FORK_DFLASH2=ON
ninja -C build-906 rpcw-hip rpcw-cuda
```
(AGENTS.md recipe; never `/opt/rocm`. `STRATA_HAVE_DFLASH2=OFF` keeps upstream's own dflash2 runtime gated off; after the Phase 1 rename the fork runtime answers to `-DSTRATA_HAVE_FORK_DFLASH2=ON`, already on the line above.) Verify the expected `ldd`: `libhipblas.so.2`, `libamdhip64.so.6` → `/opt/rocm-6.1.0/lib`; `librocblas.so.4` → `/home/srcds/rocm-gfx906-xnack/lib`. Record sha256 of both binaries (same practice as `run/fsclamp-bin/SHA256SUMS`).
Could break: upstream CMake drift changing link assumptions; flag collision mis-gating the drafter build.

**Phase 3 — CPU/parity tests (no GPU, no boot).**
Run the fork's parity units now living in the ported tree: `dflash2_parity.cpp`, `dflash_taps_test.cpp`, `spec/dflash_policy_test.cpp`, `dflash_load_test.cpp` (exact target names TBD in build files — unconfirmed), plus upstream's engine unit tests for the touched files. The helper protocol must match the pinned helper: `expert_helper_protocol.hpp` in the port tree must pair with helper sha `9d16742d` (pin the pairing in SHA256SUMS).
Could break: protocol version drift vs the existing 3080 helper binary.

**Phase 4 — trial-lane validation (operator-approved window; `:8019` only, never `:8009`).**
Boot the new build as a trial tree (`v02NN`), sockets `run/trial-spill40.sock`/-2, port 8019, trial config copied from the spill40 conventions; verify boot lines (engine path, expected flag lines, per-card "2,8" FS slots (boot line: `slots per layer per card: card0:2, card1:8`)) before firing any leg. Legs, same-day and same-protocol:
1. `bench/ab-v0210/rpcprof/knob-leg.sh` — A/B new build vs the sha-guarded prod pair (`4b8251e9`/`9d16742d`) through the `rpcprof/records.py` ledger; every leg records `max_rss_mib`, `min_avail_mib`, `max_swap_used_mib`, per-card `max_vram_mib`; tps is the comparable metric.
2. `bench/ab-v0210/rpcprof/ctx-threshold.sh` — context threshold must match the prod record.
3. `bench/ab-v0210/rpcprof/park-restore-window.sh` — park/restore incl. the checkpoint-index behavior (`dd59c56630`, `d36df99886`).
4. The 3,058-token oracle judge (`bench/ab-v0210/shortpp/out/T1b-r1-judge.txt` comparators) — output must match the recorded verdict.
5. The 239,663-token 240k leg (recorded in `bench/ab-v0210/out/`) — full-length soak on the new binary.
Could break: page-cache/swap-residue confounds — name boot state per window (AGENTS.md discipline); wrong tuple on the port — check boot lines first.

**Phase 5 — arm the tree, operator promotes.**
Lay the passing build into a fresh `v0NNN/` (launcher copy, server under `v0NNN/serve/`), with the launcher's `$WORK` pointing there; copy sockets/stop scripts unchanged; verify kill/stop logic stays in `stop-prod.sh`/`stop-all.sh` only. Produce the promotion note: leg tables, memory columns, shas. Promotion = operator action: TERM supervisors via their `run/*supervisor.json` state files, `stop-all.sh`, 20 s quiet, boot new launcher, keep `v0215` warm for one-click rollback.
Could break: any script still referencing the old `src/` layout — grep scripts for `src/core` paths before the window.

**Phase 6 — post-promotion record.**
Journal entry per the repo convention; update `docs/` style per CONTRIBUTING (plain words, measured numbers). Update `run/` pins and the `AGENTS.md` machine facts only with operator sign-off.

## 6. Risk list — fork-only features that could be lost, and the check

| Feature | Where it can die | Check that proves it survived |
|---|---|---|
| FS per-card slots ("2,8") + VRAM clamp `564e1dbbea` | upstream `foresight_swap.hpp` has no `card_slots`; `atoi` parse would silently flatten it to one count | grep for `card_slots` in port tree (must exist); trial boot line showing per-card slots; tight-VRAM leg showing the clamp elog; `knob-leg.sh` tps parity |
| Helper-IPC lane (49 subjects — `git log --format=%s origin/main..HEAD -- src include sycl \| grep -ci helper`, 2026-11-22; 7 files: `expert_helper_{candidates,protocol,reserve,socket}` + `expert_helper_fallback.cpp`) | dropped CMake wiring; protocol drift vs helper `9d16742d`; second socket flag lost | boot line shows helper attach + `--socket2`; `test_gpu_roles`-style check; launcher's socket-pair assertion at boot |
| Pool-admission suite (90 subjects, shared-file hunks in `conversation_*`, `expert_cache`, `mtp`, `read_once`, `session_save_reclaim`) | the biggest hunk-loss risk — edits sit on upstream-rewritten files | `park-restore-window.sh` pass + admission unit targets; oracle verdict unchanged |
| Fork DFlash2 runtime (135 subjects; upstream has a *different* dflash2 + same flag name) | flag-gates upstream's runtime instead of ours → wrong drafter, silent | drafter GGUF load line (`inco-dflash2-q4_0.gguf`), `dflash2_parity` CPU test, 240k leg reply hash parity |
| Prod server behaviors (+217 lines on a file upstream rewrote: +1,324/−2,941 since merge-base, `git diff --stat 54828c7efb:serve/server.py origin/main:terminal/serve/server.py`, reproduced 2026-11-22) | patch will not apply; hand-re-implementation can miss a line | line-by-line review of the old fork diff vs the new server; launcher socket/refuse guards pass on trial boot |
| gfx906 link recipe | upstream CMake drift links `/opt/rocm` by default | `ldd` equals the AGENTS.md expected trio; soname check fails the build in CI-less flow — do it by hand |
| Deployment scripts on old layout | hardcoded `src/`/`serve/` paths | grep `bench/`, `launch-*.sh`, `stop-*.sh` for old top-level paths before Phase 4 |

## 7. Explicitly NOT carried over

- No history: the 6,608 fork commits (736 merges) are not rebased, cherry-picked wholesale, or grafted. Their content moves as the 140 fork-only files (§3) + hand-merged hunks.
- The 2,368 upstream-duplicate engine subjects — upstream 0.2.18 already contains that work.
- Fork top-level legacy: `README-upstream.*`, root `CMakeLists.txt`, old `apps/` naming, `bench.sh`, `JOURNAL.md` top level — not moved into the `engine/`/`terminal/` layout except journals/bench records, which stay in the fork tree as records.
- The Hy4-Flash family (`engine/src/hy4flash/`) — inherited by the tree but not enabled, tested, or served; irrelevant to this deployment's models.
- Old `v0NNN` trees (`v0215` … `v0239`) stay as-is; they are not rebuilt or migrated.
- Fork's `metal/` commits: 0 ahead (measured) — take upstream's metal side unchanged.

## 8. Open questions for the operator

1. Pin tag `v0.2.18` or track `main`? (upstream velocity measured: 2,489 commits in the 16 days since our merge-base)
2. New tree name — continue `v0NNN` (next would be `v0240…`)? Who numbers it?
3. Which served tuple must be byte-identical after promotion? (prod today: ud-iq3xxs + w4-q4_0 DFlash2, f16 KV variant in `v0215`)
4. Keep the fork flag name `STRATA_HAVE_DFLASH2` with renamed collision (our proposal) or drop the fork runtime in favor of upstream's dflash2 eventually?
5. Do we want upstream's `AdaptiveExpertCache`/CPU-tier expert work evaluated as a later replacement of the fork pool suite, or is the fork pool lane final? (adjacency is static-analysis only; unmeasured)
6. Who schedules the Phase 4 trial window and the Phase 5 prod-down window?
7. Rollback retention: how long must `v0215` + the current supervisor state files stay boot-ready after promotion?

## 9. Review record

Two review passes, 2026-11-22, by the parent session against both trees. Pass 1 found five defects: the 44-file
port-surface gap (124 vs the full 168), a wrong citation for the FS parse, a 6-vs-7 helper-file count, a wrong
example value ("3,2" for this deployment's "2,8"), and a Phase 1/Phase 2 DFlash2-flag inconsistency. The author's
session could not land the edits (two revision turns returned without file changes), so the parent applied the
twelve exact pairs and verified each. Every load-bearing fact was independently reproduced by the reviewer,
including the 2,368-of-2,710 subject overlap, the 124-file scoped count, the FS parse behavior at the upstream tip,
the dflash2 flag collision, and the launcher's server path.

Pass 2 (the plan's author, 2026-11-22, commands re-run in `/mnt/srcds/Strata-wt-merge-ipc`): defects 2–5 fixed and
Phase 1/2 made consistent (fork flag `-DSTRATA_HAVE_FORK_DFLASH2=ON` in the build line; `STRATA_HAVE_DFLASH2=OFF`
gates upstream's runtime). Three pass-1 numbers did NOT reproduce on this tree and are now written with their exact
commands and both values: fork-only surface with `sycl/`+`serve/` mapped = **140** (124+6+10); `expert_source.cpp`
fork-vs-tip = **+1,606/−646** (review: +1,571/−639); `server.py` upstream rewrite = **+1,324/−2,941** (review:
+1,296/−2,583).

Reconciliation (the reviewer, 2026-11-22, same worktree, same day): **the author's three numbers stand.** The
review's 168 was its own mapping artifact — it counted 28 `metal/` files whose fork paths have zero fork commits
behind them (fork `metal/` is the merge-base content; upstream restructured `engine/metal/`), and §7 already takes
upstream's metal wholesale, so 140 is the port surface. The review's diff counts used `grep -c "^+[^+]"`, which
undercounts empty added lines and lines starting with `+`; `git diff --stat` (1,606/−646 and +1,324/−2,941) is
authoritative. The author's suspicion that the review ran from another tree was wrong — both sessions ran in
`/mnt/srcds/Strata-wt-merge-ipc`; the discrepancies were method, not tree. The FS citation at
`engine/src/core/expert_source.cpp:7128-7129` was confirmed at the upstream tip. All three numbers are now
reconciled in the body; nothing is left open from the review.
