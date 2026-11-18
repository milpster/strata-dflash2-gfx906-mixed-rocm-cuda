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
#   usage: champions_restore.sh [--health] [--timeout SECONDS]
#
# --health adds one bounded finite-output generation check. Only the parent passes it,
# and only when it is actually executing the panel.
# Exits 0 only on a verified fresh ready line plus a verified engine identity.
set -uo pipefail
R=/mnt/srcds/Strata
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$R/bench/ab-v0210/rpcprof/out/champions-20261118
FROZEN_CONFIG_SHA=0d9fb58b7d1dd46df002702f2d67756be6efefc101c24de469ebf9e5a1609811
TIMEOUT=1800 HEALTH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --health) HEALTH=1; shift ;;
    --timeout) TIMEOUT=$2; shift 2 ;;
    *) echo "usage: $0 [--health] [--timeout SECONDS]" >&2; exit 2 ;;
  esac
done
mkdir -p "$OUT/logs"
BOOT=$OUT/logs/restore-$(date +%Y%m%d-%H%M%S)-$$.out

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
echo "restore: engine sha $(sha256sum "$R/v0238p0/rpcw-hip/strata" | cut -c1-64)"

( cd "$R" && setsid --fork bash "$R/launch-v0238.sh" >"$BOOT" 2>&1 < /dev/null )

# the launcher names its own engine log; take it from there, never from a glob
LOG=""
DEADLINE=$(( $(date +%s) + TIMEOUT ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  LOG=$(sed -n 's/^Starting mixed backend on .*log: \([^ ]*\) .*/\1/p' "$BOOT" 2>/dev/null | tail -1)
  [ -n "$LOG" ] && break
  # the launcher refuses loudly; do not sit on the full budget when it already did
  if grep -qa 'REFUSING\|Missing executable\|readiness timed out\|Usage:' "$BOOT" 2>/dev/null; then
    echo "RESTORE-FAIL: launcher refused to start ($(date +%T)); see $BOOT"; tail -5 "$BOOT"; exit 1
  fi
  sleep 5
done
if [ -z "$LOG" ]; then echo "RESTORE-FAIL: launcher never reported its log path ($(date +%T)); see $BOOT"; exit 1; fi
echo "restore: engine log $LOG"

READY=""
TICK=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  READY=$(tr -d '\0' < "$LOG" 2>/dev/null | grep -a 'ready: http://' | tail -1)
  [ -n "$READY" ] && break
  sleep 20
  TICK=$(( TICK + 1 ))
  [ $(( TICK % 3 )) -eq 0 ] && echo "restore: still booting, $(( (DEADLINE - $(date +%s)) / 60 )) min of budget left"
done
if [ -z "$READY" ]; then echo "RESTORE-FAIL: no ready line before the deadline ($(date +%T)); see $LOG"; exit 1; fi

# the port must actually answer, not just have printed a ready line once
MODELS=""
for i in $(seq 1 12); do
  MODELS=$(curl -s -m 10 http://127.0.0.1:8009/v1/models 2>/dev/null | head -c 400)
  [ -n "$MODELS" ] && break
  sleep 5
done
if [ -z "$MODELS" ]; then echo "RESTORE-FAIL: ready line printed but /v1/models never answered ($(date +%T))"; exit 1; fi

# boot identity: the running engine must BE the adopted production binary
IDENT=$(python3 - "$R/v0238p0/rpcw-hip/strata" <<'PY'
import os, sys
want = os.path.realpath(sys.argv[1])
for d in os.listdir("/proc"):
    if not d.isdigit():
        continue
    try:
        cmd = open(f"/proc/{d}/cmdline", "rb").read()
    except OSError:
        continue
    if cmd.split(b"\x00", 1)[0].endswith(b"/strata") and b"\x00--serve\x00" in cmd:
        try:
            exe = os.path.realpath(os.readlink(f"/proc/{d}/exe"))
        except OSError:
            continue
        print(f"{'OK' if exe == want else 'MISMATCH'} pid={d} exe={exe} want={want}")
        break
else:
    print(f"NO-ENGINE exe=none want={want}")
PY
)
# RESTORED is printed only after everything asked of this restore has actually succeeded,
# including the generation check when --health was requested.
if [ "${IDENT%% *}" != "OK" ]; then echo "RESTORE-FAIL: engine identity $IDENT"; exit 1; fi
if [ "$HEALTH" = 1 ]; then
  HEALTH=$(curl -s -m 240 http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' \
    -d '{"model":"ud-iq3_xxs","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":64,"temperature":0}')
  if ! echo "$HEALTH" | python3 -c '
import json, sys
try:
    body = json.load(sys.stdin)
    choice = body["choices"][0]
    message = choice["message"]
    text = (message.get("content") or message.get("reasoning_content") or "").strip()
    finish = choice.get("finish_reason")
    tokens = int((body.get("usage") or {}).get("completion_tokens", 0))
except Exception as error:
    raise SystemExit(f"unparseable health response: {error}")
if not text:
    raise SystemExit("health generation returned no text")
if not finish:
    raise SystemExit("health generation has no finish_reason")
if tokens <= 0:
    raise SystemExit(f"health generation reported {tokens} completion tokens")
print(f"restore health: {text[:24]!r} finish={finish} completion_tokens={tokens}")
'; then
    echo "RESTORE-FAIL: ready, but the health generation was not a valid finite response ($(date +%T))"
    exit 1
  fi
fi
echo "RESTORED: $READY ($(date +%T)) $IDENT"
