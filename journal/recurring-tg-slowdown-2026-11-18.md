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
