#!/usr/bin/env bash
# helper64_test.sh — helper reserve cut in isolation: 220->156 (ROCm stays 192).
# Fresh boot, banner, fill80k + live sampler, tg80k, judges, no-NaN check.
R=/mnt/srcds/Strata
RPC=$R/bench/ab-v0210/rpcprof
OUT=$RPC/out/gpu-rounds-20261114
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log

echo "=== 0. HELPER CUT: launcher --reserve-mib 220->156 (config stays 192) ($(date +%T)) ==="
sed -i 's/--reserve-mib 220/--reserve-mib 156/' $R/launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh
grep -o -- '--reserve-mib [0-9]*' $R/launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh | head -1

echo "=== 1. FRESH BOOT on 192/156 ($(date +%T)) ==="
bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
( cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-helper64.log 2>&1 < /dev/null & )
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
    RDG=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'reading the prompt' | tail -1 | grep -o '[0-9]* s so far')
    echo "sample $j: $GPU | $RAM | read: ${RDG:-none}"
  done ) &
SAMPLER=$!
python3 $RPC/ab9_driver.py --arm HLP64 --leg fill80k --kind prefill --ctx 442400 --out $OUT/HLP64-fill80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
kill $SAMPLER 2>/dev/null

echo "=== 3. TG80K (generation NaN detector) ($(date +%T)) ==="
python3 $RPC/ab9_driver.py --arm HLP64 --leg tg80k --kind tg --ctx 442400 --max-tokens 1024 --out $OUT/HLP64-tg80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120

echo "=== 4. NO-NaN CHECK ($(date +%T)) ==="
curl -s -m 90 http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"ud-iq3_xxs","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":64,"temperature":0}' | python3 -c "
import sys, json
d = json.load(sys.stdin)
ch = d['choices'][0]
print('finish:', ch.get('finish_reason'), '| content:', repr((ch['message'].get('content') or '')[:40]))" 2>&1 | tail -1

echo "--- judges HLP64:"
tr -d '\0' < $ELOG | grep -aE 'tokens = ' | tail -3 | cut -c1-200
echo "--- NaN/503 scan:"
tr -d '\0' < "$S" | grep -aiE 'non-finite|NaN|503' | head -3
echo "=== HELPER64 TEST COMPLETE ($(date +%T)) ==="
