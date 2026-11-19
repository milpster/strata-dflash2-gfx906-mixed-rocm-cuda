# Consolidated serving-performance plan — 2026-11-18

**Integrated review: APPROVED for whole-plan research readiness only, no blockers.** Oracle `ses_e12771b3bffev76LmVooHz0NnX` reviewed the complete substantive artifact at `eab11571e48957d2bb54c188e3ca523c30fd64b9`; see [integrated review](serving-plan-integrated-review.md). This is the single current plan for **prompt processing (PP), token generation (TG), and total response turnaround**, including cache/restore and boot/recovery costs. It consolidates the earlier prefill program, F1–F8 roadmap, approved TG research chapter and RAM policy work. It supersedes their conflicting sequencing, stale estimates and acceptance wording; the earlier files remain evidence, not competing execution instructions.

The TG chapter and RAM source/CPU readiness retain their separate approvals. The whole-plan verdict above is independent; it does not extend candidate-validation claims. Consolidating, reviewing or approving this plan does not authorize implementation, benchmark traffic, a production-down window or promotion. No new speed gain is promised.

**Operator sequencing update — 2026-11-19:** “well then move on to speed improvement work and park the whole ram pressure thing for now”. RAM-pressure calibration/acceptance, raw-adapter follow-ups and restart/checkpoint fixtures are parked, not completed, and are no longer prerequisites for the speed work below. Preserve the unfinished RAM files and evidence; do not deploy the RAM candidate. The active priority is one bounded, quality-preserving PP/TG speed experiment using existing benchmark facilities, with matched controls and the relevant exact-quality/engagement checks. The slowdown investigation remains unresolved; reject a degraded control rather than count recovery as a candidate gain. Only the parent runs an isolated GPU window after the sole specialist finishes, then restores canonical production. Gaming, RAM-pressure workloads, quality/capacity reductions and promotion remain outside this change of priority.

## Hard constraints and order

- Zero quality sacrifice. Preserve quantization, precision, context capacity, model semantics and request/output budgets. Require exact relevant byte/logit/operation equivalence, or a credible unchanged-arithmetic-path proof plus executed, engaged tests. Retain established teacher-forced KL 0.00000/no-NaN checks; rounded zero, matching top-1 or matching PPL alone is not proof of bit identity.
- This document supersedes speed-plan-v2.md's relaxed numerical-adoption threshold. That threshold is not authorized.
- Exactly one specialist or GPU measurement lane at a time. The parent waits while a specialist uses the shared inference backend. No benchmark traffic during specialist inference.
- Only the parent stops/starts prod, between specialist turns. Preparation agents must finish before any operation that could interrupt their backend.
- Completed order: RAM source repair/review → plan saved → three-champion comparison → fresh history/upstream TG analysis → independent TG chapter approval → independent whole-plan approval. Subsequent work remains governed by §6 and separate operator authorization, not by the review verdict.
- The earlier comparison authorization was consumed by that completed panel. New profiling, pressure reproductions and candidate runs need separately authorized, bounded parent-owned windows. Unrelated sweeps remain parked.
- Preserve canonical production: `/mnt/srcds/Strata/launch-v0238.sh`, original `prod-config-v0238.json`, engine `b0ec0109...`. Research source anchors use `af1e64a5cb4b`; that candidate has not been promoted. Freeze an explicit matched source/control for each future experiment rather than mix RAM and performance changes between arms.
- Keep config `env` as the single source for shared knobs: it overrides launcher settings. Verify effective engine environment and `/proc/<pid>/exe` identity; a shell-prefix env or a launcher `BIN` edit alone is not engagement evidence.

## 1. RAM restore policy: source approved, live acceptance pending

Engine branch: `conv-cache-ram-guarantee`, based on `42cb41b314`.

| Revision | Outcome |
|---|---|
| `aecd836e68` | Rejected: cold-file advice could stop reclamation before parked host copies were tried. |
| `18e8acaeaa` | Correctness repairs accepted; headroom sizing still required a policy cap. |
| `af1e64a5cb4b617d52c0f9b416af76d28c42bc4f` | Explicit oracle APPROVE for source/CPU readiness and controlled deployment. |

Behavior: staged reclamation advises eligible disk-tier pages once, then consumes eligible parked host copies until admission, exhaustion, unknown telemetry, or a finite bound. No-progress advice is not exhaustion. Guarded donors and their pair sidecars are protected. Logs distinguish advised file bytes from measured availability deltas.

