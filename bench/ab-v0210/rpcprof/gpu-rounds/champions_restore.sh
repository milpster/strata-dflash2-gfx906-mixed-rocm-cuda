#!/usr/bin/env bash
# champions_restore.sh — put canonical production back, and say so honestly.
#
# Boots /mnt/srcds/Strata/launch-v0238.sh, i.e. the ORIGINAL on-disk
# prod-config-v0238.json plus the adopted b0ec engine. It never edits the config and
# never refreshes the production cache inventory.
#
# The boot gate reads the exact engine-log path out of the launcher's own stdout
# ("Starting mixed backend ... log: ...") instead of globbing logs/prod-*2026111*.log:
# a date-prefix glob happily passes on yesterday's stale log and then reports
# "RESTORED" against a dead stack.
#
# That announcement is only a hint, though, because the launcher does not own the pair:
# launch-v0238.sh boots it through tools/mixed_gpu/pair_supervisor.py, and when a
# generation fails to boot the supervisor retries it as a NEW generation with a NEW engine
# log. Latching onto the first announcement and then waiting on that one file timed out at
# 17:02 on 2026-11-18 while the retry's generation 2 was already serving. So acceptance
# re-resolves the current pair on every poll from the supervisor's own state file, and a
# log the supervisor no longer runs cannot satisfy it.
#
# Acceptance is bound to what the supervisor SELECTED, not to the first engine-shaped
# process found in /proc: the state file names this generation's engine PID, its /proc
# start time (which makes a recycled PID impossible to pass for it), and its log. The
# running image is hashed through /proc/<pid>/exe, so a correct file on disk whose bytes
# are NOT what is serving cannot pass. Verdicts come from champions_restore_verify.py,
# kept separate so the acceptance rules can be tested offline.
#
#   usage: champions_restore.sh [--health] [--timeout SECONDS] [--poll SECONDS]
#                               [--engine-sha SHA256] [--out DIR]
#
# --health adds one bounded finite-output generation check. Only the parent passes it,
# and only when it is actually executing the panel.
# --engine-sha re-points the pinned image hash (default b0ec0109…, the adopted v0238p0
# engine). It changes only what is ACCEPTED: this script never copies, installs or
# restores a candidate into production.
# --out writes this restore's own logs and captured HTTP responses somewhere else, so a
# rerun or a test cannot land in frozen measurement evidence.
# PROC_ROOT is the offline test seam for the CPU suite; production leaves it unset. It
# only ever redirects reads of /proc and grants no power over any process.
#
# Exits 0 only after, for one and the same generation: the supervisor declares its engine
# live and ready, that PID's running image is the pinned engine, /v1/models completed with
# curl and answered 2xx with a real model list, and the completion check completed with
# curl, answered 2xx and produced a real finite generation when --health asked for one. A
# response file on disk proves nothing on its own, so the transport is checked as well.
set -uo pipefail
R=/mnt/srcds/Strata
HERE=$(cd "$(dirname "$0")" && pwd)
VERIFY=$HERE/champions_restore_verify.py
OUT=$R/bench/ab-v0210/rpcprof/out/champions-20261118
FROZEN_CONFIG_SHA=95ef3693b3f598e6bdbe534ef7a9b195e13b9e1705acc0cad2d414db09f21852
ENGINE_SHA=b0ec0109ebc50c982512c371d81b2c15a8f72c70edf8a461a4ff10337da52f02
TIMEOUT=1800 HEALTH=0 POLL=20
PROC_ROOT=${PROC_ROOT:-/proc}

usage() {
  echo "usage: $0 [--health] [--timeout SECONDS] [--poll SECONDS] [--engine-sha SHA256] [--out DIR]" >&2
}
positive_int() {
  case "${1:-}" in ''|*[!0-9]*) return 1 ;; *) ;; esac
  [ "$1" -gt 0 ]
}

# flag values are validated before anything here could stop or boot a pair. Every option
# that takes a value must see one: shifting past the end does not shift at all, so an
# unguarded `shift 2` on a trailing flag would spin here forever instead of reporting.
while [ $# -gt 0 ]; do
  case "$1" in
    --health) HEALTH=1; shift ;;
    --timeout|--poll|--engine-sha|--out)
      if [ $# -lt 2 ]; then usage; echo "restore: $1 wants a value" >&2; exit 2; fi
      case "$1" in
        --timeout) TIMEOUT=$2 ;;
        --poll) POLL=$2 ;;
        --engine-sha) ENGINE_SHA=$2 ;;
        --out) OUT=$2 ;;
      esac
      shift 2 ;;
    *) usage; echo "restore: unknown argument $1" >&2; exit 2 ;;
  esac
