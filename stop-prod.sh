#!/usr/bin/env bash
# stop-prod.sh — cleanly stop the prod mixed pair: supervisor first (state-file
# PID, no pattern matching), then any orphaned launcher/engine/helper left over,
# then sockets. Safe to run repeatedly; exits 0 with a one-line status.
# Usage: ./stop-prod.sh
set -uo pipefail
cd "$(dirname "$0")"

STATE=run/prod-swift15-f16kv-mixed-supervisor.json
sup=$(jq -r '.supervisor_pid // empty' "$STATE" 2>/dev/null)
if [ -n "${sup:-}" ] && kill -0 "$sup" 2>/dev/null; then
  echo "stop-prod: TERM supervisor $sup"
  kill -TERM "$sup" 2>/dev/null || true
  timeout 90 tail --pid="$sup" -f /dev/null 2>/dev/null || true
fi

# orphan sweep from inside this file: the patterns live here, not in any
# caller's argv, so pgrep cannot match a self-containing shell command line
mapfile -t pids < <(pgrep -f 'launch-prod-swift15|strata --serve|strata-expert-helper' 2>/dev/null || true)
if [ ${#pids[@]} -gt 0 ]; then
  echo "stop-prod: TERM orphans: ${pids[*]}"
  kill -TERM "${pids[@]}" 2>/dev/null || true
  sleep 5
  mapfile -t still < <(pgrep -f 'launch-prod-swift15|strata --serve|strata-expert-helper' 2>/dev/null || true)
  [ ${#still[@]} -gt 0 ] && { echo "stop-prod: KILL stragglers: ${still[*]}"; kill -KILL "${still[@]}" 2>/dev/null || true; sleep 2; }
fi

rm -f run/prod-swift15-f16kv-mixed.sock run/prod-swift15-f16kv-mixed2.sock
left=$(pgrep -fc 'launch-prod-swift15|strata --serve|strata-expert-helper' 2>/dev/null || true)
echo "stop-prod: done — ${left:-0} strata prod processes remain"
[ "${left:-0}" -eq 0 ]
