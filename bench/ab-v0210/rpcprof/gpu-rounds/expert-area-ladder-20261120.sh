#!/usr/bin/env bash
# Defined-area expert-lock LADDER window (operator request 2026-11-20): a defined
# 55 GiB lock area, filled stepwise with the real expert content, TG + memory
# measured at every coverage level, floor-guarded (4096 MiB), no refresh ratchet.
# On this 62 GiB box the ladder will stop BELOW 55 GiB while the engine serves —
# the ceiling step is a measurement, and the dose-response curve is the deliverable.
# Runs on LIVE prod (:8009) — no restart, no downtime; every step releases cleanly.
# Evidence: bench/ab-v0210/rpcprof/out/expert-area-ladder-20261120/
set -uo pipefail
R=/mnt/srcds/Strata
G=$R/bench/ab-v0210/rpcprof/gpu-rounds
OUT=$R/bench/ab-v0210/rpcprof/out/expert-area-ladder-20261120
S2=/mnt/srcds/ai/Qwen3.8-Flash-Next-UD-IQ3_XXS-00002-of-00004.gguf
S3=/mnt/srcds/ai/Qwen3.8-Flash-Next-UD-IQ3_XXS-00003-of-00004.gguf
FLOOR=4096
PRESS_TARGET=7000
mkdir -p "$OUT"
ENG=$(python3 -c "import json;print(json.load(open('$R/run/prod-swift15-f16kv-mixed-supervisor.json'))['engine_pid'])")
echo "ladder start $(date +%T) engine=$ENG defined_area=55GiB floor=${FLOOR}MiB" | tee -a "$OUT/ladder.log"

snap() {
  { echo "--- $1 $(date +%T)"
    grep -E '^(MemAvailable|Mlocked|SwapFree|Cached):' /proc/meminfo
    awk -v p="$ENG" '/^Rss:/{t+=$2} END{printf "engine Rss: %d MiB\n", t/1024}' /proc/$ENG/smaps 2>/dev/null
    python3 "$G/file_residency.py" "$ENG" --match=IQ3_XXS-0000; } >> "$OUT/memory.log" 2>&1
}

# Step 0: unlocked baseline
snap L0-unlocked
bash "$G/prod_judge.sh" L0-0gb-unlocked 4 2>&1 | tee -a "$OUT/probe.log"

CEILING_REACHED=0
for STEP_MIB in 4096 8192 12288 16384 20480 24576 28672 32768 36864 40960 45056 49152 56320; do
  STEP_GB=$((STEP_MIB / 1024))
  # refuse to run a step that cannot respect the floor before it starts
  AVAIL=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)
  if [ $((AVAIL - STEP_MIB)) -lt $FLOOR ]; then
    echo "step ${STEP_GB}g: pre-check avail ${AVAIL} MiB cannot host it above floor — CEILING" | tee -a "$OUT/ladder.log"
    CEILING_REACHED=1
    break
  fi
  rm -f "$OUT/step.hold"
  python3 "$G/expert_shard_locker.py" --files "$S2,$S3" --floor-mib $FLOOR \
    --budget-mib $STEP_MIB --hold-file "$OUT/step.hold" > "$OUT/locker-${STEP_GB}g.log" 2>&1 &
  LOCKER=$!
  ok=""
  for i in $(seq 1 90); do
    tr -d '\0' < "$OUT/locker-${STEP_GB}g.log" 2>/dev/null | grep -aq 'LOCKED-SETTLED' && { ok=1; break; }
    sleep 2
  done
  if [ -z "$ok" ]; then
    echo "step ${STEP_GB}g: locker did not settle — releasing, CEILING" | tee -a "$OUT/ladder.log"
    rm -f "$OUT/step.hold"; wait $LOCKER 2>/dev/null; CEILING_REACHED=1; break
  fi
  GOT=$(tr -d '\0' < "$OUT/locker-${STEP_GB}g.log" | grep -ao 'LOCKED-SETTLED total=[0-9.]*' | tail -1 | cut -d= -f2)
  echo "step ${STEP_GB}g: locked ${GOT} MiB (budget ${STEP_MIB})" | tee -a "$OUT/ladder.log"
  if awk -v got="${GOT:-0}" -v bud="$STEP_MIB" 'BEGIN{exit !(got < bud - 512)}'; then
    echo "step ${STEP_GB}g: floor guard capped the fill below budget — CEILING step" | tee -a "$OUT/ladder.log"
    snap "L${STEP_GB}g-ceiling"
    bash "$G/prod_judge.sh" "L${STEP_GB}g-ceiling" 4 2>&1 | tee -a "$OUT/probe.log"
    rm -f "$OUT/step.hold"; wait $LOCKER 2>/dev/null
    CEILING_REACHED=1
    break
  fi
  snap "L${STEP_GB}g"
  bash "$G/prod_judge.sh" "L${STEP_GB}g" 4 2>&1 | tee -a "$OUT/probe.log"
  rm -f "$OUT/step.hold"; wait $LOCKER 2>/dev/null
  sleep 10
done

# Pressure leg at the ceiling coverage (only if a ceiling was found; engine serving)
if [ "$CEILING_REACHED" = "1" ]; then
  CEIL_GB=$(ls -t "$OUT"/locker-*g.log 2>/dev/null | head -1 | sed 's/.*locker-\([0-9]*\)g.log/\1/')
  if [ -n "$CEIL_GB" ]; then
    rm -f "$OUT/step.hold" "$OUT/press.hold"
    python3 "$G/expert_shard_locker.py" --files "$S2,$S3" --floor-mib $FLOOR \
      --budget-mib $((CEIL_GB * 1024)) --hold-file "$OUT/step.hold" > "$OUT/locker-ceiling-hold.log" 2>&1 &
    LOCKER=$!
    for i in $(seq 1 90); do
      tr -d '\0' < "$OUT/locker-ceiling-hold.log" 2>/dev/null | grep -aq 'LOCKED-SETTLED' && break
      sleep 2
    done
    python3 "$G/ram_pressure.py" --target-mib $PRESS_TARGET --cap-mib 40000 --hold-file "$OUT/press.hold" > "$OUT/pressure-ceiling.log" 2>&1 &
    PRESSURE=$!
    touch "$OUT/press.hold"
    t0=$SECONDS
    while [ "$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)" -gt $((PRESS_TARGET + 200)) ]; do
      [ $((SECONDS - t0)) -ge 420 ] && break; sleep 3
    done
    echo "ceiling+pressure: avail=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo) MiB after $((SECONDS - t0))s" | tee -a "$OUT/ladder.log"
    bash "$G/prod_judge.sh" "L${CEIL_GB}g-press" 6 2>&1 | tee -a "$OUT/probe.log"
    rm -f "$OUT/press.hold"; wait $PRESSURE 2>/dev/null
    rm -f "$OUT/step.hold"; wait $LOCKER 2>/dev/null
  fi
fi

sleep 10
snap FINAL
echo "=== ladder complete $(date +%T); Mlocked=$(awk '/^Mlocked:/{print int($2/1024)}' /proc/meminfo) MiB ===" | tee -a "$OUT/ladder.log"
grep -h ': .* tok/s' "$OUT/probe.log" || true
