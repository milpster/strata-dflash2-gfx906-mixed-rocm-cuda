#!/usr/bin/env bash
# ARENA-54 window: the engine's hot expert world as ONE consecutive mlocked chunk inside a
# kernel-enforced 56 GiB cgroup. Binaries: the frozen pin /mnt/srcds/Strata/v0239
# (v0239/SHA256SUMS.txt). Design (oracle ruling 2026-11-20): STRATA_ARENA_MMAP=1 makes the
# shared expert arena a read-only file mapping of the pack's experts.bin, and
# STRATA_NATIVE_MLOCK=arena locks only that one span (the pack and the GGUF shards are
# refused and stay silent), so the ~46.84 GiB of expert pages is reclaim-proof while the
# engine's own memory stays reclaimable. The arena is a fixed 46.84 GiB, so the cgroup
# wall is MemoryMax=56G / MemoryMin=51G / MemorySwapMax=4G (see launch-trial-arena54.sh)
# and the engine self-limits at 55296 MiB = 54 GiB, 2 GiB below the wall.
#
# PHASES: E1 arena pre-generation boot (uncapped, NOT measured) -> stop -> size assert ->
# measured boot under the 56G wall -> E2 boot gate (mlock proof) -> T0 no-pressure probe ->
# RAM pressure to ~7 GiB available -> T1 probe -> restore -> prod restore + completion check.
#
# RUN ONLY AFTER: (a) the arena-mode patch is accepted, (b) fresh operator prod-down go.
# Evidence: bench/ab-v0210/rpcprof/out/arena54-trial-20261120/
set -uo pipefail
R=/mnt/srcds/Strata
G=$R/bench/ab-v0210/rpcprof/gpu-rounds
OUT=$R/bench/ab-v0210/rpcprof/out/arena54-trial-20261120
PRESS_TARGET=7000
# The mapped arena file. want + one padding blob, exactly as expert_source.cpp writes it
# (50294988800 B = 46.8409 GiB); the mlock span is this whole file, so the engine's progress
# lines report it in MiB as 50294988800/2^20 = 47965 MiB.
EXPERTS_BIN=/mnt/srcds/ai/iq3xxs-ud/experts.bin
EXPERTS_BIN_BYTES=50294988800
EXPECTED_MIB=$(( EXPERTS_BIN_BYTES / 1048576 ))
# The engine's own log = the trial config's "log" entry. It is appended to (serve/server.py
# opens it "a"), so every phase reads only the bytes its own boot added (see newlog()).
ELOG=$R/logs/engine-trial-arena54-20261120.log
mkdir -p "$OUT"
echo "=== arena-54 window start $(date +%T) ===" | tee -a "$OUT/window.log"

