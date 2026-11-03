# Mixed-backend numerical measurement

The original relative-error gate is still open. The older CUDA-only harness is not
an AMD reference. No measured GPU accuracy result follows from the builds below.

## Opt-in tools

`STRATA_P2_DIAGNOSTICS=ON` requires a native-expert gfx906 build. It adds
`p2_measure_hip_reference`, linking the same HIP MMQ library as the primary engine.
The CUDA helper is a separate process. Reference backend, ordinal, device name and
architecture are recorded in report notes. No NVIDIA runtime is linked into the
HIP reference process.

The option also enables `STRATA_P2_DUMP_PREFIX` in that build's engine. On the first
prefill chunk it writes `<prefix>.<layer>.bin` for layers32–47. Each file contains
three int64 dimensions, float activations and int32 routed expert IDs. Device copies
and writes are checked. Existing files are refused, not overwritten. Check every
expected file and the capture log; a partial capture is not a valid fixture.

The option is off by default. The legacy single-layer capture is unchanged. The
new hook is compiled out, not merely disabled by an environment variable.

## Validation produced here

On 2026-11-02, `build-mixed-gpu/nmse-reference1/build.log` recorded:

- Full default CUDA/gfx906 builds, followed by the opt-in gfx906 build, with `-j11`.
- Default primary and helper executable hashes identical to the preceding build.
- Six CTests per backend passed, including the real CPU reporting test.
- All52 Python writer/build-boundary/report tests passed in1.66seconds.
- HIP runtime from ROCm6.1 and engine rocBLAS from `~/rocm-gfx906-xnack`.
- Source pins and all four protected executable hashes matched after testing.

The first CPU invocation selected an old reporting binary automatically and raised
fixture errors. That failed log is retained. The passing runs explicitly selected
the real CMake CPU binary with `STRATA_P2_MEASURE_TEST`.

`tools/mixed_gpu/build_p2_diagnostics.sh` is the bounded local build recipe. It uses
the named unprotected validation trees; it never deletes a tree or fetches sources.
Its saved manifests and build log are local evidence, not a portable installer.

## Remaining physical evidence

Use fresh real inputs from at least eight fixtures and all layers32–47. Name the
reference backend and preserve every refusal and coverage gap. Run the strict-v2
auditor before interpreting rows. Keep relative p99.9≤0.005 **and** max≤0.02. Report
absolute error and sum-energy tensor NMSE separately; no NMSE threshold is defined.
MMQ product agreement does not establish complete task-output equivalence.

Do not use the legacy measurement scripts unchanged: their process cleanup and
production restoration do not meet the current supervised canonical contract.

## Measured result — 2026-11-02 (fin1, the first fresh same-input reference run)

Real inputs: the 8 pinned fixtures' first-chunk dumps, all layers 32-47 (128 files,
sha256-pinned). Reference: gfx906 HIP on a Radeon VII; candidate: the CUDA helper
on the RTX 3080. Strict-v2 audit passed (rc 0). 125,350 of 125,350 rows measured,
0 skipped, 8/8 fixtures. Against the rule above: relative p99.9 = 0.00599 FAILS
(> 0.005), relative max = 0.01348 passes (<= 0.02). Separately: absolute p99.9 =
0.00120, absolute max = 0.00254, tensor NMSE = 4.228e-06 (energy error 2.342 /
reference energy 554,050). Coverage: rows only in layers 32-35 — the helper held
1,596 of 7,198 requested expert pairs — and 3 of 7 format pairs; both gaps stay
visible in the report notes. Evidence: `build-mixed-gpu/p2-finish/fin1/`
(measure.json sha256 8557a2e6..., audit.log, per-attempt att1-3 preserved).
MMQ product agreement still does not establish task-output equivalence.

## Measured result — 2026-11-02 (layers 36-47 completed, pooled 32-47 verdict)

The user approved completing the 36-47 coverage with three 4-layer windows over the same
fin1 capture (no recapture; the IQ3_S capacity arm was dropped). Each window ran the same
supervised outage → helper → HIP reference → strict-v2 audit → restore contract, each with
its own report, logs and result file, each restored and smoke-verified before the next:

- W1 36-39: 128,551 rows, 0 skipped, 8/8 fixtures, audit rc 0 (measure-L36-39.json,
  sha256 73b71b6c...). rel p99.9 0.00627 FAILS, rel max 0.02480 FAILS. Separately: abs
  p99.9 0.00279, abs max 0.01182, NMSE 4.332e-06 (9.581 / 2,211,791).
