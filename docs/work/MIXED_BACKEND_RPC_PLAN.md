# Strata mixed-backend RPC worker plan — ROCm coordinator + CUDA execution worker

Status: PLAN (researched, source-grounded; no implementation authorized by this document alone).
Date: 2026-10-30. Authors: parent (R1/R2 research: `rpc-upstream-research.md`, `rpc-strata-source-map.md`; charter `rpc-plan-brief.md`).

## 0. Executive decision

**Route: extend the existing out-of-process CUDA expert helper into an all-phase expert execution worker over the existing unix-socket protocol.** The HIP engine process (both Radeon VIIs) stays the single coordinator: it owns dense weights, KV, draft/speculation, sampling, and its own expert tiers; the CUDA worker process (RTX 3080) owns a **disjoint, all-layer, byte-budgeted** set of expert blobs and executes them for **prefill, verification and decode** with the SAME proven kernels the engine uses. No new math anywhere.

Why this route (researched, not preference):
- **Proven-code reuse is already real**: one grouped kernel — `native_expert_grouped` (src/kernels/cuda/iq_kernels.cu:5073+) — today serves engine verification (verify.cpp:2127), the in-process remote tier (remote_experts.cpp:584), the prefill peer offload (peer_experts.cpp:299), and the sidecar helper decode+prompt paths (helper_main.cpp:288/442/569). The worker adds transport reach, not math.
- **ggml's own RPC is unreachable**: Strata links ggml for quant FORMATS only; the sole graph use is a test (R2 §1). Adopting upstream RPC means rewriting the engine as ggml-scheduled graphs — the opposite of the reuse requirement.
- **Single-process dynamic loading (GGML_BACKEND_DL-style) is rejected**: it puts both vendor runtimes in one process (undocumented pair, H4 risk), surrenders expert-tier orchestration to a scheduler that does not know our model, and the user-reported slowness has host-staged boundary copies as its sourced mechanism (R1 §4 H1) — a cost our design keeps explicit and budgeted instead of accidental.
- **Two full Strata servers are rejected**: duplicated weights/KV/sampling; no net capacity.

Current vs target topology:

```
CURRENT (live mixed variant)                TARGET (this plan)
─────────────────────────                   ─────────────────
HIP engine proc (2x Radeon VII)             HIP engine proc (2x Radeon VII)
 ├ dense/KV/draft/sampling/spec             ├ unchanged owner of dense/KV/draft/
 ├ xcache + 2 stage expert tiers            │   sampling/spec + its expert tiers
 └ helper client ── unix socket             └ worker client ── unix socket (same protocol,
     CUDA helper proc (3080)                    extended: prefill claims)
      ├ expert blobs, layers 32..47 only     CUDA worker proc (3080)
      ├ magic 3400-slot cap                   ├ expert blobs, ALL layers, ranked
      ├ decode rows (+verify)                 ├ byte-budgeted fill (--expert-helper-mib)
      └ NO prefill                            ├ decode rows + verification rows
                                              └ NEW: prefill whole-expert claims
```

Phase dataflow (target): per decode step and per verification pass, worker-owned (layer, expert) rows travel as today (q8_1 blocks in, fp16/rows out over pinned buffers). Per prefill window (8192 tokens × layer), the engine sends the claim request (expert → token-count table), the worker replies with the claim plan (whole experts it owns, its rows), computes them with the SAME grouped prompt launch (helper_main.cpp:569 path from P1), returns rows; the engine computes the complement locally and combines exactly once.

