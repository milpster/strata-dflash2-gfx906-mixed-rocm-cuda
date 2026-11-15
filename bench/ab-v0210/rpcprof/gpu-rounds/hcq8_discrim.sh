#!/usr/bin/env bash
# hcq8_discrim.sh — discriminator: FIXED binary + INJECT (proven-weights path) teacher-forced.
# Chains on HCQ8 FIX VALIDATE COMPLETE. Pass criteria: completes all segments AND matches
# HCQ8.tsv (pre-fix inject) closely; timeout/garbage = the trim fix is broken -> revert pin.
R=/mnt/srcds/Strata
Q=$R/bench/ab-v0210/quality
WT=/mnt/srcds/Strata-wt-integ

while ! grep -q 'HCQ8 FIX VALIDATE COMPLETE' $R/logs/hcq8-fix-validate.log 2>/dev/null; do sleep 30; done
sleep 5

echo "=== DISCRIMINATOR: fixed binary + INJECT ($(date +%T)) ==="
bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
cd $WT
python3 tools/quant_quality.py run --config $Q/qqHCQ8.json --out $Q/out/HCQ8FIXI.tsv 2>&1 | tail -4
echo "--- HCQ8 (pre-fix inject) vs HCQ8FIXI (fixed inject) — near-identical expected:"
python3 tools/quant_quality.py compare $Q/out/HCQ8.tsv $Q/out/HCQ8FIXI.tsv 2>&1 | tail -4
echo "--- NIGHT (off) vs HCQ8FIXI:"
python3 tools/quant_quality.py compare $Q/out/NIGHT.tsv $Q/out/HCQ8FIXI.tsv 2>&1 | tail -3

echo "=== RESTORE PROD ($(date +%T)) ==="
cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-post-discrim.log 2>&1 < /dev/null &
sleep 10
for i in $(seq 1 90); do
  S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log 2>/dev/null | grep -v -- '-helper' | head -1)
  RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
  PORT=$(curl -s -m 5 http://127.0.0.1:8009/v1/models 2>/dev/null | head -c 20)
  [ -n "$RD" ] && [ -n "$PORT" ] && { echo "READY: $RD"; break; }
  sleep 10
done
echo "=== DISCRIM COMPLETE ($(date +%T)) ==="