Park snapshots and checkpoint host copies preserve prospective restore headroom. Restores use the unchanged **2560 MiB** safety floor, without the extra band. With the disk tier open, the band is the smallest applicable positive term among the file-read cap, cache budget/4, restore checkpoint-chain host geometry, and an absolute 1024 MiB ceiling; with that tier closed it is zero. Boot logs name the limiter and all terms. It is not an OS reservation: unrelated allocations and session-file writes can still consume the band. Real hard-low conditions still refuse safely.

Trade-offs: more disk instead of RAM hits; a refused intermediate checkpoint is also absent from the published durable chain, so restart recovery uses the deepest published checkpoint. Do not describe this as an unconditional RAM guarantee.

### Verified CPU evidence

- Focused HIP build current, exit 0; no GPU execution.
- Six explicitly enumerated CTest tests passed, exit 0: cache 4861, memory 64, reclaim 27, session-save-reclaim 119, disk-cache 241, memory-guard 134 checks (5446 total).
- Evidence inspected: `/mnt/srcds/Strata-wt-integ/build-mixed-gpu/logs/ram-policy-final-validation-20261118.log` and `logs/final-validation-20261118/LastTest.log.final-6of6` under the same build tree.
- Candidate: `/mnt/srcds/Strata-wt-integ/build-mixed-gpu/rpcw-hip/strata`, SHA256 `8f19baaf150237e5d8085c12c842a4b5728d10bd00ce315ab0ed52ff9bd819ed`, built 2026-11-18 00:46:31.
- Production remains unchanged: `/mnt/srcds/Strata/v0238p0/rpcw-hip/strata`, SHA256 `b0ec0109ebc50c982512c371d81b2c15a8f72c70edf8a461a4ff10337da52f02`.

### Controlled deployment acceptance (not yet established)

1. Boot reports a band at most 1024 MiB and a named limiter; use that line, not an inferred numerical range.
2. Existing traffic or an explicitly authorized reproduction covering at least 20 turns: record admitted-after-reclaim lines, measured deltas, and every skipped restore. A lack of pressure does not establish pressure recovery.
3. Compare headroom-related park/checkpoint skip counts against the prior baseline; check for a new recovery-point or latency regression.
4. Check restart resume against the expected deepest published checkpoint. Do not imply this was tested by CPU tests or ordinary TG/PP runs.
5. Keep rollback binary/config hashes. Do not replace the production slot merely to run a benchmark candidate.

Boot evidence **is now observed**: CURRENT selected **677 MiB**, limited by the restore host ask (file cap 4219, cache quarter 1024, chain 677, ceiling 1024 MiB). The raw engine log, not the wrapper-only `boot.json` collector, contains it. Items 2–4 remain open. A later controlled RAM window must use isolated reproducible fixtures, log every admission/reclaim/refusal and published-chain depth, and compare against the matched prior behavior. No investigation request to current live sessions is authorized.

This is also a turnaround concern: a safe cache miss can cause a large re-read, but avoiding a miss must not consume the safety floor or weaken durable recovery. Report restore latency, reread tokens, skip counts and deepest valid restart point together. More RAM hits are not a win if they create memory-pressure or recovery regressions.

## 2. Three-stack TG/PP comparison

Status 2026-11-18: **completed**, 9/9 pairs per arm. Results, observed runtime-environment differences, memory cost and restore evidence are in [the comparison report](CHAMPIONS-RESULTS-20261118.md). No build was promoted.

| Stack | PP 16k | Cached main-call TG 16k | PP 80k | Cached main-call TG 80k |
|---|---|---|---|---|
| Latest `af1e64a5` / `8f19baaf` | 266.3 | 19.7 (16.7–41.8) | 465.3 | 32.6 |
| Nov-16 `42cb41b314` / `b0ec0109` | 253.8 | 33.1 | 466.2 | 32.6 |
| Nov-15 `81e023bfa0` lineage / `36ecbe98` | 270.5 | 34.0 (13.4–36.3) | 466.1 | 34.4 |

