# The amdgpu permission fault: the pool stays mapped, the GPU's view is revoked — 2026-11-24

Nothing here was measured by me: this records a diagnosis already run, read-only. No boot, no GPU work, nothing
touched `:8009`, no push. Branch `recovery-startup-watchdog`, box GUAM, card `0000:09:00.0` (Radeon VII,
gfx906, `strata CUDA1`); one boot covers the night (`who -b`: 2026-11-23 22:04).

**elog** = `tr -d '\000' < out/faultdiff-r1/logs/default-margins.engine.log | cat -n` (614 lines at 01:14:23);
**klog** = line numbers of `journalctl -k`; "cold prefill" = one request of the 58,423-token corpus
(`corpus/pp239k.ctx323200.bin`, sha `210b16b3…`) on a freshly booted engine.

## The signature, and how constant it is

One record whole — klog 2922-2931, the 00:49:26 event:

| Field | Value |
|---|---|
| message | `amdgpu 0000:09:00.0: [mmhub0] no-retry page fault (src_id:0 ring:24 vmid:8 pasid:1256)` |
| process | `Process strata pid 899926 thread strata pid 899926` |
| page | `in page starting at address 0x00007f422bd93000 from IH client 0x12 (VMC)` |
| status | `VM_L2_PROTECTION_FAULT_STATUS:0x00800031`, `MORE_FAULTS: 0x1` |
| client | `Faulty UTCL2 client ID: XDMA (0x0)` |
| errors | `PERMISSION_FAULTS: 0x3`, `WALKER_ERROR: 0x0`, `MAPPING_ERROR: 0x0`, `RW: 0x0` |

Ten records per engine event. `journalctl -k | grep -o 'Process [a-z-]*' | sort | uniq -c`: `40 Process strata`,
`90 Process hip-fault-repro` (the positive control, `journal/gpu-fault-repro-positive-control-2026-11-24.md`); by hub
40 `[mmhub0]` vs 90 `[gfxhub0]`, by client 40 `XDMA (0x0)` vs 74 `TCP (0x8)` — the classes never mix. The engine
sees `Memory access fault by GPU node-2 … on address 0x7f422bd98000. Reason: Page not present or supervisor
privilege.` (elog 348), then aborts (`default-margins-boot.log:236`, `exit code -6`).

## Every fatal event, one shape

| # | time | pid | boot → fault | klog | build | margins | prefill tail |
|---|---|---|---|---|---|---|---|
| 1 | 22:32:16 | 422387 | 22:27:40 → 276 s | 1759 | fork `4b8251e9` | 256/192 | `W1-A-pp-r1-boot.log` 35-39 |
| 2 | 22:48:21 | 784900 | 22:43:50 → 271 s | 1901 | port `936efcc7` | 256/192 | `W1-B-pp-r1-boot.log` 38-41 |
| 3 | 23:29:49 | 1756818 | 23:25:38 → 251 s | 2010 | port `936efcc7` | 700/700 | `W1d-B-pp-r0-boot.log` 37-38 |
| 4 | 00:49:26 | 899926 | 00:44:44 → 282 s | 2922 | port `936efcc7` | 700/700 | `default-margins-boot.log` 235-236 |

Each is preceded by `reading the prompt: 58,418 of 58,423 tokens` and followed by the abort; no leg died at another
token count. **Corrected: four events, not three** — 23:29:49 is the fourth, same class (10 records, XDMA,
`RW: 0x0`); boot→fault is **251-282 s (4.2-4.7 min)**, not 4.0-4.5.

## What was ruled out

| Candidate | Evidence | Verdict |
|---|---|---|
| VRAM margins | 2 of 4 cold prefills faulted at 256/192 (clean: 301.6 tok/s `W1-B-pp-r0` elog 117, 290.8 `W1-A-pp-r1` elog 189); 2 of 5 at the engine's 700/700 default (clean: 287.4 elog 103, 294.4 elog 211, 262.8 `W1d-A-pp-r0` elog 101) | margin-independent here |
| Engine build | fork `4b8251e9e06334a8` and port `936efcc798aab395` (`boots/W1-A-pp-r1.json`, `boots/W1-B-pp-r1.json`) both took the identical record | not a build regression |
| ECC | `MEM ECC is not presented.` for 0000:09:00.0 and 0000:04:00.0 (klog, 22:04:11); `rocm-smi --showrasinfo` = `UMC DISABLED`, `GFX ENABLED 0/0` on GPU[0] and GPU[1] | no ECC surface exists |
| "the app unmapped the buffer" | the faulted VAs sit in a VMA mapped in 67 of 67 snapshots; the process had `Swap: 0 kB` | CPU mapping was alive |