- W2 40-43: 128,383 rows, 0 skipped, 8/8 fixtures, audit rc 0 (measure-L40-43.json,
  sha256 45d1d99e...). rel p99.9 0.00656 FAILS, rel max 0.01696 passes. Separately: abs
  p99.9 0.00284, abs max 0.00643, NMSE 4.534e-06 (8.607 / 1,898,552).
- W3 44-47: 128,195 rows, 0 skipped, 8/8 fixtures, audit rc 0 (measure-L44-47.json,
  sha256 469cfda6...). rel p99.9 0.00693 FAILS, rel max 0.01805 passes. Separately: abs
  p99.9 0.00693, abs max 0.03142, NMSE 4.298e-06 (11.899 / 2,768,492).

`tools/mixed_gpu/pool_p2_reports.py` (7 CPU tests) pooled the four audited reports with
the audit's own percentile method, row-identity uniqueness across reports, and energies
summed as one document would (510,479 rows, all layers 32-47, 0 ref-zero, 0 nonfinite;
`pooled-L32-47.json`). Pooled against the unchanged rule: relative p99.9 = 0.00648 FAILS
(> 0.005) **and** relative max = 0.02480 FAILS (> 0.02). Separately: absolute p99.9 =
0.00421, absolute max = 0.03142, tensor NMSE = 4.363e-06 (32.430 / 7,432,884).

Verdict: the original relative-error gate FAILS for the completed 32-47 coverage and stays
in force unchanged. The gate is not relaxed; absolute error and NMSE are reported
separately and never substituted. MMQ product agreement still does not establish
task-output equivalence. Evidence: `build-mixed-gpu/p2-finish/fin1/` (per-window reports,
audit logs, results, preflight/verify logs) and `build-mixed-gpu/p2-finish/fin1-pooled.log`.

## Input-quantizer diagnostic — 2026-11-03

Opt-in `--quantizer-only` (`5c4d4bdb`) compared the actual MMQ producer with the
helper's actual host quantizer on the same saved inputs. Each device processed
64 rows from each of 8 fixtures at layers32–47: 128 records, 8,192 row observations.
These prefixes overlap (80 distinct layer/input-hash keys), not 128 independent inputs.

| MMQ device | scales different / 655,360 | integers different / 20,971,520 |
|---|---:|---:|
| Radeon VII, HIP | 655,255 | 23 |
| RTX3080 Laptop GPU, CUDA | 655,255 | 40 |

MMQ keeps float32 scales; the helper wire format rounds them to float16. Rounding
MMQ scales to float16 still leaves 7 differing HIP scales and 22 CUDA scales.
Both MMQ runs repeated bit-identically for every record. This establishes a
format/rounding mismatch, not a quantizer race or a complete cause of changed answers.
No expert products, CPU verification or returned messages were measured here.
Evidence: `build-mixed-gpu/repro-q81-evidence3/q64-{hip,cuda}.json`, matching logs,
input/pack manifests and build log (both builds, CPU CTests and52 Python tests pass).
Input, source and protected executable hashes stayed unchanged. The pooled gate still FAILS.

## Controlled CPU/helper expert computation — 2026-11-03

**Measured cause at the expert boundary: placing a CPU expert on the helper changes
its activation arithmetic.** The CPU uses Q8_K (one float32 scale per 256 values)
for the tested IQ gate/up weights. The helper uses Q8_1 (one float16 scale per 32).
These are different inputs to the same weights, not just different GPU brands.

The opt-in `--expert-only` diagnostic (`13057ef8`) ran the first routed expert for
each of two saved `pinned-sample` tokens at layers 32–47: 32 expert observations
per device. The CPU used `native_quant_act`, `native_gu_rows`, `native_quant_h`
and `native_down_rows`. The GPU used the helper's `native_expert_grouped`.
GGUF types, sizes and input/weight hashes were checked. Devices: Radeon VII (HIP)
and RTX 3080 Laptop GPU (CUDA). No helper/server process or generation ran.

Two interventions held the weights and GPU kernels fixed:

1. Transfer the CPU's Q8_K integers and scales to helper wire blocks, rounding
   only the scales to float16. This reduced hidden and output error in all 32
   observations. Hidden error energy fell 99.74%; output error energy fell 94.82%.
2. Transfer the CPU's exact Q8_0 down input and run only the GPU down projection.
   The remaining output difference was at float32 rounding size.

| Measurement | Normal helper input | CPU input transferred |
|---|---:|---:|
| Largest hidden-row max-relative difference | 0.0192743 | 0.000494652 |
| Largest output-row max-relative difference | 0.0276884 | 0.00446645 |

