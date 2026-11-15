#!/usr/bin/env bash
# Boot an approved variant through lane_boot while streaming its engine log.
set -euo pipefail
R=/mnt/srcds/Strata
LAUNCHER=${1:?variant launcher}
TAG=${2:?tag}
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
START=$(stat -c %s "$ELOG")
echo "=== boot $TAG started $(date --iso-8601=seconds) ==="
bash "$R/bench/ab-v0210/rpcprof/gpu-rounds/lane_boot.sh" "$LAUNCHER" "$TAG" &
BOOT=$!
tail --pid="$BOOT" -c "+$((START + 1))" -f "$ELOG" \
  | stdbuf -oL tr -d '\0'
wait "$BOOT"
echo "=== boot $TAG completed $(date --iso-8601=seconds) ==="