Out of scope (named boundaries, not omissions): dense/KV/draft placement on the worker (the residual chain is serial and in-process — the worker is the expert tier, which is where the model bytes are); SYCL (unconfigured — every milestone states NOT RUN, never PASS); ggml dependency updates (vendored b11062 stays; no downloads); Vulkan; multi-host/network exposure (loopback+unix only, matching upstream's own security posture R1 §1).

## 1. Capability matrix (what proven code runs where)

| Capability | Coordinator (HIP engine) | Worker (CUDA proc) | Proven entry point | New work |
|---|---|---|---|---|
| Expert grouped matmul (gate/up/swiglu/down; v2/v2k, fused) | yes (local MMQ + native) | yes — same kernel | iq_kernels.cu:5073+ | none (transport only) |
| MMQ tab family (Q2_0/IQ2_XXS/…/NVFP4 w4a4/w4a8) | yes | buildable same tree | moe_mmq.cu:212, moe_mmq_tab.hpp:18-21 | none |
| Decode rows → helper | today | today | helper_main.cpp:442 | none |
| Verification rows → helper | today (helper serves verify) | today | existing client | coverage test after all-layer fill (M3) |
| Prefill rows (P1 wire/client/tiled fallback) | client implemented, CPU-tested (139+46 checks) | prompt launch implemented (:478/569) | expert_helper_socket.hpp:55-67 | engine-side dispatch/combine (M2) |
| Expert fill planning (all-layer, byte budget, disjoint) | inherited M1, unaccepted | consumes plan | expert_helper_candidates.{hpp,cpp} | M0 reconciliation gates |
| q8_1/fp16 block formats | yes | yes — pinned contract | expert_helper_socket.cpp:8 | none |

Compatibility limits: the worker's kernels are the engine's kernels compiled in the CUDA tree (same source, STRATA_NATIVE_EXPERTS/MMQ options as cap1 builds); any kernel-level divergence between trees is a build-config defect the M0/M2 build gates check (identical source, both trees, rc=0). Quant formats: IQ3_XXS/Q4_0 model blobs load through the existing pack path in both processes (worker receives blobs via the existing fill). Speculative path (dflash drafts/verify) stays on the coordinator; worker only sees expert row requests — it never needs model-level knowledge beyond geometry+blobs.

## 2. Capacity accounting (claim and its honest limit)

Baseline (LOG evidence, live mixed run, capacity-first-design.md §1): VII#0 6069 slots / 9.12 GiB (L0-31), VII#1 4780 / 8.94 GiB (L32-47), 3080 3400 / 6.37 GiB (L32-47 only, the magic 3400 — the cap, not the card's ~3590-slot budget, binds); unique union 14249/24576 pairs (58%); host pinned expert arena ~40 GiB; engine Pss 46.73 GiB of 62 GiB.

Target claim (to be measured, not promised): all-layer eligibility + exact byte budget raises the 3080's owned set toward its budget (≈3590+ slots at current reserve; AUTO explores to the card's live free-VRAM check) and removes the L0-31 exclusion — union ceiling ≈ 6069+4780+~3590 ≈ 14439+ pairs → ~59-75% depending on profile overlap with AMD tiers; overlap is prevented at fill (owner mask) and at runtime (disjoint adapt default-ON, M1). Evidence: per-device allocation census + unique-union set computation from FillReports + slot tables (runnable census in M4).

The honest limit — GPU residency vs host RAM: the host arena is the fallback source of truth; worker fill does NOT release host pages automatically, and deleting fallback copies would trade recovery for RAM (rejected: fallback correctness is a frozen constraint). Therefore "less RAM offload" is claimed only through the measurable path the cache exists for: higher unique VRAM residency → higher hit rate → less host expert traffic per token (the measured 663.6 MB/token CPU expert cost, expert_cache.hpp:3-7). A larger-model claim additionally requires the constrained-budget comparison (M4: same assets, larger served context/coverage or reduced arena trim only if a mapped/faulted pack option is engaged — otherwise explicitly UNPROVEN). No transparent-unified-8GB promise; the worker's ~6.4 GiB of blobs is the additive quantity.

## 3. Placement and scheduling

