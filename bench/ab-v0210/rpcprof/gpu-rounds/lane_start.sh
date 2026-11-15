#!/usr/bin/env bash
# Boot a variant, or restore canonical production, while streaming its engine log.
set -euo pipefail
R=/mnt/srcds/Strata
LAUNCHER=${1:?variant launcher or restore}
TAG=${2:?tag}
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
START=$(stat -c %s "$ELOG")
echo "=== boot $TAG started $(date --iso-8601=seconds) ==="
# The restore sentinel keeps the canonical launcher's name out of the caller's
# command line, where stop-all's launcher sweep would otherwise match it.
case "$LAUNCHER" in
  restore) bash "$R/bench/ab-v0210/rpcprof/gpu-rounds/lane_restore.sh" & ;;
  *) bash "$R/bench/ab-v0210/rpcprof/gpu-rounds/lane_boot.sh" "$LAUNCHER" "$TAG" & ;;
esac
BOOT=$!
tail --pid="$BOOT" -c "+$((START + 1))" -f "$ELOG" \
  | stdbuf -oL tr -d '\0'
wait "$BOOT"
echo "=== boot $TAG completed $(date --iso-8601=seconds) ==="
