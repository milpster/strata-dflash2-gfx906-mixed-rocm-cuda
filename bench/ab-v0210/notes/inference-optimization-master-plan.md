# Inference optimization master plan

**Status: reconciled draft, 2026-11-19.** Nemotron synthesis, corrected by the parent against all four inputs. This is a documentation deliverable, not approval to implement, measure or promote a candidate.

**Read sections 1–8 for the reconciled program, section 9 for coverage, and section 11 for the complete source snapshots.** Source snapshots preserve all original designs, numbers, schedules, tests and references. They are historical evidence, not competing execution instructions. No source plan is changed.

---

## 1. Executive Index, Scope, Provenance, and Status Legend

### 1.1 Scope Statement
This master plan consolidates the **entire whole-inference optimization program** spanning:
- Cold and incremental/cached-prefix prefill (PP)
- Token generation throughput (TG)
- Time-to-first-token / whole-request turnaround (TTFT)
- Stop/cancel tail latency
- Checkpoint/park/restore and boot/recovery
- All memory-cost accounting (engine/helper RSS/PSS, system MemAvailable, swap, per-card VRAM, cache/residency/lock placement)

**Quality mandate:** Zero quality sacrifice. Unchanged quantization, precision, model, context/KV capacity and request budgets. Require exact relevant operation/logit equivalence or a credible unchanged-arithmetic-path proof plus executed, engaged tests. Retain teacher-forced KL 0.00000/no-NaN campaign checks; rounded zero is not bit identity or an alternative pass.

**Governance:** New candidates opt-in / default-off. Default path stays byte-identical to last release. Prior reviewed source plans are **not** approval of this master or any candidate, and never authorization to implement/benchmark/promote. Fresh explicit operator GO required before ANY production downtime. Agent promotes nothing; parent alone controls windows.

### 1.2 Four Source Documents — Verified Identities (SHA256)

| # | Source Document | Path | SHA256 | Last Change | Notes |
|---|-----------------|------|--------|-------------|-------|
| 1 | **speed-plan-v2.md** (PP program) | `bench/ab-v0210/rpcprof/gpu-rounds/speed-plan-v2.md` | `db7bc537c4e39c36ed35f086ff8434dde325a07cc53037c5ca60301949961d1c` | eab11571e4, 2026-11-18 11:16 CET | Historical PP design; its existing supersession notice is preserved in the snapshot, not a direction to replace this reconciliation with the serving-plan file |
| 2 | **tg-optimization-chapter.md** (TG research plan) | `bench/ab-v0210/rpcprof/gpu-rounds/tg-optimization-chapter.md` | `02abecd2e0c10360a640881d2d641e060c5ea1ef73c80c348d7a2a36bb39552c` | 97ea0b0edd, 2026-11-18 09:05 CET | Oracle APPROVE for research-plan readiness only; reviewed substantive v1 at `d649e5bba5` |
| 3 | **code-opt-plan.md** (F1–F8 implementation) | `bench/ab-v0210/notes/code-opt-plan.md` | `fb8d30d31c6ba91ee8c9d3185dfbbad20cf02d4242475c257d551499e8f2f7ac` | bd4172ed00, 2026-11-15 17:32 CET | Amended after an APPROVE-WITH-CHANGES review for P0 start; no blanket approval of this master |
| 4 | **perf-roadmap-2026-11-15.md** (A/B/C projects) | `bench/ab-v0210/notes/perf-roadmap-2026-11-15.md` | `3bf80d0ac6e6c51e4fc0ec0b333b3c3915b0db750d4c2b6186256df44bcd6f6b` | 20c9059a68, 2026-11-15 14:08 CET | Researcher: explorer agent (glm-5.3); zero quality sacrifice constraint |

The identities above pin this reconciliation. Exact preservation is checked by comparing each embedded snapshot with its source, separately from reviewing the interpretation. No guessed byte or item totals are used.

