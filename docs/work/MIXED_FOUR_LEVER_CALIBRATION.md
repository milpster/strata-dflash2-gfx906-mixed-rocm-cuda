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

## Stage trim: unchanged retry (`trm2`)

The retry completed all nine requests. Every request read 9,185 fresh prompt
tokens and generated 512 tokens. These are measurements on the same two Radeon
VIIs and RTX 3080; the first failed window above is retained separately.

| Arm | PP rates (tok/s) | TG rates (tok/s) |
| --- | --- | --- |
| Initial canonical | 225.6, 236.5, 235.7 | 34.1, 35.1, 36.8 |
| Stage trim | 255.6, 270.7, 272.2 | 34.4, 36.0, 35.4 |
| Final canonical | 226.0, 236.8, 235.7 | 33.9, 34.9, 36.9 |

Against the pooled controls, median PP rose **235.7 → 270.7 tok/s (+14.8%)**.
Median TG was **35.0 → 35.4 tok/s (+1.1%)**, below the 5% candidate threshold.
Final/initial control ratios were 1.000 for PP and 0.994 for TG.

The initial and final canonical output messages matched exactly for all three
prompts. **All three trimmed outputs differed.** For example, the first reply's
second checklist entry changed from a routing-frequency “counter” to a “profiler”.
This is actual output divergence, not a different response timestamp or ID. It
does not by itself establish a quality loss, but it fails this test's output gate.
The verdict is **OUTPUT_MISMATCH**, not an accepted speed improvement.

The startup residency increase above repeated. All successful samples reported
helper work and drafting, with zero reported helper timeouts or fallbacks. Each
arm's prompt channel recorded 192 requests. No settings were adopted, and no
further trim run is planned in this sweep.

Canonical recovery returned `ready`. A fresh check verified HTTP 200, loaded,
context 262,144, attached supervisor 418189, helper fill, prompt-channel connection,
custom gfx906/ROCm 6.1 library mappings and unchanged pinned files. No old production
process remained. Raw requests, replies and comparator arithmetic were rechecked
against the status file; see `trm2-review1.log` and `trm2-*` in the evidence directory.

## Joint DFlash2 settings (`dfc1`)

All twelve requests completed: 9,185 fresh prompt tokens and 512 output tokens
each, on the same two Radeon VIIs and RTX 3080. Only the joint spec/block/floor
setting changed. Effective draft widths were 4, 3, 6 and 4 across the four arms.

| Arm (spec/block/floor) | PP rates (tok/s) | TG rates (tok/s) | Drafts accepted/offered |
| --- | --- | --- | --- |
| Initial canonical (`5/5/0.5`) | 225.8, 235.4, 234.6 | 34.5, 36.1, 37.6 | 820/1392 |
| `4/3/0.7` | 225.9, 235.6, 235.8 | 34.2, 36.7, 37.1 | 697/898 |
| `7/6/0.3` | 226.2, 235.7, 235.6 | 26.3, 30.4, 29.2 | 914/2937 |
| Final canonical (`5/5/0.5`) | 222.8, 235.7, 235.4 | 34.4, 35.9, 37.7 | 820/1392 |

Pooled control medians were **235.0 PP / 36.0 TG tok/s**. Both candidates'
median PP was 235.6 tok/s (+0.3%). Median TG was **36.7 (+1.9%)** for
`4/3/0.7` and **29.2 (−18.9%)** for `7/6/0.3`. Neither met the 5% timing
candidate threshold. Final/initial control ratios were 1.003 PP and 0.994 TG.

The higher acceptance fraction at `4/3/0.7` (77.6%, against 58.9% for each
control) did not establish a qualifying throughput gain. These are joint
settings, not isolated measurements of draft width or confidence floor.

Canonical output messages repeated exactly for all three prompts. Every
candidate output differed from its matched controls. Both verdicts are
**OUTPUT_MISMATCH**; neither setting is adopted or selected for confirmation.
All samples recorded drafting and helper work, with no reported helper timeouts
or fallbacks. Cache residency and helper fill stayed at the canonical values.

Recovery returned `ready`. Fresh health, fill, prompt-channel, pinned-file and
library checks passed with attached supervisor 1007911 and context 262,144.
No old production process remained. All twelve raw request/reply/metric records,
four startup reports and the comparator arithmetic were rechecked; see
`dfc1-review1.log` and the retained `dfc1-*` evidence.

## Fixture adjustment before the remaining tests

Source review found that `prefill-pipe 1` would leave the original fixture
unchanged: its 993-token final chunk is below the 1,024-token streaming threshold.
The benchmark paragraph count is therefore increased from 280 to 320 before
the prefill and split tests. Their actual prompt lengths must come from engine
summaries, and their comparisons use fresh controls with the same longer inputs.
Earlier requests and results remain unchanged. Do not compare PP rates across
the different fixtures. Context capacity, output length and all gates stay fixed.
The focused 240 CPU checks, syntax checks and four protected hashes passed again
after this adjustment (`perf-pipe-fixture-gate1.log`, 4.69 seconds).

## Prompt-pipeline chunk sizing (`ppc1`)

All twelve requests completed with the revised fixture: **10,465 fresh prompt
tokens and 512 output tokens** each. Both modes engaged. For every candidate
request, the plan log reported a 10,458-token pipeline section in **5,376-token
chunks over two stages**. The 8,192-token prefill capacity stayed fixed.

| Arm | PP rates (tok/s) | TG rates (tok/s) |
| --- | --- | --- |
| Initial canonical | 223.4, 235.5, 234.5 | 33.8, 38.1, 36.3 |
| `--prefill-pipe 1` | 232.1, 243.0, 243.1 | 34.8, 37.3, 35.9 |
| `--prefill-pipe 6144` | 231.1, 215.1, 53.5 | 34.9, 29.2, 34.7 |
| Final canonical | 223.8, 235.4, 229.6 | 33.7, 37.9, 36.4 |

Pooled control medians were **232.05 PP / 36.35 TG tok/s**. Mode `1` gave
**243.0 PP (+4.7%) / 35.9 TG (−1.2%)**. Mode `6144` gave
**215.1 PP (−7.3%) / 34.7 TG (−4.5%)**. Neither met the 5% timing threshold.
Control ratios were 0.979 PP and 1.003 TG.

The 53.5 tok/s PP sample took 195.733 seconds to read its prompt and is retained.
Its cause is not established. Since both modes produced the same chunk geometry
and matched each other's three output messages exactly, their different timings
do not establish a planner-specific effect.

Canonical outputs repeated exactly for all three prompts, but every candidate
output differed from its controls. Both verdicts are **OUTPUT_MISMATCH**. There
is no selected setting or confirmation run. Every sample recorded drafting and
helper activity with zero reported helper timeouts or fallbacks.

Canonical recovery returned `ready`. Fresh health, fill, prompt-channel, library
and pinned-file checks passed with attached supervisor 1747129 and context
262,144; no old production process remained. All twelve raw sample records,
their plan lines, four startup reports and comparator arithmetic were rechecked
in `ppc1-review1.log`. Earlier short-fixture PP results are not controls for this run.
