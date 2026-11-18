# Integrated whole-plan review — consolidated serving-performance plan

**Reviewer:** independent oracle `ses_e12771b3bffev76LmVooHz0NnX`, 2026-11-18 (TG-only reviewer lineage; new whole-plan scope).
**Reviewed artifact:** `bench/ab-v0210/rpcprof/gpu-rounds/serving-plan-current.md` (209 lines, 9 sections + header/hard-constraints), pinned commit `eab11571e48957d2bb54c188e3ca523c30fd64b9`, branch `tg-stall-cause`. Working tree confirmed at that commit (HEAD == pinned).
**Lineage read:** `AGENTS.md`; journal rounds through 18da; `speed-plan-v2.md`; `tg-optimization-chapter.md` (`d649e5b`) + `tg-optimization-review.md` (`97ea0b0`); `CHAMPIONS-RESULTS-20261118.md`.
**Source:** `/mnt/srcds/Strata-wt-integ` at `af1e64a5cb4b617d52c0f9b416af76d28c42bc4f`. Canonical prod (`launch-v0238.sh`, `prod-config-v0238.json`, `v0215/serve/server.py`, `v0238p0/rpcw-hip/strata`) inspected read-only for restore-path existence only.

## VERDICT: **APPROVE** — whole-plan research-plan readiness ONLY

Approval is for this consolidated document as the settled research/verification plan. It grants **no** implementation, benchmark, GPU-traffic, production-down, deployment, or promotion authority, and certifies **no** candidate as validated. TG-component approval (`d649e5b`) and RAM source/CPU approval are correctly kept separate and are not extended by implication; reviewed source lines 3, 13, 162 and 204 preserve that boundary.

## Blockers
**None.** No plan-level defect found. Every plan claim I re-checked against source, journal, or upstream is accurate or correctly hedged; every candidate-validation gap is a deliberately-gated future fact, not a plan defect.

## Supporting evidence (file:line)

| Focus | Result | Evidence |
|---|---|---|
| Artifact identity | OK | HEAD == `eab11571`; 9 sections; single current plan; supersedes v2 ordering/estimates/floor/gates (`serving-plan-current.md:3,10,199`) |
| §4 P1 routed-stream is real, default-off, opt-in | OK | `src/prefill/prefill.cpp:4122-4130`; depth default 16 clamped 1..256 at `include/strata/prefill/routed_stream.hpp:36` — matches "depth 32 pilot, then 16/32/64" |
| §4 P3 dequant-side + parity test present | OK | `src/prefill/gemm.cu:642` (getenv), `:1482` (side-stream into prior scratch); test `src/prefill/gemm_dequant_side_parity.cu` exists |
| §4 P0 timing/trace flags; overlap already present | OK | `STRATA_PREFILL_TIMING`/`STRATA_TRACE` present; issuer default-on `src/prefill/prefill.cpp:4109-4111` — matches journal 18cs (`journal:3930`) and `:120` |
| §4 P2a `complement_pin_limit_` real | OK | `src/core/expert_source.cpp:1618,4340,4892` |
| §4 P4 constexpr levers not env-flippable | OK | `MMQ_GROUP`/`STAGE_GRP` present; journal 18cs confirms constexpr; P4 correctly requires code change + separate review, not MMQ-on |
| v2 supersession faithful (33s bucket, ~2s, −20%, 24 tok/s floor, "bit-identical-by-contract→no KL") | OK | all demoted/retired at `:10,119,132`; journal confirms fit=slot common cause, drop ceiling ~2s, MMQ measured regression (`journal:3930`) |
| §7 quality gate has no rounded-KL loophole | OK | `:9,174`; §4 `:132` closes v2 contract-only shortcut; detached parity test explicitly insufficient |
| §1 RAM semantics honest, live gate OPEN | OK | `:30,32,50` — 2560 MiB floor unchanged; 677 MiB band named-limiter observed; items 2–4 (≥20-turn pressure, skip-count, deepest-published restart) remain open, not laundered as CPU-test proof |
| §2 champion confounds honest | OK | `:64` 50.0 vs 62.2 GiB, fixed order, swap residue, low-VRAM, different traces → "no overall winner / no quality equivalence" |
| §5 TTFT not conflated with wall | OK | `:140-141` flags existing `ab9_driver.py` wall ≠ TTFT (file exists `bench/ab-v0210/rpcprof/ab9_driver.py`); requires streaming/timestamped mode first |
| F1–F8 ledger not reviving losers | OK | `:189-198`; F3/F5 absence confirmed (no `STRATA_CPU_POOL_XQ`/`STRATA_PLE_ASYNC_GATHER`); F1 default-off `sampler_mapped.hpp:40`; F8 `penalty_hist_stage.hpp:40` |
| One-lane / prod-interruption / restore | OK | `:11-12,167,180`; canonical launcher + config + wrapper + binary all present on disk |
| Upstream (inherited from `d649e5b` review) | carried | All §3 leads previously verified true via read-only GitHub API; unchanged this round (TG facts, not re-deriving PP) |

## Non-blocking notes (parent discretion; do not gate on these)
1. `serving-plan-current.md:183` calls `lane_restore.sh` a non-canonical "v0233" restore. I confirmed it invokes a **different** launcher (`launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh`), not `launch-v0238.sh` — so "not canonical" is TRUE — but did not pin a literal `v0233` inside the script body (it shells out). Optionally cite the referenced launcher/version line so the label is directly checkable.
2. P3's parity test builds under `build-mixed-gpu/rpcw-cuda/`. §163 (ROCm 6.1, all-backend builds) and §175 ("host-only can't certify GPU") already cover this, but a one-line reminder that P3 GPU parity must engage the **gfx906/HIP** target (not only the CUDA target) would pre-empt a misread.
3. §194 F5 prohibition is compatible with source, but `early_ple_flag_mode()` returns 2 by default under HIP (`src/core/verify.cpp:389-395`). A future reader should not conflate that default PLE-flag launch timing with the rejected CPU-rows-ready raise; consider making the distinction explicit.
4. `:190` 18cq depth-128 "about 19%" — I located "15–19% slower"/"−19%" adjacent in journal (`journal:278,712`) but not a line pinning "18cq depth 128" to that figure; disposition is do-not-repeat (conservative), so no impact.

## What was NOT run (scope respected)
No builds, tests, CTest, benchmarks, GPU work, production requests (incl. health/completion), fetch/clone/ref mutation, process control, or prod interruption. Only read-only `git show`/`cat-file`/`ls-tree`/`merge-base`, `grep`/`sed`/`read`, and prior-round read-only GitHub API results. No experiments authorized to resolve candidate validation.

## Boundary: plan approval vs future gates
This APPROVE marks the plan settled for planning. The following remain **future, separately-authorized gates**, unchanged: RAM live acceptance (§1 items 2–4); G0/G1 attribution and P0 profiling windows; G2/G3/P1–P4 candidate eligibility + exact-equivalence + engaged-test proof + bracketed benefit; T1–T5 turnaround; canonical-restore verification; and any default flip/promotion (operator-only). Approval does not imply these are satisfied.

**Actionable feedback for parent:** none blocking. Artifact may be recorded as the whole-plan settled reference. Next steps proceed only under §6 §2–7 authorized windows.
