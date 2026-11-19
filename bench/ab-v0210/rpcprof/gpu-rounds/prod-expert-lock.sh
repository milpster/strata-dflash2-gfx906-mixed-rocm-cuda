#!/usr/bin/env bash
# Start the STANDING expert-page lock overlay on prod (operator promotion 2026-11-19).
# Locks resident expert extents of UD-IQ3_XXS shards 00002/00003, floor 4096 MiB
# MemAvailable, refresh every 120 s (locks newly resident pages; releases newest
# extents if avail falls below floor - game coexistence safety).
# Stop with: prod-expert-lock-stop.sh   (never kill by pattern)
set -uo pipefail
R=/mnt/srcds/Strata
G=$R/bench/ab-v0210/rpcprof/gpu-rounds
S2=/mnt/srcds/ai/Qwen3.8-Flash-Next-UD-IQ3_XXS-00002-of-00004.gguf
S3=/mnt/srcds/ai/Qwen3.8-Flash-Next-UD-IQ3_XXS-00003-of-00004.gguf
FLOOR=4096
REFRESH=120
HOLD=$R/run/prod-expert-lock.hold
STATE=$R/run/prod-expert-lock.json
TS=$(date +%Y%m%d-%H%M%S)
LOG=$R/logs/prod-expert-lock-$TS.log

if [ -e "$HOLD" ]; then echo "refusing: $HOLD exists (locker already running?)"; exit 1; fi
if [ -e "$STATE" ]; then
  oldpid=$(python3 -c "import json;print(json.load(open('$STATE')).get('pid',0))" 2>/dev/null || echo 0)
  if [ "$oldpid" != "0" ] && [ -d "/proc/$oldpid" ]; then
    echo "refusing: state file says pid $oldpid is alive; stop it first with prod-expert-lock-stop.sh"; exit 1
  fi
fi

setsid nohup python3 "$G/expert_shard_locker.py" --files "$S2,$S3" \
  --floor-mib "$FLOOR" --refresh-s "$REFRESH" --hold-file "$HOLD" > "$LOG" 2>&1 &
PID=$!
python3 - "$PID" "$LOG" "$FLOOR" "$REFRESH" "$STATE" << 'EOF'
import json, sys
pid, log, floor, refresh, state = int(sys.argv[1]), sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
json.dump({"pid": pid, "log": log, "floor_mib": floor, "refresh_s": refresh,
           "files": "UD-IQ3_XXS 00002,00003", "started": __import__('time').strftime('%Y-%m-%d %H:%M:%S')},
          open(state, "w"), indent=2)
EOF

t0=$SECONDS
until tr -d '\0' < "$LOG" 2>/dev/null | grep -aq 'LOCKED-SETTLED'; do
  if [ $((SECONDS - t0)) -ge 300 ]; then
    echo "locker did not settle in 300 s; stopping it"; rm -f "$HOLD"; sleep 35; exit 1
  fi
  sleep 3
done
echo "standing expert lock active (pid $PID, floor ${FLOOR} MiB, refresh ${REFRESH}s)"
grep -a 'LOCKED-SETTLED' "$LOG"
grep -E '^(MemAvailable|Mlocked):' /proc/meminfo
echo "log: $LOG"
echo "stop: $G/prod-expert-lock-stop.sh"
