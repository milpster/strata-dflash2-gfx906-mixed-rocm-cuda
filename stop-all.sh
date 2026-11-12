#!/usr/bin/env bash
# stop-all.sh — stop EVERY strata serving stack on the box: both pair
# supervisors (prod + trial-spill40) via their state files FIRST so nothing
# respawns, then any orphaned launchers/engines/helpers/wrappers, then
# sockets. Verifies sustained quiet for 20s before declaring done.
# Usage: ./stop-all.sh
set -uo pipefail
cd "$(dirname "$0")"

term_pid() {  # wait for a pid to exit, up to 60s
  local pid=$1
  kill -TERM "$pid" 2>/dev/null || return 0
  timeout 60 tail --pid="$pid" -f /dev/null 2>/dev/null || true
}

echo "stop-all: supervisors first (state files)"
for state in run/prod-swift15-f16kv-mixed-supervisor.json run/trial-spill40-supervisor.json; do
  [ -f "$state" ] || continue
  sup=$(jq -r '.supervisor_pid // empty' "$state" 2>/dev/null)
  [ -n "${sup:-}" ] && kill -0 "$sup" 2>/dev/null && { echo "  TERM $state supervisor $sup"; term_pid "$sup"; }
done

echo "stop-all: launcher/engine/helper/wrapper sweep (patterns live in this file)"
mapfile -t pids < <(pgrep -f 'launch-prod-|launch-trial-|strata --serve|strata-expert-helper|serve/server[.]py --engine strata' 2>/dev/null || true)
if [ ${#pids[@]} -gt 0 ]; then
  echo "  TERM ${pids[*]}"
  kill -TERM "${pids[@]}" 2>/dev/null || true
  sleep 5
  mapfile -t still < <(pgrep -f 'launch-prod-|launch-trial-|strata --serve|strata-expert-helper|serve/server[.]py --engine strata' 2>/dev/null || true)
  [ ${#still[@]} -gt 0 ] && { echo "  KILL ${still[*]}"; kill -KILL "${still[@]}" 2>/dev/null || true; sleep 2; }
fi

rm -f run/prod-swift15-f16kv-mixed.sock run/prod-swift15-f16kv-mixed2.sock \
      run/trial-spill40.sock run/trial-spill40-2.sock

count_strata() { pgrep -f 'launch-prod-|launch-trial-|strata --serve|strata-expert-helper|serve/server[.]py --engine strata' 2>/dev/null | wc -l; }
echo "stop-all: verifying 20s of quiet"
q1=$(count_strata); sleep 20; q2=$(count_strata)
echo "stop-all: before=$q1 after=$q2 — $([ "$q2" -eq 0 ] && echo ALL STOPPED || echo STILL ALIVE, RUN AGAIN)"
[ "$q2" -eq 0 ]
