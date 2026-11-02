# Four mixed-GPU speed candidates

This is a local, opt-in calibration for two Radeon VIIs and an RTX 3080.
It reuses the production engine and helper binaries. It never changes the
production settings or adopts a candidate.

## Scope

Each test starts and ends with the canonical configuration. Each arm gets
three matched, fresh prompts. The request asks for 512 output tokens; fewer
than 384 actual completion tokens fails the sample. Thinking is off and
temperature is zero. Prompt and generation rates are reported separately.

| Test | Candidate settings |
| --- | --- |
| Stage-weight trimming | `STRATA_STAGE_TRIM=1` |
| DFlash2 calibration | spec/block/floor: `4/3/0.7` and `7/6/0.3` |
| Prompt-pipeline chunks | `--prefill-pipe 1` and `6144` |
| Layer placement | Split points `30` and `34`, against `32` |

The model, quantization, 262,144-token context, helper slot cap and fill budget
stay fixed. Candidates are not combined. This small search cannot establish
that a setting is optimal.

## Running on the validation host

Read the current supervisor identity from
`/mnt/user/Strata/run/prod-mixed-supervisor.json`. Use a fresh short family.
Without `--execute`, this only prints the plan and file hashes:

```sh
python3 tools/mixed_gpu/performance_window.py \
  --family trm1 --lever trim --expected-supervisor CURRENT_PID
```

`--execute` interrupts production for the test. It records five-second
heartbeats and raw requests, replies, engine logs, private configurations and
status files under `build-mixed-gpu/cap1-validation/`. On success or failure,
it cleans up the owned processes, then restores the canonical launcher with
`--recover` and checks a completion. It refuses a concurrent production start
if an owned process survives cleanup. Never edit source or harness files
during a window.

Use `--lever dflash`, `pipe` or `split` for the other tests. A one-based
`--candidate N` selects one setting for a fresh bracketing confirmation.

## Reading the evidence

- Startup accounting checks the layer ranges, effective draft width, stage
  trimming, cache totals and helper eligibility/fill. Raw arguments are retained.
- Every sample must show a full fresh read, helper activity, active drafting
  and no reported helper timeout or fallback. Teardown must show prompt-channel work.
- Matching request hashes establish matched inputs only. The runner separately
  compares hashes of the actual returned messages, including their content.
- Changing expert residency or chunk geometry can change rounding. Output
  mismatches and unstable baseline outputs stay visible; they are not quality passes.
- A timing candidate needs at least 5% higher median throughput and final/initial
  control throughput within 0.9–1.1 for that metric. It still needs confirmation.
- Exact returned text is not proof of token-ID or numerical equivalence. The
  retained pooled numerical relative-error gate still fails. Nothing is adopted.

## Validation before hardware runs

On 2026-11-02, the focused CPU checks passed: **240 tests** across
`test_performance_window`, `test_window_pair`, `test_slots_window`,
`test_pair_supervisor` and `test_helper_ab`. Syntax checks and four dry-runs
passed. All four protected binary hashes matched. No backend source changed,
so no backend rebuild was run.

## Stage trim: first attempt (`trm1`)

The run did **not** complete. There is no speed comparison or output-equivalence
result. All three baseline requests generated 512 tokens from fresh 9,185-token
prompts. Their TG rates were **34.3, 4.9 and 35.6 tok/s**; the slow sample is
retained. Prompt rates were **222.2, 235.9 and 223.2 tok/s**.

Trimming engaged at startup. The two Radeon expert caches increased from
6,069/4,780 to **6,800/5,939 slots**: 1,890 more resident experts in total.
The helper filled 4,116 slots instead of 4,093 under the same 6,400 MiB cap.
Disjointness accounting and cross-GPU DFlash tap binding passed the startup checks.
This establishes startup residency only, not usable throughput or model capacity.

The first trimmed request lost its HTTP connection before completing. The helper
log reports `second channel: request failed: helper timeout` after 35 prompt row
requests. The engine reports a closed prompt-helper channel and CPU fallback.
The underlying cause is not established; the failure is not discarded as noise.

Canonical production was restored automatically. A completion returned `ready`,
and a subsequent health check returned HTTP 200, loaded, with context 262,144.
Supervisor 4169738 was attached; helper fill and the prompt channel were restored.
The loaded libraries were the custom gfx906 rocBLAS and ROCm 6.1 libraries. All
four protected hashes matched and no old production process survived cleanup.
Evidence is retained under `build-mixed-gpu/cap1-validation/trm1-*` and the raw
`/mnt/user/Strata/logs/window-trm1-b-*` helper/server logs.

A single fresh-family retry is planned with unchanged settings and timeouts.
