#!/usr/bin/env bash
# cycle_boots.sh N [--leave-up] [--plan]
#
# Why: the amdgpu permission fault killed ~3 of 6 boots at the 58,423-token
# prefill. Checking whether a candidate fix stops it needs repeated FRESH boots
# (the fault appears on the first big prefill of a boot, when the pool is newly
# filled), not repeats inside one boot (the second prefill is a cache hit).
#
# Each cycle: stop the previous leg -> clear its state -> boot -> wait for health
# -> fire one cold 58,423-token prefill -> record faults and memory -> keep going.
# Live output: it prints one block per cycle and per-30 s progress while booting.
#
# Engine override: when STRATA_FAULTDIFF_ENGINE (absolute path) and
# STRATA_FAULTDIFF_ENGINE_SHA256 (its sha256) are both set in the caller's
# environment they are exported unchanged, so every faultdiff_run.py call inherits
# them and the leg boots that binary instead of the rig pin. With neither set the
# pinned engine is used; a lone one is refused here, before anything starts. The
# engine that actually served is read back from the driver's record
# (records/<cycle>/B-pp239k.json: engine_sha256, engine_override), never from the
# process list.
#
# --plan       print the exact commands per cycle; start, stop and write nothing.
# --leave-up   leave the last cycle's boot running. The default is to stop it, so
#              an unattended box is never left with an engine on :8009 after the end.
#
# The run ends with one machine-readable summary,
# bench/fault-va/out/cycle-<timestamp>.tsv, one row per cycle:
#   cycle, leg, engine_sha256, override(0/1), faults, judge_line, rss, anonhuge,
#   started, ended
# A cycle that never reached a driver record leaves engine_sha256 and override
# empty (no evidence is invented) and says why in judge_line.
set -euo pipefail
ROOT=/mnt/srcds/Strata
cd "$ROOT"
PY=python3
RUN=bench/ab-v0210/rpcprof/faultdiff_run.py
LEG=${LEG:-default-margins}
OUT=bench/ab-v0210/rpcprof/out/faultdiff-r1
SUMMARY_DIR=bench/fault-va/out
CORPUS=bench/ab-v0210/rpcprof/out/port-ab-20261123/corpus/pp239k.ctx323200.bin
PROMPT_SHA=210b16b32b8658e68b863442d3c2d9ebde7eb3c9d50b0cea8c28e7d045c0ea68
TEMPLATE=bench/ab-v0210/rpcprof/out/port-ab-20261123/request-template.json
TEMPLATE_SHA=1367e0c041b3e7e6abceef27587f9ffbb8ff1d8b2ea4cc7d0141d9e945a18abd
USAGE='usage: cycle_boots.sh N [--leave-up] [--plan]'
N=
LEAVE_UP=0
PLAN=0
say() { echo "[$(date +%H:%M:%S)] cycle_boots: $*"; }
die() { say "$*"; exit 2; }

for arg in "$@"; do
  case $arg in
    --leave-up) LEAVE_UP=1 ;;
    --plan) PLAN=1 ;;
    -h|--help) printf '%s\n' "$USAGE"; sed -n '3,31p' "$0"; exit 0 ;;
    -*) die "unknown option: $arg ($USAGE)" ;;
    *) [ -z "$N" ] || die "$USAGE"; N=$arg ;;
  esac
done
[ -n "$N" ] || die "$USAGE"
case $N in *[!0-9]*) die "N must be a positive integer, not '$N' ($USAGE)" ;; esac
[ "$N" -ge 1 ] || die "N must be at least 1 ($USAGE)"

# The engine this run tests. Both variables or neither: the child refuses a lone one,
# and this side refuses it before a single boot is attempted.
ENGINE_PREFIX=''
if [ -n "${STRATA_FAULTDIFF_ENGINE:-}" ] && [ -n "${STRATA_FAULTDIFF_ENGINE_SHA256:-}" ]; then
  export STRATA_FAULTDIFF_ENGINE STRATA_FAULTDIFF_ENGINE_SHA256
  ENGINE_SHA12=${STRATA_FAULTDIFF_ENGINE_SHA256:0:12}
  ENGINE_PREFIX="STRATA_FAULTDIFF_ENGINE=$STRATA_FAULTDIFF_ENGINE STRATA_FAULTDIFF_ENGINE_SHA256=$STRATA_FAULTDIFF_ENGINE_SHA256 "
  say "engine override: $STRATA_FAULTDIFF_ENGINE sha=$ENGINE_SHA12"
