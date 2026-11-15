#!/usr/bin/env bash
# mmvf_confirm.sh — MMVF_ROWS confirmation: MMVF boot, champion control, MMVF boot.
# Fresh arm labels per boot (same-arm reruns restore from the conversation disk
# tier and measure nothing); judge-rate class is the metric, shas are not gates.
# Ends with the canonical restore.
set -uo pipefail
R=/mnt/srcds/Strata
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
OUT=$R/bench/ab-v0210/rpcprof/out/gpu-rounds-20261115
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
run_boot () {  # run_boot <launcher> <tag> <arm>
  local LAUNCHER=$1 TAG=$2 ARM=$3
  echo "=== $ARM boot $(date +%T) ==="
  bash $GR/lane_boot.sh "$LAUNCHER" "$TAG" || { echo "$ARM BOOT-FAIL"; return 1; }
  bash $GR/lane_judges.sh "$ARM" 1 > "$OUT/$ARM-judges.log" 2>&1 \
    || { echo "$ARM JUDGES-FAIL"; return 1; }
  tr -d '\0' < "$OUT/$ARM-judges.log" | grep -a 'reused + 5 read' | tail -1 | cut -c1-200
}
run_boot "$R/launch-a2mmvfrows.sh" mmvf2 MMVF2
run_boot "$R/launch-champion.sh" champ2 CTRL2
run_boot "$R/launch-a2mmvfrows.sh" mmvf3 MMVF3
echo "=== MMVF CONFIRM COMPLETE — restoring canonical prod $(date +%T) ==="
bash $GR/lane_restore.sh
