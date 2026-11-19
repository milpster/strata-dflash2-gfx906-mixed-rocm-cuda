#!/usr/bin/env bash
# Stop the standing expert-page lock overlay: remove hold file, wait for a clean
# self-exit (munlockall), verify Mlocked dropped. Never kill by pattern.
set -uo pipefail
R=/mnt/srcds/Strata
G=$R/bench/ab-v0210/rpcprof/gpu-rounds
HOLD=$R/run/prod-expert-lock.hold
STATE=$R/run/prod-expert-lock.json

PID=$(python3 -c "import json;print(json.load(open('$STATE')).get('pid',0))" 2>/dev/null || echo 0)
if [ "$PID" = "0" ] || [ ! -d "/proc/$PID" ]; then
  echo "no live locker (state pid=$PID); cleaning files"; rm -f "$HOLD"; mv "$STATE" "${STATE%.json}.stopped-$(date +%Y%m%d-%H%M%S)" 2>/dev/null; exit 0
fi
echo "stopping locker pid $PID (hold-file release)"
rm -f "$HOLD"
t0=$SECONDS
while [ -d "/proc/$PID" ] && [ $((SECONDS - t0)) -lt 90 ]; do sleep 3; done
if [ -d "/proc/$PID" ]; then
  echo "locker still alive after 90 s; sending SIGTERM"
  kill -TERM "$PID"
  t0=$SECONDS
  while [ -d "/proc/$PID" ] && [ $((SECONDS - t0)) -lt 60 ]; do sleep 3; done
fi
if [ -d "/proc/$PID" ]; then echo "ERROR: locker pid $PID still alive; inspect manually"; exit 1; fi
mv "$STATE" "${STATE%.json}.stopped-$(date +%Y%m%d-%H%M%S)" 2>/dev/null
echo "locker stopped cleanly; verifying"
grep -E '^(MemAvailable|Mlocked):' /proc/meminfo
ML=$(awk '/^Mlocked:/{print int($2/1024)}' /proc/meminfo)
[ "$ML" -lt 512 ] && echo "OK: Mlocked back to ${ML} MiB" || { echo "WARNING: Mlocked still ${ML} MiB"; exit 1; }
