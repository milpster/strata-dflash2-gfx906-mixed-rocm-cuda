# Output identity in the mixed-GPU prefill windows

## Problem and result

Strata's mixed-GPU speed search needs **faster** candidates whose returned
messages are **identical to their controls** on the same request bytes. That gate is
not met by any qualifying speed candidate. These four windows collected cleanly and their arithmetic is
verified, but no accepted production speedup exists: the candidates that read
faster return text that is not identical to the controls, and the windows whose
text matches show no gain. The root cause of the output mismatch is
**unresolved**, and **no further GPU run is authorized** by this report.

## Scope

- Hardware: two AMD Radeon VII cards and one NVIDIA RTX 3080.
- Model: Swift 1.5 (Qwen3.8-Flash-Next, IQ3-XXS quantization), 262,144-token context.
- Production settings for every window start and end unchanged: spec 5, block 5,
  floor 0.5, `--prefill 8192`, layer split 32, context 262,144, `--pcie-frac 0`,
  stage trim unset.
- Each arm runs three matched, fresh prompts asking for 512 output tokens; a
  sample under 384 actual completion tokens fails. Temperature is zero, seed is
  1729 and thinking is off. Acceptance requires identical returned messages,
  at least a 5% PP or TG gain, and control rate drift within 0.9–1.1.
- No engine, kernel, CUDA/HIP/SYCL/Metal, canonical config or launcher, protected binary, or
  library source was changed by this work. **No rebuild is implied.**

## What the three harness commits add (default-off, no engine fixes)

All three touch only `tools/mixed_gpu/*.py` and their tests; they add opt-in
baseline flags to the window harness, each default-off. Without these flags, the
derived engine config, launcher and request bytes stay unchanged:

- `6908d8c7` — iq-mt-min baseline seam (`--iq-mt-min`).
- `8d234618` — adapt-off baseline seam (`--adapt-off`).
- `82e67a00` — pinned prefill replay baselines (`--prompt-f32`, `--replay-family`).

`--prompt-f32` writes `STRATA_HIP_PROMPT_F16=0` into each arm's **private**
engine environment. It **disables the prompt-FP16 GEMM override**; it does not
make the engine run in FP32 and it is not an engine fix. `--replay-family` copies
the exact retained request bytes of an earlier window so a new family reuses
frozen prompt text. The window's read-only reviewer
(`tools/mixed_gpu/review_performance.py`) re-derives the persisted metrics with
the window's **own shared helpers** — it is tamper detection, not an independent
reimplementation.

CPU check at `82e67a00`: 317 focused tests pass
(`build-mixed-gpu/cap1-validation/ppf1-parent-cpu2.log`). No backend source was
touched, so no build applies.

## The four windows (all numbers read from the retained status files)

Control medians are the two bracketing control arms; the candidate median is the
treated arm. `PP` is prompt read rate (tok/s), `TG` is generation rate (tok/s).
Percentages are candidate over control. A run is "stable" when the two controls
produce identical text; "equal" means candidate text equals the controls.

| Family | Baseline flags | Candidate | Ctrl PP/TG | Cand PP/TG | PP / TG | Controls stable | Text equal | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dfc2 | IQ | DFlash 4/3/0.7 | 234.75 / 35.65 | 235.2 / 36.1 | +0.19% / +1.26% | yes | **no** | OUTPUT_MISMATCH |
| dfc2 | IQ | DFlash 7/6/0.3 | 234.75 / 35.65 | 234.6 / 28.3 | −0.06% / −20.62% | yes | **no** | OUTPUT_MISMATCH |
| dfc3 | IQ + adapt-off | DFlash 4/3/0.7 | 232.65 / 34.85 | 235.1 / 35.3 | +1.05% / +1.29% | yes | yes | NO_GAIN |
| dfc3 | IQ + adapt-off | DFlash 7/6/0.3 | 232.65 / 34.85 | 234.9 / 27.8 | +0.97% / −20.23% | yes | yes | NO_GAIN |
| ppc2 | IQ + adapt-off | pipe 1 | 230.25 / 37.25 | 243.2 / 35.3 | +5.62% / −5.23% | yes | **no** | OUTPUT_MISMATCH |
| ppc2 | IQ + adapt-off | pipe 6144 | 230.25 / 37.25 | 242.7 / 35.4 | +5.41% / −4.97% | yes | **no** | OUTPUT_MISMATCH |
| ppf1 | IQ + adapt-off + F16-off | pipe 6144 | 224.45 / 36.3 | 239.1 / 35.9 | +6.53% / −1.10% | **no** | **no** | INCONCLUSIVE_BASELINE_OUTPUT |