- Fill-time placement: global ranked profile (`profile_all`, pre-split) minus owner mask (primary xcache ∪ every stage cache) minus Hadamard-folded layers; longest rank prefix fitting the exact aligned byte budget (`--expert-helper-mib`; explicit `--expert-helper-slots` = cap only). All M1 machinery, reconciled by M0.
- Runtime routing: (layer, expert) owned by worker → worker computes for decode/verify/prefill; else local. Per phase: decode per-token rows (existing); verification rows (existing client); prefill per-window whole-expert claims — eligibility rule from GateA (whole MMQ group beats streaming when `cnt[e]*2*row_bytes < blob_bytes(l)`; M2 verifies the exact inequality against the walked groups before enabling, per the parent's acceptance note) with the determinism of P1's `select_prompt_rows` (cnt-DESC, ASC ties, whole-expert claims, cap-bounded skips).
- Concurrency: one client, bounded outstanding transactions (existing serialization); prefill claims are issued per (window, layer) ahead of the local complement, joined at the existing combine mark — the in-process peer seam's enqueue/join shape (prefill.cpp:778, :2061-2228 seams) transposed to the socket. Decode stays serial-shaped (the residual chain is serial; the worker overlaps its rows with the coordinator's local experts within a step — the existing production shape).
- KV/sampling/speculation: coordinator-only (named boundary §0).

## 4. Wire/control/data contract

Base: the existing protocol — magic/version/seq/req_id framing (expert_helper_protocol.hpp:107-129), hello(geometry+claimed bitmap)→ack, fill→FillReport, decode row requests, named geometry refusals, deadline+lat semantics. P1 already added the prompt-claim request/response shapes (CPU-tested with the tiled fallback). Changes needed (all additive, negotiation-gated):
1. Hello: worker advertises all-layer window + budget mode (fields exist post-M1; version pin at hello, old peers refused by version as today).
2. Prompt-claim exec: enable engine→worker dispatch of the P1 plan (M2). Row IDs/offsets/strides ride the existing packed formats; q8_1 block layout is the pinned contract (:8).
3. Worker restart epoch: new hello = new epoch; in-flight replies from the old epoch are dropped by (epoch, req_id) — stale-reply protection the current latch handles for decode and M2 extends to prefill claims.
4. Backpressure/deadlines: existing 100 ms decode deadline + window-level prefill deadline; partial results are never combined — a missed deadline falls back to bounded local recompute of that layer's claimed rows (exactly once; conservation test M2).
Imported upstream patterns (R1 §1): GRAPH_RECOMPUTE-style plan reuse when a prefill window repeats its claim shape is OPTIONAL (deferred — measure first); SET_TENSOR_HASH-style blob dedup deferred (weights cross once at fill today). Anti-pattern avoided: REAL capability negotiation — the worker's geometry refusals, never upstream's unconditional `supports_op=true` TODO.

## 5. Performance model (components, honest labels)

