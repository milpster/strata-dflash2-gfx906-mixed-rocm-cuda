# Static diff: fork (A) vs upstream 0.2.18 port (B) — 144k prefill

**EVERY CLAIM IN THIS FILE IS UNCONFIRMED STATIC ANALYSIS.** Nothing here was built, booted, benchmarked
or measured by me. Everything is read from source at two pinned trees, from boot/engine logs that already
exist on disk, and from this repo's journals. Per CONTRIBUTING.md, findings produced by reading code are
labelled as such and are not measurements. Confidence per claim is stated where it matters.

Read-only: no build, no engine, no launcher, no GPU, no `:8009`, no commit. The only file written is this one.

## 0. What could be read, and what could not

| Lineage | Tree | Commit | Engine | Read? |
|---|---|---|---|---|
| A (fork) | `/mnt/srcds/Strata` (object DB, branch `merge-helper-ipc-diet`) | `564e1dbbead9ef0e7e43de1c29d2f17bf8c02669` | 0.1.57 | yes, in full |
| B (upstream port) | `/mnt/srcds/Strata` object DB, branch `port-0218` (worktree `/mnt/srcds/Strata-wt-0218`) | `db5bc3ab1078691956cbf98767f855dbc81a630e` | 0.1.64 | yes, in full |

Both lineages are in one object database, so the diff below is a real content diff, not a reconstruction.
Path mapping is the one `docs/PLAN-UPSTREAM-0218.md` §3 states and I re-verified: `src/` → `engine/src/`,
`include/` → `engine/include/`.

