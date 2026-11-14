#!/usr/bin/env bash
# gpu_nightcap.sh — kvcpu arm -> v0233.2 swap+parity -> B7 confirm -> paired B5/B7 PP -> winner stays
R=/mnt/srcds/Strata
RPC=$R/bench/ab-v0210/rpcprof
OUT=$RPC/out/gpu-rounds-20261114
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
CFG=$R/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json
PIN_DST=$R/v0233/build-mixed-gpu/rpcw-hip/strata
PIN_NEW=/mnt/srcds/Strata-wt-integ/build-mixed-gpu/rpcw-hip-v0233pin2/strata

set_block() {
  python3 -c "
import json, sys
c = json.load(open('$CFG'))
i = c['args'].index('--dflash-block'); c['args'][i+1] = sys.argv[1]
json.dump(c, open('$CFG', 'w'), indent=2)
print('block ->', sys.argv[1])" "$1"
}
boot() {
  bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
  ( cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-nc-$1.log 2>&1 < /dev/null & )
  for i in $(seq 1 90); do
    S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log 2>/dev/null | grep -v -- '-helper' | head -1)
    RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
    PORT=$(curl -s -m 5 http://127.0.0.1:8009/v1/models 2>/dev/null | head -c 20)
    [ -n "$RD" ] && [ -n "$PORT" ] && { echo "ready $1 ($(date +%T))"; return 0; }
    sleep 10
  done
  echo "BOOT FAIL $1"; return 1
}
legs() {
  H=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*helper.log | head -1)
  python3 $RPC/ab9_driver.py --arm "$1" --leg fill80k --kind prefill --ctx 442400 --out $OUT/$1-fill80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
  python3 $RPC/ab9_driver.py --arm "$1" --leg tg80k --kind tg --ctx 442400 --max-tokens 1024 --out $OUT/$1-tg80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
  echo "--- judges $1:"; tr -d '\0' < $ELOG | grep -aE 'tokens = ' | tail -2 | cut -c1-200
}

echo "=== 1. KVCPU ARM ($(date +%T)) ==="
bash $GR/kvcpu_arm.sh

echo "=== 2. SWAP PIN -> v0233.2 ($(date +%T)) ==="
bash $R/stop-all.sh >/dev/null 2>&1; sleep 2
cp "$PIN_DST" "$R/v0233/build-mixed-gpu/rpcw-hip/strata.bak-$(sha256sum "$PIN_DST" | cut -c1-8)"
cp "$PIN_NEW" "$PIN_DST" && chmod +x "$PIN_DST"
sha256sum "$PIN_DST" | cut -c1-16 > $R/v0233/build-mixed-gpu/rpcw-hip/strata.pin-sha16
echo "pin: $(cat $R/v0233/build-mixed-gpu/rpcw-hip/strata.pin-sha16)"
set_block 5
echo "=== 3. V0233.2 PARITY (block 5) ($(date +%T)) ==="
boot v232 && legs V232

echo "=== 4. B7 CONFIRM (block 7, on v0233.2) ($(date +%T)) ==="
set_block 7
boot B7b && legs B7b

echo "=== 5. PAIRED B5 PP (block 5 reboot) ($(date +%T)) ==="
set_block 5
boot B5b && legs B5b

echo "=== NIGHTCAP COMPLETE ($(date +%T)) — config at block 5; winner decided from judges ==="
