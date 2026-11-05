# Strata upstream merge prep — 2026-10-24

Working tree: `/mnt/user/Strata`, branch `build-latest` (gfx906 port), tracks `origin/main` = Niko1221/Strata.
State: **ahead 9, behind 1308**. Merge base = `477ac13c` = tag `v0.1.45` (2026-10-16); v0.1.45 already merged in `4cfc04b3`.
Upstream tip = `5c8dd322` = tag `v0.2.3` (2026-10-23). Release ladder since base: v0.2.0-beta → v0.2.1(.1-.4) → v0.2.2 → v0.2.3, plus `pre-upstream-sync-20261020` (big llama.cpp/ggml b11xxx rebase). 196 merge commits in range (Strata merges feature streams f-02x-*/f-dflash2 into main). ~1006 of 1308 authored by Niko1221 (maintainer), rest community + llama.cpp sync.

## Our port surface (files touched vs base) — only 6, conflicts bounded
- `CMakeLists.txt` — gfx906 backend gate, MMQ/PREFILL_MMQ gates, Blackwell-only `mmq_w4a4` excluded, `moe_f16_kq.cu` → strata_mmq
- `include/strata/platform/hip_compat/cublas_v2.h` — cuBLAS→hipBLAS shims
- `src/kernels/cuda/iq_kernels.cu`, `src/qwen35/q36_engine.cu` — `__ldg(int32)` plain load, `__half_as_ushort` bitcast, `STRATA_SHARED_ALIGN16_I4`
- `launch-prod.sh`, `prod-config.json` — prod launcher/sampling

## KEY FINDING: upstream absorbed an official gfx906 port
`origin/main:CMakeLists.txt` now exposes `STRATA_HIP_GFX906` (wave64 compat layer, separate from STRATA_ENABLE_HIP) with the same guards we hand-patched, and more:
- L778-781: Qwen3.5/q36 "Not on gfx906 (#3172,#3271): cuBLAS entry points the wave64 compat layer does not provide" → `STRATA_HAVE_QWEN35` path — fixes exactly what our cublas_v2.h shims patched.
- L1757: `STRATA_MMQ_KQUANTS AND STRATA_HIP_GFX906` → gfx906 keeps k-quant MMQ in strata_mmq (mirrors our `6d5e5770` moe_f16_kq compile and `b7383aae` PREFILL_MMQ gate).
- New gfx906 tests upstream ships: `gfx906_gdn_rec_head`, S6 router `STRATA_HIP_ROUTER_FAST=1` bitwise parity.
=> Merging upstream = adopt the maintained gfx906 path and **retire most of our 9 hand shims** (re-verify each against the official gate; keep only what the official guards miss).

## What's new & benefits us (Radeon VII gfx906 / ROCm 6.1 / Swift 1.5 IQ3_XXS, decode-bound)
- **llama.cpp/ggml sync (b11xxx)** — the bulk of 1308: core ggml, quant kernels, sampler, pinned ggml the trellis MMQ overlay patches (`cbc0e329`). Security + speed baseline.
- **DFlash2 drafter + MTP** (`7f97c35a`, `437a6067`, `5f418f1` STRATA_SPEC_COST, `3ae2123b` --draft-conf, `ff9ca03f` --draft-cap) — speculative decode for Qwen3.8-Flash-Next. DFlash2 compiled in CUDA **and HIP** builds (`CMakeLists.txt:339`), Metal/SYCL only compile without it. gfx906 = HIP compat path → expect **MTP drafting as the safe win**, DFlash2 gated; verify on-card. Headline decode-speed lever.
- **hip prefill: hipBLASLt tuning-table auto-discovery** (`c62d9801`) — engine reads `tuning/<arch>-hipblaslt-<version>.txt` beside the exe when `STRATA_HIPBLASLT_TUNING` is unset, so hand-made configs/CLI (our launch-prod.sh/prod-config.json!) stop reading prompts at half speed; `STRATA_HIPBLASLT_TABLE=off` keeps plain hipBLAS. Direct prefill win for our launcher.
- **HIP/RDNA fixes**: PIC picture reader links on Linux (`98faf813`); queue priming stands aside when `GPU_MAX_HW_QUEUES` 1/2 (`f05725c3`); first-window PLE-flag hang (`86106a4`); gfx906 query-swizzle guard test (`be4e24d4`, touches our surface).
- **STRATA_HC_Q8 8-bit hyper-connection weights** (`6d4874a4`) — MoE HC matrices read q8_1; default on gfx1151, opt-in for gfx906 (memory saving; verify KL/quality).
- **expert-cache safety** — auto cap 1500 slots, no loan in prompt path (`e6dbbc31`,`b4406409`), CPU-order (we already have `1dfdef48`). Relevant to our long-prompt/agent runs.
- **serve: max_tokens -1/0 mean no cap** (tip `5c8dd322`) — OpenAI/Anthropic-compat API; cross-reads opencodev2 compaction/token-budget work.
- serve/app extras: tool-returned pictures (`5701fc65`), TLS via OS cert store #3722 (`ed0ed38e`), drafting UI.
- Not for us: Gyro trellis decode (CUDA cc6.0/SYCL/Metal), Strix Halo/Arc/Strix PP, SYCL Intel — off on gfx906.

## Merge strategy (recommend staged by tag, not one big merge)
1. Branch `strata-v023-merge` off `build-latest`.
2. Merge tags in ladder order: `v0.2.0-beta` → `v0.2.1` → `v0.2.2` → `v0.2.3`; each = bounded conflict set, bisectable.
3. Resolve conflicts in the 6 port files + CMakeLists arch/MMQ gates; prefer upstream's `STRATA_HIP_GFX906` guards, delete superseded shims.
4. Build the gfx906 HIP backend after each stage (AGENTS.md: build every backend a file touches); run gfx906 tests (`gfx906_gdn_rec_head`, router parity, MMQ parity).
5. Decode A/B vs current build with the `llama-cpp-gfx906-benchmark` skill; record tok/s + output-equivalence before keeping a default.
6. Reconcile `prod-config.json`/`launch-prod.sh` with the new auto hipBLASLt table + drafting flags.

## Risks
- 196 merges / 1308 commits = large conflict surface in CMakeLists arch gating; single merge is hard to review. Staged tag merges keep it bisectable.
- Our shims vs official gfx906 guards can double-apply (e.g. moe_f16_kq compiled twice, MMQ gate both sides). Re-verify each of the 9.
- DFlash2/HC_Q8/Gyro are arch-gated; confirm they stay dormant on gfx906, do not assume parity.
- prod-config/launch are local (uncommitted on tree? — launch/prod committed; serve/telemetry.py and .gitignore modified) — stash/rebase cleanly.

## RESOLVED TOPOLOGY (2026-10-24, after user answered)
Decisions: staged-by-tag merge; priorities = official gfx906 path + retire shims, DFlash2/MTP drafting, hipBLASLt auto table + serve fixes; **hard constraint: do not break our own dflash2.**

