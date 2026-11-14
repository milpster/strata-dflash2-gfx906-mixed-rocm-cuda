#!/usr/bin/env bash
# reserve_arm.sh N — set --vram-reserve-mib N in prod config, commit, boot prod, fill+TG legs, banners+judges.
set -u
VAL=$1
TAG=RSV$VAL
R=/mnt/srcds/Strata
RPC=$R/bench/ab-v0210/rpcprof
OUT=$RPC/out/gpu-rounds-20261114
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
CFG=$R/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json

python3 - "$VAL" "$CFG" << 'EOF'
import json, sys
p = sys.argv[2]
c = json.load(open(p))
a = c['args']
for i, f in enumerate(a):
    if f == '--vram-reserve-mib': a[i+1] = sys.argv[1]
json.dump(c, open(p, 'w'), indent=2)
print('vram-reserve-mib:', [a[i+1] for i, f in enumerate(a) if f == '--vram-reserve-mib'],
      '| layer_split:', c['layer_split'], '| mmq:', c.get('env', {}).get('STRATA_PREFILL_MMQ', 'unset'),
      '| fs:', c.get('env', {}).get('STRATA_FS_SLOTS', 'unset'), flush=True)
EOF
cd $R && git add "$CFG" && git commit -q -m "bench(prod): reserve arm $VAL"
bash $R/stop-all.sh >/dev/null 2>&1
sleep 4
cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-ud-xxs-$TAG.log 2>&1 < /dev/null &
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
echo "=== $TAG ==="
tr -d '\0' < $ELOG | grep -a 'expert cache auto' | tail -2
tr -d '\0' < $ELOG | grep -a 'expert cache [0-9]* slots' | tail -2
tr -d '\0' < $ELOG | grep -ai 'shrinking\|LOW\|reserve' | tail -3
rocm-smi --showmeminfo vram --csv 2>/dev/null | sed -n '2,3p'
H=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*helper.log | head -1)
echo "=== FILL 80k ==="
python3 $RPC/ab9_driver.py --arm $TAG --leg fill80k --kind prefill --ctx 442400 --out $OUT/$TAG-fill80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-110
echo "=== TG x2 ==="
python3 $RPC/ab9_driver.py --arm $TAG --leg tg80k --kind tg --ctx 442400 --max-tokens 1024 --out $OUT/$TAG-tg80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-110
python3 $RPC/ab9_driver.py --arm $TAG --leg tg80k --kind tg --ctx 442400 --max-tokens 1024 --out $OUT/$TAG-tg80k-2.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-110
echo "=== JUDGES ==="
tr -d '\0' < "$ELOG" | grep -aE 'tokens = ' | tail -3 | cut -c1-180
echo "=== ARM $TAG COMPLETE ==="
