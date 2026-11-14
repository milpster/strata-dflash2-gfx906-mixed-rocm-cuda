#!/usr/bin/env bash
# queue_arm36.sh — wait for split_arm.sh 30 to finish, then run split arm 36.
# Launch detached: setsid nohup bash queue_arm36.sh > /mnt/srcds/Strata/logs/queue-arm36.log 2>&1 & disown
R=/mnt/srcds/Strata
SCR=$R/bench/ab-v0210/rpcprof/gpu-rounds/split_arm.sh
while pgrep -f 'split_arm\.sh 30' >/dev/null 2>&1; do sleep 20; done
sleep 5
echo "arm 30 runner gone; starting arm 36 at $(date +%H:%M:%S)"
bash $SCR 36 > $R/logs/split-arm-36.log 2>&1
echo "=== ARM 36 DONE at $(date +%H:%M:%S) ==="
