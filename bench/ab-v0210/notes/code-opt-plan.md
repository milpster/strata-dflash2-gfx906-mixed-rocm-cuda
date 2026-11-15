# Implementation plan — code-level optimizations F1–F8 (branch hc-q8-gfx906)
- Date: 2026-11-15. Deliverable plan for the F-items in `bench/ab-v0210/notes/code-opt-analysis.md` (input, verified findings).
- Repo under change: `/mnt/srcds/Strata-wt-integ` (branch `hc-q8-gfx906`, HEAD `84fc595dd9`; serving pin is `36ecbe98` — never regress it). F9/F10 skipped (observation / negative findings).
- Context read: journal rounds 18am–18bd (`serving-tg-reeval-split-retest-2026-11-11.md`), `perf-roadmap-2026-11-15.md`.
- Every item lands **default-OFF behind an env gate** (repo convention: `STRATA_HELPER_WIN_FAST` read at `remote_experts.cpp:746/749`, `helper_win_fast_enabled()`). OFF must be byte-identical: no new kernel in the captured graph, no new frame on the wire, no changed host order.
- Hard constraints (from operator): zero quality sacrifice (KL 0.00000 / byte-identical gates only); oracle/review convention before pin promotion; gate-default flips (promotion) are operator decisions staged as explicit steps; HC_Q8 paths are dead — no edits beyond what dead code forces; pin builds only from the worktree via `tools/mixed_gpu/build_pin.sh` (guards: gfx906 HIP, MMQ state line, no nvcc, libamdhip64, .hip_fatbin).

## Anchor verification status (re-walked in the worktree at HEAD)
All analysis anchors re-verified true at HEAD, with these refinements found by reading the surrounding code (they shape the plan; none invalidate a finding):

| Anchor | Status | Refinement found |
|---|---|---|
| remote_experts.cpp:744-751 (per-frame CPU requant) | ✓ | `build_window_payload` (expert_helper_socket.cpp:574-587) **memcpys the image into the frame** → F2 needs **no wire/protocol change**: identical kBegin bytes whether the image came from CPU quantize or the GPU ring |
| verify.cpp:2352-2354 / 2320-2324 (nat_xq_ image) | ✓ | The image is written at THREE points: qdedup pre-quant at :2324 (after publish, before shared expert), folded-rewrite at :2352, plain at :2354 — all AFTER the doorbell publish (:2286-2298). A "move quantize before publish" reorder is only value-safe on **unfolded** layers (folded+qdedup passes the UNROTATED image to `shared_expert_multi` at :2332; see F2 hazard note). Helper claims never occur on folded layers (`expert_helper_candidates.cpp:80` skips `had_folded`) — so the ring is only needed there and the hazard is avoidable by construction |
| elementwise.cu:437-458 (doorbell publish family) | ✓ | `doorbell_publish_value_kernel` (:424-436) is the exact copy+fence+seq-store pattern to clone for the xq ring publish |
| verify.cpp:3518-3619 (doorbell service loop) | ✓ | pool() call at :3578-3580, flagA/flagB at :3595-3604, k==0 PLE gather at :3609-3615, `*flag=want` at :3616 — the F2 wait slots in before pool(), the F5 gather removal at k==0, the F4 prefetch hook after the classify |
| verify.cpp:3661/3721-3722 (double sync) | ✓ | In-graph greedy argmax at :2658-2664; host comment :3678-3681 explains why sampling is host-side ("a captured kernel would replay the same draws forever") — F1's mapped-params answer is exactly `coupled_stage_kernel` (sampler.cu:807) |
| verify.cpp:381-390, 1499-1500, 3504-3516 | ✓ | `early_ple_flag_mode` default 2 on HIP; layer-1 `wait_flag_ge(m_flag_,1)` + `copy_from_mapped(ple_)`; first-launch pre-raise block — F5 replaces both raise sites with the side thread |
| expert_source.cpp:5251, 5316-5327 | ✓ | `P.fetch` copy-engine callback; per-token `native_quant_act`/`act_quant_q8_1` loop — F3 replaces it for q8_1-format layers, F4b shares `P.staging` (:5232-5233) |
| expert_cache.cpp:807-855 | ✓ | `fill_slot_blocking` (HIP: single-expert staging + blocking cudaMemcpy), `fill_slot_queued` (pageable async — degenerates on HIP), `fill_slot_queued_staged` (:860-876). The batching pattern to copy is `NibbleCache` (generate.cpp:3232-3289: kBatch=64, one stream, ordered staging reuse) |
| generate.cpp:9344-9368 / 9528-9541 / 9380-9385 | ✓ | TWO serial blocking loops (CUDA0 + per-stage), verify_slot read-back checks after each; fill-rate line at :9405 is the F7c instrumentation target |
| generate.cpp:22585-22594 | ✓ | `penalty_rows` + blocking `cudaMemcpy` H2D per window, between draft and `ver.run` (:22624) |
| generate.cpp:3527-3539 | ✓ | B6 queue-priming `#if defined(STRATA_USE_HIP) && !defined(STRATA_HIP_GFX906)` — excluded from gfx906 (F7c interaction, below) |
| expert_helper_protocol.hpp | ✓ | kProtoVersion=2, feature bits :83-96, frame types :101-169, extension rule: new frame type + feature bit, old peer = typed refusal, never sent unadvertised (kBeginQ8K gate at expert_helper_socket.cpp:631-633 is the client-side pattern). ONE-outstanding-request rule per socket (expert_helper_socket.cpp:607-611) constrains F4's prefetch lane |
| tools: quant_quality.py, ab9_driver.py, lane scripts | ✓ | `tools/quant_quality.py` (teacher-forced KL; qqNIGHT.json drives a real serve engine); `bench/ab-v0210/rpcprof/ab9_driver.py`; lane toolkit `gpu-rounds/lane_boot.sh` (stop-all → setsid --fork boot → fresh ready line + /v1/models probe), `lane_judges.sh` (fill80k + tg80k, ctx 442400), `lane_restore.sh`, `make_variant.sh` (one-line-diff variant configs+launchers) |
| tests | ✓ | `tests/core/expert_helper_f32_window_test.cpp` (host-only socketpair fake-helper + negotiation + default-off equivalence + golden GPU-quant compare harness) is the exact pattern for F2/F4 engine tests; `tests/core/expert_helper_socket_test.cpp` (protocol), `tests/core/expert_helper_arena_cap_test.cpp`, `src/core/arch_defaults_test.cpp`, `src/kernels/gr_parity.cpp`; `tests/hip/ple_iq4.cpp` (PLE parity); CMake `STRATA_BUILD_TESTS` (CMakeLists.txt:219-222) |

## Master table

| Item | Phase | Commits | Effort | Validation cost (boots / GPU-hours) | Expected gain |
|---|---|---|---|---|---|
| F6 boot fill queued+parallel | P0 | 3 | S-M | 1 boot, ~0.5 h (no KL needed — verify_slot is the byte gate; 1 qq run at promotion) | BT: −15…−35 s/boot; TG/PP 0 |
| F8 penalty-hist staging | P0 | 1-2 | S | shares the P0 gate boot (1 qq KL boot) | WT +0.1-0.2% |
| F1 in-graph sampler (mapped params) | P0 | 3 | S-M | +1 KL boot (sampled config), ~1 h | WT ~+1% on sampled turns; 0 greedy |
| F7c fill-phase instrumentation | P0 | 1 | S | 0 dedicated (rides every later boot) | none direct; enables F7ab, B6, attribution |
| F2 helper xq ring | P1 | 4 | M | 2 boots (qq gate + judges) + 3× harness, ~2 h | WT +2-4% |
| F3 CPU-pool act from ring | P1 | 1-2 | S (post-F2) | +1 qq gate boot, ~1 h | WT +0.5-2% |
| F5 PLE async gather | P2 | 2 | S-M | +1 gate boot + judges, ~1.5 h | WT +0.5-3% |
| F7ab O_DIRECT + offset-sorted boot reads | P2 | 2-3 | M | 6-8 boots (class-distribution), ~4 h | PP +7-20% on slow-class boots (EV +7-10%); BT + |
| F4 speculative prefetch (helper + PCIe) | P3 | 5-6 | L | ≥3 boots + tuning, ~4-6 h | WT +5-15% (hit-rate dependent) |

Totals: ~20 commits, ~20-24 GPU-hours of validation across roughly 15-20 boots. Phase P0+P1 alone targets WT +2.5-6% and −20-40 s/boot with S/M effort and low risk.

## Phasing and dependencies (justified)

```
P0 (quick wins, parallelizable):  F6 ── F8 ── F1 ── F7c
P1 (ring):                        F2 ──> F3
P2 (independent, after F2/F3 to avoid verify.cpp run() merge conflicts):
                                   F5 ;  F7ab (generate/expert_source files, disjoint)
P3 (capstone):                    F4 (wants F2's ring + F7c's counters + F5's shorter chain)
```

