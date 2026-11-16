#!/usr/bin/env bash
# oom-protect-engines.sh — make every running strata engine OOM-kill-immune.
# Root is required: lowering oom_score_adj below the inherited 0 is privileged,
# so user-scope systemd units cannot do this (they reject OOMScoreAdjust=-1000).
# Usage:  sudo bash /mnt/srcds/Strata/bench/ab-v0210/rpcprof/gpu-rounds/oom-protect-engines.sh
# Run it after engines boot; rerun after any engine restart (new pid loses the setting).
set -euo pipefail
found=0
for pid in $(pgrep -x strata-host || true); do
  echo -1000 > /proc/$pid/oom_score_adj
  echo "protected engine pid $pid (oom_score_adj=$(cat /proc/$pid/oom_score_adj))"
  found=1
done
for pid in $(pgrep -x strata-expert-helper || true); do
  echo -1000 > /proc/$pid/oom_score_adj
  echo "protected helper pid $pid"
  found=1
done
[ "$found" = 1 ] || echo "no strata-host/strata-expert-helper processes running right now"