## The decisive observation: the VMA never moved

`bench/fault-va/maps_sampler.sh` copies `/proc/<pid>/maps` and eight `smaps_rollup` keys every 3 s; on pid
899926 it produced 67 snapshots, 00:46:03 → 00:49:24 (`/tmp/opencode/maps-sampler.log`, `bench/fault-va/run/`).

| Fact | Value | Source |
|---|---|---|
| faulted VAs (klog) | 10 addresses, `0x7f422bd93000` … `0x7f422bdb7000`, plus HIP's `0x7f422bd98000` | klog 2924, 3014; elog 348 |
| spread | 11 addresses inside 147,456 B (144 KiB) | subtraction |
| containing VMA | `7f3d64800000-7f4564800000 rw-p 00000000 00:00 0` — anonymous, no path | `bench/fault-va/run/maps-1795477764.txt:10` |
| its size | 0x800000000 = **32.0 GiB** | same line |
| present in | **67 of 67** snapshots, same line | `grep -h … maps-*.txt \| sort \| uniq -c` → 67 |
| engine's pool | `pool_bytes=34359738368` (= 32.0 GiB) | elog 289 (also 26, 134, 593) |
| process, last snapshot | `Rss 42209440`, `Anonymous 41518216`, `Swap 0` kB | `…/run/rollup-1795477764.txt` |

The 32 GiB anonymous pool VMA was mapped, unchanged, two seconds before the fault, and nothing of the process was
in swap. The GPU's translation of a *present* host page was revoked — exactly what `PERMISSION_FAULTS: 0x3` with
`RW: 0x0`, `WALKER_ERROR: 0x0`, `MAPPING_ERROR: 0x0` states. The clean arm shows the same VMA shape
(`run3/maps-1795478582.txt:10`).

## The confirming experiment

`bench/fault-va/page_stability.sh stable` (sudo at 00:56:10 per `journalctl`; saved state `/var/tmp/page_stability.saved`)
set `transparent_hugepage/enabled=never`, `defrag=never`, `khugepaged/defrag=0`, `vm.compaction_proactiveness=0`,
`vm.swappiness=0` (`page_stability.sh:64-68`). `bench/fault-va/cycle_boots.sh` then booted fresh engines, one cold
prefill each.

| boot | prefill | tok/s | faults | elog | record |
|---|---|---|---|---|---|
| 00:57:50 | 234,058 ms | 249.6 | 0 | 451 | `records/nothp/` |
| 01:04:26 | 220,891 ms | 264.5 | 0 | 564 | `records/nothp-b3/` |
| 01:13:56 | 242,301 ms | 241.1 | 0 | 670 | `records/cb-1/` |
| 01:20:07 | 263,508 ms | 221.7 | 0 | 776 | `records/cb-2/` |

Plus a cached repeat on the first boot: `58418 reused + 5 read in 228 ms` (elog 458). **Corrected: four clean fresh
boots, not two.** A fifth never started (`VERDICT: REFUSED`, `prereq0-20261124-012638.json`). `journalctl -k --since
'2026-11-24 00:50:00'` holds **0** `no-retry page fault` records; `cycle_boots.sh:69` read `anonhuge=0 kB` twice.

Pressure got worse, not better: `min_avail_mib` **8,506** (`cb-1`), 8,156 (`cb-2`), 11,462 (`nothp-b3`), 12,058
(`nothp2`), 12,177 (`nothp`) on the clean arm vs 11,308 (`default-margins`) and 12,692 (`default-margins-b2`) on
the faulting window's completed legs; `max_swap_used_mib` 6,601 / 4,309 (`cb-1`/`cb-2`), pre-boot gates 6.68, 6.68,
6.45, 4.21 GiB of swap. **So the four clean prefills are not a pressure effect** — less headroom than faulting legs.

