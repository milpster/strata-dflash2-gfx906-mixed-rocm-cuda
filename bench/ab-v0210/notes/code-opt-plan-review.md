# Review — code-opt-plan.md (F1–F8, branch hc-q8-gfx906)

- Reviewer: Oracle (reviewer of record, pre-promotion convention). Date: 2026-11-15.
- Inputs: `code-opt-plan.md` (451 L), `code-opt-analysis.md`, engine at `/mnt/srcds/Strata-wt-integ` HEAD `84fc595dd9` (clean), journal rounds 18at–18bf + today's AUXCPU1 follow-ups.
- Method: every anchor below was re-read in the worktree this session (file:line cited is what I checked, not what the plan claims). Read-only; no builds, no GPU commands, no process interaction.

## Executive verdict

**APPROVE-WITH-CHANGES for P0 start** (F6 + F8 + F1 + F7c may land, with the amendments below).

- P0 items are technically sound; their OFF-paths are genuinely byte-identical by construction.
- **F5 as designed is UNSOUND and must be redesigned before any coding** (blocking for F5, P2 — does not affect P0): the side thread raising `m_flag_` early breaks the doorbell graph's CPU-rows-ready contract. Proven from code below.
- F2 is sound in shape but has one real bug (stale `m_xq_seq_` across windows) and two evidence gaps (QFUSE-producer parity, helper-tier gating) — all cheap fixes, blocking for F2's lane boots only.
- F4's plan-level refinement (weight/blob pre-stage instead of true-row prefetch) is correct and well-argued; two design gaps remain (helper serve-thread serialization; ack-drain point) — amend before P3.
- **Baselines are invalid twice over** (pinning fault since Nov 6 + today's unexplained post-16:21 warm-TG collapse): P0 code + KL gates may proceed now (speed-independent); all judge legs and WT%/BT% acceptance numbers must be re-based on the adopted-AUX_CPUS config once the box state resolves. Details in §7.

---

## 1. Per-item findings

### F6 — boot slot pre-fill queued/batched/parallel — **SOUND** (minor amendments)

Verified in code:
- Serial loop `generate.cpp:9344-9368` (`admit` :9351, `blob_stable` :9356, `fill_slot_blocking` :9359; `read_batch`/`std::async` prefetch :9345-9348 with `aux_cpus::pin_current_thread`); per-stage loops :9528-9541 inside `OnDevice on(st.dev)` (:9450); `verify_slot` checks :9380-9385 and :9547-9550.
- `fill_slot_blocking` HIP staging discipline `expert_cache.cpp:807-837` (single-expert pinned staging + blocking copy, the pageable-async trap the plan cites at :819-826 — exact); degenerate `fill_slot_queued` :839-855 (pageable source, stream 0); `fill_slot_queued_staged` :860-876.
- NibbleCache discipline `generate.cpp:3232-3289`, the "one stream, so the staging buffer's reuse is ordered without events" comment at :3229-3231. Note NibbleCache's own staging is **device** memory (:3273 `cudaMalloc`); what F6 borrows is the batch/one-stream shape — the plan's pinned-host double-buffer with one event per half is the correct strengthening, not a copy. Design is right.

Findings:
1. **(Non-blocking, DoD)** "verify_slot's D2H compare is the byte-level proof on every boot" overstates it: `verify_slot` reads back **one** slot per boot (:9374-9383's own comment: "ONE SLOT IS READ BACK AND COMPARED"; same in the stage loops). The real all-slots proof is the in-tree test (plan's (b), good) — and see §2 for a stronger engine-level gate that already exists (`STRATA_VERIFY_COMPLEMENT`, generate.cpp:9409).
2. **(Non-blocking, expectation)** The 422–671 MB/s boot band is read-side variance (18bd), and the fill wall today is ~max(read, H2D). At 671 MB/s, 9.6 GiB reads ≈ 14.3 s of the observed 14.8 s wall — the fast class is already near read-bound, so F6's pipeline mainly removes H2D/launch serialization, and the "≥2× end-to-end" DoD may only hold when reads are cache-warm. Re-state the DoD per-phase (F7c fields) rather than end-to-end ×2. This is exactly why F7c-first is right.
3. **(Non-blocking)** Parallel threads must each establish their own `OnDevice` (cudaSetDevice is per-thread state); the stage loops' scope at :9450 does not transfer to a fresh `std::thread`. The plan's wording implies it; make it explicit in the commit.
4. **(Non-blocking)** F6's baselines were measured with the fill/stage threads parked on CPU 0 (today's pinning root cause — `strata-stage` + fill threads were among the parked). The pre-fix 422 MB/s slow class is partially confounded by that; the ≥2× target must be re-based post-AUX_CPUS (§7).

Byte math checked: 2 × 8 × ~1.38 MiB ≈ 22 MiB/cache, 44 MiB two cards — correct (blob ≈ 1.38 MB per :9376 comment); transient, freed at `close()`. Negligible vs the 46 GiB arena (18at).

### F8 — penalty-history mapped staging — **SOUND** (two text fixes)

Verified: `penalty_rows` + blocking `cudaMemcpy` at generate.cpp:22590-22594 between the adapt refill and `ver.run` (:22624); the `copy_i32_from_mapped` family (verify.cpp:1427/1435 and `copy_i32_from_mapped_kernel` elementwise.cu:462+); `mapped()` verify.cpp:515-528.

