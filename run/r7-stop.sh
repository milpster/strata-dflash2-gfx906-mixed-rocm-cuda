#!/usr/bin/env bash
# Stop the r7 attribution trial cleanly: supervisor via its state file first, then
# the launcher/engine/helper sweep. Never matches its own command line.
set -u
STATE=/mnt/srcds/Strata/run/r7-trial-supervisor.json
PY=/mnt/srcds/Strata/.venv/bin/python
if [ -f "$STATE" ]; then
  SUP=$("$PY" -c "import json;print(json.load(open('$STATE')).get('supervisor_pid',''))" 2>/dev/null)
  if [ -n "${SUP:-}" ] && kill -0 "$SUP" 2>/dev/null; then
    echo "stopping supervisor $SUP"
    kill -TERM "$SUP" 2>/dev/null
    for i in $(seq 1 15); do kill -0 "$SUP" 2>/dev/null || break; sleep 1; done
    kill -9 "$SUP" 2>/dev/null || true
  fi
fi
for pat in "launch-trial-r7.sh" "r7-trial.sock" "engine-r7a.log" "engine-r7b.log"; do
  for pid in $(pgrep -f "$pat"); do
    [ "$pid" = "$$" ] && continue
    kill -TERM "$pid" 2>/dev/null || true
  done
done
sleep 2
ss -H -ltn 'sport = :8021' && echo "8021 still up" || echo "8021 clear"
rm -f /mnt/srcds/Strata/run/r7-trial.sock /mnt/srcds/Strata/run/r7-trial-2.sock /mnt/srcds/Strata/run/r7-trial.lock
echo done
