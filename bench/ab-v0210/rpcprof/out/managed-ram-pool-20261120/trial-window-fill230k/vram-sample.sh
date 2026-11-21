#!/usr/bin/env bash
# vram-sample.sh — per-card VRAM free/used for the 40 GiB pool window (2026-11-21).
# Read-only. One CSV row every INTERVAL seconds until TERM'd.
# Columns: ts, nv3080_used_mib, nv3080_free_mib, rvii0_used_mib, rvii1_used_mib
# The RTX 3080 (expert helper, engine's CUDA1) comes from nvidia-smi; the two
# Radeon VIIs (engine CUDA0/CUDA1) from rocm-smi card0/card1.
# Usage: vram-sample.sh <out.csv> [interval_s]
set -u
OUT=$1; INTERVAL=${2:-10}
[ -s "$OUT" ] || echo 'ts,nv3080_used_mib,nv3080_free_mib,rvii0_used_mib,rvii1_used_mib' >> "$OUT"
while true; do
  ts=$(date +%H:%M:%S)
  nv=$(nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
  nvu=${nv%%,*}; nvf=${nv##*,}
  read -r r0 r1 < <(rocm-smi --showmeminfo vram --csv 2>/dev/null \
    | awk -F, '/^card[01],/{gsub(/ /,"",$3); printf "%d ", int($3/1048576)}' )
  echo "$ts,${nvu:-},${nvf:-},${r0:-},${r1:-}" >> "$OUT"
  sleep "$INTERVAL"
done