sup_pid() { python3 -c "import json;print(json.load(open('$1')).get('engine_pid',0))" 2>/dev/null || echo 0; }
size_of() { stat -c %s "$1" 2>/dev/null || echo 0; }
newlog() { # newlog <file> <byte-offset> : what that boot appended, null bytes stripped
  [ -f "$1" ] && tail -c +"$(( $2 + 1 ))" "$1" 2>/dev/null | tr -d '\0'
}
vmlck() { # VmLck/VmRSS of the engine: with arena mlock these must NOT fall under pressure
  local eng=$1 tag=$2
  if [ -n "$eng" ] && [ "$eng" != 0 ] && [ -d /proc/$eng ]; then
    echo "VmLck/VmRSS $tag:"; grep -E '^(VmLck|VmRSS):' /proc/$eng/status | sed 's/^/  /'
  fi
}
snap() {
  local eng=$(sup_pid "$R/run/trial-arena54-supervisor.json")
  { echo "--- snap $1 $(date +%T) engine=$eng"
    grep -E '^(MemAvailable|Mlocked|SwapFree|Cached):' /proc/meminfo
    if [ "$eng" != "0" ] && [ -d /proc/$eng ]; then
      vmlck "$eng" "$1"
      awk -v p=$eng '/^Rss:/{t+=$2} END{printf "engine Rss: %d MiB\n", t/1024}' /proc/$eng/smaps
      # The mapped arena's own VMA: whether the file pages kept 2 MiB pages is the first
      # confound to clear (THP on a shared file mapping can be lost silently, and then the
      # lock holds 12 million 4 KiB pages instead of 24 thousand 2 MiB ones).
      echo "experts.bin mapping:"
      awk -v want="$EXPERTS_BIN" '
        /^[0-9a-f]+-[0-9a-f]+ / { hit = ($0 ~ want"$"); if (hit) print "  " $0; next }
        hit && /^(KernelPageSize|AnonHugePages|FilePmdMapped|THPeligible|Rss|Dirty|Locked):/ { print "  " $0 }
      ' /proc/$eng/smaps
      CG=$(grep -m1 -o '[^:]*$' /proc/$eng/cgroup 2>/dev/null | tr -d ' ')
      [ -n "$CG" ] && echo "cgroup memory.max: $(cat /sys/fs/cgroup${CG}/memory.max 2>/dev/null) memory.min: $(cat /sys/fs/cgroup${CG}/memory.min 2>/dev/null) memory.current: $(cat /sys/fs/cgroup${CG}/memory.current 2>/dev/null)"
      [ -n "$CG" ] && [ -r /sys/fs/cgroup${CG}/memory.events ] && echo "cgroup memory.events: $(tr '\n' ' ' < /sys/fs/cgroup${CG}/memory.events)"
    fi
    python3 "$G/file_residency.py" "$eng" --match=IQ3_XXS-0000; } >> "$OUT/memory.log" 2>&1
  tail -20 "$OUT/memory.log"
}
restore_prod() { # shared teardown: stop the trial lane, boot prod on :8009
  bash "$R/stop-all.sh" >/dev/null 2>&1
  sleep 8
  ( cd "$R" && setsid --fork "$R/launch-v0238.sh" > "$R/logs/lane-restore.log" 2>&1 < /dev/null )
}

sha256sum "$R/trial-config-arena54.json" | tee -a "$OUT/window.log"
echo "--- pin identity (must match v0239/SHA256SUMS.txt)" | tee -a "$OUT/window.log"
( cd "$R/v0239" && sha256sum -c SHA256SUMS.txt ) 2>&1 | tee -a "$OUT/window.log"
curl -s -m 8 -o /dev/null -w 'prod models pre-window: http %{http_code}\n' http://127.0.0.1:8009/v1/models | tee -a "$OUT/window.log"

# ---- E1: arena pre-generation boot -------------------------------------------------------
# experts.bin is written once, on the first STRATA_ARENA_MMAP boot that loads the arena from
# the GGUF: the engine fills the anonymous arena, then writes want + one blob to the pack and
# prints "STRATA_ARENA_MMAP: wrote <path> (the next start maps it)". That boot loads 46.84 GiB
# of arena AND writes 46.84 GiB of file, so it runs with the cgroup props dropped
# (STRATA_TRIAL_MEMORY_MAX/_MIN/STRATA_TRIAL_SWAP_MAX = none) - it is NOT the measured boot.
# If the file is already the exact size, a mapping boot never prints the wrote line at all, so
# booting again would only burn the 20-minute poll: skip straight to the assert.
echo "--- E1 arena pre-gen phase $(date +%T) (experts.bin target $EXPERTS_BIN_BYTES B = $EXPECTED_MIB MiB)" | tee -a "$OUT/window.log"
PREG_OK=1
if [ -f "$EXPERTS_BIN" ] && [ "$(size_of "$EXPERTS_BIN")" = "$EXPERTS_BIN_BYTES" ]; then
  echo "pre-gen: experts.bin already $EXPERTS_BIN_BYTES B - no pre-gen boot needed" | tee -a "$OUT/window.log"
