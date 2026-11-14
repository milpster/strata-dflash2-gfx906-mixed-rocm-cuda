#!/usr/bin/env bash
# resume3_lane.sh — rerun after reboots: robust boot gate -> 240k stress -> quality retry
R=/mnt/srcds/Strata
Q=$R/bench/ab-v0210/quality
RPC=$R/bench/ab-v0210/rpcprof
OUT=$RPC/out/gpu-rounds-20261114
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
START=$(date +%s)
echo "=== 1. BOOT PROD (reserve-192 cfg) $(date +%T) ==="
cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-ud-xxs-post-reboot3.log 2>&1 < /dev/null &
RD=""
for i in $(seq 1 120); do
  S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log 2>/dev/null | grep -v -- '-helper' | head -1)
  TS=$(stat -c %Y "$S" 2>/dev/null || echo 0)
  RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
  PORT=$(curl -s -m 5 http://127.0.0.1:8009/v1/models 2>/dev/null | head -c 20)
  if [ -n "$RD" ] && [ -n "$PORT" ] && [ "$TS" -gt "$START" ]; then break; fi
  RD=""
  sleep 10
done
echo "READY: ${RD:-TIMEOUT} port=${PORT:-none} ($(date +%T))"
[ -z "$RD" ] && { echo "BOOT FAILED - STOPPING"; exit 1; }
sleep 5
echo "=== 2. 240K FILL + TG1024 ($(date +%T)) ==="
H=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*helper.log | head -1)
python3 $RPC/ab9_driver.py --arm F240C --leg fill240k --kind prefill --ctx 1327200 --out $OUT/F240C-fill240k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-130
echo "--- loan/oom lines:"
tr -d '\0' < $ELOG | grep -aiE 'borrows|shrinking|do not fit|out of memory|timed out' | tail -4 | cut -c1-150
echo "--- TG 1024 on 240k ctx ($(date +%T)):"
python3 $RPC/ab9_driver.py --arm F240C --leg tg240k --kind tg --ctx 1327200 --max-tokens 1024 --out $OUT/F240C-tg240k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-130
echo "--- judges (240k-context lines):"
tr -d '\0' < $ELOG | grep -aE 'prompt 2[34][0-9]{3} tokens' | tail -3 | cut -c1-210
echo "=== F240C DONE ($(date +%T)) ==="
echo "=== F240 CHAIN COMPLETE ($(date +%H:%M:%S)) ===" >> $R/logs/queue-f240.log
echo "=== 3. QUALITY RETRY ($(date +%T)) ==="
bash $Q/quality_retry.sh
echo "=== RESUME3 LANE COMPLETE ($(date +%T)) ==="