Median tok/s, three measured pairs per shape; actual input contexts 16008/79986 tokens. Full ranges and memory costs remain in the report. Different effective memory limits (50.0 versus 62.2 GiB), fixed arm order, swap residue, low-VRAM warnings and different generated traces prevent a pure engine-code attribution. Long PP is essentially unchanged; no overall winner or quality equivalence was established.

### Freeze provenance before measurements

The original comparison request was made on 2026-11-17; the approved latest candidate finished after midnight. Preserve that distinction in the manifest: CURRENT is the latest approved build above, with the historical champions from the two preceding request-relative days (2026-11-16 and 2026-11-15). Inspect journals, release/build manifests, saved configs, and git history to identify the actual pairs. If the available evidence implies a different daily boundary, record it explicitly before running, rather than inventing three distinct versions.

For every arm record: source commit, binary SHA256, HIP/helper binary identities, server identity, exact champion config and effective environment, model/pack identities, context limits, cache sizes, launcher overrides, and all benchmark-only deviations. Historical configs must be snapshots, not today's config relabeled. If a champion binary is missing, rebuild its proven source/config or report the arm unavailable.

### Controlled workloads

- Use the same model/pack and exact persisted input/token sequences across arms. Fix corpus seeds; the old prefill_judge's random-per-invocation prompts are not a paired corpus.
- Reuse the journal's established short and long-context TG/PP shapes where feasible. Freeze shapes, repeats, warm-up policy, arm order, and failure limits in the runnable manifest before execution.
- Measure TG from engine generation timing, excluding prompt processing. Measure PP from actual newly read tokens / prompt time. Report cache-reused tokens and context length separately; a cache hit is not fresh PP throughput.
- Preserve raw timing lines and client wall/TTFT separately. Use warm repeated samples, medians and spread; retain warm-up samples but exclude them by a declared rule, not post-hoc cherry-picking.
- Capacity/placement changes in a daily champion are part of a stack comparison; report them rather than attributing the entire delta to one code change.
- Never run two engine/helper pairs concurrently on this memory-limited machine. Historical per-arm cache directories must not mutate the production conversation-cache inventory.
- Prepare scripts/manifests while prod is up; finish the specialist turn before the parent starts the single background panel. Stream panel progress plus NUL-stripped engine timing/error output to the terminal. Report each arm as it finishes.
- Restore the known-good serving stack at panel exit, including failures, and verify it without leaving an inference specialist stranded on a stopped backend.

### Results and limits

Publish a three-row TG/PP comparison with input/context sizes, sample counts, medians/spread, absolute and relative deltas, configuration differences, errors, and exact artifacts/exit statuses. Do not call ordinary throughput evidence proof of the RAM-pressure fix. No winner is adopted merely because it is faster if quality, stability, or provenance is unresolved.

## 3. TG program — approved component, not an integrated approval

Status 2026-11-18: **complete and oracle-approved for research-plan readiness only**. Settled plan: [TG optimization chapter](tg-optimization-chapter.md); explicit no-blocker verdict and evidence: [independent review](tg-optimization-review.md). No candidate has been validated or enabled. Trace/attribution preparation, implementation, additional GPU traffic and any promotion remain separately gated; unrelated optimization sweeps remain parked.

Read old journals and adoption/rejection evidence first. Build a ledger of what was already tried: helper/cache limits, ring null, MMQ-off win, prefill warm/depth regression, and other relevant earlier rounds. Do not repeat a settled losing experiment without new evidence that changes its premise.

Inspect relevant upstream PRs and commits read-only; record URLs, SHAs, status, backend/model applicability, and whether equivalent work is already in this fork. Upstream claims are leads, not local benchmark evidence.

Use the three-stack results plus actual TG source paths to rank quality-preserving opportunities. Distinguish measured costs from source-backed hypotheses: the earlier regression fit and subtraction-derived per-slot GPU costs are not a profiler decomposition. Each proposal needs a mechanism, prior-work/upstream provenance, potential bottleneck evidence, implementation scope, equivalence gate, benchmark recipe, rollback path, and adoption rule. No quantization/precision/context shortcuts or relaxed quality tolerances.

The independent chapter review is complete. Its approved substance is incorporated below; its source/upstream ledger and [review notes](tg-optimization-review.md) remain supporting evidence. This combined plan has now received its own explicit verdict, recorded above.

