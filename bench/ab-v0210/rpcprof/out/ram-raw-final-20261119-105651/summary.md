# Raw CLI integration verification — Round 18dy (2026-11-19)

Branch `tg-stall-cause`, HEAD `7663ccfab7e3ee1ce913ec78b8e0a78ebb7ce358` ("fix(bench): connect
single-owner raw CLI"; owned range `6a4b9219ef..7663ccfab7` = add `6e2707850b`, wire `f99b2e534d`,
connect `7663ccfab7`). Scope: `gpu-rounds/ram_raw.py`, `gpu-rounds/ram_raw_launch.sh`,
`tests/test_ram_raw_cpu.py`. Offline CPU only + one read-only production-UP `preflight` probe.
No engine/helper/server/scope was started, signalled or imported; nothing was stopped.

**Verdict: PASS on all static + fake-integration gates, and the live production-UP `preflight`
refuses correctly (exit 2).** Three honest coverage/message observations are listed below; none is
a false-PASS defect, and no source or test was edited.

## Commands and statuses (each executed exactly once)

Test-command cwd: `/mnt/srcds/Strata/bench/ab-v0210/rpcprof`.

| # | Command | Intent | Exit | Evidence |
|---|---------|--------|------|----------|
| 1 | `python3 -m py_compile gpu-rounds/ram_raw.py tests/test_ram_raw_cpu.py` | byte-compile gate on the two owned Python files | **0** | `py_compile.log` |
| 2 | `bash -n gpu-rounds/ram_raw_launch.sh` | shell syntax gate, no execution | **0** | `bash_n.log` |
| 3 | `python3 tests/test_ram_raw_cpu.py` | the 53-test fake-integration suite | **0** | `test_run.log` — `Ran 53 tests in 0.048s` / `OK`, 0 fail / 0 err |
| 4 | `python3 gpu-rounds/ram_raw.py preflight --manifest <diagnostic> --stop-proof <synthetic>` | READ-ONLY probe against live production identity | **2 (expected refusal)** | `preflight_probe.log` |
| 5 | `git diff --check 6a4b9219ef 7663ccfab7` | whitespace gate, owned range only | **0** | `git_checks.log` |
| 6 | `git diff --name-only 6a4b9219ef 7663ccfab7` | scope gate | **0** | `git_checks.log` — 5 files, no config/engine source |
| 7 | `git status --porcelain -- <3 adapter paths>` | worktree drift gate | **0** | `git_checks.log` (empty) |
| 8 | `sha256sum` owned + artifacts | hash gate | **0** | `file_hashes.txt` |

Suite breakdown counted from `test_run.log`: ImportAndCliIsInactive 3, CliPreflight 4,
CliRunOuter 3, CliWorker 6, PlanAndArgv 6, Gates 6, ExactArgv 3, RealFactory 3, RunWorker 11,
LogWindow 1, OwnedPair 6, ThinLauncher 1 = **53**.

LSP diagnostics (Python language server): **clean, no diagnostics** on `ram_raw.py` and
`test_ram_raw_cpu.py`.

## Callgraph confirmation (static, from source)

`preflight` → `_build_from_manifest` → `phase_P_probes` (read-only pgrep sweep, supervisor-state
`os.kill(pid,0)` liveness, isolated-socket path existence) → `preflight_gate` (stop proof + phase-P
absence; no confirm, no import-env) → exit 0/2. Launches and imports nothing.

`run` → `run_gate` (confirm + clean stop proof + `import_time_env_ok` + `production_absent`, all
**raise `AdapterBlocked` before any spawn/import**) → helper factory (`clean_helper_env(os.environ)`)
→ `run_owned` (helper launch → `wait_ready` deadline → `alive()` → scoped `worker_launcher` →
`_own_cleanup` terminate+wait on EVERY path incl. worker crash) → `launch_scoped_worker`
(`scoped_worker_argv(build_worker_command(...))`, bounded wait, `read_worker_result`; missing JSON or
nonzero rc ⇒ FAIL/raise) → exit 0/4.

