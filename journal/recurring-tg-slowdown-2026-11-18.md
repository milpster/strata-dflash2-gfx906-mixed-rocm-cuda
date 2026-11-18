# Generation slowdown recurs without gaming — 2026-11-18

## Report and preservation

Operator: “it seems the tg speed crash happened again, without any gaming at all, its active now as visible on the agents tps tg”. Exact onset and a numeric current rate were not supplied. This reopens investigation of a recurring slowdown; it does not reactivate the parked game experiment. A game is not a necessary trigger if the reported no-game condition holds.

Parent preserved the running process and collected passive evidence only. No API request, restart, pressure injection, cache advice, policy change, benchmark or candidate deployment occurred. The existing restore-checker fixer remained the sole specialist; another investigator must wait for it to finish. GPU acceptance is paused.

Evidence: `bench/ab-v0210/rpcprof/out/tg-no-game-20261118-194948-2107669/`. Times are CET (`+01:00`). `capture-manifest.json` records commands, handles, outcomes and limitations. Observations started at **19:49:48**, not an invented crash-onset time.

## Observed facts

| Evidence | Observation / limit |
| --- | --- |
| Canonical identity | PID **42016**, `/proc` start ticks **19625**, canonical engine SHA `b0ec0109…`, on-disk config SHA `0d9fb58b…`. The approved RAM candidate is not serving. Start/end process records and live executable hashes were retained. |
| Engine swapped memory | `VmSwap` **3999664 kB** at 19:49:48, **4529456 kB** at 19:52:59: approximately **3.81 → 4.32 GiB**. RSS was 54262060 → 53307376 kB. These are process snapshots, not rates or an allocation-level explanation. |
| Host memory | `MemAvailable` **16689484 → 17299536 kB**, approximately 15.9 → 16.5 GiB. Available RAM includes reclaimable cache and does not establish a paging-free working set. |
| Interval paging, 19:50:37–19:51:37 | Excluding vmstat's since-boot row, swap-in reached **129928 KiB/s** (~127 MiB/s) and swap-out **168336 KiB/s** (~164 MiB/s), at different samples. Most earlier samples had little swap traffic; a burst starts around 19:51:29. Do not label the whole minute a continuous swap storm. |
| Engine CPU and I/O | pidstat records 30 samples each for engine/helper. Engine CPU was commonly roughly 700–800% with substantial disk reads; high read throughput alone does not distinguish explicit model/expert reads, file refaults and swap I/O. No individual host paging burst is attributed solely from these rates. |
| Engine-scope cgroup counters | At 19:51:46 and 19:52:59, `pswpin` **19877323 → 20407803**, `pswpout` **23143209 → 23757680**. This places logical paging activity within the engine/wrapper scope as well as the host. Counter deltas are not asserted as physical disk bytes or attributed to a particular allocation. |
| Limits / OOM | Every readable engine ancestor had `memory.max=max`, `memory.high=max`; immediate scope `memory.min=51539607552` (48 GiB). Immediate `memory.events` OOM counts were zero. Ancestors contained one cumulative OOM/kill, with no timestamp or delta establishing it occurred in this capture. Root `memory.current`/limit files and PSI were unavailable. |
| GPU state | Explicit card2/card3/card4 samples show changing clocks/busy state, often one serving card busy and the other idle; neither a permanently stuck clock nor a healthy pipeline follows. Card3 had only about 207 MiB VRAM free. Card2 GTT usage (~1431 MiB) is an observation, not proof of driver paging. iGPU samples were 5–6% busy. |
| Request-summary limitation | Shared engine-log offset remained **4263837** through 19:52:59. No completed-request summary appeared during the capture. The retained historical prelude's 18–23 tokens/s summaries cannot be used as the current crash's rate or a matched baseline. No extra request was sent to force a summary. |

## Collection and validation

`vmstat -w -t 2 31` and the 30-interval pidstat command exited **0**. The 31-sample sysfs GPU collector exited **0**. The engine stream ended at its intended 75-second deadline (tail exit **124**, wrapper exit **0**), not because the engine exited. Initial, cgroup/policy and post-interval snapshot commands exited 0; the initial broad DRM glob also matched connector directories and retained optional read errors. The later sampler used explicit valid device paths. Missing counters were reported as unavailable, never zero.

