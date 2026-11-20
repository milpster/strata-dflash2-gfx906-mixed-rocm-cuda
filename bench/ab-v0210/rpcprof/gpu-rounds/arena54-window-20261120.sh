#!/usr/bin/env bash
# ARENA-54 window: the engine's whole CPU world inside a kernel-enforced 54 GiB cgroup
# (MemoryMax=54G, MemoryMin=51G) with ONE consecutive mlocked ~41 GiB expert arena inside
# (STRATA_NATIVE_MLOCK=arena + relief 40000/41000). Helper stays outside the cgroup by design.
# RUN ONLY AFTER: (a) the arena-mode patch is accepted, (b) fresh operator prod-down go.
# Evidence: bench/ab-v0210/rpcprof/out/arena54-trial-20261120/
set -uo pipefail
R=/mnt/srcds/Strata
G=$R/bench/ab-v0210/rpcprof/gpu-rounds
OUT=$R/bench/ab-v0210/rpcprof/out/arena54-trial-20261120
PRESS_TARGET=7000
mkdir -p "$OUT"
echo "=== arena-54 window start $(date +%T) ===" | tee -a "$OUT/window.log"

sup_pid() { python3 -c "import json;print(json.load(open('$1')).get('engine_pid',0))" 2>/dev/null || echo 0; }
snap() {
  local eng=$(sup_pid "$R/run/trial-arena54-supervisor.json")
  { echo "--- snap $1 $(date +%T) engine=$eng"
    grep -E '^(MemAvailable|Mlocked|SwapFree|Cached):' /proc/meminfo
    if [ "$eng" != "0" ] && [ -d /proc/$eng ]; then
      awk -v p=$eng '/^Rss:/{t+=$2} END{printf "engine Rss: %d MiB\n", t/1024}' /proc/$eng/smaps
      CG=$(grep -m1 -o '[^:]*$' /proc/$eng/cgroup 2>/dev/null | tr -d ' ')
      [ -n "$CG" ] && echo "cgroup memory.max: $(cat /sys/fs/cgroup${CG}/memory.max 2>/dev/null) memory.min: $(cat /sys/fs/cgroup${CG}/memory.min 2>/dev/null) memory.current: $(cat /sys/fs/cgroup${CG}/memory.current 2>/dev/null)"
    fi
    python3 "$G/file_residency.py" "$eng" --match=IQ3_XXS-0000; } >> "$OUT/memory.log" 2>&1
  tail -12 "$OUT/memory.log"
}

sha256sum "$R/trial-config-arena54.json" | tee -a "$OUT/window.log"
curl -s -m 8 -o /dev/null -w 'prod models pre-window: http %{http_code}\n' http://127.0.0.1:8009/v1/models | tee -a "$OUT/window.log"

bash "$R/stop-all.sh" 2>&1 | tail -3 | tee -a "$OUT/window.log"
sleep 8
( cd "$R" && setsid --fork "$R/launch-trial-arena54.sh" > "$R/logs/lane-arena54.log" 2>&1 < /dev/null )
RD=""
for i in $(seq 1 150); do
  S=$(ls -t $R/logs/trial-arena54-mixed-*.log 2>/dev/null | grep -v -- '-helper' | head -1)
  RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
  PORT=$(curl -s -m 5 http://127.0.0.1:8019/v1/models 2>/dev/null | head -c 20)
  [ -n "$RD" ] && [ -n "$PORT" ] && break
  RD=""; sleep 10
done
[ -n "$RD" ] || { echo "TRIAL BOOT FAIL $(date +%T) — restoring prod" | tee -a "$OUT/window.log"; bash "$R/stop-all.sh" >/dev/null 2>&1; sleep 8; ( cd "$R" && setsid --fork "$R/launch-v0238.sh" > "$R/logs/lane-restore.log" 2>&1 < /dev/null ); exit 1; }
echo "trial ready: $RD ($(date +%T))" | tee -a "$OUT/window.log"
S=$(ls -t $R/logs/trial-arena54-mixed-*.log 2>/dev/null | grep -v -- '-helper' | head -1)
echo "--- boot identity (relief + arena lock + memory guard)" | tee -a "$OUT/window.log"
tr -d '\0' < "$S" | grep -a -iE 'relief|arena|native mlock|MemoryMax|memory limit' | head -10 | tee -a "$OUT/window.log"

snap A54-warm
bash "$G/trial_judge_8019.sh" T0-arena54-nopress 4 2>&1 | tee -a "$OUT/probe.log"
rm -f "$OUT/pressure.hold"
python3 "$G/ram_pressure.py" --target-mib $PRESS_TARGET --cap-mib 40000 --hold-file "$OUT/pressure.hold" > "$OUT/pressure.log" 2>&1 &
PRESSURE=$!
touch "$OUT/pressure.hold"
t0=$SECONDS
while [ "$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)" -gt $((PRESS_TARGET + 200)) ]; do
  [ $((SECONDS - t0)) -ge 480 ] && break; sleep 3
done
echo "pressure: avail=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo) MiB after $((SECONDS - t0))s" | tee -a "$OUT/window.log"
bash "$G/trial_judge_8019.sh" T1-arena54-press 6 2>&1 | tee -a "$OUT/probe.log"
rm -f "$OUT/pressure.hold"; wait $PRESSURE 2>/dev/null
snap A54-final

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
[ -n "$RD" ] && echo "prod restored: $RD ($(date +%T))" | tee -a "$OUT/window.log" || { echo "PROD RESTORE FAIL — INTERVENTION NEEDED" | tee -a "$OUT/window.log"; exit 1; }
sleep 5
curl -s -m 60 http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"ud-iq3_xxs","messages":[{"role":"user","content":"Reply with OK."}],"max_tokens":8}' \
  -o /dev/null -w 'prod completion check: http %{http_code} in %{time_total}s\n' | tee -a "$OUT/window.log"
echo "=== arena-54 window complete $(date +%T) ===" | tee -a "$OUT/window.log"
grep -h ': .* tok/s' "$OUT/probe.log" || true
