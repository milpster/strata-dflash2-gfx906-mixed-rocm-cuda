# The engine-side fix for the amdgpu userptr fault: verified — 2026-11-24

Verification record for `STRATA_POOL_NOHUGEPAGE`, the opt-in `MADV_NOHUGEPAGE` on the engine's 32 GiB RAM pool
mapping. Fixer lane, branch `recovery-startup-watchdog`, box GUAM, nothing serving, GPU idle. **I ran no boot and no
GPU work**: every number is read from an artifact and cites the file and line, or the command that produced it. No
push. One kernel boot covers the night (`who -b`: 2026-11-23 22:04). **elog** =
`tr -d '\000' < bench/ab-v0210/rpcprof/out/faultdiff-r1/logs/pool-nohuge.engine.log | cat -n` (509 lines; raw file
NUL-padded, so line numbers are taken after `tr`). **TSV1/TSV2** = `bench/fault-va/out/cycle-20261124-014437.tsv` and
`…-015920.tsv`.

## 1. The fix

| Item | Value | Source |
|---|---|---|
| code | `std::getenv("STRATA_POOL_NOHUGEPAGE")` → `::madvise(base, pool_bytes, MADV_NOHUGEPAGE)`; one line on success, a WARNING on failure, never fatal | `src/core/ram_pool.cpp:243-252` (`Strata-wt-helper-ipc`), `engine/src/core/ram_pool.cpp:243-252` (`Strata-wt-0218`) |
| mapping site | `void* base = ::mmap(…)` at **line 224**; the advice is **before** the pre-fault loop (254: `// Lock in bounded offset-sized chunks; pre-fault each page.`) | `grep -n '::mmap\|Lock in bounded'` in both |
| default path | unset ⇒ no match, no syscall, no output; the block is the only code change | `git show 473f715f62 -- src/core/ram_pool.cpp` (+22 −2) |
| commits | `473f715f62` 2026-11-24 01:26:35 +0100 (`helper-ipc-diet`); `db5bc3ab10` 01:26:40 +0100 (`port-0218`) | `git log -1` in each worktree |
| binary | `Strata-wt-0218/build-lane/strata` 30,555,608 B, 01:29 → `run/port0218-nohuge-bin/rpcw-hip/strata`, both sha256 `15d330ea4d0eec4ca64ef3973e52d332253a981c25dde42b70567bf8b3ae7577` | `sha256sum` both |
| rig pin it replaces | `run/port0218-bin/rpcw-hip/strata` = `936efcc798aab395…` | `faultdiff_configs.py:67-69` |

The knob is in the binary's strings (`strings -a`: **0** hits pinned, **2** patched) and the pinned elog has **0**
lines matching `transparent huge` — the default path is as silent as claimed. Under
`LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib`, `ldd` resolves `libhipblas.so.2`,
`libamdhip64.so.6` and `libhsa-runtime64.so.1` to `/opt/rocm-6.1.0/lib`, `librocblas.so.4` to the xnack tree;
helper unchanged (`58c3ae020a7479a1…` = rig pin).

## 2. The kernel was at real-use values

`bench/fault-va/page_stability.sh restore` ran at **01:28:51** (`journalctl | grep page_stability`: `stable` 00:56:10,
`restore` 01:28:51, nothing after) — 16 min before the first patched boot.

| Knob | Value now | Faulting configuration |
|---|---|---|
| `transparent_hugepage/enabled` | `[always] madvise never` | `[always]` |
| `transparent_hugepage/defrag` | `always [defer] …` | `[defer]` |
| `vm.compaction_proactiveness` | `20` | `20` |
| `vm.swappiness` | `10` | `10` |

So this is not the sysctl-driven result: **the engine knob is the only deliberate difference from the faulting
configuration** (the restored state file is root-only and unread; the live values are the evidence).

## 3. The runs