| Item | Action and prerequisite | Evidence/quality rule | Close or rollback rule |
|---|---|---|---|
| **G0: fixed traces and controls** | Inspect/port the absent native forcing and CPU-slice benchmark facilities only after source/API compatibility review. Pin prompt/token files and repeat same-build A/A controls. | Forcing removes token-sequence divergence, not floating-point/placement nondeterminism. A forcing mode that bypasses sampling cannot validate a sampler. Classify tests by actual hardware needs. | If the real serving path cannot be exercised, the affected candidate stays blocked; do not substitute a weaker quality test. |
| **G1: real serving attribution** | Correct helper timing ownership/reset semantics; account for draft, verification, acceptance, host work and tail synchronizations. Keep counters append-only. | Helper request counts use `helper_tier_p`, while old timing output reads different remote-cache counters. Do not turn overlapping accumulators into additive critical-path buckets. | If counters remain unengaged or boundaries cannot be reconciled, fix attribution before candidate claims. Revert the narrow instrumentation patch if needed. |
| **G2: in-graph sampler** | Revalidate existing default-off `STRATA_SAMPLER_IN_GRAPH` only if the actual request/capture path is eligible and the measured host sampler/sync cost matters. | Greedy selection is already captured. Prove mapped inputs, RNG/draw identity, filters/penalties, host bypass, vocabulary/row limits and capture/batch/layer-chain eligibility; separately prove host fallback. | No eligibility, exact-equivalence proof or bracket-separated benefit → remain off. Earlier vacuous F1 gates are not evidence. |
| **G3: gfx906 lane-cell attention** | Investigate existing `STRATA_ATTN_LANECELL` only if attention is a meaningful measured cost on the int8-KV serving path. | A source comment about bit-identical scores is not local proof. Require exact relevant intermediate/logit equivalence and engagement across applicable contexts. Account for registers/LDS/occupancy as well as allocated VRAM. | Any unresolved arithmetic difference or no meaningful gain → remain off. Reduction-order variants are not admitted by a loose KL tolerance. |

`STRATA_VERIFY_PROFILE=1` is an altered execution mode: it disables branch/fusion paths including draft branching, conv/AB, shared-stream, wait/copy, row-copy and hit-combine fusions. Pair it with profile-off controls and price the observer effect; its named-stage totals are not automatically production costs. The old mixed-boot 1201-request aggregate remains exploratory, with no accepted current-build latency budget.

G0/G1 are measurement prerequisites, not claimed optimizations. Eligible G2/G3 are hypotheses with no promised percentage gain. Do not revive F2 ring, adaptive drafting or other closed work merely to populate a test schedule.

## 4. PP program — preserved from speed-plan-v2, corrected to the current bar

**State:** active speed-work priority alongside TG under the 2026-11-19 operator update. Select a bounded experiment from measured costs and existing eligible paths; do not start an unrelated sweep. PP means actual newly read tokens / prompt-processing time, not total context / a cached request's wall.

### P0: attribution before tuning

- Read existing boot evidence for MMQ state, manifest support, model/cache identities and effective environment; do not send investigative prod requests.
- Prepare a baseline with `STRATA_PREFILL_TIMING=1` and, if supported and useful, `STRATA_TRACE=1`. Verify that the real HIP path emits wall/device timeline, copy wait, host grouping, dequant and GEMM fields with documented boundaries and overhead.
- Compare timing-on against timing-off to detect observer effects. A large wall/timeline gap motivates an idle/wait investigation; it does not by itself identify a fix. Overlapping events and request-total fetch fields must not be summed as disjoint phases.
- The old `prompt_ms = 1338 + 3.74 × fetch` fit describes slot-count common cause, not proof of missing overlap. The old **~33-second GPU bucket, ~2-second routed-stream saving and −20% goal are hypotheses/estimates, not profiler measurements or commitments**. No inherited wall-time estimates or fixed 24 tok/s decode floor control adoption.
- Read/compute overlap already exists (whole-chunk readahead plus issuer). Do not propose "add overlap" without demonstrating a remaining exposed dependency.

### Ranked conditional PP investigations