else
  bash "$R/stop-all.sh" 2>&1 | tail -3 | tee -a "$OUT/window.log"
  sleep 8
  OFF=$(size_of "$ELOG")
  ( cd "$R" && STRATA_TRIAL_MEMORY_MAX=none STRATA_TRIAL_MEMORY_MIN=none STRATA_TRIAL_SWAP_MAX=none \
      setsid --fork "$R/launch-trial-arena54.sh" > "$R/logs/lane-arena54-pregen.log" 2>&1 < /dev/null )
  WROTE=""
  for i in $(seq 1 120); do   # up to 20 min: the load plus a 46.84 GiB write
    WROTE=$(newlog "$ELOG" "$OFF" | grep -a -E 'STRATA_ARENA_MMAP: (wrote|could NOT write|NOT writing)|the next start maps it' | tail -1)
    [ -n "$WROTE" ] && break
    sleep 10
  done
  if [ -z "$WROTE" ]; then
    echo "PRE-GEN FAIL: no STRATA_ARENA_MMAP write line in 20 min - last 40 engine log lines:" | tee -a "$OUT/window.log"
    newlog "$ELOG" "$OFF" | tail -40 | tee -a "$OUT/window.log"
    PREG_OK=0
  else
    echo "pre-gen: $WROTE ($(date +%T))" | tee -a "$OUT/window.log"
    case "$WROTE" in *could\ NOT\ write*|*NOT\ writing*) PREG_OK=0 ;; esac
  fi
fi
# Hard assert on the file the measured boot maps: any other size and the engine silently
# falls back to the anonymous arena (expert_source.cpp maps only when the size matches
# exactly), so the whole run would measure the wrong thing.
BIN_BYTES=$(size_of "$EXPERTS_BIN")
echo "experts.bin size: $BIN_BYTES B (expected $EXPERTS_BIN_BYTES B) $(date +%T)" | tee -a "$OUT/window.log"
if [ "$PREG_OK" != 1 ] || [ "$BIN_BYTES" != "$EXPERTS_BIN_BYTES" ]; then
  echo "PRE-GEN ABORT: experts.bin is not the expected arena file (size $BIN_BYTES) - restoring prod" | tee -a "$OUT/window.log"
  echo "WARNING: while this file exists, never run tools/iq_pack.py --experts-bin on this pack:" >> "$OUT/window.log"
  echo "         it writes the UNPADDED size and silently breaks the mmap size match -> anon fallback." >> "$OUT/window.log"
  restore_prod
  exit 1
fi
echo "WARNING: iq_pack.py --experts-bin must NOT run on this pack while $EXPERTS_BIN exists" | tee -a "$OUT/window.log"
echo "         (it writes the unpadded size and silently breaks the mmap size match)." | tee -a "$OUT/window.log"

