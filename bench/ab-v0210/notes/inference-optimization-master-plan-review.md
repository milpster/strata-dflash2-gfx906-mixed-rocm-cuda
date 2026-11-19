# Inference Optimization Master Plan — Independent Review Ledger

**Review Round:** 1
**Date:** 2026-11-19
**Reviewer:** Independent Oracle (Nemotron 3 Ultra)
**Reviewed Artifact:** `/mnt/srcds/Strata/bench/ab-v0210/notes/inference-optimization-master-plan.md`
**Reviewed SHA256:** `1704e06940060481e96479f7033516bbc313dc595f651e3e5e9b6029ef918a33`

**Four Source Inputs (Verified Unchanged):**
| # | Source | Path | SHA256 |
|---|--------|------|--------|
| 1 | speed-plan-v2.md | `bench/ab-v0210/rpcprof/gpu-rounds/speed-plan-v2.md` | `db7bc537c4e39c36ed35f086ff8434dde325a07cc53037c5ca60301949961d1c` |
| 2 | tg-optimization-chapter.md | `bench/ab-v0210/rpcprof/gpu-rounds/tg-optimization-chapter.md` | `02abecd2e0c10360a640881d2d641e060c5ea1ef73c80c348d7a2a36bb39552c` |
| 3 | code-opt-plan.md | `bench/ab-v0210/notes/code-opt-plan.md` | `fb8d30d31c6ba91ee8c9d3185dfbbad20cf02d4242475c257d551499e8f2f7ac` |
| 4 | perf-roadmap-2026-11-15.md | `bench/ab-v0210/notes/perf-roadmap-2026-11-15.md` | `3bf80d0ac6e6c51e4fc0ec0b333b3c3915b0db750d4c2b6186256df44bcd6f6b` |

**Supporting Read-Only Context Verified:**
- code-opt-plan-review.md, tg-optimization-review.md, journal/serving-tg-reeval-split-retest-2026-11-11.md

---

## VERDICT: **CHANGES REQUIRED**

The master plan fails on **snapshot integrity** (2 of 4 source snapshots are not byte-for-byte identical), **internal contradiction** on F2→F3 scheduling, **missing mechanism detail** for ROAD-C2/C1, and **unsubstantiated coverage counts**. These are material blockers for a reconciliation document claiming exact preservation.

---

## BLOCKERS

### R1 — CRITICAL: Snapshot Integrity Failure (CODE and ROAD snapshots not byte-identical)
- **Priority:** CRITICAL
- **Master Path:** Section 11 (Archival Appendix), snapshots id=CODE and id=ROAD
- **Discrepancy:** Master claims "exact byte-for-byte copies" and "SHA256 verified above" (Section 10.1), but independent extraction shows hash mismatches.
- **Source Evidence:**
  - CODE source SHA256: `fb8d30d31c6ba91ee8c9d3185dfbbad20cf02d4242475c257d551499e8f2f7ac`
  - CODE extracted snapshot SHA256: `73c6acf9ee6289fb5f4636996bc6cc9f08e3e48e1afb4c4ef2bb00a6bdb298d8`
  - ROAD source SHA256: `3bf80d0ac6e6c51e4fc0ec0b333b3c3915b0db750d4c2b6186256df44bcd6f6b`
  - ROAD extracted snapshot SHA256: `f65fb8899b369ee2d2e46e3874433f3be646b0c39c62f3196ce772fef6dc3254`
- **Exact Differences Found:**
  1. CODE snapshot, phasing diagram line 48: Source has `F5 ;  F7ab` (two spaces before F7ab); snapshot has `F5 ; F7ab` (one space).
  2. CODE snapshot, F7ab validation ladder (c): Source says `hypothesis CONFIRMED`; snapshot says `hypothesis CONFIRMATED` (typo).
  3. ROAD snapshot, all tables: Source uses spaced pipes (`|---|------|...|`); snapshot uses compact pipes (`|---|---|...|`).
- **Required Correction:** Re-embed both snapshots as exact byte-for-byte copies. Verify with `sha256sum` after extraction. Do not claim verification passed if hashes don't match.
- **Pass/Fail Check:** Next round must show `sha256sum` match for all four extracted snapshots vs declared source SHAs.

---

### R2 — CRITICAL: Internal Contradiction on F2→F3 Schedule
- **Priority:** CRITICAL
- **Master Path:** §3.2 (line 179), §6.2 (line 464), §6.3 (line 480)
- **Discrepancy:** Three conflicting statements:
  1. §3.2 Chronology note: "No automatic F2→F3 implementation/test schedule is authorized."
  2. §6.2 Proposed Order diagram: Explicitly lists `- F2→F3 (ring transport; hard dependency F3→F2; F2 gates helper presence)` as a CAND-N dependency step.
  3. §6.3 Key Differences: "**No automatic F2→F3 schedule:** F3 not shortlisted without changed premise (TG-chapter explicit)."
- **Source Evidence:** 
  - TG-chapter (line 14): "F3 CPU-pool act from ring | Not landed | No `STRATA_CPU_POOL_XQ` in pinned tree; ring carrier has no demonstrated single-stream benefit."
  - TG-chapter (line 106): "Not shortlisted: F2/F3 without a changed premise..."
  - Code-opt-plan phasing: `P1 (ring): F2 ──> F3` (hard dependency F3 on F2, not a schedule commitment).
