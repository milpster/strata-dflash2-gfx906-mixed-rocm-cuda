#!/usr/bin/env bash
# retry the teacher-forced quality run after the 240k stress: wrapper exe (launcher LD path), compare, restore
Q=/mnt/srcds/Strata/bench/ab-v0210/quality
WT=/mnt/srcds/Strata-wt-integ
R=/mnt/srcds/Strata
while ! grep -q 'F240 CHAIN COMPLETE' $R/logs/queue-f240.log 2>/dev/null; do sleep 20; done
sleep 8
bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
cd $WT
python3 tools/quant_quality.py run --config $Q/qqNIGHT.json --out $Q/out/NIGHT.tsv 2>&1 | tail -3
echo "=== COMPARE A (Q8 ref) vs NIGHT ==="
python3 tools/quant_quality.py compare $Q/out/A.tsv $Q/out/NIGHT.tsv 2>&1 | tail -14
echo "=== RESTORE PROD ==="
cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-ud-xxs-post-quality2.log 2>&1 < /dev/null &
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
echo "=== QUALITY RETRY COMPLETE ($(date +%T)) ==="