elif [ -n "${STRATA_FAULTDIFF_ENGINE:-}" ] || [ -n "${STRATA_FAULTDIFF_ENGINE_SHA256:-}" ]; then
  die "REFUSING: set both STRATA_FAULTDIFF_ENGINE and STRATA_FAULTDIFF_ENGINE_SHA256, or neither"
else
  say "engine: pinned engine (no override in the environment)"
fi

REC_REL="$OUT/records"

if [ "$PLAN" -eq 1 ]; then
  say "plan only: nothing is started, stopped or written"
  for i in $(seq 1 "$N"); do
    echo "cycle $i/$N"
    echo "  stop     $PY $RUN --leg $LEG --stop"
    echo "  kill     TERM every engine whose argv[0] is .../rpcw-hip/strata --serve; sleep 6; null the leg state PIDs"
    echo "  boot     ${ENGINE_PREFIX}$PY $RUN --leg $LEG --boot --waive-nonengine > /tmp/opencode/cb-boot-$i.log 2>&1 &"
    echo "  health   curl -fsS -m 2 http://127.0.0.1:8009/health; 45 tries, 10 s apart, stop early if the boot exits"
    echo "  pid      ./bench/fault-va/engine_pid.sh"
    echo "  fire     $PY bench/ab-v0210/rpcprof/ab9_driver.py --arm B --leg pp239k --kind prefill --ctx 323200 --max-tokens 8"
    echo "           --out $REC_REL/cb-$i --elog $OUT/logs/$LEG.engine.log --hlog $OUT/logs/$LEG.helper.log"
    echo "           --url http://127.0.0.1:8009/v1/chat/completions --prompt-file $CORPUS --prompt-sha256 $PROMPT_SHA"
    echo "           --request-template $TEMPLATE --request-template-sha256 $TEMPLATE_SHA --timeout 5400 --engine-pid <pid>"
    echo "  measure  journalctl -k 'no-retry page fault' since the cycle start; engine VmRSS and AnonHugePages"
    echo "  row      $SUMMARY_DIR/cycle-<timestamp>.tsv += cb-$i, $LEG, <sha from $REC_REL/cb-$i/B-pp239k.json>, <override 0/1>, ..."
  done
  if [ "$LEAVE_UP" -eq 1 ]; then
    echo "finally  --leave-up: the last cycle's boot is left running"
  else
    echo "finally  stop the last boot (else pass --leave-up); also on error and on interrupt"
  fi
  exit 0
fi

faults_since() { journalctl -k --since "$1" --no-pager 2>/dev/null | grep -cE 'no-retry page fault' || true; }
tsv_field() { printf '%s' "${1-}" | tr '\t\n' '  '; }

# The engine that actually served a cycle, from the driver's own record:
# "<sha256>|<0|1>|<note>", sha and flag empty when the record is absent or unusable.
# "|" separates because a TAB is IFS whitespace: empty leading fields collapse and the
# note would land in the sha column (both seen in a live run, 2026-11-24 02:02).
#
# The driver records engine_sha256 but not engine_override: the override is the boot's
# business (faultdiff_run.py writes it in the leg state). So the flag is the digest
# compared with the rig pin - a cycle that served the pinned binary is 0, any other
# binary is 1 - and an engine_override in the record is honoured if a driver ever adds
# one. Whether the record belongs to THIS cycle is the caller's decision (it compares
# the file's stamp across the firing), so nothing here is time-guessed.
record_engine() {
  $PY - "$1" "$PIN_SHA" <<'PY'
import json, pathlib, sys
rec, pin = pathlib.Path(sys.argv[1], "B-pp239k.json"), sys.argv[2]
try:
    d = json.loads(rec.read_text())
except (OSError, ValueError):
    print("||")
    raise SystemExit(0)
sha = d.get("engine_sha256") or ""
ov = d.get("engine_override")
if isinstance(ov, dict) and ov.get("path"):
    flag = 1
elif not sha:
    flag = ""
elif pin and sha == pin:
    flag = 0
else:
    flag = 1
print(str(sha) + "|" + str(flag))
PY
}

# The rig's own engine digest, the reference an override is measured against.
pin_sha() {
  $PY - <<'PY'
import sys
sys.path.insert(0, "bench/ab-v0210/rpcprof")
import faultdiff_configs as fc
print(fc.PINS["engine_sha256"])
PY
}