- **Required Correction:** 
  - Remove the `F2→F3` line from the CAND-N diagram in §6.2, or re-label it as "F2 only; F3 gated on separately demonstrated changed premise (not in live queue)."
  - Fix the backwards notation "hard dependency F3→F2" → should be "F3 depends on F2" or "F2→F3 dependency".
  - Ensure §3.2, §6.2, §6.3 are consistent: F3 is explicitly NOT shortlisted; no schedule implied.
- **Pass/Fail Check:** Next round shows no F2→F3 schedule line in CAND-N; all three sections agree F3 is blocked without changed premise.

---

### R3 — HIGH: Missing Mechanism Detail for ROAD-C2 and ROAD-C1
- **Priority:** HIGH
- **Master Path:** §2.2 (lines 136-138), §3.10-3.12 (no corresponding mechanism sections)
- **Discrepancy:** Inventory table lists `ROAD-C2` (Bit-exact GPU expert contract) and `ROAD-C1` (HC_Q8 T-fusion / LDS retile) but there are no detailed mechanism sections (§3.x) preserving their unique designs, validation requirements, and code anchors from the source. Task explicitly requires "Missing operative TG-S3 attention card / ROAD-C2 expert determinism / ROAD-C1 LDS retile details. All original unique details must remain in exact snapshots and source crosswalk."
- **Source Evidence:** 
  - Perf-roadmap lines 39-41: C2 has 5-row validation (cold-boot ×5 + warm ×5, sha identity, KL 0.00000 vs pinned-CPU oracle), specific files (s2_expert_grouped.cu, expert_source.cpp, native_expert.cpp, helper env plumbing), effort L.
  - Perf-roadmap lines 39-40: C1 has KL≈0.019 gate class, fused_gr.cu extension to T≈32/16, retile needed, only if HC_Q8-on becomes baseline.
  - TG-S3 attention lane-cell: Perf-roadmap line 19 says "likely inert (wave32-only, verify_kernels.cu:832)"; TG-chapter line 96 identifies actual in-tree gfx906 variant at `qsa_decode_attn.cu:919,966-1014`. Master §3.9 includes TG-S3 in table but as a single row, not a full mechanism card.
- **Required Correction:** Add mechanism sections for ROAD-C2 and ROAD-C1 (and expand TG-S3) with full detail: mechanism, validation ladder, code anchors, quality gates, effort, risks — matching the pattern of §3.1-3.12. Preserve all unique source details (not just inventory summary).
- **Pass/Fail Check:** Next round has §3.x sections for ROAD-C2, ROAD-C1, and expanded TG-S3 with source-level detail.

---

### R4 — HIGH: Coverage Counts Unsubstantiated / Fabricated
- **Priority:** HIGH
- **Master Path:** §9.6 (lines 681-689)
- **Discrepancy:** Claims "172 discrete items mapped, 0 omitted" with breakdown 16+56+82+18. But:
  - TG-chapter has 18 prior-work rows + 11 upstream leads + 4 shortlist + 5 anchors + 5 phased steps + 5 gate bullets + 2 unresolved = 50 items (not 56).
  - Code-opt-plan: 14 anchors + 8 master table + 1 phasing + 1 re-baselining + 8×7 subsections (56) + cross-cutting + first-week + DoD = 82 claimed, but "7 subsections each" is false — F7c has 6, F5 redesigned has different structure, F4 has plan-level refinement. Actual subsections vary.
  - Speed-plan-v2: 6 fact bullets + Phase 0 (3 steps) + Program A (2 items) + Program B (2 items) + Gates (3 bullets) + Success bar + Wall-time = ~18, not 16.
  - Perf-roadmap: 5+6+3 = 14 items + top-3 + refuted = 18 claimed, but A5 is a multi-item refuted list.
  - No enumeration of the "172 mapped" items exists — the mapping tables (§9.1-9.4) list sections not discrete items.
- **Source Evidence:** Direct count of source document structural elements.
- **Required Correction:** Either (a) remove the specific "172" total and "0 omitted" claim, replacing with "comprehensive coverage achieved; all source sections individually cross-referenced in §9.1-9.4" OR (b) provide an actual enumerated 1:1 mapping table with 172 rows showing each discrete source item → master location. Do not invent precise totals.
- **Pass/Fail Check:** Next round either removes fabricated counts or provides verifiable enumeration.

---

### R5 — HIGH: Historical P0-P3/First-Week Schedule Not Labeled as History
- **Priority:** HIGH
- **Master Path:** §6.2 (proposed order diagram), CODE snapshot (lines 1426-1462 "First week — concrete integrator sequence")
- **Discrepancy:** The CODE snapshot includes a detailed "First week — concrete integrator sequence" (Day 1-5 with F2→F3 on Day 4-5). The master's §6.2 proposes a new order but does not explicitly label the code-opt-plan's first-week schedule as **HISTORICAL/NOV15** in the operative text. §6.3 says "No automatic F2→F3 schedule" but the embedded snapshot contains exactly that schedule.
- **Source Evidence:** Code-opt-plan lines 1426-1462: "Day 4 — F2 finish + gate... Day 5 — F3 + first F2+F3 combined gates". This was written Nov-15, before the Nov-18 null result and F3 not-shortlisted status.
- **Required Correction:** In §6.2/§6.3, explicitly state: "The code-opt-plan's first-week sequence (Day 1-5) is HISTORICAL/NOV15 — it assumed F3 would follow F2; current TG-chapter evidence shows F3 not shortlisted without changed premise. The proposed order above supersedes it." Do not rely on snapshot alone to convey this.
- **Pass/Fail Check:** Next round has explicit HISTORICAL label on the first-week schedule in operative sections.

