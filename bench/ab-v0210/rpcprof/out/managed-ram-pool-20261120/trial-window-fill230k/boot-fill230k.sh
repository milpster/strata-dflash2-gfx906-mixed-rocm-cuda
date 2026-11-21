#!/usr/bin/env bash
# boot-fill230k.sh — long-context stress row of the UD-only managed-pool series
# (2026-11-21): the prelim-winning config, one cold ~230k prefill + 10k generation.
# Pool 40 GiB (budget 45,056 MiB), helper reserve 0 (helper clamps to 64), helper fill
# 7400 MiB with a 3,700-slot cap, device margins 192/192 (in the config), context 262144.
# Env is fixed here so the boot is reproducible; launcher and config are untouched.
set -euo pipefail
ROOT=/mnt/srcds/Strata
export STRATA_PORT=8009
export STRATA_RAM_BUDGET_MIB=45056
export STRATA_HELPER_RESERVE=0
export STRATA_FILL_MIB=7400
export STRATA_HELPER_SLOTS=3700
export TRIAL_CONFIG="$ROOT/bench/ab-v0210/rpcprof/trial-managed-pool-40g-230k.json"
exec bash "$ROOT/bench/ab-v0210/rpcprof/launch-trial-managed.sh" --single-run
