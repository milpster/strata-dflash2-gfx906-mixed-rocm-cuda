#!/usr/bin/env bash
# Phase-10 profiling window (2026-11-04): decomposition + spec tuning, all as-adopted (16384, tier off).
#  X0 = P0 + STRATA_DF_EVENTS=1 (section table; capture needs the graceful 100s teardown window)
#  S1/S2 = spec-min-p 0.4/0.6   S3/S4 = dflash-block 4/6   P0 r3 = same-window TG anchor
set -uo pipefail
B=${STRATA_DEV_TREE:-/home/user/dev/Strata}/bench/ab-v0210
for spec in "X0 1" "S1 1" "S2 1" "S3 1" "S4 1" "P0 3"; do
  set -- $spec
  echo "=== $(date +%T) arm $1 round $2 ==="
  if bash "$B/run-arm.sh" "$1" "$2" 3; then
    echo "=== $(date +%T) arm $1 round $2 OK ==="
  else
    echo "!! $(date +%T) arm $1 round $2 FAILED (exit $?) — continuing (policy: rerun+ignore)"
  fi
done
echo "=== $(date +%T) window4 complete ==="
