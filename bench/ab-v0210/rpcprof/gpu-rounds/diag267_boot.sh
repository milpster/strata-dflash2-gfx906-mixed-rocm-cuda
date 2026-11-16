#!/usr/bin/env bash
# diag267_boot.sh — #267 wedge forensics driver (single-purpose, run with prod DOWN).
#
# Boots the #267-prone qq-shape engine (v0235 + STRATA_VERIFY_TRACE) as a
# descendant of this script, then either:
#   (a) boot-registration wedge: /ready never arrives, engine frozen during
#       register_whole (the 4032/4480 class), or
#   (b) request wedge: chat requests driven in a loop until one never answers.
# On wedge: capture per-thread state/wchan/syscall, KFD sysfs counters, dmesg
# tail, engine trace tail, and ALL-THREAD gdb stacks via the exec trick —
# replacing this bash with gdb makes the engine gdb's child, so PTRACE_ATTACH
# is legal under yama ptrace_scope=1. A forked watchdog takes the final KFD
# snapshot and kills the engine 5 min after capture starts.
#
# Usage: diag267_boot.sh <out_dir>   (out_dir must contain prompt.txt)
set -u
OUT="$1"
CFG=/mnt/srcds/Strata/bench/ab-v0210/quality/diag267.json
LOG="$OUT/engine.stderr.txt"; DRV="$OUT/driver.log"
ts() { date +%H:%M:%S; }
log() { echo "[$(ts)] $*" >> "$DRV"; }

resolve_epid() {
  pgrep -n -x strata-host || pgrep -n -f 'v0235p0' || echo "$BOOTPID"
}

dump_kfd() { # $1 = outfile
  local p
  for p in /sys/class/kfd/kfd/proc/$(resolve_epid)/*; do
    echo "$(basename "$p"): $(cat "$p" 2>/dev/null | head -c 300)"
  done > "$1" 2>/dev/null
}

# --- engine command line + env kit, verbatim from the json
python3 - "$CFG" > "$OUT/boot.cmd" <<'PY'
import json, shlex, sys
c = json.load(open(sys.argv[1]))
print(shlex.join([c["exe"]] + c.get("args", [])))
PY
eval "$(python3 - "$CFG" <<'PY'
import json, shlex, sys
c = json.load(open(sys.argv[1]))
for k, v in c.get("env", {}).items():
    print(f"export {k}={shlex.quote(str(v))}")
PY
)"

log "diag267: booting engine"
bash $(cat "$OUT/boot.cmd") >"$LOG" 2>&1 &
BOOTPID=$!

# --- periodic per-thread state sampler (re-resolves the pid; runs until engine death)
(
  while :; do
    P=$(pgrep -n -x strata-host || pgrep -n -f 'v0235p0' || echo "$BOOTPID")
    kill -0 "$P" 2>/dev/null || break
    echo "== $(date +%H:%M:%S)" >> "$OUT/states.timeline.txt"
    for t in /proc/$P/task/*; do
      echo "$(basename "$t") $(cat "$t/comm" 2>/dev/null) state=$(awk '{print $3}' "$t/stat" 2>/dev/null) wchan=$(cat "$t/wchan" 2>/dev/null)"
    done >> "$OUT/states.timeline.txt"
    sleep 15
  done
) &
SAMP=$!

# --- wait ready (up to 15 min); a boot-registration wedge falls through to capture
READY=1
for i in $(seq 1 180); do
  curl -s -m 2 http://127.0.0.1:8009/v1/models >/dev/null 2>&1 && { READY=0; break; }
  kill -0 "$BOOTPID" 2>/dev/null || break
  sleep 5
done
EPID=$(resolve_epid)
log "ready=$READY engine_pid=$EPID"

WEDGE_CLASS=""
if [ "$READY" -ne 0 ]; then
  WEDGE_CLASS="boot-registration"
else
  dump_kfd "$OUT/kfd.before"
  BODY=$(python3 - "$OUT/prompt.txt" <<'PY'
import json, sys
print(json.dumps({"messages": [{"role": "user", "content": open(sys.argv[1]).read()}], "max_tokens": 192}))
PY
)
  TIMEOUTS=0; N=0; T0=$SECONDS
  while :; do
    [ $((SECONDS-T0)) -gt 2700 ] && { WEDGE_CLASS="drive-timeout-none"; break; }
    [ $N -ge 120 ] && { WEDGE_CLASS="none-baseline"; break; }
    N=$((N+1))
    ANS=$(curl -s -m 170 -X POST http://127.0.0.1:8009/v1/chat/completions \
      -H 'content-type: application/json' -d "$BODY" 2>/dev/null)
    RC=$?
    log "req#$N rc=$RC len=${#ANS} logsize=$(stat -c %s "$LOG")"
    if [ $RC -eq 0 ]; then
      TIMEOUTS=0
      case "$ANS" in *verify*) log "req#$N engine-ERR-verify (released class)";; esac
      sleep 1
      continue
    fi
    TIMEOUTS=$((TIMEOUTS+1))
    S1=$(stat -c %s "$LOG"); sleep 10; S2=$(stat -c %s "$LOG")
    if [ "$S1" = "$S2" ] || [ $TIMEOUTS -ge 3 ]; then WEDGE_CLASS="request"; break; fi
    log "req#$N timed out but engine log advancing ($S1->$S2) — retry"
  done
fi
log "WEDGE class=$WEDGE_CLASS — capturing forensics"

kill "$SAMP" 2>/dev/null
EPID=$(resolve_epid)
{
  echo "=== diag267 forensics $(date) class=$WEDGE_CLASS pid=$EPID ==="
  echo "--- threads: tid comm state wchan syscall"
  for t in /proc/$EPID/task/*; do
    echo "$(basename "$t") $(cat "$t/comm" 2>/dev/null) state=$(awk '{print $3}' "$t/stat" 2>/dev/null) wchan=$(cat "$t/wchan" 2>/dev/null) syscall=$(cut -c1-30 "$t/syscall" 2>/dev/null)"
  done
  echo "--- kfd sysfs:"
  cat "$OUT/kfd.before" 2>/dev/null
  echo "--- dmesg tail:"
  dmesg 2>&1 | tail -30
  echo "--- engine trace tail:"
  tail -c 4000 "$LOG" | tr -d '\0'
} > "$OUT/wedge_forensics.txt" 2>&1
dump_kfd "$OUT/kfd.wedge"

# --- cleanup watchdog: final KFD snapshot + kill at +300 s
(
  sleep 300
  P=$(pgrep -n -x strata-host || pgrep -n -f 'v0235p0' || true)
  [ -n "$P" ] && { kill -9 "$P" 2>/dev/null; pkill -9 -P "$P" 2>/dev/null; }
  kill -9 "$BOOTPID" 2>/dev/null; kill "$SAMP" 2>/dev/null
  echo "[$(ts)] watchdog: engine killed" >> "$DRV"
) &

# --- all-thread stacks: exec trick makes the engine gdb's child (yama-legal)
EPID=$(resolve_epid)
log "exec gdb -> wedge_stacks.txt (pid $EPID)"
exec gdb -q -batch -p "$EPID" \
  -ex 'set pagination off' \
  -ex 'info threads' \
  -ex 'thread apply all bt' \
  > "$OUT/wedge_stacks.txt" 2>&1