| Item | Mechanism/scope and go/no-go condition | Memory and equivalence requirements | Falsification / rollback |
|---|---|---|---|
| **P1: routed-stream copies** (v2 A1) | Existing `STRATA_PREFILL_ROUTED_STREAM=1`; first pilot at depth 32, then 16/32/64 only if actual routed-drop/timing evidence justifies a sweep. Confirm issuer on, copy-event-every 1, copy-wait-dedupe off, no peer-device conflict. Only the eligible pinned-arena copies may be omitted; stager-staged demand copies remain. | Prove actual HIP engagement, identical consumer-visible bytes/order and correct completion/lifetime. Record ring/staging host and per-device costs. Header drop estimates are not local savings. | No drops, wrong active path, memory conflict or no bracket-separated prompt/wall gain → stop; env off. No blind three-depth sweep. |
| **P2a: host residency/profile** (v2 B1 host) | Audit actual expert-profile freshness, RAM-relief geometry, file-refault cost and `complement_pin_limit_`. Only investigate a placement change if the measured exposed cost and available budget justify it. Preserve the browser/coexistence purpose of the 50 GiB cap. | Budget against cap-adjusted room, host MemAvailable, file cache, desktop pressure and prospective restore headroom—not a historical ~16 GiB figure. Placement can change CPU/GPU arithmetic, so "same weights" alone is not equivalence proof. | No safe named budget or no exact-equivalence route → blocked. A measured PP gain that increases pressure/refault/restore cost is not automatic adoption. Restore original profile/relief settings. |
| **P2b: device residency** (v2 B1 VRAM) | Current low-free-VRAM warnings mean cache growth is unavailable without a proven reclaim of unused/duplicate/transient storage on the relevant device. No concrete shrink is currently approved. | Do not shrink context/KV capacity, change KV precision or steal helper demand reserve. State per-device peak/lifetime and allocator/graph implications. Same placement/equivalence cautions as P2a. | No named safe shrink → no experiment. Keep current capacities. |
| **P3: side-stream dequant** (v2 B2 replacement) | Audit `STRATA_DEQUANT_SIDE` in `src/prefill/gemm.cu` and the actual HIP dispatch/build guards. Proceed only if dequant is exposed in P0 and the candidate really runs on this backend. | Execute the actual GPU parity test in an authorized parent window; prove exact outputs plus stream/event/scratch ownership across repeated chunks and downstream consumers. A test's existence or a flag does not establish engagement. Account for any overlapping scratch peak. | Inert HIP flag, hidden cost, parity/lifetime gap or null performance → close. Env off; no kernel rewrite is implied. |
| **P4: GEMM/grouping work** (v2 conditional constexpr levers) | Only if P0 identifies a meaningful GEMM/grouping cost and P1/P3 do not address it: investigate `MMQ_GROUP` / `STAGE_GRP` as compile-time code changes. This does **not** authorize MMQ-on. | Separate design/implementation review, all touched backend builds, engaged exact-arithmetic/equivalence proof. No precision/reduction-order shortcut. No invented gain estimate. | If exact equivalence or a safe resource budget cannot be established, remain blocked. Revert isolated commit/variant. |

The old v2 shortcut "bit-identical by contract → no quality gate" is not carried forward. Unchanged-arithmetic/byte proof can determine the appropriate gate, but the real changed path and its scheduling/lifetime must be exercised; a detached parity test cannot certify the whole serving change. P1/P3 do not authorize unrelated A2/A3 sweeps, MMQ-on, prefill-warm, or swap-in depth 128.

## 5. Total-turnaround program — optimize the response, not just a tok/s counter

### Metrics and workload boundaries

| Metric/class | What to measure | What not to infer |
|---|---|---|
| **Time to first visible token (TTFT)** | Client send → first visible output, with queue/admission, cache lookup/restore, fresh read and first-generation boundaries where available. Preserve thinking/visible-output distinctions. | Existing non-streaming `ab9_driver.py` wall is not a TTFT measurement. Add/verify a streaming or timestamped measurement mode before claiming TTFT. |
| **Whole-response turnaround** | Client send → completed response; retain all internal continuation calls, between-call work and finalization on the critical path. Record actual generated tokens and reasoning behavior. | Main-call TG excludes continuation work. Do not shorten output/thinking, silently change stop policy or compare different traces as a causal latency gain. |
| **Cache-switch / incremental turn** | Fixed base-prefix fixture followed by a fixed appended delta; in-memory hit, disk restore and safe-miss/re-read cases separately. Record reused/newly read tokens and checkpoint-chain depth. | Fresh-prompt tags belong at the head only for intended fresh cases; doing that to a cache-hit case invalidates it. No accidental cross-arm reuse. |
| **Park/checkpoint interference** | Timestamp capture, serialization/write, response completion and next-request admission; correlate with blob I/O, host pressure and exposed device syncs. | Idle writes are not necessarily current-response latency, but may delay the next turn. Do not add overlapping phase counters into a fake wall decomposition. |
| **Boot/recovery turnaround** | Stop/boot-to-ready, first successful response, first-use warm-up and pressure recovery separately from steady-state turns. | A faster boot-fill phase is not a warm PP/TG improvement; no opaque automatic warm-up traffic. |