---

### R6 — MEDIUM: CPU-Only vs Device Parity Classification Incomplete
- **Priority:** MEDIUM
- **Master Path:** §8.1 (Test Classification), validation ladders throughout
- **Discrepancy:** §8.1 table distinguishes "Unit/Parity (Host)" vs "Unit/Parity (Device)" but validation ladders in §3.x and CODE plan don't consistently classify which tests require GPU. Task notes: "ExpertCache test needs GPU context; sampler CUDA parity not CPU-only. PP token/s vs boot fill slots/s distinct; VmHWM measures RSS not PSS."
- **Source Evidence:** 
  - Code-opt-plan F6 validation (b): "new `tests/core/expert_cache_batched_test.cpp`... Runs in the build-window ctest (needs a CUDA context; same class as the existing expert_cache tests)." → Requires GPU.
  - Code-opt-plan F1 validation (b): "NEW `tests/cuda/sampler_mapped_parity.cu`... build-window ctest" → Requires GPU (CUDA kernel).
  - §8.2 Memory metrics: "Engine/helper RSS (VmHWM via clear_refs reset)" — correct, VmHWM is RSS high-water mark, not PSS.
- **Required Correction:** Annotate each validation ladder step with GPU/CPU requirement. Fix any "CPU-only" misclassifications. Ensure VmHWM/RSS terminology is precise (not PSS).
- **Pass/Fail Check:** Next round shows GPU/CPU tags on all validation steps; no CPU-only claim for CUDA tests.

---

### R7 — MEDIUM: TG-S3 Attention Card Under-Specified
- **Priority:** MEDIUM
- **Master Path:** §3.9 (table row only), no dedicated mechanism section
- **Discrepancy:** TG-S3 is a ranked shortlist item (S3) in TG-chapter with full hypothesis/falsify/rollback structure. Master reduces it to one table row in §3.9. Task requires "Missing operative TG-S3 attention card... All original unique details must remain in exact snapshots and source crosswalk."
- **Source Evidence:** TG-chapter lines 94-99: S3 has Evidence, Scope, Budget, Proof, Falsify, Rollback — full card structure.
- **Required Correction:** Add a dedicated mechanism section for TG-S3 (or expand §3.9 to full card format) preserving the S3 structure: evidence, scope, budget, proof, falsify, rollback, with code anchors (`qsa_decode_attn.cu:919,966-1014`, `STRATA_ATTN_LANECELL`, int8 KV path).
- **Pass/Fail Check:** Next round has full S3 card in master (not just table row).

---

### R8 — MEDIUM: Mlock/PLE Table Coverage Claim Overstated
- **Priority:** MEDIUM
- **Master Path:** §1.4 (line 53), §3.8, ANNEX queue
- **Discrepancy:** §1.4 line 53 correctly states operator clarification: "do **not** mlock the PLE table; do not use blanket `mlockall()`. The intended test locks expert weights held in system RAM; GPU-resident weights remain in VRAM. The arena is already locked... The proposed native-GGUF mapping change is not yet proof that every expert-backed region is covered. Audit coverage and feasibility before calling the test complete; do not claim that every model expert is resident in RAM."
  But §3.8 and ANNEX don't reflect this constraint explicitly. The ANNEX queue item "mlock investigation" should reference the operator's specific scope (expert weights in system RAM only, not PLE table, not blanket mlockall).
- **Source Evidence:** Operator directive in task description; journal 18w/18x commit 660d9f0dc7 (46.84 GiB, 4608 spans, 128s).
- **Required Correction:** Update ANNEX queue description to match operator's exact scope: "mlock investigation — expert weights in system RAM only (GPU experts in VRAM); arena already locked; native-GGUF mapping coverage audit required; no PLE table mlock, no blanket mlockall()."
- **Pass/Fail Check:** Next round ANNEX item matches operator's exact constraints.

---

### R9 — LOW: PP-B2 DEQUANT_SIDE Parity Gate Ambiguity
- **Priority:** LOW
- **Master Path:** §3.12 (PP-B2), §2.2 (PP-B2 status 🟡 Unvalidated)
- **Discrepancy:** §3.12 says "Run parity test (GPU, lanes unparked) → if bit-identical claim holds: env-only A/B, no KL needed." But speed-plan-v2 source says "Run parity test (GPU, lanes unparked) → if bit-identical claim holds: env-only A/B, no KL needed. This is the first real GPU-overhead round." The master plan doesn't clarify that the parity test `gemm_dequant_side_parity.cu` must run on GPU with lanes unparked (not host-only). The status 🟡 Unvalidated is correct but the validation ladder should be explicit.
- **Required Correction:** In §3.12 PP-B2, specify: "Parity test: `gemm_dequant_side_parity.cu` on GPU (lanes unparked). If bit-identical: env-only A/B, no KL. If not bit-identical: candidate blocked."
- **Pass/Fail Check:** Next round has explicit GPU parity test requirement.