Per prefill window (8192 tokens, one layer): claim RTT (unix socket, µs-ms) + upload q8 rows for claimed experts' tokens (`rows_peer × n_embd` bytes — the :778 rationale: rows, not T×N) + worker compute (grouped kernel, proven) + download rows (`rows_peer × n_ff`) + combine. Versus local-only: saves local MMQ compute for those rows AND the host-side expert-byte traffic for them (the dominant measured cost), pays socket+2×PCIe crossings. MEASURED inputs today: none of the socket/PCIe RTT components for prompt claims (UNKNOWN — the P0 splitserve profiling measured CPU dispatch ~2.43 ms/token and stage waits, not the socket arm). Therefore M2 ships with instrumentation and M4's paired OFF/ON run is the decisive measurement; a bounded pre-experiment (single-window claim, one layer, timing breakdown) gates wide enablement with an explicit decision rule: prefill dispatch stays ON only if window-level wall time improves ≥5% on the long-prompt arm, else it remains opt-in-off with the numbers recorded (no silent regression, no invented speedup).
Decode/verify deltas: existing production shape; all-layer fill increases per-step owned rows (more socket rows vs less host traffic — the 100 ms deadline and latch bound the risk; M4 measures tok/s OFF/ON same-day).
Transfer path: pinned host rings (existing up_ring/#1712 staging) → unix socket → worker pinned → H2D. No cross-vendor P2P/IPC assumptions (R1 §1: even upstream's cross-server path is host-staged). RPC cannot remove physical transfers — only choose what crosses (rows, not weights, not activations-for-unowned-experts).

## 6. Numerical contract

- Provenance: worker math = engine kernels (§1); the only new numerical surfaces are claim selection (deterministic, testable), packing layout identity, and combine ordering — all CPU-testable.
- Band (declaration v2, measured 2026-10-31): the band stays unrelaxed — p99.9 ≤ 5e-3, max ≤ 2e-2 on the absolute row error of the fix9 row-error harness. Re-measured on the full 128-fixture capture (ok=128 fail=0) via the candidate helper + p2_measure harness, layers [32,47], 132,749 rows: absolute p50 2.80e-4, p90 4.75e-4, p99 7.61e-4, **p99.9 1.137e-3 PASS** (4.4× margin), **max 3.669e-3 PASS** (5.4× margin), mean 3.03e-4; relative column (reported, not the band metric): p99.9 5.83e-3, max 1.44e-2. This supersedes the v1 open failure (p99.9 5.2299e-3, partial data). Measured caveats: the measure-budget helper held 1536 of 7286 requested pairs resident (3.11 GiB, slots 3400/reserve 4000); layers 42–47 stayed fully local (empty plans), so the rows measure every layer where the helper actually participated. The NMSE-method cross-check (R1 §5, op-level same-input NMSE vs the local arm) remains OPEN: the p2_measure harness has no NMSE mode and the test-backend-ops method source is not present in this tree — declared open, not skipped; thresholds were never transplanted.
- The two known deviation families: (a) group recomposition — eliminated by whole-expert claims (a group is computed entirely on one device); (b) producer difference (device-made vs locally-made q8_1) — M2 pins byte-layout identity between `m.Xq`-style staging and what `native_expert_grouped` consumes before enabling (the GateA condition); residual = product-kernel difference only, measured not assumed.
- Determinism: same-arm temperature-0 SHA reproducibility (one engine+device set+code path) is the determinism claim; cross-backend output differences are expected, characterized, never assumed identical (upstream itself validates with tolerances, not bit-identity — R1 §5). No NaN/Inf/OOB; final-logits and task-output checks in the deep-infill probe.

## 7. Failure and recovery (authority at every state)

Worker dead mid-prefill window: deadline expires → claimed rows recomputed locally exactly once (conservation invariant: row appears in exactly one combine contribution) → window completes → subsequent windows use local-only until re-hello. Mid-decode: existing latch+fallback (production-proven, incl. SIGKILL evidence on the old path — re-verified on the NEW prefill path in M5). Worker restart: new epoch; engine either re-fills (fill request idempotent, blobs re-uploaded once) or continues CPU/AMD-only — coordinator authority, worker never assumes. No silent zeros: every refusal named (existing policy); no duplicate combine (conservation tests); no stale ownership (epoch + owner-mask refresh at re-hello). Host arena never deleted → fallback always memory-backed (the §2 honesty tradeoff, kept).

## 8. Milestones (serial, one specialist at a time when subagents are re-allowed; parent-executed otherwise)

| ID | Scope | Files/interfaces | Tests/gates |
|---|---|---|---|
| M0 | Reconcile inherited M1: verify the three findings addressed (geometry/alignment guards + UBSan evidence; request-local disable vs retained-residency transition test; truthful refusal wording), rerun CPU suites, rebuild cap1-{hip,cuda} | src/core/expert_helper_candidates.*, generate.cpp, tests/core/expert_helper_candidates_test.cpp | 97-check suite + socket/fallback/microbench green both trees; guards evidenced; parent accepts or sends bounded corrections (attempt-limited) |
| M1 | Accept all-layer byte-budget disjoint fill (M0 green ⇒ accept) | — | census hooks live-log union/overlap |
| M2 | Worker prefill dispatch (the unfinished P2 seam, reshaped to whole-expert claims): engine-side claim/compute/combine at the prefill.cpp seam; whole-group eligibility rule verified in code before enable; STRATA_PREFILL_HELPER=1 opt-in, default OFF; instrumentation for §5 | src/prefill/prefill.cpp (dispatch/combine), src/core/expert_helper_socket.cpp (client), include/strata/core/expert_helper_protocol.hpp (epoch field), tools/mixed_gpu/sidecar/helper_main.cpp (only if epoch wiring demands) | CPU: claim determinism, row conservation, combine-exactly-once, epoch/stale-reply, tiled fallback; new build dirs rpcw-{hip,cuda}; helper-OFF byte-identical argv/env/log check |
| M3 | Verification coverage after all-layer fill: row-conservation + nonzero worker participation test | tests, harness | gap found ⇒ bounded fix, else evidence only |
| M4 | Capacity/perf evidence (parent-owned hardware windows): census, miss-rate/host-RAM, paired same-day OFF/ON (short + long arms, matched everything), §5 decision rule | tools/mixed_gpu/ census + timing adapters | acceptance budget §9 |
| M5 | Deep-infill ~80k+5k probe, temp-0 SHAs (same-arm vs cross-arm), NEW-path mid-prefill kill + recovery, declaration v2 | probe script (reusable), evidence dirs | all failures retained; no promotion claims without numbers |

Rollback at every point: canonical launcher untouched; opt-ins default OFF; isolated build dirs (cap1-*, rpcw-*); live pinned binaries never written; production stays on the verified mixed variant until a candidate earns a switch.

## 9. Evidence budget (runnable protocol)

1. CPU contract tests + ASAN/UBSan on protocol/planner/claim/combine (M0/M2, both trees).
2. Numerics: fix9 harness re-run on the M2 path (same fixtures/metric) + NMSE-method cross-check column; band verdict recorded PASS/FAIL without relaxation.
3. Census: per-device slot/byte tables + unique-union computation from hello/FillReports; overlap = 0 asserted.
4. Capacity: constrained-budget comparison with existing assets — served coverage/context and host RSS/commit/Pss OFF vs ON; larger-model claim only if an existing asset demonstrates it, else explicitly UNPROVEN.
5. Paired speed: same-day, same model/config/warm-state/requests/token counts: TTFT, prompt tok/s, verify+decode tok/s, end-to-end wall; ≥3 repetitions, median+spread reported; PCIe/socket/queue breakdown from M2 instrumentation.
6. All-phase participation: nonzero worker rows in prefill+verify+decode windows; conservation (no double compute) proven by accounting equality.
7. Fault: mid-prefill kill ×2 (early/late), worker restart re-fill, decode-kill regression — clean requests after each.
8. Deep-infill: ~80k actual prompt tokens + ~5k generation on the final configuration; timings, memory snapshots, temp-0 request/content/message SHAs with same-arm reproducibility shown separately from cross-arm differences.

## 10. Risks and decision points

- R1 §4 H1 cost is real: if the §5 decision rule fails at M4, prefill stays AMD-local (opt-in-off) — recorded as a measured limit, NOT as project completion (the AMD-only endpoint is a blocker to report, per standing decision).
- The p99.9 band may still fail after whole-group + layout pinning: then the deviation is product-kernel-level; next step is a bounded kernel-parity fix on the grouped kernel inputs (NOT a tolerance change).
- 3080 usable headroom is bounded by its own live memory check (~6.4 GiB blob budget today); AUTO explores but the card decides (FillReport authoritative).
- The unaccepted-M1 findings, if M0 disproves the handoff's claims, revert to bounded corrections before anything downstream consumes the planner.

## 11. M6 — the per-layer pipeline (the async claim lane)

User-approved 2026-10-30 ("we def want the pipeline ... first do your proposals then pipeline"). Scope: engine-side
threading of the existing claim cycle. Zero protocol, header, helper or wire changes; zero new kernels.

### The dependency law (what can and cannot overlap)

Routing for layer N+1 is computed from layer N+1's post-attention state, which needs combine N. So a claim for
N+1 cannot be SELECTED before combine N, ever. What CAN leave the walk's critical path is everything between
selection and the combine's read of the staged rows:

- (a) prefill.cpp:4164 `cudaStreamSynchronize(m.hp_cs)` — the walk thread sleeps until the activation image's
  D2H completes (it is gated on an m.cs event, so the wait is the current m.cs kernel backlog).
- (b) :4165 `begin_prompt` — a blocking unix-socket write of plan + image (~2.6 MB at shallow 256-row claims;
  ~20 MB at deep 222-token claims, where the M4c short-arm win lives), 3000 ms deadline.
- (c) :4997 `finish_prompt` — a blocking reply read (usually already answered; the helper idles waiting for us).
- (d) :5003-5005 the per-row gather and the hp_stage H2D chain.

The walk thread is the kernel-queue issuer for the whole engine; every millisecond it sleeps risks GPU bubbles
when the queue drains. Measured context: the 3080's effective pull is ~0.5 GB/s (nvtop flat) — it is
orchestration-bound, not bus- or compute-bound (M4c, RTX 3080 + 2×Radeon VII, 2026-10-30).

### The design

One per-model claim worker (std::thread + condition variable + ONE claim slot; lazily created at the first
claim, joined at walk end and at model teardown). Exactly one claim is in flight at any time — by the dependency
law above, not by policy.

Per claim N:
1. Walk thread (unchanged): selection (:3956-3996 — the ExpertSource reads, `holds()`, `blob_stable()`,
   `select_prompt_rows` — stay on the walk thread per the :4015 single-writer rule), hp_pos/hp_stage staging
   setup, then record E1 on m.cs (image final), publish the slot {layer, plan, pos, claims, r0h, fmt}, ring the
   worker, and CONTINUE queueing this layer's own-expert kernels.
2. Worker: hp_cs waits E1 → enqueue the per-row D2H → sync hp_cs (the worker sleeps, not the walk) →
   `begin_prompt` → `finish_prompt` → validate → gather pair-indexed rows into hp_stage (block order, the M3
   `-r0h` indexing) → enqueue the hp_stage H2D on hp_cs → record E2 on hp_cs → publish done.
3. Walk thread at combine (:4986): read the slot's outcome latch (a condvar wait in the worst case; normally
   already done), `cudaStreamWaitEvent(m.cs, E2)`, then queue the apply/combine kernels. The walk thread never
   sleeps on the socket or the copy tier.

