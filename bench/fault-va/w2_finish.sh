#!/usr/bin/env bash
# w2_finish.sh - the remaining W2 claim boots, per the approved contract.
#
# Why a script: the contract's W2 needs 4 decode claim boots + 4 short claim
# boots, interleaved A-B-B-A, each with its own boot (stop -> boot -> health ->
# legs -> stop). Doing that by hand invites the two failures already paid for:
# firing a leg before health (503) and stopping the wrong generation.
#
# Per the contract: pilots (r0) are excluded from the claim sample and were
# frozen into the manifest before these boots. Every boot records its own
# engine sha, memory and fault fingerprint; --check decides afterwards.
set -uo pipefail
cd /mnt/srcds/Strata || exit 1
PY=python3
LEGS=bench/ab-v0210/rpcprof/port_ab_legs.py
LOGD=bench/fault-va/out/logs
mkdir -p "$LOGD"
say() { echo "[$(date +%H:%M:%S)] $*"; }

run_boot() {
  local group=$1 arm=$2 rep=$3 key="W2-$2-$1-r$3"
  say "boot $key"
  bash bench/ab-v0210/rpcprof/port_ab_boot.sh W2 "$arm" "$group" "$rep" --waive-nonengine \
    > "$LOGD/w2-boot-$group-$arm-$rep.log" 2>&1 &
  local bp=$! ok=0 i h
  for i in $(seq 1 50); do
    h=$(curl -fsS -m 2 http://127.0.0.1:8009/health 2>/dev/null | head -c 12 || true)
    [[ -n $h ]] && { ok=1; break; }
    kill -0 "$bp" 2>/dev/null || break
    sleep 10
  done
  if [[ $ok -ne 1 ]]; then
    say "  $key NOT healthy; see $LOGD/w2-boot-$group-$arm-$rep.log"
    grep -E 'REFUS|FAIL' "$LOGD/w2-boot-$group-$arm-$rep.log" | head -2 | cut -c1-150
    return 1
  fi
  say "  healthy after ~$((i*10))s"
  if [[ $group == dec ]]; then
    $PY "$LEGS" --window W2 --arm "$arm" --boot dec --rep "$rep" --shape dec144k --run 2>&1 | tail -1
  else
    for shape in pp2k pp8k ttft3; do
      $PY "$LEGS" --window W2 --arm "$arm" --boot sht --rep "$rep" --shape "$shape" --run 2>&1 | tail -1
    done
  fi
  say "  faults since boot: $(journalctl -k --since '-14 min' --no-pager 2>/dev/null | grep -cE 'no-retry page fault')"
  bash bench/ab-v0210/rpcprof/port_ab_stop.sh W2 "$arm" "$group" "$rep" 2>&1 | tail -2
}

# contract interleave: A B B A for each group
for group in dec sht; do
  for pair in "A 1" "B 1" "B 2" "A 2"; do
    read -r arm rep <<< "$pair"
    run_boot "$group" "$arm" "$rep" || say "  (continuing after failed boot)"
  done
done
say 'all W2 claim boots attempted'
$PY "$LEGS" --check W2 2>&1 | tail -25
