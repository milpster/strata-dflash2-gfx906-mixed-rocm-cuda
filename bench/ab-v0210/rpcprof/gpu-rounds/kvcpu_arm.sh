#!/usr/bin/env bash
# kvcpu_arm.sh — KV streaming round: --kv-resident 40960 (hot cells on GPU, rest pinned RAM)
# Freed VRAM should surface as extra expert cache slots in the boot banner.
R=/mnt/srcds/Strata
RPC=$R/bench/ab-v0210/rpcprof
OUT=$RPC/out/gpu-rounds-20261114
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
CFG=$R/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json
python3 - << 'PYEOF'
import json
p = '/mnt/srcds/Strata/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json'
c = json.load(open(p))
i = c['args'].index('--kv-resident'); old = c['args'][i+1]; c['args'][i+1] = '40960'
json.dump(c, open(p, 'w'), indent=2)
print(f'kv-resident: {old} -> 40960')
PYEOF
bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
( cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-kvcpu.log 2>&1 < /dev/null & )
for i in $(seq 1 90); do
  S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log 2>/dev/null | grep -v -- '-helper' | head -1)
  RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
  PORT=$(curl -s -m 5 http://127.0.0.1:8009/v1/models 2>/dev/null | head -c 20)
  [ -n "$RD" ] && [ -n "$PORT" ] && break
  sleep 10
done
echo "ready: ${RD:-TIMEOUT} ($(date +%T))"
[ -z "$RD" ] && { echo "BOOT FAIL"; exit 1; }
echo "--- banner (slots + streaming lines):"
tr -d '\0' < $(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log | grep -v -- '-helper' | head -1) | grep -aE 'expert cache|kv|stream|resident' | head -8 | cut -c1-150
H=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*helper.log | head -1)
python3 $RPC/ab9_driver.py --arm KVCPU --leg fill80k --kind prefill --ctx 442400 --out $OUT/KVCPU-fill80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
python3 $RPC/ab9_driver.py --arm KVCPU --leg tg80k --kind tg --ctx 442400 --max-tokens 1024 --out $OUT/KVCPU-tg80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
echo "--- judges KVCPU:"
tr -d '\0' < $ELOG | grep -aE 'tokens = ' | tail -3 | cut -c1-200
python3 - << 'PYEOF'
import json
p = '/mnt/srcds/Strata/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json'
c = json.load(open(p))
i = c['args'].index('--kv-resident'); c['args'][i+1] = '262144'
json.dump(c, open(p, 'w'), indent=2)
print('kv-resident restored: 262144')
PYEOF
echo "=== KVCPU ARM COMPLETE ($(date +%T)) ==="
