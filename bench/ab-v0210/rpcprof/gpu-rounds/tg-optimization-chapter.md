# TG optimization chapter — DRAFT v1, 2026-11-18

**Not approved; not authorization to implement, benchmark or promote.** Fresh analyst: `ses_e12bcc978ffeFMfJVS8eRW6VZ2`. Independent oracle review is pending. Parent condensed the research below, corrected the analyst's stale description of the latest build as unmeasured, expanded upstream links, and marked exploratory aggregates as unvalidated. Feedback returns to the same analyst.

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

**Independent review requested.** Revise with the same analyst until explicit oracle approval; only then mark this chapter approved and link it as the settled plan.