Start/end supervisor records, exact engine-log offsets, raw samples and command results are preserved. A separate SHA256 seal covers this directory after collection; it does not modify earlier game-era evidence. Offline completeness/identity checks are separate from diagnosing the slowdown.

At **19:59:05 CET**, parent independently checked supervisor identity, the 31 vmstat rows (one since-boot row plus 30 intervals), 30 pidstat samples per observed process, 31 GPU samples, collector outcomes, unchanged log offsets, both captured live executable hashes and the captured config hash. The focused check exited **0**; see `passive-capture-validation.log`. `sha256sum --check final-evidence.sha256`, run from the capture directory, then exited **0** with all **18** sealed files `OK`. This establishes saved-artifact completeness and integrity, not a slowdown cause or a current speed measurement.

Raw `.log` files require explicit force-add under this repository's ignore rules. The unrestricted staged whitespace check exited **2** on trailing whitespace emitted by `/proc` and GPU sysfs in `start-snapshot.log`; those sealed bytes are deliberately preserved. Prose changes are checked separately, without rewriting diagnostic output.

## Next investigation

After the active fixer finishes, reconcile its artifacts without a live restore, then assign one read-only investigation of this recurrence. Correlate existing request timing/cache activity, engine-scope paging, model/file working-set reads, GPU memory pressure and stage balance. Do not preselect gaming, swap allocation, a clock fault or cache parking as the cause. Preserve the current process unless the operator explicitly requests recovery. No configuration/precision/context/tolerance change or promotion is authorized by this observation.

## Operator-requested restart

Operator subsequently requested: “restart the engine and restore agent”. Parent cancelled and retained fixer session `ses_e1040f2f0ffejvpXUnixEx6qC8`; cancellation was acknowledged before any production stop. Its unfinished files remain untouched and uncommitted.

Evidence: `bench/ab-v0210/rpcprof/out/operator-restart-20261118-202211-3089242/`. At **20:22:11 CET**, preflight exited **0**: pinned canonical engine/config hashes matched, tracked launcher/config were unchanged, current process identities were captured, and only port 8009 was listening. The stopped historical trial supervisor PID was absent. The pre-stop canonical engine was still PID 42016, with `VmSwap=2868596 kB`; this later idle snapshot does not negate the earlier captured paging bursts.

Parent ran the existing `champions_stop.sh` wrapper, not the unfinished restore checker. Its stop-all delegate exited **0**, reported **ALL STOPPED**, and all five old pair processes were absent with port 8009 free at **20:23:05 CET**. The separate canonical `launch-v0238.sh` invocation detached successfully at **20:23:42 CET**. A bounded live-output watcher followed the supervisor's current generation and its own wrapper log; it exited **0** on generation-1 readiness at **20:25:30 CET**. No candidate, configuration, precision, context capacity or cache policy was changed. This restart is operator-authorized recovery, not a GPU acceptance run or a test of the unfinished restore repair.

At **20:26:35 CET**, independent parent verification exited **0**: supervisor **3131096**, generation **1**, engine PID **3132535**, start ticks **863425**, live executable path and full canonical SHA, current-generation readiness, models HTTP **200**, and a real completion HTTP **200** returning **OK**, `finish_reason=stop`, **28 completion tokens including reasoning**, **3.994894 s** total. The supervisor/engine/start/log identity matched before and after the request. Post-request engine `VmRSS=38319388 kB`, `VmSwap=195364 kB`; host available RAM was 20425 MiB. This is an availability check, not evidence of normal sustained speed or a matched before/after benchmark. The differing completion length also prevents treating it as a repetition of the earlier 256-token probe.

Only after those checks did parent resume the same retained fixer on its original bounded offline scope. No second specialist was launched. The snapshot and seal exclude `launcher.log`, which remains the active supervisor's stdout sink; `launcher-snapshot.log` preserves its captured contents without pretending a live file is immutable. Earlier no-game and game-era evidence remains unchanged. Slowdown investigation and GPU-acceptance prerequisites are still open.

