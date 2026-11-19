#!/usr/bin/env bash
# ram_raw_launch.sh — operator entry for the FUTURE production-down RAM raw pair window.
#
# THIN LAUNCHER ONLY. The single owner of the pair is the UNSCOPED Python outer `run`
# (gpu-rounds/ram_raw.py): it preflights, launches/waits the expert helper UNSCOPED, then
# launches the engine worker inside the canonical engine memory scope and reads back its result
# JSON, closing the engine (worker finally) before terminating the owned helper. This shell does NOT start, wait for, kill or scope a helper or worker itself,
# so there is exactly ONE owner. It only normalizes the canonical environment and execs `run`.
#
# This script NEVER stops or signals production: a live pair makes the Python outer owner's
# preflight (and the worker's own re-check) refuse. Helper/engine SETTINGS live in the adapter.
set -euo pipefail

RAM_RAW_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PY=${RAM_RAW_PY:-/mnt/srcds/Strata/.venv/bin/python}
MANIFEST=${RAM_RAW_MANIFEST:?set RAM_RAW_MANIFEST to the frozen test manifest}
FIXTURE=${RAM_RAW_FIXTURE:?set RAM_RAW_FIXTURE to the frozen request fixture}
STOP_PROOF=${RAM_RAW_STOP_PROOF:?set RAM_RAW_STOP_PROOF to a stop-all output that reads ALL STOPPED}
[[ -n ${RAM_RAW_TEST_OUT:-} ]] || { echo "REFUSE: set RAM_RAW_TEST_OUT to an isolated dir" >&2; exit 2; }
mkdir -p "$RAM_RAW_TEST_OUT"

# Canonical import-time + engine env serve.server reads BEFORE the worker's lazy import. These
# are the launch-v0238.sh defaults; config.env still overrides per-child via child_env last. The
# helper's CLEAN env (env -i) and the worker's unset/set normalization are applied by the adapter.
export STRATA_ENGINE_READY_S=${STRATA_ENGINE_READY_S:-2400}

# Explicit, single confirmation of the production-down window, then exec the Python outer owner.
# exec means the outer `run` REPLACES this process (no shell-side second owner, no shell trap).
exec "$PY" "$RAM_RAW_DIR/ram_raw.py" run \
  --manifest "$MANIFEST" --fixture "$FIXTURE" --stop-proof "$STOP_PROOF" \
  --confirm-production-down