Findings:
1. **(Non-blocking, correctness of claim)** "Build the rows during the previous window's GPU run" is not realizable in the serial decode loop: the rows need THIS window's draft tokens (`window.data()` at :22590-22591), and the draft runs after the previous `ver.run` returns. The realizable win — and all that is needed — is replacing the blocking H2D with a mapped stage written after the draft, before launch. Fix the text; the mechanism stands.
2. **(Non-blocking)** `kHistMax = 64` is asserted, not verified: the mapped buffer must cover the captured graph's fixed upper bound for the actual `penalty_last_n` cap this engine runs. Verify the cap at implementation; size the buffer to the capture-time constant.
3. Window-0 fallback and the per-window validity word are handled. The in-graph head copy ordering (head copy → tail sampler/`hist_d_` consumer) is consistent both with and without F1 (host sampler at :3721 reads `hist_d_` only after the :3661 sync, by which time the in-graph head copy has also completed). Sound.

### F1 — in-graph head sampler via mapped params — **SOUND** (design verified; two notes)

Verified: double sync at verify.cpp:3661 → :3721 launch → :3722 second sync; the host-sampler rationale comment :3678-3681; greedy in-graph argmax :2658-2664; `sp.counter = pos0` at :3689; `coupled_stage_kernel` sampler.cu:807-817 and `coupled_draft_stage` :1338-1343 (the mapped-staging precedent, in-graph on the drafter); spec buffers `d_spec_` sizing at :3693-3695 (`kVerifyMaxT × (1+kSpecQStride)` ints — the plan's `m_spec_` matches and covers both the draft-token and q-list sections; `kSpecQStride = 2*kSpecQEntries`, spec_prob.hpp:77); `m_out_` mapped read at :3734.

Findings:
1. **(Non-blocking)** The arming condition should include penalized-**greedy** requests: the host block at :3687 fires for `hist_d_ != nullptr` even when `sampling_.greedy` (penalized greedy). The plan's condition ("sampled AND armed AND no logit_bias") would leave penalized-greedy on the host path — correct but it silently halves F8+F1's combined value on that request class. Either arm the generic kernel for it (mode word already supports it — temperature 0 is deterministic) or say so explicitly.
2. **(Non-blocking)** The generic mode-switching kernel must reproduce each of the three kernels' exact statement order per mode (uniform branch on a device-read mode word is fine — one value per replay, no divergence). The ≥1000-window draw-identical parity harness is the right gate; keep it in ctest, not lane-only.
3. Fallback completeness (logit_bias, `measure` legs staying host-side) matches the code structure (:3692 `measure ||` arm; :3715-3718 measure error path). Sound.

### F7c — fill-phase instrumentation — **SOUND**

Verified: the fill-rate line at generate.cpp:9405-9408 (the append target), `fill_t0` pattern :9333, `ms_wait/ms_pool/ms_host` at verify.cpp:3617-3619, B6's `#if` exclusion at :3527-3539 (`STRATA_USE_HIP && !STRATA_HIP_GFX906` — gfx906 excluded, as the plan says). Lane scripts exist at `bench/ab-v0210/rpcprof/gpu-rounds/` (lane_boot/lane_judges/lane_restore/make_variant — the plan's `gpu-rounds/` prefix is relative to rpcprof; harmless). Append-only fields preserve the judges' `cut -c1-200` parsing. No behavior change; no gate needed. The B6/F7 discriminator framing (H2D-phase rate vs pack-read rate) is correct and now more valuable than the plan knew (§7).

### F2 — helper xq ring — **NEEDS-CHANGES** (3 items, then SOUND; blocking only for F2's own boots)

The design core is verified sound:
- The three image-write points and their order relative to the doorbell publish are exactly as the plan says (publish :2286-2298 → qdedup pre-quant :2324 → folded rewrite :2352 → plain :2354; `mid()` ends :2357-2360). The tail-publish placement after the whole :2344-2357 chain is correct, and it is correctly ordered after ALL `nat_xq_` writers (all on `cs`; the `sh_fork` variant writes `sh_xq_`, not `nat_xq_`).
- The folded-layer hazard the plan avoids is real: `shared_expert_multi` reads the unrotated image at :2330-2333 under qdedup, the rotated rewrite lands at :2351-2352 — a blanket quantize-before-publish reorder would corrupt the shared expert on folded+qdedup layers. Keeping the quantize in place and publishing from a tail kernel is strictly additive and avoids the case analysis entirely. Good refinement over the analysis.
- Folded layers never produce helper claims: `expert_helper_candidates.cpp:80` (`had_folded` → skip) — verified.
- No protocol change: `build_window_payload` (expert_helper_socket.cpp:574-587) memcpys whatever image pointer it is given (:586); `proxy_begin`'s default arm is :744-751 with Q8K (:684) and F32-window (:725) precedence above — the ring slots exactly at :744 with fallback paths intact. `n_tok > MAXT` checked at proxy_begin :626 and client-side :594/:636.
- Depth-2 ring safety: the doorbell protocol's happens-before holds — the GPU's publish of ring k+1 sits behind the previous step's `wait_flag_ge(m_flagB_/m_flag_, ring)` chain in `post()` (:2419-2466), and the host reads the xq slot inside `pool()` (:3578), before it raises flagB (:3595-3604) and `*flag = want` (:3616). Host read of slot k always completes before the GPU can issue publish k+2 (the next write to that slot). Depth-2 is margin, as claimed.
- The service-loop wait placement (after `set_plan_slot` :3574, before `pool` :3578) is correct — `proxy_begin` is reached inside `pool` via expert_source.cpp:5284.
- Byte math: 2 × 8 × 2880 B = 46,080 B ≈ 45 KiB/stage, 90 KiB both stages — correct ((N/32)×36 = 2880 B/row, kMaxTok=8 at protocol.hpp:64).
- Kernel clone template `doorbell_publish_value_kernel` (elementwise.cu:424-444) is the right pattern; the 1024-thread copy of ≤23 KiB is ~µs-class.

