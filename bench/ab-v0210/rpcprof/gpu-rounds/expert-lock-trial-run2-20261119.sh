#!/usr/bin/env bash
# Expert-page lock trial RUN 2 on LIVE prod — fixes run-1 harness bugs:
#   (a) ram_pressure hold-file is now created (holders STAY ALIVE during probe legs;
#       in run 1 they exited at cap and released before any probe ran),
#   (b) cap raised 24576 -> 40000 MiB and target 4500 -> 7000 MiB (engine RAM relief
#       absorbs ~everything above ~7 GiB; 4.5 GiB was unreachable synthetically).
# Legs: E0 anchor nolock (3 probes) -> lock -> E1 lock-nopress (3) -> E2 lock+pressure
#       held (6) -> unlock -> E3 nolock+pressure held (6) -> cleanup.
# Evidence: bench/ab-v0210/rpcprof/out/expert-lock-trial-20261119-run2/
set -uo pipefail
R=/mnt/srcds/Strata
G=$R/bench/ab-v0210/rpcprof/gpu-rounds
OUT=$R/bench/ab-v0210/rpcprof/out/expert-lock-trial-20261119-run2
S2=/mnt/srcds/ai/Qwen3.8-Flash-Next-UD-IQ3_XXS-00002-of-00004.gguf
S3=/mnt/srcds/ai/Qwen3.8-Flash-Next-UD-IQ3_XXS-00003-of-00004.gguf
PRESS_TARGET=7000
PRESS_CAP=40000
mkdir -p "$OUT"

ENG=$(python3 -c "import json;print(json.load(open('$R/run/prod-swift15-f16kv-mixed-supervisor.json'))['engine_pid'])")
HLP=$(python3 -c "import json;print(json.load(open('$R/run/prod-swift15-f16kv-mixed-supervisor.json'))['helper_pid'])")
echo "run2 start $(date +%T) engine=$ENG helper=$HLP target=${PRESS_TARGET}MiB cap=${PRESS_CAP}MiB" | tee -a "$OUT/trial.log"

snap() {
  { echo "--- snap $1 $(date +%T)"
    grep -E '^(MemAvailable|Mlocked|SwapFree|SwapTotal|Cached):' /proc/meminfo
    for p in "$ENG" "$HLP"; do
      awk -v p="$p" '/^Rss:/{t+=$2} END{printf "pid %s total Rss: %d MiB\n", p, t/1024}' /proc/$p/smaps 2>/dev/null
    done
    python3 "$G/file_residency.py" "$ENG" "$HLP" --match=IQ3_XXS-0000; } >> "$OUT/memory.log" 2>&1
  tail -14 "$OUT/memory.log"
}

leg() { # leg <label> <n_probes>
  echo "=== LEG $1 $(date +%T) ===" | tee -a "$OUT/trial.log"
  snap "$1"
  bash "$G/prod_judge.sh" "$1" "${2:-6}" 2>&1 | tee -a "$OUT/probe.log"
}

pressure_on() { # pressure_on <logname>
  rm -f "$OUT/pressure.hold"
  python3 "$G/ram_pressure.py" --target-mib $PRESS_TARGET --cap-mib $PRESS_CAP \
    --hold-file "$OUT/pressure.hold" > "$OUT/$1.log" 2>&1 &
  PRESSURE=$!
  touch "$OUT/pressure.hold"   # holder stays alive until this file disappears
  local t0=$SECONDS
  while [ "$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)" -gt $((PRESS_TARGET + 200)) ]; do
    [ $((SECONDS - t0)) -ge 480 ] && break
    sleep 3
  done
  echo "pressure: MemAvailable=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo) MiB after $((SECONDS - t0))s (holder pid $PRESSURE alive)" | tee -a "$OUT/trial.log"
}

pressure_off() {
  rm -f "$OUT/pressure.hold"
  wait $PRESSURE 2>/dev/null
  echo "pressure released ($(date +%T))" | tee -a "$OUT/trial.log"
}

# ---- E0: anchor, no lock, no pressure ----
leg E0-anchor-nolock 3

# ---- Lock resident expert extents (floor 4096 MiB) ----
rm -f "$OUT/locker.hold"
python3 "$G/expert_shard_locker.py" --files "$S2,$S3" --floor-mib 4096 \
  --hold-file "$OUT/locker.hold" > "$OUT/locker.log" 2>&1 &
LOCKER=$!
t0=$SECONDS
until tr -d '\0' < "$OUT/locker.log" 2>/dev/null | grep -aq 'LOCKED-SETTLED'; do
  if [ $((SECONDS - t0)) -ge 300 ]; then
    echo "locker FAILED to settle; aborting run2 safely" | tee -a "$OUT/trial.log"
    rm -f "$OUT/locker.hold"; wait $LOCKER 2>/dev/null; exit 1
  fi
  sleep 2
done
echo "locker settled (pid $LOCKER)" | tee -a "$OUT/trial.log"
tail -4 "$OUT/locker.log"

# ---- E1: locked, no pressure ----
leg E1-lock-nopress 3

# ---- E2: locked + pressure HELD during probes ----
pressure_on pressure
leg E2-lock-press 6
pressure_off

# ---- Unlock, settle, re-apply pressure ----
rm -f "$OUT/locker.hold"; wait $LOCKER 2>/dev/null
sleep 25
echo "unlocked; Mlocked now $(awk '/^Mlocked:/{print int($2/1024)}' /proc/meminfo) MiB" | tee -a "$OUT/trial.log"
pressure_on pressure2

# ---- E3: unlocked + pressure HELD during probes ----
leg E3-nolock-press 6
pressure_off

# ---- Final ----
sleep 15
snap FINAL
echo "=== run2 complete $(date +%T) ===" | tee -a "$OUT/trial.log"
grep -h ': .* tok/s' "$OUT/probe.log" || true