`run --worker` → `_cli_worker` → `worker_gate` (confirm + proof + import-env + pgrep with the owned
helper and self/ancestor chain excluded + `owned_helper_proof` + both sockets present) → refuse exit
2, else `default_engine_factory` (lazy `server.engine_args` → `_argv_drift` + `exact_argv_problems`
BEFORE the `StrataEngine` constructor that runs `end_orphans`) → `run_worker` (observed argv/exe from
`/proc`, watchdog cancel, `finally: engine.close()`) → `write_worker_result` → exit 0/4.

No unconditional "parent reserved" / `NotImplementedError` raise remains on any of these paths;
the only `# pragma: no cover` marks are the four real-effect defaults
(`_default_popen`, `_default_run`, `_default_self_pids`, `_default_proc_identity`).

Phase-correctness verified: preflight requires the isolated sockets **free**; the worker requires
them **present** as live sockets under `out_dir` — consistent, not contradictory.

## Live production-UP preflight probe (read-only)

Fixture (owned, clearly labelled, NON-RUNNABLE — `exe`/`helper_bin`/`pack`/`experts` point at files
that deliberately do not exist): `diagnostic/manifest.diagnostic.json`, with
`diagnostic/SYNTHETIC-STOP-PROOF.txt` + `.txt.code` sidecar. The stop proof is **SYNTHETIC TEST
INPUT, not a real canonical stop-all record**; it exists only to get past the proof gate and reach
the identity probes. Never used for `run`, `run --worker`, the launcher or any GPU window.

```
CMD: python3 gpu-rounds/ram_raw.py preflight --manifest .../diagnostic/manifest.diagnostic.json \
        --stop-proof .../diagnostic/SYNTHETIC-STOP-PROOF.txt
{"preflight": "REFUSE", "reasons": ["live_serving_processes: production identity present",
                                    "supervisor_alive: production identity present"]}
preflight_exit=2
```

Read-only correlation (adapter prints the probe name, not PIDs; PIDs obtained separately):

| Identity | PID | Observed |
|---|---|---|
| expert helper | 2788015 | `strata-expert-helper-patient … --socket …/prod-iq3xxs-ud-mixed.sock` |
| server | 2788256 | `.venv/bin/python …/v0215/serve/server.py --engine strata --config prod-config-v0238.json --port 8009` |
| engine | 2789182 | `…/rpcw-hip/strata --serve … --layer-split 30 …` |
| supervisor | 2788001 | alive (`/proc/2788001` exists), from the adapter's own probe target |

`preflight_pgrep_identity.txt` line 1 is a self-match of my own `bash -c` wrapper (its command line
contains the pattern text); the 3 rows above are the genuine production identities. So: **the
connected CLI refuses while production is up.** This proves nothing about GPU readiness.

## Regression / assertion review (existing tests, none added or edited)

| Claim | Code | Asserted by | Status |
|---|---|---|---|
| exact owned exe identity, not basename/pid prefix | `owned_helper_proof` `realpath(exe)==realpath(helper_bin)` + start-tick guard (:987-990) | `Gates.test_owned_helper_proof_exact_not_substring`, `CliWorker.test_worker_refuses_on_substring_basename_identity`, `test_worker_refuses_missing_owned_helper_proof` | covered |
| stop proof requires literal `<path>.code`, never implicit 0 | `_load_stop_proof` (:368-375, appends `.code`, never replaces suffix) | `Gates.test_load_stop_proof_requires_sidecar`, `CliPreflight.test_preflight_refuses_without_code_sidecar`, `..._nonzero_exit_code` | covered |
| `pgrep` rc2 / unreadable ⇒ error ⇒ treated PRESENT | `run_pgrep` (:246-251) | `Gates.test_pgrep_rc_semantics`, `test_probe_error_treated_as_present`, `test_empty_probes_block` | covered |
| helper env = `env -i`, not full parent env | `clean_helper_env` (:148-160) | `PlanAndArgv.test_clean_helper_env_is_env_i_not_full_merge` | covered |
| real `engine_args` compared BEFORE the destructive constructor | `default_engine_factory` (:826-833) | `RealFactory.test_resolved_drift_aborts_before_constructor` (`ctor["n"]==0`) | covered |
| observed pid/exe/start/argv read from proc, not reconstructed | `RealEngineHandle` (:789-793), `_default_proc_identity` | `RealFactory.test_valid_build_constructs_once_and_reads_proc_argv`, `..._argv_from_reader_not_engine` | covered |
| this-arm cache-dir value exact (no cross-arm wildcard) | `exact_argv_problems` (:430-441) | `ExactArgv.test_production_cache_value_rejected`, `test_exact_match_ok`, `test_double_serve_rejected` | covered |
| int/None token protocol, required DONE + state hashes, finite ±inf | `run_worker` (:557-571), `_evaluate_worker`, `_finite_problems` | `RunWorker.test_ignores_none_heartbeats`, `test_midstream_eof_incomplete`, `test_missing_state`, `test_nonfinite_metrics`, `test_parse_state_hashes_cases` | covered |
| deadline/cancel + finite bounds + cleanup order | `arm_timer_watchdog`, `run_worker` `finally`, `_own_cleanup` | `RunWorker.test_stalled_no_yield_is_cancelled`, `OwnedPair.test_happy_close_before_terminate`, `test_worker_crash_still_cleans_helper`, `test_helper_wait_timeout_still_returns` | covered |
| result transport nonempty + failure-closed | `launch_scoped_worker` (:916-921), `read_worker_result` | `CliRunOuter.test_run_happy_single_owner_and_result` (positive), `CliWorker.test_worker_happy_raw_ids_and_files` | positive covered |
| single owner (shell thin, scope only on worker) | `ENGINE_SCOPE` (:59), `scoped_worker_argv`, 31-line `exec`-only launcher | `PlanAndArgv.test_prepare_builds_canonical_tuple` (helper unscoped, worker scoped), `ThinLauncher.test_launcher_has_no_second_owner_and_execs_run` | covered |
| global guards on real Popen/run/server import | module `setUpModule` refuses `subprocess.Popen` + `subprocess.run` | `_POPEN_CALLS == []` asserted in the CLI tests; `ImportAndCliIsInactive.test_import_never_loads_server` | covered |

