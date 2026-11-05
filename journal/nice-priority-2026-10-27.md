# Strata priority and latest crash — GUAM, 2026-10-27

Read-only investigation; no service restart, priority change, or runtime/config edit performed. User requested no delegation; the interrupted diagnostic child was reconciled as not running. This journal is the only file written.

## Where nice +5 comes from

- Live server: PID 11280, cwd `/mnt/user/dflash2/Strata-integ`, `serve/server.py`, config `/mnt/user/Strata/prod-config-swift15-dflash-w4-q4_0.json`.
- `serve/server.py:3290–3304`, `engine_nice()`: Linux defaults to +5 if ANY KFD GPU architecture is in `HIP_IGPU_ARCHS`, not just an engine-selected GPU. `HIP_IGPU_ARCHS` at line 3403 includes `gfx90c`; KFD node 3 reports `gfx_target_version 90012` (Cezanne).
- `serve/server.py:1033–1036` installs `os.nice(nice)` as a child pre-exec hook on every engine spawn, including automatic recovery. The production launcher itself has no explicit nice invocation.
- Introduced by upstream commit `034422c4`, 2026-10-20, intended to preserve desktop responsiveness on AMD integrated-graphics PCs.
- Current wrapper log `logs/prod-swift15-dflash-w4-q4_0-20261027-181630-11272.log:6,40,485,488,554,557,633,636,688,691` confirms nice +5 at startup/recovery.
- PID 879508 (`strata-host`, engine started 19:17:24 CET) and all sampled threads were nice 0 at inspection. **User confirmed manually renicing the engine to 0.** Do not interpret the live 0 as evidence the startup rule was inactive.
- Persistent override: string `"STRATA_ENGINE_NICE": "0"` in the production JSON's `env` block, effective after the server loads that configuration. Not applied. A manual renice is lost on the next engine restart.

## Latest crash and pressure evidence

- Engine log `logs/engine-swift15-dflash-w4-q4_0-sampled.log:16017–16030`: 60 s zero progress waiting for verify layer 44 (CUDA1's configured layers 32–47); all 24 expert jobs done, 7/7 workers sleeping, host/GPU counters stuck at 32, 0/36 threads in D. Request-start VRAM 790 MiB; 11,746 MiB RAM available; 934 MiB process swap; major faults stayed at 1086 across the two stall samples.
- Wrapper lines 677–691: generation stopped at 304 tokens, then #267 verify failure at layer 44, GPU did not finish within 5 s after waits were released, exit -15. Restart 19:17:08 hit VRAM-held allocation failure; retry 19:17:24 succeeded.
- Kernel journal has no matching amdgpu/OOM/AER/watchdog entry from 19:10–19:18. Earlier this boot, 18:18:25–27, **09:00.0** had a page1 timeout, BACO reset, VRAM loss, and compute-ring fence fallback.
- At 19:25: 62 GiB RAM, about 10 GiB available, 40 GiB swap used. `vmstat 1 3` interval samples showed 77–81% CPU idle, 6% I/O wait, swap-ins 3908 then 280 KiB/s; the first line is boot-average, not an interval. Borg PID 103889 was running/in D intermittently, nice 0. At 19:27 engine RSS was about 38 GiB and process swap about 5.7 GiB. These are pressure indicators, not proof of crash-time swap starvation.
- PSI files are unavailable on this kernel. Current engine I/O priority is unset (`none`), as is Borg's; no I/O priority was changed.

## Interpretation / next step

The automatic +5 policy is a credible contention amplifier and now has an exact source and opt-out. It is not yet a proven sole cause: the latest report is GPU-side non-progress with completed CPU work, and independent kernel GPU resets also exist. Normal CPU priority does not remove RAM, storage, or GPU contention. Persist normal priority first if approved, then observe comparable real workloads; do not extend deadlines or stress-test the GPUs as part of this investigation. No controlled A/B was run.

## User clarification and follow-up

The user subsequently clarified that the GPU errors were deliberately caused by testing. **Do not use those errors as evidence of spontaneous hardware/driver instability for this assessment.** VRAM swap is offline and will remain offline. The approved production-config edit and the memory-only tuning assessment are recorded in `journal/zswap-kernel-review-2026-10-27.md`.
