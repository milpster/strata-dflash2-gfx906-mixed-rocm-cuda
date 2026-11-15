# Code-level optimization analysis — Strata inference engine (branch hc-q8-gfx906)
- Date: 2026-11-15. READ-ONLY analysis of /mnt/srcds/Strata-wt-integ. Scope: code-only changes; excludes env/flags/tuning params and HC_Q8 paths.
- Hardware context: 2x gfx906 layer-split@30 + RTX3080 helper; UD IQ3_XXS 48L, K-quant experts, int8 KV, spec5 dflash b7; warm TG 26-34 t/s bandwidth-bound; full-fill PP bimodal 432-455/352-379.

## Context established (2026-11-15)
- Engine = src/program/generate.cpp serve loop (25.5k lines) + src/core/verify.cpp `Verifier` (5.5k lines).
- Decode window = CUDA-graph replay (`Verifier::run` verify.cpp:3314; graph captured in `record_window` verify.cpp:1344; all-resident variant `ar_on()` = zero doorbells, no CPU experts mid-graph).
- Layer split@30: stage chain overlap already implemented (`chain_overlap` verify.cpp:3281, prelaunch thread verify.cpp:3495-3498; next stage graph waits on GPU hand-off flag, no host sync between stages).
- Draft = DFlash2 on RTX3080 helper; serial loop: verify -> host acceptance (`generate.cpp:22645` `while (a < T-1 && window[a+1]==outv[a])`) -> `draft_df2` (generate.cpp:22288) -> next window. `--pipeline-windows` pipelined loop (generate.cpp:21608+) already overlaps stage0 window K+1 with stage1 verify K.
- Per-window host work between GPU windows: PLE n-gram NVMe gather (verify.cpp:3396/3611), penalty hist stage H2D (generate.cpp:22590-22594), adapt_tick, taps gather.

### F1 (minor, sampled requests only): out-of-graph head sampler double-sync
- verify.cpp:3661 `cudaStreamSynchronize(cs_)` (graph done) then verify.cpp:3721 `sample_tokens(...)` kernel launch then verify.cpp:3722 second `cudaStreamSynchronize`. Greedy default has in-graph argmax (verify.cpp:2658-2664) so this only costs sampled/penalized requests. Fix: sampler kernel reading params from mapped pinned (pattern already exists: `coupled_stage_kernel` sampler.cu:807/1340 reads mapped_params). Impact ~0.1-0.5 ms/window on sampled turns only. Effort S. Risk low. Bit-exact (same Philox(seed,pos0+t)).

## Ground truth established from prod log + code (reframes "bandwidth-bound")
- Engine log `/mnt/srcds/Strata/logs/engine-prod-ud-iq3xxs-v0233.log`: `513 generated in 21532 ms (23.8 tok/s), drafts accepted 338 of 479` => 175 verify rounds in 21.5 s => **~123 ms per verify window** (T~6 rows: 1+5..7 drafts). Same class as journal (ms/verify 61-161).
- helper=reqs=6567 / 175 rounds = **37 helper claim frames per window** (~1 per layer), rows=39018 => 223 rows/window, reply bytes ~2.28 MB/window (fp32 output rows over socket).
- Geometry (include/strata/core/geometry.hpp:24-58): N=2560, 48 layers (36 GDN + 12 QSA @ interval 4), MoE every layer, 512 experts top-10, n_ff=640, hc=4, KV int8 2 heads x 256.
- Expert residency: card0 4983 + card1 3985 GPU slots + helper ~3700 => `all_resident_` is FALSE (verify.cpp:1271-1285 needs 100%) => **prod runs the DOORBELL graph**: per layer, GPU stops at a mapped-flag doorbell, host serves misses (verify.cpp:3518-3620), plan/flagA/flagB round trips. Per-layer stage names (verify.cpp:2676-2680): waitA / VRAM hits / waitB / PCIe grp / waitCPU / copy+combine.
- Bandwidth math: dense (~1 GiB/win) + experts (~5-10 GiB/win incl. PCIe+VRAM) + KV/indexer (~0.4 GiB/win) => ~7-11 GiB per 123 ms => **~60-90 GB/s aggregate achieved = ~5% of the 2x ~838 GB/s peak**. The window is NOT raw-bandwidth-bound; it is bound by the serial per-layer chain: [GPU attn+router] -> [doorbell] -> [host: classify+publish+helper frame (CPU quantize!) + act quantize + CPU rows] -> [flagA] -> [GPU VRAM hits || PCIe streams || helper RPC] -> [flagB] -> [combine], 48x per window, strictly ordered by data deps.
- Consequence: the highest-value code changes cut LATENCY and HOST WORK out of that chain, not bytes.

