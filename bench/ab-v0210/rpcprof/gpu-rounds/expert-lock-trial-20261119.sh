#!/usr/bin/env bash
# Expert-page lock trial on LIVE prod (:8009) — no downtime, no engine change.
# Legs: A nolock/no-pressure baseline -> B lock/no-pressure -> C lock/pressure
#       -> D nolock/pressure (same pressure target). Probes: prod_judge.sh 6x500tok.
# Evidence: bench/ab-v0210/rpcprof/out/expert-lock-trial-20261119/
set -uo pipefail
R=/mnt/srcds/Strata
G=$R/bench/ab-v0210/rpcprof/gpu-rounds
OUT=$R/bench/ab-v0210/rpcprof/out/expert-lock-trial-20261119
S2=/mnt/srcds/ai/Qwen3.8-Flash-Next-UD-IQ3_XXS-00002-of-00004.gguf
S3=/mnt/srcds/ai/Qwen3.8-Flash-Next-UD-IQ3_XXS-00003-of-00004.gguf
PRESS_TARGET=4500
mkdir -p "$OUT"

ENG=$(python3 -c "import json;print(json.load(open('$R/run/prod-swift15-f16kv-mixed-supervisor.json'))['engine_pid'])")
HLP=$(python3 -c "import json;print(json.load(open('$R/run/prod-swift15-f16kv-mixed-supervisor.json'))['helper_pid'])")
echo "trial start $(date +%T) engine=$ENG helper=$HLP pressure_target=${PRESS_TARGET}MiB" | tee -a "$OUT/trial.log"

snap() {
  { echo "--- snap $1 $(date +%T)"
    grep -E '^(MemAvailable|Mlocked|SwapFree|SwapTotal|Cached):' /proc/meminfo
    python3 "$G/file_residency.py" "$ENG" "$HLP" --match=IQ3_XXS-0000; } >> "$OUT/memory.log" 2>&1
  tail -n +1 "$OUT/memory.log" | tail -12
}

leg() { # leg <label>
  echo "=== LEG $1 $(date +%T) ===" | tee -a "$OUT/trial.log"
  snap "$1"
  bash "$G/prod_judge.sh" "$1" 6 2>&1 | tee -a "$OUT/probe.log"
}

wait_for() { # wait_for <file> <pattern> <timeout_s>
  local f=$1 pat=$2 tmo=$3 t0=$SECONDS
  until tr -d '\0' < "$f" 2>/dev/null | grep -aq "$pat"; do
    [ $((SECONDS - t0)) -ge "$tmo" ] && return 1
    sleep 2
  done
  return 0
}

# ---- Leg A: baseline, no lock, no pressure ----
leg A-base-nolock

# ---- Lock resident expert extents (floor 4096 MiB) ----
rm -f "$OUT/locker.hold"
python3 "$G/expert_shard_locker.py" --files "$S2,$S3" --floor-mib 4096 \
  --hold-file "$OUT/locker.hold" > "$OUT/locker.log" 2>&1 &
LOCKER=$!
if wait_for "$OUT/locker.log" 'LOCKED-SETTLED' 300; then
  echo "locker settled (pid $LOCKER)" | tee -a "$OUT/trial.log"
else
  echo "locker FAILED to settle; aborting trial safely" | tee -a "$OUT/trial.log"
  rm -f "$OUT/locker.hold"; wait $LOCKER 2>/dev/null; exit 1
fi
tail -5 "$OUT/locker.log"

# ---- Leg B: locked, no pressure ----
leg B-lock-nopress

# ---- Pressure ON (locked) ----
rm -f "$OUT/pressure.hold"
python3 "$G/ram_pressure.py" --target-mib $PRESS_TARGET --cap-mib 24576 \
  --hold-file "$OUT/pressure.hold" > "$OUT/pressure.log" 2>&1 &
PRESSURE=$!
t0=$SECONDS
while [ "$(awk '/^MemAvailable:/{print $2/1024; exit}' /proc/meminfo | cut -d. -f1)" -gt $((PRESS_TARGET + 150)) ]; do
  [ $((SECONDS - t0)) -ge 420 ] && break
  sleep 3
done
echo "pressure reached MemAvailable=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo) MiB after $((SECONDS - t0))s" | tee -a "$OUT/trial.log"

# ---- Leg C: locked + pressure ----
leg C-lock-press

# ---- Pressure OFF, unlock, settle ----
rm -f "$OUT/pressure.hold"; wait $PRESSURE 2>/dev/null
rm -f "$OUT/locker.hold"; wait $LOCKER 2>/dev/null
sleep 25
echo "unlocked; Mlocked now $(awk '/^Mlocked:/{print int($2/1024)}' /proc/meminfo) MiB" | tee -a "$OUT/trial.log"

# ---- Pressure ON again (unlocked) ----
rm -f "$OUT/pressure.hold"
python3 "$G/ram_pressure.py" --target-mib $PRESS_TARGET --cap-mib 24576 \
  --hold-file "$OUT/pressure.hold" > "$OUT/pressure2.log" 2>&1 &
PRESSURE=$!
t0=$SECONDS
while [ "$(awk '/^MemAvailable:/{print $2/1024; exit}' /proc/meminfo | cut -d. -f1)" -gt $((PRESS_TARGET + 150)) ]; do
  [ $((SECONDS - t0)) -ge 420 ] && break
  sleep 3
done

# ---- Leg D: unlocked + pressure ----
leg D-nolock-press

# ---- Cleanup ----
rm -f "$OUT/pressure.hold"; wait $PRESSURE 2>/dev/null
sleep 15
snap FINAL
echo "=== trial complete $(date +%T) ===" | tee -a "$OUT/trial.log"
grep -h ': .* tok/s' "$OUT/probe.log" || true
echo "probe lines also appended to out/gpu-rounds-20261116/spd-prod-judge.csv"