- **F6+F8+F1+F7c first**: all independent, low risk, exercise the full lane discipline (variant launcher → boot gate → qq gate → judges → restore) on cheap items before the ring work; F7c early because every subsequent boot (including F2/F5/F4 validation and B6) then self-attributes its fill phases.
- **F2 before F3** (hard dependency: same ring) and **before F4** (soft: shares the doorbell-service hook, and F4's residual-wait math assumes the begin-side quantize is gone).
- **F5 after F2/F3**: it edits the same `Verifier::run()` service loop (verify.cpp:3504-3619); landing it after the ring avoids rebase churn on the hottest file. No functional dependency.
- **F7ab parallel to F5**: disjoint files (generate.cpp fill loops, expert_source read paths); its 6-8-boot measurement campaign can run while F5 is coded.
- **F4 last**: L effort, M risk; its value is bounded below by F2+F5 shrinking the same chain first (re-measure the residual `finish` wait after P1+P2 before committing to F4's full scope — F4's refinement note says the begin RTT is already partly hidden; if the post-P2 residual is small, F4's helper half shrinks to the PCIe half).

Hard dependency edges: F3→F2 (ring); F4a→(F2, protocol C1/C2); F4b→F7c (thrash counters); nothing else.

---
# Phase 0 — quick wins

## F6 — boot slot pre-fill: queued, batched, parallel (BT win)

**Exact change list**

1. `src/core/expert_cache.cpp` / `include/strata/core/expert_cache.hpp` — new batched pinned-staged queued fill, copying `NibbleCache`'s discipline (generate.cpp:3232-3289: one stream, kBatch blobs per staging buffer, ordered reuse without events):
   - `bool ExpertCache::fill_slots_batched(const BatchFill* items, int n, std::string& err)` where `struct BatchFill { int32_t slot; const uint8_t* blob; int64_t bytes; }`.
   - Internals: a per-cache pinned host staging ring `h_stage_` (2 × kBatchStage × `blob_` bytes; kBatchStage = 8 → 2 × 8 × ~1.38 MB ≈ **22 MiB pinned per cache**, ~44 MiB for two cards; allocated on first use, freed on `close()`), a per-cache non-blocking stream `fill_stream_`, one `cudaEvent_t` per staging half. Loop: memcpy up to 8 blobs into the free half (pageable→pinned, ~RAM speed), `cudaMemcpyAsync` each into its device slot on `fill_stream_`, record event, swap half, `cudaEventSynchronize` only when both halves busy. This is the double-buffering the analysis warns about (a single reused staging buffer degenerates to blocking on HIP).
   - `fills_` accounting unchanged per blob (the boot line's MB/s math keeps its meaning).
2. `src/program/generate.cpp:9344-9368` (CUDA0 loop) and `:9528-9541` (per-split-stage loops) — behind `STRATA_BOOT_FILL_QUEUED` (default OFF, static read once like `early_ple_flag_mode()` at verify.cpp:381-390):
   - Replace the per-pair `fill_slot_blocking` (:9359 / :9535) with: `admit()` as today (rank order, unchanged capacity semantics) → queue into the batch pipeline → `fill_slots_batched` flush every 8 pairs and at loop end.
   - Parallelism: run the CUDA0 loop and each stage loop on their own `std::thread` (each already enters its device via `DeviceScope`/`OnDevice`; `srcp->blob_stable` is a read-only file-backed mapping — thread-safe; the `read_batch`/`ahead` std::async prefetch at :9346-9348 stays inside the CUDA0 thread only). Join all threads before the two `verify_slot` checks (:9380-9385, :9547-9550) — those run unchanged, once, after the joins.
   - Helper fill overlap: the kFill frame already carries the ranked pairs; verify at implementation time whether it is sent before these loops (helper fills concurrently — nothing to do) or after (then move the send before the loops, still byte-identical — frame content unchanged). Note in the commit message which was true.
3. No protocol, no kernel, no graph changes. Bit-exactness: same blob bytes → same slots; `verify_slot`'s D2H compare is the byte-level proof on every boot (generate.cpp:9374-9383's own comment).

**Commit slicing** (each builds green, OFF = today's bytes):
- `feat(core): ExpertCache::fill_slots_batched — pinned double-buffered queued H2D (unused)` — new API + unit test, no callers.
- `feat(core): STRATA_BOOT_FILL_QUEUED — batched pipeline for the boot profile fills` — both loops switch behind the gate; still single-threaded.
- `perf(core): parallelize the per-card boot profile fills behind STRATA_BOOT_FILL_QUEUED` — the thread split, same gate.

**Validation ladder**
- (a) Builds: `tools/mixed_gpu/build_pin.sh f6-<n>` from the worktree (guards: gfx906 ON, no nvcc, libamdhip64, MMQ line). Host tests: `cmake -DSTRATA_BUILD_TESTS=ON` build; run `ctest -R expert_cache` (per_layer/segmented/slot_guard tests exist at tests/core/).
- (b) In-tree: new `tests/core/expert_cache_batched_test.cpp` — fill N slots through the batched API with random blobs, `verify_slot` each (the engine's own byte check), assert slot bytes identical to `fill_slot_blocking` results. Runs in the build-window ctest (needs a CUDA context; same class as the existing expert_cache tests).
- (c) Engine gate: variant launcher via `gpu-rounds/make_variant.sh` (+env `STRATA_BOOT_FILL_QUEUED=1`), `lane_boot.sh` (fresh ready line + /v1/models probe), then read the fill-rate line (generate.cpp:9405): expect ≥2× the 422-671 MB/s band (target ≥1.2-2 GB/s aggregate H2D). Judges `lane_judges.sh` once: fill80k/fresh-read walls unchanged ±noise (this item does not touch the read path), tg80k unchanged. One `quant_quality.py` qqNIGHT run with the env at promotion time (belt-and-braces; the item is byte-gated by verify_slot).
- (d) Rollback: `gpu-rounds/lane_restore.sh`; delete the variant launcher; the gate stays default-OFF in the tree; serving pin untouched (`36ecbe98`).

**Risks + mitigations**
- HIP pageable-async trap (the reason `fill_slot_blocking` stages at all, expert_cache.cpp:819-826): mitigated by pinned-only staging in the new API; never pass the mmap pointer to `cudaMemcpyAsync`.
- Staging-buffer reuse race: two-half ring + one event per half on ONE stream per cache — the NibbleCache-proven shape (generate.cpp:3229-3231 comment: "One stream, so the staging buffer's reuse is ordered without events" — we add the second half with one event, strictly weaker assumption).
- Thread-safety of `admit()` across caches: each thread touches only ITS cache (`xcache` vs `st.cache`) — no shared mutable state; assert at review that no global allocator path is shared without a lock (aux_cpus pin helper at :9348 stays in one thread).
- Boot RAM: +44 MiB pinned transiently — negligible against the 46 GiB fill-phase staging arena (18at).
- #267-class: no new GPU-visible flags, no spin kernels, no mapped rings — nothing to invalidate on stream rebuild; the fill streams are destroyed at `close()`.

**Definition of done**: 3 clean boots with the gate on showing the pre-fill phase ≥2× faster in the :9405 line (e.g. ~15-24 s → ≤8 s), `verify_slot` pass on every boot, judges unchanged, one clean qq run at promotion; journal round records the before/after fill rates.

## F8 — penalty-history staging off the round critical path

**Exact change list**

1. `src/program/generate.cpp:22585-22594` — today: `penalty_rows(...)` into the pageable `hist_stage` vector + a **blocking** `cudaMemcpy` H2D per window, after acceptance/draft, before `ver.run` (:22624).
2. Change (mapped staging the graph copies — the `copy_i32_from_mapped` pattern at verify.cpp:1427/1435/1462-1466):
   - `Verifier` gains a mapped buffer `m_hist_` (via `mapped()`, verify.cpp:514-528) sized `kMaxTok × kHistMax × sizeof(int32_t)` = 8 × 64 × 4 = **2 KiB** + one mapped control word `{len, valid}`; API `Verifier::stage_hist(const int32_t* rows, int T, int hist_n)` (host memcpy + fence + word write).
   - `record_window` head gains, behind the gate, a `copy_i32_from_mapped(hist_dev_, m_hist_, T*hist_n_word, cs)` guarded by the mapped valid word (a fixed upper bound is captured; the copy length word is read from mapped at replay — same technique as the sampler below; alternatively copy the full 2 KiB unconditionally and let `hist_n` gate reads — simpler, bit-trivial, preferred).
   - `generate.cpp`: build the rows **during the previous window's GPU run** (host-side, cheap — `consumed`/`window` exist right after acceptance) and call `stage_hist`; the blocking `cudaMemcpy` at :22593 is deleted on the gated path.
   - Bit-exactness: same `penalty_rows` bytes, same device destination, earlier copy — the analysis's argument (same bytes, earlier) preserved.
3. Gate `STRATA_PENALTY_MAPPED` (default OFF). OFF: today's vector + blocking copy, byte for byte.

**Commit slicing**
- `feat(core): Verifier::stage_hist — mapped penalty-history staging (unused)` — buffer + copy-in-graph behind gate, no caller.
- `perf(serve): build penalty hist during the previous window; stage via mapped ring` — generate.cpp switches behind the same gate.

**Validation ladder**
- (a) build_pin.sh + host tests build.
- (b) In-tree: no dedicated harness needed beyond a small case in `tests/core/` (host-only) asserting `stage_hist` writes the same bytes the vector path stages (golden compare, the f32_window-test pattern); PLE/iq parity harnesses untouched.
- (c) Engine gate: one qqNIGHT boot with `STRATA_PENALTY_MAPPED=1` — the qq harness runs penalized+sampled windows (teacher-forced logprobs), KL 0.00000 vs the F6-gate run (or the phase-0 baseline). Boot-gate discipline via lane_boot.sh; judges optional (expected inside noise on tg80k, ~0.05-0.2 ms/round).
- (d) Rollback: lane_restore.sh + drop the env from the variant; gate default OFF in tree.

**Risks + mitigations**
- Window-0 (no previous window): stage inline at acceptance time — still ahead of `ver.run`; worst case equals today.
- hist_n changes mid-request (penalty_last_n, consumed growth): the mapped len word is written before launch and read at replay; the captured fixed-cap copy (2 KiB) makes length changes replay-safe.
- Lifetime: `m_hist_` is Verifier-owned mapped memory alongside `m_tok_` (same destructor; no cross-window GPU reference beyond the in-graph copy — completes before the window's tail).
- #267-class: no thread, no flag — only a mapped buffer the graph head copies; nothing to invalidate on stream rebuild beyond the existing ring family.

**Definition of done**: qq run KL 0.00000 with the gate on; the round-path trace shows no per-window blocking H2D between draft and run (STRATA_TRACE or the serve summary's between-window ms); journal round.

## F1 — in-graph head sampler via mapped parameters (sampled/penalized requests)

**Exact change list**

1. `src/kernels/cuda/sampler.cu` — new mapped-input staging kernel + captured sampler stage, following `coupled_stage_kernel` (sampler.cu:807-815: copy `SamplerParams` + hist ring from mapped into device copies inside the graph) and `coupled_draft_stage` (:1338-1344 host launcher):
   - `__global__ void sample_stage_mapped_kernel(const volatile SamplerParams* mp, const volatile int32_t* mhist, const volatile int32_t* mspec, SamplerParams* dp, int32_t* dhist, int32_t* dspec, int32_t* dmode)` — copies `sizeof(SamplerParams)/4` words, `hist_cap` words, `spec_cap` words, and the mode word (`0=none, 1=spec, 2=proposal`, `nq`) from mapped into the device buffers the existing kernels read.
   - The **existing** `sample_tokens` / `sample_tokens_spec` / `sample_tokens_proposal` device kernels are then recorded in `record_window`'s tail (after `head_logits_` are complete, beside the greedy argmax at verify.cpp:2658-2664) as a mode-switching capture: record the three launches guarded by mapped mode (the mode word is read at replay, so ONE captured path is chosen per replay — capture the proposal variant reading `dmode` and branching host-side is NOT possible in-graph; instead capture all three tail launches, each wrapped in a device-side guard kernel is over-engineering — **preferred design**: capture ONE generic kernel `sample_tokens_mapped(head_logits_, T, n_vocab_, dp, dhist, dmode, dspec, m_out_, cs)` whose internals are the existing three kernels' bodies selected on the device-read mode; single code path, same math order, same Philox statement order). SamplerParams.counter is read from `dp` (mapped-staged with `counter = pos0` written per window) — the "replay the same draws forever" hazard (verify.cpp:3678-3681) is exactly what mapped params fix; the analysis's bit-exactness argument (same Philox(seed, pos0+t)) is preserved because the kernel reads `pos0` from the mapped counter at replay.
2. `src/core/verify.cpp`:
   - `record_window`: gated (`STRATA_SAMPLER_IN_GRAPH`, default OFF) capture of stage+sample at the tail; `m_sp_`/`m_hist_` (shared with F8 — take F8's `m_hist_` if landed, else its own), `m_spec_` sized `kVerifyMaxT × (1+kSpecQStride) × 4` B (~a few KiB) — byte math: kVerifyMaxT=8, kSpecQStride small (~12) → ≤ ~400 B; plus `m_mode_` word.
   - `run()` :3687-3726: when the gate is on AND the captured sampler is armed AND `sampling_.logit_bias == nullptr` (logit_bias stays host-side — fallback), skip the out-of-graph launch (:3721) and the second `cudaStreamSynchronize` (:3722) — the :3661 sync already covers the graph; write `sp`, mode, `nq`, and (with F8) hist rows into mapped at `stage_inputs` (verify.cpp:3146) / just before launch.
   - Fallback rule: any condition unmet → today's host path, byte for byte. Greedy default is untouched (already in-graph, :2658-2664).
3. Bit-exactness by construction: identical kernel arithmetic, identical inputs (logits identical — same graph; params/hist/spec identical bytes via mapped staging; counter=pos0 identical), so draws are identical. The analysis's caveat ("same Philox(seed,pos0+t)") is the acceptance criterion of the parity test below.

**Commit slicing**
- `feat(kernels): sample_stage_mapped_kernel + sample_tokens_mapped (unused)` — kernels only.
- `feat(core): STRATA_SAMPLER_IN_GRAPH — capture the head sampler from mapped params` — record_window/run() wiring, gate default OFF, fallback complete.
- `test(core): sampler mapped-vs-host parity (golden draws)` — the parity harness (or fold into commit 2; keep separate so the test lands visibly).

**Validation ladder**
- (a) build_pin.sh + host build (sampler tests compile host-side? kernels are .cu — the parity test is a build-window ctest).
- (b) NEW `tests/cuda/sampler_mapped_parity.cu` (beside the prefill parity tests): random logits + params + hist + spec rows; run host `sample_tokens` family vs the mapped captured path (graph capture + replay with fresh counter each replay); assert token-identical AND draw-identical (compare the Philox state via repeated calls) over ≥1000 windows, T∈{1,2,5,6,8}, mode∈{spec,proposal}, greedy excluded.
- (c) Engine gate: one qq boot with `STRATA_SAMPLER_IN_GRAPH=1` (qq drives sampled teacher-forced logprobs) — KL 0.00000. Judges leg optional (gain only on sampled turns; the operator's tg80k leg samples — expect ~+0.5-1% on that leg if measurable).
- (d) Rollback: lane_restore.sh; env off; gate default OFF in tree.

**Risks + mitigations**
- Mode/shape baking: T and n_vocab are capture-time constants (they already are — graphs are per-T); mode/nq/params are replay-time mapped reads. The one baked thing that must never change per replay: buffer sizes (fixed caps).
- `logit_bias`: stays on the fallback path (rare in prod; noted).
- Proposal-judging measure path (:3706-3718): keep host fallback for `measure` legs (the diagnostic path) — gate the in-graph path to the non-measure serve path only; zero risk to the harness.
- #267-class: all mapped buffers are Verifier-lifetime (like m_out_, which the code already syncs and reads at :3734); no threads, no new spin waits.
- HIP graph capture of the sampler: the greedy argmax is already captured (:2659) — capture of sampler kernels is proven; the mapped-read pattern is proven by `coupled_stage_kernel` (in-graph on the drafter).

**Definition of done**: parity test green in the build window; qq KL 0.00000 with gate on; a sampled tg leg shows the second sync gone (trace) and no regression; journal round.

## F7c — per-phase fill-rate instrumentation (the attribution instrument; F7ab's and B6's enabler)

**Exact change list** (always-on, log-only — its own commit, no gate: additive stderr fields only)

1. `src/program/generate.cpp:9405` — extend the "pre-filled N of M slots ... (X MB/s)" line with phase fields: `pack_read_s` (time in `blob_stable`/`read_batch`), `h2d_s` (time enqueueing+syncing fills), `pinned_mb`, `phases=arena:As lot:Hs`. Wrap the fill loops' I/O and H2D sections with `Clock` accumulators (existing `fill_t0` pattern :9333).
2. Per-stage loops (:9528-9541): same fields per `CUDA%d`.
3. Helper fill: time the kFill→kFillReport round trip (already reported by the helper tier; surface `helper_fill_s` + slots).
4. Runtime self-attribution (the analysis's "arena read, slot fill, PCIe stream, CPU-pool rows" list): the verifier already tracks `ms_wait/ms_pool/ms_host` (verify.cpp:3617-3619); add them (window-count-averaged) + helper begin/finish ms (`proxy_ms_begin_` exists; add `ms_finish_`) + PCIe fetch ms to the serve summary line printed at request end. Keep it to fields the operator already greps (`tokens = ` line class).
5. Zero behavior change: no gate needed (stderr format only; nothing parses these lines in-engine — verify with a grep for the exact format strings' consumers before landing; the lane scripts grep them, format stays backward-compatible by APPENDING fields).

**Commit slicing**
- `chore(serve): per-phase fill-rate + window-phase fields on the boot and serve summary lines` — one commit, no gate.

**Validation ladder**
- (a) build_pin.sh (format strings compile), host build.
- (b) No new test (log-only); the boot itself is the test.
- (c) One boot via lane_boot.sh: the new line shows the phases; run lane_judges.sh once to confirm the judge parsing (cut -c1-200 on `tokens = ` lines) still sees its fields.
- (d) Rollback: revert the single commit (no state).

**Risks**: none material (log format only). One check: `bench/ab-v0210` scripts that grep the pre-filled line must keep matching — append-only fields guarantee it.

**B6 interaction (operator's queue)**: B6's queued queue-priming experiment (generate.cpp:3527-3539, compile-time excluded from gfx906; would need the `#if` lifted + `STRATA_HIP_QUEUE_PRIME`) requires ≥8 interleaved boots judged by fill-wall distribution. With F7c, each boot's log line directly reports pack-read vs slot-fill rates — the fast/slow class assignment and any priming effect read off ONE line per boot instead of full judge legs, cutting B6's GPU cost roughly in half (boots only, judges only on the deciding pair). F7c is also the discriminator between F7's page-cache hypothesis and B6's queue-placement hypothesis: queue placement would show in H2D-phase rates with IDENTICAL pack-read rates; page-cache warmth shows in pack-read rates.

**Definition of done**: the fields appear on two consecutive boots and correctly attribute a known-slow phase (e.g. the 422 MB/s boot class); journal notes the schema.

---
# Phase 1 — the activation-image ring

## F2 — ship the GPU's q8_1 image to the helper via a mapped ring (WT +2-4%)

**Design note (refines the analysis's step 1, same bit-exactness class).** The analysis proposes moving the `xm→nat_xq_` quantize BEFORE the doorbell publish and extending the publish kernel to also copy `nat_xq_`. Reading the surrounding code shows the quantize has three issue points (verify.cpp:2324 qdedup-pre, :2352 folded-rewrite, :2354 plain) and ONE ordering hazard: for a **folded** layer with qdedup on, `shared_expert_multi` reads the UNROTATED image at :2332 and the folded rewrite lands after — a blanket reorder would change what the shared expert reads. Since helper claims never occur on folded layers (`expert_helper_candidates.cpp:80` skips `had_folded`), the plan keeps the quantize exactly where it is and instead publishes the ring image from a **tail kernel issued after the final image write** (end of `mid()`, after :2357). This is strictly additive: no existing kernel moves, so the bit-exactness argument ("the same 36-byte blocks", verify.cpp:2320-2322 + remote_experts.cpp:719-724) is preserved by construction with no folded/qdedup case analysis. Cost: one extra 1024-thread copy kernel (~2-3 µs) per doorbell layer, overlapped with host classify work.

**Exact change list**

1. `src/kernels/cuda/elementwise.cu` (beside `doorbell_publish_value_kernel` :424-444) — new kernel + host launcher:
   ```
   __global__ void doorbell_xq_publish_kernel(const uint8_t* __restrict__ xq, int64_t bytes,
                                              uint8_t* __restrict__ xq_out, uint32_t* __restrict__ seq, uint32_t value)
   // for (i = tid; i < bytes; i += blockDim.x) xq_out[i] = xq[i];
   // __threadfence_system(); __syncthreads(); if (tid==0) { fence; *(volatile uint32_t*)seq = value; fence; }
   void doorbell_xq_publish(const uint8_t* xq, int64_t bytes, uint8_t* m_xq_slot, uint32_t* m_xq_seq,
                            uint32_t value, void* stream);   // <<<1, 1024, 0, stream>>>, check_launch
   ```
   Declaration in the kernels header beside `doorbell_publish` (src/kernels headers, HIP/CUDA dual path as the neighbors).
2. `src/core/verify.cpp`:
   - Allocation (init, beside the other mapped rings via `mapped()` :514-528): `m_xq_` = **2 slots × max_t_ × (N/32) × 36 B**. Byte math: N=2560 → 80 blocks × 36 B = 2880 B/row; max_t_=8 (kMaxTok, protocol.hpp:64) → 23,040 B/slot; depth 2 = **46,080 B ≈ 45 KiB per Verifier**; 2 stages (split@30) = **90 KiB pinned-portable total** — five orders below the mmap-ready-time-killer class (A1's 50.7 GiB PLE mmap, 18bc; RSS impact nil). Plus `m_xq_seq_` = 2 × 4 B. Depth-2 indexed by step parity `(step & 1)` removes any G=2 group-overlap question; the doorbell protocol's happens-before (host reads slot k only between ring k and flagB k; GPU cannot reach publish k+1 before flagB k) already makes depth-1 safe — depth-2 is margin for the waitA/waitB overlap review.
   - `record_window`, end of `mid()` (after :2357), gated `STRATA_HELPER_XQ_RING` (default OFF, static read — OFF adds NO kernel to the capture, graph bytes identical): when gate AND `expert_layout().native` AND the doorbell graph is active (not `ar_on()`): `doorbell_xq_publish(nat_xq_ + tb*(N/32)*36, n*(N/32)*36, d_m_xq_ + (step&1)*slot_bytes, m_xq_seq_ + (step&1), want, cs)`. For folded layers the image at that point is the ROTATED one (:2352) — correct for F3's CPU-pool consumer; helper never claims folded layers so the F2 consumer never sees a folded image.
   - Service loop (`run()`, verify.cpp:3518-3619): after `set_plan_slot(grp)` (:3574) and before `pool(...)` (:3578), when the gate is on: bounded spin on `m_xq_seq_[step&1] >= want` using the SAME discipline as the main doorbell wait (:3526-3561: `strata_cpu_pause`, periodic `cudaStreamQuery`, `kGpuProgressTimeout`, `release_gpu_waits` on timeout) — expected wait ≈ shared-expert+quantize tail (~tens of µs), i.e. usually already rung. Expose to the pool: `win_pool_user`/`remote_opt_` path gains the current slot pointer + byte count + a valid flag (`cur_xq()` accessor: host pointer `h_xq_ + (step&1)*slot_bytes`, bytes `n*(N/32)*36`, valid iff gate && native && this step published).
3. `src/core/remote_experts.cpp` `proxy_begin` (:619-809) — behind the same gate:
   - New precondition block before the :744 quantize: if `ring_valid && !had_folded(layer) && wire-format-is-q8_1` (the kBegin wire is always q8_1 blocks — 36 B/32 elems; the layer check is `lay.fmt[layer].gu_act` not requiring Q8_K and not q8_0-scaled non-native): call `helper_->begin(..., q8 = ring_bytes, ...)` — **the frame is byte-identical to today's** because `build_window_payload` (expert_helper_socket.cpp:586) memcpys whatever image pointer it is given. No client, protocol, or helper change.
   - Miss/fallback: ring invalid, folded, Q8_K layer, or gate off → today's `quantize_q8_1_cpu` path verbatim (:744-751). The stripe arm (:757-796) and Q8K/W1 precedence order (Q8K :684 → F32 :725 → default) are untouched; the ring slots in exactly where :744-751 sits today.
   - The per-frame `q8.assign` churn (:749-750) disappears on the ring path (the thread_local vector is not touched — the ring bytes go straight into the payload builder).
4. Protocol: **no change** (state it in the commit message: mixed engine/helper builds are safe by construction — the helper cannot distinguish the image's origin). This is the analysis's "strictly better than W1" note made concrete: W1 (`STRATA_HELPER_F32_WINDOW`) needed `kFeatureF32Window`; F2 needs nothing.

**Commit slicing**
- `feat(kernels): doorbell_xq_publish — mapped-ring publish kernel (unused)` — kernel + launcher + header.
- `feat(core): STRATA_HELPER_XQ_RING — m_xq_ mapped ring + tail publish in record_window` — allocation, capture-time-gated tail kernel, service-loop wait, `cur_xq()` (no consumer yet; OFF = identical graph).
- `feat(core): helper claim frames consume the m_xq_ ring image` — proxy_begin switch behind the same gate + fallback.
- `perf(core): drop the per-frame q8 staging alloc on the ring path` — cleanup, only reachable gate-on (fold into #3 if trivial; keep separate if it touches the W3.3 buffers).

**Validation ladder**
- (a) build_pin.sh from the worktree (`f2-<n>`); host `STRATA_BUILD_TESTS=ON` build + ctest for core.
- (b) In-tree (all host-only, the `expert_helper_f32_window_test.cpp` pattern — socketpair fake helper):
  - NEW `tests/core/expert_helper_xq_ring_test.cpp`: (i) default-off equivalence — gate unset → proxy_begin sends the CPU-quantized frame bytes, byte-compare against a golden vector; (ii) gate on + ring image → frame bytes IDENTICAL to (i) given the same input activations (this is the whole proof: producer change, bytes equal); (iii) ring-invalid → fallback bytes equal (i); (iv) folded layer never takes the ring.
  - NEW `tests/kernels/xq_triple_parity.cpp` (or extend an existing quantize test): `quantize_q8_1_rows` (GPU, build window) vs `native_quantize_q8_1` vs `quantize_q8_1_cpu` on random rows incl. rotated inputs — the bit-exact chain the analysis cites, now asserted on THIS build.
  - The f32_window test's golden GPU-quant dump-compare harness runs unchanged (regression guard for the shared payload builder).
- (c) Engine gate: variant launcher (+`STRATA_HELPER_XQ_RING=1`), `lane_boot.sh` boot-gate; `quant_quality.py run --config <qqNIGHT variant> --env STRATA_HELPER_XQ_RING=1` → `compare` vs baseline: **KL 0.00000, top-1 100%** — the campaign convention is ≥3 clean passes (18aw's 4/4-fail lesson: run the harness 3× before believing it). Then `lane_judges.sh` fill80k+tg80k ×2 boots: warm TG target +2-4% (out of the measured 2.7-5.3 ms/window host quantize tax). Watch the helper log's reqs/frame — unchanged 37/window expected (claims don't change).
- (d) Rollback: `lane_restore.sh`, delete variant launcher; gate default OFF in tree; pin untouched until the operator promotes.

**Risks + mitigations**
- **#267-class (mapped ring + new GPU→host seq)**: the ring's lifetime is Verifier-owned, identical to `m_x_/m_ids_/m_seq_` (allocated in init, freed in dtor, graph capture embeds the device alias — rebuilds re-capture the same pointers). The `m_xq_seq_` wait reuses the main doorbell's timeout+release path, so a wedged tail kernel is diagnosed by the SAME NEVER-RANG/TIMEOUT trace, not a new hang class. The B1/B2 lessons (18ba: "waits released but GPU did not finish = stream wedged on real work, never the flags") apply verbatim: the wait is bounded, the release path exists, and no spin kernel reads `m_xq_` after the copy completes (single-shot kernel, done before flagB).
- Ring sizing/wraparound: depth-2 by step parity; slot covers max_t_=8 rows; a window with T>8 cannot occur (kMaxTok=8 is the protocol's own ceiling, checked at proxy_begin :626).
- mmap/RSS: 90 KiB pinned total — negligible (the A1 lesson is about GiB-scale mappings; state the math in the commit message).
- Mixed engine/helper builds: no wire change — safe.
- Q8_K / F32-window precedence: ring path sits AFTER both (it replaces only the default :744 path) — interaction-free by ordering.
- Fallback completeness: every miss path (folded, non-native, ring stale, gate off) lands on today's code — the review checklist item is "no path reaches the ring image with a non-q8_1 wire format".

**Definition of done**: xq_triple_parity green; frame-bytes equality test green; 3× qq harness KL 0.00000 with gate on; 2× judges boots with warm TG ≥ +2% and helper reqs/frame unchanged; journal round with the measured per-window host-quantize delta (expect ~2.7-5.3 ms → ~0).

## F3 — CPU-pool activation quantize from the F2 ring (WT +0.5-2%)

**Exact change list**

1. `src/core/expert_source.cpp:5316-5327` (`expert_pool_dispatch_multi`'s per-token quantize loop) — behind `STRATA_CPU_POOL_XQ` (default OFF; separate from F2's gate so the two effects A/B independently, both reading the same `cur_xq()` plumbing):
   - When `ring_valid && lay.fmt[layer].gu_act == GGML_TYPE_Q8_1` (native layout, q8_1 act format): replace the per-token `native_quant_act` loop with ONE `memcpy(d.act_multi_base, ring_slot, n * fmt.act_bytes)` — per-row act_bytes for q8_1 = (N/32)×36 = 2880 B; n≤8 rows → ≤23 KiB memcpy vs n × 10-30 µs quantize. The `ep` skip rule (:5320, tokens whose experts all ran on GPUs) does not apply on this path (any_cpu true ⇒ some token needs CPU rows).
   - Parity argument (from the analysis, now sharpened by the anchor walk): for UNFOLDED layers ring = `quantize_q8_1_rows(xm)`; for FOLDED layers ring = `quantize_q8_1_rows(hadamard(xm))` (verify.cpp:2351-2352) — and `native_quant_act(fmt, x)` produces exactly `quantize(rot(x))` for folded fmts (verify.cpp:2344-2348's own contract: the CPU pool "rotates its own in native_quant_act"). Both cases are byte-equal; the q8_1-format gate excludes every other format; the triple-parity test (F2) asserts the chain including rotated inputs.
   - Layout check at implementation time: `fmt.act_bytes == (N/32)*36` for Q8_1 act format, and `d.act_multi[t]` rows are contiguous — assert once at init, fall back per-layer otherwise (belt-and-braces; a mismatch would be an engine bug, not a plan risk).
2. Q2-native (`act_quant_any`, :5321-5322) and q8_0/non-native (:5326) paths: untouched.
3. Gate read: same static pattern; OFF = today's loop byte for byte.

**Commit slicing**
- `feat(core): STRATA_CPU_POOL_XQ — CPU-pool act image from the m_xq_ ring (q8_1 layers)` — one commit (small, self-contained, needs F2's commit 2 landed).

**Validation ladder**
- (a) build_pin.sh + host tests build.
- (b) Extend `tests/kernels/xq_triple_parity.cpp` with the act-layout assertion (ring layout == `native_quant_act` output layout, folded and unfolded, random rows). Host-side dispatch test: a fake dispatch in `tests/core/` (the dispatch is host code around the quantizer) asserting `d.act_multi` bytes equal on both paths.
- (c) Engine gate: one qq boot with `STRATA_HELPER_XQ_RING=1 STRATA_CPU_POOL_XQ=1` → KL 0.00000 ×3 (both gates on is the shipping config; also run one gate-combination matrix boot: ring-only, pool-only impossible (pool needs ring), off — 2 combinations); judges ×2: warm TG +0.5-2% on top of F2 (expect the c1→c2 dispatch slice in the serve summary to shrink; F7c's `ms_pool` field shows it directly).
- (d) Rollback: same as F2 (env off / variant delete / pin untouched).

**Risks + mitigations**
- Wrong-image-consumed (the one real risk): the ring slot is step-parity indexed and read before flagB — same window, same group; the service loop sets `cur_xq()` per step, the dispatch cannot see a stale slot (it runs inside the same serve step). Review item: `cur_xq()` must be captured before `pool()` and cleared after.
- Fallback: format gate + `ring_valid` per layer — any miss takes today's loop.

**Definition of done**: parity test green; KL 0.00000 ×3 on the combined config; judges show the additional `ms_pool` reduction; journal round.

---
# Phase 2

## F5 — PLE gather off the layer0→layer1 handoff (side thread raises m_flag_) (WT +0.5-3%)

**Exact change list**

1. `src/core/verify.cpp`:
   - New member: a PLE gather thread with the `NextLaunch` RAII shape (verify.cpp:3483-3494: local struct, `std::thread th`, destructor joins + releases). Local per-`run()` invocation (NOT a persistent member — one thread per window, joined at window end, the analysis's rule "join at window end like NextLaunch does").
   ```
   struct PleGather {                      // RAII: joined at scope exit (window end)
       std::thread th; std::string err; bool done = false;   // err read after join
       ~PleGather() { if (th.joinable()) th.join(); }
   };
   ```
   - Thread body: compute the window's PLE row ids from the staged tokens (same `ple_rows` derivation the inline path uses at :3509/:3611 — hoist that computation into a small helper both call sites share), `ss.ple.table->gather_batch(ple_rows, T, h_ple_, err)` → on success `atomic_thread_fence(seq_cst) + strata_store_fence() + *m_flag_ = 1` (host store to the mapped flag; the graph's `wait_flag_ge(m_flag_, 1, cs)` at :1499 is a GPU spin on mapped memory — no host API involved); on failure record `err` and STILL raise `*m_flag_ = 1` (so the graph cannot spin forever) — `run()` checks `ple_gather.err` at the post-sync point (:3661-3663) and returns the error cleanly. This is the explicit hang-avoidance rule: a failed gather must not become a #267-style window death.
   - Start point: `stage_inputs` time (verify.cpp:3146 — tokens are known pre-launch; if the row-id derivation needs `pos0`/step data staged later, start at the top of `run()` BEFORE the launch — still ahead of today's k==0 gather by the whole layer-0 attention span). Gate `STRATA_PLE_ASYNC_GATHER` (default OFF).
   - Service loop changes, gated: the `ar_on()` first-launch pre-raise (:3504-3516) and the k==0 inline gather (:3609-3615) are SKIPPED (the thread owns both raises). `early_ple_flag_mode` (verify.cpp:381-390, default 2 on HIP): the mode-2 first-launch deadlock the comment describes (host inside `hipGraphLaunch` while the raise sits behind it) is DECOUPLED by design — the raise now comes from a different thread, never behind the launch call; the first launch of a graph variant behaves like mode 1 for that window (safe; the 1.4% overlap loss the comment prices applies only to that first launch, same as today's mode 2).
   - `do_ple` false or !ple_on: no thread, today's `*flag = 1` logic untouched.
2. Bit-exactness: same `gather_batch` bytes into the same pinned `h_ple_`, same fence+flag protocol, same consumer order (layer-1 `wait_flag_ge` then `copy_from_mapped(ple_)`, :1497-1500). The analysis's argument (same bytes, same flag protocol, same consumer ordering) preserved verbatim.

**Commit slicing**
- `feat(core): STRATA_PLE_ASYNC_GATHER — PLE gather on a side thread raising m_flag_` — one commit (the change is cohesive; OFF skips both the thread and the loop changes).

**Validation ladder**
- (a) build_pin.sh + host build (`ple_residency_test.cpp`, `tests/hip/ple_iq4.cpp` compile/run in the build window).
- (b) In-tree: the PLE same-bytes contracts already exist (ple_fp8_parity, ple_reader_test — roadmap A1's citation); ADD a host-only test for the failure path: fake `gather_batch` returning false → thread raises flag + records err → `run()` returns the error (the hang-avoidance rule as an asserted behavior).
- (c) Engine gate: one qq boot with `STRATA_PLE_ASYNC_GATHER=1` → KL 0.00000 ×3 (PLE rows feed layer 1 — any byte difference shows here loudly); judges ×2: warm TG +0.5-3% (the F7c `ms_host` field should show the gather leave the k==0 slice; `ms_wait` for layer 1's ring may absorb part — net is the win).
- (d) Rollback: lane_restore.sh; env off; pin untouched.

**Risks + mitigations**
- **#267-class / thread lifetime (the B1/B2 lessons, 18ba)**: the thread is joined at EVERY window end (RAII, before `release_gpu_waits` could ever run — the destructor runs at `run()` scope exit, after the :3661 sync; on the timeout/error paths the join happens before the error return, so no thread writes mapped memory after the verifier gives up). No cross-window state; a stream rebuild invalidates nothing (the thread touches only `h_ple_`, `m_flag_`, and the PLE table reader — all Verifier-lifetime or caller-lifetime objects; the PLE table pointer is captured at start and used within the window only).
- gather I/O error: flag raised + err propagated (above) — no hang, clean 5xx-class error like today's inline failure.
- Two raises (thread + inline path): `h_flag_` (:3500) is the HOST ALIAS of the same mapped word layer 1's `wait_flag_ge(m_flag_, 1)` spins on (the `mapped()` h/d pair, verify.cpp:514-528); the loop's per-step `*flag = want` (:3616) is what raises it today (k==0 writes 1 after the gather). On the gated path the host SKIPS its k==0 `*flag` write (the thread is the sole raiser of the initial value — this is the whole point: the host never blocks on the gather, the GPU stalls at layer 1's wait instead); k>0 writes continue as today (monotonic values ≥ 1, harmless to the wait). The gated path also removes the ar_on first-launch pre-raise (:3504-3516). Review item: exactly one raiser per value.
- First-launch HIP graph lock: covered by the design (raise from a non-launching thread).

**Definition of done**: failure-path test green; KL 0.00000 ×3; judges ×2 with warm TG gain in the +0.5-3% band and `ms_host` k==0 reduction visible in the F7c fields; journal round.

## F7ab — boot I/O determinism: O_DIRECT arena reads + offset-sorted fill order (PP +7-20% on slow boots; HYPOTHESIS)

**Hypothesis discipline** (the analysis marks it HYPOTHESIS; the operator already falsified PLE-mmap-as-fix in 18bc — this is the narrower page-cache-warmth mechanism with the 376-671 MB/s same-work evidence from 18bd). The measurement program decides adoption; the code lands default-off regardless.

**Exact change list**

1. (a) O_DIRECT boot reads — `src/core/expert_source.cpp` + `src/program/generate.cpp`:
   - The unbuffered read machinery already exists for swap-ins (`open_direct` O_DIRECT fds, 4 KiB-aligned windows, expert_source.cpp:2521-2551 + `read_direct` :1796; the "batched O_DIRECT pack swap-ins" landed as 0359cedf6, journal :185-186). New gated read mode for the BOOT profile fill: `STRATA_BOOT_FILL_ODIRECT=1` → the fill loops obtain blob bytes via a batched O_DIRECT pread into the F6 pinned staging halves (natural merge: F6's `fill_slots_batched` becomes the read destination — pread(2) directly into the pinned buffer, aligned window handling per `read_direct`'s existing rules) instead of `blob_stable` page-cache reads.
   - Fallback chain (explicit): O_DIRECT open fails (expert_source.cpp:2525's `why` path) or alignment impossible → page-cache `blob_stable` as today, with a boot-log line naming the fallback ("keep page-cache fallback under low RAM" is automatic: O_DIRECT REDUCES RSS pressure; the fallback exists for filesystems/failures, not RAM).
2. (b) Offset-sorted read order — `generate.cpp` fill loops:
   - Build a one-time sorted INDEX of the fill pairs by pack file offset (the source already knows each pair's blob offset — expose `blob_offset(layer, expert)` beside `blob_stable`; if not exposed, add a const accessor). The READ batch picks the next ≤kBatchStage pairs in OFFSET order; slot ADMISSION stays in rank order (`admit()` sequence unchanged — capacity-bound selection semantics identical to today; only which staging half a blob lands in first changes). Identical bytes into identical slots — bit-exact by construction (read order only).
   - Gate: `STRATA_BOOT_FILL_SORT=1` (independent of O_DIRECT so the two effects separate in the measurement matrix; sorted+page-cache is still a legitimate arm — it converts ranked-random reads into near-sequential for the page cache too).
3. Interaction with F6: the three compose (pipeline + parallel + O_DIRECT/sort). The gates are read independently; the boots matrix covers ON/OFF combinations of the three for attribution.

**Commit slicing**
- `feat(core): ExpertSource::blob_offset + offset-sorted boot read order (STRATA_BOOT_FILL_SORT)` — sort only, gate default OFF.
- `feat(core): STRATA_BOOT_FILL_ODIRECT — boot profile fills read through the O_DIRECT path` — uses F6's pinned halves; gate default OFF.

**Validation ladder**
- (a) build_pin.sh + host build; `tests/core/file_expert_source_test.cpp` (59.6K — the source-tier test) extended: O_DIRECT mode reads return identical bytes to the mmap path over a fixture pack (the byte gate for read-path changes).
- (b) In-tree: the F7c fields (already landed) are the instrument — no further test files.
- (c) Engine gate — this is a DISTRIBUTION measurement, not a single boot: ≥6 interleaved boots (3× gate-off control, 3× gate-on; grow to 8+ if the classes straddle) via `lane_boot.sh` + one `lane_judges.sh` fill80k per boot (ONE fresh fill per boot — the 18bb lesson: second fills are cache-resident and useless). Read the F7c pack-read MB/s and the fill wall per boot: hypothesis CONFIRMED if the on-boots' pack-read rate clusters tight at/above the fast class (≥600 MB/s) with the bimodal split gone; judge PP (fill tok/s) fast class 432-455 reached from every on-boot. Zero expected TG change (decode untouched) — one tg80k sanity leg on the last boot.
- (d) Rollback: env off; gates stay OFF in tree; promotion is an operator decision after the distribution evidence.

**Risks + mitigations**
- Hypothesis falsified (slow class persists with O_DIRECT): the gates stay off, F7c keeps paying (every boot self-attributes), and the mechanism search moves to B6 (queue priming) with F7c as the discriminator (see the F7c section).
- O_DIRECT read amplification on unaligned blob edges: reuse `read_direct`'s windowing (head/tail padding inside the pinned half) — the swap-in path's own rule; no new alignment math.
- Pack-file fragmentation making offset order ≠ sequential: measurable directly (F7c pack-read rate with SORT on, O_DIRECT off).
- Boot-time p85 regression on FAST-class boots (O_DIRECT drops page-cache warming that later swap-ins would reuse): the matrix measures it (fill wall + first swap-in rates with F7c fields); if it appears, O_DIRECT stays off and SORT alone may still win.

**Definition of done**: ≥6-boot distribution table in the journal round; either (i) confirmed — slow class eliminated on ≥3/3 on-boots, operator promotes the gates, or (ii) falsified — gates remain default-off with the F7c attribution data recorded and B6 re-queued as the live suspect. No middle state.

---
# Phase 3 — speculative prefetch

## F4 — next-layer miss prefetch: helper weight pre-stage + PCIe blob pre-stage (WT +5-15% ceiling)

**⚠ Plan-level refinement — flagged per the discipline.** The analysis's step (a) as written — "the helper computes candidate rows into a scratch ring" at layer l's service for layer l+1 — is **not constructible bit-exactly**: the helper's grouped kernel needs layer l+1's ACTIVATIONS (`proxy_begin(layer, x, ...)` consumes the current layer's post-attention rows, remote_experts.cpp:619-621), and `x_{l+1}` does not exist until layer l's combine completes — after the service the prefetch would precede. Rows computed from any predicted activation would fail the analysis's own consume rule ("only rows whose (layer, token, expert, weights) match the ACTUAL routing"), which checks routing and weights but cannot validate a wrong input vector. The implementable form that keeps the analysis's quality argument and its value target (the residual `finish` wait + PCIe stream time — exactly what the analysis's own F4-refinement section prices) is:
- **(a) helper weight pre-stage**: the prefetch frame carries layer + predicted ids (+ primary-residency bitmap); the helper pre-reads those experts' blobs from its pack into a scratch set keyed by (layer, expert) and pre-builds the grouped-kernel metadata; at the REAL begin (with the F2 ring image) the compute starts with zero pack-read prelude. Rows are still computed only at the real begin from the real image — bit-exact by construction (identical weights bytes — the pack is immutable; identical image; identical kernel).
- **(b) PCIe blob pre-stage**: predicted non-resident blobs pre-copied into dedicated staging-ring slots on the copy engine (idle while the host serves); at plan time entries whose (layer, expert) matches a staged slot take `P.ptr2` from the staging ring (expert_source.cpp:5232-5233) instead of a demand fetch — validated by the key, bytes identical (immutable pack offsets).
If, at implementation time, a reviewer finds a constructible true-row-prefetch (e.g. an activation-independent pre-transform), it can be added under the same consume-only rule — the plan does not depend on it.

**Exact change list**

1. **Protocol** — `include/strata/core/expert_helper_protocol.hpp` (the extension rule of :110-115/132-139, verbatim pattern):
   - `inline constexpr uint32_t kFeaturePrefetch = 256u;` (bit 8; HelloAck.features).
   - `kPrefetch = 22` (engine → helper: layer, predicted ids[≤16], primary-residency bitmap — the SAME body sections as kBegin minus the activation image) and `kPrefetchAck = 23` (helper → engine: n_staged, tiny). An old helper never sees the frame (client-side negotiation gate copying `begin_q8k`'s at expert_helper_socket.cpp:631-633) — **an old helper refuses cleanly by never being asked**; a new engine + old helper sends nothing and disables the feature.
2. **Helper side** — `tools/mixed_gpu/sidecar/helper_main.cpp` (+ its helper-tier source files):
   - Advertise `kFeaturePrefetch` in kHelloAck when the scratch set initialized (capacity-bounded: ≤64 experts × blob bytes ≈ ≤~150 MiB of its reserve — size from the helper's existing arena-cap accounting, `expert_helper_arena_cap` tests as the guard; on capacity pressure the handler answers kPrefetchAck with n_staged=0 — never an error).
   - kPrefetch handler: read the ids' blobs from its pack into the scratch set (its own stream, no lock held against a rows reply), build the grouped metadata, ack. Key = (layer, expert, pack offset) — offset in the key makes byte-equality a property of the immutable file, not a check.
   - kBegin handler (existing): before its pack read, take any matching scratch entry (key compare; consume = move out). Miss → today's read. Zero changes to the rows computation.
3. **Engine client** — `src/core/expert_helper_socket.cpp`:
   - `int ExpertHelperClient::prefetch(int32_t layer, const int32_t* ids, int n_ids, const int32_t* primary_res, int64_t deadline_ms)` — builds the frame (the `build_window_payload` shape minus image), sent under `io_mtx_`.
   - **One-outstanding rule** (expert_helper_socket.cpp:607-611 — the constraint the analysis's refinement flags): a prefetch must never block or interleave with an outstanding begin/finish pair. Design (the stripe channel-B precedent, remote_experts.cpp:757-796, is a SECOND SOCKET; here the same socket carries it): `pf_outstanding_` flag; `prefetch()` refuses (returns kErrBusy, silently ignored) if a begin is outstanding; the ack is drained NON-BLOCKING inside the NEXT `begin()` under `io_mtx_` (peek; if absent, mark the lane busy and skip the next prefetch round — never block the claim path). Helper-side order is FIFO on its worker: a prefetch arriving during layer l's rows compute is handled after the kRows reply — still before begin(l+1) in wall time for exactly the layers where pre-stage pays (helper idle-ish); where it isn't, the skip rule caps the loss at zero.
4. **Engine predictor + send hook** — `src/core/remote_experts.cpp` (+ the dispatch plumbing in expert_source.cpp):
   - Predictor state per layer: `last_ids[l][k]` (from the doorbell-read `h_ids_`, already read every service) + a decayed usage score (the adaptive tier's `drive.d.usage` where reachable from the dispatch user; else a local exponential decay over observed ids — placement trajectories recur, journal 18al). Prediction set = top-m by score ∪ last window's ids, m ≤ 8 (tunable via `STRATA_PREFETCH_TOP`).
   - Hook: at the end of layer l's doorbell service (after `proxy_begin` returns, before flagB handling — verify.cpp service loop), when `STRATA_HELPER_PREFETCH=1` and the negotiation gate passed: `helper_->prefetch(l+1, predicted, ...)`; fire-and-forget per the lane rule.
   - Instrumentation (F7c fields): `pf_sent / pf_staged / pf_hit` (hit = begin found the key staged) per window on the serve summary — the hit-rate is the feature's dashboard and its kill criterion.
5. **PCIe pre-stage (b)** — `src/core/expert_source.cpp` (the `P.fetch`/staging machinery :5203-5251):
   - Dedicated prefetch staging slots (`P.staging` ring partition: 2 of the existing group slots reserved — `bb` bytes each, ~2-3 MB device/peer memory, or a small side ring beside it; demand groups keep today's slots — **predictions never displace demand staging**).
   - At layer l's service (same hook), enqueue async pack→staging copies for predicted non-resident (layer l+1, id) pairs on the copy engine (`P.fetch`'s own path, called with the predicted set). At l+1's plan, a demand entry whose (layer, expert) matches a staged slot points `P.ptr2[q]` at it (plan-time validation = the key; the blob bytes are immutable — bit-exact by construction). Miss → demand fetch as today; the staged slot returns to the pool at window end (lifetime rule: staging slots are per-window, invalidated/reclaimed at the window's flagB join — no cross-window residue).
6. Quality argument (preserved from the analysis, now exact): predictions only change WHERE bytes come from (scratch set / staging ring vs pack read); every consumed byte is keyed to the immutable (layer, expert, pack offset); a wrong prediction is dropped and the demand path computes it. No change to what is combined.

**Commit slicing** (each independently buildable, gate OFF = no frames, no staging changes)
- `feat(proto): kPrefetch/kPrefetchAck + kFeaturePrefetch (no senders)` — protocol constants + negotiation plumbing only.
- `feat(helper): prefetch handler — scratch weight pre-stage keyed (layer, expert, offset)` — helper side + its unit test.
- `feat(core): ExpertHelperClient::prefetch — lane-busy discipline, ack drained at next begin` — client send path, no callers.
- `feat(core): STRATA_HELPER_PREFETCH — per-layer predictor + doorbell-service send hook + hit counters` — the engine behavior, default OFF.
- `feat(core): predicted PCIe blob pre-stage into reserved staging slots (plan-time validated)` — part (b), same gate (or its own `STRATA_PCIE_PREFETCH` if the operator wants independent A/B — recommended: same predictor, separate gate).

**Validation ladder**
- (a) build_pin.sh (engine) AND the helper binary build (same worktree, sidecar target — build_pin's guards cover the engine; the helper has its own build line in the lane scripts; assert both artifacts' sha recorded).
- (b) In-tree: extend `tests/core/expert_helper_socket_test.cpp`: (i) old-peer simulation — helper advertises no bit → client never sends (socket-level assertion); (ii) prefetch-then-begin round trip on a fake helper: begin's frame unchanged, ack drain never blocks (timing assertion); (iii) capacity-pressure: ack n_staged=0 → engine continues, no retry storm. Helper-side scratch test in the helper's own test file (or `expert_helper_arena_cap_test.cpp` extension): staged weights hash == pack read hash. PCIe part: a dispatch unit test — predicted staging consumed only on exact key match; wrong prediction → demand path → plan bytes identical (golden plan compare).
- (c) Engine gate: qq boot with `STRATA_HELPER_PREFETCH=1` (+PCIe gate) → **KL 0.00000 ×3** (the all-miss case is exercised naturally in the first windows — predictions warm up; the harness still must show zero divergence); judges ×3 boots: warm TG vs the post-P1/P2 baseline with the `pf_hit` rate on the summary — the acceptance curve: hit ≥50% ⇒ expect +3-8%; hit ≥70% ⇒ the analysis's +5-15% band. If hit <30% after m-tuning (m ∈ {4,8,16} via `STRATA_PREFETCH_TOP`), record and stop (see DoD).
- (d) Rollback: env off; gates default-off in tree; helper unchanged for old engines (feature bit only advertised; no behavior without the frame).

**Risks + mitigations**
- **Predictor thrash (bounded)**: worst case (all-miss, m=8): speculative pack reads ≈ 8 ids × ~2.5 MB × 48 layers × ~8 windows/s ≈ 7.7 GB/s of extra PCIe/NVMe traffic against a measured ~60-90 GB/s aggregate with ~95% headroom — a bandwidth tax, not a correctness or latency cliff (the analysis's "costs bandwidth not correctness"). Mitigations: m starts at 4; the hit-rate dashboard (F7c fields) with the operator kill-switch (gate off); predictions never displace demand work (dedicated slots, copy-engine-only, helper-scratch-only).
- **Protocol states**: the extension rule handles mixed builds both directions (old helper: never asked; old engine: never sends). The lane-busy rule caps prefetch to zero cost on a busy helper.
- **#267-class / staging lifetime**: the PCIe staging slots are reclaimed at each window's flagB join (invalidation on stream rebuild = the same window-scoped rule as demand staging); no thread, no GPU spin introduced. The helper scratch is process-local LRU keyed to immutable files — no invalidation needed, only capacity.
- **Value collapse risk**: if F2+F5 already shrinks the residual wait below ~0.3 ms/layer, F4's helper half stops paying — the plan's P3 gate decision re-measures the residual first (the F7c helper begin/finish fields) and may descope to the PCIe half only.

**Definition of done**: protocol/socket tests green; KL 0.00000 ×3 with gates on; ≥3 judge boots with the hit-rate and TG delta recorded; the journal round states either adoption (operator promotes) or the descope/stop decision with the measured residual-wait and hit-rate data. A stop is a valid close (the analysis's own hit-rate conditionality).

---

# Cross-cutting: promotion, rollback, and the oracle/review convention

**Every feature follows the same promotion path** (repo convention, the 18aw/18ax discipline):
1. Commits land on `hc-q8-gfx906` default-OFF; each commit message carries the design note lineage (the W-number style comments in remote_experts.cpp/expert_helper_socket.cpp are the in-code precedent) and the bit-exactness argument.
2. Validation ladder executed per the sections above (build → unit/parity → qq KL 0.00000 → judges).
3. Pin promotion is an OPERATOR decision: a new pin build from the worktree (`build_pin.sh <tag>` — golden gfx906 HIP config, guards enforced) is booted via a variant launcher, survives the standard legs (fill80k/tg80k + no-NaN check), and only then replaces the serving pin (today `36ecbe98`). The gate env stays in the LAUNCHER (prod config json / launch-*.sh one-line diffs via `gpu-rounds/make_variant.sh`), never a code default — flipping a default in code is a separate, later commit class the operator orders explicitly.
4. Rollback at every stage: `gpu-rounds/lane_restore.sh` (prod restore + no-NaN check) + delete the variant launcher; the tree needs no revert (gates OFF). A promoted pin rolls back by re-pointing the launcher to the previous pin binary (the pin's sha256 is recorded by build_pin.sh) — minutes, no rebuild.

**Boot-gate discipline for every validation boot** (the lane toolkit encodes it): `lane_boot.sh <launcher> <tag>` = stop-all → setsid --fork boot → FRESH `ready: http://` line + `/v1/models` probe (log-mtime alone passes stale logs) → only then legs; helper log paired (`-helper.log`); NaN/503 scan after legs (`lane_restore.sh`'s check when restoring).

---

# First week — concrete integrator sequence

Assumption: one integrator, GPU lane available ~4 h/day (prod serves otherwise; every boot/leg uses the lane toolkit; only one lane at a time per the operator's rules). Items are worktree commits on `hc-q8-gfx906` (never prod's pin).

**Day 1 — tooling warm-up + F7c + F6 start**
1. Rebuild baseline pin from HEAD to verify the toolchain: `tools/mixed_gpu/build_pin.sh wk1-base`; record sha.
2. Land F7c (one commit, log-only). Build (`build_pin.sh f7c`), boot once via `lane_boot.sh`, run one `lane_judges.sh` pair; confirm the new fields and that judge parsing still works. (~1 lane-boot + 1 judge pair ≈ 40 min GPU.)
3. F6 commit 1 (`fill_slots_batched` + unit test); host build + ctest -R expert_cache.
4. Start F6 commit 2 (loops behind `STRATA_BOOT_FILL_QUEUED`).

**Day 2 — F6 finish + first gated boot**
1. Land F6 commits 2-3; `build_pin.sh f6`; boot with the env via variant launcher; read the fill-rate line (expect ≥2×); `verify_slot` pass = byte gate; one judges pair to confirm no read-path change; `lane_restore.sh`. (~1 h GPU.)
2. F8 commits; build together with F6 gate env; qq gate boot: `quant_quality.py run --config <qqNIGHT variant> --env STRATA_BOOT_FILL_QUEUED=1,STRATA_PENALTY_MAPPED=1` → compare → **KL 0.00000** (×1 first pass; 2 more passes later in the week). (~40 min.)
3. Start F1 commit 1 (sampler kernels).

**Day 3 — F1 + F2 kernels**
1. F1 commits 1-3 + parity test; build-window ctest (sampler parity). qq boot with all three P0 envs → KL 0.00000 (pass 2). Judges pair on the P0 stack (expect small TG uptick; the main deliverable is clean gates). (~1.5 h GPU.)
2. F2 commit 1 (`doorbell_xq_publish` kernel) + commit 2 (ring + tail publish + wait). Host tests for the ring start (`expert_helper_xq_ring_test.cpp`).

**Day 4 — F2 finish + gate**
1. F2 commits 3-4 (proxy_begin consumption + staging cleanup); `xq_triple_parity` test; `build_pin.sh f2`.
2. qq gate ×2 boots: `STRATA_HELPER_XQ_RING=1` → KL 0.00000 (passes 1-2 of 3). (~1 h.)
3. Judges ×2 on F2 (warm TG +2-4% target; helper reqs/frame unchanged). `lane_restore.sh`. (~1 h.)

**Day 5 — F3 + first F2+F3 combined gates + F5 start**
1. F3 commit; triple-parity extension; combined qq gate `STRATA_HELPER_XQ_RING=1 STRATA_CPU_POOL_XQ=1` → KL 0.00000 ×1 (passes continue into week 2 as the 3× rule requires).
2. Judges ×2 on F2+F3. (~1 h.)
3. F5 commit + failure-path test (code only; gate boot in week 2).
4. Week-1 journal round: record all gate outputs, judge deltas, the F7c attribution lines from every boot (each P0/P1 boot already produced attribution data for free), and the P3 go/no-go inputs (helper begin/finish residual from the F7c fields).

Week 2 preview: F5 gate + judges; F7ab's 6-8-boot distribution campaign (can interleave with F5 coding); P3 decision point on F4 with the residual-wait data.

---

# Definition-of-done summary table

| Item | Closing evidence |
|---|---|
| F6 | 3 boots ≥2× fill-rate in the :9405 line, verify_slot green each, judges unchanged, 1 clean qq at promotion |
| F8 | qq KL 0.00000 (gate on), no per-window blocking H2D in the round trace |
| F1 | sampler parity test green; qq KL 0.00000; second sync gone on sampled turns |
| F7c | fields on ≥2 boots, one known-slow phase correctly attributed, scripts still parse |
| F2 | frame-bytes equality test + triple parity green; qq KL 0.00000 ×3; judges ×2 warm TG ≥ +2%; helper reqs/frame unchanged |
| F3 | parity green; combined-config qq KL 0.00000 ×3; judges ×2 additional ms_pool reduction |
| F5 | failure-path test green; qq KL 0.00000 ×3; judges ×2 warm TG +0.5-3%; k==0 ms_host reduction |
| F7ab | ≥6-boot distribution: either slow class eliminated (operator promotes) or falsified with attribution data recorded |
| F4 | protocol/socket tests green; qq KL 0.00000 ×3; ≥3 judge boots with hit-rate + TG delta; adopt / descope / stop decision journaled |

*End of plan.*