stop_leg() {
  $PY "$RUN" --leg "$LEG" --stop >/dev/null 2>&1 || true
  for p in $(ps -eo pid=,args= | awk '$2 ~ /rpcw-hip\/strata/ && $3=="--serve" {print $1}'); do kill -TERM "$p" 2>/dev/null || true; done
  sleep 6
  $PY - <<'PY' || true
import json, pathlib
p = pathlib.Path('bench/ab-v0210/rpcprof/out/faultdiff-r1/run/' + __import__('os').environ.get('LEG','default-margins') + '.state.json')
if p.is_file():
    s = json.loads(p.read_text())
    for k in ('server_pid','server_start','engine_pid','engine_start','helper_pid','helper_start'):
        s[k] = None
    p.write_text(json.dumps(s, indent=1))
PY
}

# Nothing this script started may outlive it: the boot is a background child, so the
# default stops the last leg on every exit path - normal end, error, Ctrl-C.
STARTED=0
cleanup() {
  set +e
  [ "$STARTED" -eq 1 ] || return 0            # nothing of ours to tear down
  if [ "$LEAVE_UP" -eq 1 ]; then
    say "--leave-up: the last boot is still running (stop it: $PY $RUN --leg $LEG --stop)"
  else
    say 'stopping the last boot (pass --leave-up to keep it)'
    stop_leg
  fi
}
trap cleanup EXIT

PIN_SHA=$(pin_sha) || die 'cannot read the rig engine pin from faultdiff_configs.py'
mkdir -p "$SUMMARY_DIR"
# One file per run, never overwritten: a second run in the same second gets -2, -3, ...
TSV_BASE="$SUMMARY_DIR/cycle-$(date '+%Y%m%d-%H%M%S')"
TSV="$TSV_BASE.tsv"
n=2
while [ -e "$TSV" ]; do TSV="$TSV_BASE-$n.tsv"; n=$((n + 1)); done
printf 'cycle\tleg\tengine_sha256\toverride\tfaults\tjudge_line\trss\tanonhuge\tstarted\tended\n' > "$TSV"
say "summary: $TSV"