Fault semantics: worker `begin_prompt` failure latches hp_sent=false; `finish_prompt` failure latches
ok_rows=false. Both route to the SAME combine-time CPU fallback that the M5 window hardware-verified on
2026-10-30 (HTTP 200 after a mid-prefill SIGKILL, one engine boot, MAXT-chunked jobs). The fallback's
quantize reads ext_x, which is safe: the next claim's D2H cannot start before the next selection, which is
after this combine. Channel death inside the client latches it unusable; no further claims.

Determinism fold-in (from the M5 det evidence): the two cold ON runs diverged because `helper_usable()` is
evaluated per layer while the helper's connect/hello lands asynchronously — run 1 claimed layers 0-2 of chunk
1, run 2 did not (386 vs 348 claim events, zero fallbacks in both). The walk now resolves helper usability
ONCE at walk entry into a latched boolean; every chunk sees the same value. This makes participation
deterministic; output determinism across arms remains governed by the numerics band, not bit equality.

Gating: STRATA_PREFILL_HELPER_PIPE=1, default OFF — the default path stays byte-identical (contribution rule).

### Validation plan

1. Rebuild both trees -j11; CPU suites green (client, protocol and helper untouched: socket, fallback,
   candidates, ownership + the cuda-side helper set).
2. One user-authorized A/B window, M4c methodology: PIPE off/on × {short 222, med 2443, long 10443, xl 78603}
   ×3, plus the OFF arm's SHA determinism pair. Success = long arm ≥ +5% median vs ON-legacy to flip the
   opt-in verdict; short arm not worse than the M4c +20.3%; OFF byte-identical.