Working source links: [PP plan](../rpcprof/gpu-rounds/speed-plan-v2.md), [TG chapter](../rpcprof/gpu-rounds/tg-optimization-chapter.md), [code plan](code-opt-plan.md), [performance roadmap](perf-roadmap-2026-11-15.md). Full copies are in [section 11](#snapshots).

### 1.3 Supporting Read-Only Context (Consulted, Not Merged)
- [Code-plan review](code-opt-plan-review.md): ownership, sequence reset, producer parity, protocol and baseline corrections.
- [TG review](../rpcprof/gpu-rounds/tg-optimization-review.md): verified timer wiring, profiler effects, sampler eligibility and sampled upstream status as of Nov 18.
- [Code analysis](code-opt-analysis.md): design lineage, not a later result.
- [Journal](../../../journal/serving-tg-reeval-split-retest-2026-11-11.md): targeted provenance and operator decisions.
- [Serving plan](../rpcprof/gpu-rounds/serving-plan-current.md): **not one of the four inputs**. Its immediate queue is context only. It does not substitute for this whole-program reconciliation.

### 1.4 Standing Operator Requirements (Current-Operations Overlay)
- **Optimization definition:** PP (cold + incremental/cached-prefix), TG, TTFT/turnaround, stop/cancel tail, checkpoint/park/restore, boot/recovery. Memory cost reported beside performance.
- **Upstream inspection + dedicated external/web research** are program-wide discovery inputs for ALL components. Record provenance URLs/SHAs/status/backend/model applicability and local equivalence. This task PLANS research; does not perform fresh web campaign.
- **Zero quality sacrifice:** exact relevant operation/logit equivalence, or a credible unchanged-arithmetic-path proof plus executed, engaged tests. Keep the teacher-forced KL 0.00000/no-NaN campaign checks; rounded zero, top-1, PPL or free-running hashes alone are insufficient. No mean-KL ≤0.001 fallback.
- **Opt-in/default-off:** default path unchanged; prior plans ≠ approval.
- **Fresh operator GO before EVERY production down.** Earlier blanket GO superseded. Parent alone controls windows.
- No locally served specialist may be mid-turn during a parent-owned GPU window. Code-only workers may not boot, stop or signal serving processes. Stop and boot are separate invocations through the existing on-disk scripts. A hard GPU/OS hang is an operator incident, not permission for reset experiments.
- **Current production baseline:** reserves 700/700 MiB, canonical engine `b0ec0109ebc50c982512c371d81b2c15a8f72c70edf8a461a4ff10337da52f02`, source `42cb41b314`, config SHA `b221140fc394957d9a52cf475d1539340d42edc859ff7ce8e4d06d8d6bf35df8`. Historical reserve values (192/156, 256/448, 576, etc.) remain historical, not instructions. ROCm 6.1.0 userspace, custom gfx906 xnack rocBLAS at `/home/srcds/rocm-gfx906-xnack/lib`, core pins `3,5,7,9,11,13,15` unchanged.
- **Immediate queue (annex, not replacement):** parking-restart correctness fix BEFORE margin retest. Margin retest: each arm (256/448 vs 700/700) ONE conversation: 80k new infill → 2048 TG → another 80k new infill → 2048 TG; speeds + RAM/VRAM + parking evidence. mlock investigation → rocBLAS investigation. Execution behind explicit downtime GO and evidence gates.
- **Locking scope, clarified by the operator:** do **not** mlock the PLE table; do **not** use blanket `mlockall()`. The intended test locks **expert weights held in system RAM**; GPU-resident expert weights remain in VRAM. The arena is already locked in current setup. Native GGUF coverage + fit need verification — NOT all model experts are RAM-resident. Prior result (journal 18w/18x, commit 660d9f0dc7: 4608 spans, 46.84 GiB, 128s) is historical evidence, not a new test. Operator asked test RAM caps removed; no automatic prod default change.

### 1.5 Status Legend (Used Throughout — Latest Evidence Only)
| Symbol | Meaning |
|--------|---------|
| ✅ **Adopted** | Operator-selected production setting or established behavior; selection alone does not prove a speed gain |
| 🟢 **Landed, default-off** | Code in tree behind env gate; not promoted |
| 🟡 **Unvalidated** | Proposed, not yet built/tested; placeholder estimates |
| **Not landed / blocked** | Missing from the pinned tree or lacking an eligibility prerequisite; distinct from rejected forever |
| ⚪ **Performance-null** | Measured, no significant gain (within noise) |
| 🔴 **Regressed/Reverted** | Measured loss or regression; reverted |
| ❌ **Rejected/Closed** | Explicitly closed; do not relabel as new |
| ⚠️ **Inapplicable** | Hardware/config mismatch; not locally relevant |
| 📊 **Measurement-only** | Instrumentation/infrastructure; no direct speed claim |
| ❓ **Unknown/Needs-recheck** | Conflicting evidence; requires fresh measurement |
| 📝 **Historical/Superseded** | Older assumption/estimate overridden by later evidence |

---

## 2. Complete Inventory — Disambiguated IDs and Cross-Reference

### 2.1 ID Namespace Map (Critical: Original IDs Collide)

| Source | Original ID Pattern | Disambiguated Prefix | Master ID Range | Domain |
|--------|---------------------|----------------------|-----------------|--------|
| speed-plan-v2 | Phase 0, A1, B1, B2, DEQUANT_SIDE | `PP-` | `PP-P0`, `PP-A1`, `PP-B1`, `PP-B2`, `PP-DEQUANT` | Prefill (PP) |
| tg-optimization-chapter | S1–S4, prior work rows (18) | `TG-` | `TG-S1`–`TG-S4`, `TG-PW-*` (18 rows) | Token Generation |
| code-opt-plan | F1–F8 (with F4a/b, F7a/b/c) | `CODE-` | `CODE-F1`–`CODE-F8` (subcases kept) | Code-level optimizations |
| perf-roadmap | A1–A5, B1–B6, C1–C3 | `ROAD-` | `ROAD-A1`–`ROAD-A5`, `ROAD-B1`–`ROAD-B6`, `ROAD-C1`–`ROAD-C3` | Config/Code/Kernel projects |

**Rule:** Never merge on ID alone. `PP-A1` is routed streaming; `ROAD-A1` is PLE mmap. `CODE-F7C` and `CODE-F7AB` are separate parts of original F7. Historical forecasts in any candidate description below remain source hypotheses, not current targets or time estimates.

### 2.2 Master Inventory Table (All Items — Latest Evidence Disposition)

| Master ID | Source ID(s) | Title | Mechanism Category | Primary Source Doc | Status (Latest Evidence) |
|-----------|--------------|-------|-------------------|-------------------|--------------------------|
| **PP-P0** | speed-plan: Phase 0 | Decisive probe (2 boots, timing/routed stream) | PP measurement infrastructure | speed-plan-v2 | 🟡 Unvalidated (plan only) |
| **PP-A1** | speed-plan: A1 | `STRATA_PREFILL_ROUTED_STREAM=1` depth sweep 16/32/64 | PP env round (bit-identical) | speed-plan-v2 | 🟡 Unvalidated |
| **PP-B1** | speed-plan: B1 | Capacity placement: host RAM relief-hot + VRAM cache growth | PP placement (bit-identical) | speed-plan-v2 | 🟡 Unvalidated (math-only so far) |
| **PP-B2** | speed-plan: B2-REPLACEMENT | `STRATA_DEQUANT_SIDE=1` side-stream dequant (built, parity test exists) | PP GPU overhead | speed-plan-v2 | 🟡 Unvalidated (parity gate pending) |
| **PP-GATES** | speed-plan: Gates | Bit-identical contract: no KL; A/B medians + decode guard ≥24 tok/s | Governance | speed-plan-v2 | 📝 Historical (superseded by current policy) |
| **TG-S1** | TG-chapter: S1 | Complete attribution on real serving path (helper timing, verifier tail) | TG measurement infrastructure | tg-optimization-chapter | 🟡 Unvalidated (research plan) |
| **TG-S2** | TG-chapter: S2 | Revalidate landed F1 in-graph sampler on eligible requests | TG sampler (overlaps CODE-F1) | tg-optimization-chapter | 🟡 Unvalidated |
| **TG-S3** | TG-chapter: S3 | Investigate gfx906 attention lane-cell variant if attention measured cost | TG kernel (attention) | tg-optimization-chapter | 🟡 Unvalidated |
| **TG-S4** | TG-chapter: S4 | Measurement infrastructure: fixed decode traces + CPU-only slice benches | TG measurement infrastructure | tg-optimization-chapter | 🟡 Unvalidated |
| **TG-PW-MMQ** | TG-chapter: prior work row 1 | `STRATA_PREFILL_MMQ=0` | PP/TG config | tg-optimization-chapter | ✅ Adopted (edf2af2489: OFF +7% PP, +12% TG) |
| **TG-PW-SPLIT** | TG-chapter: prior work row 2 | Split 30, DFlash block 7, foresight 2/8, reserves 192/156, MMVF, CPU affinity, disk placement | Config bundle | tg-optimization-chapter | ✅ Adopted configuration work (historical reserves) |
| **TG-PW-F2** | TG-chapter: prior work row 3 | F2 xq ring | TG ring | tg-optimization-chapter | ⚪ Performance-null (18cp: 25.65 vs 25.55 tok/s) |
| **TG-PW-F3** | TG-chapter: prior work row 4 | F3 CPU-pool from ring | TG CPU pool | tg-optimization-chapter | Not landed; blocked without a changed premise after F2's null result |
| **TG-PW-F5** | TG-chapter: prior work row 5 | F5 PLE gather overlap | TG PLE | tg-optimization-chapter | Not landed; original rejected, amended design still needs ownership and residual-cost proof |
| **TG-PW-F4** | TG-chapter: prior work row 6 | F4 speculative prefetch | TG prefetch | tg-optimization-chapter | Not landed; blocked pending residual wait proof |
| **TG-PW-F6** | TG-chapter: prior work row 7 | F6 queued/parallel boot fill | Boot | tg-optimization-chapter | 🟢 Landed, off in prod (boot-only, not TG) |
| **TG-PW-F8** | TG-chapter: prior work row 8 | F8 mapped penalty staging | TG penalty | tg-optimization-chapter | 🟢 Landed, off; request-dependent |
| **TG-PW-F1** | TG-chapter: prior work row 9 | F1 in-graph sampler | TG sampler | tg-optimization-chapter | 🟢 Landed, off; usable evidence incomplete (18ca) |
| **TG-PW-F7C** | TG-chapter: prior work row 10 | F7c timing attribution | Instrumentation | tg-optimization-chapter | 🟢 Landed, incomplete for current shape (18cd: zero helper fields) |
| **TG-PW-F7AB** | TG-chapter: prior work row 11 | F7ab O_DIRECT/offset sort | Boot I/O | tg-optimization-chapter | 🟢 Landed, inactive on prod fill source (helper fills from RAM arena) |
| **TG-PW-WARM** | TG-chapter: prior work row 12 | Prefill warm + swap-in depth 128 | PP config | tg-optimization-chapter | 🔴 Regressed/reverted (18cq: combined ~19% PP regression) |
| **TG-PW-OVERLAP** | TG-chapter: prior work row 13 | Prefill read/compute overlap | PP overlap | tg-optimization-chapter | ✅ Already present (prefill.cpp:4040-4077) |
| **TG-PW-DEQUANT** | TG-chapter: prior work row 14 | `STRATA_DEQUANT_SIDE` | PP dequant | tg-optimization-chapter | 🟡 Unvalidated (PP-shaped, not local TG evidence) |
| **TG-PW-ADAPTIVE** | TG-chapter: prior work row 15 | Acceptance chasing / adaptive drafting | TG adaptive | tg-optimization-chapter | ❌ Rejected (18z/18am/18ao) |
| **TG-PW-MISC** | TG-chapter: prior work row 16 | PLE mmap/row-cache, CPU KV, queue priming, IQ threshold, A2 knobs | Misc | tg-optimization-chapter | ⚪ No adoption/null (18bc/18bv/18an/18bw/18bq/18bo-bp) |
| **TG-PW-HCQ8** | TG-chapter: prior work row 17 | HC_Q8 native/inject/trim | HC_Q8 | tg-optimization-chapter | ❌ Closed/off (18aq-18ax/18ba) |
| **TG-PW-SLOWFAST** | TG-chapter: prior work row 18 | Same-build slow/fast serving modes | Mystery | tg-optimization-chapter | ❓ Unresolved (15–22 vs 27–31 tok/s; unexplained) |
| **CODE-F1** | code-opt: F1 | In-graph head sampler via mapped params (sampled/penalized) | Sampler | code-opt-plan | Landed, off; engaged correctness and benefit not established; same disposition as TG-PW-F1 |
| **CODE-F2** | code-opt: F2 | Helper xq ring: ship GPU q8_1 image via mapped ring | Ring transport | code-opt-plan | Measured performance-null, off; amended proposal predates that result; implementation differences unknown |
| **CODE-F3** | code-opt: F3 | CPU-pool activation quantize from F2 ring | CPU pool | code-opt-plan | Not landed; blocked without a demonstrated changed premise |
| **CODE-F4** | code-opt: F4 | Helper weight pre-stage + PCIe blob pre-stage | Prefetch | code-opt-plan | Not landed; conditional design, not an implementation |
| **CODE-F5** | code-opt: F5 | PLE gather overlapped with k==0 host service | PLE overlap | code-opt-plan | Not landed; unsafe original rejected; amended design conditional |
| **CODE-F6** | code-opt: F6 | Boot slot pre-fill: queued, batched, parallel | Boot fill | code-opt-plan | Landed, off in production; boot benefit only, not a new TG candidate |
| **CODE-F7C** | code-opt: F7c | Per-phase fill-rate instrumentation | Instrumentation | code-opt-plan | Landed, incomplete on the exercised path; append-only logging was intentionally ungated |
| **CODE-F7AB** | code-opt: F7ab | Boot O_DIRECT + offset-sorted reads | Boot I/O | code-opt-plan | Landed, inactive on production helper's RAM-arena fill source |
| **CODE-F8** | code-opt: F8 | Penalty-history staging off round critical path | Penalty staging | code-opt-plan | Landed, off; conditional on penalties; neutral-penalty tests are not engagement |
| **ROAD-A1** | roadmap: A1 | `--ple-io mmap` / `STRATA_PLE_AUTO_MMAP=1` | PLE I/O config | perf-roadmap | Historical proposal; later no-adoption/null ledger applies; a new use requires a changed premise |
| **ROAD-A2** | roadmap: A2 | gfx906 decode-switch sweep | Decode config sweep | perf-roadmap | Historical sweep; later null/A2 history applies; individual path applicability must be checked |
| **ROAD-A3** | roadmap: A3 | `--ple-row-cache` bump | PLE cache config | perf-roadmap | Historical proposal; later no-adoption/null ledger applies |
| **ROAD-A4** | roadmap: A4 | Audit `STRATA_HIP_PROMPT_F16=1` (LIVE in prod, not bit-identical) | PP rounding config | perf-roadmap | ⚠️ Decision item (NOT bit-exact; baselines include it) |
| **ROAD-A5** | roadmap: A5 | Refuted: --kv-grow, DENSE_MMQ, --adapt-every/FS, dflash adaptive | Refuted items | perf-roadmap | ❌ Rejected/Closed |
| **ROAD-B1** | roadmap: B1 | HC-swap harness wedge and capacity proposal | HC swap fix | perf-roadmap | Static diagnosis unconfirmed here; dependent HC_Q8 work is closed; no instruction to land the old commit |
| **ROAD-B2** | roadmap: B2 | #267 verify-timeout retry (bounded re-capture) | Reliability | perf-roadmap | 🟡 Unvalidated (bit-exact) |
| **ROAD-B3** | roadmap: B3 | Fail loudly under 512 MiB floor (L2 probe only warns) | Safety | perf-roadmap | 🟡 Unvalidated (control flow) |
| **ROAD-B4** | roadmap: B4 | Checkpoint save off read path (async copy-stream + event) | Checkpoint I/O | perf-roadmap | 🟡 Unvalidated (bit-exact if same state) |
| **ROAD-B5** | roadmap: B5 | Smarter PLE I/O default | PLE I/O default | perf-roadmap | Blocked by A1's missing benefit evidence; changing a default needs a separate operator decision |
| **ROAD-B6** | roadmap: B6 | gfx906 queue priming | Queue scheduling | perf-roadmap | Later null/no-adoption history; not a fresh candidate without a changed premise |
| **ROAD-C1** | roadmap: C1 | HC_Q8 prefill T-template fusion | Kernel fusion | perf-roadmap | Closed/ineligible under current zero-sacrifice baseline; historical rebaseline escape clause is not authorized |
| **ROAD-C2** | roadmap: C2 | Bit-exact GPU expert contract (pin CPU kernel set, placement-pin mode) | Determinism enabler | perf-roadmap | Unvalidated; exactness, dispatch applicability and performance cost must be established |
| **ROAD-C3** | roadmap: C3 | gfx906 LDS/wave64 audit of decode hot path | Kernel audit | perf-roadmap | 🟡 Unvalidated (per-kernel bit-exact required) |

---

## 3. Consolidated Mechanisms — Duplicates Merged, Unique Details Preserved

**How to read the cards:** designs and forecasts from Nov 15–17 are retained as history, not as claims that code landed or remains shortlisted. Section 2 and the later TG evidence govern status. CODE anchors refer to the source plan's `84fc595dd9` tree; TG anchors refer to `af1e64a5cb4b617d52c0f9b416af76d28c42bc4f`. ROAD/PP anchors retain their own source provenance. They have not all been reverified against current production. Recheck implementation, not just ancestry or line numbers, before proposing a change. Full change lists, commit slices, risks, tests and DoD remain indexed in section 9 and copied in section 11.

Validation labels: **CPU** means a genuinely host-only fixture; **GPU** includes kernel parity, device-context tests and any engine boot/quality/performance leg. A host comparator reading GPU-produced golden bytes does not make producing those bytes CPU-only. No validation ladder below authorizes execution or replaces the stricter quality rule in §1.4.

### 3.1 Mechanism: In-Graph Sampler (Sampled/Penalized Requests)
**Master ID:** `MECH-SAMPLER`  
**Source IDs:** `CODE-F1`, `TG-S2`, `TG-PW-F1`

| Aspect | CODE-F1 (Primary) | TG-S2 / TG-PW-F1 |
|--------|-------------------|------------------|
| **Status** | Landed default-off (`STRATA_SAMPLER_IN_GRAPH`); `sampler_mapped.hpp:40`, `verify.cpp:2846-2852` | Revalidate on eligible class; 18ca invalidated earlier engagement claim via config `exe` bug |
| **Mechanism** | Mapped params (`m_sp_`, `m_hist_`, `m_spec_`, `m_mode_`) → captured `sample_tokens_mapped` kernel (generic, mode-switched) replaces host `sample_tokens` family | Same; greedy head already in-graph at `verify.cpp:2658-2664` |
| **Arming** | Gate on `hist_d_ != nullptr` (includes penalized-greedy per review); `logit_bias` stays fallback; `measure` legs stay host | Eligibility gates: nonzero sampler blocks, `next_==nullptr`, no batch recording, bounded rows |
| **Bit-exactness** | Identical Philox(seed, pos0+t) via mapped `counter=pos0`; draw-identical parity test ≥1000 windows | Host fallback outside vocab/row/capture limits; forced traces must not bypass sampled op |
| **Validation** | **GPU:** `sampler_mapped_parity.cu`, token/draw-identical across ≥1000 windows and the sampled/penalized-greedy mode matrix; engaged qq KL 0.00000/no-NaN campaign checks; trace the removed second sync | Verify identical logits, Philox inputs/draws, filters, penalties and mapped-buffer ownership. Test host fallback separately; forced tokens must not bypass the sampler under test. |
| **Historical forecast** | Nov-15 ~+1% WT on sampled/penalized turns, not a current target | Price the actual eligible serving class first |
| **Key review amendments** | Penalized-greedy armed by design (mode word supports it); generic kernel reproduces exact statement order per mode | Sampler tests must prove eligible path runs |

**Historical hold:** the Nov-15 plan held judge legs pending warm-TG band restoration. This reconciliation does not establish that the hold was released. Any future run needs an explicit baseline/hold disposition, stable same-day controls and fresh operator GO; code already exists, so this is revalidation, not a new implementation task.

### 3.2 Mechanism: Helper XQ Ring (GPU→Helper Activation Image)
**Master ID:** `MECH-XQ-RING`  
**Source IDs:** `CODE-F2`, `TG-PW-F2` (performance-null), `TG-PW-F3` (depends on F2)

| Aspect | CODE-F2 (Amended) | TG-PW-F2 / TG-PW-F3 |
|--------|-------------------|---------------------|
| **Status** | Historical amended design, predating the measured null. Implementation differences between that design and the measured build are unknown. | F2: performance-null/off (18cp: 25.65 vs 25.55 tok/s). F3: not landed. Neither is shortlisted without a demonstrated changed premise. |
| **Mechanism** | Tail kernel `doorbell_xq_publish` after final `nat_xq_` write (verify.cpp:2357); depth-2 ring by step parity; `m_xq_seq_` reset in `stage_inputs`; helper `proxy_begin` consumes ring image via `cur_xq()` | Same ring concept; chapter attributes null to drift + launcher bug; no specific hazard identified in source |
| **Folded layer safety** | Helper never claims folded (`expert_helper_candidates.cpp:80` skips `had_folded`); ring publishes rotated image at :2352 — correct for F3 CPU pool | F3 parity argument: ring = `quantize_q8_1_rows(xm)` (unfolded) or `quantize_q8_1_rows(hadamard(xm))` (folded) — byte-equal to `native_quant_act` |
| **QFUSE producer** | `STRATA_QFUSE` defaults 1 (arch_defaults.cpp:86); q8_ffn layers get `nat_xq_` from fused GR read. **Ring NOT gated on `!q8_ffn`**. Parity chain: QFUSE==native==rows==cpu (gr_parity.cpp:416-417 is 4th leg) | No evidence here that the measured implementation omitted this leg |
| **Helper gating** | Amended design requires tail publish/consumption gated on helper presence and gate-conditional allocation (OFF footprint unchanged) | Whether measured code differed requires a source comparison |
| **Validation** | **CPU:** fake-helper frame-byte equality, negotiation/default-off and multi-window stale-seq cases. **GPU:** `xq_triple_parity` including QFUSE producer; engaged qq KL 0.00000/no-NaN ×3 and matched judges only if re-eligible. Original ≥+2% target/hold remain historical. | Parity or a repaired test does not establish benefit; 18cp is the later null result |
| **Historical forecast** | Nov-15 +2–4% WT; invalid as a current business case | Later measurement was null |

**Requirements incorporated into the Nov-15 design (not proof about the measured implementation):**
1. Per-window `m_xq_seq_` reset in `stage_inputs` beside `h_seq_` (blocking bug)
2. QFUSE-producer parity leg mandatory (gr_parity.cpp:416-417)
3. Ring allocation + tail publish gated on helper present

**Chronology:** the amended plan is Nov 15; the null evidence is recorded in the Nov-18 TG chapter. Drift and a launcher engagement error explain the earlier apparent gain, not a demonstrated difference between two ring designs. We have not established that the null tested an older hazardous implementation, lacked QFUSE parity, or missed the amendment. The amendment is therefore **not a new post-null premise**. F2/F3 remain outside the shortlist unless an implementation comparison and measured residual cost establish a changed premise. Preserve the dependency “F3 depends on F2” without scheduling either.

### 3.3 Mechanism: CPU-Pool Quantize from Ring
**Master ID:** `MECH-CPU-POOL-RING`  
**Source IDs:** `CODE-F3`, `TG-PW-F3`

| Aspect | CODE-F3 (Primary) | TG-PW-F3 |
|--------|-------------------|----------|
| **Status** | Not landed; historical hard dependency on F2 ring; blocked without a changed premise | Not shortlisted |
| **Mechanism** | `expert_source.cpp:5316-5327`: replace per-token `native_quant_act` loop with memcpy into `d.nact_multi + t*kNativeActBytes` (NOT `d.act_multi[t]`) for q8_1 layers | Same; folded rotation contract at verify.cpp:2344-2348 |
| **Destination fix** | Review corrected: `nact_multi` with stride `kNativeActBytes`; fast path if `kNativeActBytes == (N/32)*36` | Implicit |
| **Validation** | **CPU:** stride/contiguity and fallback fixtures where host-only. **GPU:** producer parity extension, combined engaged qq KL 0.00000/no-NaN ×3 and matched judges if separately re-eligible. Assert `nact_multi` stride; leave Q2/non-native paths untouched. | Historical judge hold is not declared released |

### 3.4 Mechanism: Speculative Prefetch (Helper Weight + PCIe Blob Pre-Stage)
**Master ID:** `MECH-PREFETCH`  
**Source IDs:** `CODE-F4`, `TG-PW-F4`, `TG-S1` (attribution prerequisite)

| Aspect | CODE-F4 (Primary, amended) | TG-PW-F4 / TG-S1 |
|--------|---------------------------|------------------|
| **Status** | Not landed; conditional historical design, not a current capstone commitment | F4: not shortlisted without residual wait proof. S1 attribution first. |
| **Refinement** | **True-row prefetch non-constructible**: `x_{l+1}` does not yet exist. Proposed replacement: (a) helper weight pre-stage keyed by immutable (layer, expert, pack offset); (b) PCIe blob pre-stage into reserved slots. Compute only at real begin with the real activation image. | S1: helper timing reads `remote_experts[r]` under a remote-cache gate rather than the exercised `helper_tier_p`; profiling changes fusion/branch behavior. |
| **Protocol** | `kFeaturePrefetch` (bit 8), `kPrefetch=22` (engine→helper), `kPrefetchAck=23` (helper→engine). Extension rule: old helper never sees frame; new engine+old helper sends nothing. | N/A |
| **Helper serve thread** | **Blocking fix:** handler acks immediately (n_queued), stages async on helper-side thread OR rides stripe socket 2. Never blocks demand socket's serial frame loop. | N/A |
| **Ack drain** | **Blocking fix:** `finish()` (expert_helper_socket.cpp:710) tolerates/consumes LEADING `kPrefetchAck` frames before expected kRows. `begin()` peek demoted to fast path. | N/A |
| **Demand priority / budgets** | Proposed one-outstanding-request discipline per socket: refuse/skip prefetch while begin/finish or an ack is outstanding, never block a claim. Helper scratch ≤64 experts / historical ~150 MiB must fit actual arena-cap accounting; pressure returns `n_queued=0`, no retry storm. Reserve staging without displacing demand; measure speculative I/O, hit rate and memory cost. Source's m-tuning/hit-rate forecasts remain history, not zero-cost guarantees. | Verify feature/frame allocation against the actual integration version; Nov-15 numeric IDs are not proof they remain free. |
| **Predictor** | `last_ids[l][k]` from `h_ids_` + decayed usage score; top-m ∪ last window, m≤8 (`STRATA_PREFETCH_TOP`) | N/A |
| **PCIe pre-stage** | Dedicated staging slots (2 of group slots reserved); demand never displaced; key=(layer, expert, pack offset); reclaimed at window flagB join | N/A |
| **Value collapse rule** | Historical design stops/descopes the helper half if residual wait is below ~0.3 ms/layer; price the current wait first. F2/F5 are not assumed implemented or beneficial. PCIe half is separable, with its own gate and F7c/thrash evidence. | S1 attribution is a prerequisite, not a promise to implement F2/F5 first. |
| **Validation** | **CPU:** protocol/socket fake-helper, old-peer negotiation/refusal and ack-before-kRows drain tests. **GPU:** scratch-cap/engagement checks, engaged qq KL 0.00000/no-NaN ×3, then ≥3 matched judge boots if justified. Record hit rate, residual wait, memory and adopt/descope/stop. | **CPU:** compatible slice benches. **GPU:** fixed real-path traces and profile controls. |

### 3.5 Mechanism: PLE Gather Overlap (k==0 Host Service)
**Master ID:** `MECH-PLE-GATHER`  
**Source IDs:** `CODE-F5`, `TG-PW-F5`

| Aspect | CODE-F5 (Primary, **redesigned**) | TG-PW-F5 |
|--------|-----------------------------------|----------|
| **Status** | Not landed; original design **UNSOUND** and rejected; amended design remains conditional on ownership and residual-cost proof | Not shortlisted before those proofs |
| **Original flaw** | Side thread raises `m_flag_` early, skips host k==0 raise. **Breaks doorbell graph**: `m_flag_>=ring` at verify.cpp:2460/2466 gates CPU-rows copy; flagB lands mid-pool → GPU copies mid-write = silent corruption. Schedule-dependent; qq KL gate CANNOT certify absence. | Same design rejected |
| **Redesigned mechanism** | `PleGather` RAII thread started **after `stage_inputs` returns** (reset at :3160-3163 removes ordering hazard). Thread: derive PLE row ids + `gather_batch` into `h_ple_`. **Join after `pool()` returns, before unchanged `*flag=want` (:3616)**. k==0 inline gather deleted on gated path. Flag protocol **byte-for-byte unchanged**. Win bounded by `ms_pool` slice (F7c fields show it). | N/A |
| **ar_on() variant** | Separate optional variant: thread-raise valid there (`m_flag_>=1` gates only PLE copy at :1499). Gated separately inside `STRATA_PLE_ASYNC_GATHER`. | N/A |
| **Validation** | **CPU:** host-only failure/join-order fixtures assert one raiser, RAII join and error propagation. **GPU:** real doorbell ownership/ordering and PLE parity; engaged qq KL 0.00000/no-NaN ×3; matched judges if justified, gain bounded by priced `ms_pool`. A passing KL run cannot certify absence of the rejected race. | Historical judge hold is not declared released |

### 3.6 Mechanism: Boot Fill — Queued, Batched, Parallel
**Master ID:** `MECH-BOOT-FILL`  
**Source IDs:** `CODE-F6`, `TG-PW-F6`, `ROAD-B6` (queue priming), `CODE-F7AB` (O_DIRECT/sort), `CODE-F7C` (instrumentation)

| Aspect | CODE-F6 (Primary) | TG-PW-F6 | ROAD-B6 | CODE-F7AB | CODE-F7C |
|--------|-------------------|----------|---------|-----------|----------|
| **Status** | Landed, off; revalidate existing code only if boot cost justifies it | Boot-only, not TG | Later no-adoption/null history; do not requeue without a changed premise | Landed but inactive on production helper's RAM-arena fill source | Landed, incomplete for current shape; append-only logging intentionally ungated |
| **Mechanism** | `ExpertCache::fill_slots_batched`: pinned double-buffered staging (2×8×~1.38 MB ≈ 22 MiB/cache, 44 MiB total). `NibbleCache` discipline (one stream, kBatch=8, ordered reuse). Parallel threads per card (each establishes own `OnDevice`). `STRATA_BOOT_FILL_QUEUED` gate. | Same code (18bt, afc2ad38ff/e9593357b0) | `generate.cpp:3527-3539` `#if` guard excludes gfx906; `STRATA_HIP_QUEUE_PRIME` would lift it. Bimodal fix (R9700). | (a) O_DIRECT boot reads into F6 pinned halves (reuse `read_direct` windowing). (b) Offset-sorted read order (slot admission stays rank-order). Gates: `STRATA_BOOT_FILL_ODIRECT`, `STRATA_BOOT_FILL_SORT` (independent). | Extend fill-rate line (generate.cpp:9405) with `pack_read_s`, `h2d_s`, `pinned_mb`, `phases`. Verifier `ms_wait/ms_pool/ms_host` + helper begin/finish + PCIe fetch → serve summary. Append-only, no gate. |
| **F6+F7AB composition** | Three compose: pipeline + parallel + O_DIRECT/sort. Matrix covers ON/OFF combos. | N/A | Queue priming would show in H2D-phase rates with identical pack-read rates; page-cache warmth shows in pack-read rates. F7c discriminates. | F7c fields are the instrument — no further test files. | Enables F7AB/B6 attribution: H2D-phase vs pack-read rates distinguish queue placement vs page cache. |
| **Validation** | **GPU:** `expert_cache_batched_test.cpp` needs a device context; exhaustive all-slots compare plus engine `STRATA_VERIFY_COMPLEMENT` gate-on/off checksum vectors. `verify_slot` is one slot only. Then matched boot/fill and engaged quality checks. | Existing tests are not proof they ran | **GPU, only if re-eligible:** historical ≥8 interleaved boots, fill-wall distribution | **CPU:** file/alignment/order fixtures. **GPU, only on an applicable fill source:** historical ≥6 interleaved boots with fresh class bands, one fresh fill/boot and independent F6/F7a/F7b arms | **CPU:** append-only parser regressions. **GPU:** counters actually nonzero on the exercised path and reconciled with overlap; profile allocations/observer effects measured |
| **Historical forecast** | Nov-15 −15…−35 s/boot; per-phase H2D ≥2× was a target, not a current result | No direct TG claim | Nov-15 +10–15% on slow boots, followed by null history | Nov-15 PP +7–20% on slow boots, not current evidence | No direct gain; enables attribution |

**Key review amendments (code-opt-plan-review):**
- `verify_slot` reads back ONE slot/boot — all-slots proof is `STRATA_VERIFY_COMPLEMENT` (generate.cpp:9409-9418). DoD strengthened.
- End-to-end ≥2× claim caveated: at 671 MB/s, 9.6 GiB reads ≈ 14.3 s of 14.8 s wall → fast class already read-bound. Per-phase DoD (H2D-phase ≥2×).
- Parallel threads must each establish own `OnDevice` (cudaSetDevice per-thread).
- Baselines confounded by Nov-6 pinning fault (fill threads parked on CPU 0). Re-base on adopted AUX_CPUS config.

### 3.7 Mechanism: Penalty-History Mapped Staging
**Master ID:** `MECH-PENALTY-STAGING`  
**Source IDs:** `CODE-F8`, `TG-PW-F8`

| Aspect | CODE-F8 (Primary) | TG-PW-F8 |
|--------|-------------------|----------|
| **Status** | Landed, off; historical P0 implementation proposal is not new work | Request-dependent; neutral penalties are not engagement |
| **Mechanism** | `Verifier::stage_hist` via mapped buffer (`m_hist_`, 2 KiB + control word). Build rows **after THIS window's draft, before launch** (not previous window — unrealizable in serial loop). Blocking `cudaMemcpy` at generate.cpp:22593 deleted on gated path. Gate `STRATA_PENALTY_MAPPED`. | Same; neutral penalties don't exercise intended path |
| **Bit-exactness** | Same `penalty_rows` bytes, same destination, earlier copy. Mapped len word written before launch, read at replay. | N/A |
| **Validation** | **CPU:** identical history-row bytes, capture-time bounds and validity-word fixtures. **GPU:** mapped ownership/capture replay, engaged penalized-request quality checks and trace proving the removed blocking H2D after THIS draft | Verify the actual `penalty_last_n` cap; no benefit claim from neutral-penalty requests |
| **Historical forecast** | Nov-15 WT +0.1–0.2%, inside a small/noisy slice | Must price and engage the present request class |

### 3.8 Mechanism: PLE I/O — mmap vs O_DIRECT
**Master ID:** `MECH-PLE-IO`  
**Source IDs:** `ROAD-A1`, `ROAD-A3`, `ROAD-B5`, `CODE-F7AB` (O_DIRECT for boot, not PLE steady-state)

**Disposition:** ROAD-A1/A3 have later no-adoption/null evidence; B5 is blocked on demonstrated A1 benefit and a separate default-change decision. F7ab is landed but inactive on the current helper fill source. The older designs/forecasts below do not resurrect them. Mmap, O_DIRECT and row caching are distinct mechanisms, but that distinction is not evidence that an untested portion of the old speed claim survived.

| Aspect | ROAD-A1/A3/B5 (Primary) | CODE-F7AB |
|--------|-------------------------|-----------|
| **Scope** | Steady-state PLE I/O (swap-ins, fills) | Boot-profile fills only |
| **A1: mmap** | `STRATA_PLE_AUTO_MMAP=1` or `--ple-io mmap`. Boot probe: ~13.5K rows/s ≈ 54 MB/s < 15K threshold → mmap. Removes layer-1 direct-read tax. **Bit-exact** (ple_fp8_parity.cpp:7-8, ple_reader_test.cpp:296-418). Est. +3-10% PP, fast-mode stabilization; warm TG ≈ 0. | N/A |
| **A3: row cache** | `--ple-row-cache` bump (default 1<<20 rows ≈ 170 MB). Lifts slow boundary-read mode further under mmap. Bit-exact. | N/A |
| **B5: smarter default** | NVMe + slow probe → default mmap on; SATA keeps off. Reuse `on_rotational_disk` (generate.cpp:6174). Optional 256-row probe batches (now 64). Code complement to A1. | N/A |
| **F7AB: O_DIRECT boot** | N/A | Boot fills only: O_DIRECT pread into F6 pinned halves (alignment via `read_direct` windowing). Fallback to page-cache on open/alignment failure. Offset-sorted order (admission rank-order unchanged). |
| **Conflict/overlap** | Distinct I/O stages do not establish a new premise after null evidence. No default flip or automatic campaign follows. **CPU:** parser/probe/alignment fixtures; **GPU:** engaged byte/state equivalence and matched boot/PP tests only if re-eligible. | Old page-cache-warmth hypothesis remains history; actual RAM-arena helper fill bypasses this read mechanism. |

### 3.9 Mechanism: Decode Config Sweep (gfx906 Switches)
**Master ID:** `MECH-DECODE-SWEEP`  
**Source IDs:** `ROAD-A2`, `TG-S3` (attention lane-cell)

**Disposition:** the A2 rows/forecasts below are Nov-15 history, followed by no-adoption/null evidence in TG L26. Recheck each implementation's applicability before any renewed shortlist; an empty default table proves neither eligibility nor speed. TG-S3 is a separately identified attention path, not evidence reviving the A2 bundle. All kernel/engine parity and interleaved boot tests in this table require **GPU**.

| Item | Switch | Claimed gfx1151 gain | gfx906 status | Validation |
|------|--------|---------------------|---------------|------------|
| A2.1 | `STRATA_Q8_PACKED=1` | +2.3% | Empty defaults table (arch_defaults.cpp:63); applicability open | Env-only, sha ×3 + KL 0.00000 |
| A2.2 | `STRATA_TSUM=1` | — | Empty defaults table; applicability open | Same |
| A2.3 | `STRATA_SH_STREAM=1` | — | Empty defaults table; applicability open | Same |
| A2.4 | `STRATA_LFUSE=1` | — | Empty defaults table; applicability open | Same |
| A2.5 | `STRATA_PLE_BATCH=1` | — | Empty defaults table; applicability open | Same |
| A2.6 | `STRATA_MMVF_ROWS=1` | — | Empty defaults table; MMVF 42.2 tok/s outlier did not reproduce (18bp) | Same |
| **TG-S3** | `STRATA_ATTN_LANECELL` | — | In-tree gfx906 variant at `qsa_decode_attn.cu:919,966-1014`; local int8 KV path | Engagement + exact intermediate/output equivalence; no swizzle/reduce-order tolerance |
| Historical bundle forecast | — | — | Nov-15 +2–5% warm TG, not a current target | No automatic repeat of the sweep |

**Correction:** ROAD L19's “likely inert” assessment is insufficient for `STRATA_ATTN_LANECELL`: later TG-S3 identifies a gfx906 path. Applicability/equivalence/benefit remain unproved. Do not extend this correction to MERGE_V2/EXPDEC or conflate it with the different prior A2 knobs.

### 3.10 Mechanism: HC Swap Fix + Verify Retry + Floor Safety
**Master ID:** `MECH-HC-SWAP-RELIABILITY`  
**Source IDs:** `ROAD-B1`, `ROAD-B2`, `ROAD-B3`

| Item | Description | Impact | Validation |
|------|-------------|--------|------------|
| B1 | Historical static diagnosis: ignored `hc_bf16_view_pair` failure may pass null `w_down`; scratch selected with call-time `cudaGetDevice()` may pair wrong device/stream; cls=1 may miss wait/release. Anchors layer.cpp:1595/1620, native_dense.cpp:577–647. Source proposed `84fc595dd9` and +210 slots. | Static findings unconfirmed here; HC_Q8-dependent capacity path is closed, not a landing instruction or established speed gain | **GPU, if independently justified:** historical 4× failing teacher-forced repro + 3× fixed checks, trace and exact continuation. **CPU:** safe error/ownership fixtures where possible. |
| B2 | Proposed one bounded re-capture/re-launch only after waits successfully release and input/state can be restored exactly (verify.cpp:587,3504–3560). A wedged stream is not a retryable timeout. | Reliability hypothesis; no new measured availability rate | **GPU:** `STRATA_TEST_VERIFY_STALL`, assert one clean retry, no duplicate commit/emission and identical continuation; failed release aborts to operator. |
| B3 | Proposed explicit refusal under the historical 512 MiB warning floor (generate.cpp:9638–9679, `serve own_fits`); source records NaNs at 128 MiB reserves. | Opt-in safety proposal; not permission to change a default or reduce capacity | **CPU:** budget/refusal fixtures. **GPU, only in an approved safe trial:** clean refusal and no NaN; preserve production reserves. |

### 3.11 Mechanism: Checkpoint I/O Off Read Path
**Master ID:** `MECH-CHECKPOINT-IO`  
**Source IDs:** `ROAD-B4`

| Aspect | Detail |
|--------|--------|
| Problem | Per-stage `cudaDeviceSynchronize` + host copy at every 16,384 boundary (generate.cpp:15295-15318); killer-class stalls at exact 16,384 multiples under RAM pressure |
| Fix | Async copy-stream + event, or defer to idle gap. Bit-exact if same state captured. |
| Impact | Removes boundary stall mode (32,768/180,224 freezes); worst-case fill walls improve |
| Validation | **CPU:** metadata, partial-state, cancellation and error-path fixtures. **GPU:** `STRATA_TRACE` timings and exact saved/restored state/continuation equivalence. An async copy must own a stable snapshot until its event completes; no racing reuse or changed checkpoint boundary. Restart recovery is a separate correctness gate, not implied by faster saving. |

### 3.12 Mechanism: PP Prefill — Routed Stream, Capacity Placement, Dequant Side
**Master ID:** `MECH-PP-PREFILL`  
**Source IDs:** `PP-P0`, `PP-A1`, `PP-B1`, `PP-B2` (`PP-DEQUANT`)

| Item | Description | Key Constraints |
|------|-------------|-----------------|
| PP-P0 | Historical design: Boot 1 timing+trace, `prefill_judge ×8`; Boot 2 adds routed stream depth 32 and drop counts. Price wall vs GPU timeline, wait/copy/grouping/dequant/GEMM and observer effects before choosing a lever. | **GPU**, parked until separately unparked/authorized; two-boot design is not a current schedule or time estimate |
| PP-A1 | Routed stream depth 16/32/64; unchanged ring slots/consumer walk is the source's equivalence argument. Keep ISSUER on, `COPY_EVENT_EVERY=1`, `COPY_WAIT_DEDUPE` off, no `--peer-device`; only pinned-arena copies droppable, stager copies retained. Historical 15%/47% drop and ~2 s forecast need current measured counts. | **GPU:** engagement, exact copy/consumer evidence, PP/TG and memory controls; contract alone is insufficient |
| PP-B1 | Separate budgets: host placement bounded by current MemAvailable and page-cache starvation knee; document `complement_pin_limit_` before altering relief-hot. VRAM growth must name a safe byte saving first; historical ~220 MiB free was not headroom. No context/KV/precision/model/request-budget reduction is allowed as the “shrink.” | **CPU:** current layout/budget audit first. **GPU:** applicable placement trial only if it fits without capacity loss; historic ~16 GiB room is not a current allowance |
| PP-B2 (DEQUANT_SIDE) | Existing side-stream dequant and `gemm_dequant_side_parity.cu`; no local benefit established. **GPU parity, lanes unparked, fresh GO** first. If any relevant exact-equivalence mismatch remains, candidate stays blocked; rounded KL zero cannot rescue it. If equivalent, execute engaged campaign quality checks and matched PP/TG/memory trials. If GEMM dominates instead, `MMQ_GROUP`/`STAGE_GRP` constexpr tuning needs a separate code/build/quality cycle. | No inherited no-KL shortcut or fixed speed floor. `gemm.cu` means `src/prefill/gemm.cu`. |
| Historical gates | Source's no-KL shortcut, ≥24 tok/s floor, old stretch goals and elapsed-time forecasts are retained only in the archive. | Current §1.4 and §8 govern; no relaxed quality tolerance |

---

### 3.13 Mechanism: HC_Q8 Prefill T-Template Fusion
**Master ID:** `MECH-HC_Q8-PREFILL`  
**Source IDs:** `ROAD-C1`

| Aspect | Detail |
|--------|--------|
| **Status** | **Closed/ineligible** under the unchanged zero-sacrifice baseline. The source's “unless HC_Q8-on becomes baseline” clause is history, not an escape permission in this plan. |
| **Mechanism** | Extend `launch_q8` beyond T=8 (fused_gr.cu:2611-2620); wire prefill HC sites (prefill.cpp:4433, 8332). `gr_up_q8_kernel<T>` extends to T≈32; `gr_down_q8_kernel<T>`'s `xs[T][640]` caps T≈16 on 64 KB LDS — down kernel needs a retile. |
| **Hardware/context eligibility** | Historical gfx906 HC_Q8-on proposal, dependent on ROAD-B1. HC_Q8 is off/closed locally. Int8 KV is not the reason for ineligibility; the changed weights/arithmetic and missing quality equivalence are. |
| **Quality requirements** | Source reports the HC_Q8-enable class at KL≈0.019 and proposes KL 0.00000 within an HC_Q8-on baseline with no expected sha equality. That is not equivalence to the current baseline. Different summation/weights remain below the strict bar; no baseline substitution is authorized. |
| **Memory/register/LDS constraints** | Down kernel LDS: `xs[T][640]` → T≤16 on 64 KB LDS (gfx906). Retile required for T>16. Up kernel extends to T≈32. Register pressure increases with T; occupancy must be verified per T. |
| **Validation/disposition** | No live implementation/test queue. Historical **GPU** proposal: teacher-forced comparison within HC_Q8-on, kernel parity, occupancy/LDS audit and PP timing; original effort L and forecast +2–3% PP remain in ROAD L39. They do not meet current equivalence. Preserve the disabled baseline; no new flag or rollback command is invented here. |

### 3.14 Mechanism: Bit-Exact GPU Expert Contract
**Master ID:** `MECH-GPU-EXPERT-CONTRACT`  
**Source IDs:** `ROAD-C2`

| Aspect | Detail |
|--------|--------|
| **Status** | Unvalidated determinism-enabler proposal. “Bit-exact by construction” is the source's intended contract, not an established property. No current performance claim. |
| **Mechanism** | Kill temp-0 placement nondeterminism to enable sha-gating: (1) Pin CPU kernel set — AVX-512 vs AVX-2 vs ggml round differently (native_expert.cpp:397 #2215); lone-token vs group (#152, 351-368). (2) Extend helper-process `cpu_order` coverage (only set at generate.cpp:10143). (3) Placement-pin mode (#463, generate.cpp:22434-22436). |
| **Hardware/context eligibility** | Audit actual HIP/CUDA/CPU/helper dispatch and lone-token/grouped cases. Anchors: s2_expert_grouped.cu:432–479, expert_source.cpp:5646–5673, native_expert.cpp:351–397 and helper env plumbing. Cross-backend compatibility is not yet established. |
| **Quality requirements** | Pin arithmetic/reduction order as well as placement. Source's cold ×5 + warm ×5 temp-0 sha identity and KL 0.00000 vs a pinned-CPU oracle are proposed tests, not sufficient proof alone: exact relevant operations/logits or a credible unchanged-arithmetic proof plus engaged tests remain mandatory. No no-KL shortcut. |
| **Memory/performance constraints** | Measure actual added state/register/LDS/allocator cost; do not promise zero. Historical source forecasts neutral performance or −1…−3% CPU-expert decode unless GPU order matches the fast CPU tree. Determinism work must not silently change the baseline arithmetic. |
| **Falsification/rollback** | If arithmetic cannot be matched, repeatability fails or end-to-end performance/capacity regresses, leave blocked. Proposed implementation must be default-off with a documented rollback; the source supplies no names for new gates, so none are invented here. |
| **Validation** | **CPU:** kernel-selection, group ordering and helper-policy fixtures plus slice benchmarks. **GPU:** exact intermediate/logit and cross-placement tests; historical cold ×5/warm ×5 sequence/sha diagnostics and teacher-forced campaign checks. Establish same-build A/A variation first; it is not a quality tolerance. |

### 3.15 Mechanism: gfx906 Attention Lane-Cell Variant (TG-S3)
**Master ID:** `MECH-ATTN-LANECELL`  
**Source IDs:** `TG-S3`, `ROAD-A2` (attention applicability correction, not the MMVF subcase)

| Aspect | Detail |
|--------|--------|
| **Status** | 🟡 Unvalidated — applicability **OPEN** (not "likely inert"). In-tree gfx906 variant exists at `qsa_decode_attn.cu:919,966-1014` with local int8 KV path. Roadmap L19 incorrectly labeled as "likely inert" (wave32-only, verify_kernels.cu:832). |
| **Mechanism** | `STRATA_ATTN_LANECELL` enables lane-cell attention path for gfx906. Architecture-matched source lead; unpriced. Prior A2 knobs (`STRATA_Q8_PACKED`, `STRATA_TSUM`, `STRATA_SH_STREAM`, `STRATA_LFUSE`, `STRATA_PLE_BATCH`, `STRATA_MMVF_ROWS`) were different mechanisms with null history on this box. |
| **Hardware/context eligibility** | gfx906 only; requires local int8 KV path; warm TG path where attention is a **measured cost** (S3 prerequisite: attention must be priced in TG-S1 attribution first). |
| **Quality requirements** | **Engagement + exact intermediate/output equivalence** through actual int8-KV and context cases. Query-swizzle/reduce-order variants remain blocked without exact equivalence; no tolerance substitution. A source comment claiming bit-identical attention scores is not local end-to-end equivalence evidence ([TG review](../rpcprof/gpu-rounds/tg-optimization-review.md), original L18). |
| **Memory/register/LDS constraints** | Verify launch geometry, registers/shared memory and occupancy — not just allocator bytes. No KV precision/capacity reduction. |
| **Falsification/rollback** | Falsify if: attention is not a relevant production slice (TG-S1), any quality mismatch, or no bracket-separated benefit. Rollback: env off (`STRATA_ATTN_LANECELL=0`). |
| **Validation** | **GPU:** TG-S1 attribution, lane-cell engagement, exact intermediate/output equivalence for actual int8-KV/context cases, launch/register/LDS/occupancy audit, same-build A/A and bracketed TG/PP/memory tests. No specific new test implementation or executed parity result is claimed. |

---

## 4. Status / Prior-Work Ledger (Dispositions with Evidence)

| Item | Disposition | Evidence / Reason | Source Ref |
|------|-------------|-------------------|------------|
| `STRATA_PREFILL_MMQ=0` | ✅ Adopted | edf2af2489: OFF +7% PP, +12% TG vs ON; ON had slow tail | TG-PW-MMQ |
| Split 30, DFlash block 7, foresight 2/8, reserves 192/156, MMVF, CPU affinity, disk placement | ✅ Adopted config (historical) | 18ac/18ao/18ae/18at-av/18br/18bh/18cq; MMVF 42.2 tok/s outlier did not reproduce (18bp) | TG-PW-SPLIT |
| F2 xq ring | Performance-null, off | 18cp: BASE 25.65 vs RING 25.55 tok/s; earlier gain was drift + launcher-env engagement error. Parity 16/16 and repaired test 7b3da65233 do not establish gain. No source comparison proves that this measured an older design. | TG-PW-F2 |
| F3 CPU-pool from ring | ❌ Not landed | No `STRATA_CPU_POOL_XQ` in pinned tree; ring carrier has no demonstrated single-stream benefit | TG-PW-F3 |
| F5 PLE gather overlap (original) | ❌ Not landed | 18bi review rejected original ownership/threading design; no `STRATA_PLE_ASYNC_GATHER` in tree | TG-PW-F5 |
| F4 speculative prefetch | Not landed, conditional | Price residual wait and verify protocol/ownership before shortlisting | TG-PW-F4 |
| F6 queued/parallel boot fill | 🟢 Landed, off in prod | 18bt, afc2ad38ff/e9593357b0; boot-only, not TG optimization | TG-PW-F6 |
| F8 mapped penalty staging | 🟢 Landed, off | penalty_hist_stage.hpp:40; neutral penalties don't exercise intended path | TG-PW-F8 |
| F1 in-graph sampler | 🟢 Landed, off | sampler_mapped.hpp:40, verify.cpp:2846-2852; 18ca invalidated earlier 18bu engagement claim via config `exe` bug. Registered tests ≠ evidence they ran. | TG-PW-F1 |
| F7c timing attribution | 🟢 Landed, incomplete | Units fix 9d5cb763f4/18bx; 18cd still saw zero-valued helper fields | TG-PW-F7C |
| F7ab O_DIRECT/offset sort | 🟢 Landed, inactive | dd0c0299cb/8f1ad4adb1, 18cd; helper fills from RAM arena | TG-PW-F7AB |
| Prefill warm + swap-in depth 128 | 🔴 Regressed/reverted | 18cq: combined PP regression ~19%; depth alone not independently tested. Keep default 64. | TG-PW-WARM |
| Prefill read/compute overlap | ✅ Already present | 18cs, prefill.cpp:4040-4077 and default issuer thread. Not a new optimization. | TG-PW-OVERLAP |
| `STRATA_DEQUANT_SIDE` | 🟡 Unvalidated | Existing parity test at gemm.cu:642 is not local TG evidence | TG-PW-DEQUANT |
| Acceptance chasing / adaptive drafting | ❌ Rejected | 18z/18am/18ao: acceptance rate alone did not predict TG | TG-PW-ADAPTIVE |
| PLE mmap/row-cache, CPU KV, queue priming, IQ threshold, A2 knobs | ⚪ No adoption/null | 18bc/18bv/18an/18bw/18bq/18bo-bp; do not relabel as new | TG-PW-MISC |
| HC_Q8 native/inject/trim | ❌ Closed/off | 18aq-18ax/18ba | TG-PW-HCQ8 |
| Same-build slow/fast serving modes | ❓ Unresolved | 18bm/18bn: ~15–22 vs 27–31 tok/s; helper fill class didn't explain, queue priming didn't fix | TG-PW-SLOWFAST |
| `--ple-io mmap` / `STRATA_PLE_AUTO_MMAP=1` | Historical, later no adoption/null | TG L26 governs over ROAD L18's earlier forecast; no surviving fill-wall gain is established here | ROAD-A1 |
| gfx906 decode switches sweep | Historical, later no adoption/null | TG L26; TG-S3 is a separately identified path, not revival of the sweep | ROAD-A2 |
| `--ple-row-cache` bump | Historical, later no adoption/null | Changed premise needed before renewed work | ROAD-A3 |
| `STRATA_HIP_PROMPT_F16=1` audit | ⚠️ Decision item | LIVE in prod, NOT bit-identical (gemm.cu:596-603). Never gate-measured. Baselines include it. | ROAD-A4 |
| Refuted items (--kv-grow, DENSE_MMQ, etc.) | ❌ Rejected/Closed | Compile-time impossible / measured loss / exhausted | ROAD-A5 |
| HC-swap wedge root-cause (B1) | Historical static diagnosis | Not independently reproduced here; HC_Q8-dependent path is closed; no old-commit landing instruction | ROAD-B1 |
| #267 verify-timeout retry (B2) | 🟡 Unvalidated | Bounded re-capture; bit-exact | ROAD-B2 |
| Fail loudly under 512 MiB (B3) | 🟡 Unvalidated | Control flow only | ROAD-B3 |
| Checkpoint save off read path (B4) | 🟡 Unvalidated | Async copy-stream + event; bit-exact if same state | ROAD-B4 |
| Smarter PLE I/O default (B5) | Blocked | Missing A1 benefit evidence; default change needs separate operator scope/decision | ROAD-B5 |
| gfx906 queue-priming (B6) | Historical, later no adoption/null | TG L26; no automatic repeat after inactive F7ab | ROAD-B6 |
| HC_Q8 prefill T-fusion (C1) | Closed/ineligible | KL≈0.019 class is not current-baseline equivalence; no rebaseline escape permission | ROAD-C1 |
| Bit-exact GPU expert contract (C2) | Unvalidated enabler | Exact operations/logits, eligibility and memory/performance cost need proof | ROAD-C2 |
| gfx906 LDS/wave64 audit (C3) | 🟡 Unvalidated | Per-kernel bit-exact required | ROAD-C3 |
| CODE-F1..F8 (Nov-15 plan) | Mixed; not a new work queue | Latest individual rows govern: F1/F6/F8 landed off, F7c landed incomplete, F7ab landed inactive, F2 null/off, F3/F4/F5 not landed | TG L13–21 |

**Critical Baseline Confounds (Historical/Superseded — Retained for Audit):**
- **Nov-6 pinning fault:** 64 `strata-stage` threads + watchdog/issue/fill parked on CPU 0 on ~96.5% of boots since Nov 6. Fixed by `STRATA_AUX_CPUS=3,5,7,9,11,13,15` (adopted in prod config env). **All baselines since Nov 6 measured with fault live** — 422-671 MB/s fill band, 432-455/352-379 PP classes, 123 ms/window WT denominator, 2.7-5.3 ms host-quantize tax are **placeholders**.
- **Post-16:21 warm-TG collapse:** Warm TG below 26-34 in-band on EVERY boot/config/binary since 16:21 (collapsed band 18.2-23.7; last in-band A1MMAP-1/2/3 at 15:49-16:08). Placement verified perfect (AUXCPU1 judged warm TG 19.5). Persistent box-state change (GPU under-load clocks pending; PCIe link-width under load; A1MMAP4 config differential). Post-reboot PRODCHECK2 read warm TG 22.1 — collapse survives reboot.
- **Historical Nov-15 re-baselining rule (CODE L63–76):** F7c first, speed-independent KL boots “NOW,” and tg80k/WT/BT acceptance held until warm-band restoration. These are archived instructions, not current authorization. No release of the old hold is established here. A future manifest must explicitly resolve its applicability, use fresh class bands/stable same-day controls and obtain fresh operator GO; it cannot copy old speed thresholds or treat landed code as new work.

**Historical Relaxed Criteria (Superseded — Retained for Audit):**
- The earlier relaxed numerical threshold is revoked. Current rule is §1.4's exact relevant equivalence or credible unchanged-arithmetic proof plus executed engaged tests, retaining teacher-forced KL 0.00000/no-NaN campaign checks. Rounded zero alone is not an alternate pass.
- speed-plan-v2 "−20% is stretch, not commitment" and fixed throughput floor — superseded by serving-plan-current.md (operator).
- R²/subtraction estimates (e.g., `prompt_ms = 1338 + 3.74×pcie_fetch`, R²=0.969) and overlapping timer counters are **hypotheses, not profiler decompositions**. No new made-up percentages/time promises.

---

## 5. Explicit Conflict Resolution

### 5.1 F2 chronology — old amendment does not overturn later null evidence
| Source | Statement | Date | Disposition |
|--------|-----------|------|-------------|
| CODE-F2 + review | Tail publish preserves folded/qdedup ordering; depth-2 ring, per-window seq reset, QFUSE parity and helper/allocation gates are requirements in the amended proposal. | Nov 15 | Preserve full design at CODE L209–261; it precedes the null result. |
| TG prior work | F2 performance-null/off, BASE 25.65 vs RING 25.55 tok/s; earlier gain was drift plus launcher engagement error. | Nov-18 chapter, L13 | Later disposition governs; no established gain. |
| **Resolution** | The measured implementation has not been compared with the amended plan. It is unknown whether either differed. Do not assert that the null used the unsafe reorder, lacked QFUSE parity, or tested a design superseded after the result. F2/F3 remain unshortlisted without a demonstrated changed premise; F3 depends on F2 but is not scheduled. | | |

### 5.2 Conflict: F5 PLE Gather — Original Design vs. Review Redesign
| Source | Statement | Date | Disposition |
|--------|-----------|------|-------------|
| code-opt-plan (original) | Side thread raises `m_flag_` early, skips host k==0 raise. Overlaps gather with GPU wait. | 2026-11-15 (initial) | **UNSOUND — structurally flawed.** |
| code-opt-plan-review | Original design breaks doorbell graph: `m_flag_>=ring` at :2460/2466 gates CPU-rows copy; flagB lands mid-pool → GPU copies mid-write = silent corruption. Schedule-dependent; qq KL gate CANNOT certify absence. | 2026-11-15 (review) | **Blocking finding.** |
| code-opt-plan (amended) | Redesigned: `PleGather` RAII thread started after `stage_inputs`, joined after `pool()` before unchanged `*flag=want`. Flag protocol byte-for-byte unchanged. Win bounded by `ms_pool`. ar_on() variant separate. | 2026-11-15 (amended) | **Resolved structurally.** Original design discarded, not patched. |
| TG-chapter | F5 not landed; review rejected original ownership/threading design. | 2026-11-18 | Consistent. |
| **Resolution** | Original design rejected structurally. Amended design retained as a conditional proposal, **not landed or shortlisted** until ownership and current residual cost are proved. Review approval of its shape is not implementation or runtime proof. | | |

### 5.3 Conflict: F4 Prefetch — True-Row vs. Weight/Blob Pre-Stage
| Source | Statement | Date | Disposition |
|--------|-----------|------|-------------|
| code-opt-analysis (implied) | Step (a): helper computes candidate rows into scratch ring at layer l service for layer l+1. | Pre-plan | **Non-constructible.** |
| code-opt-plan (amended) | `proxy_begin` needs real current activations; predicted rows are not valid. Proposed weight/blob pre-stage computes only at real begin. | Nov 15 | Design refinement, not landed code. |
| TG-chapter (S1) | Attribution first: current fields don't establish full critical-path decomposition; zero helper timing fields merit investigation. | 2026-11-18 | **Prerequisite.** F4 helper half value depends on residual wait measurement. |
| code-opt-plan-review | Helper serve-thread serialization + ack-drain point are blocking design gaps. Amended: handler acks fast, stages async or rides socket 2; `finish()` consumes leading acks. | 2026-11-15 (review) | **Resolved in amended plan.** |
| **Resolution** | True-row prefetch rejected as non-constructible. Weight/blob design remains conditional and not landed. TG-S1 must first price the actual residual chain; F2/F5 are not automatically resumed. Preserve async/second-socket staging, leading-ack drain, immutable keys, scratch/demand-slot budgets and separable PCIe half. | | |

### 5.4 PLE/boot I/O — distinct mechanisms, but later null/inactive status still governs
| Source | Statement | Date | Disposition |
|--------|-----------|------|-------------|
| ROAD-A1/A3/B5 | Mmap, row cache and a smarter default were proposed with boot/boundary forecasts. | Nov 15 | Preserve proposals/forecasts as history, not present benefit. |
| TG-PW-MISC | PLE mmap/row-cache and queue priming have no-adoption/null history. | Nov-18 TG L26 | Do not narrow that evidence into an unproved surviving speed claim. |
| CODE-F7AB / later TG | O_DIRECT/sort boot design differs from PLE I/O; later code is landed but inactive on the helper's RAM-arena fill source. | CODE L334–364; TG L21 | No engaged current-source campaign is implied. |
| **Resolution** | Keep mechanism distinctions and all historical tests, but keep ROAD-A1/A3/B6 and F7ab out of the live queue without a changed premise/applicable source. B5 additionally needs valid benefit evidence and a separate default-change decision. No automatic “F7ab falsified → B6 next” schedule. | | |

### 5.5 Conflict: DEQUANT_SIDE — PP Plan vs. TG Chapter vs. Code Plan
| Source | Statement | Date | Disposition |
|--------|-----------|------|-------------|
| speed-plan-v2 (PP-B2) | Existing side-stream dequant and GPU parity test are PP levers. Original no-KL shortcut is historical. | Nov 17 | Parked/unvalidated; requires separate authorization and current exact-equivalence gate. |
| TG-chapter (prior work) | `STRATA_DEQUANT_SIDE`: unvalidated, PP-shaped. Existing parity test not local TG evidence. | 2026-11-18 | Accurate: not a TG optimization. |
| code-opt-plan | Not directly listed (F1-F8 focus). Implicitly part of PP program. | 2026-11-15 | No conflict. |
| **Resolution** | One PP mechanism with two source references. GPU parity with lanes unparked must establish exact equivalence; mismatch leaves it blocked. Follow §1.4/§8, not the old contract-only/no-KL shortcut. An existing test is not evidence it ran. | | |

### 5.6 Conflict: Sampler (CODE-F1) vs. TG-S2 Revalidation
| Source | Statement | Date | Disposition |
|--------|-----------|------|-------------|
| CODE-F1 | Landed default-off; parity test exists; qq KL gate; judges leg optional but HELD per Re-baselining. | 2026-11-15 (amended) | Live candidate. |
| TG-S2 | Revalidate landed F1 on eligible request class. Evidence: source hypothesis only. Greedy head already in-graph; temperature-zero panel didn't test sampled/penalized path. Production sampling settings must be verified. | 2026-11-18 | **Not a duplicate — a validation requirement.** TG-S2 mandates eligibility verification before asserting relevance. |
| **Resolution** | CODE-F1 is the implementation; TG-S2 is the **validation scope** (eligibility gates, non-vacuous tests, production sampling settings). Both retained. TG-S2's "env flag alone proves no engagement" (review note) is a hardening requirement for CODE-F1's validation ladder. | | |

### 5.7 Conflict: Historical Throughput Floors / Forecasts
| Source | Statement | Date | Disposition |
|--------|-----------|------|-------------|
| speed-plan-v2 | "−20% is stretch, not commitment"; fixed throughput floor; contract-only gate shortcuts. | 2026-11-17 | **Superseded** by operator (serving-plan-current.md). |
| TG-chapter | No percentage gains or invented time estimates proposed. | 2026-11-18 | Consistent with current policy. |
| code-opt-plan | Old WT%/BT% assumptions were confounded by the pinning fault; judge legs held for band restoration. | Nov 15 | Historical baseline condition retained; no release established, no old target copied into current acceptance. |
| **Resolution** | Old floors, forecasts, P0–P3 order and Day 1–5 calendar are historical. No current gain/time promise follows. Historical warm-band holds are retained without asserting release; future manifests need explicit disposition plus fresh, stable same-day controls and fresh operator GO. | | |

### 5.8 Conflict: Profiler Observer Effects
| Source | Statement | Date | Disposition |
|--------|-----------|------|-------------|
| TG-chapter | `STRATA_VERIFY_PROFILE=1` disables `df_branch`, conv/AB fusion, `sh_fork` at verify.cpp:1555,1888,2369. Decomposition not automatically production's. | 2026-11-18 | Accurate. |
| TG-review | Additional guarded sites: verify.cpp:2590,2623,2643 (wait/copy, rows, hit-combine fusion); profiling also affects stamp/branch at 1770. No profiler stage total may be presented as unqualified production budget. | 2026-11-18 | **Expands** chapter's list. |
| **Resolution** | Consolidated profiler observer effects list in Verification Matrix (§8). All profiler-on measurements require explicit observer-effect controls and profile-off/profile-on bracketing. | | |

---

## 6. Proposed phases and dependencies — not an execution authorization

### 6.1 Evidence-led phases
| Phase | Deliverable / exit condition | Permission boundary |
|---|---|---|
| PREP | Whole-program upstream + external applicability ledger (§7), versioned source/quality risks, budgets and genuinely CPU-only fixtures | Read-only research/preparation; no GPU traffic or process control |
| MEAS | TG-S1 exercised-path counters and observer-effect controls; TG-S4 compatible fixed traces/CPU slice benches; PP-P0 design updated if PP lanes are unparked | Repair existing incomplete F7c, not “land it again.” Device traces, profiling and PP probes require separate authorization and fresh GO for downtime. |
| ONE CANDIDATE | Measured removable cost, applicable implementation, explicit byte/arithmetic/ownership proof, engaged quality tests, bounded memory budget and rollback | No candidate progresses solely because it appears in a historical plan; old null/rejected/inactive status must be resolved by evidence first |
| VERIFY | Same-day bracketed/interleaved A/A and candidate legs, warmup/repeats/spread, all memory metrics, whole-request/PP/TG/stop/restore non-regression | Parent-only approved window, no active specialist; stable baseline and documented historical-hold disposition |
| CLOSE | Exact artifacts/exit statuses, rejection or supported result, canonical restore/serving verification, limits and unanswered questions | Operator alone decides adoption, promotion or any default change |

The TG shortlist is S1/S4 measurement work, then eligible F1/S2 revalidation and attention/S3 **only if its slice is priced**. F6 is boot-only; F8 needs non-neutral penalties; F7ab is inactive on the present fill source. None is a new implementation merely because the Nov-15 schedule called it P0/P2. PP branches remain parked unless separately unparked; no hidden PP/GPU sweep is inserted into TG work.

### 6.2 Dependencies retained without reviving blocked work
| Relationship | Current interpretation |
|---|---|
| F3 depends on F2's ring | Historical hard code dependency; both unshortlisted without a demonstrated changed premise after the later F2 null |
| F5 after F2/F3 | Historical `verify.cpp` merge-conflict avoidance only, not functional dependency; ownership and measured residual-cost proof still needed |
| F4 helper half → F2/protocol work; PCIe half → F7c/thrash evidence | Preserve original design dependencies. Do not implement null F2 just to satisfy them. Re-evaluate current residual wait first; retain separable PCIe half and stop/descope threshold from §3.4. |
| F6/F7ab composition | Historical independent pipeline/parallel/O_DIRECT/sort matrix; only meaningful on a demonstrably applicable read/fill source |
| ROAD-B5 → A1 benefit + operator default decision | No benefit is presently established; no automatic default flip |
| ROAD-C1 → HC_Q8-on/B1 | Closed under the current quality baseline; no implementation branch authorized |
| TG-S1/S4 → candidate claims | Correct timer ownership, exercised paths and valid trace controls before causal claims; CPU slice speed is not whole-window speed |

**HISTORICAL/NOV15:** CODE L45–76's P0–P3 ordering and warm-band hold, and L432–463's Day 1–5 first-week schedule, are archived at §11.3. They do not authorize current work, repeat already landed items, or schedule F2 then F3. Their “KL boots NOW” wording is not current permission. No evidence here establishes release of the historical judge hold; a future run must explicitly address it as well as satisfy stable same-day controls and fresh operator GO. Original forecasts/effort/boot counts remain history, not new time or speed commitments.

### 6.3 Current-operations annex — separate from the broad program
| Order | Required preparation/evidence | Gate |
|---|---|---|
| Parking restart correctness first | Implement/review fix, then park → restart → continue with checkpoint reuse evidence and exact continuation/state checks | No deployment or live restart without fresh GO |
| Long-conversation margin retest | Each arm (256/448 vs 700/700) is **one conversation**: ~80k new infill → 2048 TG → append another ~80k new user message → 2048 TG. Record PP/TG/turnaround, memory and parking evidence. | After parking correctness; exact runner/manifest and separate fresh downtime GO |
| Complete expert-locking investigation/test | Expert weights held in **system RAM** only; GPU weights remain in VRAM; current arena already locked. Audit every intended expert mapping, duplicated/full-shard coverage, fit and failures before claiming completeness. **No PLE-table mlock; no blanket `mlockall()`.** Not all model experts are claimed RAM-resident. | Reuse 18w/18x/`660d9f0dc7` (4608 spans, 46.84 GiB, 128 s historical result). Remove operator-requested RAM caps in test arms only; no default/prod change. Live arms need fresh GO. |
| rocBLAS/gfx906 investigation | Actual GEMM shapes, dispatch/tuned-kernel coverage and source-backed opportunities across the inference program, not TG-only; preserve ROCm 6.1/custom xnack constraints | Read-only research first, separately approved runtime/build comparison; no automatic library promotion |

---

## 7. Program-Wide Upstream + External Research Workstream

### 7.1 Research Principles
- **Upstream speed claims are NEVER local measurements.** Local equivalence must be proven.
- Record provenance: URL, commit SHA, date, status (merged/open/closed), backend (HIP/CUDA/SYCL/Metal), model applicability, local equivalence check result.
- No blanket cherry-picks. Each lead gets a dedicated applicability check tied to a measured bottleneck.
- External/web research for relevant papers, vendor docs, competitor techniques — scoped to each mechanism.

### 7.2 Existing TG upstream leads — preserved, not a complete research campaign

Full URLs, full commit/head SHAs and original dates/status claims are preserved inside [TG snapshot](#snapshot-tg), original L60–78. Supporting TG review confirms sampled identities/status/presence **as of Nov 18**, not current compatibility or performance. Check functional equivalents locally, not ancestry alone; push target remains `fork`, never upstream.

| Lead | Pinned shorthand / original TG line | Applicability and required disposition |
|---|---|---|
| Fixed real decode/verify trace | `d486fbe7d6`, L66 | Missing native forcing facility needs a minimal compatible port; tool, not speed win; prove real-path engagement |
| CPU decode-window pool benchmark | `b94ab5f4c6`, L67 | Missing facility; prices CPU slice only, not whole-window critical path |
| Pool-row bench/load order | `8fdf701f88`, `da5e338422`, `d3851f1043`, L68 | Separate benchmark port from arithmetic/load-order optimization and exactness proof |
| Cost-based draft windows | `4079483b6d`, `15a4be9c7a`, L69 | Not shortlisted: DFlash overrides chooser with `df_pending_k+1`; failed adaptive history retained |
| Drop window finishing after STOP | `33648fef1e`, L70 | Whole-request/cancel-tail lead; not headline cached TG and not equivalent to F4 prefetch |
| Ancestor cgroup CPU quota | `9e52144fc9`, L71 | Only applicable if a binding nested quota exists; do not invent one |
| NUMA placement | `716ebae14e`, L72 | Analyst found one node; no local gain assumed, recheck topology if deployment changes |
| Generic decode attention loops | `78f5417195`, `38ab10e03e`, L73 | Compare existing gfx906-specific implementations, arithmetic and actual int8-KV engagement |
| HC_Q8 prefill vectorization | PR 3220, head `67d3b105a3`, L74 | Closed/ineligible with HC_Q8 off under unchanged baseline; preserve as history, no rebaseline permission |
| NaN-preserving BF16 | PR 2955, head `c52d60b3`, L75 | Equivalent correctness work reported in v0233.2/18am; inspect equivalence, not a TG gain claim |
| Long-prompt MTP borrowing | PR 3307, head `41bff1d3d0`, L76 | PP/admission lead only; no capacity/quality reduction or established TG mechanism |

### 7.3 Dedicated upstream AND external research across the whole program

Every row requires **both** an upstream implementation/history search and an independent external/vendor/paper search. The existing TG list is a starting point, not completion of these domains. These are research questions and planned deliverables, not findings or permission for experiments.

| Domain | Upstream inspection | Dedicated external research | Local check / deliverable and dependency |
|---|---|---|---|
| Cold and incremental/cached-prefix PP | Prefill issuer/readahead/routed-copy history, chunking, prefix/admission and borrowed workspace | Exact-arithmetic prefill/streaming techniques, sparse-expert scheduling and hardware copy/compute documentation | Compare cold vs prefix-append critical paths, copy/drop counts and cache knees; PP-P0 applicability memo before any parked lever |
| GEMM/dequant/rocBLAS | Engine GEMM/dequant dispatch plus rocBLAS/Tensile source and tuning history for actual shapes | ROCm **6.1**/gfx906 vendor material, Tensile tuning methods and independent exactness/performance reports | Shape/layout/precision/batch map, tuned-kernel gaps, launch/read-bound evidence and isolated candidate test plan; use custom xnack library, not `/opt/rocm` 7.x |
| TG attention and sampler | Generic and gfx906 attention commits, captured sampler/fallback and RNG history | Wave64/LDS/register/occupancy analysis and reproducible sampler/attention parity methods | S1-priced slice, S2 eligibility/RNG fixtures, S3 exact int8-KV/context equivalence; source title or contract alone is insufficient |
| CPU pool/helper/transport | Pool benchmarks/order, quant-image carrier, wire negotiation, staging and helper dispatch | CPU vector/reduction ordering, mapped-host visibility, IPC/socket latency and asynchronous staging literature | Trace begin/finish/pool overlap; immutable byte/ownership proof, CPU-only bench vs full-device distinction; F2/F3 null and F4/F5 prerequisites retained |
| TTFT, continuation and stop/cancel | Request scheduling, draft/verify/commit/emission boundaries, STOP-window handling | Queueing/streaming latency and cancellation-safe speculative-work techniques | End-to-end timeline and stop/cancel-tail fixtures without duplicate output/state commits; separate from cached-TG rate and F4 |
| Checkpoint, park, restart and reload | Checkpoint index/anchor selection, generation compatibility, metadata/state regions and recovery history | Crash-consistent snapshot/index design, asynchronous I/O ownership and prefix-state restore techniques | Correctness-first park→restart→continue plan and saved/restored-state equivalence; distinguish disk lookup failure, partial restore and I/O speed; no assumed format migration |
| Boot/fill and file I/O | F6/F7c/F7ab implementations, actual RAM-arena vs file source, direct-I/O fallback/alignment | Linux page cache/O_DIRECT/readahead and transfer overlap/device-thread affinity documentation | Phase-rate/boot-wall distribution with fresh classes and one fresh fill per boot; inactive/null mechanisms stay inactive absent changed premise |
| Memory placement and expert locking | Arena registration/lock machinery, file-backed expert/shard mappings, residency/pin/cgroup guards | Linux `mlock`/RLIMIT/reclaim/NUMA and HIP host-registration semantics; prior 18w/18x resources | Expert-region byte/ownership/fit ledger, actual locked coverage/failures/time, RSS/PSS/available/swap/VRAM budget. Exclude PLE and blanket locking; no complete-coverage claim until verified. |

### 7.4 Research deliverables per lead
1. **Applicability memo:** Local equivalence (arithmetic, memory layout, launch config), backend support (HIP gfx906), model compatibility (Qwen3.8-Flash-Next UD IQ3_XXS, DFlash, int8 KV).
2. **Bounded patch/dependency plan:** Files, affected backends and tests, measured bottleneck and required tooling; no invented speed or elapsed-time estimate.
3. **Quality/ownership proof plan:** Exact relevant operations/logits or credible unchanged arithmetic plus executed engaged tests; teacher-forced campaign checks remain. If equivalence cannot be established, leave blocked, not “KL fallback.”
4. **Measurement plan:** How to isolate the mechanism's contribution (A/A controls, fixed traces, bracketed profiles).
5. **Decision:** Shortlist / deprioritize / close with reason.

### 7.5 Research schedule — dependency gates, not calendar
- **Gate 1 (PREP):** Every §7.2 lead plus **each domain in §7.3**, upstream and external research, yields a dated applicability memo with full URL/SHA, source status, deployment/backend/model limits, equivalence check, prior rejection and unresolved questions.
- **Gate 2 (MEAS):** Trace/attribution controls (TG-S1, TG-S4, PP-P0, F7c) operational.
- **Gate 3 (CAND-1):** First candidate with exact gates + A/A controls.
- **No invented Week 1-4 forecasts.** Dependency gates only.

---

## 8. Credible Verification Matrix

### 8.1 Test Classification
| Class | Description | Hardware | Pinned Controls |
|-------|-------------|----------|-----------------|
| **Unit/Parity (CPU)** | Host byte/golden comparisons, parsers, fake-helper protocol/socket tests, host-only ownership/budget fixtures and CPU slice benches | CPU only if the fixture neither produces GPU golden data nor initializes a device | Record exact fixture scope; CPU slice speed is not end-to-end speed |
| **Unit/Parity (GPU)** | `expert_cache_batched_test.cpp` (device context), `sampler_mapped_parity.cu`, xq/QFUSE producer parity, PLE HIP, attention/dequant parity and capture/replay | GPU required, including tests named “core” that initialize a device | Same relevant build/inputs/path, explicit engagement and fallback cases |
| **Engine Gate (quality)** | Exact relevant operation/logit equivalence or credible unchanged-arithmetic proof plus executed engaged tests; retain teacher-forced KL 0.00000/no-NaN | GPU required | Same baseline, inputs, variant/effective env and frozen compatible qq config; a rounded KL report is not proof by itself |
| **Engine Gate (Boot/Byte)** | `STRATA_VERIFY_COMPLEMENT` all-slots checksums; `verify_slot` | GPU required | Same build, variant env, gate-on vs gate-off |
| **Judge Legs (Speed)** | `lane_judges.sh`: fill80k + tg80k (ctx 442400) | GPU required | **Same-day, matched pair**, interleaved arms, warmup, repeats, spread |
| **Distribution Campaign** | ≥6/8 interleaved boots, fresh fills, class bands | GPU required | Fresh class bands on adopted config; 18bb lesson (1 fresh fill/boot) |
| **Attribution/Profiling** | `STRATA_VERIFY_PROFILE=1`, F7c fields, helper logs | GPU required | Profile-off vs profile-on bracketed; observer effects quantified |

### 8.2 Sampling Policy & Metrics
| Metric | Collection | Reporting |
|--------|------------|-----------|
| **TG (tok/s)** | Median, full spread, slow samples (min/max), per-window | Medians primary; spread retained; slow samples NOT suppressed |
| **PP (tokens/s, wall)** | Fresh and cached-prefix/append prompt timing, actual new/reused/read tokens | Separate cold/incremental PP, cache reuse, TTFT and whole-request turnaround |
| **Boot/cache fill (bytes/s, slots/s, wall)** | F7c pack-read/H2D/pinned-byte phase evidence plus complete boot/fill wall | Not PP tokens/s. Slot and byte denominators, source and cold/warm state must be named. |
| **TTFT / Turnaround** | Whole-request wall, stop/cancel tail | Separate from TG |
| **Memory** | Engine/helper RSS (VmHWM via clear_refs reset = RSS high-water mark), PSS (sampled separately), system MemAvailable, swap, per-card VRAM (max) | Per-leg `max_rss_mib` (VmHWM), `min_avail_mib`, `max_swap_used_mib`, per-card `max_vram_mib` (ab9_driver.py); PSS reported separately |
| **Cache/Residency** | Page-cache pressure, PLE residency, lock placement | Qualitative + quantitative where measurable |
| **Quality** | Exact relevant operations/logits or unchanged-arithmetic proof plus engaged tests; teacher-forced KL 0.00000/no-NaN campaign checks; top-1/PPL/response sha diagnostics | No rounded-KL, content-hash or top-1-only pass. Same-build A/A nondeterminism is a control, never a quality tolerance. |

Report memory beside **every** speed result, including effective guard/cgroup/RLIMIT and cap-adjusted room, locked/requested/failed bytes, residency and startup lock cost where relevant. Separate engine/helper RSS high-water from sampled PSS; name any missing metric rather than invent it. Record swap residue, page-cache warmth, game/system-RAM load, warnings, GPU clocks/links, CPU placement and effective environment. The operator-requested uncapped locking arms do not change production defaults.

### 8.3 Environment & Hash Discipline
| Artifact | Hash Recorded | Where |
|----------|---------------|-------|
| Engine binary | SHA256 | `build_pin.sh` output, pin manifest |
| Helper binary | SHA256 | Lane scripts |
| Config JSON | SHA256 | Launcher variant (`make_variant.sh`) |
| Launcher script | SHA256 | `lane_boot.sh` logs |
| Effective env | Full env dump | Boot log (`STRATA_*` vars) |
| Input corpus | Frozen qqNIGHT / judge prompts | Harness config |
| Wrapper/helper | SHA256 | `lane_boot.sh` / `lane_judges.sh` |

### 8.4 Stage Timing Ownership & Observer Effects
| Timer | Owner | Overlap Risk | Mitigation |
|-------|-------|--------------|------------|
| `ms_wait` / `ms_pool` / `ms_host` | Verifier service loop | Overlap with GPU work; reset boundaries need review | F7c fields + bracketed profile-off/on |
| `helper_begin_ms` / `helper_finish_ms` | Helper tier / proxy | Suspected reporting-path mismatch (chapter: zero throughout) | TG-S1: wire correct counters |
| `pcie_fetch_ms` | Engine (request-total prompt+decode) | Not a decode percentage | Chapter: do not quote as decode % |
| Profiler (`STRATA_VERIFY_PROFILE=1`) | Verifier | Disables `df_branch`, conv/AB fusion, `sh_fork`, stamp/branch (1555,1770,1888,2369,2590,2623,2643) | Observer-effect controls mandatory; no profiler total as unqualified budget |

### 8.5 Failure/Restore Handling
| Failure Mode | Detection | Restore Procedure | Verification |
|--------------|-----------|-------------------|--------------|
| Recoverable trial error / timeout | Trace and release/stream status, exact failing identity | Abort arm; no later arm silently proceeds. Parent uses on-disk stop and restore scripts within the approved window, separate invocations. | Canonical hashes, fresh ready line, `/v1/models`, identity and real completion |
| NaN / 503 / exactness mismatch | Post-leg scan or engaged proof failure; rounded zero/top-1 agreement does not clear mismatch | Candidate blocked, flag off; preserve failures and restore approved baseline | Baseline/quality recheck and recorded exit status; no automatic promotion |
| Hard GPU/OS hang / failed release | Unresponsive or stream still wedged | Abort/operator incident. No automatic kill/reset/retry experiment. Do not infer permission to create another window. | Operator recovery decision; no claim of successful restore until actually verified |

Never use inline pattern-kills. Stop scripts terminate supervisors by their state files and verify quiet before a separate boot. No locally served specialist may be mid-turn, and every new downtime window still requires fresh explicit operator GO. Historical scripts/config paths and pins must be checked against the current baseline before use.

### 8.6 End-to-End Non-Regression & Serving Verification
- `/v1/models` identity check
- Real `OK` completion on known prompt
- Cold PP + warm TG + boundary re-read legs
- Park/restore cycle (ANNEX)
- Multi-GPU helper + engine health

---

## 9. Source coverage crosswalk — exact archive ranges and current interpretation

Each `L` range below is a **1-based line range in the pinned original source**, not a line in this growing master. The named snapshot is inside this file and contains the full source, including links, numbers, commit slices, tests, risk tables and old instructions. Operative summaries are intentionally shorter; a range mapped to the archive is not a claim its text appears verbatim in an operative card. Byte preservation and semantic reconciliation are separate checks. No invented aggregate “item count” is used.

### 9.1 PP → [§11.1 snapshot](#snapshot-pp)
| Original PP range / item | Interpretation / operative target |
|---|---|
| L1–7: review, parking and supersession notice | §1 provenance; archived notice is not permission to substitute the serving-plan document for this reconciliation |
| L8–15: six facts (regression/common cause; per-slot fetch/GPU/wall; existing overlap; MMQ-off; VRAM; config env precedence) | Historical quantitative facts and exact anchors retained at these archive lines; §3.12 budget/measurement constraints, §4 MMQ/overlap status, §8 effective-env discipline |
| L17–23: Phase 0 precheck (L18), timing boot (L19–20), routed pilot (L21–22), branch (L23) | PP-P0, §3.12, §6.1; original two-boot design retained, parked/not authorized |
| L25–30: routed stream, ceiling, depth and eligibility | PP-A1, §3.12; exact ring/copy constraints retained |
| L31: dropped A2 event-overhead and A3 default-depth proposals | Historical rejected PP levers, not ROAD-A2/A3; original reasons retained at L31, not a live queue |
| L33–40: separate RAM/VRAM placement budgets, complement pin limit, cache starvation | PP-B1, §3.12; current headroom must be remeasured without reducing capacity |
| L41–46: DEQUANT_SIDE parity and constexpr fallback levers | PP-B2/PP-DEQUANT, §3.12 and §5.5; existing GPU test ≠ executed evidence |
| L48–52: three gates, separate instrumentation guard | PP-GATES, §1.4/§8; old no-KL/fixed-floor shortcuts superseded, exact-quality requirement retained; instrumentation change not hidden in numerics arm |
| L54–57: success/stretch goal | Historical forecast retained here, not an operative target |
| L59–61: elapsed-time forecasts | Historical estimates retained here, no current time promise |

### 9.2 TG → [§11.2 snapshot](#snapshot-tg)
| Original TG range / section | Interpretation / operative target |
|---|---|
| L1–7: approval boundary, source/binary pin and related evidence | §1.2/§1.3 and §3 anchor-version rule; no inherited approval or production pin change |
| L9–28: prior-work table | Each data row mapped immediately below; latest disposition in §2.2/§4 |
| L30–34: champion medians/spreads and causal limits | Retained in archive, not converted into a winner. Guard limits 50.0 vs 62.2 GiB, fixed order, swap/VRAM/trace/continuation confounds inform §8 controls. |
| L36–49: exploratory aggregate | All eight statistics (L42–49) and mutable-log/identity/parser limits retained here; **not reproducible accepted evidence**, no invented operative aggregate ID or critical-path budget |
| L51–58: six pinned path-map bullets | DFlash chooser L53, verifier/tail L54, sampler L55, helper transport L56, counter mismatch L57, profiling/observer effects L58; retained at these lines, interpreted in §3.1/§3.4/§8.4 |
| L60–78: upstream repository, 11 leads, full URLs/SHAs and equivalence warning | One row per lead in §7.2, matching original L66–76 in order; all full references remain in this snapshot |
| L80–86: S1 attribution card | TG-S1, §3.4/§3.6/§6.1/§8.4; full evidence/scope/budget/proof/falsify/rollback at L84–86 |
| L88–92: S2 sampler card | TG-S2/MECH-SAMPLER, §3.1; all eligibility/budget/RNG/non-vacuous-test constraints retained |
| L94–98: S3 attention card | TG-S3/MECH-ATTN-LANECELL, §3.15; price slice then exact int8-KV/context equivalence |
| L100–104: S4 trace/CPU-bench card | TG-S4, §6.1/§7/§8; input/tokenizer fidelity, allocations, default-off, real device engagement, A/A and forcing≠correctness constraints retained here |
| L106: not-shortlisted list | §2/§4/§6.2; F2/F3, conditional F4/F5, boot-only F6/F7ab, neutral-penalty F8, failed/closed adaptive/MMQ/HC_Q8 and parked PP levers not silently queued |
| L108–114: five verification steps | §6/§8; offline compatibility and bounded build resources, instrumentation, parent-only observer-controlled profiling, one eligible candidate, close/restore/operator incident |
| L116–124: hard gate and review boundary | §1.4/§8; exactness, A/A, no tolerance fallback, reviewer obligations, low-RAM/restart gate and parked scope retained; approval belongs to the old TG research plan only |

**TG prior-work data rows — actual source row → existing master ID:**
| Original TG line / item | Master ID (§2.2 and §4) |
|---|---|
| L11 MMQ-off | TG-PW-MMQ |
| L12 configuration bundle | TG-PW-SPLIT |
| L13 F2 ring | TG-PW-F2 / CODE-F2 |
| L14 F3 pool | TG-PW-F3 / CODE-F3 |
| L15 F5 gather | TG-PW-F5 / CODE-F5 |
| L16 F4 prefetch | TG-PW-F4 / CODE-F4 |
| L17 F6 boot fill | TG-PW-F6 / CODE-F6 |
| L18 F8 penalty staging | TG-PW-F8 / CODE-F8 |
| L19 F1 sampler | TG-PW-F1 / CODE-F1 |
| L20 F7c attribution | TG-PW-F7C / CODE-F7C |
| L21 F7ab I/O | TG-PW-F7AB / CODE-F7AB |
| L22 warm/depth regression | TG-PW-WARM |
| L23 overlap already present | TG-PW-OVERLAP |
| L24 dequant side | TG-PW-DEQUANT / PP-B2 |
| L25 adaptive rejection | TG-PW-ADAPTIVE |
| L26 miscellaneous nulls | TG-PW-MISC; ROAD-A1/A2/A3/B6 dispositions |
| L27 HC_Q8 closed | TG-PW-HCQ8; ROAD-C1 ineligible |
| L28 slow/fast unresolved | TG-PW-SLOWFAST |

### 9.3 CODE → [§11.3 snapshot](#snapshot-code)

Each F-range includes its **entire** original change list, commit slicing, validation ladder, risks/mitigations and DoD where present. These remain archival in full; §3 is the shorter reconciled mechanism view, not a verbatim replacement. F7c and F7ab are separate rows, as in the source's nine-row master/DoD tables.

| Original CODE range / section | Interpretation / operative target |
|---|---|
| L1–7: branch/pins, skipped F9/F10, constraints and amendment history | §1/§3 provenance and §8 default-off/build gates; historical paths/pins not current deployment instructions |
| L9–27: anchor/refinement table | All 14 data rows individually indexed below; exact anchors/refinements remain archival |
| L29–43: nine-row master table and totals | CODE-F1…F8 including F7c/F7ab in §2; old phases, commits/effort, GPU-hours and speed forecasts are history, not current promises |
| L45–61: phase diagram/dependencies | §6.2 retains hard/soft/merge/measurement edges without scheduling null/inactive work |
| L63–76: AUX_CPUS/warm-collapse re-baselining | §4/§6.2 preserve confounds and old hold; no unproved release or “KL NOW” permission |
| L79–114: F6 | CODE-F6 / MECH-BOOT-FILL (§3.6); already landed off, exhaustive device/all-slots gate and per-thread device scope |
| L116–145: F8 | CODE-F8 / MECH-PENALTY-STAGING (§3.7); already landed off, THIS-draft history bounds/ownership, penalized engagement |
| L147–179: F1 | CODE-F1 / MECH-SAMPLER (§3.1); already landed off, exact mode/RNG parity and fallback requirements |
| L181–204: F7c | CODE-F7C / §3.6/§8.4; landed intentionally ungated append-only logging, current counters incomplete; B6 discriminator is not evidence of gain |
| L207–261: F2 | CODE-F2 / MECH-XQ-RING (§3.2/§5.1); full tail-publish design preserved, later null governs, measured implementation comparison unknown |
| L263–288: F3 | CODE-F3 / MECH-CPU-POOL-RING (§3.3); not landed, blocked; actual native destination/stride and unchanged Q2/non-native paths |
| L291–332: F5 | CODE-F5 / MECH-PLE-GATHER (§3.5/§5.2); original race rejected structurally, amended RAII/one-raiser/join/error design retained, not landed |
| L334–364: F7ab | CODE-F7AB / §3.6/§3.8; landed inactive on production source; exact fallback/alignment/order, independent gates and ≥6-boot historical program preserved |
| L367–416: F4 | CODE-F4 / MECH-PREFETCH (§3.4/§5.3); no predicted activations, feature negotiation/fast ack/drain/async staging, scratch/demand budgets and stop/descope preserved |
| L420–430: promotion, rollback, review and boot discipline | §1.4/§6/§8.5; operator-only promotion, default flags vs code defaults, canonical hash/identity/real-completion restore; old pin/window instructions historical |
| L432–463: first week, Day 1–5 | Explicit **HISTORICAL/NOV15**, §6.2; exact tasks/estimates retained at these lines, no current F2→F3 schedule |
| L466–480: nine DoD rows/end matter | Original F6 L470, F8 L471, F1 L472, F7c L473, F2 L474, F3 L475, F5 L476, F7ab L477, F4 L478; shorter card summaries in §3, latest status/strict proof govern |

**CODE anchor-table data rows — exact original range → operative context:**
| Original CODE line / anchor subject | Operative context (full refinement retained at that archive line) |
|---|---|
| L14 remote requant / frame memcpy | §3.2 no protocol change |
| L15 three image writes / folded hazard | §3.2 preserve ordering, no assertion about the later measured implementation |
| L16 doorbell publish family | §3.2 ring publication |
| L17 service loop/pool/flags | §3.2/§3.4/§3.5 ownership |
| L18 double sync / sampler rationale | §3.1 |
| L19 ar_on vs doorbell PLE readiness | §3.5 |
| L20 pool quantize / fetch staging | §3.3/§3.4 |
| L21 cache-fill methods / batching precedent | §3.6 |
| L22 fill loops / single-slot vs all-slot proof | §3.6 |
| L23 per-window penalty copy | §3.7 |
| L24 gfx906 priming exclusion | §3.6, later null status retained |
| L25 protocol/version/feature/one-outstanding rule | §3.4 |
| L26 quality/driver/lane tools | §8; paths/guards must be repinned before use |
| L27 tests/golden/protocol/parity infrastructure | §8.1; classify actual device needs before execution |

### 9.4 ROAD → [§11.4 snapshot](#snapshot-road)
| Original ROAD range / item | Interpretation / operative target |
|---|---|
| L1–12: author/constraints/verified anchors/hardware/model/rates | §1/§3 version rule; original quality wording and speed bands historical, stricter current rule governs |
| L14–18 A1 mmap | ROAD-A1, §3.8/§5.4; infeasible `--ple-io ram` budget (50.7+46 GiB >62 GiB) retained at L18; later null governs |
| L19 A2 switches, including six named flags and attention assessment | ROAD-A2, §3.9/§3.15; preserve each switch/forecast/test, distinguish separately identified TG-S3 |
| L20 A3 cache | ROAD-A3, §3.8; later null governs |
| L21 A4 prompt FP16 audit | ROAD-A4, §2/§4; existing baseline includes non-bit-exact rounding, proposed on/off audit needs exact current-baseline interpretation/operator decision, not silent removal |
| L22 A5 refutations | ROAD-A5, §2/§4; exact compile-time/divisibility/failed-adaptive reasons retained at L22 |
| L24–28 B1 swap diagnosis | ROAD-B1, §3.10; historical static finding/HC_Q8-dependent scope, not “land old commit” |
| L29 B2 retry | ROAD-B2, §3.10; safe bounded retry only after successful release and exact state preservation |
| L30 B3 refusal floor | ROAD-B3, §3.10; opt-in safety, no default change |
| L31 B4 checkpoint I/O | ROAD-B4, §3.11; snapshot ownership/restore correctness first |
| L32 B5 PLE default | ROAD-B5, §3.8/§6.2; blocked without benefit/default authorization |
| L33 B6 priming | ROAD-B6, §3.6; later no-adoption/null history, not automatically requeued |
| L35–39 C1 HC_Q8 templates/LDS retile | ROAD-C1, §3.13; closed/ineligible, all original dimensions/anchors/effort/quality caveats retained |
| L40 C2 exact expert contract | ROAD-C2, §3.14; full arithmetic/CPU/helper/placement design and cold ×5/warm ×5 tests retained, unvalidated |
| L41 C3 LDS/wave64 audit | ROAD-C3, §2/§7.3 attention/GEMM research; anchors verify_kernels.cu:832/1010–1709, iq_kernels.cu gfx906, router_top10.cu:190–461 and per-kernel parity/greedy-sha proposal retained at L41; strict relevant equivalence required |
| L43–47 top-three recommendations (A1+B5, B1, A2) | Historical priority list, **not** current order; later null/closed evidence governs |
| L49 deprioritized/refuted list | Preserve HC_Q8, kv-grow, DENSE_MMQ, reserve/FS/adaptive, KV-to-CPU and fp16-KV negative history; no blind repeat |

### 9.5 Supporting context and coverage boundary

The two linked reviews are not among the four merged sources. CODE's amended text incorporates its original review requirements; §3/§5 preserve their ordering/ownership/protocol consequences. The TG review adds actual counter-path, sampler-eligibility and profiler-observer details in §3.1/§3.4/§8.4. Their original verdicts do not approve this master. Journal/operator overlay is explicitly separated in §1.4/§6.3.

The complete archival source bodies have **61 + 124 + 480 + 49 = 714 original lines**. This is a byte-preservation scope, not a semantic item-count proof. The crosswalk points to actual source ranges/operative sections; it does not invent missing IDs, claim every archive paragraph is in the summary, or classify unknown science as solved.

---

## 10. Reconciliation audit and limits

**Readiness is pending independent review of the final hash.** The [review ledger](inference-optimization-master-plan-review.md) records each oracle round, its exact reviewed artifact SHA and unresolved/resolved blockers. The parent journal records actual preservation/command evidence. An author's self-check claim or an approval of an earlier hash is not approval of this version.

Preservation check: extract bytes **between** each `source-snapshot` opening fence and closing fence without trimming or normalizing whitespace; require four unique IDs, source SHA matching §1.2/marker, extracted bytes equal source bytes, and originals unmodified. A separator newline accidentally inserted into the body is a mismatch. Code-fenced headings/links are archival text, not operative targets; working snapshot links use explicit anchors outside the fences. A link/heading scan or matching hashes cannot establish semantic completeness by itself.

Semantic check: compare §2/§3/§4/§5/§6 against each source range in §9, distinguishing dates, proposed vs landed vs null/closed, functional vs merge/measurement dependencies, historical holds/forecasts and strict current quality/operations policy. Preserve full originals inside this file rather than pretending short cards reproduce every test/commit/risk detail. Versioned code anchors are evidence to recheck, not current-tree validation claims.

| Still open / not established | Required future evidence, not a blocker to honest documentation readiness |
|---|---|
| Same-build slow/fast modes and historical warm collapse | Current identity-pinned A/A controls, clocks/links/placement/boot-state attribution; no assumed release of old hold |
| Exploratory historical-log aggregate | Pinned slice, parser, per-boot identity and independent recomputation before using its statistics |
| F2 measured-vs-amended implementation difference | Actual source/artifact comparison and changed premise; until then null/off, F3 not shortlisted |
| F4/F5 residual cost and ownership | Real-path instrumentation, structural ownership proof and justified applicability before implementation |
| Prompt-F16 baseline audit / C2 exact expert contract | Exact current-baseline interpretation and engaged arithmetic/logit evidence; no silent baseline switch |
| HC_Q8/C1 | Closed/ineligible, not a permissive unresolved rebaseline choice |
| Parking recovery, complete expert locking and uncapped arms | Separate correctness/coverage/fit and parent-owned runtime gates; existing arena lock or low-RAM boot band does not prove them |

This reconciliation performs no new candidate implementation, device test, performance measurement or production change, and authorizes none. Research scope in §7 is planned, not a claim of a new web campaign. Parent real-completion probes used to verify serving before specialist dispatch are operational readiness checks, **not** candidate validation. Any earlier checks made stale by later edits must be repeated on the final artifact.

---

<a id="snapshots"></a>
## 11. Archival Appendix — Four Exact Verbatim Source Snapshots

The source bodies below preserve all four pinned originals. Verify byte equality using §10's extraction rule, separately from semantic review. They are historical reference only: embedded approvals, commands, tolerances, defaults, forecasts and schedules do **not** override current master policy or authorize execution. Relative Markdown links inside fences stay verbatim; operative links outside fences resolve from this master directory.

<a id="snapshot-pp"></a>
### 11.1 PP — speed-plan-v2.md (original L1–61)

<!-- source-snapshot id=PP path=bench/ab-v0210/rpcprof/gpu-rounds/speed-plan-v2.md sha256=db7bc537c4e39c36ed35f086ff8434dde325a07cc53037c5ca60301949961d1c -->
````markdown
# Speed plan v2 — serving prefill program (2026-11-17, post-review)

Reviewed by independent oracle (ses_e15829d7effe). All review claims spot-verified at source.
Supersedes v1. Lanes parked by operator until unparked; nothing here executes before then.

**Historical component, not the current execution plan.** The complete PP + TG + turnaround program is consolidated in [serving-plan-current.md](serving-plan-current.md), which supersedes this file's ordering, estimates, fixed throughput floor and contract-only gate shortcuts. The operator requires zero quality sacrifice; the relaxed numerical-adoption threshold previously written below is revoked. Keep the original text below as design history, not competing instructions.

## Facts (measured/settled)
- prompt_ms = 1338 + 3.74×pcie_fetch (R²=0.969) — COMMON CAUSE (slots drive both), not overlap debt.
- Fresh 10.3k prompt: ~12 150 streamed slots (22.3 GiB), fetch 1.15 ms/slot steady 1.82 GiB/s,
  GPU-side ~2.6 ms/slot ⇒ ~33 s of ~46 s wall is per-slot GPU cost (gather/dequant/launch, FLOPs ≈ 0).
- Overlap already on: whole-chunk readahead + issuer thread (prefill.cpp:4040, 4110); ring past saturation.
- MMQ=0 in prod is a MEASURED win, not a default: edf2af2489 "OFF +7% PP, +12% TG vs ON". MMQ-ON is dead.
- vram_free_mib ≈ 220 at boot — engine VRAM is effectively full; any VRAM growth needs a named shrink.
- Env single source: config env map (overrides launcher block).

## Phase 0 — decisive probe (2 boots, ~1 h)
0a. Zero-cost pre-check (existing logs): `prefill mmq:` line; STRATA_MANIFEST state.
0b. Boot 1: config env += STRATA_PREFILL_TIMING=1, STRATA_TRACE=1 → prefill_judge ×8.
    Parse `strata prefill timing:` table: wall vs GPU timeline; wait copy; host grouping; dequant; gemm.
0c. Boot 2: same + STRATA_PREFILL_ROUTED_STREAM=1 (depth 32) → prefill_judge ×8.
    TRACE prints routed-drop counts → converts A1 from hypothesis to measurement; the arm doubles as A1 pilot.
Branch: wall − timeline large ⇒ idle real (A gains weight); wall ≈ timeline ⇒ GPU-bound (B weight).

## Program A (env rounds)
- A1 ONLY: ROUTED_STREAM=1, depth sweep 16/32/64. Bit-identical by source contract
  (routed_stream.hpp: same ring slots, consumer walk unchanged, "same bits").
  Ceiling: header-measured drop 15% @ 8k-chunks (47% @ 1k) ⇒ ~1800 slots × 1.15 ms ≈ **2 s** expected,
  exact from Boot-2 drop line. Eligibility: keep ISSUER on, COPY_EVENT_EVERY=1, COPY_WAIT_DEDUPE off
  (they hold routed off), no --peer-device. Only pinned-arena copies droppable; stager-staged keep copies.
- DROPPED: A2 (Linux event overhead ~0 µs — 40 ms/prompt), A3 (32 IS the default; 16 optional undershoot).

## Program B
- B1 capacity placement — budgets stated separately (v1 mixed them):
  * Host RAM side: relief-hot-mib 15000→ raises RAM-resident packs; Δ ≤ MemAvailable headroom (~16 GiB
    at serve), and every GiB stolen from page cache starves the streamed blobs (self-defeating past a knee).
    Document complement_pin_limit_ behavior at target before flipping.
  * VRAM side: engine cache growth needs ΔVRAM ≤ (16 GiB − KV int8 − graph pools − arenas) — with
    vram_free 220 MiB that means naming a shrink first (kv/graph-pool candidate), else null.
  Both bit-identical placement. Honest expectation: single-digit seconds unless a real VRAM pairing exists.
- B2-REPLACEMENT — the 33 s bucket (precommitted lever):
  * STRATA_DEQUANT_SIDE=1 — ALREADY BUILT (gemm.cu:642; side-stream dequant; dedicated parity test
    gemm_dequant_side_parity.cu in repo). Run parity test (GPU, lanes unparked) → if bit-identical claim
    holds: env-only A/B, no KL needed. This is the first real GPU-overhead round.
  * If Phase 0 shows gemm (not dequant) dominates: recompile levers (MMQ_GROUP 16→, STAGE_GRP 32→) are
     constexpr — code change, separate cycle, zero-quality-loss-gated.

## Gates (corrected wording)
- Bit-identical-by-contract rounds (A1, B1, DEQUANT_SIDE-after-parity): no KL; A/B medians + decode guard.
- No relaxed numerical-adoption tolerance is authorized. Use byte-exact evidence or the established teacher-forced KL 0.00000/no-NaN gates with the changed path engaged; do not treat rounded printed zero as proof of bit identity.
- Decode guard ≥ 24 tok/s every round. B3 (helper instrumentation fix): own commit + own guard reading,
  never folded into a numerics round.

## Success bar (honest)
Phase 0 decisive; ≥1 gated adoption. Pre-Phase-0 priced candidates: A1 ~2 s + DEQUANT_SIDE (unpriced,
bucket is 33 s — could be large or zero) ⇒ −20% is stretch, not commitment. The 33 s bucket is the only
path to it; Phase 0's table prices where in it we aim.

## Wall-time
0: ~1 h · A1 sweep: ~30 min · B1 analysis now (read-only) + 1 boot if it survives math ·
DEQUANT_SIDE: parity run + A/B ~45 min.
````
<!-- /source-snapshot id=PP -->

---

<a id="snapshot-tg"></a>
### 11.2 TG — tg-optimization-chapter.md (original L1–124)

<!-- source-snapshot id=TG path=bench/ab-v0210/rpcprof/gpu-rounds/tg-optimization-chapter.md sha256=02abecd2e0c10360a640881d2d641e060c5ea1ef73c80c348d7a2a36bb39552c -->
````markdown
# TG optimization chapter — APPROVED research plan, 2026-11-18

**Oracle APPROVE for research-plan readiness only; not authorization to implement, benchmark or promote.** Independent reviewer: `ses_e12771b3bffev76LmVooHz0NnX`; no blockers. The reviewed substantive v1 is preserved at commit `d649e5b`. See [review and evidence notes](tg-optimization-review.md) for confirmed source/upstream facts and non-blocking clarifications. Pre-review uncertainty labels below remain conservative; unperformed experiments remain unvalidated. Fresh analyst: `ses_e12bcc978ffeFMfJVS8eRW6VZ2`.

## 1. Provenance and prior work

Source anchors below refer to `af1e64a5cb4b617d52c0f9b416af76d28c42bc4f` in `/mnt/srcds/Strata-wt-integ`. Its binary `8f19baaf150237e5` **was measured** in the completed champion panel; it is not deployed. Canonical production remains `b0ec0109` / `42cb41b314`. Read the old journal before proposing another experiment: `journal/serving-tg-reeval-split-retest-2026-11-11.md`, related `notes/code-opt-{plan,analysis,plan-review}.md`, and the historical round records.

| Prior work | Analyst's classification | Evidence / reason not to repeat blindly |
|---|---|---|
| `STRATA_PREFILL_MMQ=0` | Adopted | 18ad / `edf2af2489`: OFF +7% PP, +12% TG versus ON; ON had a slow tail. |
| Split 30, DFlash block 7, foresight 2/8, reserves 192/156, MMVF rows, CPU affinity, conversation disk placement | Adopted configuration work | 18ac/18ao/18ae/18at-av/18br/18bh/18cq; MMVF's 42.2 tok/s outlier did not reproduce (18bp). |
| F2 xq ring | Performance-null, off | 18cp: BASE 25.65 versus RING 25.55 tok/s; earlier gain was drift plus a launcher-env engagement error. Parity 16/16 and repaired test artifact `7b3da65233` do not establish a speed gain. |
| F3 CPU-pool act from ring | Not landed | No `STRATA_CPU_POOL_XQ` in pinned tree; ring carrier has no demonstrated single-stream benefit. |
| F5 PLE gather overlap | Not landed | 18bi review rejected the original ownership/threading design; no `STRATA_PLE_ASYNC_GATHER` in tree. |
| F4 speculative prefetch | Not landed | Prior plan required remeasuring residual wait first. |
| F6 queued/parallel boot fill | Landed, off in prod | 18bt, `afc2ad38ff` / `e9593357b0`; boot-only, not a TG optimization. |
| F8 mapped penalty staging | Landed, off; request-dependent | `penalty_hist_stage.hpp:40`; neutral penalties do not exercise the intended path. |
| F1 in-graph sampler | Landed, off; usable evidence incomplete | `sampler_mapped.hpp:40`, `verify.cpp:2846-2852`; 18ca invalidated the earlier 18bu engagement claim through the config `exe` bug. Registered tests are not evidence they ran. |
| F7c timing attribution | Landed, incomplete for current shape | Units fix `9d5cb763f4` / 18bx; 18cd still saw zero-valued helper fields. |
| F7ab O_DIRECT / offset sort | Landed, inactive on prod fill source | `dd0c0299cb` / `8f1ad4adb1`, 18cd; the helper fills from the RAM arena. |
| Prefill warm + swap-in depth 128 | Regressed, reverted | 18cq: combined PP regression about 19%; depth alone was not independently tested. Keep default 64. |
| Prefill read/compute overlap | Already present | 18cs, `prefill.cpp:4040-4077` and default issuer thread. Not a new optimization. |
| `STRATA_DEQUANT_SIDE` | Unvalidated, PP-shaped | Existing parity test at `gemm.cu:642` is not local TG evidence. |
| Acceptance chasing / adaptive drafting | Rejected in prior shapes | 18z/18am/18ao: acceptance rate alone did not predict TG. |
| PLE mmap/row-cache, CPU KV, queue priming, IQ thread threshold, A2 load-order knobs | No adoption / null | 18bc/18bv/18an/18bw/18bq/18bo-bp; do not relabel these as new proposals. |
| HC_Q8 native/inject/trim | Closed/off | 18aq-18ax/18ba. |
| Same-build slow/fast serving modes | Unresolved | 18bm/18bn: roughly 15–22 versus 27–31 tok/s; helper fill class did not explain it, queue priming did not fix it. |

## 2. Measured facts and unresolved attribution

The completed panel and its limitations are in `CHAMPIONS-RESULTS-20261118.md`. Cached main-call TG medians were latest **19.7/32.6**, Nov-16 **33.1/32.6**, Nov-15 **34.0/34.4** tok/s at 16k/80k. Latest short TG ranged **16.7–41.8**, Nov-15 **13.4–36.3**; do not suppress these slow samples or claim a winner.

Config files match outside harness fields, but effective launch environments do not: latest/Nov-16 guard limit 50.0 GiB versus Nov-15 62.2 GiB. Fixed arm order, swap residue, low-VRAM warnings, different generated traces/lengths and server continuation work limit causal attribution. No quality equivalence was established by throughput or response hashes.

### Exploratory historical-log aggregate — not yet accepted as reproducible evidence

The analyst reports parsing 1201 requests from the mutable `logs/engine-prod-ud-iq3xxs-v0233.log`. That file spans boots/pins and lacks per-line identity. No pinned input slice, parser artifact or independently checked aggregate has yet been provided. The following numbers are **analyst-reported exploratory observations**, not current-build latency budgets or adoption evidence:

| Reported statistic | Value | Constraint on interpretation |
|---|---|---|
| Decode rate / context | 21.8 tok/s; context median 72229, max 246028 | Mixed requests and engine identities. |
| Windows per generated token | 0.381; reciprocal 2.62 tokens/window | Derived, not a fixed DFlash acceptance constant. |
| Decode time per counted window | 123.3 ms median | Numerator may include work beyond the verifier's service loop. |
| `ms_wait` / `ms_pool` per window | 21.93 / 17.04 ms medians | Source semantics, reset boundaries, stage aggregation and overlap need independent review. |
| `(wait+pool)/window wall` | 0.332 reported median | **Not** proof that two-thirds of GPU time is uninstrumented; these counters are not assumed disjoint critical-path buckets. |
| `helper_begin_ms` / `helper_finish_ms` | Reported 0.0 throughout despite helper requests | Suspected reporting-path mismatch; requires source confirmation. |
| `pcie_fetch_ms` | Reported 2128 ms/request median | Request-total prompt+decode mixture; do not quote it as a decode percentage. |
| `strata batch:` run/commit/emit split | Reported absent in serving log | Batch-only instrumentation is not a serving decomposition. |

### Pinned-source path map (analyst anchors for review)

- DFlash draft/window selection: `generate.cpp:23036,23107-23109`; `T = df_pending_k + 1` overrides the generic window chooser in this path.
- Per-window verification: `verify.cpp:3840-3976`; layer doorbell waits and host pool service contribute counters. Post-loop syncs at `3986-3989,4017-4029` need attribution before assigning any missing wall time to them.
- Greedy head / optional mapped sampler: `verify.cpp:2828-2852`.
- Helper transport: `remote_experts.cpp:619-864`, `expert_helper_socket.cpp:615-618,724,1065,1203`.
- Suspected counter mismatch: serving request counters read `helper_tier_p` at `generate.cpp:24109-24117`, while helper timing fields inspect `remote_experts[r]` at `24294-24298` under a remote-cache gate. Confirm before changing wiring.
- Existing `STRATA_VERIFY_PROFILE=1`: `verify.cpp:1293,1304-1306,1525,2921-2945`; named stage timing includes attention, routing, KV resolution, VRAM hits, waits, combine, head and gaps. **Observer effect:** profiling disables `df_branch`, conv/AB fusion and `sh_fork` at `1555,1888,2369`; its decomposition is not automatically production's.

## 3. Upstream leads — verification pending

Repository reported by analyst: `https://github.com/Niko1221/Strata`; local push target is the user's `fork`, never upstream. Dates/status/presence below are analyst claims for independent confirmation. Commit ancestry alone does not prove absence of an equivalent cherry-pick.

| Lead | Exact upstream reference | Claimed status/date and relevance |
|---|---|---|
| Fixed token trace through real decode/verify windows | [d486fbe7d6](https://github.com/Niko1221/Strata/commit/d486fbe7d6f5957f213855f5beb3999dd0540e7d) | Merged Nov-06; native `STRATA_FOLLOW` / `force_check.py` reportedly absent from this fork outside SYCL. Candidate measurement aid, not a speed win. |
| CPU-only decode-window pool benchmark | [b94ab5f4c6](https://github.com/Niko1221/Strata/commit/b94ab5f4c6316c7567b0d7d5ebe0ad358f674376) | Merged Nov-06; `kq_window_bench` reportedly absent. Could price only the CPU slice, not whole-window critical path. |
| Pool-row benchmark and row load order | [8fdf701f88](https://github.com/Niko1221/Strata/commit/8fdf701f88b3c87cd0bffbb890b79fa7376f9c84), [da5e338422](https://github.com/Niko1221/Strata/commit/da5e3384221f3f40620fd2c437256fc3241112cf), [d3851f1043](https://github.com/Niko1221/Strata/commit/d3851f1043372a30fd9fa16066026f48f98bc9ce) | Merged Nov-11; benchmark presence and arithmetic/order changes need separate checks. Do not assume the optimization is quality-neutral because its benchmark is CPU-only. |
| Cost-based draft-window selection | [4079483b6d](https://github.com/Niko1221/Strata/commit/4079483b6d26463cdbd86795db34bd1ecb008df5), [15a4be9c7a](https://github.com/Niko1221/Strata/commit/15a4be9c7a2fa4e445f0ee1ba18a53f917c48e39) | Nov-11–13 family; not shortlisted while DFlash sets `df_pending_k + 1`, and previous adaptive policies lost locally. |
| Drop a window finishing after STOP | [33648fef1e](https://github.com/Niko1221/Strata/commit/33648fef1eecb2bdc527fe00e17a70feb2fabc5d) | Nov-12; cancel/stop wall-time lead, not the headline cached-TG metric. |
| Tightest ancestor cgroup CPU quota | [9e52144fc9](https://github.com/Niko1221/Strata/commit/9e52144fc973b6c152fe8539285514b11a5a3dd1) | Nov-13; not a gain without a binding nested CPU quota. |
| NUMA-local placement | [716ebae14e](https://github.com/Niko1221/Strata/commit/716ebae14e364f774d09638ec0529f815190f365) | Nov-14; analyst found one NUMA node, so not locally applicable. |
| Decode-attention generic loop changes | [78f5417195](https://github.com/Niko1221/Strata/commit/78f5417195edf4a698362d5bcd810ed1756844c1), [38ab10e03e](https://github.com/Niko1221/Strata/commit/38ab10e03e3db0ef487c7b81634d1db263cd0198) | Nov-07/08; local gfx906-specific variants already exist. Applicability cannot be inferred from title. |
| HC_Q8 prefill vectorization | [PR 3220](https://github.com/Niko1221/Strata/pull/3220), head `67d3b105a331f819eb36e23b78c5710dd4051d88` | Reportedly open/unmerged; HC_Q8 off locally, so not shortlisted. |
| NaN-preserving BF16 | [PR 2955](https://github.com/Niko1221/Strata/pull/2955), head `c52d60b31094c82ecbc73faaacbb517e692c19b9` | Reportedly open, with equivalent correctness work picked into v0233.2 per 18am; not a TG claim. |
| Long-prompt MTP memory borrowing | [PR 3307](https://github.com/Niko1221/Strata/pull/3307), head `41bff1d3d02f238cc5f7bda21d787ab19bbc42cd` | Reportedly open; PP/admission, no established TG mechanism. |

No upstream author's speed claim is a measurement on this box. Fork presence must be checked by implementation, not commit ancestry alone.

## 4. Ranked shortlist — hypotheses, not adopted changes

### S1. Complete attribution on the real serving path first

**Evidence:** current fields do not establish a full critical-path decomposition; zero helper timing fields merit investigation. **Scope:** narrowly wire the correct helper timing counters and bracket verifier tail/whole-window boundaries, with source-verified reset/aggregation semantics. Reuse existing profiler only with its fusion/branch observer effects explicitly measured.

**Budget:** counters should add no material RAM/VRAM demand; verify actual profiler allocations rather than promise zero overhead. **Proof:** append-only log fields and parser tests; unchanged arithmetic path plus matched output/quality checks. **Falsify:** counters still do not report the exercised helper path, or timings cannot reconcile with documented overlap; repair instrumentation, do not optimize an invented slice. **Rollback:** revert logging patch / unset profiler. No numerical kernel proposal advances on unpriced source shape alone.

### S2. Revalidate the landed F1 in-graph sampler on its eligible request class

**Evidence:** source hypothesis only. Greedy head selection is already in-graph; the temperature-zero champion panel did not test the proposed sampled/penalized path. Production sampling settings must be verified before asserting relevance. **Scope:** existing `STRATA_SAMPLER_IN_GRAPH`, `sampler_mapped.hpp:40`, `verify.cpp:2846-2852`; no default flip.

**Budget:** confirm mapped-buffer size and ownership; do not assume "a few hundred bytes" without reading allocation code. **Proof:** execute sampler parity tests; prove identical inputs, RNG/Philox draws, filters/penalties and actual host-path bypass on sampled requests. Forced-trace performance mode must not bypass the sampler and make this test vacuous. Require the hard gate below. **Falsify:** engagement absent or no improvement outside a same-session baseline bracket. **Rollback:** flag off.

### S3. Investigate the in-tree gfx906 attention lane-cell variant only if attention is a measured cost

**Evidence:** unpriced, architecture-matched source lead; `qsa_decode_attn.cu:919,966-1014`, `STRATA_ATTN_LANECELL`, local int8 KV path. The source's bit-identical-score claim is not local proof, nor proof of end-to-end logits. Prior A2 knobs were different.

**Budget:** verify launch geometry, registers/shared memory and occupancy, not just allocator bytes. No KV precision/capacity reduction. **Proof:** engagement plus exact relevant intermediate/output equivalence through actual int8-KV and context cases. Query-swizzle/reduce-order variants remain blocked without exact equivalence; no tolerance substitution. **Falsify:** attention is not a relevant production slice, any quality mismatch, or no bracket-separated benefit. **Rollback:** env off.

### S4. Measurement infrastructure: fixed decode traces and CPU-only slice benches

**Evidence:** free-running traces differed across arms; repeatability is an open problem. **Scope:** inspect/minimally port upstream forcing and pool benchmarks after confirming API/source compatibility. This is a prerequisite measurement tool, not a speed optimization.

**Budget/proof:** check all new allocations, tokenizer/input fidelity, real HIP/CUDA verification-path engagement and production-default-off behavior. Forcing identical tokens does **not** eliminate floating-point/placement nondeterminism; it only removes token-sequence divergence. Run A/A controls on the same trace. A benchmark-only forcing mode must never substitute for sampled-path correctness tests. **Falsify:** forcing cannot exercise the relevant path; leave the candidate blocked or design a valid control, not silently relax quality. **Rollback:** omit tool/flag.

Not shortlisted: F2/F3 without a changed premise; F4/F5 before residual cost and ownership proof; boot-only F6/F7ab; penalty-only F8 on neutral-penalty shapes; MMQ-on, HC_Q8, prior failed adaptive policies and parked PP levers. No percentage gains or invented time estimates are proposed.

## 5. Phased verification (requires later authorization)

1. **Offline evidence/compatibility:** pin historical log slice/parser if retaining its statistics; verify timer ownership/units and each upstream lead. Produce patch/dependency plans, not blanket cherry-picks. Build modestly (`nice -n 19 taskset -c 8-15`, bounded jobs). Execute only genuinely CPU-only tests here; classify parity tests by actual hardware needs before running. Freeze token inputs and metadata.
2. **Measurement infrastructure:** S4 trace controls and S1 instrumentation before candidate claims. Specify sampling correctness fixtures separately. Write an exact parent-owned run manifest with one pair at a time, effective environment, baseline, warm-up, corpus, arm order, repeats, failure limits and canonical restore.
3. **Authorized parent-only profiling:** zero active specialists; compare profile-off and profile-on using repeated/bracketed windows and quantify changed fusion/branch behavior. Do not use altered-profile stage totals as unqualified production budgets. Reconcile overlap and request/draft/verify boundaries.
4. **One eligible candidate:** predeclare measured cost to remove, exact engagement and quality gate; bracket/interleave with same-build A/A controls and fixed trace where valid. Keep generation-only, whole-response/continuation wall and fresh PP separate. Retain medians, full spread, slow samples, RSS, cap-adjusted room, host MemAvailable, swap and per-card VRAM. No benefit inside baseline variability is established.
5. **Close:** record every rejection and artifact/exit status; restore canonical launcher/config/binary, fresh ready line, `/v1/models`, identity and real `OK` completion. Any default/promotion is the operator's separate decision. A hard GPU/OS hang is an abort/operator incident, not permission for reset experiments.

## 6. Quality hard gate and unresolved questions

- Zero quality sacrifice. No quantization, precision, context-capacity or algorithm shortcuts; **no mean-KL ≤0.001 fallback**. Retain established teacher-forced KL 0.00000/no-NaN and bit-exact requirements with the changed path engaged; rounded printed zero is not proof of exact logits.
- Where arithmetic/order or sampling can change, require exact relevant logit/operation equivalence, or a credible unchanged-arithmetic-path proof plus executed, engaged tests. Same top-1/PPL or free-running content hashes are insufficient on their own.
- Establish same-build A/A nondeterminism before candidate quality conclusions. **Performance** spread is not a quality tolerance. If exact equivalence cannot be established, the candidate stays blocked; do not call it quality-neutral because it is default-off or a benchmark.
- The oracle must confirm: actual timer overlap/reset semantics; historical aggregate reproducibility; upstream presence/compatibility; sampler eligibility/RNG equivalence and non-vacuous tests; attention variant arithmetic; test hardware classification and practical phase order.
- RAM policy's observed 677 MiB boot band is not proof of low-RAM restore recovery or restart depth. Those live gates remain separate. Unrelated PP/GPU sweeps stay parked. This draft authorizes no code change, traffic or promotion.

**Independent review complete: APPROVE, no blockers.** The bounded research plan is settled; candidate validation and any implementation, GPU window or promotion require separate authorization.
````
<!-- /source-snapshot id=TG -->


<a id="snapshot-code"></a>
### 11.3 CODE — code-opt-plan.md (original L1–480)

<!-- source-snapshot id=CODE path=bench/ab-v0210/notes/code-opt-plan.md sha256=fb8d30d31c6ba91ee8c9d3185dfbbad20cf02d4242475c257d551499e8f2f7ac -->
````markdown
# Implementation plan — code-level optimizations F1–F8 (branch hc-q8-gfx906)
- Date: 2026-11-15. Deliverable plan for the F-items in `bench/ab-v0210/notes/code-opt-analysis.md` (input, verified findings).
- Repo under change: `/mnt/srcds/Strata-wt-integ` (branch `hc-q8-gfx906`, HEAD `84fc595dd9`; serving pin is `36ecbe98` — never regress it). F9/F10 skipped (observation / negative findings).
- Context read: journal rounds 18am–18bd (`serving-tg-reeval-split-retest-2026-11-11.md`), `perf-roadmap-2026-11-15.md`.
- Every item lands **default-OFF behind an env gate** (repo convention: `STRATA_HELPER_WIN_FAST` read at `remote_experts.cpp:746/749`, `helper_win_fast_enabled()`). OFF must be byte-identical: no new kernel in the captured graph, no new frame on the wire, no changed host order.
- Hard constraints (from operator): zero quality sacrifice (KL 0.00000 / byte-identical gates only); oracle/review convention before pin promotion; gate-default flips (promotion) are operator decisions staged as explicit steps; HC_Q8 paths are dead — no edits beyond what dead code forces; pin builds only from the worktree via `tools/mixed_gpu/build_pin.sh` (guards: gfx906 HIP, MMQ state line, no nvcc, libamdhip64, .hip_fatbin).
- **Amended in place per `code-opt-plan-review.md` (2026-11-15, APPROVE-WITH-CHANGES)**: F5 redesigned (the review's one blocking design flaw), F2/F4/F6/F8/F3/F1/F7ab amended, new Re-baselining section added; the anchor table, master/DoD tables, phasing, and first-week sequence updated for consistency. Amended/new section headers are marked.

## Anchor verification status (re-walked in the worktree at HEAD)
All analysis anchors re-verified true at HEAD, with these refinements found by reading the surrounding code (they shape the plan; none invalidate a finding):

| Anchor | Status | Refinement found |
|---|---|---|
| remote_experts.cpp:744-751 (per-frame CPU requant) | ✓ | `build_window_payload` (expert_helper_socket.cpp:574-587) **memcpys the image into the frame** → F2 needs **no wire/protocol change**: identical kBegin bytes whether the image came from CPU quantize or the GPU ring |
| verify.cpp:2352-2354 / 2320-2324 (nat_xq_ image) | ✓ | The image is written at THREE points: qdedup pre-quant at :2324 (after publish, before shared expert), folded-rewrite at :2352, plain at :2354 — all AFTER the doorbell publish (:2286-2298). A "move quantize before publish" reorder is only value-safe on **unfolded** layers (folded+qdedup passes the UNROTATED image to `shared_expert_multi` at :2332; see F2 hazard note). Helper claims never occur on folded layers (`expert_helper_candidates.cpp:80` skips `had_folded`) — so the ring is only needed there and the hazard is avoidable by construction |
| elementwise.cu:437-458 (doorbell publish family) | ✓ | `doorbell_publish_value_kernel` (:424-436) is the exact copy+fence+seq-store pattern to clone for the xq ring publish |
| verify.cpp:3518-3619 (doorbell service loop) | ✓ | pool() call at :3578-3580, flagA/flagB at :3595-3604, k==0 PLE gather at :3609-3615, `*flag=want` at :3616 — the F2 wait slots in before pool(), the F5 gather removal at k==0, the F4 prefetch hook after the classify |
| verify.cpp:3661/3721-3722 (double sync) | ✓ | In-graph greedy argmax at :2658-2664; host comment :3678-3681 explains why sampling is host-side ("a captured kernel would replay the same draws forever") — F1's mapped-params answer is exactly `coupled_stage_kernel` (sampler.cu:807) |
| verify.cpp:381-390, 1499-1500, 3504-3516 | ✓ | `early_ple_flag_mode` default 2 on HIP; layer-1 `wait_flag_ge(m_flag_,1)` + `copy_from_mapped(ple_)`; ar_on raise blocks (:3393-3403/:3504-3516) — **(amended per review)**: the :1499 wait is the PLE consumer ONLY inside the `ar_on()` graph; under the doorbell graph `m_flag_ >= ring` is the CPU-rows-ready signal (:2460/:2466), so F5's redesign keeps every raise exactly as today and only overlaps the gather with the k==0 host service (thread-raise remains valid for ar_on() only) |
| expert_source.cpp:5251, 5316-5327 | ✓ | `P.fetch` copy-engine callback; per-token `native_quant_act`/`act_quant_q8_1` loop — F3 replaces it for q8_1-format layers, F4b shares `P.staging` (:5232-5233) |
| expert_cache.cpp:807-855 | ✓ | `fill_slot_blocking` (HIP: single-expert staging + blocking cudaMemcpy), `fill_slot_queued` (pageable async — degenerates on HIP), `fill_slot_queued_staged` (:860-876). The batching pattern to copy is `NibbleCache` (generate.cpp:3232-3289: kBatch=64, one stream, ordered staging reuse) |
| generate.cpp:9344-9368 / 9528-9541 / 9380-9385 | ✓ | TWO serial blocking loops (CUDA0 + per-stage), verify_slot read-back checks after each (ONE slot per boot by design, :9374-9383); fill-rate line at :9405 is the F7c instrumentation target; the ALL-slots engine byte gate is `STRATA_VERIFY_COMPLEMENT` (:9409-9418 — reads back and hashes every filled slot) — **(amended per review)** |
| generate.cpp:22585-22594 | ✓ | `penalty_rows` + blocking `cudaMemcpy` H2D per window, between draft and `ver.run` (:22624) |
| generate.cpp:3527-3539 | ✓ | B6 queue-priming `#if defined(STRATA_USE_HIP) && !defined(STRATA_HIP_GFX906)` — excluded from gfx906 (F7c interaction, below) |
| expert_helper_protocol.hpp | ✓ | kProtoVersion=2, feature bits :83-96, frame types :101-169, extension rule: new frame type + feature bit, old peer = typed refusal, never sent unadvertised (kBeginQ8K gate at expert_helper_socket.cpp:631-633 is the client-side pattern). ONE-outstanding-request rule per socket (expert_helper_socket.cpp:607-611) constrains F4's prefetch lane |
| tools: quant_quality.py, ab9_driver.py, lane scripts | ✓ | `tools/quant_quality.py` (teacher-forced KL; qqNIGHT.json drives a real serve engine); `bench/ab-v0210/rpcprof/ab9_driver.py`; lane toolkit `gpu-rounds/lane_boot.sh` (stop-all → setsid --fork boot → fresh ready line + /v1/models probe), `lane_judges.sh` (fill80k + tg80k, ctx 442400), `lane_restore.sh`, `make_variant.sh` (one-line-diff variant configs+launchers) |
| tests | ✓ | `tests/core/expert_helper_f32_window_test.cpp` (host-only socketpair fake-helper + negotiation + default-off equivalence + golden GPU-quant compare harness) is the exact pattern for F2/F4 engine tests; `tests/core/expert_helper_socket_test.cpp` (protocol), `tests/core/expert_helper_arena_cap_test.cpp`, `src/core/arch_defaults_test.cpp`, `src/kernels/gr_parity.cpp`; `tests/hip/ple_iq4.cpp` (PLE parity); CMake `STRATA_BUILD_TESTS` (CMakeLists.txt:219-222) |

## Master table

| Item | Phase | Commits | Effort | Validation cost (boots / GPU-hours) | Expected gain |
|---|---|---|---|---|---|
| F6 boot fill queued+parallel | P0 | 3 | S-M | 2 boots, ~0.75 h (byte gate = one `STRATA_VERIFY_COMPLEMENT` all-slots boot, gate-on vs gate-off; 1 qq run at promotion; tg80k leg held — Re-baselining) | BT: −15…−35 s/boot (placeholder, re-based post-AUX_CPUS); TG/PP 0 |
| F8 penalty-hist staging | P0 | 1-2 | S | shares the P0 gate boot (1 qq KL boot) | WT +0.1-0.2% (placeholder) |
| F1 in-graph sampler (mapped params) | P0 | 3 | S-M | +1 KL boot (sampled config), ~1 h | WT ~+1% on sampled + penalized-greedy turns (placeholder); 0 plain greedy |
| F7c fill-phase instrumentation | P0 | 1 | S | 0 dedicated (rides every later boot; FIRST code item — mandatory, Re-baselining) | none direct; enables F7ab, B6, attribution |
| F2 helper xq ring | P1 | 4 | M | 2 qq-gate boots + 3× harness, ~1.5 h (judges legs HELD for band restoration — Re-baselining) | WT +2-4% (placeholder) |
| F3 CPU-pool act from ring | P1 | 1-2 | S (post-F2) | +1 qq gate boot, ~0.5 h (judges HELD — Re-baselining) | WT +0.5-2% (placeholder) |
| F5 PLE gather overlap (k==0) | P2 | 1-2 | S | +1 qq gate boot, ~0.5 h (judges HELD — Re-baselining) | WT +0.5-3%, bounded by the overlapped ms_pool slice (placeholder) |
| F7ab O_DIRECT + offset-sorted boot reads | P2 | 2-3 | M | 6-8 boots (class-distribution), ~4 h | PP +7-20% on slow-class boots (EV +7-10%); BT + |
| F4 speculative prefetch (helper + PCIe) | P3 | 5-6 | L | ≥3 boots + tuning, ~4-6 h | WT +5-15% (hit-rate dependent) |

Totals: ~20 commits, ~20-24 GPU-hours of validation across roughly 15-20 boots. Phase P0+P1 alone targets WT +2.5-6% and −20-40 s/boot with S/M effort and low risk. *(Amended per review)*: every WT%/BT% figure above is a placeholder measured under the Nov-6 pinning fault, and every tg80k judge leg is conditional on warm-TG band restoration — see **Re-baselining** below; qqNIGHT KL gates proceed now.

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
- **F5 after F2/F3**: it edits the same `Verifier::run()` service loop (verify.cpp:3504-3619); landing it after the ring avoids rebase churn on the hottest file. No functional dependency. *(Amended per review)*: F5 is coded ONLY from the amended (redesigned) section below — the review's blocking condition; the redesign leaves the flag protocol untouched, so no new functional edge appears.
- **F7ab parallel to F5**: disjoint files (generate.cpp fill loops, expert_source read paths); its 6-8-boot measurement campaign can run while F5 is coded.
- **F4 last**: L effort, M risk; its value is bounded below by F2+F5 shrinking the same chain first (re-measure the residual `finish` wait after P1+P2 before committing to F4's full scope — F4's refinement note says the begin RTT is already partly hidden; if the post-P2 residual is small, F4's helper half shrinks to the PCIe half).

Hard dependency edges: F3→F2 (ring); F4a→(F2, protocol C1/C2); F4b→F7c (thrash counters); nothing else. *(Amended per review)* measurement edges, not code: every tg80k judge leg and every WT%/BT% acceptance number → confirmed warm-TG band restoration on the adopted AUX_CPUS config (Re-baselining); F7c → first code item, mandatory, before everything.

## Re-baselining — AUX_CPUS adopted + the post-16:21 warm-TG collapse (new per review, 2026-11-15)

The plan was written before two facts landed (journal 18bf follow-ups, today):

1. **The stage-thread pinning fault (live since Nov 6) is FIXED, and the fix is adopted.** 64 `strata-stage` threads + watchdog/issue/fill were parked on CPU 0 on ~96.5% of boots since Nov 6; `STRATA_AUX_CPUS=3,5,7,9,11,13,15` is now **adopted in the prod config env** by operator decision. Every baseline this plan was calibrated on — the 422-671 MB/s fill-rate band, the 432-455/352-379 PP classes, the 123 ms/window WT denominator, the 2.7-5.3 ms host-quantize tax — was measured with the fault live: they are **placeholders**.
2. **Warm TG did NOT recover and is still collapsed.** Warm TG has been below the 26-34 in-band on EVERY boot/config/binary since 16:21 today (collapsed band 18.2-23.7; last in-band boots A1MMAP-1/2/3 at 15:49-16:08). Placement is verified perfect (AUXCPU1 judged warm TG 19.5), so the last host-side theory is falsified; a persistent post-16:21 box-state change (GPU under-load clocks pending; the A1MMAP4 config differential) is the live suspect, and a post-reboot re-measurement is in flight.

Binding consequences for every section above and below:

- **F7c instrumentation lands FIRST — mandatory** (Day 1, before any other code item): every subsequent boot then self-attributes both its fill phases and its decode host chain (`ms_wait/ms_pool/ms_host`) on the adopted config, for free.
- **qqNIGHT KL gates may proceed NOW.** They are speed-independent (18be's explicit note): code, host tests, builds, and KL boots for every item are not blocked by the box state.
- **ALL tg80k judge legs and WT%/BT% acceptance numbers WAIT for confirmed warm-TG band restoration** on the adopted config — then re-base once and re-state every target. Until then the judge legs in each validation ladder are HELD. Boot-side legs (fill80k, fill walls, boundary) may run sooner: fills and boundary were in normal bands in the collapsed state (AUXCPU1: fill 403.0, boundary 246.1).
- **F6/F7ab boot-side measurements run on the adopted AUX_CPUS config with FRESH class bands**: the classes must re-establish themselves in each distribution; the pre-fix 422-671 MB/s and 432-455/352-379 bands are NOT carried forward as acceptance thresholds (the fill threads were among the parked-on-CPU-0 set, so the slow class is partially confounded by the fault).
- Lane hygiene while re-baselining: 18be's lesson (local tool CPU contention poisons TG) — keep planner/analyzer work off the box during judge legs.

---
# Phase 0 — quick wins

## F6 — boot slot pre-fill: queued, batched, parallel (BT win) (amended per review, 2026-11-15)

**Exact change list**

1. `src/core/expert_cache.cpp` / `include/strata/core/expert_cache.hpp` — new batched pinned-staged queued fill, copying `NibbleCache`'s discipline (generate.cpp:3232-3289: one stream, kBatch blobs per staging buffer, ordered reuse without events):
   - `bool ExpertCache::fill_slots_batched(const BatchFill* items, int n, std::string& err)` where `struct BatchFill { int32_t slot; const uint8_t* blob; int64_t bytes; }`.
   - Internals: a per-cache pinned host staging ring `h_stage_` (2 × kBatchStage × `blob_` bytes; kBatchStage = 8 → 2 × 8 × ~1.38 MB ≈ **22 MiB pinned per cache**, ~44 MiB for two cards; allocated on first use, freed on `close()`), a per-cache non-blocking stream `fill_stream_`, one `cudaEvent_t` per staging half. Loop: memcpy up to 8 blobs into the free half (pageable→pinned, ~RAM speed), `cudaMemcpyAsync` each into its device slot on `fill_stream_`, record event, swap half, `cudaEventSynchronize` only when both halves busy. This is the double-buffering the analysis warns about (a single reused staging buffer degenerates to blocking on HIP).
   - `fills_` accounting unchanged per blob (the boot line's MB/s math keeps its meaning).
2. `src/program/generate.cpp:9344-9368` (CUDA0 loop) and `:9528-9541` (per-split-stage loops) — behind `STRATA_BOOT_FILL_QUEUED` (default OFF, static read once like `early_ple_flag_mode()` at verify.cpp:381-390):
   - Replace the per-pair `fill_slot_blocking` (:9359 / :9535) with: `admit()` as today (rank order, unchanged capacity semantics) → queue into the batch pipeline → `fill_slots_batched` flush every 8 pairs and at loop end.
   - Parallelism: run the CUDA0 loop and each stage loop on their own `std::thread` (each already enters its device via `DeviceScope`/`OnDevice`; `srcp->blob_stable` is a read-only file-backed mapping — thread-safe; the `read_batch`/`ahead` std::async prefetch at :9346-9348 stays inside the CUDA0 thread only). Join all threads before the two `verify_slot` checks (:9380-9385, :9547-9550) — those run unchanged, once, after the joins. **Per-thread device scope (review note)**: `cudaSetDevice` is per-thread state — the stage loops' `OnDevice on(st.dev)` at :9450 does NOT transfer to a fresh `std::thread`; every spawned thread establishes its own scope explicitly (state it in the commit message).
   - Helper fill overlap: the kFill frame already carries the ranked pairs; verify at implementation time whether it is sent before these loops (helper fills concurrently — nothing to do) or after (then move the send before the loops, still byte-identical — frame content unchanged). Note in the commit message which was true.
3. No protocol, no kernel, no graph changes. Bit-exactness: same blob bytes → same slots. **Byte gates, stated exactly (review amendment)**: `verify_slot` reads back ONE slot per boot by design (:9374-9383's own comment, both loops) — a spot check, not the all-slots proof; the exhaustive host-side proof is the in-tree batched-vs-blocking test (b); the engine-level all-slots gate is `STRATA_VERIFY_COMPLEMENT` (generate.cpp:9409-9418 — reads back and hashes EVERY filled slot), used in the ladder below.

**Commit slicing** (each builds green, OFF = today's bytes):
- `feat(core): ExpertCache::fill_slots_batched — pinned double-buffered queued H2D (unused)` — new API + unit test, no callers.
- `feat(core): STRATA_BOOT_FILL_QUEUED — batched pipeline for the boot profile fills` — both loops switch behind the gate; still single-threaded.
- `perf(core): parallelize the per-card boot profile fills behind STRATA_BOOT_FILL_QUEUED` — the thread split, same gate.

**Validation ladder**
- (a) Builds: `tools/mixed_gpu/build_pin.sh f6-<n>` from the worktree (guards: gfx906 ON, no nvcc, libamdhip64, MMQ line). Host tests: `cmake -DSTRATA_BUILD_TESTS=ON` build; run `ctest -R expert_cache` (per_layer/segmented/slot_guard tests exist at tests/core/).
- (b) In-tree: new `tests/core/expert_cache_batched_test.cpp` — fill N slots through the batched API with random blobs, `verify_slot` each (the engine's own byte check, applied to EVERY slot here — the exhaustive host-side proof, unlike the boot line's one-slot spot check), assert slot bytes identical to `fill_slot_blocking` results. Runs in the build-window ctest (needs a CUDA context; same class as the existing expert_cache tests).
- (c) Engine gate: variant launcher via `gpu-rounds/make_variant.sh` (+env `STRATA_BOOT_FILL_QUEUED=1`), `lane_boot.sh` (fresh ready line + /v1/models probe). **All-slots byte gate (review amendment)**: one additional boot with `STRATA_VERIFY_COMPLEMENT=1`, gate-on vs gate-off — the per-slot checksum vectors must be EQUAL (closes the wrong-slot/corruption class on real packs; ~+0.25 boot). **Fill-rate, per-phase (review amendment)**: read the F7c fields on the :9405 line — H2D-phase rate ≥2× today's; the END-TO-END pre-fill wall is read-bound on cache-cold boots (the wall is ~max(read, H2D); at 671 MB/s the ~9.6 GiB reads alone are ~14.3 s of the observed ~14.8 s wall), so an end-to-end ×2 claim is caveated to read-warm boots — exactly why F7c lands first. Judges: fill80k/fresh-read leg unchanged ±noise (this item does not touch the read path); the tg80k leg is HELD per Re-baselining. One `quant_quality.py` qqNIGHT run with the env at promotion time (belt-and-braces; the item is byte-gated by the complement boot).
- (d) Rollback: `gpu-rounds/lane_restore.sh`; delete the variant launcher; the gate stays default-OFF in the tree; serving pin untouched (`36ecbe98`).

**Risks + mitigations**
- HIP pageable-async trap (the reason `fill_slot_blocking` stages at all, expert_cache.cpp:819-826): mitigated by pinned-only staging in the new API; never pass the mmap pointer to `cudaMemcpyAsync`.
- Staging-buffer reuse race: two-half ring + one event per half on ONE stream per cache — the NibbleCache-proven shape (generate.cpp:3229-3231 comment: "One stream, so the staging buffer's reuse is ordered without events" — we add the second half with one event, strictly weaker assumption).
- Thread-safety of `admit()` across caches: each thread touches only ITS cache (`xcache` vs `st.cache`) — no shared mutable state; assert at review that no global allocator path is shared without a lock (aux_cpus pin helper at :9348 stays in one thread).
- Boot RAM: +44 MiB pinned transiently — negligible against the 46 GiB fill-phase staging arena (18at).
- Baseline confound (review note): the 422-671 MB/s band and the ≥2× expectation were measured with the fill/stage threads parked on CPU 0 (the Nov-6 pinning fault); every number re-bases on the adopted `STRATA_AUX_CPUS` config (Re-baselining).
- #267-class: no new GPU-visible flags, no spin kernels, no mapped rings — nothing to invalidate on stream rebuild; the fill streams are destroyed at `close()`.

**Definition of done**: 3 clean boots with the gate on showing the H2D fill phase ≥2× faster in the F7c per-phase fields (end-to-end pre-fill wall reported per boot, caveated as read-bound on cache-cold boots; end-to-end ≥2× claimed only when reads are warm), `STRATA_VERIFY_COMPLEMENT` all-slots checksums EQUAL gate-on vs gate-off, `verify_slot` pass on every boot, fill80k judges leg unchanged (tg80k held until band restoration), one clean qq run at promotion; journal round records the before/after per-phase fill rates on the adopted AUX_CPUS config.

## F8 — penalty-history staging off the round critical path (amended per review, 2026-11-15)

**Exact change list**

1. `src/program/generate.cpp:22585-22594` — today: `penalty_rows(...)` into the pageable `hist_stage` vector + a **blocking** `cudaMemcpy` H2D per window, after acceptance/draft, before `ver.run` (:22624).
2. Change (mapped staging the graph copies — the `copy_i32_from_mapped` pattern at verify.cpp:1427/1435/1462-1466):
   - `Verifier` gains a mapped buffer `m_hist_` (via `mapped()`, verify.cpp:514-528) sized `kMaxTok × kHistMax × sizeof(int32_t)` = 8 × 64 × 4 = **2 KiB** + one mapped control word `{len, valid}`; API `Verifier::stage_hist(const int32_t* rows, int T, int hist_n)` (host memcpy + fence + word write).
   - `record_window` head gains, behind the gate, a `copy_i32_from_mapped(hist_dev_, m_hist_, T*hist_n_word, cs)` guarded by the mapped valid word (a fixed upper bound is captured; the copy length word is read from mapped at replay — same technique as the sampler below; alternatively copy the full 2 KiB unconditionally and let `hist_n` gate reads — simpler, bit-trivial, preferred).
   - `generate.cpp`: build the rows **after THIS window's draft, before launch** — the rows need this window's draft tokens (`window.data()` at :22590-22591), and the serial decode loop returns from the previous `ver.run` before the draft exists, so "during the previous window's GPU run" is NOT realizable (review correction). The realizable win — and all that is needed — is the mapped stage itself: same build point as today, `stage_hist` call, and the blocking `cudaMemcpy` at :22593 deleted on the gated path.
   - Bit-exactness: same `penalty_rows` bytes, same device destination, earlier copy — the analysis's argument (same bytes, earlier) preserved.
3. Gate `STRATA_PENALTY_MAPPED` (default OFF). OFF: today's vector + blocking copy, byte for byte.

**Commit slicing**
- `feat(core): Verifier::stage_hist — mapped penalty-history staging (unused)` — buffer + copy-in-graph behind gate, no caller.
- `perf(serve): stage the penalty hist via the mapped buffer after the draft (blocking H2D removed)` — generate.cpp switches behind the same gate.

**Validation ladder**
- (a) build_pin.sh + host tests build.
- (b) In-tree: no dedicated harness needed beyond a small case in `tests/core/` (host-only) asserting `stage_hist` writes the same bytes the vector path stages (golden compare, the f32_window-test pattern); PLE/iq parity harnesses untouched.
- (c) Engine gate: one qqNIGHT boot with `STRATA_PENALTY_MAPPED=1` — the qq harness runs penalized+sampled windows (teacher-forced logprobs), KL 0.00000 vs the F6-gate run (or the phase-0 baseline). Boot-gate discipline via lane_boot.sh; judges optional (expected inside noise on tg80k, ~0.05-0.2 ms/round).
- (d) Rollback: lane_restore.sh + drop the env from the variant; gate default OFF in tree.

**Risks + mitigations**
- First window: identical path — every window stages after its OWN draft in the amended design, so no previous-window special case exists (the mapped validity word still guards the in-graph copy).
- kHistMax cap (review note): kHistMax=64 is an asserted constant, not a verified one — at implementation, verify the mapped buffer covers the captured graph's fixed upper bound for the `penalty_last_n` ceiling this engine actually runs, and size to the capture-time constant.
- hist_n changes mid-request (penalty_last_n, consumed growth): the mapped len word is written before launch and read at replay; the captured fixed-cap copy (2 KiB) makes length changes replay-safe.
- Lifetime: `m_hist_` is Verifier-owned mapped memory alongside `m_tok_` (same destructor; no cross-window GPU reference beyond the in-graph copy — completes before the window's tail).
- #267-class: no thread, no flag — only a mapped buffer the graph head copies; nothing to invalidate on stream rebuild beyond the existing ring family.

**Definition of done**: qq run KL 0.00000 with the gate on; the round-path trace shows no per-window blocking H2D between draft and run (STRATA_TRACE or the serve summary's between-window ms); journal round.

## F1 — in-graph head sampler via mapped parameters (sampled/penalized requests) (amended per review, 2026-11-15)

**Exact change list**

1. `src/kernels/cuda/sampler.cu` — new mapped-input staging kernel + captured sampler stage, following `coupled_stage_kernel` (sampler.cu:807-815: copy `SamplerParams` + hist ring from mapped into device copies inside the graph) and `coupled_draft_stage` (:1338-1344 host launcher):
   - `__global__ void sample_stage_mapped_kernel(const volatile SamplerParams* mp, const volatile int32_t* mhist, const volatile int32_t* mspec, SamplerParams* dp, int32_t* dhist, int32_t* dspec, int32_t* dmode)` — copies `sizeof(SamplerParams)/4` words, `hist_cap` words, `spec_cap` words, and the mode word (`0=none, 1=spec, 2=proposal`, `nq`) from mapped into the device buffers the existing kernels read.
   - The **existing** `sample_tokens` / `sample_tokens_spec` / `sample_tokens_proposal` device kernels are then recorded in `record_window`'s tail (after `head_logits_` are complete, beside the greedy argmax at verify.cpp:2658-2664) as a mode-switching capture: record the three launches guarded by mapped mode (the mode word is read at replay, so ONE captured path is chosen per replay — capture the proposal variant reading `dmode` and branching host-side is NOT possible in-graph; instead capture all three tail launches, each wrapped in a device-side guard kernel is over-engineering — **preferred design**: capture ONE generic kernel `sample_tokens_mapped(head_logits_, T, n_vocab_, dp, dhist, dmode, dspec, m_out_, cs)` whose internals are the existing three kernels' bodies selected on the device-read mode; single code path, same math order, same Philox statement order). SamplerParams.counter is read from `dp` (mapped-staged with `counter = pos0` written per window) — the "replay the same draws forever" hazard (verify.cpp:3678-3681) is exactly what mapped params fix; the analysis's bit-exactness argument (same Philox(seed, pos0+t)) is preserved because the kernel reads `pos0` from the mapped counter at replay.
2. `src/core/verify.cpp`:
   - `record_window`: gated (`STRATA_SAMPLER_IN_GRAPH`, default OFF) capture of stage+sample at the tail; `m_sp_`/`m_hist_` (shared with F8 — take F8's `m_hist_` if landed, else its own), `m_spec_` sized `kVerifyMaxT × (1+kSpecQStride) × 4` B (~a few KiB) — byte math: kVerifyMaxT=8, kSpecQStride small (~12) → ≤ ~400 B; plus `m_mode_` word.
   - `run()` :3687-3726: when the gate is on AND the captured sampler is armed AND `sampling_.logit_bias == nullptr` (logit_bias stays host-side — fallback), skip the out-of-graph launch (:3721) and the second `cudaStreamSynchronize` (:3722) — the :3661 sync already covers the graph; write `sp`, mode, `nq`, and (with F8) hist rows into mapped at `stage_inputs` (verify.cpp:3146) / just before launch. **Arming includes penalized-GREEDY (review amendment)**: the host block at :3687 fires for `hist_d_ != nullptr` even when `sampling_.greedy` — those requests arm the generic kernel too (the mode word already supports it; the temperature-0 path is deterministic argmax, so draws are token-identical by construction). Excluding them would silently halve F8+F1's combined value on that request class; plain greedy (`hist_d_ == nullptr`) never arms — it is already served in-graph at :2658-2664. Included by design and documented here per the review, not excluded.
   - Fallback rule: any condition unmet → today's host path, byte for byte. Greedy default is untouched (already in-graph, :2658-2664).
3. Bit-exactness by construction: identical kernel arithmetic, identical inputs (logits identical — same graph; params/hist/spec identical bytes via mapped staging; counter=pos0 identical), so draws are identical. The analysis's caveat ("same Philox(seed,pos0+t)") is the acceptance criterion of the parity test below.

**Commit slicing**
- `feat(kernels): sample_stage_mapped_kernel + sample_tokens_mapped (unused)` — kernels only.
- `feat(core): STRATA_SAMPLER_IN_GRAPH — capture the head sampler from mapped params` — record_window/run() wiring, gate default OFF, fallback complete.
- `test(core): sampler mapped-vs-host parity (golden draws)` — the parity harness (or fold into commit 2; keep separate so the test lands visibly).

**Validation ladder**
- (a) build_pin.sh + host build (sampler tests compile host-side? kernels are .cu — the parity test is a build-window ctest).
- (b) NEW `tests/cuda/sampler_mapped_parity.cu` (beside the prefill parity tests): random logits + params + hist + spec rows; run host `sample_tokens` family vs the mapped captured path (graph capture + replay with fresh counter each replay); assert token-identical AND draw-identical (compare the Philox state via repeated calls) over ≥1000 windows, T∈{1,2,5,6,8}, mode∈{spec,proposal,penalized-greedy}, plain greedy excluded (already in-graph); the penalized-greedy arm compares against the host's own hist+greedy path at :3687.
- (c) Engine gate: one qq boot with `STRATA_SAMPLER_IN_GRAPH=1` (qq drives sampled teacher-forced logprobs) — KL 0.00000. Judges leg optional (gain only on sampled turns; the operator's tg80k leg samples — expect ~+0.5-1% on that leg if measurable).
- (d) Rollback: lane_restore.sh; env off; gate default OFF in tree.

**Risks + mitigations**
- Mode/shape baking: T and n_vocab are capture-time constants (they already are — graphs are per-T); mode/nq/params are replay-time mapped reads. The one baked thing that must never change per replay: buffer sizes (fixed caps).
- `logit_bias`: stays on the fallback path (rare in prod; noted).
- Penalized-greedy: armed by design (above) — the generic kernel's mode word covers it deterministically; if the operator later prefers exclusion, it must be documented as a deliberate value loss on that request class.
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

## F2 — ship the GPU's q8_1 image to the helper via a mapped ring (WT +2-4%) (amended per review, 2026-11-15)

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
   - Allocation (init, beside the other mapped rings via `mapped()` :514-528): `m_xq_` = **2 slots × max_t_ × (N/32) × 36 B**. Byte math: N=2560 → 80 blocks × 36 B = 2880 B/row; max_t_=8 (kMaxTok, protocol.hpp:64) → 23,040 B/slot; depth 2 = **46,080 B ≈ 45 KiB per Verifier**; 2 stages (split@30) = **90 KiB pinned-portable total** — five orders below the mmap-ready-time-killer class (A1's 50.7 GiB PLE mmap, 18bc; RSS impact nil). Plus `m_xq_seq_` = 2 × 4 B. Depth-2 indexed by step parity `(step & 1)` removes any G=2 group-overlap question; the doorbell protocol's happens-before (host reads slot k only between ring k and flagB k; GPU cannot reach publish k+1 before flagB k) already makes depth-1 safe — depth-2 is margin for the waitA/waitB overlap review. The allocation itself is made only when the gate reads on at init, so OFF boots keep an identical RSS footprint (review note). **Per-window `m_xq_seq_` reset (the review's blocking bug, fixed)**: ring values restart at 1 every window (`(l-lb_)*G+grp+1` is capture-baked), and `stage_inputs` resets the sibling words (`h_seq_`/`h_flag_`/flagA/flagB → 0, verify.cpp:3160-3163) — the ring MUST get the same reset: `*(volatile uint32_t*) m_xq_seq_[0] = 0; *(volatile uint32_t*) m_xq_seq_[1] = 0;` beside them. Without it, window ≥2's `m_xq_seq_[step&1] >= want` (want=1..) passes instantly on stale values from the previous window and the host reads a STALE image. Safe: the previous window's graph is fully synced at :3661 before the next `stage_inputs`.
   - `record_window`, end of `mid()` (after :2357), gated `STRATA_HELPER_XQ_RING` (default OFF, static read — OFF adds NO kernel to the capture, graph bytes identical): when gate AND `expert_layout().native` AND the doorbell graph is active (not `ar_on()`) AND **the helper tier is actually configured** (`remote_opt_`/helper present — publishing with no consumer is pure waste; review note): `doorbell_xq_publish(nat_xq_ + tb*(N/32)*36, n*(N/32)*36, d_m_xq_ + (step&1)*slot_bytes, m_xq_seq_ + (step&1), want, cs)`. For folded layers the image at that point is the ROTATED one (:2352) — correct for F3's CPU-pool consumer; helper never claims folded layers so the F2 consumer never sees a folded image.
   - **QFUSE note (review amendment)**: ring use is NOT gated on `!q8_ffn`. `STRATA_QFUSE` defaults **1** (arch_defaults.cpp:86) and q8_ffn layers (verify.cpp:2207 — `gr_read_group(1, true, ...)`, "nat_xq_ holds the MoE input's q8_1") get their image from the fused GR read, not from `quantize_q8_1_rows`; gating on `!q8_ffn` would void most of F2's value. Correctness for those layers rests on the parity chain — the QFUSE-producer leg below (`gr_parity.cpp:416-417` is the in-tree fourth-leg evidence: "the read's own q8_1 image of `mixed` must be the bytes native_quantize_q8_1 writes") — not on exclusion.
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
  - NEW `tests/core/expert_helper_xq_ring_test.cpp`: (i) default-off equivalence — gate unset → proxy_begin sends the CPU-quantized frame bytes, byte-compare against a golden vector; (ii) gate on + ring image → frame bytes IDENTICAL to (i) given the same input activations (this is the whole proof: producer change, bytes equal); (iii) ring-invalid → fallback bytes equal (i); (iv) folded layer never takes the ring; (v) **multi-window stale-seq case (review amendment)**: a window-2 replay with the `stage_inputs` `m_xq_seq_` reset must NOT pass the wait on window-1's stale seq values, and the consumed image equals window 2's (catches the blocking bug in-tree, not on the lane).
  - NEW `tests/kernels/xq_triple_parity.cpp` (or extend an existing quantize test): `quantize_q8_1_rows` (GPU, build window) vs `native_quantize_q8_1` vs `quantize_q8_1_cpu` on random rows incl. rotated inputs — the bit-exact chain the analysis cites, now asserted on THIS build — **plus the QFUSE-producer leg (review amendment)**: the fused GR read's own q8_1 image joins the chain (either a QFUSE arm in this test or `gr_parity` run in the same ctest window; `gr_parity.cpp:416` already asserts it), giving **QFUSE == native == rows == cpu** — QFUSE is the producer for most images in the default config, so this leg is mandatory, not optional.
  - The f32_window test's golden GPU-quant dump-compare harness runs unchanged (regression guard for the shared payload builder).
- (c) Engine gate: variant launcher (+`STRATA_HELPER_XQ_RING=1`), `lane_boot.sh` boot-gate; `quant_quality.py run --config <qqNIGHT variant> --env STRATA_HELPER_XQ_RING=1` → `compare` vs baseline: **KL 0.00000, top-1 100%** — the campaign convention is ≥3 clean passes (18aw's 4/4-fail lesson: run the harness 3× before believing it). Then `lane_judges.sh` legs — **HELD until warm-TG band restoration (Re-baselining)**; when released: fill80k + tg80k ×2 boots, warm TG target +2-4% (out of the measured 2.7-5.3 ms/window host quantize tax — itself a placeholder pending re-basing). Watch the helper log's reqs/frame — unchanged 37/window expected (claims don't change); this check needs no judge leg and may be read off the KL boots.
- (d) Rollback: `lane_restore.sh`, delete variant launcher; gate default OFF in tree; pin untouched until the operator promotes.

**Risks + mitigations**
- **#267-class (mapped ring + new GPU→host seq)**: the ring's lifetime is Verifier-owned, identical to `m_x_/m_ids_/m_seq_` (allocated in init, freed in dtor, graph capture embeds the device alias — rebuilds re-capture the same pointers). The `m_xq_seq_` wait reuses the main doorbell's timeout+release path, so a wedged tail kernel is diagnosed by the SAME NEVER-RANG/TIMEOUT trace, not a new hang class. The B1/B2 lessons (18ba: "waits released but GPU did not finish = stream wedged on real work, never the flags") apply verbatim: the wait is bounded, the release path exists, and no spin kernel reads `m_xq_` after the copy completes (single-shot kernel, done before flagB).
- Ring sizing/wraparound: depth-2 by step parity; slot covers max_t_=8 rows; a window with T>8 cannot occur (kMaxTok=8 is the protocol's own ceiling, checked at proxy_begin :626).
- mmap/RSS: 90 KiB pinned total — negligible (the A1 lesson is about GiB-scale mappings; state the math in the commit message).
- Mixed engine/helper builds: no wire change — safe.
- Q8_K / F32-window precedence: ring path sits AFTER both (it replaces only the default :744 path) — interaction-free by ordering.
- Stale `m_xq_seq_` across windows (the review's blocking bug): fixed by the `stage_inputs` reset; the multi-window test case (v) is the guard.
- QFUSE producer divergence: covered by the fourth parity leg — if `gr_parity` ever fails, the ring falls back per-layer via `ring_valid` until fixed.
- Fallback completeness: every miss path (folded, non-native, ring stale, gate off) lands on today's code — the review checklist item is "no path reaches the ring image with a non-q8_1 wire format".

**Definition of done**: xq_triple_parity green INCL. the QFUSE-producer leg (QFUSE==native==rows==cpu); frame-bytes equality test green incl. the multi-window stale-seq case; 3× qq harness KL 0.00000 with gate on; 2× judges boots with warm TG ≥ +2% (held until band restoration, then re-based) and helper reqs/frame unchanged; journal round with the measured per-window host-quantize delta (expect ~2.7-5.3 ms → ~0, re-based post-AUX_CPUS).

## F3 — CPU-pool activation quantize from the F2 ring (WT +0.5-2%) (amended per review, 2026-11-15)

**Exact change list**

1. `src/core/expert_source.cpp:5316-5327` (`expert_pool_dispatch_multi`'s per-token quantize loop) — behind `STRATA_CPU_POOL_XQ` (default OFF; separate from F2's gate so the two effects A/B independently, both reading the same `cur_xq()` plumbing):
   - When `ring_valid && lay.fmt[layer].gu_act == GGML_TYPE_Q8_1` (native layout, q8_1 act format): replace the per-token `native_quant_act` loop (:5324) with ONE memcpy into the native-path destination **`d.nact_multi + t*kNativeActBytes`** — NOT `d.act_multi[t]` (:5322/:5326 are the Q2-native and non-native destinations; review correction). Contiguous fast path when `kNativeActBytes == (N/32)*36` (per-row 2880 B; n≤8 rows → ≤23 KiB memcpy vs n × 10-30 µs quantize); if the stride differs, memcpy per-row with the actual stride or take the per-layer fallback. The `ep` skip rule (:5320, tokens whose experts all ran on GPUs) does not apply on this path (any_cpu true ⇒ some token needs CPU rows).
   - Parity argument (from the analysis, now sharpened by the anchor walk): for UNFOLDED layers ring = `quantize_q8_1_rows(xm)`; for FOLDED layers ring = `quantize_q8_1_rows(hadamard(xm))` (verify.cpp:2351-2352) — and `native_quant_act(fmt, x)` produces exactly `quantize(rot(x))` for folded fmts (verify.cpp:2344-2348's own contract: the CPU pool "rotates its own in native_quant_act"). Both cases are byte-equal; the q8_1-format gate excludes every other format; the triple-parity test (F2) asserts the chain including rotated inputs.
   - Layout check at implementation time: `kNativeActBytes == (N/32)*36` for the Q8_1 act format, and the `d.nact_multi` rows are contiguous at that stride — assert once at init, fall back per-layer otherwise (belt-and-braces; a mismatch would be an engine bug, not a plan risk).
2. Q2-native (`act_quant_any`, :5321-5322) and q8_0/non-native (:5326) paths: untouched.
3. Gate read: same static pattern; OFF = today's loop byte for byte.

**Commit slicing**
- `feat(core): STRATA_CPU_POOL_XQ — CPU-pool act image from the m_xq_ ring (q8_1 layers)` — one commit (small, self-contained, needs F2's commit 2 landed).

**Validation ladder**
- (a) build_pin.sh + host tests build.
- (b) Extend `tests/kernels/xq_triple_parity.cpp` with the act-layout assertion (ring layout == `native_quant_act` output layout, folded and unfolded, random rows). Host-side dispatch test: a fake dispatch in `tests/core/` (the dispatch is host code around the quantizer) asserting `d.nact_multi` bytes equal on both paths (the native destination per :5324; review correction).
- (c) Engine gate: one qq boot with `STRATA_HELPER_XQ_RING=1 STRATA_CPU_POOL_XQ=1` → KL 0.00000 ×3 (both gates on is the shipping config; also run one gate-combination matrix boot: ring-only, pool-only impossible (pool needs ring), off — 2 combinations); judges ×2 HELD until band restoration (Re-baselining) — when released: warm TG +0.5-2% on top of F2 (expect the c1→c2 dispatch slice in the serve summary to shrink; F7c's `ms_pool` field shows it directly).
- (d) Rollback: same as F2 (env off / variant delete / pin untouched).

**Risks + mitigations**
- Wrong-image-consumed (the one real risk): the ring slot is step-parity indexed and read before flagB — same window, same group; the service loop sets `cur_xq()` per step, the dispatch cannot see a stale slot (it runs inside the same serve step). Review item: `cur_xq()` must be captured before `pool()` and cleared after.
- Fallback: format gate + `ring_valid` per layer — any miss takes today's loop.
- Benign, noted for posterity (review): with the ring memcpy, tokens the ep-rule skips (:5320) receive fresh bytes where today they keep stale ones — unread either way; say so in the commit message so nobody "fixes" it later.

**Definition of done**: parity test green; KL 0.00000 ×3 on the combined config; judges (after band restoration) show the additional `ms_pool` reduction; journal round.

---
# Phase 2

## F5 — PLE gather overlapped with the k==0 host service (amended per review, 2026-11-15) (WT +0.5-3%, bounded by the overlapped ms_pool slice)

**Redesign note — why the original design was dropped (the review's blocking finding).** The original section raised `m_flag_ = 1` early from a side thread and skipped the host's k==0 raise, on the premise that the graph's `wait_flag_ge(m_flag_, 1)` at verify.cpp:1499 is the PLE consumer. That premise holds ONLY inside the `ar_on()` graph — :1497-1500 guards the wait with `if (ar_on())` before `copy_from_mapped(ple_, m_ple_, ...)`. Prod runs the **doorbell** graph, where `m_flag_` carries a different, load-bearing contract: in `post()`, each step's `wait_flag_ge(m_flag_, ring, cs)` at :2460 (the code's own comment: "the CPU's share is in the mapped rows") or the fused `copy_rows_from_mapped_after(m_flag_, ring, ...)` at :2466 gates the copy of the **CPU's computed expert rows** out of `m_ymiss_` — for k==0, ring=1, so layer 0's waitCPU is exactly `m_flag_ >= 1`, the value the thread would have raised early. The host raises `*flag = want` at :3616 only AFTER `pool()` returns (:3578-3580 — rows complete); flagA goes up mid-dispatch from `publish_plan` (:3867-3871, via `P.publish` at expert_source.cpp:5249, BEFORE the CPU quantize/compute at :5316+), and flagB goes up **mid-pool** from `fetch_dma`'s `cudaLaunchHostFunc` when the PCIe DMA lands (:3839-3865). The GPU routinely sits at `wait_flag_ge(m_flag_, ring)` while the CPU rows are still being written — that IS the design. An early `m_flag_ = 1` would let the GPU pass layer-0 waitCPU the moment flagB(1) lands mid-pool and copy `m_ymiss_` **mid-write** — silent numeric corruption, and schedule-dependent (it needs flagB mid-pool AND the copy started before the rows finish to even show), so the qq KL gate can PASS on a losing schedule: no gate certifies the old design's absence-of-race. Dropped structurally, not patched by testing.

**Exact change list** (redesigned; the doorbell graph is the prod path)

1. `src/core/verify.cpp` — gather thread overlapped with the k==0 host service:
   - `PleGather` RAII in the `NextLaunch` shape (verify.cpp:3480-3494: local struct, `std::thread th`, destructor joins). Local per-`run()` invocation; the destructor covers every error return (join before any return completes).
   ```
   struct PleGather {                      // RAII: joined at k==0 / at scope exit
       std::thread th; std::string err;    // err read after join
       ~PleGather() { if (th.joinable()) th.join(); }
   };
   ```
   - Start point: AFTER `stage_inputs` returns (verify.cpp:3146-3175). The row-id derivation needs the staged tokens, and `stage_inputs` resets the flag words at :3160-3163 — starting after the reset removes every ordering question (in this redesign the thread never writes a flag at all, so the original design's raise-wiped-by-reset hazard disappears entirely).
   - Thread body: derive the window's PLE row ids (hoist the `ple_rows` computation the inline paths use at :3377-3386/:3609-3611 into a helper all call sites share), then `ss.ple.table->gather_batch(ple_rows, T, h_ple_, err)`. On failure record `err` — **no flag write, no raise**; `run()` checks `ple_gather.err` at the post-sync point (:3661-3663) and returns the error cleanly (the hang-avoidance rule kept from the original design: a failed gather must not become a #267-style window death — here it cannot, because the join precedes the raise).
   - Service loop, gated `STRATA_PLE_ASYNC_GATHER` (default OFF), doorbell path: at k==0, **JOIN the thread after `pool()` returns (:3578-3581) and BEFORE the UNCHANGED `*flag = want` (:3616)**. The k==0 inline gather (:3609-3615) is deleted on the gated path; every flag write (flagA/flagB orchestration, `*flag = want`) stays byte-for-byte today's code — exactly one raiser per value, exactly as today. The gather now overlaps the classify/publish/pool host work instead of running serially after it.
   - `do_ple` false or !ple_on: no thread, today's logic untouched.
2. `ar_on()` variant ONLY (non-prod): the thread-raise design remains valid there — `m_flag_ >= 1` gates only the PLE copy (:1499) — so the ar raise blocks (:3393-3403 pre-launch, :3504-3516 post-launch gather) may keep the raise-from-thread form (which also decouples the raise from the `early_ple_flag_mode` mode-2 first-launch deadlock, verify.cpp:381-390). Implement the doorbell redesign first; split the variants inside `STRATA_PLE_ASYNC_GATHER` only if the ar variant is wanted at all.
3. Bit-exactness: same `gather_batch` bytes into the same pinned `h_ple_`, and the consumer order is untouched — the GPU reads `ple_`/`m_ymiss_` only after the :3616 raise, which now happens strictly after the join, so the gather's writes precede every GPU read. The flag protocol is unchanged from today: bit-exact by construction, strictly stronger than the original design (which changed raise timing).
4. **Win restated (review)**: the gather hides inside the k==0 host span (classify + publish + pool). The saving is bounded by the `ms_pool` slice (typically the bulk of the window's host time — the F7c fields show it), NOT by the full gather time; the +0.5-3% band keeps that bound as its ceiling and is a placeholder until re-based (Re-baselining).

**Commit slicing**
- `feat(core): STRATA_PLE_ASYNC_GATHER — overlap the k==0 PLE gather with the doorbell host service (join before *flag = want)` — one commit (cohesive; OFF skips the thread and the loop change).
- (optional, only if wanted) `feat(core): ar_on() pre-raise from the gather thread` — the non-prod variant, separately switchable inside the same env.

**Validation ladder**
- (a) build_pin.sh + host build (`ple_residency_test.cpp`, `tests/hip/ple_iq4.cpp` compile/run in the build window).
- (b) In-tree: the PLE same-bytes contracts already exist (ple_fp8_parity, ple_reader_test — roadmap A1's citation); ADD host-only tests: (i) failure path — fake `gather_batch` returning false → err recorded, `run()` returns the error, no hang; (ii) **join-ordering** — the thread is finished before the k==0 `*flag = want` write (the one-raiser rule as an asserted behavior).
- (c) Engine gate: one qq boot with `STRATA_PLE_ASYNC_GATHER=1` → KL 0.00000 ×3 (PLE rows feed layer 1 — any byte difference shows here loudly). Scope note per the review: the KL gate proves byte-exactness OF THE REDESIGN; it could NOT have certified the original design (a schedule-dependent race can pass a KL run), which is why the fix is structural. Judges leg (warm TG in the restated band; the k==0 `ms_host` slice shrinking in the F7c fields, `ms_wait` for layer 1 possibly absorbing part) is HELD until band restoration (Re-baselining) — the KL boots run now.
- (d) Rollback: lane_restore.sh; env off; pin untouched.

**Risks + mitigations**
- **#267-class / thread lifetime (the B1/B2 lessons, 18ba)**: the thread is joined at k==0 BEFORE the flag raise on the happy path, and by RAII at `run()` scope exit on every error path (join before the error return); no thread writes mapped memory, and the GPU reads `h_ple_` only after the raise that follows the join. No cross-window state; a stream rebuild invalidates nothing (the thread touches only `h_ple_` and the PLE table reader — Verifier/caller-lifetime objects, used within the window only).
- gather I/O error: err propagated at :3661-3663 — no hang, clean 5xx-class error like today's inline failure.
- Exactly one raiser per value: unchanged from today (the service loop alone); the thread never raises on the doorbell path — the original design's two-raiser/mid-write hazards are gone by construction, not by testing.
- First-launch HIP graph lock: relevant only to the optional ar_on() thread-raise variant (raise from a non-launching thread); the doorbell path never raises from the thread at all.
- Win band: bounded by the ms_pool slice; placeholder until re-based post-AUX_CPUS (Re-baselining).

**Definition of done**: failure-path + join-ordering tests green; qq KL 0.00000 ×3; judges ×2 AFTER band restoration with warm TG gain inside the restated ms_pool-bounded band and the k==0 `ms_host` reduction visible in the F7c fields; journal round.

## F7ab — boot I/O determinism: O_DIRECT arena reads + offset-sorted fill order (PP +7-20% on slow boots; HYPOTHESIS) (amended per review, 2026-11-15)

**Hypothesis discipline** (the analysis marks it HYPOTHESIS; the operator already falsified PLE-mmap-as-fix in 18bc — this is the narrower page-cache-warmth mechanism with the 376-671 MB/s same-work evidence from 18bd). The measurement program decides adoption; the code lands default-off regardless. **Re-baselining caveat (review)**: that evidence predates the AUX_CPUS fix — the fill threads were among the parked-on-CPU-0 set, so the slow class is partially confounded by the pinning fault; the campaign runs on the adopted config and lets the classes re-establish themselves.

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
- (c) Engine gate — this is a DISTRIBUTION measurement, not a single boot: ≥6 interleaved boots (3× gate-off control, 3× gate-on; grow to 8+ if the classes straddle) via `lane_boot.sh` + one `lane_judges.sh` fill80k per boot (ONE fresh fill per boot — the 18bb lesson: second fills are cache-resident and useless). Read the F7c pack-read MB/s and the fill wall per boot: hypothesis CONFIRMED if the on-boots' pack-read rate clusters tight at/above the fast class with the bimodal split gone — the ≥600 MB/s reference and the 432-455/352-379 PP classes are pre-fix numbers: RE-ESTABLISH the class bands on the adopted AUX_CPUS config and use the fresh bands as the thresholds (review note). Zero expected TG change (decode untouched) — one tg80k sanity leg on the last boot, only once the band is restored (in the collapsed state it certifies nothing; Re-baselining).
- (d) Rollback: env off; gates stay OFF in tree; promotion is an operator decision after the distribution evidence.

**Risks + mitigations**
- Hypothesis falsified (slow class persists with O_DIRECT): the gates stay off, F7c keeps paying (every boot self-attributes), and the mechanism search moves to B6 (queue priming) with F7c as the discriminator (see the F7c section).
- O_DIRECT read amplification on unaligned blob edges: reuse `read_direct`'s windowing (head/tail padding inside the pinned half) — the swap-in path's own rule; no new alignment math.
- Pack-file fragmentation making offset order ≠ sequential: measurable directly (F7c pack-read rate with SORT on, O_DIRECT off).
- Boot-time p85 regression on FAST-class boots (O_DIRECT drops page-cache warming that later swap-ins would reuse): the matrix measures it (fill wall + first swap-in rates with F7c fields); if it appears, O_DIRECT stays off and SORT alone may still win.

**Definition of done**: ≥6-boot distribution table in the journal round; either (i) confirmed — slow class eliminated on ≥3/3 on-boots, operator promotes the gates, or (ii) falsified — gates remain default-off with the F7c attribution data recorded and B6 re-queued as the live suspect. No middle state.

---
# Phase 3 — speculative prefetch

## F4 — next-layer miss prefetch: helper weight pre-stage + PCIe blob pre-stage (WT +5-15% ceiling) (amended per review, 2026-11-15)

**⚠ Plan-level refinement — flagged per the discipline.** The analysis's step (a) as written — "the helper computes candidate rows into a scratch ring" at layer l's service for layer l+1 — is **not constructible bit-exactly**: the helper's grouped kernel needs layer l+1's ACTIVATIONS (`proxy_begin(layer, x, ...)` consumes the current layer's post-attention rows, remote_experts.cpp:619-621), and `x_{l+1}` does not exist until layer l's combine completes — after the service the prefetch would precede. Rows computed from any predicted activation would fail the analysis's own consume rule ("only rows whose (layer, token, expert, weights) match the ACTUAL routing"), which checks routing and weights but cannot validate a wrong input vector. The implementable form that keeps the analysis's quality argument and its value target (the residual `finish` wait + PCIe stream time — exactly what the analysis's own F4-refinement section prices) is:
- **(a) helper weight pre-stage**: the prefetch frame carries layer + predicted ids (+ primary-residency bitmap); the helper pre-reads those experts' blobs from its pack into a scratch set keyed by (layer, expert) and pre-builds the grouped-kernel metadata; at the REAL begin (with the F2 ring image) the compute starts with zero pack-read prelude. Rows are still computed only at the real begin from the real image — bit-exact by construction (identical weights bytes — the pack is immutable; identical image; identical kernel).
- **(b) PCIe blob pre-stage**: predicted non-resident blobs pre-copied into dedicated staging-ring slots on the copy engine (idle while the host serves); at plan time entries whose (layer, expert) matches a staged slot take `P.ptr2` from the staging ring (expert_source.cpp:5232-5233) instead of a demand fetch — validated by the key, bytes identical (immutable pack offsets).
If, at implementation time, a reviewer finds a constructible true-row-prefetch (e.g. an activation-independent pre-transform), it can be added under the same consume-only rule — the plan does not depend on it.

**Exact change list**

1. **Protocol** — `include/strata/core/expert_helper_protocol.hpp` (the extension rule of :110-115/132-139, verbatim pattern):
   - `inline constexpr uint32_t kFeaturePrefetch = 256u;` (bit 8; HelloAck.features).
   - `kPrefetch = 22` (engine → helper: layer, predicted ids[≤16], primary-residency bitmap — the SAME body sections as kBegin minus the activation image) and `kPrefetchAck = 23` (helper → engine: n_queued, tiny — the handler ACKS FAST and stages asynchronously; see the serve-thread constraint below). An old helper never sees the frame (client-side negotiation gate copying `begin_q8k`'s at expert_helper_socket.cpp:631-633) — **an old helper refuses cleanly by never being asked**; a new engine + old helper sends nothing and disables the feature.
2. **Helper side** — `tools/mixed_gpu/sidecar/helper_main.cpp` (+ its helper-tier source files):
   - Advertise `kFeaturePrefetch` in kHelloAck when the scratch set initialized (capacity-bounded: ≤64 experts × blob bytes ≈ ≤~150 MiB of its reserve — size from the helper's existing arena-cap accounting, `expert_helper_arena_cap` tests as the guard; on capacity pressure the handler answers kPrefetchAck with n_queued=0 — never an error).
   - kPrefetch handler — **serve-thread constraint (review, blocking for F4's design)**: the helper is a serve-thread-per-socket SERIAL frame loop (helper_main.cpp:24-31, :315-355; a second channel exists only with opt-in `--socket2`). A handler that synchronously reads up to m×~2.5 MB blobs BLOCKS that socket's next kBegin — the prefetch would delay the exact demand path it is meant to hide. Therefore the handler acks immediately (n_queued) and hands the work to a helper-side staging thread, which reads the blobs into the scratch set and builds the grouped metadata (its own stream, no lock held against a rows reply); OR the prefetch lane rides the second socket (`helper2_`/stripe channel-B precedent, remote_experts.cpp:757-796) and never shares the demand socket's serve thread. Pick one at implementation; the socket test covers both orders. Key = (layer, expert, pack offset) — offset in the key makes byte-equality a property of the immutable file, not a check.
   - kBegin handler (existing): before its pack read, take any matching scratch entry (key compare; consume = move out). Miss → today's read. Zero changes to the rows computation.
3. **Engine client** — `src/core/expert_helper_socket.cpp`:
   - `int ExpertHelperClient::prefetch(int32_t layer, const int32_t* ids, int n_ids, const int32_t* primary_res, int64_t deadline_ms)` — builds the frame (the `build_window_payload` shape minus image), sent under `io_mtx_`.
   - **One-outstanding rule** (expert_helper_socket.cpp:607-611 — the constraint the analysis's refinement flags): a prefetch must never block or interleave with an outstanding begin/finish pair. `pf_outstanding_` flag; `prefetch()` refuses (returns kErrBusy, silently ignored) if a begin is outstanding. **Ack-drain point (review, blocking for F4's design)**: draining only inside the next `begin()` misses the interleave where the helper is still staging when kBegin(l+1) is sent — the ack then arrives BEFORE kRows(l+1) on the socket, the reply reader hits an unexpected frame type, the protocol fails, and the tier disables. The drain must live in the **reply reader (`finish()`, expert_helper_socket.cpp:710)**: `finish()` tolerates and consumes any number of LEADING `kPrefetchAck` frames under `io_mtx_` before the expected kRows (a non-blocking peek in `begin()` may remain as a fast path, but `finish()` is the correctness point); if an ack has not landed when the next prefetch round is considered, mark the lane busy and skip — never block the claim path. Helper-side order is FIFO per serve thread; where the prefetch cannot be handled in time, the skip rule caps the loss at zero.
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
- `feat(core): ExpertHelperClient::prefetch — lane-busy discipline, acks drained in the reply reader (finish)` — client send path, no callers.
- `feat(core): STRATA_HELPER_PREFETCH — per-layer predictor + doorbell-service send hook + hit counters` — the engine behavior, default OFF.
- `feat(core): predicted PCIe blob pre-stage into reserved staging slots (plan-time validated)` — part (b), same gate (or its own `STRATA_PCIE_PREFETCH` if the operator wants independent A/B — recommended: same predictor, separate gate).

**Validation ladder**
- (a) build_pin.sh (engine) AND the helper binary build (same worktree, sidecar target — build_pin's guards cover the engine; the helper has its own build line in the lane scripts; assert both artifacts' sha recorded).
- (b) In-tree: extend `tests/core/expert_helper_socket_test.cpp`: (i) old-peer simulation — helper advertises no bit → client never sends (socket-level assertion); (ii) prefetch-then-begin round trip on a fake helper: begin's frame unchanged, ack drain never blocks (timing assertion); (ii-b) **ack-before-kRows interleave (review)**: a fake helper that stages slowly so the ack lands between kBegin(l+1) and kRows(l+1) → `finish()` consumes the leading ack, the rows reply parses, the tier stays enabled; (iii) capacity-pressure: ack n_queued=0 → engine continues, no retry storm. Helper-side scratch test in the helper's own test file (or `expert_helper_arena_cap_test.cpp` extension): staged weights hash == pack read hash. PCIe part: a dispatch unit test — predicted staging consumed only on exact key match; wrong prediction → demand path → plan bytes identical (golden plan compare).
- (c) Engine gate: qq boot with `STRATA_HELPER_PREFETCH=1` (+PCIe gate) → **KL 0.00000 ×3** (the all-miss case is exercised naturally in the first windows — predictions warm up; the harness still must show zero divergence); judges ×3 boots: warm TG vs the post-P1/P2 baseline with the `pf_hit` rate on the summary — the acceptance curve: hit ≥50% ⇒ expect +3-8%; hit ≥70% ⇒ the analysis's +5-15% band. If hit <30% after m-tuning (m ∈ {4,8,16} via `STRATA_PREFETCH_TOP`), record and stop (see DoD).
- (d) Rollback: env off; gates default-off in tree; helper unchanged for old engines (feature bit only advertised; no behavior without the frame).

**Risks + mitigations**
- **Predictor thrash (bounded)**: worst case (all-miss, m=8): speculative pack reads ≈ 8 ids × ~2.5 MB × 48 layers × ~8 windows/s ≈ 7.7 GB/s of extra PCIe/NVMe traffic against a measured ~60-90 GB/s aggregate with ~95% headroom — a bandwidth tax, not a correctness or latency cliff (the analysis's "costs bandwidth not correctness"). Mitigations: m starts at 4; the hit-rate dashboard (F7c fields) with the operator kill-switch (gate off); predictions never displace demand work (dedicated slots, copy-engine-only, helper-scratch-only).
- **Protocol states**: the extension rule handles mixed builds both directions (old helper: never asked; old engine: never sends). The lane-busy rule caps prefetch to zero cost on a busy helper, and the serve-thread rule (handler acks fast, stages async or rides socket 2) makes that structural: the prefetch never blocks the demand socket's frame loop.
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

**Day 1 — box state + tooling warm-up + F7c + F6 start**
0. Re-baselining checkpoint (mandatory, per the Re-baselining section): record the post-reboot re-measurement outcome (warm-TG band status) in the journal; every judge leg below stays conditional on it. Rebuild baseline pin from HEAD to verify the toolchain: `tools/mixed_gpu/build_pin.sh wk1-base`; record sha.
1. Land F7c (one commit, log-only) — the FIRST code item, mandatory per Re-baselining. Build (`build_pin.sh f7c`), boot once via `lane_boot.sh`, run one `lane_judges.sh` fill80k leg (tg80k leg only if the band is already restored); confirm the new fields and that judge parsing still works. (~1 lane-boot + 1 judge leg ≈ 30-40 min GPU.)
2. F6 commit 1 (`fill_slots_batched` + unit test); host build + ctest -R expert_cache.
3. Start F6 commit 2 (loops behind `STRATA_BOOT_FILL_QUEUED`).

**Day 2 — F6 finish + first gated boot**
1. Land F6 commits 2-3; `build_pin.sh f6`; boot with the env via variant launcher; read the F7c per-phase fields (H2D-phase ≥2×; end-to-end caveated read-bound); byte gate = one `STRATA_VERIFY_COMPLEMENT=1` boot, gate-on vs gate-off, all-slots checksums equal (+`verify_slot` pass each boot); fill80k leg to confirm no read-path change (tg80k held per Re-baselining); `lane_restore.sh`. (~1.25 h GPU.)
2. F8 commits; build together with F6 gate env; qq gate boot: `quant_quality.py run --config <qqNIGHT variant> --env STRATA_BOOT_FILL_QUEUED=1,STRATA_PENALTY_MAPPED=1` → compare → **KL 0.00000** (×1 first pass; 2 more passes later in the week). (~40 min.)
3. Start F1 commit 1 (sampler kernels).

**Day 3 — F1 + F2 kernels**
1. F1 commits 1-3 + parity test (incl. the penalized-greedy arm); build-window ctest (sampler parity). qq boot with all three P0 envs → KL 0.00000 (pass 2). fill80k leg on the P0 stack, tg80k held (the main deliverable is clean gates). (~1.5 h GPU.)
2. F2 commit 1 (`doorbell_xq_publish` kernel) + commit 2 (ring + tail publish + wait). Host tests for the ring start (`expert_helper_xq_ring_test.cpp`).

**Day 4 — F2 finish + gate**
1. F2 commits 3-4 (proxy_begin consumption + staging cleanup); `xq_triple_parity` test; `build_pin.sh f2`.
2. qq gate ×2 boots: `STRATA_HELPER_XQ_RING=1` → KL 0.00000 (passes 1-2 of 3). (~1 h.)
3. Judges ×2 on F2 HELD until band restoration (Re-baselining) — run qq pass 3 instead; helper reqs/frame (unchanged 37/window) is readable from the KL boot's helper log. `lane_restore.sh`. (~0.5 h.)

**Day 5 — F3 + first F2+F3 combined gates + F5 start**
1. F3 commit; triple-parity extension; combined qq gate `STRATA_HELPER_XQ_RING=1 STRATA_CPU_POOL_XQ=1` → KL 0.00000 ×1 (passes continue into week 2 as the 3× rule requires).
2. Judges ×2 on F2+F3 HELD (band restoration, Re-baselining); additional combined-config qq passes instead. (~0.5 h.)
3. F5 commit per the amended (redesigned) section + failure-path/join-ordering tests (code only; gate boot in week 2).
4. Week-1 journal round: record all gate outputs, judge deltas, the F7c attribution lines from every boot (each P0/P1 boot already produced attribution data for free), and the P3 go/no-go inputs (helper begin/finish residual from the F7c fields).

Week 2 preview: F5 KL gate boots (judges legs when the band is restored); F7ab's 6-8-boot distribution campaign ON THE ADOPTED AUX_CPUS CONFIG with fresh class bands (can interleave with F5 coding); the one-time re-baselining pass re-states every WT%/BT% target once the warm-TG band is confirmed restored; P3 decision point on F4 with the residual-wait data.

---

# Definition-of-done summary table

| Item | Closing evidence |
|---|---|
| F6 | 3 boots: H2D-phase rate ≥2× in the F7c fields (end-to-end caveated read-bound), `STRATA_VERIFY_COMPLEMENT` all-slots checksums equal gate-on vs gate-off, verify_slot green each, fill80k unchanged (tg80k held), 1 clean qq at promotion |
| F8 | qq KL 0.00000 (gate on), no per-window blocking H2D between draft and run in the round trace (staged after the draft — no previous-window claim) |
| F1 | sampler parity test green (incl. penalized-greedy arm); qq KL 0.00000; second sync gone on sampled + penalized turns |
| F7c | fields on ≥2 boots, one known-slow phase correctly attributed, scripts still parse |
| F2 | frame-bytes equality green (incl. multi-window stale-seq); parity green incl. the QFUSE-producer leg (QFUSE==native==rows==cpu); qq KL 0.00000 ×3; judges ×2 warm TG ≥ +2% after band restoration; helper reqs/frame unchanged |
| F3 | parity green (nact_multi stride asserted); combined-config qq KL 0.00000 ×3; judges ×2 additional ms_pool reduction after band restoration |
| F5 | failure-path + join-ordering tests green; qq KL 0.00000 ×3; judges ×2 (post-restoration) warm TG inside the ms_pool-bounded band; k==0 ms_host reduction |
| F7ab | ≥6-boot distribution on the adopted AUX_CPUS config with fresh class bands: either slow class eliminated (operator promotes) or falsified with attribution data recorded |
| F4 | protocol/socket tests green (incl. ack-before-kRows drain in finish); qq KL 0.00000 ×3; ≥3 judge boots (post-restoration) with hit-rate + TG delta; adopt / descope / stop decision journaled |

*End of plan.*
````
<!-- /source-snapshot id=CODE -->

---

<a id="snapshot-road"></a>
### 11.4 ROAD — perf-roadmap-2026-11-15.md (original L1–49)

<!-- source-snapshot id=ROAD path=bench/ab-v0210/notes/perf-roadmap-2026-11-15.md sha256=3bf80d0ac6e6c51e4fc0ec0b333b3c3915b0db750d4c2b6186256df44bcd6f6b -->
````markdown
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
````
<!-- /source-snapshot id=ROAD -->


---

## 12. Output Path
**Single master file:** `/mnt/srcds/Strata/bench/ab-v0210/notes/inference-optimization-master-plan.md`

---

*End of master plan. Independent final-hash review pending. No implementation, benchmark or promotion authority.*