Persist a finite corpus/trace manifest before execution: existing fresh 16k/80k shapes; matched incremental fixtures reflecting the journal's long-base/small-new-input class; isolated conversation switches; first-use versus declared warm state. Add near-capacity or pressure cases only within an authorized resource-safe window. Use public/synthetic pinned inputs, not investigative replay of live user sessions. Freeze output/request parameters, generated trace where valid, warm-up exclusion, repeats and predeclared steady-state tail rule.

Historical 18cq observations (large contexts, small new reads, prompt-dominated TTFT, small queue time) motivate prioritizing exposed read/restore cost; they do not establish the same phase shares for every workload today. Collect a current per-request timeline before ranking turnaround bottlenecks.

### Targeted turnaround actions retained from earlier work

| Item | Current disposition / condition | Acceptance and rollback |
|---|---|---|
| **T1: cache/restore recovery** | RAM source repair is ready; complete §1 pressure/skip/restart gate. Conversation files already moved to separate storage (18cq), adopted for reduced contention, not a PP throughput win. | Same logical state and expected deepest published checkpoint; safe refusal below floor; account for miss/re-read latency and disk/host cost. Keep original production rollback intact. |
| **T2: checkpoint critical path** (old roadmap B4) | Audit current snapshot/device-sync/copy/write timing first; do not assume the old source path or original stall mechanism is unchanged after RAM repair. Async/deferred capture is a future design only if a real exposed stall remains. | Any future design must preserve coherent state, ordering, buffer lifetime, durable publication and restore equivalence. Never publish a partial snapshot or reuse mutable state early. Structural ownership proof plus failure/restart tests; otherwise blocked. |
| **T3: server continuation / host gaps** | Attribute existing continuation, acceptance/draft, serialization, park/admission gaps. Optimize redundant work only after identifying a measured cost. No new speculative server redesign is pre-approved. | Preserve request semantics, reasoning/output policy, stop/cancel behavior and visible/durable state. Require both whole-response gain and the relevant PP/TG guards. Revert isolated change on failure. |
| **T4: cold-start hot-set / file-cache cost** | Profile freshness and relief-band changes are P2a's bounded analysis, not a license to increase helper fill or launch broad warm-up loops. Earlier fill-mib increase bought only 26 slots and was reverted. | Separate deliberate warm-up cost from time saved, include desktop coexistence and pressure/refault behavior. No benefit may be claimed only after unreported extra traffic. |
| **T5: boot-only landed facilities** | F6 queued/parallel fills and F7ab read/sort code are landed default-off; neither is a new implementation task. F7ab is inactive on the current arena-sourced boot. Reconsider only with new engaged-path and material boot/recovery evidence. | F6 needs correct per-thread device scope/staging lifetime and all-slots `STRATA_VERIFY_COMPLEMENT` equality, not a one-slot spot check. Same bytes into the same slots; measure end-to-end boot as well as H2D phase. |

## 6. Dependency order and execution ownership

