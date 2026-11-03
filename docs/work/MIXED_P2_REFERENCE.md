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
