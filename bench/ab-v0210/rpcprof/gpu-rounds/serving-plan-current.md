# Current serving plan — 2026-11-18

## Hard constraints and order

- Zero quality sacrifice. Do not change quantization, precision, context capacity, or model semantics to claim a speed gain. No relaxed KL threshold: use byte-exact evidence or the established teacher-forced KL 0.00000/no-NaN gates, with the changed path actually engaged. Printed zero alone is not proof of bit identity.
- This document supersedes speed-plan-v2.md's relaxed numerical-adoption threshold. That threshold is not authorized.
- Exactly one specialist or GPU measurement lane at a time. The parent waits while a specialist uses the shared inference backend. No benchmark traffic during specialist inference.
- Only the parent stops/starts prod, between specialist turns. Preparation agents must finish before any operation that could interrupt their backend.
- Finish the RAM fix and oracle review, save this plan, compare the three champion stacks, then launch a fresh TG-analysis agent and an iterative independent review of its chapter.
- The comparison lane is authorized; unrelated speculative GPU optimization lanes remain parked.

## 1. RAM restore policy: source approved, live acceptance pending

Engine branch: `conv-cache-ram-guarantee`, based on `42cb41b314`.

| Revision | Outcome |
|---|---|
| `aecd836e68` | Rejected: cold-file advice could stop reclamation before parked host copies were tried. |
| `18e8acaeaa` | Correctness repairs accepted; headroom sizing still required a policy cap. |
| `af1e64a5cb4b617d52c0f9b416af76d28c42bc4f` | Explicit oracle APPROVE for source/CPU readiness and controlled deployment. |

Behavior: staged reclamation advises eligible disk-tier pages once, then consumes eligible parked host copies until admission, exhaustion, unknown telemetry, or a finite bound. No-progress advice is not exhaustion. Guarded donors and their pair sidecars are protected. Logs distinguish advised file bytes from measured availability deltas.

Park snapshots and checkpoint host copies preserve prospective restore headroom. Restores use the unchanged plain safety floor. The band is the smallest applicable term among the file-read cap, cache budget/4, restore checkpoint-chain host geometry, and an absolute 1024 MiB ceiling. Boot logs name the limiter and all terms. It is not an OS reservation: unrelated allocations and session-file writes can still consume the band. Real hard-low conditions still refuse safely.

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

## 2. Three-stack TG/PP comparison

Status 2026-11-18: **completed**, 9/9 pairs per arm. Results, observed runtime-environment differences, memory cost and restore evidence are in `CHAMPIONS-RESULTS-20261118.md`. No build was promoted. The next lane is the fresh TG analysis below, not another optimization sweep.

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

## 3. New TG optimization chapter (fresh analysis agent, then review)

Status 2026-11-18: **complete and oracle-approved for research-plan readiness only**. Settled plan: [TG optimization chapter](tg-optimization-chapter.md); explicit no-blocker verdict and evidence: [independent review](tg-optimization-review.md). No candidate has been validated or enabled. Trace/attribution preparation, implementation, additional GPU traffic and any promotion remain separately gated; unrelated optimization sweeps remain parked.

Read old journals and adoption/rejection evidence first. Build a ledger of what was already tried: helper/cache limits, ring null, MMQ-off win, prefill warm/depth regression, and other relevant earlier rounds. Do not repeat a settled losing experiment without new evidence that changes its premise.

Inspect relevant upstream PRs and commits read-only; record URLs, SHAs, status, backend/model applicability, and whether equivalent work is already in this fork. Upstream claims are leads, not local benchmark evidence.

Use the three-stack results plus actual TG source paths to rank quality-preserving opportunities. Distinguish measured costs from source-backed hypotheses: the earlier regression fit and subtraction-derived per-slot GPU costs are not a profiler decomposition. Each proposal needs a mechanism, prior-work/upstream provenance, potential bottleneck evidence, implementation scope, equivalence gate, benchmark recipe, rollback path, and adoption rule. No quantization/precision/context shortcuts or relaxed quality tolerances.

Send the chapter to an independent oracle; return concrete feedback to the same analyst; repeat until explicit approval. Persist the approved chapter and journal outcome. Analysis/review does not itself authorize implementation or a new GPU optimization sweep.

## 4. Deferred PP work

Keep the existing timing/routed-stream/dequant-side ideas as hypotheses, not new adopted changes. Any later profiling must distinguish device work from stream waits rather than infer critical-path placement solely from overlapping timers. Retain settled no-win/regression evidence and the zero-quality-loss gates above.
