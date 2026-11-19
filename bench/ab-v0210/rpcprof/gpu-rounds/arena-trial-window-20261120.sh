#!/usr/bin/env bash
# Arena-resize trial window (arm 1: relief hot 24000 / cap 28000 MiB) on :8019.
# RUN ONLY AFTER: (a) the Swift parking fixer session has ENDED (it rides prod :8009),
# (b) a fresh explicit operator go for prod downtime. This script stops prod, boots
# the trial, runs the legs, restores prod through its launcher, and verifies identity.
set -uo pipefail
R=/mnt/srcds/Strata
G=$R/bench/ab-v0210/rpcprof/gpu-rounds
OUT=$R/bench/ab-v0210/rpcprof/out/arena-resize-trial-20261120
mkdir -p "$OUT"
PRESS_TARGET=7000
echo "=== arena trial window start $(date +%T) ===" | tee -a "$OUT/window.log"

sup_pid() { python3 -c "import json;print(json.load(open('$1')).get('engine_pid',0))" 2>/dev/null || echo 0; }
snap() {
  local eng=$(sup_pid "$R/run/trial-arena24-supervisor.json")
  local prod=$(sup_pid "$R/run/prod-swift15-f16kv-mixed-supervisor.json")
  { echo "--- snap $1 $(date +%T) prod_engine=$prod trial_engine=$eng"
    grep -E '^(MemAvailable|Mlocked|SwapFree|Cached):' /proc/meminfo
    for p in "$eng" "$prod"; do
      [ "$p" != "0" ] && [ -d /proc/$p ] && awk -v p=$p '/^Rss:/{t+=$2} END{printf "engine %s Rss: %d MiB\n", p, t/1024}' /proc/$p/smaps
    done
    [ "$eng" != "0" ] && [ -d /proc/$eng ] && python3 "$G/file_residency.py" "$eng" --match=IQ3_XXS-0000; } >> "$OUT/memory.log" 2>&1
  tail -10 "$OUT/memory.log"
}

# 0) Pre-flight: prod healthy, no prod lock leftovers, config sha recorded
sha256sum "$R/trial-config-arena24-28.json" | tee -a "$OUT/window.log"
curl -s -m 8 -o /dev/null -w 'prod models pre-window: http %{http_code}\n' http://127.0.0.1:8009/v1/models | tee -a "$OUT/window.log"

# 1) Stop prod (operator-authorized downtime; stop-all also TERMs supervisors and verifies quiet)
bash "$R/stop-all.sh" 2>&1 | tail -3 | tee -a "$OUT/window.log"
sleep 8

# 2) Boot trial pair on :8019
( cd "$R" && setsid --fork "$R/launch-trial-arena24-28.sh" > "$R/logs/lane-arena24.log" 2>&1 < /dev/null )
RD=""
for i in $(seq 1 120); do
  S=$(ls -t $R/logs/trial-arena24-mixed-*.log 2>/dev/null | grep -v -- '-helper' | head -1)
  RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
  PORT=$(curl -s -m 5 http://127.0.0.1:8019/v1/models 2>/dev/null | head -c 20)
  [ -n "$RD" ] && [ -n "$PORT" ] && break
  RD=""; sleep 10
done
[ -n "$RD" ] || { echo "TRIAL BOOT FAIL $(date +%T) — restoring prod" | tee -a "$OUT/window.log"; bash "$R/stop-all.sh" >/dev/null 2>&1; sleep 8; ( cd "$R" && setsid --fork "$R/launch-v0238.sh" > "$R/logs/lane-restore.log" 2>&1 < /dev/null ); exit 1; }
echo "trial ready: $RD ($(date +%T))" | tee -a "$OUT/window.log"
# Verify trial identity from boot lines (engine path + the resized relief flags)
S=$(ls -t $R/logs/trial-arena24-mixed-*.log 2>/dev/null | grep -v -- '-helper' | head -1)
tr -d '\0' < "$S" | grep -a -E 'relief-hot-mib|relief-arena-cap|hot.*24000|cap.*28000|arena' | head -6 | tee -a "$OUT/window.log" || echo "WARNING: relief flags not found in boot log"

# 3) Legs (same shape as expert-lock run 2 so numbers are comparable)
snap T-warm
bash "$G/trial_judge_8019.sh" T0-arena24-nopress 3 2>&1 | tee -a "$OUT/probe.log"
rm -f "$OUT/pressure.hold"
python3 "$G/ram_pressure.py" --target-mib $PRESS_TARGET --cap-mib 40000 --hold-file "$OUT/pressure.hold" > "$OUT/pressure.log" 2>&1 &
PRESSURE=$!
touch "$OUT/pressure.hold"
t0=$SECONDS
while [ "$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)" -gt $((PRESS_TARGET + 200)) ]; do
  [ $((SECONDS - t0)) -ge 480 ] && break; sleep 3
done
echo "pressure: avail=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo) MiB after $((SECONDS - t0))s (holder $PRESSURE alive)" | tee -a "$OUT/window.log"
bash "$G/trial_judge_8019.sh" T1-arena24-press 6 2>&1 | tee -a "$OUT/probe.log"
rm -f "$OUT/pressure.hold"; wait $PRESSURE 2>/dev/null
snap T-final

# 4) Stop trial, restore prod, verify identity
bash "$R/stop-all.sh" 2>&1 | tail -2 | tee -a "$OUT/window.log"
sleep 8
( cd "$R" && setsid --fork "$R/launch-v0238.sh" > "$R/logs/lane-restore.log" 2>&1 < /dev/null )
RD=""
for i in $(seq 1 120); do
  S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log 2>/dev/null | grep -v -- '-helper' | head -1)
  RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
  PORT=$(curl -s -m 5 http://127.0.0.1:8009/v1/models 2>/dev/null | head -c 20)
  [ -n "$RD" ] && [ -n "$PORT" ] && break
  RD=""; sleep 10
done
[ -n "$RD" ] && echo "prod restored: $RD ($(date +%T))" | tee -a "$OUT/window.log" || { echo "PROD RESTORE FAIL $(date +%T) — INTERVENTION NEEDED" | tee -a "$OUT/window.log"; exit 1; }
sleep 5
curl -s -m 60 http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"ud-iq3_xxs","messages":[{"role":"user","content":"Reply with OK."}],"max_tokens":8}' \
  -o /dev/null -w 'prod completion check: http %{http_code} in %{time_total}s\n' | tee -a "$OUT/window.log"
echo "=== arena trial window complete $(date +%T) ===" | tee -a "$OUT/window.log"
grep -h ': .* tok/s' "$OUT/probe.log" || true