---

## NON-BLOCKING SUGGESTIONS

### S1 — Terminology Consistency
- Use "VmHWM (RSS high-water)" not just "RSS" in §8.2 memory metrics table for precision.
- "PP slots/s" vs "TG tok/s" — maintain distinction everywhere (mostly correct).

### S2 — Cross-Reference Anchors
- Add explicit section anchors (e.g., `#mech-sampler`) for all mechanism sections to enable stable cross-references.

### S3 — F4 Value-Collapse Gate Clarity
- §3.4 says "re-measure residual wait via F7c helper begin/finish fields post-P1/P2". Add explicit threshold: "If residual wait <0.3 ms/layer, descope to PCIe half only." (Already in CODE plan, ensure master reflects it).

### S4 — Profiler Observer Effects List
- §8.4 table is good but could add the TG-review expanded sites (verify.cpp:2590,2623,2643,1770) explicitly in the mitigation column.

---

## VALIDATION EVIDENCE (Commands Actually Run)

```bash
# 1. Verify all four source hashes match declared values
sha256sum /mnt/srcds/Strata/bench/ab-v0210/rpcprof/gpu-rounds/speed-plan-v2.md \
          /mnt/srcds/Strata/bench/ab-v0210/rpcprof/gpu-rounds/tg-optimization-chapter.md \
          /mnt/srcds/Strata/bench/ab-v0210/notes/code-opt-plan.md \
          /mnt/srcds/Strata/bench/ab-v0210/notes/perf-roadmap-2026-11-15.md
# Exit: 0 (all match declared SHAs)

# 2. Verify source files unmodified
cd /mnt/srcds/Strata && git status --porcelain bench/ab-v0210/rpcprof/gpu-rounds/speed-plan-v2.md \
          bench/ab-v0210/rpcprof/gpu-rounds/tg-optimization-chapter.md \
          bench/ab-v0210/notes/code-opt-plan.md \
          bench/ab-v0210/notes/perf-roadmap-2026-11-15.md
# Exit: 0 (clean, no modifications)

# 3. Extract and verify PP snapshot (id=PP)
sed -n '/<!-- source-snapshot id=PP path=/,/<!-- \/source-snapshot id=PP -->/p' \
  /mnt/srcds/Strata/bench/ab-v0210/notes/inference-optimization-master-plan.md | \
  sed '1d;$d' | sed '1d;$d' > /tmp/pp_snapshot.md
sha256sum /tmp/pp_snapshot.md /mnt/srcds/Strata/bench/ab-v0210/rpcprof/gpu-rounds/speed-plan-v2.md
# Result: MATCH (db7bc537c4e39c36ed35f086ff8434dde325a07cc53037c5ca60301949961d1c)

# 4. Extract and verify TG snapshot (id=TG)
sed -n '/<!-- source-snapshot id=TG path=/,/<!-- \/source-snapshot id=TG -->/p' \
  /mnt/srcds/Strata/bench/ab-v0210/notes/inference-optimization-master-plan.md | \
  sed '1d;$d' | sed '1d;$d' > /tmp/tg_snapshot.md
sha256sum /tmp/tg_snapshot.md /mnt/srcds/Strata/bench/ab-v0210/rpcprof/gpu-rounds/tg-optimization-chapter.md
# Result: MATCH (02abecd2e0c10360a640881d2d641e060c5ea1ef73c80c348d7a2a36bb39552c)

# 5. Extract and verify CODE snapshot (id=CODE)
sed -n '/<!-- source-snapshot id=CODE path=/,/<!-- \/source-snapshot id=CODE -->/p' \
  /mnt/srcds/Strata/bench/ab-v0210/notes/inference-optimization-master-plan.md | \
  sed '1d;$d' | sed '1d;$d' > /tmp/code_snapshot.md
sha256sum /tmp/code_snapshot.md /mnt/srcds/Strata/bench/ab-v0210/notes/code-opt-plan.md
# Result: MISMATCH (snapshot: 73c6acf9ee6289fb5f4636996bc6cc9f08e3e48e1afb4c4ef2bb00a6bdb298d8 vs source: fb8d30d31c6ba91ee8c9d3185dfbbad20cf02d4242475c257d551499e8f2f7ac)
# Diff: whitespace in phasing diagram + "CONFIRMATED" typo

# 6. Extract and verify ROAD snapshot (id=ROAD)
sed -n '/<!-- source-snapshot id=ROAD path=/,/<!-- \/source-snapshot id=ROAD -->/p' \
  /mnt/srcds/Strata/bench/ab-v0210/notes/inference-optimization-master-plan.md | \
  sed '1d;$d' | sed '1d;$d' > /tmp/road_snapshot.md
sha256sum /tmp/road_snapshot.md /mnt/srcds/Strata/bench/ab-v0210/notes/perf-roadmap-2026-11-15.md
# Result: MISMATCH (snapshot: f65fb8899b369ee2d2e46e3874433f3be646b0c39c62f3196ce772fef6dc3254 vs source: 3bf80d0ac6e6c51e4fc0ec0b333b3c3915b0db750d4c2b6186256df44bcd6f6b)
# Diff: table pipe formatting (compact vs spaced)

# 7. Spot-check critical code anchors in source tree (read-only)
grep -n "wait_flag_ge(m_flag_, ring" /mnt/srcds/Strata/src/core/verify.cpp | head -1
# Match at line 2460: "wait_flag_ge(m_flag_, ring, cs)"

grep -n "had_folded" /mnt/srcds/Strata/src/core/expert_helper_candidates.cpp | head -1
# Match at line 80: "if (had_folded[(size_t) l]) { ++stats.hadamard; continue; }"

grep -n "STRATA_QFUSE" /mnt/srcds/Strata/src/core/arch_defaults.cpp | head -1
# Match at line 86: "STRATA_QFUSE = 1,"

grep -n "proxy_ms_begin_" /mnt/srcds/Strata/src/core/remote_experts.cpp | head -1
# Match at line 177: "proxy_ms_begin_ = 0;"

grep -n "STRATA_VERIFY_PROFILE" /mnt/srcds/Strata/src/core/verify.cpp | head -3
# Matches at lines 1147, 1158, 1379
```

