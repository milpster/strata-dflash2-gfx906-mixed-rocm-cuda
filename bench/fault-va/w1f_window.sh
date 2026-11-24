#!/usr/bin/env bash
# w1f_window.sh - drive the remaining W1f boots end to end.
#
# Why a script: each boot is boot -> wait-for-health -> fire the frozen leg -> stop
# with a 20 s quiet bookend. Doing that by hand per boot invites the two classic
# errors this harness already paid for: firing the leg before the engine is ready
# (503), and stopping the wrong generation. This runs the sequence once per boot and
# prints one line per stage so the progress is visible while it runs.
#
# Usage: w1f_window.sh "A 1" "B 1" ...   (arm, rep pairs, in order)
#        w1f_window.sh --remaining        (B0 + the six claim boots, in runbook order)
set -uo pipefail
ROOT=/mnt/srcds/Strata
cd "$ROOT" || exit 1
PY=python3
LEGS=bench/ab-v0210/rpcprof/port_ab_legs.py
OUT=bench/ab-v0210/rpcprof/out/port-ab-20261123
say() { echo "[$(date +%H:%M:%S)] $*"; }

one_boot() {
  local arm=$1 rep=$2
  local key="W1f-$arm-pp-r$rep"
  local elog="$OUT/logs/$key.engine.log"
  say "boot $key"
  bash bench/ab-v0210/rpcprof/port_ab_boot.sh W1f "$arm" pp "$rep" --waive-nonengine \
    > "/tmp/opencode/w1f-boot-$arm-$rep.log" 2>&1 &
  local bp=$!
  local ok=0 i h
  for i in $(seq 1 50); do
    h=$(curl -fsS -m 2 http://127.0.0.1:8009/health 2>/dev/null | head -c 12 || true)
    [[ -n $h ]] && { ok=1; break; }
    kill -0 "$bp" 2>/dev/null || break
    sleep 10
  done
  if [[ $ok -ne 1 ]]; then
    say "  $key did NOT become healthy; see /tmp/opencode/w1f-boot-$arm-$rep.log"
    grep -E 'REFUS|FAIL|PROBLEM' "/tmp/opencode/w1f-boot-$arm-$rep.log" | head -3 | cut -c1-150
    return 1
  fi
  say "  healthy after ~$((i*10))s; firing the leg"
  $PY "$LEGS" --window W1f --arm "$arm" --boot pp --rep "$rep" --shape pp239k --run 2>&1 | tail -2
  local rc=${PIPESTATUS[0]}
  say "  leg exit $rc"
  tr -d '\000' < "$elog" 2>/dev/null | grep -aE 'prompt [0-9]+ tokens = ' | tail -1 | cut -c1-140
  local gen
  gen=$(tr -d '\000' < "$elog" 2>/dev/null | grep -acE 'Memory access fault by GPU node-')
  say "  engine fault lines this boot: $gen"
  bash bench/ab-v0210/rpcprof/port_ab_stop.sh W1f "$arm" pp "$rep" 2>&1 | tail -2
  return 0
}

PLAN=()
if [[ ${1:-} == --remaining ]]; then
  PLAN=("B 0" "A 1" "B 1" "B 2" "A 2" "A 3" "B 3")
else
  PLAN=("$@")
fi
[[ ${#PLAN[@]} -gt 0 ]] || { echo 'usage: w1f_window.sh "A 1" ... | --remaining' >&2; exit 2; }

for item in "${PLAN[@]}"; do
  read -r arm rep <<< "$item"
  one_boot "$arm" "$rep" || say "  (continuing after a failed boot)"
done
say 'done'
$PY "$LEGS" --check W1f 2>&1 | tail -20
