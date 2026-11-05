# Production nice override and memory-policy review — 2026-10-27

## Scope and user clarification

- User approved adding `STRATA_ENGINE_NICE=0` to the production configs, and requested zswap/kernel-tuning advice only.
- No delegation. No Strata start/restart, priority change, swap operation, sysctl write, kernel/GRUB edit, or GPU manipulation performed.
- User says GPU errors came from intentional testing: exclude them from causal attribution and tuning recommendations. VRAM swap is offline and will remain offline: exclude it from the proposed policy.

## Applied and verified

Added the string `"STRATA_ENGINE_NICE": "0"` to the existing `env` object of all seven production engine configs:

- `prod-config.json`
- `prod-config-swift15.json`
- `prod-config-swift15-original.json`
- `prod-config-swift15-q8ple.json`
- `prod-config-swift15-dflash-w3.json`
- `prod-config-swift15-dflash-w4.json`
- `prod-config-swift15-dflash-w4-q4_0.json`

Historical `.bak-*` files and `prod-config-swift15.shared-settings.json` (request/UI settings, not an engine config) were not edited. All eight `prod-config*.json` files passed `.venv/bin/python -m json.tool`; search verified exactly seven override entries. `git diff --check -- prod-config*.json` passed. Tracked `prod-config.json` also gained a final newline; no other semantic delta in its diff. The override takes effect when a server loads the updated config. Strata was already stopped during the later measurements and was left stopped.

## Observations (CET, kernel 7.2.12-x64v3-xanmod1)

- Running boot command line: `zswap.enabled=0`; first zswap pool creation logged at **19:23:28**, compressor LZ4. Thus zswap was not providing a compressed cache for the previously examined 19:17 crash.
- At 19:33–19:35, live zswap settings: enabled Y, compressor `lz4`, max pool 3% (about 1.9 GiB on 62.2 GiB RAM), accept threshold 90%, shrinker Y.
- 19:33 meminfo: `Zswap=222228 kB`, `Zswapped=220812 kB`: about 217 MiB of pool RAM for 216 MiB of original pages, effectively no net RAM saving in that snapshot (pool overhead/fragmentation included). 19:35 repeated this pattern: 219412 / 217632 kB. These samples were taken after Strata stopped, not during a controlled steady-state Strata workload; they cannot establish its compression ratio under load.
- VM counters at 19:33: `zswpout=6659925`, `zswpin=5187844`, `zswpwb=1175045`: zswap really has accepted pages, served loads, and written pages back. They are cumulative counters, not current rates. Rejection reasons and pool-limit-hit counts could not be read: debugfs is protected and `sudo -n` requires a password.
- Disk swap only: four 32 GiB swapfiles. `/mnt/swap2` on NVMe priority 2; `/home/swap` and `/home/swap3` on `/dev/sdb` priority 1; newly activated `/mnt/swap4` on NVMe priority -1, unused at the sample. No zram or VRAM swap in `swapon --show`.
- Current VM settings: swappiness 100, page-cluster 3, watermark_scale_factor 10, min_free_kbytes 67584, dirty ratios 10/20, compaction_proactiveness 20, vfs_cache_pressure 50, overcommit_memory 0. CPU governor and EPP were already performance; MGLRU enabled (`0x0007`).
- Boot-cumulative activity: 51103 normal+movable direct-reclaim allocation stalls; 58674 synchronous compaction stalls, 32863 failed compactions. These counts identify plausible memory-latency work, not its duration or proof of a particular crash cause. They stopped increasing between the sampled snapshots after Strata stopped.
- THP: globally `always`, defrag `madvise`, PMD pages inherit the global setting. Strata's `src/core/pinned.cu:276–315` explicitly handles global madvise and avoids requesting arena THP when free huge blocks are insufficient; global always is not required by this engine.
- PSI is compiled in but default-disabled (`CONFIG_PSI=y`, `CONFIG_PSI_DEFAULT_DISABLED=y`); `/proc/pressure` is absent. `systemd-oomd` is enabled but inactive, with unit conditions requiring PSI paths: enabling PSI may also enable the userspace OOM-killer's policy, which needs separate review.
- `/etc/default/grub` already says `zswap.enabled=1`, unlike this running boot. Generated `/boot/grub/grub.cfg` was unreadable, so generation/next-boot effectiveness could not be verified. Persistence of the live compressor and pool cap was not established.

## Advice, not applied

