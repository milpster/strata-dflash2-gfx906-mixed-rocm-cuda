#!/usr/bin/env bash
# wait for the rsv192 arm to complete, then 240k fill stress on its boot
R=/mnt/srcds/Strata
while ! grep -q 'RSV192 CHAIN COMPLETE' $R/logs/queue-rsv192.log 2>/dev/null; do sleep 20; done
sleep 8
bash $R/bench/ab-v0210/rpcprof/gpu-rounds/fill240k.sh > $R/logs/fill240k.log 2>&1
echo "=== F240 CHAIN COMPLETE ($(date +%H:%M:%S)) ==="
