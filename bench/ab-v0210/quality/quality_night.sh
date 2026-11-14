#!/usr/bin/env bash
# quality_night.sh — after the fs 2,9 arm completes:
# 1) SHA temp-0 reproducibility (6x identical prompt, 512 tok) on live prod
# 2) stop prod -> teacher-forced quant_quality run (qqNIGHT: new pin, XXS pack, MMQ=0)
# 3) compare vs A.tsv (Q8_0 reference) and A vs C (historical IQ3_S reproduction)
# 4) restore prod supervised.
set -u
Q=/mnt/srcds/Strata/bench/ab-v0210/quality
WT=/mnt/srcds/Strata-wt-integ
R=/mnt/srcds/Strata
PY=python3

while ! grep -q 'COMPLETE' $R/logs/fs-arm-29.log 2>/dev/null; do sleep 20; done
sleep 8

echo "=== 1. SHA TEMP-0 REPRO (3x, live prod) ($(date +%T)) ==="
for i in 1 2 3; do
  RESP=$(curl -s -m 240 http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' \
    -d '{"model":"ud-iq3_xxs","messages":[{"role":"user","content":"Explain how a transformer attention head works, step by step, covering the query, key and value projections, the softmax over scores, and the output projection."}],"max_tokens":512,"temperature":0}')
  echo "$RESP" | $PY -c "
import sys, json, hashlib
d = json.load(sys.stdin)
c = d['choices'][0]['message']['content']
print(f'repro $i: {len(c.split())} words sha16={hashlib.sha256(c.encode()).hexdigest()[:16]} finish={d[\"choices\"][0].get(\"finish_reason\",\"?\")}')"
done

echo "=== 2. STOP PROD + TEACHER-FORCED RUN ($(date +%T)) ==="
bash $R/stop-all.sh >/dev/null 2>&1
sleep 4
cd $WT
$PY tools/quant_quality.py run --config $Q/qqNIGHT.json --out $Q/out/NIGHT.tsv 2>&1 | tail -5
echo "=== 3. COMPARES ($(date +%T)) ==="
echo "--- A (Q8_0 ref) vs NIGHT (XXS + pin 2e433a82, MMQ=0):"
$PY tools/quant_quality.py compare $Q/out/A.tsv $Q/out/NIGHT.tsv 2>&1 | tail -12
echo "--- A (Q8_0 ref) vs C (historical quant run):"
$PY tools/quant_quality.py compare $Q/out/A.tsv $Q/out/C.tsv 2>&1 | tail -12

echo "=== 4. RESTORE PROD ($(date +%T)) ==="
cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-ud-xxs-post-quality.log 2>&1 < /dev/null &
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
echo "=== QUALITY NIGHT COMPLETE ($(date +%T)) ==="