1. Do not enlarge the zswap pool or switch to a heavier compressor from these snapshots. Keep the small fast LZ4 experiment while measuring an actual Strata+backup workload at nice 0. Quantized weights are generally poor compression candidates; zswap can still help other application pages, but is not extra physical RAM. Need under-load compression savings, fault/reclaim rates, and root-readable limit/rejection counters before selecting a bigger cap.
2. If retaining the current zswap experiment, explicitly persist `zswap.enabled=1 zswap.compressor=lz4 zswap.max_pool_percent=3`; merely enabling it does not establish persistence of the other live choices. Retain current hysteresis/shrinker initially.
3. First memory-latency trial: boot `transparent_hugepage=madvise` and set THP defrag policy to `defer` (the latter is a separate sysfs setting). This avoids synchronous THP allocation compaction in ordinary fault paths while preserving opportunities for explicitly requested huge pages. Measure startup/decode trade-offs; no guaranteed speedup claimed.
4. A separate moderate trial is swappiness 60 instead of 100: disk-backed swap with almost no observed compression savings should not automatically be treated as cheaper than file-cache paging. Do not use 0 as a universal fix. This can trade anonymous-memory residency against file-cache hits, so compare real workload behavior.
5. If direct reclaim still climbs, a later `vm.watermark_scale_factor=100` trial can wake background reclaim earlier than current 10; it does not create RAM and may increase background I/O. Do not simultaneously alter dirty limits, page-cluster, compaction_proactiveness, or other unrelated knobs without new evidence.
6. `psi=1` is valuable for diagnosis, not itself a performance fix. Review the enabled systemd-oomd policy before enabling it. Keep the already active CPU performance policy and avoid adding unrelated GPU/PCIe knobs.
7. Change one performance policy at a time. No forced load tests, swapoff/drop-caches experiments, or conclusions based on the deliberately induced GPU errors.

## Documentation checked

- https://docs.kernel.org/admin-guide/mm/zswap.html
- https://docs.kernel.org/admin-guide/sysctl/vm.html
- https://docs.kernel.org/admin-guide/mm/transhuge.html
- https://docs.kernel.org/accounting/psi.html

## Production restored at user's subsequent request (19:46–19:48 CET)

- Port 8009 and the production server/engine were absent before starting. Ran the existing `launch-prod-swift15-dflash-w4-q4_0.sh` with the updated production config; did not replace an existing instance.
- Startup log: `logs/prod-swift15-dflash-w4-q4_0-20261027-194625-1298360.log`. Server PID 1298488; engine PID 1298872. Launcher inherited nice 0.
- At 19:47:51, `GET http://127.0.0.1:8009/health` returned HTTP success, `status: ok`, `loaded: true`, model `swift-1.5-iq3_xxs`, context 262144. `/v1/models` also listed the loaded model. Listener is restricted to 127.0.0.1:8009.
- All 19 engine threads present at readiness had nice 0. No generation/load test performed; production left running. No kernel, VM, zswap, or swap settings changed.

## Loaded-model follow-up (19:52 CET)

- Read-only snapshot requested to refine the tuning advice. Production health still reports `loaded: true`; no settings changed and no load test run.
- Zswap is now storing 2088320 KiB of original pages in 944812 KiB of pool memory: **2.21:1 effective ratio, about 1.09 GiB RAM saved**. This supersedes the near-1:1 stopped-process snapshots as evidence of current utility; it still does not establish a steady-state workload hit rate.
- Live settings remain LZ4, 3% pool cap, shrinker enabled. Current pool is below its approximately 1.86 GiB cap. Running boot command line still contains `zswap.enabled=0`, although zswap is enabled at runtime; verify persistence separately.
- About 39.7 GiB swap space is allocated, but `SwapCached` is 27.1 GiB: do not equate allocated swap with that much memory absent from RAM. MemAvailable is 11.1 GiB.
- Three one-second vmstat intervals: 144–332 KiB/s swap-ins, no swap-outs, 76–78% CPU idle, 6% I/O wait. Direct-reclaim, compaction-stall and failed-compaction counters did not increase over this brief interval. Boot totals remain high (106384 direct-reclaim allocation stalls; 90037 compaction stalls). No sustained swap thrashing established by this sample.
- Priority mismatch now matters: `/mnt/swap2` (NVMe, priority 2) is effectively full; `/home/swap` and `/home/swap3` (`/dev/sdb`, priority 1) hold about 7.7 GiB together, while `/mnt/swap4` (NVMe, priority -1) remains unused. Recommend putting both NVMe swapfiles ahead of `/home` at the next maintenance window; do not swapoff a large live allocation to force this change during production.
- Revised tuning priority: keep useful LZ4 zswap and its 3% cap; persist those choices explicitly; fix disk-swap ordering; separately trial THP `madvise` with defrag `defer` to target allocation latency. Given the improved compression evidence, retain swappiness 100 initially rather than recommending 60 without comparative measurements. Leave other VM knobs unchanged initially.
- `psi=1` remains a diagnostics recommendation only, conditional on reviewing systemd-oomd policy first. No GPU-error attribution or VRAM-swap proposal is part of this assessment.

## Current production process ownership

- Launched from this assistant session using the existing production launcher. OS owner is `user` (UID 1000).
- Server PID 1298488 is its own session/process-group leader, has no controlling terminal, reads stdin from `/dev/null`, and writes stdout to the recorded production log. Parent PID 7361 is the user systemd manager; this is adoption of the detached process, not a dedicated Strata service or restart policy.
- Engine PID 1298872 and vision PID 1298634 are children of the server. Server and engine are nice 0.
- Despite `setsid`/`nohup`, the server remains in the originating `app-org.gnome.Terminal.slice/tmux-spawn-436f3a60-05e7-4499-9a1c-662dbdf38e61.scope` cgroup. Detachment does not remove that cgroup membership. Ownership inspection changed nothing.
