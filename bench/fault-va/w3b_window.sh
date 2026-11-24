#!/usr/bin/env bash
# w3b_window.sh - arm-B-only W3 boots (promoted engine 15d330ea).
#
# Why arm-B only: the fork arm (4b8251e9) has no pool fix and faults on long
# prefills at its historic rate, which costs boots without adding information.
# Operator directive 2026-11-24: ignore the old version, measure the new one.
#
# Each boot: stop previous -> boot -> wait health -> fire the 1024-token tg leg
# -> record judge lines and fault count -> stop.
set -uo pipefail
cd /mnt/srcds/Strata || exit 1
PY=python3
LEGS=bench/ab-v0210/rpcprof/port_ab_legs.py
OUT=bench/ab-v0210/rpcprof/out/port-ab-20261123
LOGD=bench/fault-va/out/logs
mkdir -p "$LOGD"
say() { echo "[$(date +%H:%M:%S)] $*"; }

one() {
  local rep=$1
  local key="W3-B-pp-r$rep"
  say "boot $key"
  bash bench/ab-v0210/rpcprof/port_ab_boot.sh W3 B pp "$rep" --waive-nonengine \
    > "$LOGD/w3-b-r$rep-boot.log" 2>&1 &
  local bp=$! ok=0 i h
  for i in $(seq 1 50); do
    h=$(curl -fsS -m 2 http://127.0.0.1:8009/health 2>/dev/null | head -c 12 || true)
    [[ -n $h ]] && { ok=1; break; }
    kill -0 "$bp" 2>/dev/null || break
    sleep 10
  done
  if [[ $ok -ne 1 ]]; then say "  $key did not become healthy"; return 1; fi
  say "  healthy after ~$((i*10))s; firing the tg leg"
  $PY "$LEGS" --window W3 --arm B --boot pp --rep "$rep" --shape pp239k --run 2>&1 | tail -2
  say "  judge lines:"
  tr -d '\000' < "$OUT/logs/$key.engine.log" | grep -aE 'prompt [0-9]+ tokens = ' | tail -2 | cut -c1-150 | sed 's/^/    /'
  say "  faults this boot: $(journalctl -k --since '-12 min' --no-pager 2>/dev/null | grep -cE 'no-retry page fault')"
  bash bench/ab-v0210/rpcprof/port_ab_stop.sh W3 B pp "$rep" 2>&1 | tail -1
}

for rep in "$@"; do one "$rep" || say "  (continuing)"; done
say 'done'
