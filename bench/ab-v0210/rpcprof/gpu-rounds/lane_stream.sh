#!/usr/bin/env bash
# Stream the live engine log while the unchanged standard judges run.
set -euo pipefail
R=/mnt/srcds/Strata
ARM=${1:?arm}
OUT=${STRATA_LANE_OUT:-$R/bench/ab-v0210/rpcprof/out/gpu-rounds-20261115}
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
START=$(stat -c %s "$ELOG")
mkdir -p "$OUT"
echo "=== judges $ARM started $(date --iso-8601=seconds) ==="
bash "$R/bench/ab-v0210/rpcprof/gpu-rounds/lane_judges.sh" "$ARM" 1 &
JUDGE=$!
tail --pid="$JUDGE" -c "+$((START + 1))" -f "$ELOG" \
  | stdbuf -oL tr -d '\0' | tee "$OUT/$ARM-engine.log"
wait "$JUDGE"
test -f "$OUT/$ARM-fill80k.json/$ARM-fill80k.json"
test -f "$OUT/$ARM-tg80k.json/$ARM-tg80k.json"
echo "=== judges $ARM completed $(date --iso-8601=seconds) ==="
