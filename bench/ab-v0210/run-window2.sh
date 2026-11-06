#!/usr/bin/env bash
# Phase-7 comparison window (2026-11-04): same-day stock frame vs adopted-K2 prod config.
#  C  = genuine stock v0.2.10 (no helper, no drafter)   P0 = prod config verbatim (prefill 16384, tier off)
# Usage: run-window2.sh   (one arm at a time; GPUs must be free; continues past arm failures)
set -uo pipefail
B=${STRATA_DEV_TREE:-/home/user/dev/Strata}/bench/ab-v0210
for spec in "C 1" "C 2" "P0 1" "P0 2"; do
  set -- $spec
  echo "=== $(date +%T) arm $1 round $2 ==="
  if bash "$B/run-arm.sh" "$1" "$2" 3; then
    echo "=== $(date +%T) arm $1 round $2 OK ==="
  else
    echo "!! $(date +%T) arm $1 round $2 FAILED (exit $?) — continuing (policy: rerun+ignore timeout deaths)"
  fi
done
echo "=== $(date +%T) window2 complete ==="