## The mechanism, as the code documents it

`include/strata/core/pin_budget.hpp:129-134`: "a registered host range is a KFD userptr mapping, and amdgpu
invalidates the GPU queues' view of it whenever the CPU page tables under it change — reclaim, swap and compaction
all do … cudaHostRegister keeps the pages DMA-able but leaves them movable". Exactly the three actors the clean
arm's sysctls touch.

A repair exists in-tree, default off: `STRATA_ARENA_MLOCK` — knob `pin_budget.hpp:146-156`, call sites
`pinned.cu:737`, `expert_source.cpp:4113` and `:4157`. Measured 2026-11-14: **46.84 GiB locked over 4,608 spans
in 128.0 s**, RLIMIT_MEMLOCK unlimited, after a 563.2 s registration, no request-time spin
(`journal/serving-tg-reeval-split-retest-2026-11-11.md:3195`).

## The competing candidates the static audit raised

| Candidate | Code | Why less likely now |
|---|---|---|
| `release_pair`/`refill_pair` unregister + `MADV_DONTNEED` | `expert_source.cpp:7264-7340` (`cudaHostUnregister` 7313, `madvise` 7322), `refill_pair` 7128 | the cap re-rank's demote path; `grep -c 'cap re-rank'` = **0** in the default-margins elog and 0 in all three faulting W1/W1d elogs — the path never ran |
| pageable H2D staging | `expert_source.cpp:2128` (`if (!stage_buf_[v].get_deleter().pinned) return;`) | stage buffers are their own ≤ 2.66 MiB allocations (`blob_max=2662400`, elog 290); the faulted VAs are inside the 32 GiB pool VMA |
| helper's `ext_x_` munmap | `expert_helper_socket.cpp:828-833` (munmap at 830) | the helper's own mapping and process; the record names `Process strata pid 899926`, and the pool VMA was present 67/67 snapshots |

None is disproved — each owns a different VMA, only the pool VMA was sampled. The experiment implicates
THP/compaction but **does not separate "collapse" from "split" from "compaction migration"**, nor exclude anon
reclaim as the trigger the knobs merely damped (`swappiness=0` lowers, never stops, reclaim).

## What this does NOT show

- **n = 1 system state, not an A/B.** The sysctls changed five things at once; none is individually implicated.
- **Luck is not excluded.** At 4 faults in 9 cold attempts, P(0 faults in 4) = 0.095. Fisher exact on the 700/700
  arm before vs after, `[[2,3],[0,4]]`, gives **p = 0.44** — the brief's "p≈0.5" is no number here; 0.44 is the
  closest, and equally insignificant.
- **No per-fault memory reading.** No faulting leg has a `mem` block (the driver writes one only on completion,
  `records/`), so 00:49:26 is bracketed only by the 00:44:41 gate (4.35 GiB swap) and the 00:57:47 gate
  (6.68 GiB) — the "~6.8-7 GiB at fault time" is the *later* reading, i.e. post-fault.
- **`AnonHugePages 1.27 GiB` at fault time is in no artifact I could find.** `maps_sampler.sh:19` samples only
  `Rss|Pss|Anonymous|Shared_*|Private_*|Swap` — never `AnonHugePages` — so the faulted boot has no THP reading;
  the figure appears only in a commit message (`db5bc3ab10`), uncorroborated, and is not load-bearing. The gap
  matters: 2026-11-04 notes record `AnonHugePages 0` on a 46 GiB arena under THP `always` (`JOURNAL.md:342`).
- **No speed claim.** 221.7-264.5 tok/s clean vs 287.4/294.4 before is n = 1 per boot, swap states differ, THP off;
  descriptive only. The last snapshot is 2 s before the fault, and a VMA is not a page: which blobs lived where
  inside it is not sampled.

## Next

1. Engine side: `MADV_NOHUGEPAGE` on the pool behind an opt-in knob, so the operator need not trade the box's THP
   policy; `STRATA_ARENA_MLOCK` is the other half, separately default-off.
2. Restore the sysctls (`page_stability.sh restore`) and re-test — the missing control for the p = 0.095.
3. Longer exposure: 4-6 cold prefills per boot, two boots per arm.
