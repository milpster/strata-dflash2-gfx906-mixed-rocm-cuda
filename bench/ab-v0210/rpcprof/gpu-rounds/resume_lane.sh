#!/usr/bin/env bash
# resume_lane.sh — post-reboot resume: prod boot (192 cfg) -> 240k stress -> quality retry
R=/mnt/srcds/Strata
Q=$R/bench/ab-v0210/quality
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
echo "=== 1. BOOT PROD (post-reboot, reserve-192 cfg) $(date +%T) ==="
cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-ud-xxs-post-reboot.log 2>&1 < /dev/null &
RD=""
for i in $(seq 1 90); do
  S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log 2>/dev/null | grep -v -- '-helper' | head -1)
  RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
  TS=$(stat -c %Y "$S" 2>/dev/null)
  [ -n "$RD" ] && [ $(( $(date +%s) - TS )) -lt 300 ] && break
  sleep 10
done
echo "READY: ${RD:-TIMEOUT}"
[ -z "$RD" ] && { echo "BOOT FAILED - STOPPING RESUME"; exit 1; }
echo "=== 2. 240K FILL + TG1024 (resumed on the 192 boot) $(date +%T) ==="
bash $GR/fill240k.sh 2>&1 | tee $R/logs/fill240k.log
echo "=== F240 CHAIN COMPLETE ($(date +%H:%M:%S)) ===" >> $R/logs/queue-f240.log
echo "=== 3. QUALITY RETRY (teacher-forced via launcher-env wrapper) $(date +%T) ==="
bash $Q/quality_retry.sh
echo "=== RESUME LANE COMPLETE ($(date +%T)) ==="
