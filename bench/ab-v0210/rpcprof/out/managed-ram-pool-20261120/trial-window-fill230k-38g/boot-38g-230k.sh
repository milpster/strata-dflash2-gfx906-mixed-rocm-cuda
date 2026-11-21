#!/usr/bin/env bash
# boot-38g-230k.sh — rerun of the 230k fill + 10k generation after the 40 GiB run died
# on CUDA0 VRAM at 7,518 generated tokens (2026-11-21).
# Delta against that run: managed host budget 45,056 -> 43,008 MiB (pool 40 -> 38 GiB),
# CUDA0 margin 256 (config), thinking budget 2,000 (config), fresh cold cache dir.
# Helper reserve 0 (component clamps to 64), fill 7400 MiB, slots 3700 (the launcher's
# --slots is inert in this helper build; the engine's --expert-helper-slots 4600 bounds
# the fill). Env is fixed here so the boot is reproducible; launcher and config untouched.
set -euo pipefail
ROOT=/mnt/srcds/Strata
export STRATA_PORT=8009
export STRATA_RAM_BUDGET_MIB=43008
export STRATA_HELPER_RESERVE=0
export STRATA_FILL_MIB=7400
export STRATA_HELPER_SLOTS=3700
export TRIAL_CONFIG="$ROOT/bench/ab-v0210/rpcprof/trial-managed-pool-38g-230k.json"
exec bash "$ROOT/bench/ab-v0210/rpcprof/launch-trial-managed.sh" --single-run