3. Claim-set logs (STRATA_DBG_PROMPT_HELPER) captured both arms — identical participation per arm is a
   precondition for comparing SHAs.

### Risks

- Worker/walk races on the client: all ExpertHelperClient calls move to the worker (single-threaded use at
  all times; `holds()` reads hello-time state, immutable).
- hp_stage/hp_rows lifetime: per-model members, one slot, serial by construction; join before teardown.
- The walk-end join must precede the decode tier's first client use (one request at a time; enforced by
  waiting the slot at the last combine).
- Debug lines interleave from two threads (cosmetic only).

## 12. M7 — the drafter lane (DFlash displaced to the helper)

What moves: the DFlash block drafter (the standalone dense 5-layer GGUF, ~498M BF16 = about 1.0 GiB
of weights plus its ring scratch) leaves the target engine's GPU and runs on the helper's card. The
target keeps verification; the helper produces candidates.

Why this lane is structurally different from the prefill claims: the drafter is a per-step,
latency-bound dense forward. It is not layer-cadence-bound like the prompt claims, which measured as
a dead end twice (M4c claim depth, M6 pipelining). Today the drafter runs between verifies on a VII
and costs both VRAM (about 1 GiB) and per-step compute. Displacing it recovers both: the freed VII
VRAM grows the expert cache (decode hit rate) and the VII keeps the whole step budget for verify.

