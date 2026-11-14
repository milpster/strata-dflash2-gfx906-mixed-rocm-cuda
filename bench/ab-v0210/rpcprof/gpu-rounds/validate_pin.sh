#!/usr/bin/env bash
# validate_pin.sh — after build_pin.sh completes: swap the prod pin to the new binary, boot,
# verify banners (MMQ state line, helper fill at reserve 240), fill+TG legs, temp-0 probes x3.
set -u
R=/mnt/srcds/Strata
WT=/mnt/srcds/Strata-wt-integ
RPC=$R/bench/ab-v0210/rpcprof
OUT=$RPC/out/gpu-rounds-20261114
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
PIN=$R/v0233/build-mixed-gpu/rpcw-hip/strata
NEW=$WT/build-mixed-gpu/rpcw-hip-v0233pin/strata

# wait for the build to complete with guards green
while ! grep -q 'BUILD PIN COMPLETE' $R/logs/build-pin-v0233.log 2>/dev/null; do sleep 20; done
grep -q 'GUARD PASS' $R/logs/build-pin-v0233.log || { echo "guards not green - aborting pin swap"; exit 1; }
NEWSHA=$(sha256sum "$NEW" | cut -c1-16)
echo "new binary sha16: $NEWSHA"

# swap pin with backup
cp "$PIN" "$R/v0233/build-mixed-gpu/rpcw-hip/strata.bak-$(sha256sum "$PIN" | cut -c1-8)" 2>/dev/null
cp "$NEW" "$PIN" && sha256sum "$PIN" | cut -c1-16 > $R/v0233/build-mixed-gpu/rpcw-hip/strata.pin-sha16
echo "pin swapped: $(cat $R/v0233/build-mixed-gpu/rpcw-hip/strata.pin-sha16)"

bash $R/stop-all.sh >/dev/null 2>&1
sleep 4
cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-ud-xxs-pin-validation.log 2>&1 < /dev/null &
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
echo "=== PIN VALIDATION BANNERS ==="
tr -d '\0' < $ELOG | grep -a 'prefill mmq:' | tail -1
tr -d '\0' < $ELOG | grep -a 'expert cache [0-9]* slots' | tail -2
H=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*helper.log | head -1)
tr -d '\0' < "$H" | grep -a 'filled\|reserve' | tail -3
echo "=== FILL 80k ==="
python3 $RPC/ab9_driver.py --arm PINV --leg fill80k --kind prefill --ctx 442400 --out $OUT/PINV-fill80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-110
echo "=== TG x2 ==="
python3 $RPC/ab9_driver.py --arm PINV --leg tg80k --kind tg --ctx 442400 --max-tokens 1024 --out $OUT/PINV-tg80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-110
python3 $RPC/ab9_driver.py --arm PINV --leg tg80k --kind tg --ctx 442400 --max-tokens 1024 --out $OUT/PINV-tg80k-2.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-110
echo "=== TEMP-0 PROBES x3 ==="
for i in 1 2 3; do
  RESP=$(curl -s -m 180 http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"ud-iq3_xxs","messages":[{"role":"user","content":"Describe the water cycle in exactly three sentences."}],"max_tokens":256,"temperature":0}')
  SHA=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); c=d['choices'][0]['message']['content']; print(len(c.split()), __import__('hashlib').sha256(c.encode()).hexdigest()[:16])" 2>/dev/null || echo PARSE-FAIL)
  echo "probe $i: tokens/sha16 = $SHA"
done
echo "=== JUDGES ==="
tr -d '\0' < "$ELOG" | grep -aE 'tokens = ' | tail -4 | cut -c1-180
echo "=== PIN VALIDATION COMPLETE ($(date +%H:%M:%S)) ==="