1. **Complete: integrated read-only review.** Oracle explicitly approved the whole artifact at `eab11571`, including older PP and turnaround work, with no blockers. This is a separate verdict, not the TG-only approval extended by implication. Remaining candidate and live-validation gates are unchanged.
2. **Later, only when authorized: offline preparation.** Inspect native trace-tool ports, test hardware requirements, PP/TG timing ownership, eligibility and resource math. Prepare narrow default-off code/instrumentation and real tests only within the approved implementation scope. G0/G1 and P0 share a control/attribution foundation; they are not competing independent GPU sweeps. Build modestly with bounded jobs and the repository's pinned toolchain; ROCm 6.1.0 runtime/linkage for gfx906, never ROCm 7.x. All touched backend builds need explicit evidence or an explicit unresolved gate.
3. **RAM acceptance PARKED by the operator on 2026-11-19.** The pressure-turn, skip-count and deepest-published-checkpoint requirements remain unfulfilled; ordinary throughput tests do not satisfy them. Do not resume their preparation or execution without a new request, and do not claim cache/recovery readiness. This gate no longer blocks the separately scoped speed work.
4. **Current priority: establish a matched speed baseline and timelines.** Reuse existing benchmark facilities with frozen source/config/effective env/corpus, same-build A/A controls, per-boot identities, current memory and warm-state evidence. Run PP timing and TG attribution controls before numerical candidates. Do not silently inherit historical warm bands or refresh a baseline to hide a regression.
5. **Pick one measured bottleneck.** PP P1/P3, TG G2/G3 or turnaround T2/T3 only if eligible; P2 capacity work first needs safe budget/placement proof. Predeclare hypothesis, falsification, changed-path evidence, quality test, performance metric and rollback. A null result closes a lane; no requirement to manufacture an adoption.
6. **Reconcile then validate once.** Focused author checks first; final integrated checks only after writers finish. No benchmarking beside agent inference, local builds, analysis or another engine/helper pair. Gate passing applies only to the exact source/config tested; later edits require proportionate revalidation.
7. **Report, restore, then ask about adoption.** Report PP/TG, TTFT/whole-response, cache/recovery, memory, failures and all exclusions together. Restore canonical v0238 on success/failure. Promotion/default flips remain operator decisions even after a whole-plan approval and successful experiments.

## 7. Common proof, performance and safety gates

| Gate | Required evidence |
|---|---|
| **No quality sacrifice** | Pinned reference and full expected corpus/row coverage; exact relevant logits/bytes/operations or credible unchanged-arithmetic-path proof with engaged tests. Retain teacher-forced KL 0.00000, no NaN, top-1/PPL checks as diagnostics, not a relaxed tolerance. Repeat the established three clean passes where applicable. Same-build quality nondeterminism is an investigation/blocker, not permission to accept candidate drift. |
| **Non-vacuous tests** | Correct config `exe`, running binary SHA, effective env, actual kernel/feature/counter engagement, complete outputs and expected rows; test identities plus exact commands/exit codes/reports. Instrumented/forced/measure paths must not bypass the operation being certified. Host-only tests cannot certify GPU kernels; registered/skipped tests are not passed tests. |
| **Measured benefit** | Matched/bracketed controls and predeclared interleaving, same workload and effective env/warm-state conditions. Publish all samples, medians/spread and baseline drift; no claimed gain inside control variability. Retain slow results. Report whole-response and PP/TG separately; no unreported output shortening or caching advantage. |
| **No hidden regression** | PP, cached/main and whole-response TG, turnaround, failure rates and declared workload classes checked against the current control. Any material cross-metric trade-off requires an explicit operator decision, not automatic adoption. Do not claim tail-percentile improvement from three samples. |
| **Resource budget** | Engine RSS/high-water versus unique RAM clearly labeled; cap-adjusted room and host MemAvailable distinct; swap/refault/I/O, per-card VRAM/peak lifetimes, desktop coexistence, file-cache and restore-headroom costs recorded. No capacity/precision sacrifice and no speculative growth into low-VRAM warnings. |
| **Liveness/state** | Finite boot/request deadlines; exact failures retained. A timed-out client does not cancel server work: abort/controlled stop rather than overlapping a retry. Preserve stop/cancel/checkpoint/restore contracts; partial/failed legs cannot count as complete. A hard kernel/SMU hang is an operator incident, not authorization for GPU/OS resets. |
| **Serving safety** | Only parent process control through inspected on-disk stop scripts. Clean stop exit **and** ALL STOPPED before booting another pair. Canonical restoration through `launch-v0238.sh`, fresh ready line, expected live executable/SHA and bounded real nonempty finite completion; never silently report restoration from a stale log. |
| **Artifact isolation** | New run-specific outputs/cache directories, immutable corpus/manifest pins and preserved previous records. CPU fixtures use owned project-local scratch; no cleanup of real benchmark data. Verify before/after evidence when exercising the harness. Stream live NUL-stripped panel/engine output and report each arm as it completes. |