---

## F2 (MAJOR, warm TG +3-4%): helper claim frames CPU-requantize what the GPU already quantized
- Mechanism: every doorbell-served layer with helper claims runs `quantize_q8_1_cpu(x, n_tok*2560, q8)` on the HOST inside the claim path (src/core/remote_experts.cpp:744-751; the W1 comment at :719-724 prices it at "~72-144 us/frame"). Meanwhile the captured graph ALREADY produced a byte-identical q8_1 image of the same activations on the GPU: `quantize_q8_1_rows(xm -> nat_xq_)` (src/core/verify.cpp:2352-2354; verify.cpp:2320-2322 states quantize_q8_1_rows and native_quantize_q8_1 "write the same bytes", and remote_experts.cpp:719-724 says the helper's GPU quantizer is "bit-identical to the CPU quantizer").
- Measured scale: 37 frames/window x 72-144 us = **2.7-5.3 ms of pure host quantize per 123 ms window** (2-4% TG), serialized inside layer service.
- Change: (1) in record_window, move the xm->nat_xq_ quantize BEFORE the doorbell publish (both on stream cs; depends only on xm — reorder is legal and value-identical); (2) extend the doorbell publish kernel family (src/kernels/cuda/elementwise.cu:437-458, doorbell_publish_kernel <<<1,1024>>>) with a variant that also copies nat_xq_ (T x 80 x 36 B = 17.3 KB at T=6) into a new mapped ring m_xq_; (3) in proxy_begin, when the ring holds this layer's image (format q8_1), pass the ring bytes to helper_->begin(...) instead of quantize_q8_1_cpu. Keep the CPU path as fallback for layers whose act format is not q8_1.
- Quality: bit-exact by construction — the same 36-byte blocks the helper's grouped kernel reads today (engine's own parity claims cited above); gate plan: teacher-forced KL 0.00000 + `moe_hit_parity`/helper row compare, one boot.
- Impact: warm TG +2-4% (removes 2.7-5.3 ms/window host-serial time); also removes the per-frame `q8.assign` zero-fill alloc churn (remote_experts.cpp:750). No effect on PP (prompt claims use the prompt path).
- Effort: M (new mapped ring + publish-kernel variant + claim-path plumb). Risk: low-medium (ring sizing/wraparound; fallback keeps semantics on any miss).
- Note: strictly better than the landed-but-off W1 (`STRATA_HELPER_F32_WINDOW`): ships 1.125 B/elem pre-quantized instead of 4 B/elem raw + helper re-quant; needs no helper feature bit.

