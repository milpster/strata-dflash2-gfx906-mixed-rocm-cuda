#!/usr/bin/env bash
# wait for quality-night to complete, then reserve 256->192 arm (operator push, +64 MiB)
R=/mnt/srcds/Strata
while ! grep -q 'QUALITY NIGHT COMPLETE' $R/logs/quality-night.log 2>/dev/null; do sleep 20; done
sleep 8
bash $R/bench/ab-v0210/rpcprof/gpu-rounds/reserve_arm.sh 192 > $R/logs/reserve-arm-192.log 2>&1
echo "=== RSV192 CHAIN COMPLETE ($(date +%H:%M:%S)) ==="
