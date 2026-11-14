#!/usr/bin/env bash
# tg5k_arm.sh — after NIGHTCAP: 100k infill then TG x5120, adaptive (B0 conf 0.55) vs B7
R=/mnt/srcds/Strata
RPC=$R/bench/ab-v0210/rpcprof
OUT=$RPC/out/gpu-rounds-20261114
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
CFG=$R/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json
while ! grep -q 'NIGHTCAP COMPLETE' $R/logs/gpu-nightcap.log 2>/dev/null; do sleep 30; done
set_cfg() {
  python3 - "$1" "$2" << 'PYEOF'
import json, sys
blk, conf = sys.argv[1], sys.argv[2]
p = '/mnt/srcds/Strata/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json'
c = json.load(open(p))
i = c['args'].index('--dflash-block'); c['args'][i+1] = blk
if '--draft-conf' in c['args']:
    j = c['args'].index('--draft-conf'); del c['args'][j:j+2]
if conf != '-': c['args'] += ['--draft-conf', conf]
json.dump(c, open(p, 'w'), indent=2)
print('cfg: block', blk, 'conf', conf)
PYEOF
}
boot() {
  bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
  ( cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-tg5k-$1.log 2>&1 < /dev/null & )
  for i in $(seq 1 90); do
    S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log 2>/dev/null | grep -v -- '-helper' | head -1)
    RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
    PORT=$(curl -s -m 5 http://127.0.0.1:8009/v1/models 2>/dev/null | head -c 20)
    [ -n "$RD" ] && [ -n "$PORT" ] && { echo "ready $1 ($(date +%T))"; return 0; }
    sleep 10
  done
  echo "BOOT FAIL $1"; return 1
}
for arm in 'T5AD 0 0.55' 'T5B7 7 -'; do
  set -- $arm; TAG=$1; B=$2; C=$3
  echo "=== ARM $TAG (block $B conf $C) ($(date +%T)) ==="
  set_cfg "$B" "$C"
  boot "$TAG" || continue
  H=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*helper.log | head -1)
  python3 $RPC/ab9_driver.py --arm "$TAG" --leg fill100k --kind prefill --ctx 550000 --out $OUT/$TAG-fill100k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
  python3 $RPC/ab9_driver.py --arm "$TAG" --leg tg5k --kind tg --ctx 550000 --max-tokens 5120 --out $OUT/$TAG-tg5k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
  echo "--- judges $TAG:"; tr -d '\0' < $ELOG | grep -aE 'tokens = ' | tail -2 | cut -c1-210
done
set_cfg 5 -
echo "=== TG5K ARM COMPLETE ($(date +%T)) ==="
