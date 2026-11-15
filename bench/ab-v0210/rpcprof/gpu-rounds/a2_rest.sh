#!/usr/bin/env bash
# a2_rest.sh — the remaining 6 A2 arms sequentially (BASE already ran), then prod restore.
# One invocation = the whole tail of the sweep; if the script dies at an arm's ready-transition
# (the known killer, so far mmap-only), salvage that arm's legs via lane_judges.sh and re-run
# this script — a2_arm.sh is idempotent (variant regen + boot + judges).
set -uo pipefail
GR=/mnt/srcds/Strata/bench/ab-v0210/rpcprof/gpu-rounds
for ARM in Q8_PACKED SH_STREAM TSUM LFUSE PLE_BATCH MMVF_ROWS; do
  echo "===== A2 arm $ARM starting ($(date +%T)) ====="
  bash $GR/a2_arm.sh "$ARM" 2>&1 | tr -d '\0'
done
bash $GR/lane_restore.sh 2>&1 | tr -d '\0'
echo "=== A2 REST COMPLETE ($(date +%T)) ==="
