# The engine-side fix for the amdgpu userptr fault: verified — 2026-11-24

> ## CORRECTION — 2026-11-25. Three claims below are disproven. Nothing is deleted; the wrong text stays where it
> was written and is marked at the point where it appears.
>
> This record's title says **verified**. As evidence about the fault, that framing does not stand: the promoted
> binary faulted again on 2026-11-24 at 20:30:37 and at 20:44:13. The 20:44:13 fault resolves inside the 32 GiB pool
> VMA; the 20:30:37 one has no snapshot and is unresolved
> (`journal/fault-investigation-corrections-2026-11-24.md` §4). What this record did establish is narrower — that
> the knob fires, that the pool VMA carries `AnonHugePages: 0 kB`, and that the prefill and soak workload of
> 2026-11-24 01:45-04:37 did not fault.
>
> | # | Claim written here | Status | Disproven by |
> |---|---|---|---|
> | 1 | `STRATA_ARENA_MLOCK=1` fixed the fault, and mlock costs 7-14 % on prefill | **both false** | the pool is mlocked unconditionally in `Pool::create` (`engine/src/core/ram_pool.cpp:260`, no `getenv` gate); a live engine booted **without** `STRATA_ARENA_MLOCK` reads `VmLck: 33718272 kB` with the pool VMA flagged `lo`. `STRATA_ARENA_MLOCK` locks only spans passed to `arena_mlock_span` (`include/strata/core/pin_budget.hpp:154-187`), and this tuple registers none — the arena never opens. The "mlock on" and "mlock off" runs were **the same pool configuration** |
> | 2 | The pool got huge pages anyway despite `MADV_NOHUGEPAGE` | **false, and it was inference from a process-wide number** | per-VMA `/proc/<pid>/smaps` puts the 32 GiB pool VMA at `AnonHugePages: 0 kB` with flags `rd wr mr mw me lo ac sd nh` (`bench/fault-va/out/sampler-selftest/huge-1795557323.txt:3`). The cited `smaps_rollup` figure is process-wide and cannot be attributed to a mapping |
> | 3 | The relief/re-rank unregister path revokes the GPU's translation | **false** | `release_pair` has one caller and `refill_pair` two, all inside gates that need `srcp == &arena_src` (`engine/src/program/generate.cpp:26756-26773`, `engine/src/core/expert_source.cpp:7691`, `:8043`, `:8050`). With `STRATA_RAM_BUDGET_MIB=36864` the expert source is the pool adapter (`generate.cpp:9550`), so `ArenaExpertSource::open` never runs and every boot says so: `nothing dropped: this run's expert source is not the arena` (25 times in `logs/engine-prod-port0218.log`) |
>
> What still stands: the fault recurs **inside the 32 GiB pool VMA**, that VMA has **0 kB huge pages and is locked,
> and it faults anyway** — so `MADV_NOHUGEPAGE` never addressed the cause. See
> `journal/fault-investigation-corrections-2026-11-24.md` for the ordered story, the dates, and the sampler
> upgrade that produced the per-VMA evidence.

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
   on the other card, not evidence for or against the knob; counted across boots in **§7**.
3. **The override flag was wrong in the first TSV.** The driver writes `engine_exe`, `engine_sha256`,
   `engine_identity_reason` and no `engine_override` (`records/cb-2/B-pp239k.json`), so the old `record_engine` printed
   0 — a runner bug fixed in `674a1cf0ca` (flag derived from digest-vs-pin, hence TSV2's 1). **The digest is the
   evidence**, and in all four usable rows it is `15d330ea…`.
4. **One TSV2 row is not its own cycle's prefill.** TSV2 cb-2 says `stale record (older than this cycle's prefill)`
   and carries a `212588 ms` judge line — the previous run's, inherited through the appended elog. That is the cycle
   that died with `-6`; the record-stamp and elog-offset fix in `674a1cf0ca`/its amend is the repair.

## 6. The mixed-workload soak that followed

The same patched binary was then booted once more (**03:02:25**, sha `15d330ea4d0eec4c`; the boot repeats the rig-pin
override warning, `/tmp/opencode/mixed-boot.log:3`) and driven with `--load --minutes 20`: two conversations
alternating at **80,000 and 16,000** tokens, one request in flight (`faultdiff_run.py:750-775`), so every switch parks
one and restores the other. The engine's start line is `2026-11-24 03:02:27` (elog 515); the watcher's last line,
six seconds before the memory sample, read `engine 1047021 up 1427s`; the last log write is 03:26:45, **1,458 s
after the start line**, and the leg closed at 03:27:44 (`run/pool-nohuge.state.json`: `ran_s` 1519.7, teardown
included) — against the 251–282 s at which the historic faults landed (§4).

| what | value | source |
|---|---|---|
| requests | 16 `tg`, `max_tokens 1024`, ctx alternating 80,000 / 16,000 | `records/pool-nohuge/load/faultdiff-pool-nohuge-n{1..16}.json` |
| decode (request 2 of each) | **10.3–25.6 s** per request, **277.5 s** total | `walls[1]` in those records |
| prefill (request 1 of each) | 35.5–88.6 s | `walls[0]` |
| parks / restores | 15 / 16 | `grep -c 'parked [0-9]* tokens'`, `grep -c restored` on the elog |
| faults | 0 / 0 | `journalctl -k --since '2026-11-24 03:00'`: `grep -c 'no-retry page fault'` 0, `grep -c 'GPU reset begin'` 0 |
| at stop, engine alive (03:26:20) | `VmRSS 42,337,732 kB` (40.4 GiB), `AnonHugePages 3,778,560 kB` (3.6 GiB) | `/proc/1047021/status`, `/proc/1047021/smaps_rollup` |

> **CORRECTION (2026-11-25) to the paragraph below.** `AnonHugePages` here is `smaps_rollup`, which is
> process-wide; it cannot say which mapping held the pages, and at this hour the sampler had no per-VMA reading at
> all (`bench/fault-va/maps_sampler.sh` gained the per-VMA `huge-*.txt` output only later that evening, 22:55).
> The claim "3.6 GiB in the process's **other** mappings" is therefore an inference, not a measurement — the
> subtraction assumes the pool held none, which is exactly the question. A later per-VMA reading does put the pool
> at `AnonHugePages: 0 kB` (`sampler-selftest/huge-1795557323.txt:3`), but that is a different boot, taken ~19 h
> after this one. What this boot measured is the process-wide total and nothing finer.

`AnonHugePages` is the number that matters: **3.6 GiB of huge pages were still resident in the process's other
mappings** while the pool mapping — the one under `MADV_NOHUGEPAGE` — produced no fault across the 21 minutes of
parking, restoring and disk spill that the load drove (15 parks, 16 restores: the first request has nothing to park).
The exclusion is specific to that mapping, not a global THP effect. The reading was taken at 03:26:20 through `bench/fault-va/engine_pid.sh` (it resolves the real engine, never a shell wrapper) as
`engine_pid.sh | xargs -r -I{} sh -c 'grep VmRSS /proc/{}/status; grep -i anonhugepages /proc/{}/smaps_rollup'`;
the same pid, 1047021, is in every load record's `mem.engine_pid`. `/proc/1047021` is gone now, so the figure lives
in the session's shell output, not in a repo artifact — the command above is what produced it.

`--stop` printed `NOT QUIET: still alive [1046631, 1046178]` (`faultdiff_run.py:733`), but that is a reaping
artefact of the helper: both are `Zs` `<defunct>` (`python`, `strata-expert-h`), `/dev/kfd` has no holder and `:8009`
no listener. Nothing was left running. The `exit -6` cycle on `0000:04:00.0` belongs to the prefill runs (§5.2) and is
not counted again here.

**Soak limits.** n = 1 boot, 16 requests, one binary. The load is a synthetic alternating-filler pattern, not real
traffic, and sequential by design. **No speed claim**: 10.3–25.6 s per request is load traffic with draft-accept
variance (313–749 tokens actually generated per request), not a measured leg and not a tg rate. And the
`0000:04:00.0` reset class stays unexplained and untouched — this soak not reproducing it is one sample, not a closure.

## What this does NOT show

- **No controlled A/B.** n = 4 prefills on one binary with the knob on; that binary without `STRATA_POOL_NOHUGEPAGE`
  was not run tonight, so "the knob is why" is inference from timing and mechanism, not a matched comparison.
- **No head-to-head with the kernel experiment.** THP-off (4 boots) and the engine knob (4 boots) ran hours apart,
  different swap residue, page-cache state and load; neither arm is the other's control.
- **The pool is not the only registered range.** `AnonHugePages` after the prefill stayed **4.15-5.70 GiB** (§3), so
  other anonymous mappings — prefill ring slots, PLE host buffers, the arena — keep transparent huge pages. A fault
  originating there would not be stopped by this knob.
  **CORRECTED 2026-11-25:** that figure is process-wide, so it does not show which mappings kept the pages; the
  attribution to "other mappings" was an inference (see the note in §6). The part that survives is the plain one:
  the knob is scoped to the pool's own VMA and says nothing about any other mapping.
- **The `0000:04:00.0` reset is untouched and unexplained.** One occurrence in this boot, and its shape recurs
  in earlier boots (**§7**); no coredump analysed, no attempt to attribute it to the pool, the knob, load
  or the card.
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

## 7. Addendum: 692 resets at the reboot, one fence loop since

Added 2026-11-24 from the kernel journal and two logs; bare `journalctl -k` means **this** boot. `R0` /
`R0.elog` = `out/port-ab-20261123/logs/W1-B-pp-r0{-boot.log,.engine.log}`; `elog` unqualified is the §3 one.

| boot | resets | card, `Source:` | first → last |
|---|---|---|---|
| 0 (22:04:09 →) | **1** | `04:00.0`, 4 | 02:02:57 |
| -1 (16:53:07 → 22:02:24) | **692** | `09:00.0`, 1 all | 21:43:15 → 22:02:24 |
| -2 (14:55:10 → 16:52:34) | 2 | `04:00.0`, 4, 4 | 15:55:48, 16:29:23 |
| -3 (12:26:56 → 14:54:32) | 2 | `04:00.0`, 1, 4 | 12:27:13, 14:48:35 |
| -4 | 0 | — | — |
| -5 (22:32:12 → 23:05:29) | 2 | `09:00.0`, 1, 1 | 23:01:26, 23:01:29 |

**The storm.** 692 in 1149 s (465 gaps of 2 s, 198 of 1 s, 21 of 0 s, 7 of 3 s), one block repeated with the fence
never advancing (`page1 1836/1838` 571, `page0 2391/2395` 118, `2391/2403` 3): `ring page1 timeout, signaled
seq=1836, emitted seq=1838` / `GPU reset begin!. Source:  1` / `device lost from bus!` / `GPU reset end with ret =
-19` / `GPU Recovery Failed: -19`. `device lost from bus!` is in the **first** block, so the card was already off the
bus and the other 691 re-ran a reset that could not work (692 coredumps). The SMU failed first — first `0xffffffff`
reply 21:42:16, 59 s ahead, 20,521 `0x10` failures and 30,179 `powerplay` lines that boot — and at 21:42:17 the audio
function too (`snd_hda_intel 0000:09:00.1: Unable to change power state from D3hot to D0, device inaccessible`).
At 21:43:10, seconds before the first reset: `Failed to send message 0x10, response 0xffffffff`, `[powerplay] Failed
to export SMU metrics table!`, `[GetActivityMonitor] Attempt to Set Dram Addr High Failed!`, `[GetPowerProfile] Failed
to get activity monitor!`, `Failed to send message 0x3c, response 0xffffffff`.

The failures sit between boots: `W1-A-pp-r0` was stopped at 21:41:05 (`supervisor-events.log:1`) and its record
closed at 21:42:32, and `W1-B-pp-r0`'s first engine line is 21:43:12. Gen 1 sat at `still starting (880 s)` (R0
7-50) and gen 2 (21:58:16) at `still starting (240 s)` (R0 94) until the reboot cut it;
R0.elog holds no line between one `engine started` and the next (6, 12). The supervisor logged `gen 1 recovering:
helper did not attach and fill before the startup deadline` at 21:58:10
(`run/W1-B-pp-r0.supervisor-events.log:1`). That leg's one `ready:` (R0 116) is gen 3 at 22:22:03, after the
reboot, which served 301.6 tok/s (R0.elog 117): **the engine never became ready during the storm**; the operator
reboot is what ended it (`systemd-logind: The system will reboot now!` 22:02:16, shutdown 22:02:24, next boot
22:04:09).

