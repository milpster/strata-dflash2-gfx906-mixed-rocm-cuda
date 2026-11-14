#!/usr/bin/env bash
# Restore-boot helper (2026-11-14): stop prod, relaunch the champion config,
# wait for the 'ready: http://' line in the newest non-helper server log, then
# verify one chat completion answers. Runs from a file so stop-all's pgrep
# patterns ('launch-prod-') cannot match this script's own cmdline.
set -uo pipefail
ROOT=/mnt/srcds/Strata
bash "$ROOT/stop-all.sh"
sleep 3
T0=$(date +%s)
setsid nohup bash "$ROOT/launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh" \
  > "$ROOT/logs/spec-arm-restore-champion.log" 2>&1 < /dev/null &
for i in $(seq 1 120); do
  sleep 10
  L=$(ls -t "$ROOT"/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-*.log 2>/dev/null | grep -v -- -helper | head -1)
  if [ -n "$L" ] && [ "$(tr -d '\0' < "$L" | grep -ac 'ready: http://')" -gt 0 ]; then
    echo "READY after $(( $(date +%s) - T0 ))s in $L"
    tr -d '\0' < "$L" | grep -a 'ready: http://'
    break
  fi
  [ $((i % 6)) -eq 0 ] && echo "  ...waiting ($((i * 10))s) $(date +%T)"
done
L=$(ls -t "$ROOT"/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-*.log 2>/dev/null | grep -v -- -helper | head -1)
[ "$(tr -d '\0' < "$L" | grep -ac 'ready: http://')" -gt 0 ] || { echo "READY LINE MISSING"; exit 4; }
sleep 3
echo "--- chat completion check $(date +%T)"
curl -s http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Reply with the single word: ok"}],"max_tokens":8,"temperature":0,"enable_thinking":false}' \
  | head -c 400
echo
echo "--- engine judge line for the curl"
tail -c 4000 "$ROOT/logs/engine-prod-ud-iq3xxs-v0233.log" | tr -d '\0' | grep -a 'strata serve:' | tail -1