STARTED=1
for i in $(seq 1 "$N"); do
  say "cycle $i/$N: stopping previous"
  stop_leg
  T0=$(date '+%Y-%m-%d %H:%M:%S')
  say "booting ($T0)"
  $PY "$RUN" --leg "$LEG" --boot --waive-nonengine > "/tmp/opencode/cb-boot-$i.log" 2>&1 &
  BP=$!
  ok=0
  for _ in $(seq 1 45); do
    H=$(curl -fsS -m 2 http://127.0.0.1:8009/health 2>/dev/null | head -c 12 || true)
    if [ -n "$H" ]; then ok=1; break; fi
    kill -0 "$BP" 2>/dev/null || break
    sleep 10
  done
  if [ "$ok" -ne 1 ]; then
    say "boot did not come up; see /tmp/opencode/cb-boot-$i.log"
    kill -TERM "$BP" 2>/dev/null || true
    tail -3 "/tmp/opencode/cb-boot-$i.log" | cut -c1-140 || true
    F=$(faults_since "$T0")
    say "cycle $i done: boot did not come up faults=$F"
    printf 'cb-%s\t%s\t\t\t%s\tboot did not come up\t\t\t%s\t%s\n' \
      "$i" "$LEG" "$F" "$T0" "$(date '+%Y-%m-%d %H:%M:%S')" >> "$TSV"
    continue
  fi
  if ! EP=$(./bench/fault-va/engine_pid.sh); then
    say 'no engine pid found'
    F=$(faults_since "$T0")
    printf 'cb-%s\t%s\t\t\t%s\tno engine pid found\t\t\t%s\t%s\n' \
      "$i" "$LEG" "$F" "$T0" "$(date '+%Y-%m-%d %H:%M:%S')" >> "$TSV"
    continue
  fi
  say "engine $EP up; firing the cold prefill"
  # Only this cycle's record counts. The record path is shared by every leg and by earlier
  # cycles, so the file's stamp is read across the firing: unchanged means the driver wrote
  # nothing (HTTP 503, timeout) and this cycle has no digest and no rate. A live run showed
  # why that matters (2026-11-24 02:02): a failed prefill inherited both.
  REC_FILE="$REC_REL/cb-$i/B-pp239k.json"
  REC_BEFORE=$(stat -c %y "$REC_FILE" 2>/dev/null || echo none)
  ELOG_OFF=$(tr -d '\000' < "$OUT/logs/$LEG.engine.log" 2>/dev/null | wc -c || echo 0)
  FIRE_RC=0
  $PY bench/ab-v0210/rpcprof/ab9_driver.py --arm B --leg pp239k \
    --kind prefill --ctx 323200 --max-tokens 8 \
    --out "$REC_REL/cb-$i" \
    --elog "$OUT/logs/$LEG.engine.log" --hlog "$OUT/logs/$LEG.helper.log" \
    --url http://127.0.0.1:8009/v1/chat/completions \
    --prompt-file "$CORPUS" --prompt-sha256 "$PROMPT_SHA" \
    --request-template "$TEMPLATE" --request-template-sha256 "$TEMPLATE_SHA" \
    --timeout 5400 --engine-pid "$EP" 2>&1 | tail -2 || FIRE_RC=1
  REC_AFTER=$(stat -c %y "$REC_FILE" 2>/dev/null || echo none)
  if [ "$REC_AFTER" = none ]; then
    say "the prefill wrote no record (driver failed): this cycle has no rate and no engine digest"
    FIRE_RC=1
  elif [ "$REC_AFTER" = "$REC_BEFORE" ]; then
    say "the prefill left the record untouched (driver failed): no rate and no engine digest for this cycle"
    FIRE_RC=1
  fi
  F=$(faults_since "$T0")
  # The judge line is the engine log's own prefill line for THIS firing: the log is
  # appended to, so only the tail after the offset at fire time can belong to it.
  JUDGE=''
  if [ "$FIRE_RC" -eq 0 ]; then
    JUDGE=$(tr -d '\000' < "$OUT/logs/$LEG.engine.log" 2>/dev/null | tail -c +"$((ELOG_OFF + 1))" \
      | grep -a 'prompt 58423 tokens' | tail -1 \
      | sed -E 's/.*(prompt 58423 tokens = [^)]*).*/\1/' || true)
  else
    JUDGE='prefill failed (no record written)'
  fi
  HUGE=$(grep -i anonhugepages "/proc/$EP/smaps_rollup" 2>/dev/null | awk '{print $2, $3}' || echo 'gone')
  RSS=$(awk '/VmRSS/{printf "%.1f GiB", $2/1048576}' "/proc/$EP/status" 2>/dev/null || echo gone)
  if [ "$FIRE_RC" -eq 0 ]; then
    IFS='|' read -r R_SHA R_OVR R_NOTE <<< "$(record_engine "$REC_REL/cb-$i")" || true
  else
    R_SHA=''; R_OVR=''; R_NOTE=''
  fi
  SHA_TXT=${R_SHA:0:12}
  [ -n "$SHA_TXT" ] || SHA_TXT='?'
  OVR_TXT=''
  if [ "${R_OVR:-}" = 1 ]; then OVR_TXT=' (override)'; fi
  # The record, not the request, is the evidence: say so when they disagree. No record
  # is already reported above (nothing served), so it is not compared to anything here.
  if [ -n "$ENGINE_PREFIX" ] && [ -n "${R_SHA:-}" ] && [ "${R_OVR:-}" != 1 ]; then
    say "WARNING: the override was requested but the record names no override; this cycle ran the rig pin"
  elif [ -n "$ENGINE_PREFIX" ] && [ -n "${R_SHA:-}" ] && [ "${R_SHA}" != "$STRATA_FAULTDIFF_ENGINE_SHA256" ]; then
    say "WARNING: the record's engine sha256 is ${R_SHA}, the requested override is $STRATA_FAULTDIFF_ENGINE_SHA256"
  fi
  say "cycle $i done: engine $EP up sha=$SHA_TXT$OVR_TXT faults=$F rss=$RSS anonhuge=$HUGE"
  if [ -n "${R_NOTE:-}" ]; then say "note: $R_NOTE (no engine digest for this cycle)"; fi
  echo "    judge: $JUDGE"
  printf 'cb-%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$i" "$LEG" "${R_SHA:-}" "${R_OVR:-}" "$F" "$(tsv_field "$JUDGE")" \
    "$(tsv_field "$RSS")" "$(tsv_field "$HUGE")" "$T0" "$(date '+%Y-%m-%d %H:%M:%S')" >> "$TSV"
done
say "all cycles done; summary $TSV"
