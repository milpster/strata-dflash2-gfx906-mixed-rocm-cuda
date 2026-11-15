#!/usr/bin/env bash
# hcq8_native_arm.sh — HC_Q8 NATIVE path (STRATA_HC_Q8=1, NO INJECT — GGUF ships Q8_0 projections):
# boot on tuned pin (36ecbe98, TUNE default on), slots banner, fill/TG judges, greedy sha 3x,
# teacher-forced qqHCQ8N vs A/HCQ8/NIGHT, restore normal prod.
R=/mnt/srcds/Strata
RPC=$R/bench/ab-v0210/rpcprof
OUT=$RPC/out/gpu-rounds-20261114
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
Q=$R/bench/ab-v0210/quality
WT=/mnt/srcds/Strata-wt-integ

echo "=== 1. BOOT with STRATA_HC_Q8=1 only (native Q8 path, no INJECT) ($(date +%T)) ==="
bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
( cd $R && STRATA_HC_Q8=1 setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-hcq8n.log 2>&1 < /dev/null & )
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
echo "--- banner (slots/VRAM vs baseline 4983/9.67 GiB, INJECT-arm 4577/8.48):"
S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log | grep -v -- '-helper' | head -1)
tr -d '\0' < "$S" | grep -aiE 'hc_q8|hyper|expert cache|VRAM free' | head -8 | cut -c1-160

echo "=== 2. JUDGE LEGS ($(date +%T)) ==="
H=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*helper.log | head -1)
python3 $RPC/ab9_driver.py --arm HCQ8N --leg fill80k --kind prefill --ctx 442400 --out $OUT/HCQ8N-fill80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
python3 $RPC/ab9_driver.py --arm HCQ8N --leg tg80k --kind tg --ctx 442400 --max-tokens 1024 --out $OUT/HCQ8N-tg80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
echo "--- judges HCQ8N:"
tr -d '\0' < $ELOG | grep -aE 'tokens = ' | tail -4 | cut -c1-200

echo "=== 3. GREEDY SHA 3x (live, temp-0) ($(date +%T)) ==="
for i in 1 2 3; do
  RESP=$(curl -s -m 240 http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' \
    -d '{"model":"ud-iq3_xxs","messages":[{"role":"user","content":"Explain how a transformer attention head works, step by step, covering the query, key and value projections, the softmax over scores, and the output projection."}],"max_tokens":512,"temperature":0}')
  echo "$RESP" | python3 -c "
import sys, json, hashlib
d = json.load(sys.stdin)
c = d['choices'][0]['message']['content']
print(f'sha repro $i: {len(c.split())} words sha16={hashlib.sha256(c.encode()).hexdigest()[:16]} finish={d[\"choices\"][0].get(\"finish_reason\",\"?\")}')" 2>&1 | tail -1
done

echo "=== 4. TEACHER-FORCED qqHCQ8N (prod stopped) ($(date +%T)) ==="
bash $R/stop-all.sh >/dev/null 2>&1
sleep 4
cd $WT
python3 tools/quant_quality.py run --config $Q/qqHCQ8N.json --out $Q/out/HCQ8N.tsv 2>&1 | tail -3
echo "--- compare A (Q8_0 ref) vs HCQ8N (native):"
python3 tools/quant_quality.py compare $Q/out/A.tsv $Q/out/HCQ8N.tsv 2>&1 | tail -4
echo "--- compare HCQ8 (inject) vs HCQ8N (native) — same weights, must be near-identical:"
python3 tools/quant_quality.py compare $Q/out/HCQ8.tsv $Q/out/HCQ8N.tsv 2>&1 | tail -4
echo "--- compare NIGHT (knob off) vs HCQ8N:"
python3 tools/quant_quality.py compare $Q/out/NIGHT.tsv $Q/out/HCQ8N.tsv 2>&1 | tail -4

echo "=== 5. RESTORE NORMAL PROD ($(date +%T)) ==="
cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-post-hcq8n.log 2>&1 < /dev/null &
sleep 10
for i in $(seq 1 90); do
  S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log 2>/dev/null | grep -v -- '-helper' | head -1)
  RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
  PORT=$(curl -s -m 5 http://127.0.0.1:8009/v1/models 2>/dev/null | head -c 20)
  [ -n "$RD" ] && [ -n "$PORT" ] && { echo "READY: $RD"; break; }
  sleep 10
done
echo "=== HCQ8 NATIVE ARM COMPLETE ($(date +%T)) ==="