From the restart evidence directory, `sha256sum --check final-evidence.sha256` exited **0**, with all **20** static files `OK`; a separate check confirmed the mutable launcher file is excluded. Raw diagnostic logs retain their original whitespace and are not subject to prose whitespace cleanup.

## Read-only investigation dispatched after the repair

Restore-checker repair `95b99a7` passed parent review and the 53-test CPU file; no live restore was used to test it. Before assigning another specialist, parent freshly verified canonical serving at **21:25:06–21:25:09 CET**: live engine/config hashes, current ready log, real HTTP **200** completion **OK** / stop / **28 tokens including reasoning**, **3.369719 s** total, unchanged supervisor **3131096**, generation **1**, engine **3132535**, start **863425**. Command exit **0**; evidence is `out/slowdown-investigation-20261118-212506-865959/`. This check is not a sustained-speed benchmark.

The sole explorer `ses_e0fa9abbcffeL15aq5Oiy3C4W9` is examining saved evidence and canonical source only. No file writes, probes, tests, workloads, process operations or policy changes are allowed. Its output must separate observations, source-backed mechanisms and hypotheses, then identify the smallest missing discriminator. Parent owns reconciliation and any subsequent authorization. Game testing remains parked and GPU acceptance paused.

## Parent reconciliation in progress

The first specialist report is **provisional and not accepted as a causal finding**. Parent requested corrections in the same retained session: separate shared-log engine lifetimes and untimestamped request rows; verify whether disk page-release totals are cumulative; correct vmstat interrupt/context-switch columns and mixed GPU timing; distinguish reclaim protection from allocation/admission limits; and avoid interpreting CPU doorbell wait as proof of a GPU/HMM fault. A large footprint and small admission margin alone do not demonstrate an implementation defect.

The saved availability responses also contain explicit decode timing: the 20:26 response reports **17.8 tokens/s**, and the 21:25 response **20.4 tokens/s** for their respective 28-token completions. Dividing tokens by total HTTP time would wrongly include prefill and must not be called generation throughput. These short probes still do not establish sustained recovery or a matched before/after result. No new request or production operation was performed for this review.

## Further recurrence and direct GPU eviction evidence

Operator subsequently reported that the engine had slowed again and suggested the approximately 1.5 GB free-VRAM reading might be a signature. At **22:27:23 CET**, parent saved a passive snapshot under `out/tg-recurrence-20261118-222722-2700212/`: the same canonical supervisor **3131096**, generation **1**, engine **3132535**, start **863425**, live engine/config hashes unchanged. Engine `VmRSS=55080712 kB`, `VmSwap=2661140 kB`; host available RAM **14434 MiB**. Card2 free VRAM was **1493852160 bytes** (~1.39 GiB); card3 **188825600 bytes** (~180 MiB). No inference probe was sent. Last saved completed-request summaries were still **17.4–19.0 tokens/s**; they do not quantify an unfinished request or timestamp the new reported onset.

Operator then requested: “restart the engine and re-send the agent otherwise itll never finsih”. Parent cancelled and retained the sole explorer, with cancellation acknowledged before production operations. Before restarting, parent also answered the follow-up: “can you find out if any of the drivers of the gpus started swapping vram to ram?”

The **22:28:37 CET** read-only driver snapshot supplies stronger evidence than aggregate GTT usage alone:

| Engine DRM client | Requested VRAM | Resident VRAM | `amd-evicted-vram` | Resident GTT |
| --- | --- | --- | --- | --- |
| card2, PCI `0000:04:00.0`, client 605 | 16743428 KiB | 15291396 KiB | **1452032 KiB (~1.38 GiB)** | **1454148 KiB (~1.39 GiB)** |
| card3, PCI `0000:09:00.0`, client 606 | 16463320 KiB | 16463320 KiB | **0 KiB** | 2116 KiB |

Both devices bind to `amdgpu`. On card2 the requested-minus-resident VRAM difference exactly matches the driver's evicted-VRAM field, and almost all resident GTT corresponds to that difference. This establishes the driver reports evicted engine VRAM allocations with associated system-memory residency at observation time. It does **not** establish eviction onset, migration direction/rate over time, repeated per-token movement, or causal responsibility for the slowdown. There is no matched healthy per-process fdinfo baseline yet.

