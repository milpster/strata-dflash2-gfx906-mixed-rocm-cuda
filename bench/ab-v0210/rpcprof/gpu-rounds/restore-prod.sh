#!/usr/bin/env bash
# Restore prod on the best-measured config after a GPU window.
# Default: residency (if round1+2 green), else relief+576. Override: restore-prod.sh relief
set -euo pipefail
STRATA=/mnt/srcds/Strata
OUT="$STRATA/bench/ab-v0210/rpcprof/out/gpu-rounds-20261114"
MODE="${1:-auto}"
if [ "$MODE" = "auto" ]; then
  if [ -f "$OUT/round2.ok" ]; then MODE=residency; else MODE=relief; fi
fi
echo "restore mode: $MODE"
bash "$STRATA/stop-all.sh" 2>&1 | tail -1
if [ "$MODE" = "residency" ]; then
  setsid nohup "$STRATA/launch-prod-swift15-iq3s-int8kv-dflash-w4-mixed.sh" \
    > "$STRATA/logs/prod-v0233-restore-residency.log" 2>&1 &
else
  cp "$OUT/config.pre-residency.bak" "$STRATA/prod-config-swift15-iq3s-int8kv-dflash-w4-mixed.json"
  cp "$OUT/strata.pre-residency.bak" "$STRATA/v0233/build-mixed-gpu/rpcw-hip/strata"
  (cd "$STRATA/v0233/build-mixed-gpu/rpcw-hip" && sha256sum strata > SHA256SUMS.new && \
    grep -v ' strata$' SHA256SUMS >> SHA256SUMS.new 2>/dev/null || true; mv SHA256SUMS.new SHA256SUMS)
  setsid nohup "$STRATA/launch-prod-swift15-iq3s-int8kv-dflash-w4-mixed.sh" \
    > "$STRATA/logs/prod-v0233-restore-relief.log" 2>&1 &
fi
for i in $(seq 1 40); do sleep 15; curl -s -m 3 http://127.0.0.1:8009/v1/models 2>/dev/null | grep -q swift && { echo "UP (~$((i*15))s) on $MODE"; exit 0; }; done
echo "FAILED to come up"; exit 1