The interface, grounded at the call sites: `df2.draft(ver.tap_dev(), pos, ctx, anchor, drafts,
probs, err)` (generate.cpp:17534) and `mtp.draft(T, outv, p, a, drafts, err, dprob, min_p)`
(:16013, :17561). One narrow call: the taps (the verifier's collected target residual planes,
5 planes x hidden per query block, about 51 KB at the published geometry) plus position, context
and the anchor token go in; the candidate block and its probabilities come out.

The wire (new frames, opt-in): `kDraftReq` carries the taps payload and the scalars; `kDraftReply`
carries the candidate ids, the probabilities and a refusal bit. Same socket, same framing as the
existing tiers. The helper loads the DFlash artifact at startup, before the expert fill; the fill
is elastic (it fills whatever VRAM is free), so draft mode simply shrinks the mirrored expert slots
by about a GiB — no new cap knob. Env: `STRATA_DRAFT_HELPER=1`, default off.

Engine side: a remote-drafter adapter with the same `draft()` signature, selected at the existing
`--dflash` wiring when the helper advertises draft service in its hello; the call sites stay
unchanged. The drafter's ring state lives on the helper.

Failure semantics: a draft timeout or a dead helper disables drafting for the rest of the request
(the same permanent-loss rule the prefill claims use) and decode falls back to plain target-only
steps. Drafts are speculative, so correctness is never exposed; only acceptance statistics can
shift (same weights, cross-vendor floating point). The p99.9/max band does not apply to the
drafter; the accepted/offered ratio before and after is the health metric.