`bench/fault-va/cycle_boots.sh` (255 lines then, commit `674a1cf0ca`; amended `adb7fa97b4` is 302) with
`LEG=pool-nohuge` plus `STRATA_FAULTDIFF_ENGINE`/`…_SHA256`; the leg turns the knob on via `ENV_LEVER`
(`faultdiff_configs.py:94`). The boot printed `STRATA_POOL_NOHUGEPAGE=1: MADV_NOHUGEPAGE on the pool's own mapping`
(`/tmp/opencode/cb-boot-3.log:1`) and `sha256 15d330ea4d0eec4c; pinned 936efcc798aab395` (`cb-boot-2.log:5`). **The
knob-fired assertion** — all five patched boots printed this, one line each (elog 26, 131, 236, 324, 430):

```
strata: RAM pool: 32768.0 MiB (34359738368 bytes) mapping: transparent huge pages off (MADV_NOHUGEPAGE; STRATA_POOL_NOHUGEPAGE)
```

Four cold 58,423-token prefills completed, each genuinely cold (`0 reused + 58423 read`):

| cycle | engine start | prefill | tok/s | 4-tok tail | AnonHugePages after | min avail | max swap | rss | elog |
|---|---|---|---|---|---|---|---|---|---|
| TSV1 cb-2 | 01:45:04 | 220,624 ms | 264.8 | 15.3 tok/s | 4,352,000 kB | 11,510 MiB | 7,150 MiB | 40.0 GiB | 102 |
| TSV1 cb-3 | 01:51:03 | 212,588 ms | 274.8 | 14.8 tok/s | 4,683,776 kB | — | — | 40.4 GiB | 207 |
| TSV2 cb-3 | 02:04:08 | 298,585 ms | 195.7 | 14.0 tok/s | 5,869,568 kB | 13,408 MiB | 8,731 MiB | 40.4 GiB | 400 |
| TSV2 cb-4 | 02:11:28 | 208,560 ms | 280.1 | 14.4 tok/s | 5,980,160 kB | 13,459 MiB | 8,751 MiB | 40.4 GiB | 506 |

Prefill lines from the elog rows; `anonhuge`/`rss` are TSV columns (`cycle_boots.sh:235-236`); `min avail`/`max
swap` from `mem` in `records/cb-<n>/B-pp239k.json`; starts from `engine started:` lines 6/111/216/304/410. "—" =
overwritten before the reading (`records/cb-N` is shared): unknown, not zero; tails are **not a tg rate**.

```
$ journalctl -k --since '2026-11-24 01:44:00' --until '2026-11-24 02:16:30' --no-pager | grep -c 'no-retry page fault'
0
```

Same from the elog (`grep -c` → 0) and from 00:50:00 onward. Pressure was **not** lower: the faulting window's
completed legs recorded `min_avail_mib` 11,308 and 12,692 (`amdgpu-userptr-fault-diagnosis-2026-11-24.md:88-90`)
against 11,510-13,459 here, and swap here (7.1-8.8 GiB) exceeded the 4.35 GiB gate before the 00:49:26 fault (:126).

## 4. Against the baseline

Four fatal permission faults earlier tonight, each 10 kernel records, all `0000:09:00.0`, all `XDMA (0x0)`,
`PERMISSION_FAULTS: 0x3` (`journalctl -k | grep -o 'Process [a-z-]*' | sort | uniq -c` → `40 Process strata` = 4
× 10). Boot→fault and token count from `amdgpu-userptr-fault-diagnosis-2026-11-24.md:34-41`, pids re-checked here:

| # | time | pid | boot → fault | at |
|---|---|---|---|---|
| 1 | 22:32:16 | 422387 | 276 s | 58,418 of 58,423 tokens |
| 2 | 22:48:21 | 784900 | 271 s | same prefill |
| 3 | 23:29:49 | 1756818 | 251 s | same prefill |
| 4 | 00:49:26 | 899926 | 282 s | same prefill |

**4 fatal events in the earlier cold attempts (4 of 9, same file:120), 0 in the 4 here.** The earlier kernel-level
experiment — `page_stability.sh stable`, four boots at 00:57:50 / 01:04:26 / 01:13:56 / 01:20:07, 234,058 / 220,891 /
242,301 / 263,508 ms, 0 faults (file:79-84) — this run **supersedes as evidence**: no kernel change, real-use policy.

## 5. Honest observations