Raw fdinfo and sysfs values are in `pre-restart-driver-snapshot.log` (snapshot exit **0**). Direct dmesg access failed with **1**, `Operation not permitted`; debugfs was inaccessible. The readable kernel-journal slice contains no GPU fault/reset/eviction record, which does not rule out ordinary unlogged migration. The documentation fetch tool also failed with missing `jsdom`; the positive observation above relies on saved live counter values, not a successful documentation lookup.

Parent ran the reviewed canonical restore harness with `--health --poll 5 --out <this-capture>/restart` after pinned input checks and cancellation acknowledgement. This is the user's requested recovery, not a GPU acceptance run or a restart solely to test the checker. It exited **0** at **22:32:29 CET**: **ALL STOPPED**, unchanged canonical/config pins, generation **1**, engine **2789182**, models accepted and a real **OK** / stop / **28-token** completion. The saved response reports **20.1 tokens/s** decode; it remains a small availability check, not proof of sustained recovery.

At **22:33:16 CET**, independent parent checks matched the stored real completion, canonical live path/SHA and config, new supervisor **2788001**, generation **1**, engine **2789182**, start **1624418**, with identical placement identity before/after the read-only snapshot. Both card2 and card3 now report **0 KiB `amd-evicted-vram`** and **2116 KiB resident GTT** per engine client. Requested and resident VRAM are equal on each card. Card2 device free VRAM is **45035520 bytes (~43 MiB)**, card3 **340787200 bytes (325 MiB)**. This provides a post-restart no-eviction baseline, not a controlled causal comparison; buffers, context and workload differ from the pre-stop state. Engine `VmSwap=19468 kB`, RSS **38444052 kB**. Snapshot exit **0**.

Only then did parent resume the same retained explorer, prioritizing the driver's eviction/residency field semantics, the limits of the new before/after observation and a minimal passive discriminator, alongside corrections to the earlier provisional report. No second specialist was launched. The live launcher stdout `restart/logs/restore-20261118-223003-2774354.out` must stay outside the evidence seal; `restart-launcher-snapshot.log` is its immutable captured copy.

Parent's offline artifact validation exited **0**: manifest consistency, exact **4422877-byte** shared-log prefix, unchanged pre-restart identity between snapshots and unchanged post-restart identity across the residency snapshot. The original champion seal also passed for all **9** files after the separate-output restore. `sha256sum --check final-evidence.sha256`, run from this recurrence directory, exited **0** with all **28** static files `OK`; the mutable launcher sink is explicitly excluded. No further endpoint request was used for these artifact checks. Raw logs retain their original bytes; source/prose whitespace checks are separate.

## Reconciled driver semantics and assessment

The retained explorer finished its bounded read-only review. Parent independently checked the actual snapshot artifacts, host module metadata, canonical source warnings and the relevant upstream accounting code. Host kernel is **7.3.4-x64v3-xanmod1**; the in-tree amdgpu module reports srcversion **D9E308BDF3BCCD5917640D1**. The exact installed driver's source tree was not available to the investigator, so upstream semantics below are a consistency check, not a verified diff of the running XanMod module.

Upstream [`amdgpu_show_fdinfo`](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/amd/amdgpu/amdgpu_fdinfo.c) prints `amd-evicted-vram` from `stats[TTM_PL_VRAM].evicted`. [`amdgpu_vm_update_stats_locked`](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/amd/amdgpu/amdgpu_vm.c) adds/subtracts a buffer's size when its current placement is outside its preferred domains; resident bytes are separately accounted by actual placement. `amdgpu_vm_get_memory` copies those current statistics under the stats lock. Thus this is a **current placement gauge, not cumulative migration traffic**. It cannot reconstruct onset, move count or movement rate. Requested-minus-resident matching evicted bytes in these samples is accounting consistency, not an independent movement measurement or a universal equality for every possible placement policy. The separately observed GTT residency supplies the system-memory corroboration.

