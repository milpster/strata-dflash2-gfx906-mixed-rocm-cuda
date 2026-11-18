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