## F3 (MEDIUM, warm TG +0.5-2%): CPU-pool rows re-quantize per token per layer on the same critical path
- Mechanism: expert_pool_dispatch_multi (src/core/expert_source.cpp:5316-5327) calls `native_quant_act`/`act_quant_q8_1` per token for every layer that has CPU rows — another host quantize of the SAME activations already image-quantized on the GPU (nat_xq_), executed after the helper begin but before the CPU rows compute (serial).
- Change: for layers whose act format is q8_1, consume the same m_xq_ ring from F2 (d.act_multi points into a copy taken from the ring at doorbell read). Other formats keep the CPU quantizer.
- Quality: bit-exact — same bytes (the QDEDUP comment's parity claim covers the q8_1 equivalence; per-layer format check is the gate).
- Impact: T x ~10-30 us x layers-with-CPU-rows (~1-3 rows/layer typical warm; more on cold) => ~0.5-2 ms/window. Effort: S once F2's ring exists. Risk: low.

## F4 (MAJOR, warm TG +5-15% if predictor hits ~70%): speculative helper/PCIe prefetch of the NEXT layer's misses
- Mechanism: the serial chain means the helper RPC (send after doorbell of layer l, join before combine of layer l) and the PCIe blob streams (P.fetch, expert_source.cpp:5251) sit on every layer's critical path. But routing is highly autocorrelated across windows (the adaptive tier's decayed usage `drive.d.usage` and the previous window's same-layer ids are strong predictors — same reason placement trajectories recur, journal 18al).
- Change: at layer l's doorbell service, ALSO (a) send the helper a prefetch frame for layer l+1 with predicted ids (from usage + last-window ids); the helper computes candidate rows into a scratch ring keyed by (layer, ids-hash); at layer l+1's doorbell, claims that match the prediction are consumed from the ring (zero RTT), the rest take today's path; (b) same for PCIe: pre-stage predicted non-resident blobs into the staging ring on the copy engine (idle while the host serves), validated at plan time.
- Quality: bit-exact — only rows whose (layer, token, expert, weights) match the ACTUAL routing are consumed; a wrong prediction is dropped and the demand path computes it. No change to what is combined. Gate: teacher-forced KL 0.00000 + helper row-bytes compare.
- Impact: removes (RTT + helper compute + PCIe stream time) x hit-rate from ~37 helper frames and ~5 PCIe streams per layer per window. At 0.3-1 ms/frame hidden and ~70% hit: **8-25 ms of the 123 ms window => +7-20% TG upper bound; claim +5-15% conservative**.
- Effort: L (helper protocol extension + validation + fallback + staging-ring lifetime). Risk: M (protocol states; must degrade to today's path cleanly; predictor thrash costs bandwidth not correctness).
- NOT quality-unsafe: predictions only prefetch, never substitute.

## F5 (MEDIUM, warm TG +0.5-3%): steady-state PLE gather gates the layer0->layer1 handoff
- Mechanism: early_ple_flag_mode defaults to 2 on HIP (src/core/verify.cpp:381-390): the pre-launch gather+flag applies only until that graph variant's first launch; steady windows gather at k==0 (verify.cpp:3609-3615) BETWEEN publishing layer-0's plan (flagA) and raising *flag (line 3616) — so the NVMe/page-cache gather (~260 KB random reads; direct-IO class measured 54 MB/s, journal 18as note 4) delays layer 1's PLE-dependent pre(). prefetch_rows during the draft (generate.cpp:22294-22308) warms some of it, but the gather itself is a blocking host call inside the layer loop.
- Change: start the gather on a small side thread at stage_inputs time (window tokens known pre-launch); the thread writes h_ple_ (pinned) and raises m_flag_ itself (the graph already waits on m_flag via wait_flag_ge, verify.cpp:1499 — a GPU spin, no host involvement needed). The serving thread never blocks on it.
- Quality: bit-exact — same bytes, same flag protocol, same consumer ordering (wait_flag_ge before layer 1's copy_from_mapped(ple_)).
- Impact: up to the gather time (~0.3-4 ms) minus what layer-0 GPU work already hides => +0.5-3% TG. Effort: S-M. Risk: low (#267-class care: thread must never outlive the window; join at window end like NextLaunch does).

## F6 (MEDIUM, boot/lane throughput): serial blocking per-slot H2D in the boot slot pre-fill
- Mechanism: the profile pre-fill loop (src/program/generate.cpp:9344-9368) admits each slot then calls `fill_slot_blocking` per slot — on HIP that is memcpy-to-staging + a BLOCKING cudaMemcpy per expert blob (src/core/expert_cache.cpp:807-837), one at a time, card0 then card1 then helper, single thread. Measured: "pre-filled 4983 slots in 14.8 s (671 MB/s)" vs "23.5 s (422 MB/s)" on identical work (engine log lines 9343/12943-class) — 9.6 GiB at copy speeds that a queued pipeline would beat several-fold.
- Change: (1) switch the loop to `fill_slot_queued` (+ one sync every N slots) — the queued API already exists for refills (expert_cache.cpp:839+); (2) fill the two cards' caches on parallel threads (each pinned to its device), helper fill overlapped where it is not already.
- Quality: bit-exact — same bytes into the same slots; the existing verify_slot read-back check (generate.cpp:9380-9385) still runs.
- Impact: boot slot-fill phase 2x~15-24 s -> likely ~4-8 s total; saves ~20-40 s of every 3-6 min boot. Direct lane-throughput value (every benchmark boot pays it). Effort: S-M. Risk: low (HIP staging buffer reuse must be double-buffered to stay queued — else it degenerates to blocking again).

## F7 (fill bimodality — mechanism hypothesis + determinism fix, PP +7-20% on slow-class boots if confirmed)
- Evidence: same-config boots land fast (432-455 PP) or slow (352-379); the SAME engine read pattern measures 671 vs 422 MB/s across boots (slot pre-fill, above); A1 mmap test replaced the bimodal distribution with a tight mid cluster (journal 18bc) — consistent with page-cache/NVMe warmth of the 25-40 GiB pack/arena reads being the class variable, PLE mmap itself EV-neutral.
- Code change (determinism, not tuning): (a) take the arena + slot-fill reads through the batched O_DIRECT path that already exists for swap-ins ("batched O_DIRECT pack swap-ins", journal :185) so boot I/O no longer depends on page-cache state; (b) sort the profile fill order by file offset (read blobs in offset order; identical bytes, bit-exact by construction) to convert ranked-random reads into near-sequential; (c) add per-phase fill-rate fields (arena read, slot fill, PCIe stream, CPU-pool rows) to the PP/summary line so the next slow-class boot self-attributes.
- Quality: bit-exact (read order only). Impact: IF the hypothesis holds, slow-class boots (352-379) move to the fast class => +14-20% on those boots; EV across boots ~ +7-10%. Even if not the mechanism, (c) is the missing instrument. Effort: M. Risk: low (O_DIRECT path exists; keep page-cache fallback under low RAM).
- Explicitly marked HYPOTHESIS (the operator falsified PLE-mmap-as-fix; this is the narrower page-cache-warmth mechanism with new evidence: the 671/422 MB/s same-work boots).

## F8 (minor, warm TG +0.1-0.2%): penalty-history staging blocks the round critical path
- Mechanism: `penalty_rows` + a BLOCKING cudaMemcpy H2D per window (src/program/generate.cpp:22585-22594) run after acceptance/draft and before ver.run; the window tokens are already known at draft time.
- Change: build the hist rows during the previous window's GPU run (host-side, cheap) and stage via pinned+async copy ordered before the graph launch, or through the mapped staging the graph itself copies (copy_i32_from_mapped pattern, verify.cpp:1427/1435).
- Quality: bit-exact (same bytes, earlier). Impact: ~0.05-0.2 ms/round. Effort: S. Risk: low.

## F9 (observation, not a proposal): draft cannot overlap next verify without speculation
- The DFlash drafter lives on the LAST stage's gfx906 (generate.cpp:8059-8066: `on_dflash(last_st->dev)`), runs between windows while card0 idles; the draft needs the accepted boundary (outv[a]) which exists only after the window's head + host acceptance — a strict dependency. The overlap answer is speculative windows = the `--pipeline-windows` decode loop (generate.cpp:21608+, PV[][] parity verifiers, theta gate) — already implemented, config-gated (operator's territory). Moving the drafter to the 3080 (`--dflash-remote-only`) is likewise config. No bit-exact code-only overlap exists for the serial loop beyond F2-F5,F8 shrinking the between-window host gap. The between-window gap also contains: keep_sync() join, suffix propose, PLE prefetch — all small once F5 lands.

## F10 (cross-cutting sweep results — negative findings, recorded to prevent re-digging)
- Dense-weight FP16 re-dequant per chunk (HIP compiles out the w16 cache, gemm.cu:1408 `#ifndef STRATA_USE_HIP`; dequant_f16 in front of each GEMM, gemm.cu:1497): at 16k-token chunks this amortizes to ~6 GB per ~36 s chunk (~170 MB/s, <1% of chunk) — NOT a fill bottleneck at chunk=16k. The w16 cache is also VRAM-impossible on this box (expert cache owns all headroom). The side-stream dequant overlap (`STRATA_DEQUANT_SIDE`, gemm.cu:638-644) is hardcoded OFF on HIP — irrelevant per the math above.
- Expert kernels are already well-fused: native_gu_swiglu_kernel does gate+up+SiLU+q8_1-quantize in one launch with a shared-memory iq-grid and grouping over same-expert entries (iq_kernels.cu:2882-2948); s2 path has the pair variants. Per layer the expert phase is 2 kernels + plan + combine. No fp64 remnants found in hot kernels; accumulation fp32 as expected.
- KV/indexer reads at 80k are small (top-2048-cell select + pooled indexer => ~0.4 GiB/window) — not the wall; consistent with the 240k stress test degrading only mildly (journal 18ai).
- PLE prompt gather is already double-buffered on an async thread per chunk (prefill.cpp:3867-3873, 3918) — fill-side PLE is NOT on the chunk critical path (decode-side is F5).
- rocBLAS solution tables are read-only files keyed by shape (gemm.cu:452-535) — deterministic per config; NOT a bimodality source.
- Checkpoint stalls at 16,384 multiples and boundary re-reads: known/covered elsewhere (B4), skipped per scope.

### F4 refinement (from expert_helper_socket.cpp:589-626)
- `begin()` writes the frame and returns (the reply is read by `finish()` at join time; "ONE outstanding request" per layer, comment at :607-611). So the RTT is already partly hidden behind the layer's CPU/VRAM work; F4's prefetch value = the RESIDUAL `finish` wait (helper compute + reply exceeding the layer's parallel work), plus removing the begin-side quantize via F2. The stripe channel-B infrastructure (helper2_/begin_stripe, remote_experts.cpp:757-796) is protocol precedent for a second outstanding lane — the prefetch frame can follow that pattern.

---

# Ranked summary (warm TG = WT, full-fill PP = PP, boot/lane = BT)

| # | Finding | Where (file:line) | WT | PP | BT | Quality | Effort | Risk |
|---|---|---|---|---|---|---|---|---|
| F4 | Speculative helper/PCIe prefetch of next layer's misses (route-heat predictor, validated at plan) | expert_source.cpp:5251+, remote_experts.cpp:619+, verify.cpp:3577-3581 | +5-15% | - | - | bit-exact (validated consume-only) | L | M |
| F2 | Ship the GPU's q8_1 activation image to the helper (mapped ring) instead of per-layer CPU requantize | remote_experts.cpp:744-751, verify.cpp:2352-2354, elementwise.cu:446 | +2-4% | - | - | bit-exact (same bytes, engine parity claims) | M | low-M |
| F7 | Fill bimodality = page-cache/NVMe warmth hypothesis; O_DIRECT + offset-sorted arena reads + fill-phase instrumentation | generate.cpp:9344-9368, expert_cache.cpp:807 | - | +7-20% (slow boots) | + | bit-exact (read order) | M | low |
| F6 | Boot slot pre-fill: queued+parallel slot fills instead of serial blocking per-slot copies | generate.cpp:9344-9368, expert_cache.cpp:807-837 | - | - | ~20-40 s/boot | bit-exact | S-M | low |
| F5 | PLE gather off the layer0->layer1 critical path (side thread raises m_flag_) | verify.cpp:3609-3616, 381-390 | +0.5-3% | - | - | bit-exact | S-M | low |
| F3 | CPU-pool act quantize reuses the F2 ring (q8_1 layers) | expert_source.cpp:5316-5327 | +0.5-2% | - | - | bit-exact (per-layer format gate) | S (post-F2) | low |
| F8 | Penalty-hist staging moved off the round path (async/mapped) | generate.cpp:22585-22594 | +0.1-0.2% | - | - | bit-exact | S | low |
| F1 | In-graph sampler via mapped params (sampled requests only) | verify.cpp:3661-3722, sampler.cu:1340 pattern | ~+1% (sampled turns) | - | - | bit-exact (same Philox draws) | S | low |

## Top-3
1. **F4 speculative prefetch** — the only finding with a >10% TG ceiling: it attacks the measured ~123 ms window's dominant structure (per-layer serial host service + helper finish-waits) while staying bit-exact by construction. Land F2 first (it shrinks the same chain and shares the ring infrastructure).
2. **F2 (+F3) activation-image ring** — highest certainty-per-effort: removes a measured 2.7-5.3 ms/window host tax that the code's own comments price, with the engine's own parity artifacts as the bit-exactness evidence.
3. **F7 bimodality determinism + instrumentation** — the fill fast/slow class is the largest single PP swing on the board (+14-20% between classes); even if the page-cache hypothesis fails, the per-phase instrumentation converts every future boot into an attribution experiment.

## NOT QUALITY-SAFE / rejected
- (none of the above) — all proposals are bit-exact by construction. Ideas rejected during the survey as not meeting the bar: fusing the MoE combine into the down-kernel via atomics (changes summation order — would need a KL gate and belongs to the HC_Q8-adjacent C1 family already parked); route-prior/resident re-ranking (changes routing output, env-gated experiment, documented -4%..+37% variance); any expert format change (bytes are the product).
