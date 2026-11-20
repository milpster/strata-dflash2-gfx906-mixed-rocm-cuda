#!/usr/bin/env bash
# stop-all.sh — stop EVERY strata serving stack on the box: every pair
# supervisor (any lane whose state file sits in run/ as *-supervisor.json) via
# its state file FIRST so nothing respawns, then any orphaned
# launchers/engines/helpers/wrappers, then sockets. Verifies sustained quiet
# for 20s before declaring done.
# Usage: ./stop-all.sh
set -uo pipefail
cd "$(dirname "$0")"

term_pid() {  # wait for a pid to exit, up to 60s
  local pid=$1
  kill -TERM "$pid" 2>/dev/null || return 0
  timeout 60 tail --pid="$pid" -f /dev/null 2>/dev/null || true
}

echo "stop-all: supervisors first (state files)"
# Every lane drops its pair-supervisor state file in run/ as *-supervisor.json (prod,
# trial-spill40, trial-arena54, ...), so the loop globs them instead of naming them: a
# supervisor left alive here respawns its pair forever (pair_supervisor.py:276, backoff
# capped at 30 s), the sweep below then flaps against the launcher's socket guards, and the
# quiet check passes while a supervisor still lives (count_strata does not match
# pair_supervisor.py). A lane that does not write such a file has to be added here by hand.
for state in run/*-supervisor.json; do
  [ -f "$state" ] || continue
  sup=$(jq -r '.supervisor_pid // empty' "$state" 2>/dev/null)
  # TERM it only if that pid really is a pair supervisor: a state file outlives its run and
  # its pid can be recycled to something unrelated by the time the next lane stops.
  [ -n "${sup:-}" ] && kill -0 "$sup" 2>/dev/null && grep -qa 'pair_supervisor[.]py' "/proc/$sup/cmdline" 2>/dev/null && { echo "  TERM $state supervisor $sup"; term_pid "$sup"; }
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
      run/prod-iq3xxs-ud-mixed.sock run/prod-iq3xxs-ud-mixed2.sock \
      run/trial-spill40.sock run/trial-spill40-2.sock \
      run/trial-arena54.sock run/trial-arena54-2.sock

count_strata() { pgrep -f 'launch-prod-|launch-trial-|strata --serve|strata-expert-helper|serve/server[.]py --engine strata' 2>/dev/null | wc -l; }
echo "stop-all: verifying 20s of quiet"
q1=$(count_strata); sleep 20; q2=$(count_strata)
echo "stop-all: before=$q1 after=$q2 — $([ "$q2" -eq 0 ] && echo ALL STOPPED || echo STILL ALIVE, RUN AGAIN)"
[ "$q2" -eq 0 ]
