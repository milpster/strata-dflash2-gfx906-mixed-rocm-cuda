#!/usr/bin/env bash
# split_arm.sh N — set layer_split=N in prod config, commit, boot prod, run fill+TG legs, dump banners+judges.
# Gremlin-proof: run via `setsid nohup bash split_arm.sh N > $R/logs/split-arm-N.log 2>&1 & disown`.
set -u
ARM=$1
R=/mnt/srcds/Strata
RPC=$R/bench/ab-v0210/rpcprof
OUT=$RPC/out/gpu-rounds-20261114
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
CFG=$R/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json

python3 - "$ARM" "$CFG" << 'EOF'
import json, sys
p = sys.argv[2]
c = json.load(open(p))
c['layer_split'] = int(sys.argv[1])
json.dump(c, open(p, 'w'), indent=2)
print('layer_split:', c['layer_split'], '| spec:', [c['args'][i+1] for i, f in enumerate(c['args']) if f == '--spec'], flush=True)
EOF
cd $R && git add "$CFG" && git commit -q -m "bench(prod): split arm $ARM"
bash $R/stop-all.sh >/dev/null 2>&1
sleep 4
cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-ud-xxs-split$ARM.log 2>&1 < /dev/null &
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
echo "=== SPLIT$ARM ==="
tr -d '\0' < $ELOG | grep -a 'layer split' | tail -1
tr -d '\0' < $ELOG | grep -a 'expert cache auto' | tail -2
rocm-smi --showmeminfo vram --csv 2>/dev/null | sed -n '2,3p'
H=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*helper.log | head -1)
echo "=== FILL 80k ==="
python3 $RPC/ab9_driver.py --arm S$ARM --leg fill80k --kind prefill --ctx 442400 --out $OUT/S$ARM-fill80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-110
echo "=== TG x2 ==="
python3 $RPC/ab9_driver.py --arm S$ARM --leg tg80k --kind tg --ctx 442400 --max-tokens 1024 --out $OUT/S$ARM-tg80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-110
python3 $RPC/ab9_driver.py --arm S$ARM --leg tg80k --kind tg --ctx 442400 --max-tokens 1024 --out $OUT/S$ARM-tg80k-2.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-110
echo "=== JUDGES ==="
tr -d '\0' < "$ELOG" | grep -aE 'tokens = ' | tail -3 | cut -c1-180
echo "=== ARM $ARM COMPLETE ==="