Required changes:
1. **(BLOCKING for F2 boots — real bug)** **Per-window reset of `m_xq_seq_`.** The ring values restart at 1 every window (`(l-lb_)*G+grp+1` is capture-baked), and `stage_inputs` resets the sibling words (`h_seq_/h_flag_/flagA/flagB` → 0, verify.cpp:3160-3163). Without the same reset for `m_xq_seq_`, window ≥2's `m_xq_seq_[step&1] >= want` (want=1..) passes **instantly on stale values from the previous window**, and the host reads a stale image. The qq KL gate would catch it, but only after wasting lane boots. Add `*(volatile uint32_t*) m_xq_seq_[0..1] = 0` to `stage_inputs` beside `h_seq_` (safe: the previous window's graph is fully synced at :3661 before the next `stage_inputs`).
2. **(BLOCKing for F2's evidence chain, trivial)** **The QFUSE producer leg.** `STRATA_QFUSE` defaults **1** (arch_defaults.cpp:86) and `q8_ffn` layers (verify.cpp:2207) get their `nat_xq_` image from the fused GR read — not from `quantize_q8_1_rows`. The plan's triple-parity test asserts rows==native==cpu but omits the producer that actually writes most images. The missing leg exists in-tree (`gr_parity.cpp:416`: "the read's own q8_1 image of `mixed` must be the bytes native_quantize_q8_1 writes"), so the chain QFUSE==native==rows==cpu is complete — but the plan must (a) cite gr_parity as the fourth-leg evidence, (b) add a QFUSE-producer arm to `xq_triple_parity` (or run gr_parity in the same ctest window), and (c) NOT gate ring consumption on `!q8_ffn` (that would void most of F2's value; rely on the parity chain instead).
3. **(Non-blocking)** Gate the tail publish (or at least the ring consumption) on the helper tier actually being configured (`remote_opt_`/helper-present); publishing with no consumer is pure waste. Also consider gating the `m_xq_` allocation itself so OFF boots keep identical RSS footprint.

### F3 — CPU-pool quantize from the ring — **SOUND** (two precision fixes)

Verified: the per-token loop expert_source.cpp:5316-5327 (`native_quant_act` :5324, `act_quant_any` :5322, `act_quant_q8_1` :5326, ep-skip :5320), the c1/c2 timing :5315/5329, the folded-rotation contract at verify.cpp:2344-2348 ("the CPU pool rotates its own in native_quant_act") — the plan's folded parity argument is the engine's own documented contract.

Findings:
1. **(Non-blocking, naming)** The native-path destination is `d.nact_multi + t*kNativeActBytes` (:5324), not `d.act_multi[t]` (:5322/:5326 are the Q2/non-native destinations). The contiguity/stride assertion must target `nact_multi` with its actual stride; if `kNativeActBytes != (N/32)*36` the memcpy needs per-row striding or the per-layer fallback.
2. **(Non-blocking)** With the ring memcpy, tokens the ep-rule skips (:5320) get fresh bytes where today they keep stale ones — unread either way, so benign; note it in the commit so nobody "fixes" it later.

### F5 — PLE gather side thread raising `m_flag_` — **NEEDS-CHANGES / REDESIGN REQUIRED** (blocking for F5)

The plan's core premise — "the graph's `wait_flag_ge(m_flag_, 1)` at :1499 is the consumer, a GPU spin, no host API" — is **true only in the ar_on() graph**. Verified at verify.cpp:1497-1500: `if (ar_on()) wait_flag_ge(m_flag_, 1, cs);` — the layer-1 PLE wait is inside the all-resident branch. Prod runs the **doorbell** graph (analysis's own ground truth), and there `m_flag_` has a different, load-bearing meaning:

- In `post()`, per step: `wait_flag_ge(m_flag_, ring, cs)` at :2460 (or the fused `copy_rows_from_mapped_after` at :2466) gates the copy of the **CPU's computed expert rows** out of `m_ymiss_` — ":2460 … the CPU's share is in the mapped rows". For k==0, ring=1: **layer 0's own waitCPU is `m_flag_ >= 1`** — the same value the plan's thread would raise early. The code's own comment at :3605-3608 confirms the PLE copy merely *piggybacks* on this wait (layer 1's `pre(1,0)` runs after layer 0's post in stream order).
- The host raises `*flag = want` at :3616 **after `pool()` returns** (rows complete) — that is the readiness signal. flagA is raised mid-dispatch by `publish_plan` (verify.cpp:3867-3871, via `P.publish` at expert_source.cpp:5249, *before* the CPU quantize/compute at :5316+), and flagB is raised by `fetch_dma`'s `cudaLaunchHostFunc` when the PCIe DMA lands (verify.cpp:3839-3865, issued at :5251 — concurrent with CPU compute). So the GPU routinely sits at `wait_flag_ge(m_flag_, ring)` **while the CPU rows are still being written** — that is the design.
- Consequence of the plan's design (thread raises `m_flag_ = 1` at stage_inputs; host skips its k==0 `*flag` write): the GPU passes layer-0 waitCPU the moment flagB(1) lands (mid-`pool`), and `copy_rows_from_mapped`/`remote_opt_->copy_rows` (:2463-2469) read `m_ymiss_` **mid-write**. That is silent numeric corruption — and worse, it is *schedule-dependent*: a qq run can pass while the race loses (see §2, gate critique).
- The plan's own words — "the GPU stalls at layer 1's wait instead" — are the error: in the doorbell graph the GPU stalls at **layer 0's CPU-rows wait**, whose early satisfaction is exactly the corruption.

Required redesign (keeps ~all the value, bit-exact by construction):
- Start the gather thread after `stage_inputs` returns (note: `stage_inputs` resets `*h_flag_ = 0` at :3161 — a thread started concurrently with it could have its raise wiped → layer-1/waitCPU hang; start it *after*, or write the flag only post-launch).
- The service loop **joins** the thread at k==0 after `pool()` returns and **before** `*flag = want` (:3616), which itself stays exactly as today. The gather then overlaps classify/publish/pool host work instead of running serially after it. Win bounded by `ms_pool` (typically the bulk of the window's host time) rather than the full gather — restate the band accordingly.
- The ar_on() variant (pre-raise block :3504-3516) may keep the plan's thread-raise design as-is — there `m_flag_>=1` gates only the PLE copy (:1499). If desired, gate the two variants separately inside `STRATA_PLE_ASYNC_GATHER`.
- The failure-path rule (failed gather → raise anyway + propagate err at the post-sync check :3661-3663) is good; keep it, and keep the NextLaunch-shaped RAII join (verify.cpp:3480-3494 verified as the pattern).

### F7ab — O_DIRECT + offset-sorted boot reads — **SOUND** (measurement discipline note)

Verified: O_DIRECT machinery exists (`open` with `O_DIRECT` + fallback-with-reason at expert_source.cpp:2521-2531; `open_prompt_direct` :2543-2552; `read_direct` :1796 as cited). The >6-boot interleaved distribution with ONE fresh fill per boot matches the 18bb lesson (second fills are conversation-cache-resident — journal :3462). Hypothesis framing is honest. Notes:
1. **(Non-blocking)** The fast/slow class bands and the 671/422 MB/s evidence predate the AUX_CPUS fix; the fill threads were among the parked-on-CPU-0 set. Run the distribution campaign on the adopted config and let the classes re-establish themselves; do not carry the old 432-455/352-379 bands forward as acceptance thresholds.
2. **(Non-blocking)** F6-composition is natural (pread into F6's pinned halves) but the alignment padding must stay inside the half — the plan already says this via `read_direct`'s windowing.

### F4 — speculative prefetch (helper weight + PCIe blob pre-stage) — **NEEDS-CHANGES** (design gaps; P3)

The plan-level refinement is **correct and important**: the analysis's step (a) as written is indeed non-constructible — `proxy_begin` consumes the current layer's post-attention activations (remote_experts.cpp:619-621), `x_{l+1}` does not exist at layer l's service, and a row computed from a predicted activation cannot be validated at consume time. Weight/blob pre-stage keyed `(layer, expert, pack offset)` with compute only at the real begin is the constructible form; the quality argument (predictions only change WHERE immutable bytes come from) is sound.

Verified: protocol extension room — feature bits 0-7 used (protocol.hpp:83-96), bit 8 (256) free; frame types 1-21 used (:101-169), 22/23 free; the extension-rule comments (:110-115, :132-139) say exactly what the plan claims; the client-side negotiation-gate pattern (`begin_q8k`'s `if (!q8k_) return kErrRequest;` at expert_helper_socket.cpp:631-633); the ONE-outstanding rule (:98-100 protocol + io_mtx_/`begin_outstanding_` at socket.cpp:607-625); `P.staging`/`P.ptr2` plan-time staging (expert_source.cpp:5230-5243, fetches < 64, `P.fetch` :5251).

Required changes (before P3):
1. **(Blocking for F4's design)** **Helper serve-thread serialization.** The helper is a serve-thread-per-socket frame loop (helper_main.cpp:25-31, 317-353 — serial per socket). A kPrefetch handler that synchronously reads up to m×~2.5 MB blobs **blocks that socket's next kBegin** — the prefetch would delay the demand path it is meant to hide. The handler must ack fast and stage asynchronously (helper-side staging thread), or the prefetch lane must ride the second socket (helper2_/stripe precedent the plan itself cites at remote_experts.cpp:757-796). The plan's "own stream, no lock held against a rows reply" covers the GPU stream, not the serve thread — amend.
2. **(Blocking for F4's design)** **Ack-drain point.** Draining the ack only "inside the NEXT `begin()`" misses the interleave where the helper is still staging when kBegin(l+1) is sent: the ack then arrives **before** kRows(l+1) on the socket, and the reply reader (`finish`) hits an unexpected frame type → protocol failure → tier disabled. The reply reader must tolerate/consume leading kPrefetchAck frames; specify that.
3. **(Non-blocking)** The ≤~150 MiB helper scratch is on the RTX3080's reserve — verify against the helper's actual arena-cap accounting (the plan cites the right test, `expert_helper_arena_cap_test.cpp`).
4. **(Non-blocking)** The predictor's `last_ids` from `h_ids_` is available per service (the loop reads it); fine. Keep `STRATA_PREFETCH_TOP` m∈{4,8,16} and the hit-rate kill criterion.

---

## 2. Validation-ladder critique (does each gate prove what it claims?)

| Gate | Verdict |
|---|---|
| F6 "verify_slot is the byte gate on every boot" | **Overstated** — one slot per boot (:9374-9383). The all-slots proof is the in-tree test; strengthen the engine gate with one `STRATA_VERIFY_COMPLEMENT=1` boot (generate.cpp:9409-9418: reads back and hashes **every** filled slot) gate-on vs gate-off, checksum vectors equal. That closes the wrong-slot/corruption class on real packs at the cost of one boot. |
| F2 frame-bytes equality test (fake helper, host-only) | **Strong** — producer change with byte-equal frames is the whole claim. Add the multi-window stale-seq case (window 2 replay) so finding #1 is caught in-tree, not on the lane. |
| qq teacher-forced KL 0.00000 ×3 | **Strong for byte-exactness, speed-independent** (18be's own note). The ×3 discipline correctly encodes 18aw's 4/4 lesson. Keep as the promotion gate for every wire/graph-affecting item. |
| F5 KL gate vs the m_flag_ race | **Would NOT reliably catch the design flaw** — the race is schedule-dependent (flagB must land mid-pool AND the copy must start before the rows finish). A passing qq run proves nothing about absence of the race. This is the review's clearest example of a gate that can pass a real regression; fix the design instead. |
| F1 draw-identical parity (≥1000 windows, T/mode matrix) | **Strong** — token- AND draw-identity over the mode matrix is the right strength for a sampler rewrite. Keep in ctest. |
| F7ab ≥6-boot distribution, one fresh fill/boot | **Correct design** (18bb lesson encoded). Re-base the class bands post-AUX_CPUS (see §7). |
| OFF-identical boots | Holds by construction for F1/F2/F3/F5/F6/F8 (static gates; F2 adds no kernel to the capture when OFF — verified the publish sites are capture-time code paths). Minor: F2's ring allocation should itself be gated to keep OFF RSS identical; F7c is intentionally always-on (log-only) — acceptable with the append-only check. |
| Rollback (lane_restore + env-off + pin untouched) | Consistent with the convention; pin `36ecbe98` untouched by everything reviewed; build_pin.sh guards verified in-script (gfx906 ON, no nvcc, libamdhip64, .hip_fatbin, MMQ line, sha256). |

---

## 3. Lifetime / liveness

- **#267-class**: F2's ring is Verifier-lifetime mapped memory beside `m_x_/m_ids_/m_seq_` (init alloc at the :769/:978 family; rebuilds re-capture the same pointers) — same class as the existing ring family; the wait reuses the NEVER-RANG/TIMEOUT/`release_gpu_waits` discipline (:3526-3561). The 18ba lesson ("waits released but GPU did not finish = stream wedged on real work") is correctly applied. F6/F8/F1 add no spin kernels. F4's staging slots are window-scoped; helper scratch is an LRU over immutable files — no invalidation. All fine.
- **F6 threads**: joined before the verify_slot checks; per-thread device scope needed (§1). The one-stream-per-cache + event-per-half staging reuse is correctly race-free (stream order for enqueue, event sync for host-side reuse).
- **F5 thread**: RAII join per window is the right shape — but the redesigned version must join *before* `*flag = want` at k==0, and start *after* `stage_inputs`' flag reset (:3161) or the raise can be wiped (hang, not corruption — the waitCPU would then be raised only by the host's own :3616 write; actually with the redesign there is no early raise at all, so this hazard disappears entirely — another point in favor).
- **Pinned budget**: 44 MiB (F6, transient) + 90 KiB (F2) + ~2 KiB (F8) + <1 KiB (F1) — negligible against the 46 GiB fill arena (18at). No cross-boot accumulation (F6 frees at close; rings are Verifier-lifetime).
- **Ring overflow under burst**: F2's depth-2 covers the worst in-window overlap by the flagB happens-before (verified §1-F2); kMaxTok=8 is enforced at both proxy_begin (:626) and the client (:594/:636), so the slot cannot overflow rows.

---

## 4. Phasing and sequencing

- **P0 independence**: functionally yes; textually, F1 and F8 both edit `record_window`/`run()` and share `m_hist_` — the plan's Day 2 (F8) → Day 3 (F1) order already serializes them correctly. F7c-first is right and now doubly so (§7).
- **Dependency edges** (F3→F2, F4a→F2+protocol, F4b→F7c): verified correct. F5-after-F2/F3 (merge-churn reason only) is fine — but **F5 must not be coded until its redesign is folded into the plan**.
- **Move earlier**: nothing. **Split**: F4's PCIe half (b) is separable and lower-risk than the helper half (a); if the post-P2 residual-wait measurement shrinks F4's helper half (the plan's own value-collapse note), consider landing (b) alone — the commit slicing already permits it (own gate recommended in the plan — agreed).
- **First-week sequence**: GPU-hours per day are comfortably under 4 h (Day 1 ~0.7 h, Day 2 ~2 h, Day 3 ~2.5 h, Day 4 ~2 h, Day 5 ~1.5 h including restore legs) — **credible**, with two caveats: (i) the current box-state instability (§7) may add re-boots; (ii) F2's Day-4 judges legs are uninterpretable until the warm-TG band is restored — swap in the KL boots, hold the judges.

---

## 5. Scope realism

- Byte math: F2 45 KiB/stage ✓; F6 22 MiB/cache ✓; F8 2 KiB ✓ (cap to verify); F1 ≤ ~550 B ✓.
- WT% estimates: the mechanisms remove measured host-ms from the serial chain — but the 123 ms/window denominator and the 2.7-5.3 ms quantize tax were measured **with the pinning fault live** (all boots since Nov 6). Treat all WT% as placeholders until re-based (§7). The PP/BT items are measurable now (fills/boundary were in normal bands in the collapsed state — AUXCPU1: fill 403.0, boundary 246.1).
- Effort ratings look right; F4's L/M stands even with the two amendments.

---

## 6. Fresh context — the AUX_CPUS result and re-baselining (the plan predates it)

What happened after the plan was written (journal 18bf follow-ups, today):
1. The pinning fault is real (64 `strata-stage` threads + watchdog/issue/fill parked on CPU 0 on ~96.5% of boots since Nov 6) and the fix (`STRATA_AUX_CPUS=3,5,7,9,11,13,15`) is **adopted into the prod config env** by operator decision.
2. **But warm TG did NOT recover**: with placement verified perfect, AUXCPU1 judged warm TG **19.5** — still in the collapsed band (18.2-23.7 on every boot since 16:21 today, vs the 26-34 in-band; last in-band boots were A1MMAP-1/2/3 at 15:49-16:08). The last host-side theory is falsified; a persistent post-16:21 box-state change (GPU under-load clocks pending; A1MMAP4 config differential in flight) is the live suspect.

Consequences for the plan:
- **Affected business cases**: every WT% target (F2 +2-4%, F3 +0.5-2%, F5 +0.5-3%, F8 +0.1-0.2%, F1 ~+1%) is calibrated on the 123 ms window measured under broken pinning — invalid denominator; the 2.7-5.3 ms quantize tax itself may shrink once CPU 0 is uncontended. F6/F7ab's fill-rate bands (422-671 MB/s; 432-455/352-379 PP classes) were also measured with the fill threads parked — the slow class is partially confounded; the page-cache hypothesis must be re-tested on the adopted config. F7c's value rises: it attributes both the fill classes and (via ms_wait/ms_pool/ms_host) the decode host chain on every boot.
- **Re-baselining order relative to P0**:
  1. Land F7c first (Day 1 as planned) — every subsequent boot then self-attributes.
  2. Proceed with P0 code, host tests, builds, and **qq KL gates** (speed-independent — 18be's explicit note) — do not wait.
  3. Hold/defer all **tg80k judge legs and WT% acceptance numbers** (F2 Day-4/F3 Day-5 judges, F5's, F4's) until the post-16:21 collapse is attributed (under-load clocks / A1MMAP4) and the warm-TG band is restored on the adopted config; then re-base once and re-state the targets.
  4. F6/F7ab's boot-side measurements may run sooner (fills/boundary were in normal bands in the collapsed state), but on the adopted AUX_CPUS config, and the class bands must re-establish themselves in the distribution rather than being carried from pre-fix data.
- Lane hygiene while re-baselining: 18be's lesson (local tool CPU contention poisons TG) applies to the integrator's own tool phases — keep planner/analyzer work off the box during judge legs.

---

## 7. Prioritized change list for the planner

1. **F5 redesign (blocking, P2)**: drop the thread-raises-`m_flag_` design for the doorbell graph (CPU-rows contract at verify.cpp:2460/:2466; flagB lands mid-pool via `fetch_dma` :3839-3865 → mid-write copy). New design: gather thread started after `stage_inputs`, joined after `pool()` at k==0 before the unchanged `*flag = want` (:3616). Keep the thread-raise variant only for ar_on(). Re-state the win band (bounded by `ms_pool`). Note that the KL gate cannot certify the old design's absence-of-race.
2. **F2 amendments (blocking for its boots, P1)**: add `m_xq_seq_` reset in `stage_inputs` beside `h_seq_` (:3160); extend `xq_triple_parity` with the QFUSE-producer leg (gr_parity.cpp:416 is the in-tree fourth-leg evidence; do NOT gate ring use on `!q8_ffn` — QFUSE is default-on at arch_defaults.cpp:86); gate the tail publish/ring consumption on the helper tier being present; add a multi-window (stale-seq) case to the frame-equality test.
3. **F4 amendments (blocking for its design, P3)**: kPrefetch handler must not block the helper's serve thread (async staging or stripe-socket lane — helper_main.cpp:25-31/317-353 is serial per socket); the ack drain must live in the reply reader (`finish`), not only `begin()`, for the ack-before-kRows interleave.
4. **Re-baseline discipline (process)**: add a Day-0/Day-1 step — re-establish the warm-TG band and fill-rate bands on the adopted AUX_CPUS config once the post-16:21 box state is attributed; re-state all WT%/BT% targets; make tg80k judge legs conditional on "band restored". F6/F7ab measurements run on the adopted config with fresh class bands.
5. **F6 DoD re-state (non-blocking)**: per-phase targets from F7c fields (H2D-phase rate ≥2×; end-to-end wall claim caveated as read-bound on cold boots); use `STRATA_VERIFY_COMPLEMENT` (:9409) for an all-slots engine-level byte gate instead of leaning on single-slot `verify_slot`; note the per-thread `OnDevice` requirement.
6. **F8 text fixes (non-blocking)**: "previous window" framing → "after draft, mapped stage"; verify `kHistMax` cap vs actual `penalty_last_n` ceiling.
7. **F1 arming condition (non-blocking)**: include penalized-greedy (`hist_d_ != nullptr` with greedy, :3687) in the in-graph path or document its exclusion.

The plan's structure — default-OFF gates, commit slicing, lane discipline, promotion as an operator decision, pin `36ecbe98` untouched — is consistent with the campaign conventions throughout; with the changes above (1-4 before their respective phases), P0 may start.

*End of review.*

---

## Re-review (round 2, 2026-11-15)

Re-reviewed the amended plan (451 → 480 lines; amendment markers walked section by section; the three load-bearing NEW citations spot-checked in the worktree at HEAD `84fc595dd9`, read-only). Fresh context since round 1: post-reboot PRODCHECK2 read warm TG **22.1** — the collapse survives a full reboot; pinning fix confirmed live; PCIe link-width under load is the open suspect alongside clocks.

### Blocking findings — resolution status

**1. F5 redesign — RESOLVED.**
- The amended section (plan :293-332) drops the early-raise **structurally**: doorbell path keeps every flag write byte-for-byte today's (`*flag = want` at :3616 unchanged, sole raiser = the service loop); the thread only derives rows + `gather_batch` and is **joined after `pool()` returns and before the raise**; started after `stage_inputs` returns (removing the raise-wiped-by-reset hazard entirely since the thread never writes a flag); failure path = err recorded, checked at :3661-3663, no flag write, no hang. This is exactly the redesign required.
- The "why the original was dropped" note (:295) states the mechanism correctly and with the right anchors: `:1497-1500` guards the PLE wait with `if (ar_on())`; `m_flag_ >= ring` is the CPU-rows contract (`:2460/:2466`); flagA mid-dispatch from `publish_plan` (`:3867-3871` via `P.publish` at expert_source.cpp:5249); flagB mid-pool from `fetch_dma`'s host func (`:3839-3865`) — all verified in round 1 and correctly transcribed. It also correctly concedes the KL gate could not certify the old design (schedule-dependent race) — the fix is structural, not tested-in.
- The ar_on() thread-raise variant is retained only as an optional, separately-switchable non-prod variant (:311) — matches the round-1 recommendation.
- Win honestly restated as bounded by the overlapped `ms_pool` slice (:313), and the DoD adds a **join-ordering test** plus the failure-path test (:321) — the one-raiser rule as an asserted behavior.
- New anchors spot-checked: `ple_rows` derivation at verify.cpp:**3377-3386** ✓ (local `prev` copy only — `ss.ple_prev` is not written there, so hoisting to the thread is race-free; `prefetch_rows` warming at :3384 moves with it, harmless/earlier); the ar pre-launch raise block at verify.cpp:**3393-3403** ✓ (gather :3396, `h_flag_ = 1` at :3401, `flag_raised` :3402 — the block the amendment labels "pre-launch" as distinct from the :3504-3516 post-launch block; both exist as described).

**2. F2 stale-seq + QFUSE + gating — RESOLVED.**
- **Stale-seq**: plan :226 now specifies the exact `stage_inputs` reset (`m_xq_seq_[0]/[1] = 0` beside `h_seq_` at verify.cpp:3160-3163) with the correct rationale (capture-baked ring values restart at 1; stale values would pass `>= want` instantly) and the safety argument (previous graph fully synced at :3661). Test case (v) (:245) covers a window-2 replay in-tree. Risks table (:257) and DoD (:261) updated to match.
- **QFUSE**: plan :228 states ring use is NOT gated on `!q8_ffn`, cites the right facts (STRATA_QFUSE default 1 at arch_defaults.cpp:86; `q8_ffn` at verify.cpp:2207), and rests correctness on the parity chain; :246 makes the QFUSE-producer leg of `xq_triple_parity` **mandatory**, citing gr_parity.cpp:416-417 (verified in round 1) for QFUSE==native, giving QFUSE==native==rows==cpu. Risks (:258) add the per-layer fallback if gr_parity ever fails. Exactly what was required.
- **Gating**: the tail publish now requires the helper tier to be configured (:227) and the ring allocation itself is gate-conditional so OFF boots keep identical RSS (:226). Both accepted.

**3. F4 serve-thread serialization + ack-drain — RESOLVED.**
- **Serve thread**: plan :383 names the constraint with the verified anchors (helper_main.cpp serve-thread-per-socket serial frame loop) and makes the fix structural: the kPrefetch handler **acks immediately** (n_queued) and stages on a helper-side staging thread, OR the prefetch rides the second socket (stripe channel-B precedent) — "pick one at implementation; the socket test covers both orders." Ack semantics changed from n_staged to n_queued accordingly (:380). Correct.
- **Ack drain**: plan :387 moves the correctness point to the **reply reader** — `finish()` "tolerates and consumes any number of LEADING kPrefetchAck frames under `io_mtx_` before the expected kRows," with the begin() peek demoted to a fast path. Spot-checked: `ExpertHelperClient::finish` is at expert_helper_socket.cpp:**710**, its whole read under `io_mtx_` (:716) with a single `read_frame` at :723 — exactly where a leading ack would otherwise be a protocol failure, so the amendment targets the right line. New test (ii-b) (:406) exercises the ack-between-kBegin-and-kRows interleave on a fake helper; the commit message for the client path (:400) names the finish() drain.
- Remaining F4 content (predictor, PCIe pre-stage, thrash bounds, descope rule) unchanged from round 1 and still sound.

### Non-blocking items — resolution status

- **F6** — RESOLVED: `STRATA_VERIFY_COMPLEMENT` all-slots boot added to the ladder (c) and DoD (checksums equal gate-on vs gate-off); `verify_slot`'s one-slot-per-boot nature stated exactly (:93, :102); per-phase DoD (H2D-phase ≥2×, end-to-end caveated read-bound with the 671 MB/s ≈ 14.3 s/14.8 s math restated); per-thread `OnDevice` requirement in the change list (:91) with commit-message instruction; baseline-confound risk bullet (:111) tying to Re-baselining.
- **F8** — RESOLVED: reframed to "after THIS window's draft, before launch" with the round-1 reasoning transcribed (:124); window-0 special case eliminated by design (:139); `kHistMax` marked asserted-not-verified with an implementation-time check (:140).
- **F3** — RESOLVED: destination corrected to `d.nact_multi + t*kNativeActBytes` with the Q2/non-native destinations distinguished (:268); stride fast path + per-row/fallback; test target updated to `nact_multi` (:279); ep-skip fresh-bytes note kept for posterity (:286).
- **F1 penalized-greedy** — RESOLVED: armed by design with the correct code fact (`hist_d_ != nullptr` fires at :3687 even when greedy; temperature-0 deterministic) (:156); parity matrix extended with the penalized-greedy mode (:167); risks note that excluding it later would be a documented value loss (:174).
- **Re-baselining section** — RESOLVED and, critically, **mechanism-independent**: F7c first (mandatory), qq KL gates proceed now (speed-independent), ALL tg80k judge legs and WT%/BT% acceptance numbers held for *confirmed warm-TG band restoration on the adopted config*, fresh class bands for F6/F7ab, lane hygiene. The new PRODCHECK2 datum (22.1 post-reboot — collapse survives reboot; PCIe link-width the open suspect) changes nothing in the rules: they gate on band restoration, not on any particular suspect being confirmed. Cosmetic only: the suspect list in the section preamble (:68 — "GPU under-load clocks pending; the A1MMAP4 config differential... a post-reboot re-measurement is in flight") predates PRODCHECK2 and could add the PCIe-link-width suspect and the reboot-survival datum; the journal, not the plan, is the right home for that update.

### Internal consistency

- Master table (:33-41): F6 2 boots/complement gate/tg80k held; F7c "FIRST code item — mandatory"; F2/F3/F5 judges HELD; F5's band restated ms_pool-bounded; all gains marked placeholder. ✓
- Phasing (:57, :61): F5 coded only from the redesigned section; measurement edges (judge legs → band restoration; F7c → first) added as a distinct edge class. ✓
- First week (:436-462): Day-1 step 0 re-baselining checkpoint; F7c before everything; Day-2 complement boot; Day-4/Day-5 judge legs replaced by additional qq passes while held; F5 code-only from the amended section. GPU-hour estimates updated coherently (~0.75-1.5 h/day + held legs deferred, not deleted). ✓
- DoD summary table (:468-478): every row carries its amendment (complement checksums, stale-seq + QFUSE legs, nact_multi stride, join-ordering tests, fresh class bands, ack-before-kRows). ✓
- **One residual wording nit (non-blocking)**: F1's ladder (c) (:168 — "Judges leg optional … expect ~+0.5-1%") and its DoD row (:179 — "a sampled tg leg shows the second sync gone") do not carry the explicit "held until band restoration" marker that F2/F3/F5/F6 carry. The blanket Re-baselining rule (:74, "ALL tg80k judge legs … WAIT") and Day 3 (:448, "tg80k held") govern, so there is no substantive contradiction — but the two F1 lines should carry the marker for textual consistency when the plan is next touched. The "+0.5-1%" there is also an unrebased placeholder like the others.

### Final verdict

**APPROVE for P0 start** (upgraded from APPROVE-WITH-CHANGES).

All three blocking findings are resolved structurally in the amended text — not merely re-worded — and the four blocking-class constraints land with verified anchors and in-tree tests that would catch their respective regressions. The non-blocking items are all resolved. The plan's own remaining gate is operational, not textual: judge legs and speed-based acceptance numbers stay held until the warm-TG band is restored on the adopted AUX_CPUS config (per PRODCHECK2, still collapsed at 22.1 post-reboot, PCIe link-width the open suspect) — the plan encodes this correctly and mechanism-independently. P0 (F7c first, then F6/F8/F1) may start; F5 is to be coded only from its amended section; F2's lane boots follow its amended ladder; F4 proceeds to P3 per its amended design.

*End of re-review.*