**The other class, 02:02:57, `04:00.0`, this boot:** `qcm fence wait loop timeout expired`, `The cp might be in an
unrecoverable state due to an unsuccessful queues preemption`, `GPU reset begin!. Source:  4`, `BACO reset`, `VRAM is
lost due to GPU reset!`; that cycle's engine (up 01:59:47) printed `HW Exception by GPU node-1 … reason :GPU Hang`
(elog 298) and `the engine stopped unexpectedly (exit code -6)` (`pool-nohuge-boot.log:125`), and the next boot at
02:04:08 was clean. **One occurrence here, unexplained, on the card that never permission-faults** — the shape
recurs in boot -2 (15:55:48, 16:29:23) and -3 (14:48:35), a `ring page0` variant with `Source: 1` there at 12:27:13.

**What §7 does NOT show.** No link to the permission fault: zero `page fault` records in 21:40-22:03, while that boot
had three permission-fault events 4 h earlier (17:00:04, 17:07:39, 17:21:47; 10 records each, XDMA) — no
co-occurrence, and a gap in time is not a mechanism; SMU/powerplay is another subsystem than VM translation. No
cause: nothing separates power delivery, thermals, a bus fault or a driver path, and 692 identical failures are one
fault, not 692. Nothing about the knob: the storm met `936efcc798aab395…`, which has no `STRATA_POOL_NOHUGEPAGE` string, and the
04:00.0 reset happened *with* the knob and is no permission fault either. No trend: one storm in six retained boots,
the others 0-2. **What would test it:** the fault-differential legs on the fixed binary (see **Next**) and a soak long
enough for a reset to appear on its own.

