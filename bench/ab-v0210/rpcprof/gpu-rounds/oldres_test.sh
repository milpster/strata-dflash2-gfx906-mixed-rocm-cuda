#!/usr/bin/env bash
# oldres_test.sh — A/B discriminator: revert reserves to 192/220, clean the wedge, fresh boot,
# SAME fill80k leg, sampling RAM/GPU/read-progress during the fill (wedge signature hunt).
R=/mnt/srcds/Strata
RPC=$R/bench/ab-v0210/rpcprof
OUT=$RPC/out/gpu-rounds-20261114
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log

echo "=== 0. REVERT RESERVES -> 192/220 ($(date +%T)) ==="
python3 - << 'PYEOF'
import json
p = '/mnt/srcds/Strata/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json'
c = json.load(open(p))
a = c['args']
i = a.index('--vram-reserve-mib')
a[i+1] = '192'
json.dump(c, open(p, 'w'), indent=2)
print('vram-reserve-mib -> 192')
PYEOF
sed -i 's/--reserve-mib 156/--reserve-mib 220/' $R/launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh
grep -o -- '--reserve-mib [0-9]*' $R/launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh | head -1

echo "=== 1. CLEAN THE WEDGE ($(date +%T)) ==="
kill 2496791 2>/dev/null && echo "orphan driver killed" || echo "driver already gone"
bash $R/stop-all.sh >/dev/null 2>&1; sleep 4

echo "=== 2. FRESH BOOT on 192/220 ($(date +%T)) ==="
( cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-oldres.log 2>&1 < /dev/null & )
RD=""
for i in $(seq 1 90); do
  S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log 2>/dev/null | grep -v -- '-helper' | head -1)
  RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
  PORT=$(curl -s -m 5 http://127.0.0.1:8009/v1/models 2>/dev/null | head -c 20)
  [ -n "$RD" ] && [ -n "$PORT" ] && break
  sleep 10
done
echo "ready: ${RD:-TIMEOUT} ($(date +%T))"
[ -z "$RD" ] && { echo "BOOT FAIL"; exit 1; }
S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log | grep -v -- '-helper' | head -1)
tr -d '\0' < "$S" | grep -aiE 'expert cache|VRAM free' | head -3 | cut -c1-150

echo "=== 3. FILL80K + LIVE SAMPLES ($(date +%T)) ==="
H=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*helper.log | head -1)
( for j in $(seq 1 20); do
    sleep 15
    GPU=$(rocm-smi -u 2>/dev/null | grep -o 'GPU use (%): [0-9]*' | tr '\n' ' ')
    RAM=$(free -m | awk '/Mem:/{print $3"MiB used"}')
    SW=$(vmstat 1 1 | tail -1 | awk '{print "so="$8"KB/s"}')
    RDG=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'reading the prompt' | tail -1 | grep -o '[0-9]* s so far')
    echo "sample $j: $GPU | $RAM | $SW | read: ${RDG:-none}"
  done ) &
SAMPLER=$!
python3 $RPC/ab9_driver.py --arm OLDRES --leg fill80k --kind prefill --ctx 442400 --out $OUT/OLDRES-fill80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
kill $SAMPLER 2>/dev/null
echo "--- judges OLDRES:"
tr -d '\0' < $ELOG | grep -aE 'tokens = ' | tail -2 | cut -c1-200
echo "--- RAM after fill:"; free -h | head -2
echo "=== OLDRES TEST COMPLETE ($(date +%T)) ==="
