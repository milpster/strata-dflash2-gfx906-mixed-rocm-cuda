# Strata gfx906 serving-performance roadmap — 2026-11-15

Researcher: explorer agent (glm-5.3), session ses_e20b9def0ffe5rh03BT4hEjxWl, after campaign rounds 18am–18ax.
Constraint: **zero quality sacrifice** — bit-exact by construction or gate-provable (KL 0.00000 / identical PPL) only.
Citations spot-verified post-delivery: arch_defaults.cpp:63 (gfx906 empty table) ✓, layer.cpp:1595/1620 (ignored view failure) ✓,
gemm.cu:596-603 (prompt_f16 not bit-identical) ✓, generate.cpp:6136-6167 (PLE_AUTO_MMAP gate) ✓,
prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json:89 (`STRATA_HIP_PROMPT_F16: "1"` live in prod) ✓.

Hardware basis: 2× gfx906 Radeon VII (HIP/ROCm 6.1, split at 30) + RTX 3080 helper, 62 GiB RAM, NVMe.
Qwen3.8-Flash-Next UD IQ3_XXS, 48 layers, K-quant experts (MMQ 0), int8 KV, 262144 ctx, spec 5 dflash block 7.
Warm TG 26–34 tok/s (bandwidth-bound); full-fill PP 432–455 @80k. Slow classes: first-gen-after-fill 10–15 tok/s,
boundary re-reads 44–250 tok/s bimodal, fill walls 179–187 vs 211–227 s.

## A) Config-only quick wins (testable today, no rebuild)

| # | Item | Expected impact @80k | Quality risk | Effort | Files/knobs | Validation |
|---|------|----------------------|--------------|--------|-------------|------------|
| A1 | **`--ple-io mmap`** (or `STRATA_PLE_AUTO_MMAP=1`; boot probe measures ~13.5K rows/s ≈ 54 MB/s < 15K threshold, generate.cpp:6137-6165) | Removes the layer-1 direct-read tax from fills; prime suspect for fill-wall bimodality (179-187 vs 211-227 s) and the 44-250 tok/s boundary re-read mode. Est. **+3-10% PP**, fast-mode stabilization; warm TG ≈ 0 | **None: bit-exact** — same bytes both I/O modes (ple_fp8_parity.cpp:7-8, ple_reader_test.cpp:296-418) | S | args: `"--ple-io","mmap"`; `--ple-io ram` infeasible (50.7 GiB table + 46 GiB staging > 62 GiB RAM) | Back-to-back boots ×3: judge read rates + boundary re-read legs |
| A2 | **Sweep gfx1151/gfx12 decode switches on gfx906** (empty defaults table at arch_defaults.cpp:63). Env-gated, default OFF: `STRATA_Q8_PACKED=1` (+2.3% on gfx1151), `STRATA_TSUM=1`, `STRATA_SH_STREAM=1`, `STRATA_LFUSE=1`, `STRATA_PLE_BATCH=1`, `STRATA_MMVF_ROWS=1` | Each +0.5-2% if transfers; bundle target **+2-5% warm TG**. PP ≈ 0 | **None: bit-exact** — engine tables mark these bit-identical; still gate each | S per arm | env only; STRATA_ATTN_LANECELL/MERGE_V2/EXPDEC likely inert on gfx906 (wave32-only, verify_kernels.cu:832) — deprioritize | One arm per boot pair, interleaved; sha ×3 + teacher-forced KL 0.00000 |
| A3 | `--ple-row-cache` bump (default 1<<20 rows ≈ 170 MB host, generate.cpp:684) | Lifts the slow boundary-read mode further under mmap too | **None: bit-exact** | S | args | Same A/B shape as A1 |
| A4 | **Audit `STRATA_HIP_PROMPT_F16=1` — LIVE in prod** (config json:89). Rounds each prompt GEMM output to FP16, "not bit-identical" (gemm.cu:596-603). Never gate-measured; the 432-455 band includes it | If dropped, fills slow (unknown on Vega20). Keep if gate-accepted | **NOT bit-exact** — decision item; note all campaign baselines/gates were measured WITH it, so the "zero sacrifice" reference includes this rounding | S | env in prod config json | One teacher-forced pair on/off; report KL/PPL; operator decides |
| A5 | Refuted: `--kv-grow` (compile-time impossible, vmm.cpp:157-158), `STRATA_DENSE_MMQ` (K=10240>8192, 320%256≠0; dense MMQ lost 7-12% in 18ad), `--adapt-every`/FS (exhausted 18ag/18ah), dflash adaptive | — | — | — | — | — |

## B) Small code fixes

