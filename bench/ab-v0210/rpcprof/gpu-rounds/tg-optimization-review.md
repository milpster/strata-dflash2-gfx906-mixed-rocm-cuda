# Independent TG research-plan review — 2026-11-18

| Field | Result |
|---|---|
| Reviewer | Oracle session `ses_e12771b3bffev76LmVooHz0NnX` |
| Reviewed artifact | `d649e5b:bench/ab-v0210/rpcprof/gpu-rounds/tg-optimization-chapter.md` (draft v1) |
| Verdict | **APPROVE — for research-plan readiness only** |
| Blocking findings | **None** |
| Not established or authorized | Candidate correctness/performance, implementation, GPU execution, production promotion |

The reviewer independently inspected the pinned source and sampled upstream references through read-only GitHub API calls. No fetch, clone, ref change, build, test, endpoint request or process operation was performed. The champion-panel statistics remain parent-owned evidence, not an independent statistical reanalysis by the oracle.

## Verified findings

- Serving helper request counters use `helper_tier_p` at `src/program/generate.cpp:24110-24117`. Helper timing output instead reads `remote_experts[r].ms_begin()/ms_finish()` under a remote-cache gate at `24294-24298`. The proxy has separate `proxy_ms_begin_` accumulators (`src/core/remote_experts.cpp:720,747,819,839`); the plain timing uses a different path at `998`. Zero helper timing output does not establish zero helper latency. The narrow S1 attribution investigation is justified.
- Profiler observer effects extend beyond the three sites named in the chapter: `src/core/verify.cpp:2590,2623,2643` also guard wait/copy, rows and hit-combine fusion with `!prof_on_`; profiling also affects stamp/branch handling at `1770`. No profiler stage total may be presented as an unqualified production budget.
- Plain greedy head selection is already captured at `verify.cpp:2828-2834`. The optional sampled path is default-off and has additional eligibility gates. Capture at `2846-2853` requires nonzero sampler blocks, `next_ == nullptr`, no batch recording and bounded rows; the mapped sampler is a no-op beyond its 64-block vocabulary bound. An env flag alone proves no engagement.
- The verifier service-loop, post-loop synchronization, DFlash window-selection and lane-cell attention anchors sampled by the oracle were correct. A source comment claiming bit-identical attention scores is not local end-to-end equivalence evidence.
- The sampled missing facilities are absent from the pinned built tree: non-SYCL `STRATA_FOLLOW`, `force_check.py`, `kq_window_bench.cpp`, `STRATA_CPU_POOL_XQ` and `STRATA_PLE_ASYNC_GATHER`. Ancestry alone was not treated as proof of functional absence.
- Upstream commit identities/dates and PR 3220/2955/3307 open status/head identities sampled in the chapter were confirmed. Upstream's default branch is `main`. The forcing and decode-window benchmark commits are merged there but not native facilities that can merely be enabled in this pinned tree; compatibility/port work remains gated.

## Non-blocking notes and disposition

| Note | Disposition |
|---|---|
| Use full prefill source paths | `prefill.cpp` means `src/prefill/prefill.cpp`; `gemm.cu` means `src/prefill/gemm.cu`. Recorded here without changing the reviewed substantive v1. |
| Upgrade sampled upstream presence/status claims | Independent confirmations are recorded above. Untested compatibility, arithmetic and performance claims remain unproven. |
| Price all profiler path changes | Additional guarded sites are recorded above; the chapter already requires observer-effect controls. |
| Make sampler tests non-vacuous at eligibility boundaries | Future fixtures must prove the eligible path runs, and separately verify host fallback outside vocabulary/row/capture limits. Forced traces must not bypass the sampled operation being judged. No candidate is enabled by this note. |

The parent inspected the cited serving counter wiring, profiler fusion guards and mapped-sampler capture/contract after the verdict. These notes refine evidence and future checks; they do not expand the approved scope or relax a gate. No new benchmark or implementation was performed.

## Approval boundary

The substantive plan at `d649e5b` is settled. The attached chapter's approval header and review link record this verdict; its pre-review uncertainty labels remain intentionally conservative. The mixed-boot historical aggregate is **not accepted as reproducible current-build evidence**. No quality tolerance may be inferred from baseline performance variation or rounded KL output.

Next work, only after separate authorization: prepare reproducible trace/attribution controls, then investigate eligible candidates one at a time. Exact engagement and unchanged-arithmetic/exact-equivalence gates, memory accounting, PP non-regression and canonical restoration remain mandatory. The RAM pressure/restart gate and SSH publication are separate outstanding matters.