With the exact CPU down input, the largest output-row max-relative difference
was **1.78199e-7**. These use the existing `max_abs / max(row_amax, eps)` metric;
the columns are maxima over 32 observations, not a new acceptance test.

Every repeated GPU output was bit-identical. Observable v1 and normal grouped
outputs matched bit-for-bit in all 32 observations on each device. Final GPU
expert rows also matched bit-for-bit across HIP and CUDA in all 32 observations.
Thus, in this sample, CPU/helper arithmetic changes the result while switching
between the two GPU backends does not. Matching only input scales to MMQ would
not preserve the CPU reference contract.

Source trace: `src/kernels/cpu/native_expert.cpp:131–151` selects the CPU producer;
`src/core/expert_helper_socket.cpp:98–126` implements the host Q8_1 producer;
`tools/mixed_gpu/sidecar/helper_main.cpp:590–606` runs the grouped helper kernel.
The helper window replaces CPU work; the prompt helper claims before the CPU
share (`src/prefill/prefill.cpp:4003–4017`). Assignment can therefore change the
arithmetic contract. Same-config assignment changes were not traced in this run.

Evidence: `build-mixed-gpu/repro-expert-evidence1/expert2-{cuda,hip}.json`, matching
logs, `expert2-reports.sha256`, input/source/binary manifests and `build.log`.
Both diagnostic builds, both CPU CTests and 52 Python report tests passed.
Protected executable hashes remained unchanged.

Limits: one fixture prefix, single-entry expert computations, not full inference
or a live placement trace. This establishes a causal numerical mechanism, not
that it is the only cause of every changed answer. No production fix was applied.
The pooled numerical gate and frozen-request returned-message validation remain
unpassed. Production remains down with permission; no speed claim or adoption.

## The Q8_K reference kernel: the helper can run the CPU's arithmetic (q8k3)

`build-mixed-gpu/repro-q8k-evidence3`, bounded run 2026-11-03 ~15:00+01, RTX 3080,
the same pinned inputs and weights as expert2 (32 single-entry rows, layers 32-47).

`native_expert_grouped_q8k` (`src/kernels/cuda/iq_kernels.cu`, a diagnostic entry
with no caller in any serving path) consumes ggml's Q8_K activation image for
gate/up (292 bytes per 256 values: one FP32 scale, 256 int8) and keeps the CPU
reference's arithmetic: the exact `bsum` integers (odd scales `2*ls+1`, no
per-sub-block truncation, the trailing power-of-two row factor of quants.c), then
SwiGLU and the down rows on the default path. Measured against the CPU reference
on the same rows as expert2:

| gate/up input | worst output rel, 32 rows |
|---|---|
| helper wire Q8_1 (FP16 scale per 32) | 2.77e-2 |
| CPU integers, scales rounded to FP16 (expert2 `cx`) | 4.47e-3 |
| CPU Q8_K image through the Q8_K kernel (this run) | 1.78e-7 |

On 32/32 rows the output residual equals the matched-down counterfactual's exactly
(the down kernel's summation order, expert2's measured floor); hidden rows are
1.1e-8..2.2e-7 for every gate/up type 17/18/21/22; GPU repeats are bit-identical
(`q8k_repeat_equal` 32/32). The h re-quantization reproduced the CPU's Q8_0
integers on every row (0/32 boundary flips).

Two intermediate runs are retained as the measurement's own controls. `q8k2`
(truncating integers, the CUDA q8_1 forms) left 1e-5..1.1e-4 on types 17/18/22
while type 21 matched at 1e-7: the integers, not the scales. `q8k1` had a
whole-warp shuffle called inside a single-lane branch (garbage output, rel about
1); fixed before any conclusion was drawn.

Limits: single-entry expert rows on one fixture prefix; no complete inference; no
engine, protocol or helper change (the wire still carries Q8_1 - carrying Q8_K
through the prompt and window paths is the next step); no timing claim; the
frozen-request identity gates remain unpassed.

