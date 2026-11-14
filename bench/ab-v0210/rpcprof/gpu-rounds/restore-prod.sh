#!/usr/bin/env bash
# Restore prod on the best-measured config after a GPU window.
# Default since 2026-11-14 evening: ud-iq3_xxs relief champion (TG warm-80k 29.1, PP fresh-80k 516.3).
# Modes: xxs (default) | iq3s (IQ3_S relief restore from round-1 backups) | residency (IQ3_S full, experimental)
set -euo pipefail
STRATA=/mnt/srcds/Strata
OUT="$STRATA/bench/ab-v0210/rpcprof/out/gpu-rounds-20261114"
MODE="${1:-xxs}"
echo "restore mode: $MODE"
bash "$STRATA/stop-all.sh" 2>&1 | tail -1
case "$MODE" in
  xxs)
    setsid nohup "$STRATA/launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh" \
      > "$STRATA/logs/prod-restore-xxs-relief.log" 2>&1 & ;;
  iq3s)
    cp "$OUT/config.pre-residency.bak" "$STRATA/prod-config-swift15-iq3s-int8kv-dflash-w4-mixed.json"
    cp "$OUT/strata.pre-residency.bak" "$STRATA/v0233/build-mixed-gpu/rpcw-hip/strata"
    (cd "$STRATA/v0233/build-mixed-gpu/rpcw-hip" && sha256sum strata > SHA256SUMS.new && \
      grep -v ' strata$' SHA256SUMS >> SHA256SUMS.new 2>/dev/null || true; mv SHA256SUMS.new SHA256SUMS)
    setsid nohup "$STRATA/launch-prod-swift15-iq3s-int8kv-dflash-w4-mixed.sh" \
      > "$STRATA/logs/prod-v0233-restore-relief.log" 2>&1 & ;;
  residency)
    setsid nohup "$STRATA/launch-prod-swift15-iq3s-int8kv-dflash-w4-mixed.sh" \
      > "$STRATA/logs/prod-v0233-restore-residency.log" 2>&1 & ;;
  *) echo "Usage: restore-prod.sh [xxs|iq3s|residency]" >&2; exit 2 ;;
esac
WAIT_FOR='ud-iq3_xxs'; [ "$MODE" != "xxs" ] && WAIT_FOR='swift'
for i in $(seq 1 40); do sleep 15; curl -s -m 3 http://127.0.0.1:8009/v1/models 2>/dev/null | grep -q "$WAIT_FOR" && { echo "UP (~$((i*15))s) on $MODE"; exit 0; }; done
echo "FAILED to come up"; exit 1