done
positive_int "$TIMEOUT" || { usage; echo "restore: --timeout wants a positive integer of seconds" >&2; exit 2; }
positive_int "$POLL" || { usage; echo "restore: --poll wants a positive integer of seconds" >&2; exit 2; }
case "$ENGINE_SHA" in ''|*[!0-9a-fA-F]*) ENGINE_SHA= ;; esac
[ ${#ENGINE_SHA} -eq 64 ] || { usage; echo "restore: --engine-sha wants 64 hex digits" >&2; exit 2; }
ENGINE_SHA=$(printf '%s' "$ENGINE_SHA" | tr 'A-F' 'a-f')
[ -n "$OUT" ] || { usage; echo "restore: --out wants a directory" >&2; exit 2; }
[ -f "$VERIFY" ] || { echo "RESTORE-FAIL: acceptance verifier $VERIFY is absent"; exit 2; }
mkdir -p "$OUT/logs" || { echo "restore: cannot create $OUT/logs" >&2; exit 2; }

ENGINE=$R/v0238p0/rpcw-hip/strata
STATE=$R/run/prod-swift15-f16kv-mixed-supervisor.json
BASE=http://127.0.0.1:8009
BOOT=$OUT/logs/restore-$(date +%Y%m%d-%H%M%S)-$$.out
MODELS_FILE=$OUT/logs/restore-models-$$.json
HEALTH_FILE=$OUT/logs/restore-health-$$.json

# stop through the wrapper so this script's own argv never carries a swept pattern.
# BOTH conditions are required: a zero exit and the ALL STOPPED verdict. Booting another
# 60 GiB pair onto a box that still holds one is the failure mode to avoid hardest.
bash "$HERE/champions_stop.sh" >"$OUT/logs/restore-stop-$$.log" 2>&1
STOP_CODE=$?
tail -2 "$OUT/logs/restore-stop-$$.log"
if [ "$STOP_CODE" -ne 0 ] || ! grep -q 'ALL STOPPED' "$OUT/logs/restore-stop-$$.log"; then
  echo "RESTORE-FAIL: stop-all was not clean (exit $STOP_CODE); refusing to launch another pair"
  exit 1
fi

echo "restore: config sha $(sha256sum "$R/prod-config-v0238.json" | cut -c1-64)"
[ "$(sha256sum "$R/prod-config-v0238.json" | cut -c1-64)" = "$FROZEN_CONFIG_SHA" ] \
  || echo "restore: WARNING on-disk config no longer matches the frozen production config"
echo "restore: engine on-disk sha $(sha256sum "$ENGINE" 2>/dev/null | cut -c1-64)"
echo "restore: the serving image must hash to $ENGINE_SHA"

( cd "$R" && setsid --fork bash "$R/launch-v0238.sh" >"$BOOT" 2>&1 < /dev/null )

DEADLINE=$(( $(date +%s) + TIMEOUT ))

# First announcement only: it proves the launcher got as far as naming a pair, and its
# refusal lines are worth reading now rather than after the whole budget. Readiness below
# does not wait on this file; it follows whichever generation the supervisor runs.
LOG=""
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  LOG=$(sed -n 's/^Starting mixed backend on .*log: \([^;[:space:]]*\); helper:.*/\1/p' "$BOOT" 2>/dev/null | tail -1)
  [ -n "$LOG" ] && break
  # the launcher refuses loudly; do not sit on the full budget when it already did
  if grep -qa 'REFUSING\|Missing executable\|readiness timed out\|Usage:' "$BOOT" 2>/dev/null; then
    echo "RESTORE-FAIL: launcher refused to start ($(date +%T)); see $BOOT"; tail -5 "$BOOT"; exit 1
  fi
  sleep 5
done
if [ -z "$LOG" ]; then echo "RESTORE-FAIL: launcher never reported its log path ($(date +%T)); see $BOOT"; exit 1; fi
echo "restore: launcher announced $LOG"

IDENTITY=""
pick() { printf '%s\n' "$2" | sed -n "s/^$1=//p" | tail -1; }
field() { pick "$1" "$IDENTITY"; }
# the verdict's own reason line, so the restore log explains itself; if the verifier said
# nothing recognisable, the last thing it did say is better than reporting nothing
refuse() {
  local line
  line=$(printf '%s\n' "$2" | grep -a "^$1-FAIL" | tail -1)
  printf '%s' "${line:-$(printf '%s\n' "$2" | grep -av '^[[:space:]]*$' | tail -1)}"
}

verify_identity() {
  # $1: the generation token an earlier step accepted; "" to accept whichever is current
  IDENTITY=$(python3 "$VERIFY" identity --state "$STATE" --engine "$ENGINE" \
             --expected-sha "$ENGINE_SHA" --log "$BOOT" --proc-root "$PROC_ROOT" \
             --expect "$1" 2>&1)
  ID_CODE=$?
}

# One pass accepts one generation from first look to last. Anything failing inside it
# discards that token: a result measured across two generations is not evidence of a
# serving pair, which is precisely how 2026-11-18 got away with reporting a timeout.
accepted_pass() {
  verify_identity "$TOKEN"
  if [ "$ID_CODE" -eq 3 ]; then FATAL=$(refuse IDENTITY "$IDENTITY"); return 1; fi
  if [ "$ID_CODE" -ne 0 ]; then PROBLEM=$(refuse IDENTITY "$IDENTITY"); return 1; fi
  local token ready generation log pid sha
  token=$(field token)
  ready=$(field ready)
  generation=$(field generation)
  log=$(field log)
  pid=$(field pid)
  sha=$(field sha)
  if [ "$log" != "$SEEN" ]; then
    echo "restore: following generation $generation, engine pid $pid, engine log $log"
    SEEN=$log
  fi

  # a stored body is not an answer: the request has to have completed and come back 2xx
  local curl_rc=0 status="" models_out
  status=$(curl -s -o "$MODELS_FILE" -w '%{http_code}' -m 10 "$BASE/v1/models" 2>/dev/null) \
    || curl_rc=$?
  if [ "$curl_rc" -ne 0 ]; then
    PROBLEM="MODELS-FAIL the models request did not complete (curl exit $curl_rc)"
    return 1
  fi
  models_out=$(python3 "$VERIFY" models --status "${status:-0}" --json "$MODELS_FILE" 2>&1)
  if [ $? -ne 0 ]; then PROBLEM=$(refuse MODELS "$models_out"); return 1; fi
  echo "restore: gen $generation accepted: pid $pid image $sha, models $(pick model_ids "$models_out")"

  # the pair must still be the very one we started from, or the answer is not this pair's
  verify_identity "$token"
  if [ "$ID_CODE" -ne 0 ]; then PROBLEM=$(refuse IDENTITY "$IDENTITY"); return 1; fi

  if [ "$HEALTH" = 1 ]; then
    # same rule for the completion: exit status, HTTP status and body must all agree
    local health_rc=0 health_status="" health_out
    health_status=$(curl -s -o "$HEALTH_FILE" -w '%{http_code}' -m 240 \
      -H 'Content-Type: application/json' \
      -d '{"model":"ud-iq3_xxs","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":64,"temperature":0}' \
      "$BASE/v1/chat/completions" 2>/dev/null) || health_rc=$?
    if [ "$health_rc" -ne 0 ]; then
      PROBLEM="HEALTH-FAIL the completion request did not complete (curl exit $health_rc)"
      return 1
    fi
    health_out=$(python3 "$VERIFY" health --status "${health_status:-0}" --json "$HEALTH_FILE" 2>&1)
    if [ $? -ne 0 ]; then PROBLEM=$(refuse HEALTH "$health_out"); return 1; fi
    echo "restore: gen $generation health: $(pick finish "$health_out") after $(pick completion_tokens "$health_out") tokens"
    verify_identity "$token"
    if [ "$ID_CODE" -ne 0 ]; then PROBLEM=$(refuse IDENTITY "$IDENTITY"); return 1; fi
  fi

  TOKEN=$token
  READY=$ready
  GEN=$generation
  LIVE_PID=$pid
  LIVE_SHA=$sha
  return 0
}

FATAL="" PROBLEM="" TOKEN="" READY="" GEN="" LIVE_PID="" LIVE_SHA="" SEEN="" TICK=0

# One finite deadline for the whole acceptance — readiness, the models answer and the
# completion share it. A supervisor retry that lands late still gets through; a stack that
# never comes up still cannot hang here forever.
while :; do
  if accepted_pass; then break; fi
  if [ -n "$FATAL" ]; then echo "RESTORE-FAIL: $FATAL"; exit 1; fi
  TOKEN=""
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then break; fi
  sleep "$POLL"
  TICK=$(( TICK + 1 ))
  if [ $(( TICK % 3 )) -eq 0 ]; then
    echo "restore: still accepting, $(( (DEADLINE - $(date +%s)) / 60 )) min of budget left${PROBLEM:+ ($PROBLEM)}"
  fi
done

if [ -z "$READY" ]; then
  echo "RESTORE-FAIL: no generation was accepted before the deadline ($(date +%T)); ${PROBLEM:-see $BOOT}"
  exit 1
fi
echo "RESTORED: $READY ($(date +%T)) gen $GEN engine pid $LIVE_PID image $LIVE_SHA"
