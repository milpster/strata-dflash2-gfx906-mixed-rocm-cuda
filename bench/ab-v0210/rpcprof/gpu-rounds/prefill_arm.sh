#!/usr/bin/env bash
# prefill_arm.sh — sweep --prefill 8192/16384/32768; fill80k + tg80k per value; back-to-back boots
R=/mnt/srcds/Strata
RPC=$R/bench/ab-v0210/rpcprof
OUT=$RPC/out/gpu-rounds-20261114
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
CFG=$R/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json
for V in 8192 16384 32768; do
  echo "=== ARM PF$V ($(date +%T)) ==="
  python3 -c "
import json
c = json.load(open('$CFG'))
i = c['args'].index('--prefill'); c['args'][i+1] = '$V'
json.dump(c, open('$CFG', 'w'), indent=2)"
  bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
  ( cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-pf-$V.log 2>&1 < /dev/null & )
  RD=""
  for i in $(seq 1 90); do
    S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log 2>/dev/null | grep -v -- '-helper' | head -1)
    RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
    PORT=$(curl -s -m 5 http://127.0.0.1:8009/v1/models 2>/dev/null | head -c 20)
    [ -n "$RD" ] && [ -n "$PORT" ] && break
    RD=""; sleep 10
  done
  echo "ready PF$V: ${RD:-TIMEOUT} ($(date +%T))"
  [ -z "$RD" ] && { echo "BOOT FAIL at $V"; break; }
  H=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*helper.log | head -1)
  python3 $RPC/ab9_driver.py --arm PF$V --leg fill80k --kind prefill --ctx 442400 --out $OUT/PF$V-fill80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-130
  python3 $RPC/ab9_driver.py --arm PF$V --leg tg80k --kind tg --ctx 442400 --max-tokens 1024 --out $OUT/PF$V-tg80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-130
  echo "--- guards PF$V:"; tr -d '\0' < $ELOG | grep -aiE 'shrinking|do not fit|out of memory|timed out' | tail -2 | cut -c1-140
done
echo "=== JUDGES (sweep, last 8) ==="
tr -d '\0' < $ELOG | grep -aE 'tokens = ' | tail -8 | cut -c1-215
echo "=== PREFILL SWEEP COMPLETE ($(date +%T)) ==="