# ---- measured boot, under the 56G wall ---------------------------------------------------
bash "$R/stop-all.sh" 2>&1 | tail -3 | tee -a "$OUT/window.log"
sleep 8
OFF=$(size_of "$ELOG")
( cd "$R" && setsid --fork "$R/launch-trial-arena54.sh" > "$R/logs/lane-arena54.log" 2>&1 < /dev/null )
RD=""
for i in $(seq 1 150); do
  S=$(ls -t $R/logs/trial-arena54-mixed-*.log 2>/dev/null | grep -v -- '-helper' | head -1)
  RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
  PORT=$(curl -s -m 5 http://127.0.0.1:8019/v1/models 2>/dev/null | head -c 20)
  [ -n "$RD" ] && [ -n "$PORT" ] && break
  RD=""; sleep 10
done
[ -n "$RD" ] || { echo "TRIAL BOOT FAIL $(date +%T) — restoring prod" | tee -a "$OUT/window.log"; restore_prod; exit 1; }
echo "trial ready: $RD ($(date +%T))" | tee -a "$OUT/window.log"
S=$(ls -t $R/logs/trial-arena54-mixed-*.log 2>/dev/null | grep -v -- '-helper' | head -1)
echo "--- boot identity (relief + arena lock + memory guard)" | tee -a "$OUT/window.log"
tr -d '\0' < "$S" | grep -a -iE 'relief|arena|native mlock|MemoryMax|memory limit' | head -10 | tee -a "$OUT/window.log"

# ---- E2: boot gate - prove the run really mapped AND locked the arena ---------------------
# PASS needs all three: the mlock summary, the "file pages locked" note (it is the mapped-path
# note, only when the span completed), and per-chunk progress naming the arena span with the
# expected 47965 MiB total. "nothing dropped: this run maps" is EXPECTED here (the RAM-relief
# knob is inert in mmap mode) - it is not a failure. A missing pass line, or any of the fail
# lines below (anon fallback, a refused/partial lock, no room to write the file), ends the run.
ENG_PID=$(sup_pid "$R/run/trial-arena54-supervisor.json")
{ echo "--- E2 boot gate $(date +%T) engine=$ENG_PID"
  newlog "$ELOG" "$OFF" | grep -a -iE 'arena|native mlock|mlock|relief|memory limit|MemoryMax' | head -25
  echo "cgroup of engine: $(cat /proc/$ENG_PID/cgroup 2>/dev/null | tr '\n' ' ')"
  grep -E '^VmLck:' /proc/$ENG_PID/status 2>/dev/null; } > "$OUT/boot-gate.log" 2>&1
tail -20 "$OUT/boot-gate.log" | tee -a "$OUT/window.log"
GATE=1
MLOCKED=$(newlog "$ELOG" "$OFF" | grep -a -c 'strata native mlock: locked')
FILELOCK=$(newlog "$ELOG" "$OFF" | grep -a -c 'file pages locked, not pinned')
PROG=$(newlog "$ELOG" "$OFF" | grep -a -E 'ArenaExpertSource shared expert arena progress chunk [0-9]+: [0-9]+ of [0-9]+ MiB locked' | tail -1)
[ "$MLOCKED" -ge 1 ] || { echo "GATE FAIL: no 'strata native mlock: locked' line" | tee -a "$OUT/window.log"; GATE=0; }
[ "$FILELOCK" -ge 1 ] || { echo "GATE FAIL: no 'file pages locked, not pinned' note (arena mapped without a complete lock?)" | tee -a "$OUT/window.log"; GATE=0; }
if [ -z "$PROG" ]; then
  echo "GATE FAIL: no arena mlock progress line" | tee -a "$OUT/window.log"; GATE=0
else
  [[ $PROG =~ of\ ([0-9]+)\ MiB\ locked ]] && PROGT=${BASH_REMATCH[1]} || PROGT=?
  echo "arena mlock progress (last): $PROG" | tee -a "$OUT/window.log"
  [ "${PROGT:-?}" = "$EXPECTED_MIB" ] || { echo "GATE FAIL: arena span is $PROGT MiB, expected $EXPECTED_MIB MiB" | tee -a "$OUT/window.log"; GATE=0; }
fi
for BAD in 'STRATA_ARENA_MMAP: not locked' 'partial, not pinned' 'NOT writing' 'could NOT write' 'MAP_HUGETLB unavailable' 'expert arena read'; do
  HIT=$(newlog "$ELOG" "$OFF" | grep -a -m1 -F "$BAD")
  [ -z "$HIT" ] || { echo "GATE FAIL on '$BAD': $HIT" | tee -a "$OUT/window.log"; GATE=0; }
done
[ "$GATE" = 1 ] || { echo "BOOT GATE ABORT $(date +%T) - restoring prod" | tee -a "$OUT/window.log"; restore_prod; exit 1; }
echo "boot gate PASS $(date +%T): the arena is mapped and locked ($MLOCKED report line, $FILELOCK note)" | tee -a "$OUT/window.log"
{ echo "--- VmLck at ready (before/after comparison starts here) $(date +%T)"; vmlck "$ENG_PID" "at-ready"; } | tee -a "$OUT/window.log"

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
