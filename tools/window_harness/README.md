# window_harness - branch-tracked copy of the ops harness import closure

Source: `/mnt/user/dflash2/ops/` (the parent's live harness, which has no git history). Copied
2026-10-23 onto `upstream-integ-023` so the tooling is durable on the new branch while the old
lane keeps its own state. Source modules ONLY: no result JSON, no live config, no secrets, no
logs, no models, no temp dirs, no caches.

| file | state vs the ops original |
|---|---|
| `window.py` | byte-identical |
| `model_window.py` | byte-identical (carries the `STRATA_WINDOW_CONFIG_DIR` override) |
| `greedy_gate.py` | byte-identical |
| `divergence_window.py` | byte-identical |
| `regression_window.py` | byte-identical |
| `test_regression_helpers.py` | identical except fixture TEMP OUTPUT: scratch dirs are created in `window.OPS` (`/mnt/user/dflash2/ops`, the dir the controllers already write results into) instead of the module's own directory, so running the fixtures never dirties the source tree. Assertions untouched. |

The cloner these files import is NOT duplicated here: `sys.path.insert(0, f"{mw.CLONE}/tools")`
resolves it to `<selected clone>/tools/test_inco_dflash2_integration.py` (the branch copy, with the
same env overrides), so the imported clone-provider and the controller always agree.

## Path contract (opt-in, defaults byte-for-byte unchanged)

| env | default when absent | selects |
|---|---|---|
| `STRATA_WINDOW_CLONE` | `/mnt/user/dflash2/Strata-wt` | engine tree, proxy cwd, `tools/` imports |
| `STRATA_WINDOW_PROTO_BINARY` | `<CLONE>/build-gfx906-dflash2/strata` | the cloned `exe` for every NEW-binary arm |
| `STRATA_WINDOW_PROTO_SERVER` | `<CLONE>/serve/server.py` | the isolated prototype server |
| `STRATA_WINDOW_CONFIG_DIR` | `/mnt/user/dflash2/config` | where configs are written and read |

Overrides must be ABSOLUTE paths - a relative value raises at import, it is never resolved against
a cwd. Never overridable: `PROD_CONFIG`, `PROD_BINARY`/`PROD_EXE` (the explicit OLD production
arm), the production launcher, ports (isolated 8019, production 8009), the log dir, and every
identity/ownership/lineage guard. The isolated-lineage recognition in the cloner requires a config
path under `/mnt/user/dflash2/config/`, so a selected dir must stay inside it (e.g.
`/mnt/user/dflash2/config/integ-023`); anything else is `unknown` = fail-closed refusal.

## Prompt selection (opt-in; defaults byte-for-byte unchanged)

| flag | selects |
|---|---|
| (none) | the ONE default prompt - gate-set position 1 (the existing behavior) |
| `--prompt-set` | the FULL eight-prompt gate set, in order (the existing behavior) |
| `--requests N [N ...]` | SUBSET DIAGNOSTIC: ONLY those original ONE-BASED gate-set positions (1..8), in the order given |

`--requests` is valid ONLY with `--prompt-set` and NEVER with `--reenable`; an empty selection, a
duplicate, `0`, and an out-of-range index are refused BEFORE any snapshot, signal, config read, port
probe or HTTP action. It changes NO control: the same three arms, pin 2000, chunk 512, no borrowing,
adaptation off, park/cache off, min-p 0, and `--max-tokens` stays bounded by the class cap (it can only
lower it). Such a run is labelled **SUBSET DIAGNOSTIC** - never a full eight-prompt correctness pass:
`full_eight_prompt_gate_established` is false, the standard `gates_ok` stays false, and the separate
`subset_diagnostic_ok` covers the strict comparisons of the SELECTED responses plus the runtime gates.
Every plan row and every response records the original full-set index beside the sequential index.

```sh
python3 tools/window_harness/divergence_window.py --prompt-set --requests 8 --max-tokens 64 --equalize-mib 1372   # DRY RUN
python3 tools/test_inco_request_selection.py    # CPU fixtures for the selection (writes nothing)
```

## Run

```sh
cd <clone>
python3 tools/window_harness/<controller>.py --help        # dry run is the default everywhere
python3 tools/test_inco_window_paths.py                    # CPU path fixtures
python3 tools/window_harness/test_regression_helpers.py    # existing 29 helper groups
```

Outputs stay in the project: results in `/mnt/user/dflash2/ops`, engine/server logs in
`/mnt/user/dflash2/logs`, configs in the selected config dir. Nothing here writes into a
production tree, into this repository, or into `/tmp`.
