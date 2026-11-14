# Recommendations — full-residency wall, ROCm landscape, upstream adoption (2026-11-14)

Consolidated from four research lanes (ROCm driver-wall, release-notes sweep,
residency archaeology, upstream commits/PRs). All findings read-only; nothing
below has been applied except where noted.

## TL;DR

1. The full-residency wall is **not a memory-size limit and not a ROCm-version
   bug**: registered arena memory is a KFD userptr (HMM) range that is **not
   actually pinned** (live engine: `VmLck 0`, 7 GiB of arena in swap). Any MMU
   invalidation (reclaim/swap/compaction) suspends GPU queues until amdgpu's
   `restore_userptr_worker` re-faults the **entire** registered range one
   4-KiB page at a time. Cost scales with TOTAL registered pages — which is
   exactly what round 18v measured (12.3M pages = unbounded spin; relief 5.6M
   pages = fits the patience window; chunking into 4608 ranges changed nothing).
2. Cheapest discriminator, ready at next reboot: **boot kernel 7.3.1 (still
   installed) and rerun the unchanged v0233 full-residency flip**. Full
   residency only ever served on kernel ≤7.3.1; every wedge ran on 7.3.4.
3. Best structural fix: **mlock the registered complement after registration**
   (port of public-project PR #3913 "route B") + `vm.compact_unevictable_allowed=0`.
   Tiny patch, strictly fewer invalidations, also helps relief (live relief
   engine shows `evicted_ms ≈ 21.3 s/GPU` cumulative and 7 GiB arena swap).
4. A ROCm upgrade is **low expected payoff** for this wall; if ever needed, the
   last officially-supported gfx906 line is 6.3.x (6.4.0 dropped gfx906);
   TheRock nightlies carry everything but have open SDMA-under-memory-pressure
   known issues and measured slower for us.
5. Upstream (Niko1221/Strata) is 473 commits + v0.2.13 ahead; nothing fixes
   the wall, `pinned.cu` is untouched → our lanes adopt cleanly. Wave-1
   cherry-picks are zero-conflict correctness fixes for our exact gfx906 path.

## A. Root-cause chain (measured + sourced)

- `HSA_USERPTR_FOR_PAGED_MEM` (default on) makes `hipHostRegister` map host
  memory as a KFD userptr (HMM) range — pages are NOT pinned
  (ROCT-Thunk `src/fmm.c`; public Strata PR #3913 documents the mechanism).
- On non-XNACK gfx906, MMU invalidation of a registered page evicts the
  process's GPU queues until amdgpu revalidates the range
  (`amdgpu_amdkfd_restore_userptr_worker` → `hmm_range_fault`, single
  workqueue, 4-KiB granularity) — TheRock#7956/#5993, Strata #2729 (>122 s hang).
- This box actively invalidates: `VmLck 0`, `VmSwap 7.0 GiB`,
  `vm.compact_unevictable_allowed=1`, KFD `evicted_ms ≈ 21,300 ms/GPU` on the
  live relief engine; MemAvailable 8.3 GiB at measurement time.
- First request = VRAM allocations → eviction events → full-range sweep.
  ~10–20 µs/page ⇒ 12.3M pages = minutes-to-forever; 5.6M pages stays inside
  the ~300 s kill window. Chunking cannot help (cost ∝ total pages).
- The "one core 100%, zero I/O" signature is ROCclr's 4 ms signal-wait re-arm
  preventing backoff while sync waits on evicted queues (rocm-systems #12528).
- Arena is NOT THP-backed in practice: `pinned.cu:434-474` tries
  `MAP_HUGETLB|MAP_HUGE_2MB` first but `vm.nr_hugepages=0` → anon fallback;
  measured `AnonHugePages: 0` for the engine (THP faults failing under
  fragmentation; defrag=defer won't compact synchronously). `mm/hmm.c` DOES
  batch PMD-mapped THP (512× fewer walk steps) — capability unused.
- Why it "worked before": full residency last served 11-03→11-04 on kernel
  7.3.0 (boot -36) with less swap/fragmentation pressure. T1 (620 tok/s PP)
  does NOT anchor full residency — it was relief + SWAPIN_DEPTH=64.
  Full-residency speed anchors: 2026-11-04 control arm TG 38.05, Oct 40 GiB arena.

## B. Reboot experiment matrix (operator-executed)

| # | Test | Cost | Discriminates |
|---|---|---|---|
| 1 | Boot **7.3.1** (still installed), rerun unchanged v0233 full-residency flip | reboot only | kernel 7.3.4 regression vs environment (pressure/invalidation) |
| 2 | Boot 7.3.4 with `vm.nr_hugepages=24576` → pinned.cu's existing (currently dead) MAP_HUGETLB path engages; test REGISTRATION ALONE first (hugetlb-backed userptr registration unproven on this kernel) | reboot + cmdline | THP/hugetlb sweep-cost reduction (~512× fewer walk steps) |
| 3 | Add `amdgpu.vm_fragment_size=9` (2 MiB GPUVM fragments) to cmdline | free, orthogonal | TLB/fragment pressure (does NOT reduce hmm fault count) |
| 4 | Cold-boot THP check (order-9 availability early, `AnonHugePages` in engine smaps) | free with #1 | whether fragmentation alone defeats fault-time THP |
| 5 | After any reboot: instrument `/sys/class/kfd/kfd/proc/<pid>/stats_*/evicted_ms` before/after first request | free | confirms eviction/restore machinery is firing |

Suggested order: #1 first (cheapest, answers "regression?"), then #2+#3+#5 on
whichever kernel serves. `sysctl vm.compact_unevictable_allowed=0` is only
needed with the mlock patch (C.1), not for the stock binary.

## C. Ranked fixes

1. **mlock the registered complement** after every cudaHostRegister (port PR
   #3913 route B; needs `ulimit -l` unlimited — already is — and
   `vm.compact_unevictable_allowed=0`). Eliminates reclaim/swap/compaction
   invalidations; registration cost unchanged; strictly helps relief too.
   Watch RAM: 46.8 GiB unevictable.
2. **THP/hugetlb-backing the arena** (B.2/B.4) — zero code for the hugetlb
   path; hmm sweep cost drops ~512×. Risk: hugetlb userptr registration on
   this kernel is the one unverified link. Do NOT add MADV_HUGEPAGE under
   THP=always (pinned.cu #771: 25 s → 432 s with defrag).
3. **`HSA_USERPTR_FOR_PAGED_MEM=0`** (+ `ttm.pages_limit` raise; currently
   ~31 GiB global < 46.8 GiB needed). Structurally removes the class (GTT
   pinned, no MMU notifier) but changes relief's path too — A/B mandatory;
   sister project measured ~2.4× slower prompt reads via fine-grained GTT on
   gfx1100; ROCm 10.2+ additionally needs `HSA_SVM_FOR_PAGED_MEM=0`.
4. `amdgpu.vm_fragment_size=9` (B.3) — adjunct, negligible risk.
5. **ROCm upgrade — low payoff**: no release-note/commit fixes large-userptr
   registration/restore through 7.x; the class reproduces on ROCm 7.10
   (Strata #1705); AMD's real fix (drm/amdkfd batch userptr allocation, v2
   2026-01-04, lore) is NOT merged. Also: any newer tree needs a fresh gfx906
   rocBLAS story (stock rocBLAS broken on gfx906 since 6.4; our custom 4.1
   build exists because of that), and 6.4.4 is operator-forbidden.

## D. ROCm version landscape (gfx906)

- ≤5.7 fully supported; 6.0–6.3.x deprecated-but-shipped (sys-reqs verified);
  **6.4.0 first ❌** (MI50/Radeon VII/PRO VII); 7.0+ ❌; current 10.1: gfx906
  only via TheRock nightlies (SUPPORTED_GPUS.md ✅, `amdrocm-gfx906` packages,
  nightly-only, no hardware tests).
- Best user-space candidate if ever upgrading: **ROCm 6.3.4** — last patch of
  the last officially-supported gfx906 line; carries the June–July 2024
  libhsakmt userptr/SVM registration rework (`626eb4bfa` fallback-to-legacy-
  userptr, `4844a70d9`, `a31e84eae`; guaranteed in 6.3.0, maybe 6.2.0 — old
  ROCT repo deleted so 6.2.0 unverifiable). Test with matching amdgpu-dkms
  since the spin may be KFD-side.
- HIP 6.4.1 has an actual gfx906-family SDMA ordering fix (MI50 listed) — but
  6.4.x is unsupported-for-gfx906 and operator-forbidden on this box.
- TheRock: carries ALL 2025–2026 registration/pinned work (clr `3d6026600`
  revoke-on-unlock, host-register pool locks, staging-copy instead of pinning)
  — but open known issues #8843/#8844 (SDMA + memory pressure → page faults,
  long wedges) overlap our failure mode; matches operator's "slower than 6.1".
- Repo facts: ROCclr lives in ROCm/clr since ~6.1; ROCT-ROcr deleted, libhsakmt
  absorbed into ROCm/ROCR-Runtime; legacy official builds at ROCm/legacy-rocm-build.

## E. Upstream adoption plan (Niko1221/Strata, 473 commits + v0.2.13 ahead)

Merge-base `54828c7efb` (Nov 6). Conflict zones ONLY `generate.cpp` (51
upstream) and `expert_source.cpp` (14); `pinned.cu`, `pinned.hpp`,
`foresight_swap.hpp` byte-identical → our register-last/staging/foresight
lanes adopt cleanly.

| Wave | Refs | What / why | Risk |
|---|---|---|---|
| 1 (now, zero-conflict) | `f225844d19`, `14dd746f6a`, `697b6d67a7` | MMQ row-id tables sized with the margin llama.cpp kernels read past (J≤128 tiles, stream-k fixup) — fixes verified OOB read in our exact gfx906 prefill path (`STRATA_DENSE_MMQ=1`; vulnerable pattern still in our tree: gemm.cu:1282-1296, prefill.cpp:2387; MTP-draft q8 path too) | low, new header + ~20 lines |
| 1 | `e008709d77`, `269f33859b`, `13eedf5524`, `711a7e9378`, `03e687ace4`, `a950466908` | free-mem query error ≠ 0 free; per-device fused-gr shared mem (layer split!); stop bound all backends; stall report asks prompt-path stream + AMD driver-pause time; bounded GPU-reader retries | low |
| 2 | #5327 (`28bd0185f9`+), #5722 `7ee50cc8c7` | arena RAM shortfall servable from file cache/hugetlb with warning (RAM-tight boots; reporter 47→124 tok/s @1M ctx); per-card prompt-loan pricing | med (expert_source.cpp +44) |
| 3 | `bca7931f3d`, `24c6c2f682`, `8c71c49278`, `feaa928f90` | peer-tier ADAPT_OVERLAP/ADAPT_FETCH; evicted-conversation RAM return; resident-pin knob | med-high, conflicts with our lanes |
| track | open PR #3913 | one-go expert allocation when MemAvailable covers it + `kfd_evicted_ms` in /health — closest upstream work to our wall; port ideas, don't carry commits | — |
| track | open PRs #4534/#4522/#4549, #5864, #4859 | layer-split pipelined pricing (34→48→66.5 tok/s 2-card); batch slots 2-card (+53%); gfx1100 WMMA prompt-kernel port (+84% PP — same skipped-arch argument applies to gfx906; biggest conceivable PP lever) | research leads |
| defer | `508e6999ab` | vendored ggml restructure | next full sync, separate |

## F. XXS flip state (staged, NOT benched — GPU frozen pending reboot)

- Config `prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json` rebuilt on the
  champion shape (v0233 exe, champion env, relief args, 576 reserves, XXS
  shards/pack/tokenizer), commit `bb0ec893f3`.
- Launcher fixed through three bugs (all committed): socket names to
  `prod-iq3xxs-ud-mixed{,2}.sock` (`c770aa7411` + follow-up), helper pack
  `iq3xxs-ud` + `Qwen3.8-Flash-Next-UD-IQ3_XXS-00001-of-00004.gguf` + era fill
  6796 (`a9eac52642`). Helper reaches fill preflight cleanly (6.82 GiB budget
  capped, warmup OK) — the boot was killed by supervisor teardown from the
  socket2 mismatch, not by the helper.
- Bench staged: fill 80k (ctx 442400) then direct 1024 TG, temp 0 + sha, via
  `ab9_driver.py --arm XXS`; watch chain excludes `-helper` logs.
- Next GPU window: boot XXS → bench → journal. Kernel/reboot experiments (B)
  can share the same window.

## Sources

Journal rounds 18k–18v (serving-tg-reeval-split-retest-2026-11-11.md);
ROCT-Thunk fmm.c; Niko1221/Strata PR #3913, issues #1705/#1691/#2729/#2850;
rocm-systems #12528; ROCm/TheRock #7956/#5993/#8013/#8843/#8844; torvalds/linux
mm/hmm.c, amdgpu_drv.c, amdgpu_amdkfd_gpuvm.c; lore kernel amdkfd batch-userptr
v2 (2026-01-04); ROCm sys-reqs 6.1.0/6.3.0/6.4.0; /proc + /sys live readings
2026-11-14; upstream git 54828c7efb..origin/main.
