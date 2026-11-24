# Fault-track harness: running one leg on a different engine binary — 2026-11-24

Why: the amdgpu XDMA permission fault (`journal/amdgpu-userptr-fault-diagnosis-2026-11-24.md`)
needs a leg on a patched engine build without editing the rig pins. The fault harness
(`bench/ab-v0210/rpcprof/faultdiff_run.py`) pinned the engine as path + sha256 and refused
anything else; a substitution now needs to be possible AND impossible to mistake for the rig.

## What was added (fixer lane: code + static checks only, nothing booted)

| piece | where |
|---|---|
| `STRATA_FAULTDIFF_ENGINE` + `STRATA_FAULTDIFF_ENGINE_SHA256`, both or neither | `faultdiff_configs.engine_expected()` |
| pre-boot check + actual launch honour it | `faultdiff_run.load()` (returns the engine) |
| boot visibility | `WARNING:` line before the gates, `engine ... (OVERRIDE ...)` dry-run line, `engine`/`engine_sha256`/`engine_override`/`config_pinned_sha256` in the boot record |
| status / stop / load visibility | `override_words()` + a `WARNING:` line in each |
| config-side check at the exe test | `faultdiff_configs.exe_problems()`, wired into `verify()` |
| dry-run self-test of the contract | `bench/ab-v0210/rpcprof/faultdiff_override_selftest.sh` |

The override reaches the engine through the window's booted config (`out/faultdiff-r1/configs/<leg>.json`),
because `serve/server.py` starts the engine from `cfg["exe"]` (server.py:850,913). The frozen file under
`gen/faultdiff/` is never written, and the manifest pin that the copy no longer matches is recorded beside it
(`config_pinned_sha256`). No pin value, leg config or gate threshold changed.

## Checks run (all dry-run / read-only)

- `faultdiff_configs.py --check`: OK (10 legs, base 0068f4bb0dd38d7b), with and without a valid override.
- `--boot --dry-run` with no env: output byte-identical to `HEAD` for `parking-off`, `default-margins` and
  `xnack-1` (only the live `MemAvailable` reading differed between the two runs).
- `--boot --dry-run` both vars set (patched build `run/port0218-nohuge-bin/rpcw-hip/strata`, sha
  `15d330ea…`): warning + override engine line, exit 0.
- path only -> `REFUSING: STRATA_FAULTDIFF_ENGINE_SHA256 is not set`; sha only -> `REFUSING:
  STRATA_FAULTDIFF_ENGINE is not set`; both with a wrong sha -> refusal naming both shas; nonexistent path
  -> `names no file`. All exit 1.
- booted-config writer: no override -> bytes identical to the frozen file; override -> only `exe` differs,
  frozen file untouched.
- `--status` and `--stop` on a hand-written state fixture in `/tmp/opencode` (not `out/`): both name the
  override; the same fixture without the key reads as the pinned rig, with `--stop` output identical to
  `HEAD`.

## Not run

No boot, no GPU work, no process control, no push. The patched build was hashed, not executed.
