#!/usr/bin/env bash
# agent-turn-watch.sh - watch a cancelled agent's final in-flight turn.
# Exits early when the engine log goes quiet (turn over) or raises an alarm if:
#   - prod health fails (agent stopped/restarted prod)
#   - the repo gains modifications beyond the known baseline set
# Usage: agent-turn-watch.sh <repo> <health-url> <engine-log> <known-dirty-regex> <max-seconds>
set -u
REPO=$1; HEALTH=$2; ENGINE_LOG=$3; KNOWN=$4; MAXS=$5
BASE_COUNT=$(grep -cE "^strata serve:" "$ENGINE_LOG" 2>/dev/null || echo 0)
QUIET=0
for ((i=0; i<MAXS; i+=10)); do
  sleep 10
  if ! curl -sf -m 5 "$HEALTH" >/dev/null; then echo "ALARM: prod health FAILED at $(date +%H:%M:%S) - agent may have stopped prod"; exit 2; fi
  NEW=$(cd "$REPO" && git status -s | grep -vE "$KNOWN" | head -5)
  if [[ -n "$NEW" ]]; then echo "ALARM: unexpected repo changes at $(date +%H:%M:%S):"; echo "$NEW"; exit 3; fi
  COUNT=$(grep -cE "^strata serve:" "$ENGINE_LOG" 2>/dev/null || echo 0)
  if (( COUNT > BASE_COUNT )); then QUIET=0; BASE_COUNT=$COUNT; else QUIET=$((QUIET+10)); fi
  echo "$(date +%H:%M:%S) engine-requests=$COUNT quiet-for=${QUIET}s"
  if (( QUIET >= 90 )); then echo "DONE: engine quiet 90s - agent turn appears finished, prod still healthy"; exit 0; fi
done
echo "TIMEOUT after ${MAXS}s - agent still active, prod healthy"; exit 1