1. **Two of seven cycles never booted.** `prerequisite 0` refused them at 01:44:44 and 01:59:30 with `hot non-pinned
   thread(s) on the stage cores`: `2339269/git 95.8%, 3384260/ps 300.0%` (`…/prereq0-20261124-014444.json`,
   `VERDICT: REFUSED`) and `3750883/ps 300.0%` (`…-015930.json`). My own lanes. The two TSVs hold **7 cycle rows (3 + 4),
   not 6**: 2 refused, 1 died with `-6`, so **4 prefills** are the usable sample.
2. **One cycle's engine died of something else.** Second run, cycle 2 (engine up 01:59:47): `the engine stopped
   unexpectedly (exit code -6)` (`…/logs/pool-nohuge-boot.log:125`) with `HW Exception by GPU node-1 (Agent handle:
   0x3f2a01b0) reason :GPU Hang` (elog 298). The kernel recorded a **GPU reset on `0000:04:00.0`, the card that never
   faults**: 02:02:30 `Failed to send message 0x10, response 0xffffffff`; 02:02:57 `qcm fence wait loop timeout
   expired`, `The cp might be in an unrecoverable state due to an unsuccessful queues preemption`, `GPU reset
   begin!. Source: 4`, `BACO reset`; 02:02:58 `VRAM is lost due to GPU reset!`, `GPU reset(1) succeeded!`. A
   reset/hang class, not a permission fault (`grep -c '04:00.0.*no-retry page fault'` = 0): separate, unexplained,
   on the other card, not evidence for or against the knob.
3. **The override flag was wrong in the first TSV.** The driver writes `engine_exe`, `engine_sha256`,
   `engine_identity_reason` and no `engine_override` (`records/cb-2/B-pp239k.json`), so the old `record_engine` printed
   0 — a runner bug fixed in `674a1cf0ca` (flag derived from digest-vs-pin, hence TSV2's 1). **The digest is the
   evidence**, and in all four usable rows it is `15d330ea…`.
4. **One TSV2 row is not its own cycle's prefill.** TSV2 cb-2 says `stale record (older than this cycle's prefill)`
   and carries a `212588 ms` judge line — the previous run's, inherited through the appended elog. That is the cycle
   that died with `-6`; the record-stamp and elog-offset fix in `674a1cf0ca`/its amend is the repair.

## What this does NOT show

- **No controlled A/B.** n = 4 prefills on one binary with the knob on; that binary without `STRATA_POOL_NOHUGEPAGE`
  was not run tonight, so "the knob is why" is inference from timing and mechanism, not a matched comparison.
- **No head-to-head with the kernel experiment.** THP-off (4 boots) and the engine knob (4 boots) ran hours apart,
  different swap residue, page-cache state and load; neither arm is the other's control.
- **The pool is not the only registered range.** `AnonHugePages` after the prefill stayed **4.15-5.70 GiB** (§3), so
  other anonymous mappings — prefill ring slots, PLE host buffers, the arena — keep transparent huge pages. A fault
  originating there would not be stopped by this knob.
- **The `0000:04:00.0` reset is untouched and unexplained.** One occurrence, no coredump analysed, no attempt to
  attribute it to the pool, the knob, load or the card.
- **No speed claim.** 195.7-280.1 tok/s over 4 boots, one binary, swap 7.1-8.8 GiB: descriptive, a prompt-read rate.
- **Not built into the serving binary.** `:8009`'s pin (`run/port0218-bin/rpcw-hip/strata`) lacks the string; inert.

## Next

1. **Promotion needs the rebuilt binary plus the quality gate.** Build the fix into the `:8009` engine, then PPL / KL /
   top-1 against the current champion before prod serves it; `MADV_NOHUGEPAGE` should be behaviour-neutral, but that
   is an assumption until the gate measures it. Nothing to restore: the sysctls are at real-use values already, and
   `/var/tmp/page_stability.saved` can go once the operator is satisfied.
2. **Re-run the fault-differential legs on top of the fixed binary**: `parking-off`, `single-card-1`, `xnack-0`,
   `xnack-1`. A fault in any would say another mechanism contributes; four clean legs narrow the remaining
   candidates to the pool's neighbours.
3. **Track `0000:04:00.0` as its own class**: the `qcm fence wait loop timeout` → BACO → `VRAM is lost` signature has
   its own history (`gpu-crash-attribution-2026-10-26.md:14-17`) and is not part of this work.
