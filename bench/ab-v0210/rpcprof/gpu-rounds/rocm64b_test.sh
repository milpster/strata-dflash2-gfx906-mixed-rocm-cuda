#!/usr/bin/env bash
# rocm64b_test.sh — next bisect step: ROCm reserve 128->64 (helper stays 220).
# Chains on ROCM64 COMPLETE + cleanliness gate (no wedge signature), then same fill + sampler.
R=/mnt/srcds/Strata
RPC=$R/bench/ab-v0210/rpcprof
OUT=$RPC/out/gpu-rounds-20261114
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log

while ! grep -q 'ROCM64 TEST COMPLETE' $R/logs/rocm64-test.log 2>/dev/null; do sleep 20; done
sleep 5
# gate: no sample may show a 300+ s read (wedge signature) in the completed ROCM64 log
if grep -qE 'read: [3-9][0-9]{2} s so far' $R/logs/rocm64-test.log; then
  echo "GATE FAIL: ROCM64 showed wedge signature - not reducing further"; exit 1
fi
echo "gate pass: ROCM64 clean"

echo "=== 0. ROCm CUT DEEPER: vram-reserve-mib 128->64 ($(date +%T)) ==="
python3 - << 'PYEOF'
import json
p = '/mnt/srcds/Strata/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json'
c = json.load(open(p))
a = c['args']
i = a.index('--vram-reserve-mib')
old = a[i+1]; a[i+1] = '64'
json.dump(c, open(p, 'w'), indent=2)
print(f'vram-reserve-mib: {old} -> 64')
PYEOF

echo "=== 1. FRESH BOOT on 64/220 ($(date +%T)) ==="
bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
( cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-rocm64b.log 2>&1 < /dev/null & )
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

echo "=== 2. FILL80K + LIVE SAMPLES ($(date +%T)) ==="
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
python3 $RPC/ab9_driver.py --arm ROCM64B --leg fill80k --kind prefill --ctx 442400 --out $OUT/ROCM64B-fill80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
kill $SAMPLER 2>/dev/null
echo "--- judges ROCM64B:"
tr -d '\0' < $ELOG | grep -aE 'tokens = ' | tail -2 | cut -c1-200
echo "--- RAM after fill:"; free -h | head -2
echo "=== ROCM64B TEST COMPLETE ($(date +%T)) ==="