## 8. Addendum: the 40-minute soak — 2,873 s up, no fault, no reset

Added 2026-11-24. This closes the open item at the end of **§7** ("a soak long enough for a reset to appear on its
own") for this exposure. Same leg and binary, booted again with `faultdiff_run.py --leg pool-nohuge --boot
--waive-nonengine` and the override naming `run/port0218-nohuge-bin/rpcw-hip/strata` (sha
`15d330ea4d0eec4c…`, `/tmp/opencode/soak2-boot.log:3`). Prerequisite 0 opened at 03:49:31
(`prereq0-20261124-034931.json`, `VERDICT: OPEN`); the engine start line is **03:49:34** (**elog 804**) and the
knob line is **824**, with no `RAM pool: WARNING` line in the file and both `expected.present`/`absent` assertions
true (`run/pool-nohuge.state.json`). NUL-stripped elog is now **1,245 lines**; this soak is **804-1,245**
(442 lines).

| what | value | source |
|---|---|---|
| requests | **29** `kind: tg`, ctx alternating 80,000 / 16,000, `max_tokens 1024`, two completions each | `records/pool-nohuge/load/` |
| prefill | **35.7-118.5 s** per request, median **77.4** | `walls[0]` |
| decode | **10.3-27.2 s** per request, median **16.9**, **514.0 s** total | `walls[1]` |
| load wall | **2,482.7 s summed = 41.4 min**; first request 03:50:47, last reply 04:32:13 | `wall_s`; `/tmp/opencode/soak2-load.log` |
| parks / restores | **28 / 29** in this boot's slice — **43 / 45** over the whole file, which still carries §6's 15 / 16 | `grep -c 'parked [0-9]* tokens'`, `grep -c restored` on the stripped elog |
| engine | up **2,873 s** at the last live watch reading; **2,884 s** from the 03:49:34 start line to the 04:37:38 close (47.9 / 48.1 min) | session watch output (not a repo artifact); `state.json: ended_at`, `ran_s` 2886.8 |
| kernel, the soak's own window | **0 / 0 / 0** | `journalctl -k --since '2026-11-24 03:50:00'` → `no-retry page fault` 0, `GPU reset begin` 0, `grep -cE 'no-retry page fault|GPU reset begin|qcm fence|unrecoverable'` 0; state's `kernel_fingerprint.count` 0 over 03:49:31-04:37:38 |
| engine fault lines | **0** | `state.json: engine_fault_count` |

**The monitoring mistake, recorded.** An early check ran `journalctl -k --since '-130 min'` and reported "1 reset" at
04:02. That window opens at ~01:52 and therefore contains the **02:02:57** `0000:04:00.0` `qcm fence`/`Source: 4`
reset of the *previous* cycle (§5.2, §7) — a relative window is not a boot anchor. The shape reproduces exactly:
`--since '-172 min'` (opens ~01:50) returns 1 and it is the 02:02:57 line, while `--since '2026-11-24 03:50:00'`
returns 0. A windowing mistake, not an event.

**Memory.** Engine RSS per record (`max_rss_mib`, VmHWM) stayed **40,640-41,910 MiB (39.7-40.9 GiB)** across
all 29 records; the two live VmRSS samples taken during the soak (04:25, 04:31) read **40.3 and 40.6 GiB**, and
`min_avail_mib` bottomed at **11,214 MiB** with swap peaking at **8,469 MiB** — no cheaper regime than §3's. The
live samples are session shell output, not a repo artifact (the pid, 2146452, is gone; it is the same pid in every
load record's `mem.engine_pid`). The pool's own exclusion was in force the whole time: `RAM pool: … mapping:
transparent huge pages off (MADV_NOHUGEPAGE; STRATA_POOL_NOHUGEPAGE)` (elog 824).

**Against the baseline.** §4's four fatal permission faults landed **251-282 s after boot**. This engine ran
**2,873 s**, **10.2-11.4×** that horizon, through 41.4 min of parking, restoring and disk spill.

**What it does NOT show.** Still **n = 1** for each of the two soaks (four prefills, two soaks, one binary), so
"the knob holds" rests on 6 boots, not on repetition. The traffic is a synthetic alternating filler, sequential,
**not real use**. **No speed claim**: 10.3-27.2 s per request is load traffic with draft-accept variance
(336-1,006 tokens actually generated per decode request, 26.3-39.8 tok/s; 27 of the 29 under 1,000, the bar a tg
rate needs). And the `0000:04:00.0` `qcm fence` class recurs historically (§7: 02:02:57 this boot, 15:55:48 and
16:29:23 in boot -2, 14:48:35 in boot -3), so **40 clean minutes is one sample that it did not appear, not proof it
cannot**.

**What it closes.** The §7 "what would test it" item — a longer soak watching for a reset — is **satisfied at this
exposure** (2,873 s up, 41.4 min of load, 0 faults, 0 resets) and **remains open at longer horizons** and under real
traffic.
