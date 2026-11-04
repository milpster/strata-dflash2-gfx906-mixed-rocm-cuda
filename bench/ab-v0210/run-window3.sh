#!/usr/bin/env bash
# Phase-7 lever window (2026-11-04): prefill dose-response + ring-mult, all as-adopted (tier off).
#  K3 = prefill 24576   K4 = prefill 32768   P1 = prefill 16384 + STRATA_DF_RING_MULT=8 (ring-mult build)
# Usage: run-window3.sh   (one arm at a time; GPUs must be free; continues past arm failures)
set -uo pipefail
B=/home/user/dev/Strata/bench/ab-v0210
for spec in "K3 1" "K4 1" "P1 1"; do
  set -- $spec
  echo "=== $(date +%T) arm $1 round $2 ==="
  if bash "$B/run-arm.sh" "$1" "$2" 3; then
    echo "=== $(date +%T) arm $1 round $2 OK ==="
  else
    echo "!! $(date +%T) arm $1 round $2 FAILED (exit $?) — continuing (policy: rerun+ignore timeout deaths)"
  fi
done
echo "=== $(date +%T) window3 complete ==="