Before any authorized window, freeze the runnable manifest with exact commands, ports/sockets, binary/helper/server hashes, source, quality reference, model/pack/tokenizer identities, resource settings, workload/repeat policy, deadlines and restore command. The old `lane_restore.sh` restores v0233 and is **not** the canonical restore path; use the corrected champion-restore logic or a reviewed run-specific equivalent, not legacy defaults. Keep production cache inventory and existing measurements untouched.

## 8. Prior-work disposition and supersession map

| Earlier item | Current treatment |
|---|---|
| MMQ-off, split/DFlash/foresight configuration, MMVF, stage affinity, separate conversation-cache storage | Adopted baseline facts; not new candidate gains. Preserve unless an explicit separately reviewed change is authorized. |
| Prefill warm + depth 128 | Combined regression, reverted (18cq); depth alone was not independently tested. No blind repeat. |
| PLE mmap / row-cache, CPU KV, queue priming, IQ thresholds and A2 load-order switches | Prior no-adoption/null or ineligible work; no fresh sweep without a changed premise. |
| F1 / F8 | Landed default-off, request-dependent. F1's old gate was vacuous; current G2 requires real eligibility/RNG/path proof. F8 is not a benefit on neutral-penalty requests; no unpriced enabling bundle. |
| F2 / F3 | F2 performance-null and off (25.65 versus 25.55 tok/s); repaired parity is not a speed win. F3 not landed; ring dependency has no demonstrated sequential benefit. Concurrency is untested, not a current lane. |
| F4 / F5 | Not landed; only retained conditional designs, not scheduled implementation. F4 requires measured exposed residual cost, immutable-byte keys and nonblocking demand/protocol/ack ownership. F5 may never use the rejected early CPU-rows-ready flag raise; any future gather overlap must join before the unchanged readiness signal. |
| F6 / F7ab / F7c | F6/F7ab landed off, boot/source-dependent. F7c landed but helper timing wiring remains incomplete; G1 owns that attribution. Do not reimplement landed features. |
| HC_Q8 swap/fusion, different quant formats, FP16-KV/context cuts, acceptance chasing/adaptive policies | Closed, rejected or outside the zero-quality/capacity bar. No relabeling as a new opportunity. |
| `STRATA_HIP_PROMPT_F16=1` audit | Existing reference behavior, not a newly approved precision choice. Do not silently toggle it or redefine the quality baseline; any correctness audit is distinct from claiming a speed win. |
| Old exact cross-device expert contract / broad wave64 audit | Conditional research, not an approved rewrite or kernel sweep. G0 controls nondeterminism; G3 is the narrow attention lead. Any broader arithmetic change needs its own measured case and exact-equivalence route. |
| Old fixed first-week schedule, GPU-hour/gain estimates, 24 tok/s floor, "at least one adoption" target | Retired as current commitments. Use measured controls, actual artifact evidence and valid null-result closure. No new time estimate is made here. |

## 9. Evidence and approval record

- [Three-champion results](CHAMPIONS-RESULTS-20261118.md), frozen manifest/summaries under `../out/champions-20261118/`, and `final-evidence.sha256`.
- [TG chapter](tg-optimization-chapter.md) and [its independent component review](tg-optimization-review.md): approved research component at `d649e5b`, verdict recorded in `97ea0b0`, not this whole plan's approval.
- [Integrated oracle review](serving-plan-integrated-review.md): explicit whole-plan APPROVE, no blockers, for the complete substantive artifact at `eab11571`; subsequent changes here record approval status only.
- [Earlier PP program](speed-plan-v2.md): source of P0–P4; its historical inferred GPU bucket, contract-only quality shortcut and stale thresholds do not override this file.
- `../../notes/perf-roadmap-2026-11-15.md`, `../../notes/code-opt-analysis.md`, `../../notes/code-opt-plan.md` and `../../notes/code-opt-plan-review.md`: historical candidate/design lineage, amended designs and old component approvals. Their stale pending/adopted labels are reconciled above.
- `/mnt/srcds/Strata/journal/serving-tg-reeval-split-retest-2026-11-11.md`: adoption/rejection history through the comparison and TG review; engine RAM evidence paths in §1.

**Integrated oracle verdict: APPROVE — whole-plan research readiness only, no blockers.** Candidate correctness/performance, live RAM pressure/restart acceptance and execution/promotion authority remain separate. SSH signing/publication is an administrative blocker, not permission to skip source, quality or execution gates.
