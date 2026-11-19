# Final offline RAM-helper validation — Round 18du (2026-11-19)

Branch `tg-stall-cause`, HEAD `3090a6f060c2790a0fca1c8387b70deef5ad05b4` (amends `5b939685ea`).
Scope: the offline test-only RAM-acceptance helper and its 48 fixture tests. CPU-only, no engine,
helper, server, network, GPU or production access.

## Commands and statuses (each executed exactly once)

Working directory for the test commands: `/mnt/srcds/Strata/bench/ab-v0210/rpcprof`.

| # | Command | Intent | Exit | Output |
|---|---------|--------|------|--------|
| 1 | `python3 -m py_compile gpu-rounds/ram_acceptance.py tests/test_ram_acceptance_cpu.py` | byte-compile gate: syntax/compile errors in the two owned files | **0** | `py_compile.log` (empty body, no diagnostics) |
| 2 | `python3 tests/test_ram_acceptance_cpu.py` | run the full offline fixture suite | **0** | `test_run.log` — `Ran 48 tests in 0.017s` / `OK`, 0 failures, 0 errors |
| 3 | `git diff --check 5b939685ea^ 3090a6f0` | whitespace gate over the explicit owned commit range only | **0** | `checks.log` (no output) |
| 4 | `git diff --name-only 5b939685ea^ 3090a6f0` | scope gate: which files the owned commits touch | **0** | `checks.log` — exactly the 5 owned files |
| 5 | `git status --porcelain -- <5 owned paths>` | worktree drift gate vs HEAD on owned paths | **0** | `checks.log` (empty → clean) |
| 6 | `sha256sum` on owned files | hash gate against the declared repaired SHAs | **0** | `file_hashes.txt` |

Test-suite breakdown by class, counted from `test_run.log`: Config 18, LogPredicates 9,
ExactRestore 8, DrmGuard 13 = **48**.

LSP diagnostics (Python language server, both files): **clean, no diagnostics**.

## Hashes (match declared values exactly)

- `gpu-rounds/ram_acceptance.py` — `59fe13eb898d08b92d1204996db1c8df9ef4fc135c271a505214553bd925b61c`
- `tests/test_ram_acceptance_cpu.py` — `991641e644d2d03897f9c99a482b6f72c09fe566637a57ef6af8668c328b763d`

