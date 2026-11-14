#!/usr/bin/env bash
# wait for the FS arm to complete (log marker), then run the reserve-256 arm (operator order; historical fatal value, safety-gated)
R=/mnt/srcds/Strata
while ! grep -q 'COMPLETE' $R/logs/fs-arm-28.log 2>/dev/null; do sleep 20; done
sleep 5
bash $R/bench/ab-v0210/rpcprof/gpu-rounds/reserve_arm.sh 256 > $R/logs/reserve-arm-256.log 2>&1
echo "=== RESERVE256 CHAIN COMPLETE ($(date +%H:%M:%S)) ==="