The pooled numerical relative-error gate **remains FAIL**. ppf1 drift: PP
0.9987, TG 1.0. Timing columns are timings only; none of them is an accepted
gain, and nothing is adopted.

## What ppf1 actually ran

`ppf1` is a fresh family that replays the exact frozen request bytes of `ppc2`
(rep1 `73c808d5…f1a494`, rep2 `3a2a25e2…d2db`, rep3 `90394092…70bd`; source
status `ac6bc5a7…09dc`), with a bracket `[a control, b pipe 6144, c control]`,
three reps each. Every arm (controls and candidate) ran with
`STRATA_HIP_PROMPT_F16=0`, `STRATA_IQ_MT_MIN=1` and `--adapt-swaps 0`; the helper
held 4093 slots / 6.25 GiB over two channels. Each request read 10,465 prompt
tokens with zero reuse. The candidate's 10,458-token bulk prefill used 5376-token
chunks over two stages; the controls used the default 8192-token chunk size.
The new family is for artifact identity, not a new
prompt.

Matched-request observation (character positions in the returned text, **not**
token ids, and no first-token claim):

- The two controls are text-identical in reps 1 and 3 and differ in rep 2, first
  difference at zero-based character 1135 — so the control baseline is **not** stable.
- Every matched rep, the candidate differs from both controls at zero-based character 25.
- The three reps carry **distinct request bytes**, so comparing outputs across
  reps is **not** a determinism/self-repeat comparison.

In rep 2 the controls drafted 271/501 with 39287 helper rows against 274/532
with 40822 helper rows; noted as an observation, not as a mechanism.

## Integrity versus correctness — kept apart

- **Collected and integrity-checked (passed):** all four windows complete with
  `success=true`; requests/replies/engine segments/private configs/manifests are
  retained; per-arm request hashes match the recorded values; the read-only
  reviewer recompute matches (`ppf1-parent-review1.json`,
  `ppf1-parent-postreview1.json`). `success` means collection finished, **not**
  that a correctness gate passed.
- **Correctness / speed gates (failed or inconclusive):** dfc2 and ppc2 are
  OUTPUT_MISMATCH; dfc3 is NO_GAIN; ppf1 is INCONCLUSIVE_BASELINE_OUTPUT because
  its own controls are not text-stable. Text identity does not clear the pooled
  numerical gate.

## What is **not** claimed

No root cause is established. This report does **not** claim that the IQ route is
unnecessary, that FP16 removal caused the nondeterminism, that near-ties explain
the difference, that a kernel is the cause, that a CPU path is absent, or that
speedup is universally impossible. dfc2 and dfc3 use **different prompts**, so
they are not a clean causal/cost pair. ppc2 and ppf1 share bytes but differ in
chronology and runtime, so F16-off is **not** proven to be the cause.

## Recovery and production state

After every window the canonical launcher runs `--recover`; the parent verified
recovery at 07:25 +01 (ppf1 supervisor 2938724 gen 1, helper 2938785, engine
2940091, HTTP health 200, context loaded 262144, canonical `F16=1`, no IQ, no
adapt-off, 4093 slots / 6.25 GiB with two channels; protected, canonical and
library pins unchanged; no stray windows, ports, or old processes
— `ppf1-parent-postcheck1.log`). This is a point check, not a long-term
stability claim. Production defaults remain as listed above.

## Reproduction

The one performed ppf1 command (historic; the family is spent and **cannot be
re-run** — do not copy this as a fresh run):

```
python3 tools/mixed_gpu/performance_window.py \
  --family ppf1 --lever pipe --candidate 2 \
  --iq-mt-min --adapt-off --prompt-f32 \
  --replay-family ppc2 --expected-supervisor 2734972 --execute
```

A fresh family name and a current supervisor PID are required for any new
window; the production interruption is explicit in `--execute`.

## Evidence

Statuses and per-arm artifacts: `build-mixed-gpu/cap1-validation/{dfc2,dfc3,ppc2,ppf1}-perf-status.json`
plus their request/reply/engine/config/manifest files. Timing table derived by
`output-identity-numbers1.log`; ppf1 hashes and chunk geometry by
`output-identity-msghash1.log`, `output-identity-serve1.log`,
`output-identity-config1.log`. Read-only audit of all four families by
`output-identity-audit1.json` (shared helpers, not independent arithmetic).

## Next

Root cause is unresolved and no extra GPU run is authorized by this report.
Before any further run, existing diagnostics need source-backed validation for
the actual serving path. The proposed CLI/`--window-hashes` replay recipe is
**not** accepted: its serving-path equivalence has not been established.
No speculative flags, kernel changes, or persistent diagnostics are proposed here.
