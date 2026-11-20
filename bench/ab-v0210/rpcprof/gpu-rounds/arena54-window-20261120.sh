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
# Evidence: everything lands in $OUT, i.e. bench/ab-v0210/rpcprof/out/arena54-trial-20261120
# (the same name as this file's date tag; the window echoes it at start so the log proves it).
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
# Lane state files (pair_supervisor.py records the running pair there, including the log path
# of THIS boot and the engine pid) and the arena-file content record.
TRIAL_STATE=$R/run/trial-arena54-supervisor.json
PROD_STATE=$R/run/prod-swift15-f16kv-mixed-supervisor.json
BIN_SHA_FILE=$OUT/experts-bin.sha256
mkdir -p "$OUT"
echo "=== arena-54 window start $(date +%T) ===" | tee -a "$OUT/window.log"
echo "evidence dir: $OUT" | tee -a "$OUT/window.log"

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
  local eng=$(sup_pid "$TRIAL_STATE")
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
      CG=$(grep -m1 '^0::' /proc/$eng/cgroup 2>/dev/null | sed 's/^0:://' | tr -d ' ')
      [ -n "$CG" ] && echo "cgroup memory.max: $(cat /sys/fs/cgroup${CG}/memory.max 2>/dev/null) memory.min: $(cat /sys/fs/cgroup${CG}/memory.min 2>/dev/null) memory.current: $(cat /sys/fs/cgroup${CG}/memory.current 2>/dev/null)"
      [ -n "$CG" ] && [ -r /sys/fs/cgroup${CG}/memory.events ] && echo "cgroup memory.events: $(tr '\n' ' ' < /sys/fs/cgroup${CG}/memory.events)"
    fi
    python3 "$G/file_residency.py" "$eng" --match=IQ3_XXS-0000; } >> "$OUT/memory.log" 2>&1
  tail -20 "$OUT/memory.log"
}
sup_field() { python3 -c "import json;print(json.load(open('$1')).get('$2',''))" 2>/dev/null || echo ""; }
# Readiness of one lane, immune to the previous boot's log: the path comes from this run's
# supervisor state file (pair_supervisor.py records the launcher's log there), and a path that
# still equals the snapshot taken BEFORE the boot is refused - so a log that already contains
# 'ready: http://' from an earlier boot can never be mistaken for this one.
# Sets READY_LINE/READY_LOG; returns 1 when the tries run out.
wait_ready() { # wait_ready <state-file> <pre-boot-log> <port> <tries>
  local state=$1 prev=$2 port=$3 tries=$4 log rd i
  READY_LINE="" READY_LOG=""
  for i in $(seq 1 "$tries"); do
    log=$(sup_field "$state" engine_log)
    if [ -n "$log" ] && [ "$log" != "$prev" ] && [ -f "$log" ]; then
      rd=$(tr -d '\0' < "$log" 2>/dev/null | grep -a 'ready: http://' | tail -1)
      if [ -n "$rd" ] && [ -n "$(curl -s -m 5 "http://127.0.0.1:$port/v1/models" 2>/dev/null | head -c 20)" ]; then
        READY_LINE=$rd READY_LOG=$log; return 0
      fi
    fi
    sleep 10
  done
  return 1
}
# The pair supervisor publishes engine_pid when it attaches, and it attaches on the same
# ready line the poll above waits for - give it a minute before calling a 0 pid a failure.
engine_pid_ready() { # engine_pid_ready <state-file> -> echoes the pid, or 0 after a minute
  local state=$1 p=0 i
  for i in $(seq 1 12); do
    p=$(sup_pid "$state")
    [ "$p" != "0" ] && [ -d "/proc/$p" ] && { echo "$p"; return; }
    sleep 5
  done
  echo "$p"
}
record_bin_sha() { # the mapped arena file's content, once per window, for the later assert
  sha256sum "$EXPERTS_BIN" > "$BIN_SHA_FILE" 2>>"$OUT/window.log" &&
    echo "experts.bin sha256 recorded ($(cut -d' ' -f1 "$BIN_SHA_FILE")) $(date +%T)" | tee -a "$OUT/window.log"
}
restore_prod() { # shared teardown: stop every lane, boot prod on :8009, and VERIFY it came back
  bash "$R/stop-all.sh" 2>&1 | tail -2 | tee -a "$OUT/window.log"
  sleep 8
  local prev; prev=$(sup_field "$PROD_STATE" engine_log)
  ( cd "$R" && setsid --fork "$R/launch-v0238.sh" > "$R/logs/lane-restore.log" 2>&1 < /dev/null )
  if wait_ready "$PROD_STATE" "$prev" 8009 120; then
    echo "prod restored: $READY_LINE ($(date +%T)) log: $READY_LOG" | tee -a "$OUT/window.log"
    return 0
  fi
  echo "PROD RESTORE FAIL — INTERVENTION NEEDED $(date +%T): prod did not answer on :8009" | tee -a "$OUT/window.log"
  return 1
}
# A signal in the middle of the pressure leg used to be the end of it: ram_pressure.py holds
# the box at ~7 GiB available until its hold file disappears, so a killed window left 40 GiB
# squeezed and prod un-restored. The trap releases the hold, stops the holder, hands prod back.
on_signal() {
  trap - INT TERM   # never twice
  echo "SIGNAL $(date +%T): releasing $OUT/pressure.hold, stopping the holder, restoring prod" | tee -a "$OUT/window.log"
  rm -f "$OUT/pressure.hold"
  [ -n "${PRESSURE:-}" ] && kill -TERM "$PRESSURE" 2>/dev/null
  restore_prod
  exit 1
}
trap on_signal INT TERM   # armed for every phase after this point (oracle round 3): a signal in
                          # pre-gen or T0 also leaves prod down; PRESSURE is unset there, so the
                          # handler's kill/rm are safe no-ops and restore_prod does the work.

sha256sum "$R/trial-config-arena54.json" | tee -a "$OUT/window.log"
echo "--- pin identity (must match v0239/SHA256SUMS.txt)" | tee -a "$OUT/window.log"
( cd "$R/v0239" && sha256sum -c SHA256SUMS.txt ) 2>&1 | tee -a "$OUT/window.log"
curl -s -m 8 -o /dev/null -w 'prod models pre-window: http %{http_code}\n' http://127.0.0.1:8009/v1/models | tee -a "$OUT/window.log"

# ---- pre-flight: the mapped arena file must be absent, or exactly right ------------------
# With ANY experts.bin present the engine does not read the GGUF at all (from_gguf=false), so
# the "wrote ... (the next start maps it)" line can never print: a wrong-size file - the
# unpadded one tools/iq_pack.py --experts-bin writes, for instance - does not merely slow the
# pre-gen boot, it makes it unwinnable. So this aborts before any serving lane is touched,
# instead of 20 minutes into a poll that could not have ended well. restore_prod is still the
# right teardown: it guarantees a verified prod-serving end state however far the window got,
# and costs one prod restart in the case where nothing had been started yet.
BIN_BYTES=$(size_of "$EXPERTS_BIN")
if [ -f "$EXPERTS_BIN" ] && [ "$BIN_BYTES" != "$EXPERTS_BIN_BYTES" ]; then
  echo "PRE-FLIGHT ABORT $(date +%T): experts.bin is $BIN_BYTES B, expected $EXPERTS_BIN_BYTES B." | tee -a "$OUT/window.log"
  echo "  A stale/unpadded experts.bin blocks pre-gen: delete it or re-derive it, and never run" | tee -a "$OUT/window.log"
  echo "  tools/iq_pack.py --experts-bin on this pack while this window depends on the file." | tee -a "$OUT/window.log"
  restore_prod
  exit 1
fi

# ---- E1: arena pre-generation boot -------------------------------------------------------
# experts.bin is written once, on the first STRATA_ARENA_MMAP boot that loads the arena from
# the GGUF: the engine fills the anonymous arena, then writes want + one blob to the pack and
# prints "STRATA_ARENA_MMAP: wrote <path> (the next start maps it)". That boot loads 46.84 GiB
# of arena AND writes 46.84 GiB of file, so it runs with the cgroup props dropped
# (STRATA_TRIAL_MEMORY_MAX/_MIN/STRATA_TRIAL_SWAP_MAX = none) - it is NOT the measured boot.
# If the file is already there, the pre-flight above has just proved its size is exact, and a
# mapping boot prints no wrote line at all - booting anyway would only burn the 20-minute poll.
# The file's CONTENT is pinned too: the first run that makes it records its sha256, and a later
# run with a different file aborts rather than measuring a different world.
echo "--- E1 arena pre-gen phase $(date +%T) (experts.bin target $EXPERTS_BIN_BYTES B = $EXPECTED_MIB MiB)" | tee -a "$OUT/window.log"
PREG_OK=1
PREGEN_BOOT_RAN=0   # 1 only on the run that actually generated the arena file
BIN_SHA_PENDING=1   # set to 0 once $BIN_SHA_FILE holds a hash this run agreed with
[ -s "$BIN_SHA_FILE" ] || echo "pre-gen: no $BIN_SHA_FILE record yet - hashing this file is deferred until the boot gate passes" | tee -a "$OUT/window.log"
if [ -f "$EXPERTS_BIN" ]; then
  if [ -s "$BIN_SHA_FILE" ]; then
    WANT_SHA=$(cut -d' ' -f1 "$BIN_SHA_FILE")
    GOT_SHA=$(sha256sum "$EXPERTS_BIN" | cut -d' ' -f1)
    if [ "$GOT_SHA" = "$WANT_SHA" ]; then
      BIN_SHA_PENDING=0
      echo "pre-gen: experts.bin content matches the recorded $WANT_SHA ($(date +%T))" | tee -a "$OUT/window.log"
    else
      echo "PRE-GEN ABORT $(date +%T): experts.bin content changed - recorded $WANT_SHA, now $GOT_SHA." | tee -a "$OUT/window.log"
      echo "  The measured boot would map a different file than the earlier legs did." | tee -a "$OUT/window.log"
      restore_prod
      exit 1
    fi
  fi
  echo "pre-gen: experts.bin already $EXPERTS_BIN_BYTES B - no pre-gen boot needed" | tee -a "$OUT/window.log"
else
  PREGEN_BOOT_RAN=1
  bash "$R/stop-all.sh" 2>&1 | tail -3 | tee -a "$OUT/window.log"
  sleep 8
  OFF=$(size_of "$ELOG")
  ( cd "$R" && STRATA_TRIAL_MEMORY_MAX=none STRATA_TRIAL_MEMORY_MIN=none STRATA_TRIAL_SWAP_MAX=none \
      STRATA_TRIAL_STARTUP_TIMEOUT_S=2400 \
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
# The boot that just wrote the file is the only one that can attest to its content.
if [ "$PREGEN_BOOT_RAN" = 1 ]; then
  echo "pre-gen: recording the content of the file this boot just wrote" | tee -a "$OUT/window.log"
  record_bin_sha && BIN_SHA_PENDING=0
fi
echo "WARNING: iq_pack.py --experts-bin must NOT run on this pack while $EXPERTS_BIN exists" | tee -a "$OUT/window.log"
echo "         (it writes the unpadded size and silently breaks the mmap size match)." | tee -a "$OUT/window.log"

# ---- measured boot, under the 56G wall ---------------------------------------------------
bash "$R/stop-all.sh" 2>&1 | tail -3 | tee -a "$OUT/window.log"
sleep 8
OFF=$(size_of "$ELOG")
PREV_TRIAL_LOG=$(sup_field "$TRIAL_STATE" engine_log)   # the previous boot's log, to be refused
( cd "$R" && setsid --fork "$R/launch-trial-arena54.sh" > "$R/logs/lane-arena54.log" 2>&1 < /dev/null )
wait_ready "$TRIAL_STATE" "$PREV_TRIAL_LOG" 8019 150 || {
  echo "TRIAL BOOT FAIL $(date +%T) — restoring prod" | tee -a "$OUT/window.log"; restore_prod; exit 1; }
RD=$READY_LINE; S=$READY_LOG
echo "trial ready: $RD ($(date +%T)) log: $S" | tee -a "$OUT/window.log"
echo "--- boot identity (relief + arena lock + memory guard)" | tee -a "$OUT/window.log"
tr -d '\0' < "$S" | grep -a -iE 'relief|arena|native mlock|MemoryMax|memory limit' | head -10 | tee -a "$OUT/window.log"

# ---- E2: boot gate - prove the run really mapped AND locked the arena ---------------------
# PASS needs all three: the mlock summary, the "file pages locked" note (it is the mapped-path
# note, only when the span completed), and per-chunk progress naming the arena span with the
# expected 47965 MiB total. "nothing dropped: this run maps" is EXPECTED here (the RAM-relief
# knob is inert in mmap mode) - it is not a failure. A missing pass line, or any of the fail
# lines below (anon fallback, a refused/partial lock, no room to write the file), ends the run.
ENG_PID=$(engine_pid_ready "$TRIAL_STATE")
# The kernel-side evidence (VmLck, smaps, the cgroup line) is read from this pid alone: a 0, a
# dead pid, or one left over from the previous boot would silently drop that evidence or
# describe the wrong process. Accepted identity markers in its command line are the trial's own
# config path or - what serve/server.py actually passes down - the trial's helper socket path.
if [ "${ENG_PID:-0}" = "0" ] || ! kill -0 "$ENG_PID" 2>/dev/null \
   || ! grep -qaE 'trial-config-arena54|trial-arena54[.]sock' "/proc/$ENG_PID/cmdline" 2>/dev/null; then
  echo "GATE FAIL $(date +%T): engine pid '$ENG_PID' is missing, dead, or is not this trial's engine" | tee -a "$OUT/window.log"
  CMDLINE=$([ -r "/proc/$ENG_PID/cmdline" ] && tr '\0' ' ' < "/proc/$ENG_PID/cmdline" | head -c 200)
  echo "  state file: $TRIAL_STATE; that pid's command line: ${CMDLINE:-(unreadable - no such process)}" | tee -a "$OUT/window.log"
  restore_prod
  exit 1
fi
echo "gate engine pid: $ENG_PID (identity confirmed in /proc/$ENG_PID/cmdline)" | tee -a "$OUT/window.log"
{ echo "--- E2 boot gate $(date +%T) engine=$ENG_PID"
  newlog "$ELOG" "$OFF" | grep -a -iE 'arena|native mlock|mlock|relief|memory limit|MemoryMax' | head -25
  echo "cgroup of engine: $(cat /proc/$ENG_PID/cgroup 2>/dev/null | tr '\n' ' ')"
  grep -E '^VmLck:' /proc/$ENG_PID/status 2>/dev/null; } > "$OUT/boot-gate.log" 2>&1
tail -20 "$OUT/boot-gate.log" | tee -a "$OUT/window.log"
GATE=1
MLOCKED=$(newlog "$ELOG" "$OFF" | grep -a -c 'strata native mlock: locked')
SPANS=$(newlog "$ELOG" "$OFF" | grep -a -o 'strata native mlock: locked.*' | grep -a -o 'over [0-9]* spans' | grep -a -o '[0-9]*' | head -1)
FILELOCK=$(newlog "$ELOG" "$OFF" | grep -a -c 'file pages locked, not pinned')
PROG=$(newlog "$ELOG" "$OFF" | grep -a -E 'ArenaExpertSource shared expert arena progress chunk [0-9]+: [0-9]+ of [0-9]+ MiB locked' | tail -1)
[ "$MLOCKED" -ge 1 ] || { echo "GATE FAIL: no 'strata native mlock: locked' line" | tee -a "$OUT/window.log"; GATE=0; }
[ "${SPANS:-0}" = "1" ] || { echo "GATE FAIL: mlock summary reports ${SPANS:-no} spans, expected exactly 1 (more = Mode::all triple-lock: pack/shards locked too)" | tee -a "$OUT/window.log"; GATE=0; }
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
# A first run that found the file without a hash record gets one now: the gate has proved this
# boot really maps it, so its content is the content the measurement depends on.
if [ "$BIN_SHA_PENDING" = 1 ]; then
  echo "gate passed: recording experts.bin content as the reference for later runs" | tee -a "$OUT/window.log"
  record_bin_sha && BIN_SHA_PENDING=0
fi

snap A54-warm
bash "$G/trial_judge_8019.sh" T0-arena54-nopress 4 2>&1 | tee -a "$OUT/probe.log"
# From here to the end of the pressure leg the window holds the box at ~7 GiB available; the
# trap armed after on_signal() covers this phase too (re-arming would be a no-op anyway).
rm -f "$OUT/pressure.hold"
python3 "$G/ram_pressure.py" --target-mib $PRESS_TARGET --cap-mib 49000 --hold-file "$OUT/pressure.hold" > "$OUT/pressure.log" 2>&1 &
PRESSURE=$!
touch "$OUT/pressure.hold"
t0=$SECONDS
while [ "$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)" -gt $((PRESS_TARGET + 200)) ]; do
  [ $((SECONDS - t0)) -ge 480 ] && break; sleep 3
done
echo "pressure: avail=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo) MiB after $((SECONDS - t0))s" | tee -a "$OUT/window.log"
bash "$G/trial_judge_8019.sh" T1-arena54-press 6 2>&1 | tee -a "$OUT/probe.log"
rm -f "$OUT/pressure.hold"; wait $PRESSURE 2>/dev/null
trap - INT TERM   # the holder is stopped and released; the scripted teardown takes over
snap A54-final

# Scripted teardown: restore_prod does the stop-all, the prod boot and its own readiness poll,
# so this path ends verified exactly like the four abort paths do.
restore_prod || exit 1
sleep 5
curl -s -m 60 http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"ud-iq3_xxs","messages":[{"role":"user","content":"Reply with OK."}],"max_tokens":8}' \
  -o /dev/null -w 'prod completion check: http %{http_code} in %{time_total}s\n' | tee -a "$OUT/window.log"
echo "=== arena-54 window complete $(date +%T) ===" | tee -a "$OUT/window.log"
grep -h ': .* tok/s' "$OUT/probe.log" || true