| # | Item | Expected impact @80k | Quality risk | Effort | Files/functions | Validation |
|---|------|----------------------|--------------|--------|-----------------|------------|
| B1 | **Root-cause the hc-swap harness wedge, land 84fc595dd9** (+210 slots ≈ +4.2% cache → est. **+1-3% warm TG**). Static audit: (a) layer.cpp:1595/1620 ignore `hc_bf16_view_pair` failure — nullptr w_down reaches `fused_gr_read` → GPU fault → #267-class hang; (b) `hc_bf16_view` picks scratch by `cudaGetDevice()` at call time (native_dense.cpp:582-584) — cross-device buffer+stream pair possible; (c) cls=1 decode takes no event wait/release (588 gates cls 0) | TG **+1-3%** (slot math proven: 5193 vs 4983) | **None: bit-exact** — swap changes storage, not math (Q8 = GGUF's own bytes; dequant kernel proven bit-exact 18aq) | M | native_dense.cpp:577-647, layer.cpp:1595/1620, generate.cpp:5660-5674 | 4× teacher-forced harness (the 4/4-fail gate) + 3× on fix; STRATA_VERIFY_TRACE repro |
| B2 | **#267 verify-timeout retry**: one bounded window re-capture/re-launch after successful release (40 s timeout verify.cpp:587, 3545-3560 currently errors out) | Availability: avoids engine death + 3-5 min re-read per ~1/20 event | **None: bit-exact** — same window recomputed from same inputs | S-M | verify.cpp:3504-3560, release 610+ | STRATA_TEST_VERIFY_STALL hook (589-594): force stall, assert clean retry + identical continuation |
| B3 | **Fail loudly under the 512 MiB floor**: L2 probe only warns (generate.cpp:9638-9679); at 128 MiB reserve the engine silently NaNs (18at) | Enabler: safe reserve tuning, no NaN churn | **None** (control flow) | S | generate.cpp:9663-9679, serve own_fits | Boot at artificially low reserve: clean refusal, no NaN |
| B4 | **Checkpoint save off the read path**: per-stage `cudaDeviceSynchronize` + host copy at every 16,384 boundary (generate.cpp:15295-15318); killer-class stalls at exact 16,384 multiples under RAM pressure | Removes boundary stall mode (32,768/180,224 freezes); worst-case fill walls improve | **None: bit-exact** if same state captured (async copy-stream + event, or defer to idle gap) | M | generate.cpp:15295-15480 | STRATA_TRACE checkpoint timings; restore-equivalence test |
| B5 | **Smarter PLE I/O default** (code complement to A1): NVMe + slow probe → default mmap on (SATA keeps off); reuse `on_rotational_disk` (6174); optionally 256-row probe batches (now 64, 3509) | Same class as A1 for every future boot | **None: bit-exact** | S | generate.cpp:6141-6167 | Probe decision matrix unit test; A/B boot parity |
| B6 | **gfx906 queue-priming experiment**: R9700 bimodal fix (generate.cpp:3527-3539) compile-time excluded from gfx906; our fill bimodality smells like the same queue-placement lottery | If it removes the slow class: **+10-15% on slow-class boots** | **None: bit-exact** (scheduling) | S-M | generate.cpp:3527 `#if` guard; `STRATA_HIP_QUEUE_PRIME` | ≥8 interleaved boots on/off; fill-wall distribution |

## C) Kernel projects

| # | Item | Expected impact @80k | Quality risk | Effort | Files/functions | Validation |
|---|------|----------------------|--------------|--------|-----------------|------------|
| C1 | **HC_Q8 prefill T-template fusion**: extend `launch_q8` beyond T=8 (fused_gr.cu:2611-2620), wire prefill hc sites (prefill.cpp:4433, 8332). `gr_up_q8_kernel<T>` extends to T≈32; `gr_down_q8_kernel<T>`'s `xs[T][640]` caps T≈16 on 64 KB LDS — down kernel needs a retile | PP **+2-3%** (dequant slice ~3.5% of fill, 18aq) — only pays once HC_Q8-on serves (needs B1). TG ≈ 0 | **NOT zero**: different summation order + different weights; gate class = HC_Q8 enable gate (KL ≈ 0.019). Flagged below the strict bar unless operator re-baselines HC_Q8-on | L | fused_gr.cu:1992-2621, prefill.cpp:4428-4439/8329-8354, native_dense.cpp:577-627 | Teacher-forced vs HC_Q8-on baseline: KL 0.00000 within that baseline; sha equality NOT expected — document |
| C2 | **Bit-exact GPU expert contract** (kills temp-0 placement nondeterminism → sha-gating everywhere): pin CPU kernel set (AVX-512 vs AVX-2 vs ggml round differently, native_expert.cpp:397 #2215; lone-token vs group #152, 351-368), extend helper-process cpu_order coverage (only set at generate.cpp:10143), placement-pin mode (#463, generate.cpp:22434-22436) | Perf ≈ neutral (enabler); may cost −1..-3% CPU-expert decode unless GPU order matches fast CPU tree | **None: bit-exact by construction** | L | s2_expert_grouped.cu:432-479, expert_source.cpp:5646-5673, native_expert.cpp:351-397, helper env plumbing | Cold-boot ×5 + warm ×5: all shas identical at temp 0; KL 0.00000 vs pinned-CPU oracle |
| C3 | **gfx906 LDS/wave64 audit** of decode hot path (GCN5: no packed fp32, wave64): inert A2 switches mark wave32-only kernels worth wave64 variants | Unknown; bounded by A2 findings | Per-kernel **bit-exact required** | M-L per kernel | verify_kernels.cu:832/1010-1709, iq_kernels.cu gfx906 sections, router_top10.cu:190-461 | Kernel parity harnesses + greedy sha |

## Top-3 recommendations

1. **A1+B5 (PLE → mmap on NVMe)** — the only lead attacking both worst measured classes (boundary re-reads 44-250 tok/s; fill-wall bimodality). Bit-exact by the engine's own parity contracts, testable today with two config lines. Est. +3-10% PP, zero TG risk.
2. **B1 (hc-swap wedge root-cause → land 84fc595dd9)** — +210 slots (+4.2% cache), largest bit-exact capacity win; three concrete audit points give a wedge-taming fix; B2 retry is the safety net converting residual #267 from engine death to a seconds-scale stall.
3. **A2 (gfx906 decode-switch sweep)** — gfx906 ships an empty defaults table while siblings run measured bit-identical switches worth +2-5% TG. Cheapest TG lever; every arm env-only, sha + KL-0 gated.

Deprioritized/refuted: C1 (KL-0.019 class — only if HC_Q8-on becomes the accepted baseline), `--kv-grow` (impossible), STRATA_DENSE_MMQ (ineligible), reserve/FS/adaptive cuts (falsified 18ag-18av), KV-to-CPU and fp16-KV (falsified).
