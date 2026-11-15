#!/usr/bin/env bash
# lane_restore.sh — restore the normal prod boot (boot-gate discipline) + no-NaN generation check.
set -u
R=/mnt/srcds/Strata
bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
( cd $R && setsid --fork ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-post-lane.log 2>&1 < /dev/null )
sleep 10
RD=""
for i in $(seq 1 90); do
  S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log 2>/dev/null | grep -v -- '-helper' | head -1)
  RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
  PORT=$(curl -s -m 5 http://127.0.0.1:8009/v1/models 2>/dev/null | head -c 20)
  [ -n "$RD" ] && [ -n "$PORT" ] && break
  RD=""; sleep 10
done
echo "RESTORED: ${RD:-FAIL} ($(date +%T))"
curl -s -m 90 http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"ud-iq3_xxs","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":64,"temperature":0}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('no-NaN check:', repr((d['choices'][0]['message'].get('content') or '')[:20]), d['choices'][0].get('finish_reason'))" 2>&1 | tail -1