Latency budget: per step about 51 KB up and tens of bytes down, one socket round trip plus a
5-layer 498M forward on a query block (the 3080's compute edge). The budget is the slot the local
drafter already occupies between verifies; if the round trip exceeds it, disable and measure.

Evidence (the M7 gate):
- builds green and both suites pass (hip 4/4, cuda 6/6); with the flag off the default path stays
  byte-identical;
- one window, OFF and ON-draft arms: end-to-end tok/s, drafts accepted/offered (the acceptance
  shift), the helper's window and draft counters, the VII's freed VRAM grown into expert slots,
  zero target-path fallbacks;
- the long-arm rule: at least +5% end-to-end decode tok/s at the 78k arm, or record PAR and stop.

### 12a. Correction: the two DFlash2 runtimes and the true seam

This tree carries TWO mutually exclusive drafters (CMakeLists.txt:102-107 refuses a build with both):
`STRATA_HAVE_DFLASH2` (upstream v0.2.3's `src/core/dflash2.cpp`, the in-verify taps, the CLI spec
loop — the default ON both candidate builds carry) and `STRATA_ENABLE_DFLASH2` (ours:
`dflash_inco.cu`, the serve / layer-split taps — what production and the capture config run, with
`--dflash GGUF`). The interface grounded above (`df2.draft`) is upstream's CLI-only drafter; the
M7 target is the SERVE runtime, because production parity is the point.

The serve seam (include/strata/core/dflash.hpp:124, `DFlashDrafter`):
- `add_context_f32(const float* taps, n_taps, stride_floats, pos0, rows, err)` feeds the ring —
  BOTH the prompt phase (bulk, per chunk) and each round's committed window rows. A u16 variant
  (`add_context`, bf16 rows) exists and would halve the wire (25.6 vs 51.2 KB per row); fp32 stays
  the v1 wire format, u16 the v2 option.
- `propose(int32 anchor, int64_t pos, int block, int32* out, err, float* probs)` returns the
  candidate block and its probabilities.
- The engine keeps `dflash.tap_collector()` (verifier-attached; its pinned fp32 rows are exactly
  what ships). The helper loads the artifact, uploads to its device, and binds the target's
  WeightTable + NativeHead from the pack / native GGUF it already reads.

Wire mapping: `kDraftCtx` -> `add_context_f32` (reset=1 on a request's first frame);
`kDraftReq` -> `add_context_f32(n_new rows)` then `propose(anchor)`; `kDraftReply` -> the block's
token ids and probabilities. The frames defined in step 1 already carry these payloads unchanged.

Build consequence: both candidate builds reconfigure to `-DSTRATA_ENABLE_DFLASH2=ON
-DSTRATA_HAVE_DFLASH2=OFF` (production parity; also the capture-harness fix — its per-pair engines
died rc=1 on "dflash2: rebuild with -DSTRATA_ENABLE_DFLASH2=ON"). Banked M4-M6 arms are internally
valid (same build on both arms of each A/B, no draft args); any window after the flip re-runs its
own baseline.

## 12b. The two-channel split (opt-in, the M7 gate's head-of-line finding)

The M7 gate measured the drafter lane on the helper's single socket: decode throughput fell −19.1%,
its per-round round trip serialized behind the window traffic, and a bounded draft wait timed out
twice during a 78k-token prefill while prompt-row frames and window frames queued together. The
cause is structural, not the drafter: ONE socket carries ONE in-flight request, so the window tier
(kBegin/kRows), the prompt tier (kBeginP/kRowsP) and the drafter lane's frames all wait for each
other.

The split is two sockets and two serve threads, opt-in and byte-identical when the second path is
absent:

- helper: `--socket2 PATH` binds a second socket and runs the same accept/frame loop on its own
  thread. `kFill` runs on ONE channel; both channels serve rows out of the same filled mirror, so
  the shared device state (the expert mirror, the staged activations, the drafter ring) is
  serialized by one lock per served frame, never held across a channel's idle read. The second
  channel never re-listens: its absence is the single-channel case, not a peer to wait for.
- engine: `--expert-helper-socket2 PATH` (config key `expert_helper_socket2`). Channel A keeps the
  handshake, the fill, the window tier and the drafter lane; channel B carries the prompt tier.
  Channel B handshakes with the same geometry and caps and never fills. Every failure of channel B
  is a printed notice and the single-channel behaviour, never a failed open.
- failure law: each channel latches its own tier. The prompt tier's channel is resolved once per
  request (`note_request_start`) and pinned for the request, because prefill stages its rows into
  that channel's pinned activation buffer before it sends: a channel that dies mid-request makes the
  send fail by name and the caller's same-layer local recompute runs, and it never sends frames
  built for the other channel. The prompt tier re-arms at the next request on its own channel if it
  is alive, else back on the primary.
- protocol: NO new frame type, no layout change (`expert_helper_protocol.hpp` static_asserts
  untouched).

Validation (CPU-only, no GPU and no device call): `tests/core/expert_helper_ownership_test.cpp`
grew scenarios (D) and (E) — the real proxy over two real AF_UNIX channels with two scripted fake
helpers: the prompt channel dies and the window tier serves every window in between; the window
channel dies, claims zero rows, releases its residency for the promotion gate, and the prompt tier
keeps serving on its own channel. 5/5 `ctest -R helper` in `build-mixed-gpu/rpcw-hip` and
`build-mixed-gpu/rpcw-cuda`; the two-channel end-to-end gain on hardware is NOT measured — that A/B
is the parent's window.
