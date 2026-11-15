#!/usr/bin/env bash
# hcq8_fix_validate.sh — GO LANE (launch on operator's go, after reserve64_lane.sh):
# waits for RES64 COMPLETE + the hcq8trim build, swaps pin, boots HC_Q8=1 native (fixed swap+trim),
# banner slots (expect > 4983), fill/TG judges, teacher-forced qqHCQ8N 3x (quality + #267 correlation),
# then restores normal prod (fixed pin stays).
R=/mnt/srcds/Strata
RPC=$R/bench/ab-v0210/rpcprof
OUT=$RPC/out/gpu-rounds-20261114
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
Q=$R/bench/ab-v0210/quality
WT=/mnt/srcds/Strata-wt-integ
PIN_DST=$R/v0233/build-mixed-gpu/rpcw-hip/strata
PIN_NEW=$WT/build-mixed-gpu/rpcw-hip-hcq8trim/strata

while ! grep -q 'RES64 LANE COMPLETE' $R/logs/res64-lane.log 2>/dev/null; do sleep 20; done
while [ ! -s "$WT/build-mixed-gpu/rpcw-hip-hcq8trim/strata.sha256" ]; do sleep 20; done
sleep 5

echo "=== 0. FLIP PIN -> hcq8trim fix ($(date +%T)) ==="
bash $R/stop-all.sh >/dev/null 2>&1; sleep 2
cp "$PIN_DST" "$R/v0233/build-mixed-gpu/rpcw-hip/strata.bak-$(sha256sum "$PIN_DST" | cut -c1-8)"
cp "$PIN_NEW" "$PIN_DST" && chmod +x "$PIN_DST"
sha256sum "$PIN_DST" | cut -c1-16 > $R/v0233/build-mixed-gpu/rpcw-hip/strata.pin-sha16
echo "pin: $(cat $R/v0233/build-mixed-gpu/rpcw-hip/strata.pin-sha16)"

echo "=== 1. BOOT: HC_Q8=1 native + TRIM default (fixed swap) ($(date +%T)) ==="
( cd $R && STRATA_HC_Q8=1 setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-hcq8fix.log 2>&1 < /dev/null & )
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
echo "--- banner (slots vs OFF 4983/9.67, broken-ON 4577/8.48):"
tr -d '\0' < "$S" | grep -aiE 'expert cache|VRAM free|LOW|hc_q8' | head -8 | cut -c1-160

echo "=== 2. JUDGE LEGS ($(date +%T)) ==="
H=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*helper.log | head -1)
python3 $RPC/ab9_driver.py --arm H8FIX --leg fill80k --kind prefill --ctx 442400 --out $OUT/H8FIX-fill80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
python3 $RPC/ab9_driver.py --arm H8FIX --leg tg80k --kind tg --ctx 442400 --max-tokens 1024 --out $OUT/H8FIX-tg80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
echo "--- judges H8FIX:"
tr -d '\0' < $ELOG | grep -aE 'tokens = ' | tail -4 | cut -c1-200

echo "=== 3. GREEDY SHA 3x ($(date +%T)) ==="
for i in 1 2 3; do
  RESP=$(curl -s -m 240 http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' \
    -d '{"model":"ud-iq3_xxs","messages":[{"role":"user","content":"Explain how a transformer attention head works, step by step, covering the query, key and value projections, the softmax over scores, and the output projection."}],"max_tokens":512,"temperature":0}')
  echo "$RESP" | python3 -c "
import sys, json, hashlib
d = json.load(sys.stdin)
c = d['choices'][0]['message']['content']
print(f'sha repro $i: sha16={hashlib.sha256(c.encode()).hexdigest()[:16]} finish={d[\"choices\"][0].get(\"finish_reason\",\"?\")}')" 2>&1 | tail -1
done

echo "=== 4. TEACHER-FORCED qqHCQ8N x3 (#267 correlation + quality) ($(date +%T)) ==="
for run in 1 2 3; do
  bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
  cd $WT
  python3 tools/quant_quality.py run --config $Q/qqHCQ8N.json --out $Q/out/HCQ8F$run.tsv 2>&1 | tail -2
  echo "--- run $run vs NIGHT:"
  python3 tools/quant_quality.py compare $Q/out/NIGHT.tsv $Q/out/HCQ8F$run.tsv 2>&1 | tail -3
done

echo "=== 5. RESTORE NORMAL PROD (fixed pin, HC_Q8 off default) ($(date +%T)) ==="
cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-post-hcq8fix.log 2>&1 < /dev/null &
sleep 10
for i in $(seq 1 90); do
  S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log 2>/dev/null | grep -v -- '-helper' | head -1)
  RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
  PORT=$(curl -s -m 5 http://127.0.0.1:8009/v1/models 2>/dev/null | head -c 20)
  [ -n "$RD" ] && [ -n "$PORT" ] && { echo "READY: $RD"; break; }
  sleep 10
done
echo "=== HCQ8 FIX VALIDATE COMPLETE ($(date +%T)) ==="