Full list (including the plan, fixtures and journal files, and this run's logs) in `file_hashes.txt`.

## Scope confirmation

The owned commit range `5b939685ea^..3090a6f0` touches exactly:

1. `bench/ab-v0210/rpcprof/gpu-rounds/ram_acceptance.py`
2. `bench/ab-v0210/rpcprof/tests/test_ram_acceptance_cpu.py`
3. `bench/ab-v0210/rpcprof/gpu-rounds/ram-parking-gpu-test-plan.md`
4. `bench/ab-v0210/rpcprof/gpu-rounds/ram-parking-gpu-fixtures.md`
5. `journal/serving-tg-reeval-split-retest-2026-11-11.md`

No engine source, no candidate build, no production config. The unrelated live file
`run/prod-swift15-f16kv-mixed-supervisor.json` and all untracked runtime files were left
untouched and unstaged.

## Import-safety / inactive-defaults confirmation (static)

`ram_acceptance.py` imports only `copy`, `re`, `sys`, `dataclasses`, `pathlib` plus the three
reused import-safe siblings (`champions_log.parse_window`, `champions_prepare.sha256_path`,
`watch_gpu_residency.read_fdinfo_clients`). A grep for `serve.server`, `StrataEngine`,
`subprocess`, `socket`, `http`/`urllib` matches only the module docstring's prohibition — no such
import exists, so importing the module starts nothing. `OFFLINE_PREPARATION = True` and every
verdict is reported with `OUTSTANDING_GATES`, so the helper cannot read as a runnable GPU gate.

## Narrow assertion review of the repaired regressions

Read-only; existing tests were used, none added or edited.

| Repaired behaviour | Asserted by | Result |
|---|---|---|
| FALLBACK needs a completed row + zero reuse + positive caller-supplied byte eviction; empty window / plain fresh prompt / zero-eviction miss all FAIL | `test_fallback_requires_real_eviction_evidence` (test:310) vs `classify_turn` (ram_acceptance.py:564-575) | covered |
| Boot limiter-description and plain `RAM guard admitted` are diagnostics only → UNENGAGED; refusal / eviction-to-admit / `RAM guard refused` → PASS | `test_unengaged_pressure_is_never_pass` (:325), `test_pressure_evidence_vs_diagnostics_split` (:288) vs `pressure_engagement` (:588-600) | covered |
| Bare / partial `RestoreExpectation` BLOCKS; non-numeric zero depth BLOCKS; draft class compared as exact full string (numeric prefix `checkpoint at 32` rejected); shallower and deeper depth both FAIL | `test_empty_or_missing_required_expectation_is_blocked` (:367), `test_draft_class_is_exact_not_substring` (:392), `test_shallower_and_wrong_depth_rejected` (:384) vs `evaluate_restore` (:662-691) | covered |
| Every distinct client on each required PCI is checked; any positive eviction ABORTs; missing eviction/residency gauge or absent PCI BLOCKS; duplicate fds collapse to one client with both fds | `test_zero_first_positive_second_aborts` (:462), `test_positive_first_zero_second_aborts` (:468), `test_zero_first_missing_gauge_second_blocks` (:474), `test_missing_*_gauge_blocks` (:485,:490), `test_duplicate_fd_collapse_to_one_client` (:506) vs `gpu_eviction_guard` (:733-757) | covered |
| argv compared as an exact token sequence, only the isolated `--conversation-cache-dir` value slot normalised; single-dash and positional drift FAIL; duplicate protected flags and missing values are problems, not `IndexError` | `test_verify_rejects_single_dash_token_drift` (:158), `..._positional_model_path_drift` (:148), `..._protected_value_change` (:167), `test_verify_rejects_duplicate_floor/cap` (:173,:180), `test_verify_missing_value_is_problem_not_indexerror` (:185) vs `_normalized_args`/`_arg_differences` (:307-335), `_protected_flag_problems` (:275-301) | covered |
| `verify_provenance` fails on sha256 drift, byte-size drift, missing/added role, and an empty frozen expectation (never a vacuous pass) | `test_verify_provenance_passes_and_rejects_drift` (:254) vs `verify_provenance` (:367-392) | covered |

**No defect found.** No source or test file was modified by this pass.

## Static seal

Recorded facts for this pass: HEAD `3090a6f0`; owned files byte-identical to the hashes above
before and after; owned commit range limited to the 5 listed files; whitespace check clean; no
worktree drift on owned paths; suite count 48 with exit 0 and no failures or errors; LSP clean on
both Python files. Explicitly **not** claimed: any GPU, cap-calibration, lifecycle-adapter or
runtime-readiness result.

## Limits (unchanged by this pass)

- Exact raw token-ID/state equality is the settled requirement (HTTP text / state-hash / log-token
  text is NOT equivalent) — the choice is SETTLED, but the faithful raw-transport **lifecycle
  adapter** (`engine_args`/`child_env` + helper/env/scope/affinity, exactly-one-pair, no serving
  loop) remains **unimplemented**.
- Finite calibrated startup-only cap ladder plus physical `MemAvailable`/swap bounds: **not
  established** (parent-owned).
- Exact GPU deepest-PUBLISHED cold-restart and same-boot RAM checkpoint-selection fixtures:
  **not established**. Optional claims about real cap calibration or a staged GPU cache fixture are
  UNTESTED, not defects of this pure helper.
- RAM floor stays **2560 MiB**; no quality, capacity or tolerance change anywhere.
- GPU acceptance remains **paused**; no promotion. Production untouched; the sealed passive
  capture `04544bdd18` and canonical `prod-config-v0238.json` are unaffected.
- Not run (out of scope for this pass): `champions` 53-test suite, collector 15, C++ 6, Bun, and
  the repository root full suite.
