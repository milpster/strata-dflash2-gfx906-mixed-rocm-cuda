#!/usr/bin/env bash
# swap_pin.sh — CORRECT order: stop prod, swap pin binary, relaunch, verify identity line, legs.
set -u
R=/mnt/srcds/Strata
WT=/mnt/srcds/Strata-wt-integ
RPC=$R/bench/ab-v0210/rpcprof
OUT=$RPC/out/gpu-rounds-20261114
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
PIN=$R/v0233/build-mixed-gpu/rpcw-hip/strata
NEW=$WT/build-mixed-gpu/rpcw-hip-v0233pin/strata

bash $R/stop-all.sh >/dev/null 2>&1
sleep 4
pgrep -f 'rpcw-hip/strata' >/dev/null && { echo "engine still alive - aborting"; exit 1; }
cp "$PIN" "$R/v0233/build-mixed-gpu/rpcw-hip/strata.bak-f094ddcb" 2>/dev/null
cp "$NEW" "$PIN" && sha256sum "$PIN" | cut -c1-16 > $R/v0233/build-mixed-gpu/rpcw-hip/strata.pin-sha16
echo "pin swapped: $(cat $R/v0233/build-mixed-gpu/rpcw-hip/strata.pin-sha16) (was f094ddcb, backup kept)"

cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-ud-xxs-pin-swap.log 2>&1 < /dev/null &
sleep 10
RD=""
for i in $(seq 1 60); do
  S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log | grep -v -- '-helper' | head -1)
  RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
  TS=$(stat -c %Y "$S" 2>/dev/null)
  [ -n "$RD" ] && [ $(( $(date +%s) - TS )) -lt 150 ] && break
  sleep 10
done
echo "READY: ${RD:-TIMEOUT}"
[ -n "$RD" ] || exit 1
echo "=== IDENTITY ==="
tr -d '\0' < $ELOG | grep -a 'prefill mmq:' | tail -1
echo "=== BANNERS ==="
tr -d '\0' < $ELOG | grep -a 'expert cache [0-9]* slots' | tail -2
H=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*helper.log | head -1)
tr -d '\0' < "$H" | grep -a 'filled' | tail -1
echo "=== FILL 80k ==="
python3 $RPC/ab9_driver.py --arm PIN2 --leg fill80k --kind prefill --ctx 442400 --out $OUT/PIN2-fill80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-110
echo "=== TG x2 ==="
python3 $RPC/ab9_driver.py --arm PIN2 --leg tg80k --kind tg --ctx 442400 --max-tokens 1024 --out $OUT/PIN2-tg80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-110
python3 $RPC/ab9_driver.py --arm PIN2 --leg tg80k --kind tg --ctx 442400 --max-tokens 1024 --out $OUT/PIN2-tg80k-2.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-110
echo "=== JUDGES ==="
tr -d '\0' < "$ELOG" | grep -aE 'tokens = ' | tail -3 | cut -c1-180
echo "=== PIN SWAP VALIDATION COMPLETE ($(date +%H:%M:%S)) ==="