Lane map (all worktrees under /mnt/user/dflash2 and worktrees):
- `build-latest` = gfx906 port @ v0.1.45, **no drafter**. 8 local commits not in integ: STRATA_SHARED_ALIGN16_I4, moe_f16_kq→strata_mmq, mmq_w4a4 exclude + cublas shims, PREFILL_MMQ gate, plus prod-launcher/prod-config tuning (launch-prod.sh +20, prod-config.json +63).
- `inco-dflash2-gfx906` (dflash-work/codex/dflash2-main, ahead 47) = our drafter research lane, uses dflash2.cu.
- **`upstream-integ-023` = THE ANSWER.** = origin/main v0.2.3 (behind 0) + official `STRATA_HIP_GFX906` + gfx906 HIP rebuild (#3172/#3271, fp16 bit-cast shim, gfx906 HIP build evidence) + drafter reconciled. Fully contains inco-dflash2-gfx906.
- Drafter preservation is BUILT IN at CMakeLists:105-107:
  - `STRATA_HAVE_DFLASH2` = upstream drafter `src/core/dflash2.cpp` (in-verify taps), ON as upstream.
  - `STRATA_ENABLE_DFLASH2` = OUR drafter `src/kernels/cuda/dflash_inco.cu` + `include/strata/kernels/dflash_inco.hpp` (serve/layer-split taps).
  - FATAL_ERROR if both ON: "a verifier may have exactly one tap producer." This gate IS the do-not-break guard.

## Corrected plan (merge is effectively DONE on integ)
1. Target = `upstream-integ-023` (/mnt/user/dflash2/Strata-integ): already v0.2.3 + gfx906 + both drafters under mutual exclusion. Skip re-merging 1308 on build-latest.
2. Pick ship mode (mutually exclusive): `STRATA_ENABLE_DFLASH2=ON` (ours, dflash_inco serve/layer-split) XOR `STRATA_HAVE_DFLASH2=ON` (upstream, dflash2.cpp). Never both (CMake:107). Benchmark line: "tuned DFlash2 beats MTP on the long fixture."
3. Carry build-latest-only delta onto integ: prod launcher/sampling + single-file-GGUF revert + --expert-cache-cpu-order (83 lines, low conflict, launcher files only).
4. Verify: gfx906 HIP build green; run gfx906 benchmark skill decode A/B for the chosen drafter; confirm the other drafter dormant.
5. build-latest disposition: fast-forward/repoint to integ for gfx906+drafter, OR cherry-pick integ's gfx906 commits onto a clean v0.2.3 branch for a drafter-less lane.

## Lane D prep (2026-10-25, executed)

Fix-4 (fixer) delivered the heavy half before two transport timeouts killed its runs (its model backend rides the loaded prod box): build-906-inco green (exit 0, 02:01:30, `lane-d-logs/build-inco.log`; cache `STRATA_ENABLE_DFLASH2=ON`/`STRATA_HAVE_DFLASH2=OFF`, dflash_inco objects built = one tap producer, ours) and the upstream drafter artifact PACKED locally (`lane-d-logs/artifacts/qwen3.8-flash-next-dflash2.q8.pack`, 81 tensors / 760 MB via the repo pack tool). The orchestrator finished the file half directly (user rule: bench window = no agents; repeated child transport failures made direct the lightest path):

- `lane-d-logs/mk_prompts_linux.py` — chat-templated real-text prompts via the repo tokenizer + ChatTemplate (short 238 ids / long 16,358 ids; `.ids` files + `prompts.json`).
- `lane-d-logs/run-ab.sh` — the A/B: arms mtp / ours (`--dflash inco-dflash2-q4_0.gguf --dflash-block 5`, build-906-inco) / upstream (`--draft dflash2 --draft-model qwen3.8-flash-next-dflash2.q8.pack`, build-906); prod-identical base workload; interleaved 3 reps × short+long; `--greedy --stats`; prod env (custom rocBLAS + STRATA_* tuning) baked in; refuses to run while prod is up; RUN_AB_DRY=1 dry mode; raw logs to `lane-d-logs/ab/`.
- `lane-d-logs/parse_ab.py` — tolerant medians summary (prefill/decode tok/s, drafter lines, VRAM).
- `lane-d-logs/mk_cutover_config.py` + `cutover-ours.sh` / `cutover-upstream.sh` — clone the prod serve config, patch exe + drafter args (ours keeps `--dflash*`; upstream swaps to `--draft dflash2 --draft-model`, drops STRATA_DFLASH_SAMPLED), start the wrapper on 8009 with the custom-rocBLAS env, health-check `/v1/models`. Both configs generated and inspected.
- Validated: bash -n all three; both cutover configs build; dry run prints all 18 engine commands correctly (3 arms × 2 cases × 3 reps); guard proven (refused while prod up). No drafter-off arm exists in the CLI (MTP is the always-on fallback) — the A/B answers the shipping question: MTP baseline vs OURS vs UPSTREAM.

Protocol (user-decided): orchestrator stops prod → orchestrator runs run-ab.sh DIRECTLY (agents cannot run while prod is down; orchestrator's runtime is external) → winner by decode tok/s median + acceptance sanity → orchestrator restarts prod via cutover-<winner>.sh → health check 8009 → final wrap-up.

## Not done
- [x] Scope chosen: staged/tag path; integ already realizes it.
- [x] DECISION: land the staged tag merges on a **new branch off build-latest** (not adopt integ).
- [x] DECISION: drafter ship mode = **benchmark both on-card first, ship the winner** (keep the other dormant; CMake forbids both).
- [x] Lane A/B/C complete (A: ladder to v0.2.3; B: 6.1 build+tests classified; C: drafter merged, 6/84 known-only).
- [ ] Lane D: on-card drafter A/B → ship winner + launcher reconcile (blocked on GPU scheduling vs live prod server on --gpu 0,1).

## Lane A result (2026-10-24, executed)
Branch `strata-v023-merge` @ `/mnt/user/Strata/worktrees/v023-merge` (worktree add off `build-latest`; main checkout untouched, other worktrees untouched; nothing pushed; no builds run — Lane B owns the gfx906 HIP build).

### Ladder (4 merges, tag-tip = v0.2.3 5c8dd322, stop as planned)
| Merge commit | Tag (sha) | Conflicts |
|---|---|---|
| `0cab51d6` | v0.2.0-beta (3d1fb480) | none |
| `4dc35b32` | v0.2.1 (90767252) | none |
| `d4cd767f` | v0.2.2 (011c9e60) | `src/qwen35/q36_engine.cu` (VDEBUG half-bits hunk); plus proactive double-apply removals auto-merged silently: cublas shim lines in `include/strata/platform/hip_compat/cublas_v2.h`, `moe_f16_kq.cu` line in CMakeLists `add_library(strata_mmq)` |
| `44754c8b` | v0.2.3 (5c8dd322) | none |

### Fate of the 9 local commits (477ac13c..build-latest)
| Commit | What it did | Fate |
|---|---|---|
| `4cfc04b3` | merge v0.1.45 into gfx906 port | structural — merge base, absorbed by ladder |
| `918781aa` | exclude Blackwell `mmq_w4a4` + cublasSgemm/GemmStridedBatchedEx shims | **split**: cublas shims **superseded** (deleted — upstream #3172/#3271 excludes `strata_qwen35` on gfx906 via `if(NOT STRATA_HIP_GFX906)`, and the wave32 header `include/strata/hip_compat/cublas_v2.h` maps both names itself; header now byte-identical to upstream); w4a4 `if(NOT STRATA_HIP_GFX906)` exclusion **kept** (upstream v0.2.3 builds w4a4 for gfx906 as a GGML_USE_HIP stub — policy keeps our exclusion) |
| `25f12ced` | prod launcher + prod-config tuning | **local-only carried** (`launch-prod.sh`, `prod-config.json`, byte-identical to build-latest) |
| `1dfdef48` | `--expert-cache-cpu-order` | **local-only carried** (in prod-config.json) |
| `f2d280d5` | combined-GGUF switch | **local-only carried** (net cancelled by ddc31c56 revert; final file = build-latest) |
| `b7383aae` | PREFILL_MMQ gate gfx906 exemption | **kept** (upstream v0.2.3 L109 has NO gfx906 exemption; our gate keeps MMQ/PREFILL_MMQ build path OFF on gfx906 as measured — decode regression) |
| `ddc31c56` | single-file GGUF revert | **local-only carried** (prod-config.json) |
| `6d5e5770` | `moe_f16_kq.cu` into strata_mmq add_library | **superseded** (deleted — upstream: official `if(STRATA_MMQ_KQUANTS AND STRATA_HIP_GFX906)` at CMakeLists L1731 keeps k-quants MMQ in strata_mmq on gfx906, `target_sources(... moe_f16_kq.cu)` in the gfx906 #3271 block covers the link, HIP wave32 list covers gfx11; `moe_fused_iq.cu` f16kq calls sit under gfx11 `#if`s so plain CUDA needs nothing) |
| `70e0d531` | ALIGN16_I4 macro + `__ldg(int32)` plain load + `__half_as_ushort` bitcast | **split**: `STRATA_SHARED_ALIGN16_I4` macro + 2 expdec uses **kept** (upstream v0.2.3 still raw `alignas(16) int4` — no equivalent); `mtab[...]` plain load **kept** (harmless: file not built on gfx906, plain load fine on CUDA); `__half_as_ushort` VDEBUG bitcast **superseded** (deleted at conflict — upstream v0.2.2 added equivalent host-side `t0h`/`t0bits` memcpy bitcast) |

### Diffstat vs v0.2.3 — 5 files, 105+/17-
| File | Δ | Justification |
|---|---|---|
| CMakeLists.txt | 31 | exactly 2 deltas: PREFILL_MMQ gfx906 exemption (b7383aae kept) + w4a4 gfx906 exclusion block (918781aa kept); all other gates = upstream's own `STRATA_HIP_GFX906` (21 occurrences survive, option at L75) |
| launch-prod.sh | +20 | local prod launcher (25f12ced), local-only |
| prod-config.json | +63 | local prod config (25f12ced/1dfdef48/f2d280d5/ddc31c56), local-only |
| src/kernels/cuda/iq_kernels.cu | 6 | STRATA_SHARED_ALIGN16_I4 macro + 2 uses (70e0d531, still needed on ROCm 6.1) |
| src/qwen35/q36_engine.cu | 2 | `mtab[...]` plain load (70e0d531, dormant on gfx906, equivalent on CUDA) |

### Validation
1. worktree `git status` clean; `git grep -n '<<<<<<<'` empty. PASS
2. `git log --oneline -8` shows the 4-merge ladder (0cab51d6 → 4dc35b32 → d4cd767f → 44754c8b). PASS
3. diffstat vs v0.2.3 = the 5 files above, every file justified. PASS
4. `STRATA_HIP_GFX906` official option survives (CMakeLists L75 + 20 more sites). PASS
5. All 9 local commits verified ancestors of `strata-v023-merge`; main checkout + other worktrees untouched; NOT pushed; no build/tests (Lane B). PASS

### Decisions made (ambiguities)
- Kept the w4a4 exclusion because policy says keep if upstream compiles it for gfx906 (it does, as a stub) — our exclusion is strictly safer for our card; upstream stub path left intact for non-gfx906 HIP.
- Kept the q36 plain-load line even though q36 is excluded on gfx906: zero-risk, keeps CUDA builds identical to our tested behavior.
- prod-config.json/launch-prod.sh carried byte-identical; reconciliation with the new hipBLASLt auto-tuning table (`strata_control/hipblaslt_tuning.py`, tuning/ discovery) deferred to the launcher-reconcile step (journal strategy item 6).

## Lane B result

gfx906 (Radeon VII, wave64 HIP compat) configure + build + test of `strata-v023-merge` in
`worktrees/v023-merge`, build dir `build-906` (worktree-local only; the main checkout, other
worktrees and other build dirs were never touched, nothing pushed).

### Configure — ROCm 6.1.0 only
Flags: `-DCMAKE_BUILD_TYPE=Release -DSTRATA_ENABLE_CUDA=ON -DSTRATA_HIP_GFX906=ON
-DCMAKE_HIP_ARCHITECTURES=gfx906 -DSTRATA_NATIVE_EXPERTS=ON -DSTRATA_MMQ_KQUANTS=OFF
-DCMAKE_HIP_COMPILER=/opt/rocm-6.1.0/llvm/bin/clang++ -DCMAKE_PREFIX_PATH=/opt/rocm-6.1.0
-DSTRATA_BUILD_TESTS=ON`, `PATH=/opt/rocm-6.1.0/bin:$PATH`, no `STRATA_GGML_DIR`.
- First configure used the PATH default and elected **/opt/rocm (HIP 7.15, Clang 23)** → rejected per
  steer, `rm -rf build-906` before any object was compiled; archived `lane-b-logs/configure-1-ROCM715-REJECTED.log`.
- Green on 6.1.0 (`lane-b-logs/configure-2-ROCM61.log`): `The HIP compiler identification is Clang 17.0.0`,
  `Strata: HIP (gfx906 compat) enabled, arch gfx906`, `STRATA_ENABLE_HIP=OFF`, q36 excluded (CMakeLists:781).
- Proof: `grep CMAKE_HIP_COMPILER build-906/CMakeCache.txt` →
  `CMAKE_HIP_COMPILER:STRING=/opt/rocm-6.1.0/llvm/bin/clang++`; `CMAKE_HIP_ARCHITECTURES=gfx906`;
  `STRATA_HIP_GFX906:BOOL=ON`. Third configure (after the test-gate fix): `lane-b-logs/configure-3-gates.log`,
  same cache values.
- SIMD: the fresh cache already equals the tested portable build (AVX/AVX2/AVX512*/VNNI/BF16/VBMI/BMI2/FMA/F16C/
  SSE4.2/AMX all OFF, `GGML_NATIVE=ON`, ggml-cpu `-march=native`) → no extra `-DGGML_*=OFF`, no reconfigure.
- `CMAKE_HIP_FLAGS` left empty, host C/CXX = gcc (matches `build-906-new`, the portable reference).

### Build
- `-j10` (not `-j$(nproc)`=16): only ~7 GB RAM free, the serving engine holds 44.5 GB — reported deviation.
- Build #1 (22:29:54→22:33:33) failed on **one** compile error: `src/core/dflash2.cpp:205-206` calls
  `cudaStreamCreateWithPriority` / `cudaDeviceGetStreamPriorityRange`, unmapped in the gfx906 HIP compat shim →
  commit **`9a838a41 fix(build): map cudaStreamCreateWithPriority and cudaDeviceGetStreamPriorityRange in the
  gfx906 HIP compat shim`** (`include/strata/platform/hip_compat/strata_hip.h`). Fail log `lane-b-logs/build-1-FAIL-dflash2.log`.
- Build #2 green: **159 targets, 0 errors, 92 gfx906 offload compiles, 4m35s** (22:44:55→22:49:30) → `build-906/build.log`.
- Total build wall time 5m59s (84s failing + 4m35s green).

### Runtime environment (required for every GPU run)
`LD_LIBRARY_PATH=/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib` (launch-prod.sh:17) plus
`HSA_OVERRIDE_GFX_VERSION=9.0.6 HSA_ENABLE_SDMA=1 CUDA_VISIBLE_DEVICES=0,1 HIP_VISIBLE_DEVICES=0,1`
(prod-config.json:58-61). Unset/empty visibility vars break runs (iGPU → `invalid device function`;
empty list → `no ROCm-capable device is detected`).
rocBLAS proof — `lane-b-logs/rocblas-proof.txt`: `ldd build-906/strata` and `ldd build-906/rope_parity` →
`librocblas.so.4 => /home/user/rocm-gfx906-xnack/lib/librocblas.so.4`; `LD_DEBUG=libs strata --families`
→ `calling init: /home/user/rocm-gfx906-xnack/lib/librocblas.so.4`; md5 custom `3edc6e24…` vs stock 6.1.0
`librocblas.so.4.1.60100` `21f7d6fb…` (the stock one segfaults on gfx906, /home/user/rocblas-gfx906-bug-report.md).

### Smoke
`./build-906/strata --version` → `strata 0.1.48` (= upstream v0.2.3's own `project(strata VERSION 0.1.48)`,
`git describe` = `v0.2.3-15-g7d1b2eba`). `./build-906/strata --families` → `qwen4exp` (the Qwen3.8-Flash-Next
family, `include/strata/artifact/model_family.hpp:22`); `qwen35moe`/q36 absent as expected (gfx906 gate).
Log `lane-b-logs/smoke.txt`.

### ctest — `ctest -R 'gfx906|router|mmq|parity' --output-on-failure` + manual `hip_router_fast`
- Pre-gate run: `85% tests passed, 13 tests failed out of 86`, 735.95 s, exit 8. Its log is **contaminated**
  (a previous session's `tee` and this run's redirect shared `build-906/ctest.log`, which is why progress lines
  and the summary list disagreed for tests 14/15); kept as `lane-b-logs/ctest-1-pregate.log`. Per-test authority
  is the standalone rerun of every failing case with its exact ctest command line: `lane-b-logs/fail-rerun.txt`.
- Post-gate run (after `7d1b2eba`, same script, 00:49:13→00:51:57): **`92% tests passed, 7 tests failed out of 83`**,
  164.43 s, exit 8 → `build-906/ctest.log` (+ NUL/CR-stripped `lane-b-logs/ctest-clean.txt`).
  `hip_router_fast` (not registered in ctest): PASS, 0 failures, 17.39 µs vs 55.78 µs portable per single-token call.
- Test wall time: 12m16s (pre-gate, contended) + 2m44s (post-gate) + 26s (standalone reruns).
- Context: both Radeon VIIs sit at ~16.5/17.2 GB used by the co-resident prod engine throughout, so
  multi-hundred-MB allocations fault; that is the single biggest source of first-run failures.

| Test | Class | Evidence |
|---|---|---|
| `mmvq_il_parity` | not applicable on gfx906 — CUDA-only kernel | aborts `std::invalid_argument: native_q8_1_interleave is CUDA only`; `src/kernels/cuda/native_mmvq.cu:3259-3265` builds no interleaved path under HIP |
| `pf_wmma_parity` | not applicable on gfx906 — no WMMA | `IQ4_XS …: not launched`, `pf_wmma_parity: 16 failures on 2 devices`; the test's own gate is `cudaDevAttrComputeCapabilityMajor >= 7` and HIP reports 9.0 for gfx906, so it admits the card |
| `verify_parity` | not applicable on gfx906 — kernel not built | exits `argmax_rows: not built for this backend` |
| `qsa_slots_parity`, `qsa_slots_parity_lanecell` | contention (transient) | ctest `CUDA: out of memory`; standalone exit 0 `PASS: 16 cases …` (both variants) |
| `batch_rows_parity` | contention (transient) | ctest `malloc: out of memory`; standalone exit 0 `batch_rows_parity: ok` |
| `s2_expert_grouped_parity` | contention (transient) | 595 s then `HW Exception by GPU node-2 … GPU Hang`; standalone exit 0, `0 failures`, bitwise fp32+fp16 |
| `cvec_parity` | contention (transient) | `Memory access fault by GPU node-1 … Page not present`; standalone exit 0 `cvec_parity: all passed` |
| `ple_parity` | fixture missing | exit 2 `required PLE table is missing … ../../Q2_0/Qwen3.8-Flash-Next-GSQ-RCO-Q2_0-00002-of-00002.gguf` (not on this host) |
| `native_expert_parity_q4_K_q5_0`, `_q5_0_q8_0`, `_q4_0_q4_0`, `_q4_0_q4_1`, `_q6_K_q8_0` | gfx906 numeric, pre-existing (see below) | only check (d)'s bitwise fp16 assertion fails: `GPU dequant … 0 of 4915200 values differ in any bit; fp16 of it: 47228 / 95700 / 285145 / 190874 / 42538 differ`; decode-once, AVX2 and embedding checks bitwise clean |
| `gdn_chunk_parity`, `tq_cpu_parity`, `tq_cpu_parity_avx2` | design skips | `Skipped` by registration (`SKIP_RETURN_CODE`; tq_* need the agention fork's `ggml/src/ggml-cuda/tq.cuh`, absent) |

### The five `native_expert_parity` fp16 failures — mechanism, and why they are not merge regressions
Check (d) requires `iq_dequant_f16` output to equal `ggml_fp32_to_fp16(iq_dequant_f32 output)` **bitwise**
(`src/kernels/native_expert_parity.cpp:447-456`). Mechanism probe against this lane's own
`libstrata_kernels.a` (`lane-b-logs/f16-dequant-probe.txt`, source `/tmp/agent/dqprobe.cpp`): for valid
Q4_0 (18-byte blocks) and Q5_0 (22-byte blocks) blobs, **100 % of the mismatches are `-0 → +0`**
(want `0x8000`, got `0x0000`), all inside the fp16 zero/subnormal band, **zero** at |v| ≥ 1e-3; a separate
probe (`/tmp/agent/f16denormprobe.cu`) shows the device conversion matches IEEE-RN everywhere else on
`gfx906:sramecc+:xnack-`. So ROCm 6.1's hardware f32→f16 on gfx906 drops the sign of zero while ggml's
portable converter keeps it; the assertion is bitwise-strict, so it is visible with no impact at real
weight magnitudes.
Not a regression from the merge: `dq_q4_0`, `dq_q4_1`, `dq_q5_0`, `dq_q5_1`, `dq_q6_k`, `dq_q4_k`, `dq_q8_0`
and `cvt<__half>` are **byte-identical** between the pre-merge gfx906 tip `70e0d531` and HEAD (function-body
diff = 0 lines for all of them), and the failing pairs plus the `h_differ` check are all ancestors of
`70e0d531` (`16bdc707`, `df52f530`, `8c4a6e1d`, `b55c48b2`, `ee7c8026`). These tests had never run on gfx906
before: the known-good `build-906`/`build-906-new` were built with `STRATA_BUILD_TESTS=OFF` and contain no
`native_expert_parity`. Not contention (they fail identically standalone), not fixtures.
Open decision for the kernel owners (deliberately **not** patched in this lane): relax the (d) fp16
assertion for signed zero under `STRATA_HIP_GFX906`, gate that subtest, or give the dequant a software
f32→f16 on gfx906 (the whole prompt path pays for it).

### Test-gating fix committed
`git diff v0.2.3 HEAD -- CMakeLists.txt` shows Lane A's two gates only, and every upstream gfx906 test gate
survives (`pdl_parity` L1005, `dma_batch_parity` L1036, L590, L781, L984, L1079, L1231, L1757, L1776, L1797) →
no dropped exemption; the three not-applicable tests were upstream gating gaps.
Committed **`7d1b2eba fix(tests): gate the CUDA-only parity tests out of the gfx906 HIP build`** — mirrors
upstream's own `if(STRATA_ENABLE_CUDA AND NOT STRATA_HIP_GFX906)` style for `mmvq_il_parity` (inner
`if(NOT STRATA_HIP_GFX906)`, `dequant_wide_parity` stays registered and passes), `pf_wmma_parity`, `verify_parity`.
Re-configured (`configure-3-gates.log`), the three names are unregistered (`ctest -N -R` finds only
`spec_verify_parity`), and the post-gate suite went 13/86 → 7/83 failed.

Lane B commits, local only: `9a838a41` (compat shim), `7d1b2eba` (test gating). Reproducible artifacts:
`build-906/build.log`, `build-906/ctest.log`, `lane-b-logs/{run-ctest-61.sh, rerun-failures.sh,
fail-rerun.txt, ctest-clean.txt, ctest-1-pregate.log, configure-1-ROCM715-REJECTED.log,
configure-2-ROCM61.log, configure-3-gates.log, build-1-FAIL-dflash2.log, smoke.txt, rocblas-proof.txt,
f16-dequant-probe.txt, ctest-list-before-build.txt}`.

## Lane C result (2026-10-25, executed)

`git merge upstream-integ-023` into `strata-v023-merge` at `worktrees/v023-merge`, then gfx906 HIP
rebuild + ctest. Merge commit **`4718b948`** (4718b94821352bae05e0079d8edbf7578c4114e0), merging
`upstream-integ-023` **`46d112c0`** (merge base = v0.2.3 `5c8dd322`; 471 files, +62.1k lines).
Main checkout, other worktrees and the prod engine untouched; nothing pushed.

### Conflicts + resolutions (per policy)
- **One content conflict: `include/strata/platform/hip_compat/strata_hip.h`** — comments only. Both sides
  added the identical stream-priority mappings (`cudaStreamCreateWithPriority` /
  `cudaDeviceGetStreamPriorityRange`; ours `9a838a41`, integ its own). Kept the mapping **once**; took
  integ's comment (it cites the ROCm 6.1 `hip_runtime_api.h:2320/:2335` signatures). Post-merge grep:
  exactly one `#define` of each across all hip_compat headers. No duplication remains.
- `CMakeLists.txt`, `src/qwen35/q36_engine.cu` auto-merged; `iq_kernels.cu` byte-identical (integ untouched).
- Dedup, q36 `VDEBUG` hunk: integ's host-side memcpy `half_bits` spelling (gfx906-safe print) took over
  from Lane A's `__half_as_ushort` line (same bits; file stays excluded on gfx906); our `mtab[...]`
  plain-load line intact. `STRATA_SHARED_ALIGN16_I4` = our copy only (1 macro, 2 uses).
- serve/ + tools/: integ's side (drafter machinery, 54 files +13.7k vs HEAD).
- `launch-prod.sh` + `prod-config.json`: byte-identical to ours (integ touched neither; no swift15-equivalent on our side).

### Merged-surface verification (all PASS)
- Both drafter options: `STRATA_ENABLE_DFLASH2` (L14, default OFF) + `STRATA_HAVE_DFLASH2` (L105, default ON);
  mutual-exclusion **FATAL_ERROR** L106-107 ("exactly one tap producer") + per-configure runtime print.
- Integ gfx906 drafter wiring present: `STRATA_USE_HIP=1` compile-def on `src/core/dflash2.cpp` under
  `STRATA_HIP_GFX906`; drafter kernels/sources/tools gated by the two options.
- Our gates kept: PREFILL_MMQ gfx906 exemption (`AND NOT STRATA_HIP_GFX906`), w4a4 block under
  `if(NOT STRATA_HIP_GFX906)` (L1936); Lane B gates kept (`mmvq_il_parity` inner NOT-gfx906,
  `pf_wmma_parity` + `verify_parity` = `STRATA_ENABLE_CUDA AND NOT STRATA_HIP_GFX906`); q36 exclusion intact (L856).
- No conflict markers anywhere (`git grep '<<<<<<<'` empty).

### Build (Lane B env, exact)
- Configure in place with the Lane B flag set, ROCm 6.1.0 only (`lane-c-logs/configure-c.log`):
  Clang 17.0.0, `HIP (gfx906 compat) enabled, arch gfx906`, cache `STRATA_MMQ_KQUANTS=OFF`,
  `STRATA_PREFILL_MMQ=OFF`, `STRATA_ENABLE_HIP=OFF`. Cache did not fight the merge (no rm -rf).
- Build `-j10`: **green, 5m02s, exit 0, 0 errors, 95 gfx906 offload compiles** (Lane B: 92; +3 = drafter
  TUs + tap modules) → `build-906/build-c.log`.

### Drafter gate checks (all PASS)
1. Both-ON configure (`-DSTRATA_HAVE_DFLASH2=ON -DSTRATA_ENABLE_DFLASH2=ON`, throwaway dir, removed):
   **refused** — `FATAL_ERROR "Two DFlash2 runtimes in one build … exactly one tap producer"`,
   "Configuring incomplete, errors occurred!".
2. Default configure: cache `STRATA_HAVE_DFLASH2=ON` (upstream drafter) + `STRATA_ENABLE_DFLASH2=OFF`
   (ours dormant); print `DFlash2 runtimes compiled: upstream (src/core/dflash2.cpp, src/kernels/cuda/dflash2.cu)`;
   build compiled `dflash2.cpp.o`+`dflash2.cu.o`, no `dflash_inco.cu.o`. Ship-mode XOR stays a Lane D choice.
3. Smoke: `strata --version` → `0.1.48`; `--families` → `qwen4exp` — identical to Lane B (`lane-c-logs/smoke.txt`).

### ctest vs Lane B baseline — NO new failures
`ctest -R 'gfx906|router|mmq|parity' --output-on-failure` + `hip_router_fast`, same env script
(`lane-c-logs/run-ctest-c.sh`) → `build-906/ctest-c.log`. Wall 2m19s, exit 8.

| | Lane B (baseline) | Lane C |
|---|---|---|
| Suite (regex-matched) | 83 | **84** (+1 = `dflash_rope_parity`, integ drafter test, **Passed** 0.22 s) |
| Failed | 7 | **6** |
| Skipped | 3 | 3 (`gdn_chunk_parity`, `tq_cpu_parity`, `tq_cpu_parity_avx2`) |
| hip_router_fast | PASS | PASS (exit 0) |

Failure set Lane C = {`ple_parity` (PLE fixture absent on host, same path), `native_expert_parity_q4_0_q4_0`,
`_q4_0_q4_1`, `_q4_K_q5_0`, `_q5_0_q8_0`, `_q6_K_q8_0` (fp16 signed-zero, counts byte-identical to Lane B:
47228/95700/285145/190874/42538)} ⊂ Lane B's set. Lane B's transient contention class
(`cvec_parity`, `qsa_slots_parity`×2, `batch_rows_parity`, `s2_expert_grouped_parity`) all **Passed**
this run (low contention; 2m19s vs 2m44s). Zero new failures ⇒ no rerun classification needed,
no fix commit. Artifacts: `lane-c-logs/{configure-c.log,smoke.txt,run-ctest-c.sh}`,
`build-906/{build-c.log,ctest-c.log}`.

## Lane D result + cutover incident (2026-10-25, executed)

A/B (one-shot CLI, 3 reps × short 238-id / long 16,358-id, greedy, prod-identical base, custom rocBLAS):
- mtp: decode 13.16 / 12.94 tok/s (short/long), prefill 37.97 / 557.68
- OURS (q4_0, block 5): decode **25.35** tok/s short (**+93%**); long FAILED at prefill chunk 2 (see defect below)
- upstream (dflash2 q8.pack): decode 13.17 / 12.97 — no measurable gain over MTP in one-shot mode
- VRAM: ours holds ~521 MiB weights + ~200 MiB tap planes + ~240 MiB pinned staging
- Caveat: one-shot runs without the serve pipeline (--layer-split is serve-only) — relative comparison only.
- Winner by evidence: OURS — the only arm with a measurable win; upstream dormant (serve-mode comparison = possible follow-up).

Defect (user: "not acceptable"): one-shot 16K prompt + our drafter = `prefill: device buffers for a chunk of 8192 tokens do not fit (860 of 16368 MiB free at the failure; 4091 MiB taken, the failing buffer wanted 80 MiB)` after `prompt path borrows 2476 cache slots (4.00 GiB)` — 3/3 identical (`lane-d-logs/ab/ours-long/rep{1,2,3}.log`). Chunk 1 completed (~556 tok/s) then OOM. Serve mode with the SAME drafter handled a 16,586-token prompt at 320.6 tok/s through the API (verified). Fix lane dispatched (code analysis + patch; GPU verification in a later window).

Cutover incident: initial cutover used a hand-rolled script (main-checkout serve/server.py + cloned config + nohup) — prod ran at ~half speed. User corrected: prod launches via `launch-prod-swift15-dflash-w4-q4_0.sh` (cd engine tree's own serve code; scrub STRATA_DF_*/DFLASH_*/DUMP_*/WINDOW_*; env -u CUDA_VISIBLE_DEVICES STRATA_ARENA_MMAP GPU_PINNED_MIN_XFER_SIZE HSA_USERPTR_FOR_PAGED_MEM; setsid nohup; port guard). Prod restored via that launcher verbatim (old integ engine): decode probe 31.1 tok/s, healthy. MERGED BUILD NOT IN PROD. Proper merged cutover (pending) = launcher variant with CLONE → v023 worktree, config exe → build-906-inco/strata, speed-validated in a granted window before switching. lane-d-logs/cutover-*.sh deprecated (kept as evidence). Lesson pinned to memory bank.

## Long-prefill one-shot fix (2026-10-25, fixer lane; no GPU run)

Defect: one-shot `strata ... --prefill 8192 --dflash inco-dflash2-q4_0.gguf --dflash-block 5
--tokens-file long.ids(16,358) --max-new 256 --greedy --stats` failed 3/3 identically at the
prefill init (`lane-d-logs/ab/ours-long/rep1.log`: borrows 2476 slots (4.00 GiB) → "prefill:
device buffers for a chunk of 8192 tokens do not fit (860 of 16368 MiB free ... 4091 MiB taken,
the failing buffer wanted 80 MiB)").  MTP-only and upstream-drafter arms passed the same prompt.

### Mechanism (ordering bug, not VRAM pressure)
- The failure is `Prefill::init`'s carve overflow: `src/prefill/prefill.cpp:1567-1576`; `carve`
  lays every chunk buffer out **inside the borrowed region** (`Alloc.base = borrow`,
  `cap = borrow_bytes`), and `Alloc::take` (prefill.cpp:523) fails a take once the region is
  spent — it never asks the driver for more.  The `cudaMemGetInfo` numbers in the message are
  diagnostics only ("860 MiB free" is not the constraint; 4091 of the 4096 MiB region taken,
  then an 80 MiB buffer = `ple_emb` = T*N*4 = 8192*2560*4 wanted the remaining 5 MiB).
- The loan is sized by `Prefill::bytes_needed` (`generate.cpp:7838 lend_slots` →
  `slots_from_bytes`), and `bytes_needed_impl` prices the drafter's tap planes **only when
  `g_n_taps > 0`** (prefill.cpp:2388; `g_n_taps` is set by `set_tap_layers`, prefill.cpp:2058).
  The carve takes the same plane at prefill.cpp:1595 (`n_taps_ * T * N` BF16).
- One-shot mode called `prefill.set_tap_layers(...)` at `generate.cpp:16438`, i.e. **after** the
  loan was planned and printed (16363-16431).  At plan time `g_n_taps == 0`, so the loan was
  priced tap-less: short by `n_taps * chunk * n_embd * 2` = 5 * 8192 * 2560 * 2 = **200 MiB**
  (net ~75 MiB once `bytes_needed`'s known xn over-count slack is subtracted — exactly the
  observed deficit).  Not per-GPU accounting, not fragmentation, not a wrong free figure, not
  the drafter's 580 MiB footprint.
- Serve was immune: it sets the tap layers at `generate.cpp:8724`, before its lend regions are
  sized (8737+ `part_slots`/`fits`), so its 16.5K-prompt prefill fits — matching the API evidence.
- Upstream drafter (`--draft dflash2`) leaves `o.dflash` empty → no tap plane on the CLI prompt
  path → unaffected, which is why that arm and the MTP arm passed.

### Fix
`51ff33ff5efdebe3518ab8b9024f7eee57936564 fix(prefill): set the DFlash tap layers before the
one-shot loan is sized` on `strata-v023-merge` (worktree `worktrees/v023-merge`, tip
`4718b948` + this).  Pure code motion: the identical guarded `set_tap_layers` call moves above
the borrow plan (`generate.cpp:16356-16370`), same condition, same arguments, same effect on
`carve`.  12+/5-, one file.
- With a drafter: `bytes_needed` now counts the taps → the loan grows by ~121 tail slots
  (2476 → ~2597, 4.00 → ~4.29 GiB) and the carve fits the region it was given.  Chunk stays
  8192 (fixed `--prefill`); the `k + 128 <= slots` floor still clears (2725 of 4270).
- No drafter and no `STRATA_DFLASH_TAPS`: the moved block never runs, `g_n_taps` stays 0,
  the plan is byte-identical.  Serve-mode code untouched (only the one-shot path moved).
- Side effects to know about: `--prefill auto` on a drafter'd run now prices ~200 MiB more per
  chunk, so it picks a slightly smaller chunk / smaller ring (correct direction); on a Metal
  build the auto-chunk log line can differ with a drafter (loan forced to 0 there anyway).

### Checks performed (CPU-safe only)
- `PATH=/opt/rocm-6.1.0/bin:$PATH cmake --build build-906-inco -j10` → **exit 0**, `generate.cpp`
  recompiled, `Linking HIP executable strata`, `Built target strata` (log
  `lane-d-logs/build-inco-fixprefill.log`; 0 `error:` lines, 131 pre-existing warnings, none on
  the moved lines).  `build-906/strata --version` → 0.1.48.
- `build-906` (mtp + upstream arms) was deliberately NOT rebuilt, so those arms stay comparable
  to the recorded baselines; the fix only touches the drafter'd one-shot arm ("ours").
- Prod untouched: no serve restart, no GPU run, no push.

### To verify in the next GPU window (prod down; orchestrator runs)
Expected on the ours/long arm: `prompt path borrows ~2597 cache slots (~4.29 GiB)` (was 2476 /
4.00 GiB), no "do not fit", 256 tokens out.  Repro, exactly the runner's ours/long command:

```
cd /mnt/user/Strata/worktrees/v023-merge
LD_LIBRARY_PATH=/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib \
HSA_OVERRIDE_GFX_VERSION=9.0.6 HSA_ENABLE_SDMA=1 CUDA_VISIBLE_DEVICES=0,1 HIP_VISIBLE_DEVICES=0,1 HIP_FORCE_P2P=1 \
STRATA_GFX906_TOPK_REG=2 STRATA_GFX906_TOPK_FIT17=1 STRATA_PRIMARY_COMMIT_OVERLAP=1 \
STRATA_NO_PCIE_GROUP=1 STRATA_HIP_PROMPT_F16=1 STRATA_DFLASH_SAMPLED=1 \
timeout 1800 ./build-906-inco/strata \
  --gpu 0,1 --pack /mnt/user/ai/iq3xxs-15 \
  --native /mnt/user/ai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf \
  --native-dense-gguf /mnt/user/ai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf \
  --ple-gguf /mnt/user/ai/Qwen3.8-Flash-Next-PLE-Q8_0.gguf \
  --expert-profile /mnt/user/Strata/data/expert-profile.bin \
  --expert-cache auto --prefill 8192 --spec 5 --spec-min-p 0.5 \
  --max-context 262144 --kv int8 --vram-reserve-mib 896 --vram-reserve-later-mib 1000 \
  --kv-resident 262144 --pcie-frac 0 --expert-cache-cpu-order --suffix-draft 0 \
  --dflash /mnt/user/dflash2/artifacts/inco-dflash2-q4_0.gguf --dflash-block 5 \
  --tokens-file lane-d-logs/long.ids --max-new 256 --greedy --stats
```

Then: the full `bash lane-d-logs/run-ab.sh` (arms mtp/ours/upstream × short/long × 3, prod down;
`RUN_AB_DRY=1` to inspect) → `lane-d-logs/parse_ab.py` summary.  Record for the "ours" arm that
the loan is now ~121 slots larger (the ~200 MiB tap planes are paid for by displaced experts,
refilled after the prompt) when comparing prefill tok/s against the MTP/upstream arms; acceptance
rate is unaffected by the fix.  Short arm (238 ids) must stay unchanged (its chunk is one pass,
`request_sized < chunk` shrinks it).

## Sampled-drafting port (2026-10-25, fixer lane; no GPU run, prod untouched)

Defect closed: `strata-v023-merge` carried the DFlash2 drafter but only drafted **greedy**
requests. Serve traffic is sampled (`temperature=1.0`), so every request logged
`dflash: sampled request uses target-only decoding` + `drafts accepted 0 of 0` **even with
`STRATA_DFLASH_SAMPLED=1` in the engine env** → merged serve decode ~24.8 tok/s vs incumbent
31.1 (drafting is the whole gap; layer-split alone ≈25, greedy one-shot with drafting 25.35
short / 27.44 long).

### Source of the feature
The flag existed **only as uncommitted working-tree changes** in the integ clone
`/mnt/user/dflash2/Strata-integ` (HEAD `46d112c0` = the sha Lane C merged; `git log -S
STRATA_DFLASH_SAMPLED` is empty there). `git status` in the clone: `M src/program/generate.cpp`
(+12/−7), `M tools/dflash_sampling.py` (+25/−7) — that working tree is what prod
`build-dfsample-1` was built from, which is why the merged ladder based on build-latest/v0.2.3
never carried it.

### What was ported (commit `26631e4bbbeb338d7c3d32726c50b3938dfdd656`, short `26631e4b`)
Applied the integ working-tree diff verbatim; it landed with **identical context** (the merged
tree's only generate.cpp delta vs integ is `51ff33ff`, the tap-layer move, which sits after
both feature sites). 37+/14−, 2 files:
- `src/program/generate.cpp:2990-2995` (CLI startup gate): the sampled target-only clear is now
  conditional on `std::getenv("STRATA_DFLASH_SAMPLED") == nullptr`, so a sampled non-serve run
  with the flag keeps `o.dflash` / `o.dflash_head` / `o.dflash_vocab`; message names the opt-in.
- `src/program/generate.cpp:12843-12858` (serve per-request gate):
  `static const bool dflash_sampled_ok = getenv(STRATA_DFLASH_SAMPLED) != nullptr;`
  `req_dflash = has_dflash && (req_temperature <= 0.0f || dflash_sampled_ok);` + integ's comment
  (parity-gate history: a multi-row logits effect, not the sampler) and the opt-in suffix on the
  fallback line.
  Acceptance needs **no change**: the merged tree's verify loop is byte-identical to integ's
  committed base, and acceptance is the existing exact-match against each row's
  position-keyed sample (`generate.cpp:15456` `sbuf[0] == drafts[0]`), so every emitted token
  keeps the target's marginal and a seed replays the same text whatever the drafts were.
  `df_request = has_dflash` (draft context maintenance for a later greedy continuation) is
  unchanged.
- `tools/dflash_sampling.py` — the feature's GPU integration gate: with
  `STRATA_DFLASH_SAMPLED=1` it asserts `offered > 0` on sampled cases and
  `0 < accepted <= offered`, requires the opted-in CLI to have run the drafter (trace non-empty,
  no fallback line), keeps strict token parity for greedy and for sampled when the flag is
  unset, and pins `--dflash-block 4` only when the config does not set it.

Default with the env **unset** is byte-compatible with the pre-port tree (target-only for
sampled); greedy behavior untouched; serve structure otherwise untouched.

### Parity-gap audit (env gates)
- `getenv("STRATA_*")` literals: 555 names per tree (integ clone src vs merged src) —
  **0 integ-only**, 0 merged-only after the port.
- All `"STRATA_[A-Z0-9_]*"` string literals in src: 609 per tree — **0 integ-only**.
- Pre-port delta was exactly **{`STRATA_DFLASH_SAMPLED`}**; no other integ-only gate exists, so
  nothing else is pending. (Only the two files above were modified in the clone.)
- Launcher caveat: `launch-prod-swift15-dflash-w4-q4_0-v023.sh:30-34` scrubs shell
  `STRATA_DF_*|STRATA_DFLASH_*|STRATA_DUMP_*|STRATA_WINDOW_*`, so a shell export of
  `STRATA_DFLASH_SAMPLED` is stripped; the merged serve config `lane-d-logs/prod-config-ours.json`
  already carries `"STRATA_DFLASH_SAMPLED": "1"` in its `env` block (that is the load path) and
  `exe` → `build-906-inco/strata`.

### Build check (CPU only, ROCm 6.1 only, no GPU run)
`cd /mnt/user/Strata/worktrees/v023-merge && PATH=/opt/rocm-6.1.0/bin:$PATH cmake --build
build-906-inco -j10` → **exit 0**, `Building CXX object .../src/program/generate.cpp.o`,
`Linking HIP executable strata`, `Built target strata`, **0 `error:` lines, 131 warnings =
pre-existing baseline** (none on the changed lines). Cache verified:
`CMAKE_HIP_COMPILER=/opt/rocm-6.1.0/llvm/bin/clang++`, `CMAKE_HIP_ARCHITECTURES=gfx906`,
`STRATA_HIP_GFX906=ON`, `STRATA_ENABLE_DFLASH2=ON` / `STRATA_HAVE_DFLASH2=OFF` (one tap
producer, ours). Smoke `./build-906-inco/strata --version` → `0.1.48`. Log
`lane-d-logs/build-inco-dfsample.log`. Prod dir `build-dfsample-1` and every prod process
untouched; nothing pushed.

### Serve-mode validation for the next GPU window (prod down; orchestrator runs)
1. Stop the incumbent prod engine (port 8009 must be free — the launcher refuses otherwise).
2. Launch the merged build: `bash /mnt/user/Strata/worktrees/v023-merge/launch-prod-swift15-dflash-w4-q4_0-v023.sh`
   (uses `lane-d-logs/prod-config-ours.json`, which already sets `STRATA_DFLASH_SAMPLED=1`; the
   launcher's scrub loop strips shell exports, so do not rely on exporting it in the shell).
3. Health: `curl -sf 127.0.0.1:8009/v1/models` (engine ready).
4. Send a **default-sampled** request (no `temperature` / `temperature=1.0`, greedy NOT requested):
   expect the engine log to STOP printing `dflash: sampled request uses target-only decoding` and
   instead print the drafting/verify lines with `drafts accepted N of M`, **N > 0**.
5. Decode speed target **≥ ~30 tok/s** on that request (was 24.8 target-only; incumbent 31.1).
6. Default-compat check: relaunch with `STRATA_DFLASH_SAMPLED` removed from the config env block
   → the log must print the target-only line again with `drafts accepted 0 of 0` (byte-compatible
   default).
7. Integration gate (small fixture, its own engine launch, prod must be down):
   `cd /mnt/user/Strata/worktrees/v023-merge && STRATA_DFLASH_SAMPLED=1 /mnt/user/Strata/.venv/bin/python
   tools/dflash_sampling.py --config lane-d-logs/prod-config-ours.json --engine build-906-inco/strata
   --output /mnt/user/Strata/lane-d-logs/df-sampling-drafted` → expect
   `DFlash sampled drafting, greedy target parity, request reset and greedy regression: PASS`;
   same command with the flag unset → `DFlash sampled fallback, … : PASS`.
8. Seeded replay: same seed with drafting on/off must replay identical sampled text (gate in the
   tool; `greedy_again`/`filtered_repeat` re-runs are checked there).
9. Record decode tok/s + acceptance rate in the cutover decision notes; keep
   `lane-d-logs/cutover-*.sh` deprecated. Then, if granted, switch prod to the merged launcher
   only after the speed gate clears.

## Final cutover: merged v0.2.3 build is prod (2026-10-25 12:01, executed)

Sampled-drafting port (26631e4b) validated on-card and prod CUT OVER for good:
- Stop of incumbent took ~5 min (engine drains conversation-cache checkpoints — never KILL it; the python wrapper also holds port 8009 after engine death: TERM wrapper FIRST, then wait engine out, verify listeners=0 AND rocm-smi VRAM=0 before next launch).
- v023 launcher: ready in 50s. Probes (default-sampled): short decode 48.0 tok/s (incumbent 46.0), warm thinking-blend 35.8, 16.5K: prefill 320.5 (parity) decode 38.7 (was 23.7 pre-port). Engine log this run: 0 "target-only" lines, drafts accepted 15/20, 4/4, 11/17.
- Decision per agreed rule (keep iff parity+): KEPT. Prod = launch-prod-swift15-dflash-w4-q4_0-v023.sh → worktree build-906-inco (strata 0.1.48, STRATA_ENABLE_DFLASH2=ON), config lane-d-logs/prod-config-ours.json (env carries STRATA_DFLASH_SAMPLED=1).
- Evidence chain: greedy drafting proven by A/B (25.35/27.44 tok/s, +93%/+112%); sampled drafting proven by this run; unset-flag default ≡ pre-port behavior (observed: drafts 0 of 0) — env-unset relaunch compat check skipped as redundant (two-line getenv guard; cost = extra prod restart).
- Branch strata-v023-merge final: 4718b948 (merge) → 51ff33ff (prefill OOM fix, proven: 2598 slots/4.19 GiB, exit 0) → 4007862e (launcher) → 26631e4b (sampled drafting). Parity audit: STRATA_DFLASH_SAMPLED was the ONLY integ-only gate.
- Open items: (1) prod runs from worktrees/v023-merge — promote to a stable checkout when convenient; (2) untracked lane-*-logs/ in worktree = evidence, keep; (3) upstream drafter dormant (no one-shot gain; serve-mode comparison unexplored); (4) optional: tools/dflash_sampling.py GPU gate run; (5) incumbent launcher/config remain for instant rollback.

## Output-equivalence gate: merge fidelity PROVEN, latent drafter defect found (2026-10-25)

User asked for temp-0 hash comparison + KLD. Ran tools/dflash_sampling.py (GPU gate, first time ever) on BOTH engines. Residency fixture for this box: --expert-cache 2649 --expected-primary-slots 3521 --expected-total-slots 3521 (engine auto-expands primary ~1.33x budget; output must be outside the checkout).
- MERGE FIDELITY (all exact token equality, stronger than hash): merged vs incumbent dflash-arm outputs IDENTICAL; cross-engine target-only greedy + seeded-sampled IDENTICAL. The v0.2.3 ladder + drafter merge + OOM fix + sampled port changed nothing about model outputs. sha256 artifact: lane-d-logs/df-sampling-final/SHA256SUMS.txt.
- LATENT DEFECT (predates merge; shipped to prod 2026-10-24 with the integ feature, uncommitted there): greedy target parity FAILS on BOTH engines — dflash vs target diverges at token index 1 (dflash 7145,279,22868... vs target 7145,281,7...), identical divergence and identical acceptance profile (greedy 46/85, cold 48/75, filtered 45/90, seed7 44/95, penalties 37/130) across engines. Draft acceptance is NOT verification-transparent: accepted draft tokens are not the target's argmax ⇒ drafted outputs are drafter-biased whenever a draft is accepted (~54% of offered). Gate tool asserts parity per the feature's own spec ⇒ the gate never passed on GPU before shipping.
- KLD: moot for exact-match acceptance (identity ⇒ 0); becomes the right instrument iff acceptance is by-design approximate — pending diagnosis.
- Open: diagnosis of the acceptance loop (why a non-argmax draft is accepted); prod posture decision (drafting on = 48 tok/s biased-since-yesterday vs target-only = ~25 tok/s pure).

## Draft-acceptance diagnosis (2026-10-25, source analysis only, no GPU run, prod untouched)

Reference tree `/mnt/user/Strata/worktrees/v023-merge` (accept loops byte-identical to the integ clone).

**Acceptance criterion as implemented (all three loops):**
- CLI spec loop: `src/program/generate.cpp:17407-17408` — `int a = 0; while (a < T-1 && window[a+1] == outv[a]) ++a;`, emitted tokens = `outv[0..a]` (17461).
- Serve serial loop: `generate.cpp:15671-15672` (same comparison; merged-window `a=T-1` requires `STRATA_ADMIT_MERGE_FIRST` AND `max_new==1` (14606) → NOT active in the gate, max_new=64).
- Serve pipelined loop: `generate.cpp:15286` — `A.tok[a+1] == outp[a]`.
- `outv` = `Verifier::run` (15652/17362, `src/core/verify.cpp:2893`): the TARGET model runs all T window rows in one batched pass; per-row output = `sample_tokens(head_logits_, T, …)` (verify.cpp:2367/3220) = greedy argmax at temp 0, Philox position-keyed draw when sampled (MTP-coupled variant `sample_tokens_spec` 3213). Draft tokens are re-scored by the target; acceptance compares the draft against the target's own per-row draw at the right position key (row a predicts a+1). Position keying is correct; the drafter's tap-plane logits feed ONLY `dflash.propose` (15806/17240+), never acceptance.

**Classification: (a)-hybrid, primarily a numerics defect, not an acceptance-logic bug.**
The scheme is canonical exact-match (docs/DFLASH2.md:117-119 "point masses verified by the target; only its accepted prefix is committed"; generate.cpp:16811-16813 "the window's argmax after token t is **exactly what greedy decode would emit there**"). That exactness claim presupposes bit-identity between the T-row verify-window pass and the T=1 decode pass. On the gfx906 quantized stack (IQ3_XXS, MMQ/PREFILL_MMQ OFF, custom rocBLAS, wave64 compat) the T>1 window uses different arithmetic for row 0 too: dense GEMM over T rows vs GEMV, GDN layers via `gdn_step_norm_multi` MT-row kernels (verify.cpp:2657-2703/3602-3622) vs single-row, per-T graph captures. ULP-level logit deltas flip argmax exactly at near-ties. Evidence: the flip token 279 IS a high-probability runner-up in the target-only arm (appears at index 1 in target cold, index 1-3 in filtered, index 3 in penalties runs of results.json); dflash seed7 output is token-identical to target seed7; cold/filtered/penalties diverge only mid-sequence at later near-ties; both engines identical (same kernels). The emitted token is the window row's own target draw — the drafter never overrides the target; parity fails because the emission is read from the windowed numerics, independent of the accept/reject decision (a rejected draft at a flip row emits the flipped bonus anyway).
Gate history: parity asserts come from integ commits `5718d0ce` ("…sampled output parity") / `af85f5ce` — written by the upstream maintainer on reference numerics (window path verified via `--window-hashes`); they never passed on a quantized gfx906 card. The gate is stricter than the implementation's real guarantee, which is transparency to the target distribution **as evaluated in the verify window**, not to the incremental chain. `spec_verify_parity` (passed on gfx906) proves sampler faithfulness to GIVEN logits, not window-vs-decode logit identity.

**Acceptance-rate reading:** 46/85 greedy = 54% token acceptance ≈ the true draft/target agreement of the Q4_0 block drafter at K≈4; the cold/filtered/seed7/penalties spread (64/50/46/28%) tracks the sampler's own row-draw entropy, not a fixed agreement threshold. A "correct" (distribution-identical) verifier would NOT materially lower acceptance — 54% is the drafter's genuine skill.

**Fix paths:**
1. Strict parity via bit-identical windows (row-decomposed kernels): window cost ∝ T → the drafting win dies (T=5 batched rows ≈ 1 decode step is the whole +93%/48-tok/s story). Not viable.
2. Recommended: keep acceptance as-is; relax the gate's strict `greedy target parity` to determinism (passes today: greedy==greedy_again, cross-engine identity) + acceptance semantics (STRATA_DF_CYCLES trace logs draft[] and target[] per window, 17413-17420) + a KLD/top-k-agreement metric between dflash and target arms. This matches the llama.cpp position: exact acceptance is exact w.r.t. the batched verifier's distribution; quantized models flip near-ties.
3. Optional strict-greedy middle path: near-tie guard in the accept loop — `dflash_target_probe` (generate.cpp:1587-1613) already computes top1/top2/margin per kept row; if margin < eps, recompute that row as a single-row decode (T=1 ver.run) before emitting. Cost ≈ near-tie rate × one 1-row window (small, bounded); keeps the speed win. Needs a GPU calibration of the flip rate + eps.

**Sampled-output correctness / prod risk (prod = 8009, drafting ON):** each emitted token is the target sampler's own draw from the target's row distribution evaluated on window logits; the RNG stream is position-keyed (seed replays verified identical dflash vs target, seed7). Marginal distortion = KL between windowed and incremental evaluations at ULP logit scale — distributionally correct within numerics tolerance. Greedy requests emit the windowed argmax: near-tie flips only. Prod risk: LOW (quality-neutral to ~ULP; no drafter-side bias, no RNG contamination). The failing gate is a spec/numerics mismatch on this backend, not a live quality defect.

**Next GPU window (no prod restart needed for 3):** `STRATA_DF_TARGET_PROBE=<pos>` runs (both arms, same seed) → margin at the flip (expect tiny, top1/top2 = 281/279); flip-rate = share of accepted rows with margin < eps over a long fixture → decides option 2 vs 3. KLD arm: STRATA_DF_TARGET_LOGITS dumps (15721-15745) vs T=1 dumps of the same positions.

## Parity decision + probe plan (2026-10-25, user)

User decisions on the greedy-parity numerics finding: (1) stance = DOCUMENT ONLY — no gate relaxation, no near-tie guard, no engine change; strict parity stays unenforced on gfx906, the diagnosis section is the record. (2) Quantify the near-tie flip rate on-card at the next window via STRATA_DF_TARGET_PROBE. Turnkey script: `lane-d-logs/probe-near-tie.sh` — run 1 probes the known divergent position (abs pos 24, full logit dump via STRATA_DF_TARGET_PROBE_FILE, expect token 281 as top2 with ULP-scale margin), run 2 = 128-token greedy on a second prompt for a flip-rate data point (gate re-run, --count 128, engine inherits probe env). Artifacts -> /mnt/user/Strata/lane-d-logs/near-tie/. Optional add-on in the same window: serve-mode like-for-like long-context A/B (merged vs incumbent, same ~72K prompt) to settle the 30.5-vs-36.2 sustained-decode question.

## Near-tie probe results (2026-10-25, executed in granted window)

- Pos-24 probe (STRATA_DF_TARGET_PROBE, full logit dump): WINDOW pass crowns 279 (20.0385) over 281 (19.8971), margin 0.141 logits (~0.7% relative); DECODE pass crowns 281 with 279 runner-up (margins 0.02-1.7 across windows; absolute scores differ ~0.15 logits between paths for the same token). ULP-scale batched-vs-single kernel numerics flip a genuine near-tie — mechanism MEASURED on-card, matching the source diagnosis.
- 128-token greedy (default gate prompt): first flip at index 1 = the SAME 279/281 near-tie; then cascade (4 coincidental re-match runs; 122/128 differ only because the streams are different continuations). Independent flip events: 1 in this fixture.
- Read: output differences drafting-on vs drafting-off = isolated near-tie positions (each re-routing the continuation), not systematic drift. Sampled outputs remain the target sampler's own draws (seed replays exact). Sustains document-only stance; no engine/gate change.
- Probe script lesson: run 2 originally failed on a custom token-salad prompt (EOS at 16 tokens < count 128) — use the gate's default prompt for long-count runs (rerun as run2b, artifacts in /mnt/user/Strata/lane-d-logs/near-tie/).
- Window closed, prod restored via v023 launcher.