| Supported conclusion | Remaining limit |
| --- | --- |
| Card2's engine client reported **1.38 GiB** of VRAM-preferred allocations outside VRAM, with matching excess GTT residency; card3 did not. | The snapshot does not identify which buffers were hot or what triggered placement. |
| After the separately authorized restart, both clients reported **zero evicted VRAM**. | Different contexts, buffers and workload prevent a controlled causal or sustained-speed comparison. |
| Canonical `42cb41b314:src/program/generate.cpp:2207–2210` and `:19044–19049` explicitly describe driver spill to system RAM as a possible few-tokens/s / stalled-window failure mode. | An existing application warning is not proof that this occurrence has the same cause. No reserve, context or quality setting was changed. |
| The earlier slow-era capture also had elevated device GTT, but no saved per-client eviction gauge. | Do not retroactively claim direct engine eviction proof for that earlier boot from aggregate GTT alone. |

**VRAM spill is now the leading concrete lead, not an established slowdown root cause.** Host paging remains a co-occurring possible contributor; memory protection/admission policy is background context, not a demonstrated defect.

### Corrected request attribution

Parent independently extracted raw byte offsets **4263837–4275504** from the later preserved full shared log (`engine-start.log`), using the no-game end offset and the **20:22:11** pre-stop offset. This **11668-byte** suffix contains **10** completed-request rows at **5.3–8.3 tokens/s**, with average per-window CPU doorbell waits **297.18–501.26 ms**. These rows were appended after the unchanged **19:52:59** capture offset and before the later pre-stop snapshot. They supply quantitative follow-up slow completions, without inventing onset timestamps or contradicting the absence of a completion during the initial passive capture.

The corrected specialist report still misplaced one row: the `conv-25` park and its `47943 MiB released` total **are inside this suffix**, after several already-slow completions. That ordering does not establish a triggering park. The printed release values are `dc.stats().pages_released(_bytes)` (canonical `generate.cpp:14855–14863`), tier page-cache release statistics whose byte values describe advised file sizes (`conversation_disk_cache.hpp:107–109`), not one park's resident-RAM footprint. The original claimed 106k→157k onset progression and zero-headroom line attribution remain withdrawn because shared-log lifetimes/times were not established.

The other retractions stand: vmstat's million-scale numbers were interrupts, not context switches; GPU idle/busy samples do not consistently align with the paging burst; `memory.min` is reclaim protection, not a forced cap; different availability instruments do not prove a guard bug; CPU doorbell wait does not prove a GPU/HMM fault; and total HTTP latency is not decode time. Parent's pinned-source/offset checks exited **0**; independent upstream source retrieval succeeded via the available GitHub tool and jsDelivr fallback after the earlier documentation-tool failure.

The smallest next discriminator is a bounded passive time series of engine-identity-bound DRM residency plus existing request-log offsets/timestamps during ordinary workload. Rediscover DRM clients by PCI identity rather than hardcoding fd 7/8 across restarts. Do not send synthetic requests, alter driver controls or change configuration. A new monitor has not been launched by this report; its scope/duration should be agreed before proceeding. GPU acceptance remains paused and the future game experiment parked.

## Approved passive monitor — preparation

Operator selected **“Run monitor (Recommended)”** for a **one-hour**, **30-second interval** passive monitor during ordinary workload. Approval is limited to reading residency counters and existing request output; it does not authorize synthetic requests, driver/config changes, restart, gaming or GPU acceptance.

The sole fixer `ses_e0f3fc15fffexiXw0Y8LeQw71Z` is preparing a small reusable stdlib collector, `gpu-rounds/watch_gpu_residency.py`, and isolated CPU tests, `tests/test_gpu_residency_cpu.py`. It may not run against live production. Parent owns review, final focused checks, the actual monitored hour and closeout. This is a passive observation helper in the existing harness, not a new benchmark framework.

Verification budget: exercise actual collection against scratch state/proc/sys/log fixtures; check identity/start binding and duplicate DRM descriptors, memory units/unavailable values, new-log-offset exclusion and reset handling, fresh output and bounded timing. Parent will compile the two files and run only the dedicated stdlib test file after reconciliation, then inspect safety and evidence formats before launching. Live progress must be visible, with raw residency/log-increment evidence retained in a fresh directory. Source log byte-observation times only bound when completions were noticed; they are not invented request timestamps. The hour **has not started yet**.