---

## SUMMARY

| Blocker ID | Priority | Status |
|------------|----------|--------|
| R1 | CRITICAL | **OPEN** — Snapshot integrity failed (CODE, ROAD) |
| R2 | CRITICAL | **OPEN** — F2→F3 schedule contradiction |
| R3 | HIGH | **OPEN** — Missing ROAD-C2/C1/TG-S3 mechanism detail |
| R4 | HIGH | **OPEN** — Fabricated coverage counts |
| R5 | HIGH | **OPEN** — Historical schedule not labeled |
| R6 | MEDIUM | **OPEN** — GPU/CPU test classification incomplete |
| R7 | MEDIUM | **OPEN** — TG-S3 under-specified |
| R8 | MEDIUM | **OPEN** — Mlock scope not reflected in ANNEX |
| R9 | LOW | **OPEN** — DEQUANT_SIDE parity gate clarity |

**Overall:** The master plan has strong structural reconciliation and conflict resolution, but fails the core requirement of **exact source preservation** (2/4 snapshots corrupted) and has **internal contradictions** on the most critical scheduling question (F2→F3). The coverage counts are unsubstantiated. These must be fixed before approval.

**Next Round:** Reviewer will re-verify all four snapshot hashes, check F2→F3 consistency across §3.2/§6.2/§6.3, verify ROAD-C2/C1/TG-S3 mechanism sections exist with source-level detail, and confirm coverage counts are either removed or enumerated.
---
---

# Review Round 2 — Appended Independent Review

**Review Round:** 2 (appended; Round 1 above is preserved unmodified)
**Date:** 2026-11-19 (post 21:03 CET operator restart)
**Reviewer:** Independent Oracle — Swift-1.5 (Qwen3.8-Flash-Next IQ3_XXS, llamacpp). Different reviewer backend than Round 1 (Nemotron). Round 1 findings and its extraction evidence were **re-verified independently, not trusted**.
**Reviewed Artifact:** `/mnt/srcds/Strata/bench/ab-v0210/notes/inference-optimization-master-plan.md`
**Reviewed SHA256:** `301a4cadd2b1b519ae752424f64325beee89869ca5e1d98e9e4392f6e3e86d08` (master mtime 2026-11-19 20:34:23 CET, frozen)
**Scope:** Documentation / research-plan readiness only. No implementation, benchmark, GPU, HTTP, process or promotion authority implied. No fresh GPU experiments demanded or performed.

**Four Source Inputs (re-verified unchanged at this round):** identical paths/SHAs to the Round 1 table — `db7bc537…61d1c` (61 lines), `02abecd2…9552c` (124), `fb8d30d3…f7ac` (480), `3bf80d0a…6f6b` (49). Σ 714 original lines.

---

## VERDICT: **APPROVE**