Provenance (corrected per the parent's amendment — the shas are **binary** sha256s, not commits):

* A: `run/fsclamp-bin/rpcw-hip/strata` = `4b8251e9e06334a8…` (`run/fsclamp-bin/SHA256SUMS`), helper
  `9d16742d5f30e9c1…`; built from the fork tree whose tip is `564e1dbbea` (`CHAMPION.md:22-23`,
  `bench/ab-v0210/rpcprof/port_ab_preflight.py:41-45` says "built from the merge-helper-ipc-diet tree at 564e1dbbea").
  `CMakeLists.txt:11` = `project(strata VERSION 0.1.57)`.
* B: `run/port0218-nohuge-bin/rpcw-hip/strata` = `15d330ea4d0eec4c…`, helper `58c3ae02…`. The
  promotion record names the build: "0.2.18 port, engine 0.1.64: the build that made 936efcc7… plus
  `db5bc3ab10`" (`journal/promotion-pool-nohuge-2026-11-24.md:88`), the patch being
  `MADV_NOHUGEPAGE` on the pool (`journal/promotion-pool-nohuge-2026-11-24.md:90`). The `port-0218`
  branch tip is exactly `db5bc3ab10` ("RAM pool: opt-in MADV_NOHUGEPAGE on the pool mapping").
  `engine/CMakeLists.txt:13` = `project(strata VERSION 0.1.64)`.

Not readable / not used: the prebuilt binary trees themselves are binaries (no symbols read here); I did
not inspect `v0215` or `run/port0218-server` server trees beyond confirming the engine argv they produced
(both engine start lines in the logs are byte-identical except `--conversation-spill-mib 0`, so the servers
are not a lever for the engine-reported read rate). The 2× Radeon VII hardware was not touched.

## 1. The 144k comparison as it was actually run (from the on-disk logs)

Source: `bench/ab-v0210/rpcprof/out/port-ab-20261123/logs/W2-A-dec-r1.engine.log` and `W2-B-dec-r1.engine.log`.
Both configs carry **`STRATA_PREFILL_MMQ=0`** (verified in
`…/configs/W2-A-dec-r1.json` and `…/W2-B-dec-r1.json`, env block), plus
`STRATA_PREFILL_HELPER=1`, `STRATA_HIP_PROMPT_F16=1`, `--prefill 16384`, `--layer-split 30`, `--spec 5`.
The two env blocks differ in exactly one key: `STRATA_POOL_NOHUGEPAGE=1` (B only) — the declared structural pair.

| Item | A (fork) | B (port) | Log line |
|---|---|---|---|
| mmq audit | `prefill mmq: off (env=0) [built=1]` | `prefill mmq: off (env=0) [built=0]` | A:37 / B:40 |
| prompt-kernel line | *(none — the fork prints no such line)* | `strata: prompt kernels: every chunk runs 48 of 48 layers on the int8 kernels.` | B:64 |
| dense matrices | 301, 2331.54 MiB | 188, 1474.17 MiB | A:14 / B:14 |
| dense per-stage trim line | *(absent)* | `layer split: CUDA0 loads the dense weights of layers 0-29 only` | B:12 |
| cache slots | 4951 (9.17 GiB) | 5739 (10.64 GiB) | A:39 / B:42 |
| resident experts | 9125 | 11149 | A:60 / B:63 |
| helper fill | 3655 slots | 3683 slots | A:51 / B:54 |
| prompt-tier helper traffic (3 prompts) | 270 reqs / 68775 rows | 270 reqs / 69116 rows | A:125 / B:127 |
| read of 143893 tokens | 446260 ms (322.4 tok/s) | 481769 ms (298.7 tok/s) | A:112 / B:115 |

Arithmetic for per-chunk cost (§5 uses it): 143893 tokens at `--prefill 16384` is **9 chunks**
(8×16384 + 12821), i.e. 432 layer-walks per prompt (9 × 48). Median rates 320.65 vs 305.60 tok/s
(`journal/w2-window-2026-11-24.md`) mean 448.7 s vs 470.9 s: **≈22.1 s total, ≈2.45 s per chunk**.
Any candidate must be able to hide ~2.5 s in a chunk that takes ~50 s.

## 2. `prefill mmq built=1` vs `built=0` — precise verdict

**It is a compile-time (link-time) property, not a capability probe and not a size/quant guard, and it
cannot cause this gap: the runtime state is `off` in both arms because both configs set `STRATA_PREFILL_MMQ=0`.**

* The guard: `on = mmq::built() && (env == nullptr || std::atoi(env) != 0)` — A
  `564e1dbbea:src/prefill/prefill.cpp:1625`; B `db5bc3ab10:engine/src/prefill/prefill.cpp:2167`
  (B adds a `!gp100_fp16_default(env)` term). The audit string is printed two lines later
  (A `:1629-1630`, B `:2170-2171`). With `env="0"`, `on` is false in both, so
  `p.fallback = !on || layers <= 0` is true in both (A `:1635`, B `:2176`) and no layer takes MMQ
  (`p.layer` stays all-zero; the per-layer loop is gated on `on`).
* What `built()` is:
  * A: the MMQ translation unit is compiled in and `mmq::built() { return true; }`
    (`564e1dbbea:src/prefill/moe_mmq.cu:305`), forced on by `target_compile_definitions(… STRATA_PREFILL_MMQ=1
    STRATA_PREFILL_FUSED=1)` (`564e1dbbea:CMakeLists.txt:3122` and `:3267`; option at `:137`).
  * B: llama.cpp's MMQ kernels are **not in the tree at all** — "llama.cpp's MMQ left the tree in 0.2.16"
    (`db5bc3ab10:engine/cmake/backends/hip.cmake:30`, `:42` builds `strata_pf_own` with
    `src/prefill/mmq_absent.cpp`), and `mmq_absent.cpp:19` is `bool built() { return false; }`. A test even
    asserts the shipped binaries carry no MMQ symbols (`hip.cmake:49-56`, `tools/check_no_mmq.py`).
  * `prefill.cpp`'s own `#ifndef STRATA_PREFILL_MMQ` stub block also answers false, but that is the
    "no native-expert sources" build, not this one (A `prefill.cpp:115-155`, B `:153-193`).
* So: A = **MMQ compiled in, declined at runtime by the env**; B = **MMQ does not exist in this lineage, and
  the env value is moot**. The `[built=N]` field is an audit of availability only. Both arms ran the same
  *runtime* state for MMQ, which is why this difference is the one concrete boot-line difference that is
  **excluded as a cause** (high confidence).

What each engine does instead (this is the real difference, and it is candidate #1):

* A, with `on=false`: no MMQ layer, and the fused kernel path requires `use_mmq || fused_only`
  (`564e1dbbea:src/prefill/prefill.cpp:4979-4983`), so neither runs; the layer takes the **FP16 path —
  "STRATA_PREFILL_MMQ=0: the FP16 path everywhere (the A/B)" (`:1565`), "each expert dequantized to FP16 once
  per super-chunk, then … two FP16 GEMMs" (`:7986-7989`)**. The boot log confirms the absence of the fused
  notice (no `prompt experts on the fused int8 kernels` line, which is printed at `:5029-5033`).
* B, in a build without MMQ: `native_expert_i8` is the default prompt-expert kernel on **every** card
  (`db5bc3ab10:engine/include/strata/kernels/i8_expert_rule.hpp:34-38`,
  `engine/src/prefill/prefill.cpp:234` calls `native_expert_i8_set_mmq_free()` before `main`), and only
  formats/sizes/group-size can push a layer back (`i8_expert_takes`, `:56-59`; `kI8MinGroupEntries = 256`,
  `:32`). The start-up line reports the actual split: 48 of 48 layers on int8
  (`engine/include/strata/prefill/own_only_line.hpp:41`, printed at `prefill.cpp:2253-2257`/`:4053`).

## 3. Inventory: every prefill-relevant change between the two trees

Per-file line counts are `git diff --no-index` of the mapped paths (A `src/x` ↔ B `engine/src/x`).

| Path (`src/prefill/` or `include/strata/prefill/`) | A→B | gfx906 prefill relevance | What it is |
|---|---|---|---|
| `prefill.cpp` | +2047 / −309 | **HIGH** | own-only int8 dispatch, i8 group plan (host, per layer), staged/fused entry rules, share rules, tail loan, claim plumbing; span machinery unchanged |
| `moe_fused_iq.cu` | +1352 / −94 | none | gfx11/gfx12 WMMA expert tiles (gfx906 not covered; `pf_wmma()` hard-false on HIP, A `prefill.cpp:1610-1612`) |
| `kernels.cu` | +182 / −57 | low | `moe_combine16` (gfx11 dm16), gdn chunked (gfx12 ≥30k tokens), `fewrows_record` (research), FP16 prompt helpers |
| `moe_fused.cu` | +69 / −27 | none | CUDA-era fused int8; A `prefill.cpp:158`: "the fused int8 experts are CUDA-only" |
| `gemm.cu` | +35 / −12 | none | hipBLASLt packaged table now gfx1151-only (`:316-322`); `prompt_f16` default now gfx103x/780M (`:609-625`); gfx906 unchanged |
| `share_rules.hpp` | +84 / −1 | low-med | link-aware CPU-share limit (BW-H) + gfx12 limit 1536→2304; no share line in either boot log, so not demonstrably active |
| `prefill.hpp` | +28 / −3 | med | loan tail semantics (−1 vs floor−1), stats fields |
| `mmq_resident_sort.hpp` | +82 / −12 | none | MMQ-only |
| `moe_f16_common.cuh` / `moe_f16_kq.cu` | +17/−5, +15/−10 | low | FP16 path, both sides keep it |
| `kernels.hpp`, `moe_fused*.hpp`, `pf_fused_override.hpp` | small | none | declarations/flags |
| 46 files (incl. `moe_mmq.cu`, `routed_stream.hpp`, `pipeline_chunks.hpp`, `moe_ws*`, `copy_events.hpp`) | **identical** | — | the expert stream/ring/staging machinery and the whole MMQ implementation are byte-identical |
| new in B: `mmq_absent.cpp`, `moe_helpers.cu`, `moe_p1a.cu`, `moe_fused_iq_q36.cu`, `kv_host_dma_parity.cpp`, 22 rule headers | — | `mmq_absent` decides `built=0`; `moe_helpers` carries the own kernels | |

Outside `prefill/`, two defaults matter:

* **Dense-weight residency**: A `generate.cpp:946` `bool trim_stage_weights = false;` (opt-in
  `--trim-stage-weights`, `:4128`); B `generate.cpp:1291` `bool trim_stage_weights = true;` with
  `--no-trim-stage-weights` (`:4592`) and an env override (`:6336`). A therefore replicates all 48 layers'
  dense weights on each stage (301 matrices / 2331.54 MiB), B loads only its stage's layers (188 / 1474.17 MiB).
* **Pool THP**: B `engine/src/core/ram_pool.cpp:237-252` — `MADV_NOHUGEPAGE` when the env var is merely
  *present*. A has no such code path (the config's `STRATA_POOL_NOHUGEPAGE` has no consumer in A).

## 4. What did NOT change (and therefore is not a candidate)

| Ruled out | Evidence |
|---|---|
| Chunk geometry at 144k | Both `--prefill 16384` with a layer split. B's equal cut applies to **one GPU only** (`equal_cut_rule.hpp:96-124` "a layer split keeps today's cut"; `generate.cpp:12233-12240` `pick(env, one_gpu)`), so W2 (2 GPUs, `--layer-split 30`) uses `today_chunk` = 8×16384 + 12821 in both arms. A's equal cut is opt-in `STRATA_PREFILL_EQUAL=1` only (`A generate.cpp:11158-11166`). B's tail-0 auto scan needs `--prefill auto` (`fit_out_rule.hpp:3,26-31`); an explicit `--prefill N` keeps today's count. |
| Expert streaming / ring / staging | `routed_stream.hpp`, `pipeline_chunks.hpp`, `copy_events.hpp`, `moe_ws*` byte-identical (§3) |
| MMQ plan | off in both by env (§2) |
| Helper claim volume | 270 reqs / 68775 rows vs 270 / 69116 rows (+0.5 %) (§1) |
| Server tree | engine argc/argv identical except `--conversation-spill-mib 0`; the rate is the engine's own `prompt … read in` line |
| Chunk pipeline / PLE / handoff code | same source |

## 5. Ranked candidates

Sign convention: a candidate can make **B slower** (explains the −4.7 %) or **A faster** (a confound that
means the engine regression is larger than −4.7 %). Both are listed honestly.

### #1 — The prompt expert GEMMs are different code: A = FP16 dequant+GEMM path, B = Strata's own int8 dot kernels
* (a) A `src/prefill/prefill.cpp:1565`, `:4979-4983`, `:7986-7989` (FP16 path everywhere when `STRATA_PREFILL_MMQ=0`; fused needs `use_mmq`); boot log A:37 `off (env=0)`. B `engine/include/strata/kernels/i8_expert_rule.hpp:32-59`, `engine/src/prefill/prefill.cpp:228-237`, `engine/include/strata/prefill/own_only_line.hpp:41`; boot log B:64 "48 of 48 layers on the int8 kernels". This is the only difference that touches ~all of a chunk's MoE FLOPs; everything around it (gather, ring, bounds) is shared or identical.
* (b) Knob: **B `STRATA_EXPERT_I8=0`** forces B's own FP16 path (`i8_expert_takes` returns false at state 0), i.e. the closest same-binary control for A's kernel class. A cannot be moved to int8 (the fused/own int8 kernels are CUDA/gfx11+ in the fork, A `prefill.cpp:158`); A's `STRATA_PREFILL_MMQ=1` would change the walk type, not to int8.
* (c) Span: `expert_us` on claiming layers and `post_us`/`rest_us` on non-claiming ones (fields in A `:6934-6949`, B `:8431-8446`).
* (d) Prediction if this is the cause: same chunk `tokens` count, same `intake_us`/`tail_us`, B's `expert_us`+`post_us` ≈5 % higher, i.e. `walk_us` absorbs the whole delta. Direction is not knowable from source (B's own header cites RDNA2 numbers where int8 beats MMQ by 1.10-1.35× and calls FP16 "4-5× slower" on an RX 6700 XT; gfx906 has MFMA fp16 and wave64 dp4a, neither measured here), so treat the sign as an open question this leg would settle.

### #2 — Dense residency default → cache slots → resident experts → per-chunk miss volume (confound that favours B)
* (a) A `generate.cpp:946` (trim off by default) vs B `generate.cpp:1291` (trim on by default, `:4592`/`:6336`). Consequence at boot: 301/2331.54 MiB vs 188/1474.17 MiB, free VRAM 9.67 vs 11.14 GiB, **4951 vs 5739 slots**, **9125 vs 11149 resident experts**, decode hit 96.1 vs 97.4 % (§1, `journal/w2-window-2026-11-24.md:39-43`). More resident experts = fewer expert blobs pulled per chunk.
* (b) Knob: **A `--trim-stage-weights`**, **B `--no-trim-stage-weights`** (plus the env override at B `:6336`).
* (c) Span: `intake_us` and `claim_us` (expert copy/staging volume), *not* `expert_us`.
* (d) Prediction: with A trimmed, A's slots/expert residency rise toward B's and A gets ~0-2 % faster; with B untrimmed, B loses the same. If matching this closes the gap, the "engine regression" is really this residency difference. **It cannot explain B being slower** — it is B's advantage, so the true kernel/dispatch cost in B is larger than 4.7 %.

### #3 — The own-int8 walk's per-layer host work and stream structure (group plan, bounds, launches, syncs)
* (a) B builds the int8 group plan on the host per layer (`engine/src/prefill/prefill.cpp:6905-6960`: `i8_same`, `i8_groups = ceil(i8_experts/MMQ_GROUP)`, per-group bounds), then runs one launch pair per open group (`:7653-7700`, `:7749-7800`); A's FP16 path stages per expert and runs two FP16 GEMMs per group (`A prefill.cpp:7986-8000`). Static sync counts in `prefill.cpp`: A 56 `cudaStreamSynchronize`/2 `cudaDeviceSynchronize`, B 60/3 (weak proxy, both files much larger).
* (b) Knob: B `STRATA_I8_COUNT=1` (diagnostic: per-chunk line of how many routed pairs took int8 vs the fallback, B `:2053`), and `STRATA_EXPERT_I8` to take the whole path out.
* (c) Span: `barrier_us`, `group_us`, `rest_us`, `claim_us` in the layer line.
* (d) Prediction: B shows extra `barrier_us`/`group_us` per layer on the *plus* side of `layer_us`, with `expert_us` itself level — i.e. per-layer overhead, scaling with 48 layers × 9 chunks, not with expert rows.

### #4 — `STRATA_POOL_NOHUGEPAGE=1` (B only, the declared structural flag) — low, likely nil here
* (a) B `engine/src/core/ram_pool.cpp:237-252` maps the 32 GiB pool with `MADV_NOHUGEPAGE` before pre-faulting. A has no consumer. But both boots print `RAM pool store: … resident_pairs=0 hits=0 misses=0` (A:26 / B:28) and `--expert-helper-ram-relief: nothing dropped: this run's expert source is not the arena` (A:55 / B:58), so the pool holds no expert pairs in this configuration: the advice changes page size on a pre-faulted, functionally empty mapping, and cannot add per-chunk cost. The *speed* cost of the knob is acknowledged unmeasured by the promotion record (`journal/promotion-pool-nohuge-2026-11-24.md` §8a).
* (b) Knob: unset `STRATA_POOL_NOHUGEPAGE` in B.
* (c) Span: none expected; `intake_us` flat.
* (d) Prediction: no span moves; a B leg with the knob unset lands inside run-to-run noise.

### #5 — Helper claim path — weak
Both arms 270 prompt-tier requests and rows within 0.5 % (§1). The claim/send/wait structure in the walk differs in source, but the traffic is the same. Would show as `claim_us`/`wait_us`/`helper_us`; prediction: ≤1 % of `layer_us` in both.

### #6 — `--conversation-spill-mib 0` tier present in B — excluded by declaration and by the measurement shape
Declared "not a lever" (config docstring, `bench/ab-v0210/rpcprof/port_ab_configs.py:12-16`). The 144k leg is a cold read (0 reused tokens), and the rate is reported by the engine's own prompt timer. No source path was found that spends per-chunk time while the tier is off at 0 MiB.

## 6. Confounding — what A-vs-B cannot attribute

| Difference | Value | Confounds the claim? |
|---|---|---|
| helper fill | 3655 vs 3683 slots (+0.8 %) | mildly; not separable from engine without a helper-swap leg |
| cache slots / resident experts | 4951/9125 vs 5739/11149 (+25 % / +22 %) | **yes** — engine-side (dense trim), favours B |
| dense matrices resident | 301/2331.54 MiB vs 188/1474.17 MiB | **yes** — same cause as the row above |
| decode hit rate | 96.1 vs 97.4 % | outcome of the slot difference, not an independent cause |
| `STRATA_POOL_NOHUGEPAGE` | B only | no (pool empty this run) |

A 2×2 engine×server window (W4) does **not** separate #1/#2/#3: all three live inside the engine binary
pair, and the server contributes nothing to the engine-reported read rate (identical argv). The separating
experiments are (i) **knob legs on one binary** — B with `STRATA_EXPERT_I8=0`, and B `--no-trim-stage-weights`
/ A `--trim-stage-weights` — and (ii) **W3s spans** (`STRATA_ENGINE_SPANS=1`, both binaries emit the same
`rpcprofile:` families; `journal/span-leg-w3s-2026-11-24.md`), read as shares within each arm:
`expert_us`/`post_us` → #1, `intake_us`/`claim_us` → #2, `barrier_us`/`group_us` → #3.

## 7. What I could not verify

* No kernel was timed; every claim about which path is *faster* is inference, not measurement. The sign of
  candidate #1 is genuinely unknown from source.
* The per-layer/per-span decomposition cannot be checked because no Phase-2 log carries spans
  (`journal/span-leg-w3s-2026-11-24.md`: all W1/W2/W3 engine logs hold zero `rpcprofile:` lines).
* A's exact per-expert FP16 kernel (which dequant/GEMM route `run_impl`'s FP16 path takes for IQ3_XXS on
  gfx906: hipBLASLt vs `gemm_iq_f16_grouped`) was not followed to the kernel; only the path label is established
  (`prefill.cpp:1565`, `:7986`, boot log A:37 and the missing fused notice).
* Whether `STRATA_EXPERT_I8=0` in B reproduces A's kernel class *exactly* is unverified: B's FP16 path is the
  same source family (`moe_f16_kq.cu` diff is +15/−10) but the surrounding walk differs by 2047 lines.
* The server trees were not read; the claim that the server is not a lever rests on the identical engine argv
  and on the rate being engine-reported.
* The helper change (`9d16742d` → `58c3ae02`) was not diffed (different binary provenance); its effect is
  bounded only by the near-identical fill/row counts.
* Nothing here was measured: the 320.65 / 305.60 tok/s figures are the parent's, n=2 per arm, descriptive only.