HIP rerun resolved (q8k4, clean tree): the failures were the RUN ENVIRONMENT, not
code - this box's gfx906 runtime needs the canonical env
(`HSA_OVERRIDE_GFX_VERSION=9.0.6`, `HIP_FORCE_P2P=1`, xnack-first
`LD_LIBRARY_PATH`), which `prod-config-swift15-dflash-w4-q4_0-mixed.json`'s `env`
block and the project's own tooling (`capture_window.py`, `finish_window.py`,
`build_l8.sh`) document. Without the override every first kernel launch fails
with `invalid device function`, on the pre-change tree as on HEAD (a from-scratch
rebuild did not change it). With the canonical env the run PASSES and matches the
CUDA arm: worst output rel 1.78e-7 (identical), output q8k == matched-down on
32/32 rows, `q8k_repeat_equal` 32/32, and the output rows are BITWISE equal to
the CUDA arm's 32/32 (same down-input integers, same down kernel); the hidden
rows differ backend-to-backend only in summation order (0/32 bitwise, all within
2.2e-7 of the CPU reference).

Evidence: `build-mixed-gpu/repro-q8k-evidence3/q8k3-cuda.{json,log}` and
`build-mixed-gpu/repro-q8k-evidence4/q8k4-hip.{json,log}`,
`q8k3-hip*.log` (the failed attempts, retained), `q8k3-reports.sha256`,
`q8k4-reports.sha256`, `q8k3-final-audit.log`, `q8k4-final-audit.log`, the
`q8k3-prechecks.log` (in `repro-q8k-evidence1/`), the retained
`repro-q8k-evidence1`/`repro-q8k-evidence2` runs, and the build logs. Both
diagnostic builds (the HIP tree from scratch in evidence4), both CPU CTests and
52 Python report tests passed; input and protected-executable pins unchanged.

## The Q8_K helper wire: built and measured — and the pooled gate's answer (q8k-wire2, q8k1)

The wire is implemented and OFF by default. With `STRATA_EXPERT_HELPER_Q8K=1`
(read at each call, never cached) AND a helper that advertises `kFeatureQ8K`,
the engine sends the CPU's own activation arithmetic to the helper: Q8_K blocks
(one FP32 scale per 256 values, 292 bytes) instead of Q8_1 (one FP16 scale per
32). The window tier uses a new `kBeginQ8K` frame; the prompt tier marks the
image `image_format=2` in the existing `kBeginP` header. A peer without the
feature bit never receives either frame, and with the env unset the default
frames are byte-identical to before. The helper refuses a Q8_K frame for any
gate/up type or row width it cannot serve exactly; the engine then falls back
to its established path. Host tests (`tests/core/expert_helper_socket_test.cpp`,
section 19) pin the CPU-twin quantizer's structure on hand-checked blocks, the
local refusal with a silent wire, the exact `kBeginQ8K` frame, the prompt image
bytes, and the default-off byte identity. Both diagnostic builds, the socket
CTest and the 52 Python tests passed (evidence `build-mixed-gpu/q8k-wire2/`).

The pooled gate did NOT flip. One window was measured with the wire on: layers
32-35, the same fin1 capture and fixtures, strict-v2 audit rc 0, 125,978 of
125,978 rows, 0 skipped. Candidate helper and candidate harness, both
sha-pinned; driver `tools/mixed_gpu/measure_q8k_wire.py`; evidence
`build-mixed-gpu/p2-finish/q8k1/`.

| same window (L32-35, same inputs) | rel p99.9 | rel max | tensor NMSE |
|---|---:|---:|---:|
| stock Q8_1 wire (fin1, measured 2026-11-02) | 0.00599 | 0.01348 | 4.228e-06 |
| Q8_K wire (q8k1, measured 2026-11-03) | 0.02507 | 0.03857 | 1.913e-04 |

The wire fired: full retention with zero refusals is only possible through the
format-2 acceptance path, and the error moved to the Q8_1-vs-Q8_K class, uniform
(~0.021 p99) across all three format pairs and all four layers. Helper-vs-MMQ
agreement became about four times WORSE, not better. The reason is structural.
The pooled gate's reference side is MMQ over MMQ's OWN Q8_1 activation bytes
(float32 scales). A helper faithful to the CPU's Q8_K arithmetic cannot also be
faithful to MMQ, because MMQ's own inputs deviate from the CPU's by the same
class this run measured (0.02507 p99.9 here; expert2 measured 0.0277 for the
mirror direction, CPU reference vs the Q8_1 wire). The gate as written can only
pass on a helper that shares MMQ's input format. Redefining the reference side
is a maintainer decision; the gate stays in force unchanged. The remaining
windows (36-47) and the pooling were NOT run: with this reference they are a
foregone FAIL, not evidence.

Limits: one window of four; no new pooled file (the retained 2026-11-02 pooled
verdict stands); the stock row is the retained fin1 measurement, not a same-day
rerun; no returned-message validation here — the frozen-request identity arms
are the next step; no timing claim. Production remains down with permission.