**Unconditional approval, for this exact artifact only:** `301a4cadd2b1b519ae752424f64325beee89869ca5e1d98e9e4392f6e3e86d08`. Approval covers documentation/research-plan readiness — faithful preservation, internal consistency, current strict quality gates and operator-safety overlay. It authorizes no implementation, benchmark, downtime or promotion. Any later byte change to the master voids this verdict (§10's own rule; re-review on the new hash).

---

## Prior Blockers R1–R9 — Resolution Status (assessed against the actual current artifact)

| ID | Round-1 complaint | Round-2 status | Evidence (master / source path:line) |
|----|-------------------|----------------|--------------------------------------|
| **R1** | CRITICAL — CODE + ROAD snapshots not byte-identical (whitespace, "CONFIRMATED" typo, compact table pipes) | **RESOLVED** | My own extractor (below, different method than Round 1's `sed`) rebuilt each snapshot body between the `source-snapshot` fences with fence/ID/marker asserts and byte-compared to the sources. All four extracted hashes equal the pinned source hashes exactly (PP `db7bc537…`, TG `02abecd2…`, CODE `fb8d30d3…`, ROAD `3bf80d0a…`). CODE body master L1003–1482 (480 lines), ROAD L1493–1541 (49). "CONFIRMATED" absent tree-wide; `hypothesis CONFIRMED` now correct at master L1357. |
| **R2** | CRITICAL — F2→F3 schedule contradiction across §3.2/§6.2/§6.3 | **RESOLVED** | Old CAND-N diagram removed. §6.2 is now a dependency-retention table: "F3 depends on F2's ring — Historical hard code dependency; both unshortlisted without a demonstrated changed premise after the later F2 null" (master L500). §3.2 chronology: "Preserve the dependency 'F3 depends on F2' without scheduling either" (L183). §6.2 HISTORICAL paragraph: "…or schedule F2 then F3" is explicitly denied (L508). No `F2→F3` schedule line survives anywhere (§9.3 L717 mentions it only to deny it). |
| **R3** | HIGH — Missing ROAD-C2 expert-determinism and ROAD-C1 LDS-retile mechanism detail | **RESOLVED** | §3.13 (master L324–335): C1 closed/ineligible, `launch_q8` beyond T=8 (fused_gr.cu:2611-2620), prefill sites 4433/8332, `gr_up_q8_kernel<T>` T≈32 vs `xs[T][640]` T≈16 64 KB LDS retile, KL≈0.019 class not current-baseline equivalence, effort L/+2–3% retained via ROAD L39 (roadmap row C1 is exactly original L39 — verified). §3.14 (L337–349): C2 with #2215/#152/351-368, cpu_order at generate.cpp:10143, placement-pin #463 generate.cpp:22434-22436, cold×5+warm×5 sha and KL-vs-pinned-CPU-oracle tests, −1…−3% band, "bit-exact by construction is the source's intended contract, not an established property". Crosswalk §9.4 L753–L754 points to the exact archive lines (L35–39, L40). |
| **R4** | HIGH — fabricated coverage counts ("172 discrete items, 0 omitted") | **RESOLVED** | §9.6 and all magic totals removed. §9 preamble: "No invented aggregate 'item count' is used." §9.5: "61 + 124 + 480 + 49 = 714 original lines… a byte-preservation scope, not a semantic item-count proof." I independently verified the crosswalk's claimed original line ranges against the real sources: PP (L1–7/8–15/17–23/25–30/31/33–40/41–46/48–52/54–57/59–61 all exact); TG (18 prior-work rows exactly L11–L28; upstream leads exactly L66–L76; S1–S4 exactly L82/88/94/100 inside claimed L80–86/88–92/94–98/100–104; L106 not-shortlisted exact; L108–114, L116–124 exact); CODE (14 anchor rows = L14–L27 incl. backticked `expert_helper_protocol.hpp` at L25; 9-row master L29–43; F-ranges F6 L79–114, F8 L116–145, F1 L147–179, F7c L181–204, F2 L207–261, F3 L263–288, F5 L291–332, F7ab L334–364, F4 L367–416 — every section header inside its claimed range; DoD rows exactly L470–478 with F7c/F7ab separate = 9 rows); ROAD (A1 L14–18, A2 L19, A3 L20, A4 L21, A5 L22, B1 L24–28, B2–B6 L29–33, C1 L35–39, C2 L40, C3 L41, top-3 L43–47, deprioritized L49 — all exact). Counts asserted in the brief's audit targets hold: 18 TG prior-work rows, 11 upstream rows, 14 CODE anchors, 9+9 CODE master/DoD. |
| **R5** | HIGH — P0–P3 / Day 1–5 schedule not labeled historical in operative text | **RESOLVED** | Operative §6.2 carries a dedicated "**HISTORICAL/NOV15**" paragraph (master L508): CODE L45–76 and L432–463 "archived at §11.3. They do not authorize current work, repeat already landed items, or schedule F2 then F3. Their 'KL boots NOW' wording is not current permission." §9.3 L717 repeats the label per range. §5.7 resolution: "Old floors, forecasts, P0–P3 order and Day 1–5 calendar are historical." Snapshot retains the schedule verbatim (required) — now fenced off by operative text. |
| **R6** | MEDIUM — CPU-only vs device parity classification incomplete | **RESOLVED** | §3 preamble (L144–146): "CPU means a genuinely host-only fixture; GPU includes kernel parity, device-context tests and any engine boot/quality/performance leg. A host comparator reading GPU-produced golden bytes does not make producing those bytes CPU-only." §8.1 splits Unit/Parity (CPU) vs (GPU) with `expert_cache_batched_test.cpp` (device context) and `sampler_mapped_parity.cu` explicitly GPU-class, incl. "tests named 'core' that initialize a device." Every §3 card's Validation row now carries explicit **CPU:**/**GPU:** tags. §8.2 distinguishes slot/byte boot-fill denominators from PP tokens/s and states "VmHWM via clear_refs reset = RSS high-water mark" with PSS separately sampled. |
| **R7** | MEDIUM — TG-S3 attention card under-specified | **RESOLVED** | Dedicated §3.15 `MECH-ATTN-LANECELL` (master L351–363): status OPEN with `qsa_decode_attn.cu:919,966-1014` + local int8 KV; explicit correction of roadmap L19 "likely inert" (verified against roadmap L19 actual text, which also names MERGE_V2/EXPDEC — §3.9 L287 correctly limits the correction and forbids conflating the six prior A2 knobs); prerequisite = S1-priced attention slice; exact int8-KV/context equivalence with no swizzle/reduce-order tolerance; falsify/rollback rows; cites TG review original L18 for "source comment claiming bit-identical attention scores is not local end-to-end equivalence evidence" — I verified tg-optimization-review.md line 18 contains exactly that sentence. |
| **R8** | MEDIUM — ANNEX mlock scope not reflecting operator constraints | **RESOLVED** | §6.3 annex row 3 (master L515): expert weights held in **system RAM only**; GPU weights in VRAM; arena already locked; coverage/fit audit required; "**No PLE-table mlock; no blanket `mlockall()`**"; "Not all model experts are claimed RAM-resident"; 18w/18x `660d9f0dc7` (4608 spans, 46.84 GiB, 128 s) correctly labelled historical; RAM caps removed in test arms only; no default/prod change; live arms need fresh GO. §1.4 L53 and §7.3 memory row (L559) carry the same scope. Journal facts independently confirmed: journal L3194–3195 records commit `660d9f0dc7`, "locked 46.84 GiB over 4608 spans in 128.0 s". |
| **R9** | LOW — DEQUANT_SIDE parity-gate ambiguity | **RESOLVED** | §3.12 PP-B2 (L319): "**GPU parity, lanes unparked, fresh GO** first… If any relevant exact-equivalence mismatch remains, candidate stays blocked; rounded KL zero cannot rescue it." The old "env-only A/B, no KL needed" survives only inside the archive (L842) with §5.5 explicitly routing to current §1.4/§8, and §2.2 PP-GATES / §3.12 historical row marked superseded. This is stricter than R9 asked; correct under current policy. |

**Round 2 new blockers: NONE (R10+ not opened).**

Confirmations of the standing concern list (independently re-checked, no new defects found):
- **F2 chronology honesty:** §3.2/§4/§5.1 (master L183, L373, L424) state the measured-vs-amended comparison is **unknown** and forbid asserting the null tested an older implementation, lacked QFUSE parity, or missed the amendment. F2/F3 unshortlisted; no schedule. ✓
- **Latest-TG status inventory:** §2.2/§4 carry F3/F4/F5 not landed; F1/F6/F8 landed off; F7c landed incomplete; F7ab landed inactive (RAM-arena fill). Inventory, mechanism cards and phase tables agree. ✓
- **ROAD-A1/A3/B6/A2:** historical + later null/no-adoption rows (§4 L389–391, L399; §5.4; §3.8/§3.9 disclaimers) — no resurrection; "distinct mechanisms ≠ surviving speed claim" stated. HC_Q8 closed; C1 closed with "historical rebaseline escape clause is not authorized" (§3.13 L330). ✓
- **Quality gates:** exact relevant operation/logit equivalence OR credible unchanged-arithmetic proof + executed engaged tests; teacher-forced KL 0.00000/no-NaN retained; roundedly-zero/top-1/PPL/free-hash insufficient; no mean-KL ≤0.001 fallback (§1.4 L47, §8.2 L598, §3.14 L346). Same-build A/A = control, never tolerance (§8.2 L598, §3.14 L349). No precision/quant/context/KV/request-budget shortcut as "shrink" (§3.12 PP-B1 L318). ✓
- **Whole-program research:** §7.3's eight domains cover cold/incremental PP, GEMM/dequant/rocBLAS (ROCm 6.1 + xnack constraint honored), TG attention/sampler, CPU pool/helper transport, TTFT/stop/cancel, checkpoint/park/restart, boot/fill I/O, memory placement/expert locking — each with upstream AND external rows and deliverables; §7.2 preserves the 11 TG leads with full URLs/SHAs archived at TG L60–78; §10 L785 states research is planned, not executed. ✓
- **Operator safety:** fresh GO before EVERY down, parentalone windows, no specialist mid-turn, stop/boot separate via on-disk scripts, no inline pattern-kills, hard hang = operator incident (§1.4 L49–50, §8.5 L624–628). No promotion inherited from plan approval (§1.1 L22, §6.1 CLOSE L493). Historical P0–P3/first-week and old ≥24 tok/s / no-KL floors are archive-only. No new calendar weeks (§7.5 L572). ✓
- **Provenance:** §1.2's four "Last Change" commit/date claims match `git log -1` exactly (eab11571e4 11-18 11:16, 97ea0b0edd 11-18 09:05, bd4172ed00 11-15 17:32, 20c9059a68 11-15 14:08). §1.2's `d649e5bba5` (chapter prints shortened `d649e5b`) is a real commit: `d649e5bba5ca0fa9ee1c34369707bc32122543ab`, "docs(bench): save TG research draft for independent review", 2026-11-18. ✓
- **Versioned-anchor discipline:** §3 preamble L144 + §9/§10: anchors pinned to `84fc595dd9` / `af1e64a5cb4b…` trees, "not all reverified against current production", recheck implementation not ancestry/line numbers. (Round 1's raw current-tree greps are superseded by this discipline; I did not re-assert them either.) ✓

---

## Current-Operations Annotation (ledger-owned; master FROZEN, do not edit to chase runtime state)

Master §1.4 L51 records "**Current production baseline:** reserves 700/700 MiB … config SHA `b221140fc394…35df8`". That was true when the master froze (mtime 2026-11-19 **20:34:23 CET**). At **21:03:35 CET** the operator directed a prod restart changing **reserve values 700/700 → 256/448 MiB** (helper 156 MiB, engine/capacity/parking/RAM unchanged) with **config SHA `95ef3693b3f598e6bdbe534ef7a9b195e13b9e1705acc0cad2d414db09f21852`**; canonical engine unchanged (`b0ec0109ebc50c982512c371d81b2c15a8f72c70edf8a461a4ff10337da52f02`, generation1 enginePID 3538773; /v1/models + real completion HTTP200 "OK", finish stop, 28 completion tokens, identity unchanged before/after). Evidence: `/mnt/srcds/Strata/bench/ab-v0210/rpcprof/out/operator-low-reserve-restart-20261119-210335-3506907/`. This is an operator-selected runtime state, not a benchmark result or plan acceptance. Consequence for readers: master §1.4's config SHA/reserve values are a **dated pre-restart baseline**; the §6.3 margin retest's 256/448 arm is now also the live runtime config. Identity/restore checks (§8.5, §8.6) must read hashes from the evidence directory / launcher variants, **not** from §1.4 L51, or they will misjudge the operator-approved config as drift. If the parent ever unfreezes the master for an unrelated byte change, add a "(as of 2026-11-19 20:34 CET, superseded 21:03 by 256/448 + config 95ef3693…)" date annotation — not required for approval of the frozen artifact.

---

## Validation Evidence (commands actually run this round, read-only)

```text
1) sha256sum master + 4 sources; wc -l            → master 301a4cad… (frozen, matches pin); sources match pins;
                                                    61+124+480+49 = 714 lines. EXIT 0.
2) python3 /tmp/opencode/rev2/extract_verify.py   → parses all four <!-- source-snapshot --> marker pairs (unique IDs,
                                                    fence asserts), rebuilds body between fences WITHOUT trimming,
                                                    byte-compares to sources:
                                                    PP    masterL798-859   marker-sha-ok=True  bytes-identical=True  extracted db7bc537c4e39c36…
                                                    TG    masterL869-993   marker-sha-ok=True  bytes-identical=True  extracted 02abecd2e0c10360…
                                                    CODE  masterL1002-1482 marker-sha-ok=True  bytes-identical=True  extracted fb8d30d31c6ba91e…
                                                    ROAD  masterL1492-1541 marker-sha-ok=True  bytes-identical=True  extracted 3bf80d0ac6e6c51e…
                                                    ALL-FOUR-BYTE-IDENTICAL: True. EXIT 0.
                                                    (Bodies also saved under /tmp/opencode/rev2/ for audit. Scratch only;
                                                    no project file outside this ledger was written.)
3) git log -1 per source                           → §1.2 provenance table verified exactly (4/4).
4) git log/cat-file d649e5bba5                     → real commit d649e5bba5ca0fa9… "save TG research draft for
                                                    independent review" 2026-11-18. §1.2 expansion legit.
5) python3 /tmp/opencode/rev2/lines_check.py       → structural line numbers of all four sources dumped and compared
                                                    against every §9 crosswalk range claim: all exact (see R4 above).
6) sed -n '14,22p' tg-optimization-review.md      → line 18 = bit-identical-attention-scores caveat (as cited by §3.15).
7) grep journal serving-tg-reeval-split-retest-2026-11-11.md → L3194-3195: 660d9f0dc7, 4608 spans, 46.84 GiB, 128.0 s ✓.
8) Master residue greps                             → no "172", no "0 omitted", no "CONFIRMATED"; "≥24 tok/s" and
                                                    "no KL needed" appear ONLY inside archival fences (master L842/849)
                                                    or as explicitly superseded rows (L93, L320); no CAND-N residue.
9) read (full file) master L1–1553 in four passes   → operative sections 1–10 read in full; all four originals read in
                                                    full (byte-identical to the complete §11 snapshots).
No production/process/HTTP/GPU/build operations were performed by this review. Production :8009 stayed up throughout.
```

---

## Non-Blocking Suggestions (kept separate from blockers; none gate approval)

- **N1 (runtime-state annotation):** the §1.4-baseline dating described above, applicable only if the master is ever reopened for another byte change. Current-state truth lives in this ledger + the journal.
- **N2 (inherited S2, cosmetic):** stable anchors for §3 mechanism cards (`#mech-…`) would tighten cross-references. Snapshot anchors (`#snapshot-*`) already exist and resolve.
- Round-1 suggestions S1 (VmHWM/PSS wording), S3 (F4 <0.3 ms/layer descope threshold) and S4 (extended profiler-site list) are now **satisfied** in §8.2, §3.4 and §8.4 respectively.

## Summary Table

| Item | Status |
|------|--------|
| R1 snapshot integrity (all four) | **RESOLVED — byte-identical, independently extracted & hashed** |
| R2 F2→F3 scheduling contradiction | **RESOLVED** |
| R3 ROAD-C1/C2 mechanism detail | **RESOLVED** |
| R4 fabricated coverage counts | **RESOLVED — crosswalk now line-exact (verified programmatically)** |
| R5 historical schedule labeling | **RESOLVED** |
| R6 CPU/GPU test classification | **RESOLVED** |
| R7 TG-S3 card | **RESOLVED** |
| R8 ANNEX mlock scope | **RESOLVED** |
| R9 DEQUANT_SIDE parity gate | **RESOLVED** |
| New Round-2 blockers | **NONE** |

**Final verdict (Round 2): APPROVE — unconditional for `301a4cadd2b1b519ae752424f64325beee89869ca5e1d98e9e4392f6e3e86d08`, documentation/research-plan readiness only.**
No implementation, benchmark, downtime or promotion authority is conferred. Any future byte change to the master requires re-review on the new hash.
