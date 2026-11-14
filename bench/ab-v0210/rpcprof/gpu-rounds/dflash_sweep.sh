#!/usr/bin/env bash
# dflash_sweep.sh — 6 arms: block {5,8} x draft-conf {default,0.35,0.55} on pin v0233.1
# waits for the v0233.1 build, swaps the pin, validation boot = arm 1 (baseline cfg)
R=/mnt/srcds/Strata
RPC=$R/bench/ab-v0210/rpcprof
OUT=$RPC/out/gpu-rounds-20261114
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
CFG=$R/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json
LAUNCH=$R/launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh
PIN_DST=$R/v0233/build-mixed-gpu/rpcw-hip/strata
PIN_NEW=/mnt/srcds/Strata-wt-integ/build-mixed-gpu/rpcw-hip-v0233pin1/strata
set_cfg() {  # set_cfg <block> <conf|-> : conf '-' = remove --draft-conf
  python3 - "$1" "$2" << 'PYEOF'
import json, sys
blk, conf = sys.argv[1], sys.argv[2]
c = json.load(open('/mnt/srcds/Strata/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json'))
i = c['args'].index('--dflash-block'); c['args'][i+1] = blk
if '--draft-conf' in c['args']:
    j = c['args'].index('--draft-conf'); del c['args'][j:j+2]
if conf != '-': c['args'] += ['--draft-conf', conf]
json.dump(c, open('/mnt/srcds/Strata/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json', 'w'), indent=2)
print('cfg: block', blk, 'conf', conf)
PYEOF
}
boot() {  # stop, boot, gate on port; $1 = label
  bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
  ( cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-df-$1.log 2>&1 < /dev/null & )
  for i in $(seq 1 90); do
    S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log 2>/dev/null | grep -v -- '-helper' | head -1)
    RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
    PORT=$(curl -s -m 5 http://127.0.0.1:8009/v1/models 2>/dev/null | head -c 20)
    [ -n "$RD" ] && [ -n "$PORT" ] && { echo "ready $1 ($(date +%T))"; return 0; }
    sleep 10
  done
  echo "BOOT FAIL $1"; return 1
}
legs() {  # legs <arm-tag>
  H=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*helper.log | head -1)
  python3 $RPC/ab9_driver.py --arm "$1" --leg fill80k --kind prefill --ctx 442400 --out $OUT/$1-fill80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
  python3 $RPC/ab9_driver.py --arm "$1" --leg tg80k --kind tg --ctx 442400 --max-tokens 1024 --out $OUT/$1-tg80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
}
echo "=== waiting for v0233.1 build ($(date +%T)) ==="
while ! grep -q 'BUILD PIN COMPLETE' /mnt/srcds/Strata-wt-integ/build-mixed-gpu/rpcw-hip-v0233pin1/../build-pin-v0233-1c.log 2>/dev/null && ! grep -q 'BUILD PIN COMPLETE' $R/logs/build-pin-v0233-1c.log 2>/dev/null; do
  grep -qE 'Error [0-9]' $R/logs/build-pin-v0233-1c.log 2>/dev/null && { echo "BUILD FAILED - ABORT SWEEP"; exit 1; }
  sleep 20
done
echo "=== build ok: swap pin -> v0233.1 ($(date +%T)) ==="
[ -x "$PIN_NEW" ] || { echo "no new pin binary"; exit 1; }
bash $R/stop-all.sh >/dev/null 2>&1; sleep 2
cp "$PIN_DST" "$R/v0233/build-mixed-gpu/rpcw-hip/strata.bak-$(sha256sum "$PIN_DST" | cut -c1-8)" 2>/dev/null
cp "$PIN_NEW" "$PIN_DST" && chmod +x "$PIN_DST"
sha256sum "$PIN_DST" | cut -c1-16 > $R/v0233/build-mixed-gpu/rpcw-hip/strata.pin-sha16
echo "pin swapped: $(cat $R/v0233/build-mixed-gpu/rpcw-hip/strata.pin-sha16)"
set_cfg 5 -
echo "=== ARM DF-B5-Cdef (validation boot, $(date +%T)) ==="
boot B5Cdef && { sleep 3; tr -d '\0' < $(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log | grep -v -- '-helper' | head -1) | grep -aE 'prefill mmq:|driver|pause' | tail -3 | cut -c1-130; legs DF-B5Cdef; }
for arm in 'B5C035 5 0.35' 'B5C055 5 0.55' 'B8Cdef 8 -' 'B8C035 8 0.35' 'B8C055 8 0.55'; do
  set -- $arm; TAG=$1; B=$2; C=$3
  echo "=== ARM DF-$TAG ($(date +%T)) ==="
  set_cfg "$B" "$C"
  boot "$TAG" && legs "DF-$TAG"
done
echo "--- pause/driver lines seen during sweep:"
tr -d '\0' < $ELOG | grep -aiE 'driver.*paus|paus.*driver|gpu paus' | tail -5 | cut -c1-150
echo "=== DFLASH SWEEP COMPLETE ($(date +%T)) ==="