### Observations (honest gaps, not defects — no code was changed)

1. **Refusal message carries the probe name, not PIDs.** `production_absent` reports
   `"live_serving_processes: production identity present"`; the parsed PID list from `run_pgrep` is
   not surfaced. Fail-safe direction (it still refuses), but the parent's expectation of a
   PID-naming message is met only by external correlation. `ram_raw.py:297-298`.
2. **`launch_scoped_worker`'s failure branches are not directly asserted.** Its positive path runs
   inside `CliRunOuter.test_run_happy_single_owner_and_result` (scope wrapper asserted), but "worker
   produced no result JSON" (`:916-917`) and the nonzero-rc ⇒ FAIL downgrade (`:919-920`) have no
   dedicated case. Code is closed-fail; only coverage is missing.
3. **Provenance is recorded, not verified, by this CLI.** `prepare_plan` emits
   `RA.provenance_manifest(supplied_files)` (`:227`) and the test asserts only
   `RA.manifest_is_public`; the raw adapter never calls `RA.verify_provenance` against a frozen
   expectation. `verify_provenance` itself is covered by the unchanged 48-test acceptance suite, so
   the operation exists — freezing and comparing is currently the caller's job.
4. Minor: the tests' fake server does not assert that `serve_dir` (`:1011`, `:1163`) and `cfg["cwd"]`
   (`:833`) are forwarded; the code does forward them, and the real seams (`real_server_import`,
   `real_process_reader`, `_default_*`) are never executed offline by design.

## Limits (unchanged, explicitly not claimed)

- What PASSED here: raw-adapter **code + fake-integration coverage**, and the live production-UP
  `preflight` refusal.
- Still **UNTESTED**: actual factory GPU compatibility, live helper-vs-engine placement, real raw
  state values, and every real seam (`real_server_import`, `real_process_reader`, `_default_popen`,
  `_default_run`, `_default_proc_identity`, `_default_self_pids`).
- Still **OPEN / delegated**: finite cap calibration + physical `MemAvailable`/swap bounds, exact
  published-generation cold restart, same-boot checkpoint fixtures (offline fixture/policy
  preparation remains delegated work, not parent coding).
- The systemd scope is a **declared contract**, not kernel-enforced exclusivity; no lock or numeric
  timer relation was added.
- Floor **2560 MiB** and all quality/context/KV/precision/quant/spec/min-p/DFlash tolerances
  immutable; GPU acceptance **paused**, no promotion; the passive capture is unchanged; production
  untouched (the live supervisor/engine/helper/server PIDs above were only read, never signalled).