The fixer's first terminal response contained only non-executed tool-call markup. Parent checked both required paths and confirmed neither file existed, then resumed the same retained fixer with instructions to use actual tool calls and report a precise blocker if execution is unavailable. No implementation, passing test or running monitor is inferred from that incomplete response; the hour remains unstarted.

The second terminal response again emitted tool-call text instead of executing it; parent confirmed no collector files existed. Rather than repeat that failed handoff, the same fixer is now asked to return a literal two-file patch as its implementation artifact. Parent will apply it with actual tools and own **all** execution/validation; the specialist must not claim it ran tests. This changes only the delivery method, not the approved passive scope. The reason for the malformed specialist output is not diagnosed, and production/model configuration has not been changed to work around it.

## Passive collector checked and running

The fixer returned a literal patch without claiming execution. Parent review found launch-blocking timestamp handling and selection of the wrapper log instead of `engine_stderr_log`, plus a backlog test that contradicted the read cap. Parent applied a corrected implementation and retained fixture coverage for these cases, finite timing (including rejecting NaN/infinity), monotonic deadlines, complete supervisor/PID/start/log identity, duplicate DRM descriptors, missing counters, partial request lines, NUL-preserving raw increments and log resets. No collector was launched from the unreviewed patch.

Final parent checks from `bench/ab-v0210/rpcprof`, completed **23:57:57 CET**:

| Check | Result |
| --- | --- |
| `python3 -m py_compile gpu-rounds/watch_gpu_residency.py tests/test_gpu_residency_cpu.py` | Exit **0** |
| `python3 tests/test_gpu_residency_cpu.py` | Exit **0**, **15 tests**, **0.081 s**, `OK` |
| LSP, final collector and unchanged test file | **No diagnostics** |

Evidence: `out/gpu-watch-checks-20261118-235657-1190002/`. The initial passing test run preceded small type-diagnostic edits and is superseded by `final-checks.log`. Source hashes matched before/after final checks. The tests use only scratch proc/sys/state/log fixtures under `out/tests-tmp`; no hardware performance or full-repository result is claimed.

After read-only canonical live-image/config/start checks, parent launched:

`python3 -u gpu-rounds/watch_gpu_residency.py --out out/gpu-residency-watch-20261118-235841-1238437 --duration 3600 --interval 30`

The actual start is **2026-11-18 23:58:41 CET**, scheduled end **2026-11-19 00:58:41 CET**. Background shell `sh_1f0e23e38001hTgJ9LBTqOBIui` streams progress and newly observed request summaries in the terminal. Initial supervisor **2788001**, generation **1**, engine **2789182**, start **1624418** matched; initial shared-log offset **4470351** excludes all older rows. Both Radeon clients initially report **zero evicted VRAM**, each with **2116 KiB resident GTT**. The first sample and launch metadata were inspected independently; the monitor is actually running, not merely detached without evidence.

The all-node scan reports **20 unavailable counters** on other/virtual DRM nodes (`evdi.*` and PCI `0000:01:00.0`). These are retained as null plus read errors; they are not GPU fault reports or zero-valued readings. The engine's three AMD clients and both target Radeon device counters are readable. This limitation does not invalidate those Radeon samples and does not establish anything about the unobserved device's residency.

No synthetic prompts, API probes, restart, driver or configuration changes were added. Ordinary workload must supply useful active observations; idle/no-eviction samples alone cannot test the slowdown hypothesis. Live capture/console files remain unsealed until completion. The static validation/launch record can be sealed separately. Root cause, RAM acceptance prerequisites and the parked game experiment are unchanged.

Operator then requested **“delegate”**. Parent assigned sole read-only explorer `ses_e0f18fb21ffelV0KX92IJwaEky` a bounded collector/interim-evidence review; it must not edit the running collector, rerun valid tests, send probes or wait/poll for the hour's completion. Parent owns reconciliation and closeout. The old fixer unexpectedly reopened after its patch had already been delivered; lifecycle inspection reported it running, so parent cancelled that obsolete run with acknowledgement to restore the one-specialist constraint. No production stop/restart accompanied cancellation. Any inference work from that unexpected reactivation is not independently quantified and must not be presented as a controlled workload.
