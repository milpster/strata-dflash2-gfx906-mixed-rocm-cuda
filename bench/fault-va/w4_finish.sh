#!/usr/bin/env bash
# w4_finish.sh - the remaining W4 2x2 boots (cells 3-4) plus the W3s span pair.
#
# Why a script and setsid: an aborted orchestrator call killed a leg mid-prefill and
# the retry silently resumed from a parked checkpoint (reused 131072) instead of doing
# a cold read. Every step here is detached and every leg's reuse is asserted.
#
# Five guards, each for a failure this runner has had or would have had:
#   0. SERVER ARM IS DERIVED, NEVER ASSUMED. W4's whole purpose is to cross engine and
#      server, but port_ab_boot.sh defaults SERVER_ARM=$ARM. On 2026-11-25 a "cross" cell
#      was booted through that default and measured as a native control under a cross-cell
#      key (boots/W4-B-dec-r1.json: arm B, server_arm B), which is what produced a wrong
#      published conclusion. Every boot now takes --server-arm read from the window's own
#      schedule, and a cell the schedule does not know is refused rather than guessed.
#   1. prerequisite 0 runs ONCE before any boot. A refusal prints its FAIL/WAIVED lines and
#      exits 3 (4 when the precheck could not be read at all). On 2026-11-25 a 2.03 GiB
#      process and a backup refused every boot in about a second; the runner looped over the
#      cells, logging "boot <cell>" and looking alive for an hour while nothing reached the GPU.
#   2. one run at a time: an flock on out/w4-finish.lock. A held lock prints the holder and
#      exits 2 (a second runner would boot into the first one's sockets and port).
#   3. per-boot identity, checked after the health probe and BEFORE the leg: engine sha256 and
#      path, the SERVER arm and its tree, the HELPER sha256, the port, a boot record written
#      after this boot started, and the live process's own /proc/<pid>/exe. The record alone
#      cannot prove which process answers :8009. A mismatch is BOOT-IDENTITY-MISMATCH: stop,
#      fail the cell, go on.
#   4. per-leg coldness and freshness: the first request's judge line must read "0 reused", its
#      token count must belong to this shape, the record must be newer than this leg and must
#      name this engine. A killed leg leaves a parked checkpoint and the retry silently resumes
#      it; such a leg is INVALID, its reused count is logged, and it is not counted as measured.
#
# --dry-run prints the planned cells (engine, server, port, shape) and runs only the
# prerequisite check: it boots nothing, starts nothing and is safe with no GPU.
# --waive-nonengine passes the operator's non-engine RSS waiver to the pre-gate as well as to
# the boots; it is OFF by default so the waiver is always an explicit act.
set -uo pipefail
cd /mnt/srcds/Strata || exit 1
ROOT=/mnt/srcds/Strata
PY=python3
LEGS=bench/ab-v0210/rpcprof/port_ab_legs.py
BOOT=bench/ab-v0210/rpcprof/port_ab_boot.sh
STOP=bench/ab-v0210/rpcprof/port_ab_stop.sh
OUT=bench/ab-v0210/rpcprof/out/port-ab-20261123
LOGD=bench/fault-va/out/logs
LOCK=bench/fault-va/out/w4-finish.lock
DRY=0
WAIVE=0
CELLS_FAILED=0
LEGS_MEASURED=0
LEGS_INVALID=0
IDENTITY_EXPECTED=""
IDENTITY_FOUND=""

CHECK_LEG=""
CHECK_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --waive-nonengine) WAIVE=1; shift ;;
    --check-leg)   # RECORD ARM WINDOW SHAPE SINCE - audit one leg record, no GPU, no boot
      CHECK_LEG=$2; shift 2
      CHECK_ARGS=("$1" "$2" "$3" "$4"); shift 4 ;;
    -h|--help) echo "usage: w4_finish.sh [--dry-run] [--waive-nonengine]"
               echo "       w4_finish.sh --check-leg RECORD ARM WINDOW SHAPE SINCE_EPOCH"
               exit 0 ;;
    *) echo "w4_finish: unknown argument: $1" >&2
       echo "usage: w4_finish.sh [--dry-run] [--waive-nonengine]" >&2
       echo "       w4_finish.sh --check-leg RECORD ARM WINDOW SHAPE SINCE_EPOCH" >&2; exit 2 ;;
  esac
done

mkdir -p "$LOGD"
say() { echo "[$(date +%H:%M:%S)] $*"; }

# cell_spec WINDOW ARM BOOT REP -> "server_arm" from the window's own schedule.
# A cell the schedule does not plan is a bug in this runner, never a guess.
cell_spec() {
  $PY - "$ROOT" "$1" "$2" "$3" "$4" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/bench/ab-v0210/rpcprof")
import port_ab_legs as L
win, arm, boot, rep = sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
for row in L.schedule(win):
    if row["arm"] == arm and row["boot"] == boot and int(row["rep"]) == rep:
        print(row.get("server_arm") or arm)
        raise SystemExit(0)
raise SystemExit("no schedule row for %s arm %s boot %s rep %s" % (win, arm, boot, rep))
PY
}

arm_identity() {   # ARM -> "engine_sha256 engine_path helper_sha256 server_tree port"
  $PY - "$ROOT" "$1" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/bench/ab-v0210/rpcprof")
import port_ab_configs as pc
spec = pc.ARMS[sys.argv[2]]
print(spec["engine_sha256"], spec["engine"], spec["helper_sha256"], spec["server_tree"], pc.TUPLE["port"])
PY
}

server_tree_of() {  # SERVER_ARM -> the tree path that arm's server must run from
  $PY - "$ROOT" "$1" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/bench/ab-v0210/rpcprof")
import port_ab_configs as pc
print(pc.ARMS[sys.argv[2]]["server_tree"])
PY
}

shape_info() {     # WINDOW SHAPE -> "corpus_bytes tokens_expected gen_cap kind"
  $PY - "$ROOT" "$1" "$2" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/bench/ab-v0210/rpcprof")
import port_ab_contract as C
win, shape = sys.argv[2], sys.argv[3]
params = C.SHAPES[C.corpus_shape(shape)]
cap = C.gen_tokens(shape, win) if shape in C.LEGS else "-"
print(params["bytes"], params.get("tokens_expected"), cap, C.LEGS.get(shape, {}).get("kind", "-"))
PY
}

take_lock() {
  exec 9>>"$LOCK" || { say "cannot open lock $LOCK"; exit 2; }
  if ! flock -n 9; then
    say "LOCK HELD: $LOCK - another w4_finish run is live, refusing to start a second"
    if [ -s "$LOCK" ]; then sed 's/^/    holder: /' "$LOCK"; else say "    holder: (lock file empty)"; fi
    exit 2
  fi
  printf 'pid %s start %s script %s\n' "$$" "$(date -Is)" "$0" > "$LOCK"
  trap 'exec 9>&-' EXIT
}

# prerequisite 0 (read-only: /proc, ps, statvfs; no signals, no GPU). The tool's verdict is
# OPEN, OPEN (WAIVED: ...) or REFUSED - it never prints "PASS". Its EXIT STATUS is checked
# too: a precheck that prints OPEN and then fails while writing its snapshot has not opened
# anything. Tolerant of indentation so a formatting change cannot masquerade as a crash.
prereq_gate() {
  local log rc verdict
  log="$LOGD/prereq-w4run-$(date +%Y%m%d-%H%M%S).log"
  say "prerequisite 0 (pre-boot gate) -> $log"
  local args=(--out bench/fault-va/out/prereq-w4run --cache-dir "$OUT")
  [ "$WAIVE" -eq 1 ] && args+=(--waive-nonengine)
  $PY bench/ab-v0210/rpcprof/port_ab_prereq0.py "${args[@]}" > "$log" 2>&1
  rc=$?
  verdict=$(grep -aE '^[[:space:]]*VERDICT:[[:space:]]*' "$log" | tail -1 | sed 's/^[[:space:]]*//')
  case "$verdict|$rc" in
    "VERDICT: OPEN"*"|0")
      say "  gate $verdict"
      return 0 ;;
  esac
  if [ -n "$verdict" ]; then say "  gate $verdict (exit $rc)"; else say "  gate: NO VERDICT LINE (exit $rc) - the precheck did not finish"; fi
  say "  REFUSING to boot any cell - the gate's FAIL/WAIVED lines:"
  grep -aE 'FAIL:|WAIVED' "$log" | sed 's/^ */    /' || say "    (none printed; read $log)"
  say "  full snapshot and output: $log"
  [ -n "$verdict" ] || { say "  an absent verdict is a broken precheck, not a refusal"; return 4; }
  if [ "$WAIVE" -eq 0 ]; then
    say "  (this gate ran without --waive-nonengine while the boots pass it; re-run with"
    say "   --waive-nonengine if the operator's desktop RSS sum is the waived baseline)"
  fi
  return 3
}

wait_vram() {
  local i v
  for i in $(seq 1 40); do
    # The engine's cards are what matter; the harness records card2/card3 plus nvidia0, so a
    # card0/card1 sum can read zero forever. Sum every card and treat the GPU row as advisory.
    v=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card[0-9]+/{s+=$3} END{print s+0}')
    [ "${v:-0}" -lt 2000000000 ] && { say "  VRAM clear (${i}0s)"; return 0; }
    sleep 10
  done
  say '  WARNING: VRAM still held after 400s'; return 1
}

# The leg's own judge lines, read inside each request's recorded engine-log window.
# A judge line is "strata serve: prompt N tokens = R reused + K read in ...". Only the FIRST
# request decides coldness: a tg leg sends the same prompt twice and request 1 is cached by
# design. A request without its own window is INVALID - the engine log is shared, so falling
# back to the whole file would validate this leg on a previous leg's cold line.
leg_coldness() {   # RECORD ARM WINDOW SHAPE SINCE -> "VALID ..." or "INVALID <reason>"
  $PY - "$1" "$2" "$3" "$4" "$5" "$ROOT" <<'PY'
import json, re, sys
JUDGE = re.compile(r"prompt (\d+) tokens = (\d+) reused \+ (\d+)")
path, arm, win, shape, since, root = sys.argv[1:7]
sys.path.insert(0, root + "/bench/ab-v0210/rpcprof")
import port_ab_configs as pc
import port_ab_contract as C

def bad(reason):
    print("INVALID " + reason)
    raise SystemExit(0)

try:
    rec = json.load(open(path))
except (OSError, ValueError):
    bad("no leg record at " + path)

want_sha = pc.ARMS[arm]["engine_sha256"]
if rec.get("engine_sha256") != want_sha:
    bad("record engine_sha256 %s != arm %s %s" % (str(rec.get("engine_sha256"))[:12], arm, want_sha[:12]))
try:
    ts, started = float(rec.get("ts") or 0), float(since)
except (TypeError, ValueError):
    bad("record ts or leg start is not a number")
if ts < started - 1:
    bad("record ts %.0f predates this leg (%.0f): a previous run's file" % (ts, started))

expected_bytes = C.SHAPES[C.corpus_shape(shape)]["bytes"]
if rec.get("prompt_bytes") not in (None, expected_bytes):
    bad("record prompt_bytes %s != shape %s %d" % (rec.get("prompt_bytes"), shape, expected_bytes))

reqs = rec.get("requests") or []
if not reqs:
    bad("record has no requests")
first = reqs[0]
start, end = first.get("elog_start"), first.get("elog_end")
if not (isinstance(start, int) and isinstance(end, int) and end > start):
    bad("request 0 has no engine-log window; refusing to judge it on the whole file")
try:
    with open(rec["elog"], "rb") as f:
        f.seek(start)
        blob = f.read(end - start).replace(b"\0", b"").decode("utf-8", "replace")
except (OSError, KeyError, TypeError) as error:
    bad("cannot read the engine-log window: %s" % error)

hits = JUDGE.findall(blob)
if not hits:
    bad("no judge line in request 0's window")
tokens, reused = int(hits[0][0]), int(hits[0][1])
# tokens_expected is the corpus's nominal size (144000 for dec144k) while the engine reports
# the tokens it actually tokenised (143893). A 10% band still separates every shape in the
# contract, which differ by 10x and 70x, so it catches a cross-shape judge line.
nominal = C.SHAPES[C.corpus_shape(shape)].get("tokens_expected") or 0
if nominal and abs(tokens - nominal) > 0.10 * nominal:
    bad("judge line serves %d tokens, which is not shape %s (~%d)" % (tokens, shape, nominal))
if reused != 0:
    bad("reused %d on the first request != 0 (warm; a resumed checkpoint)" % reused)
# The leg's KIND is per window: dec144k is a tg leg (two requests, the second cached by
# design) in W2 and W4 but a plain prefill elsewhere. LEGS[shape]["kind"] is only the
# default, so a tg leg must not be judged on request 1's reuse.
leg = C.LEGS.get(shape, {})
kind = (leg.get("kind_by_window") or {}).get(win) or leg.get("kind")
GEN = re.compile(r"prompt (\d+) tokens = (\d+) reused \+ \d+(?: of \d+)? read in [^,]*, (\d+) generated")
if kind == "tg":
    # A tg leg is defined by its SECOND request: the same prompt again, cached, then generation.
    # Request 0 alone proves only that a cold prefill happened, so a record whose decode request
    # never ran would pass as measured and could then supply a decode rate. That is the shape of
    # the W4-B-dec-r1 leg, whose first attempt really did stop after the prefill.
    if len(reqs) < 2:
        bad("a tg leg needs 2 requests; this record has %d, so the decode request is missing"
            % len(reqs))
    second = reqs[1]
    s2, e2 = second.get("elog_start"), second.get("elog_end")
    if not (isinstance(s2, int) and isinstance(e2, int) and e2 > s2):
        bad("request 1 has no engine-log window; refusing to accept a decode that cannot be shown")
    try:
        with open(rec["elog"], "rb") as f:
            f.seek(s2)
            blob2 = f.read(e2 - s2).replace(b"\0", b"").decode("utf-8", "replace")
    except (OSError, KeyError, TypeError) as error:
        bad("cannot read request 1's engine-log window: %s" % error)
    gen = GEN.findall(blob2)
    if not gen:
        bad("no judge line with a generation count in request 1's window")
    tok2, gen2 = int(gen[0][0]), int(gen[0][2])
    if gen2 <= 0:
        bad("request 1 generated %s tokens; a decode request that produced nothing is not a leg"
            % gen2)
    # Request 1 must be the SAME prompt as request 0, or a short foreign decode could supply the
    # rate. Same 10% band as request 0, which still separates every shape in the contract.
    if nominal and abs(tok2 - nominal) > 0.10 * nominal:
        bad("request 1 serves %d tokens, which is not the same prompt as request 0 (%d, ~%d)"
            % (tok2, tokens, nominal))
    # Bounded by the shape's own cap: a rate above the configured maximum is not this leg.
    cap = C.gen_tokens(shape, win) if (shape in C.LEGS and win in C.WINDOWS) else None
    if isinstance(cap, int) and gen2 > cap:
        bad("request 1 generated %d tokens, above the shape's cap of %d" % (gen2, cap))
else:
    rest = [int(r) for _, r, _ in hits[1:]]
    if any(rest):
        bad("reused %s on a %s leg (every request must be cold)" % (rest, kind))
print("VALID reused=0 tokens=%d kind=%s" % (tokens, kind))
PY
}

# The expected tuple, proven from five places: the boot wrapper's identity line (the plan it
# sourced after prerequisite 0 passed), the engine log's "engine started:" path, the boot
# record (engine sha, path, SERVER arm, server tree, helper sha, port, start time), the record's
# own mtime, and the live process's /proc/<pid>/exe. The record alone proves a plan ran; only
# the live process proves what is answering the port.
identity_check() {   # KEY ARM BOOT_EPOCH SERVER_ARM -> 0 when the boot is the expected tuple
  local key=$1 arm=$2 since=$3 srv_arm=$4
  local want_sha="" want_exe="" want_helper="" want_srv="" want_port="" want_srv_arm="$srv_arm"
  local blew_sha="" blew_helper="" elog_exe="" rec_line="" bad=0 pid="" live_exe=""
  local blo="$LOGD/$key-boot.log" elog="$OUT/logs/$key.engine.log" rec="$OUT/boots/$key.json"
  read -r want_sha want_exe want_helper want_srv want_port < <(arm_identity "$arm")
  # BOTH paths must be absolute: the boot record stores an absolute server_tree
  # (e.g. /mnt/srcds/Strata/run/port0218-server), and comparing it to the relative
  # "run/port0218-server" from the arm configs would fail every legitimate boot.
  want_srv="$ROOT/$(server_tree_of "$srv_arm")"
  want_exe="$ROOT/$want_exe"
  IDENTITY_EXPECTED="engine $arm ${want_sha:0:12} $want_exe + helper ${want_helper:0:8} | server $srv_arm $want_srv | port $want_port"

  if [ -f "$blo" ]; then
    blew_sha=$(grep -a -m1 "^port_ab_boot: $key engine " "$blo" | awk '{print $4}')
    blew_helper=$(grep -a -m1 "^port_ab_boot: $key engine " "$blo" | awk '{print $6}')
    if grep -qa 'REFUSING' "$blo"; then
      say "    boot log carries a REFUSING line: $(grep -a -m1 'REFUSING' "$blo" | cut -c1-120)"
      bad=$((bad + 1))
    fi
  else
    say "    no boot log at $blo"
    bad=$((bad + 1))
  fi
  [ "${blew_sha:0:12}" = "${want_sha:0:12}" ] || {
    say "    boot log engine sha '${blew_sha:-none}' != expected arm $arm '${want_sha:0:12}'"
    bad=$((bad + 1)); }
  [ "${blew_helper:0:8}" = "${want_helper:0:8}" ] || {
    say "    boot log helper sha '${blew_helper:-none}' != expected '${want_helper:0:8}'"
    bad=$((bad + 1)); }

  if [ -f "$elog" ]; then
    elog_exe=$(tr -d '\000' < "$elog" | grep -a 'engine started: ' | tail -1 \
      | sed 's/.*engine started: //' | awk '{print $1}')
  fi
  [ "$elog_exe" = "$want_exe" ] || {
    say "    engine log's last 'engine started:' path '${elog_exe:-none}' != expected '$want_exe'"
    bad=$((bad + 1)); }

  if [ -f "$rec" ]; then
    rec_line=$($PY -c 'import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: d={}
print("|".join(str(d.get(k) or "-") for k in ("engine_sha256","engine","port","server_arm","server_tree","helper_sha256","started_epoch")))' "$rec")
  fi
  local rec_sha rec_exe rec_port rec_srv_arm rec_srv_tree rec_helper rec_started
  IFS='|' read -r rec_sha rec_exe rec_port rec_srv_arm rec_srv_tree rec_helper rec_started <<< "${rec_line:------}"
  [ "$rec_sha" = "$want_sha" ] || { say "    boot record engine_sha256 '${rec_sha:-missing}' != expected '$want_sha'"; bad=$((bad + 1)); }
  [ "$rec_exe" = "$want_exe" ] || { say "    boot record engine '${rec_exe:-missing}' != expected '$want_exe'"; bad=$((bad + 1)); }
  [ "$rec_port" = "$want_port" ] || { say "    boot record port '${rec_port:-missing}' != expected '$want_port'"; bad=$((bad + 1)); }
  # The server half: the arm with the same config cwd would pass every engine check and still
  # measure a native control, which is exactly the 2026-11-25 defect.
  [ "$rec_srv_arm" = "$want_srv_arm" ] || { say "    boot record server_arm '${rec_srv_arm:-missing}' != expected '$want_srv_arm'"; bad=$((bad + 1)); }
  [ "$rec_srv_tree" = "$want_srv" ] || { say "    boot record server_tree '${rec_srv_tree:-missing}' != expected '$want_srv'"; bad=$((bad + 1)); }
  [ "$rec_helper" = "$want_helper" ] || { say "    boot record helper_sha256 '${rec_helper:-missing}' != expected '$want_helper'"; bad=$((bad + 1)); }
  awk -v started="$rec_started" -v since="$since" 'BEGIN{exit !(started + 0 >= since - 1)}' || {
    say "    boot record started_epoch ${rec_started} predates this boot ($since)"
    bad=$((bad + 1)); }
  if [ -f "$rec" ]; then
    [ "$(stat -c %Y "$rec")" -ge "$((since - 1))" ] || {
      say "    boot record mtime is older than this boot; it is a previous run's file"
      bad=$((bad + 1)); }
  fi

  # What is actually answering the port, independent of any record. Resolve the pid by EXACT
  # exe path rather than through bench/fault-va/engine_pid.sh: that helper accepts any basename
  # matching strata-*, which is meant to cover the expert helper too, and it returns the first
  # match. A helper matched first would make this check fail closed on a legitimate boot, so the
  # expected path itself is the search key here.
  pid=""
  live_exe=""
  for cand in $(ps -eo pid=,args= | awk '$3=="--serve" {print $1}'); do
    cand_exe=$(readlink "/proc/$cand/exe" 2>/dev/null || true)
    if [ "$cand_exe" = "$want_exe" ]; then pid=$cand; live_exe=$cand_exe; break; fi
  done
  if [ -z "$pid" ]; then
    say "    no process runs '$want_exe --serve', so the port is answered by something unknown"
    bad=$((bad + 1))
  fi

  # The server half, live. Until now it was proven only by the boot record, which the boot
  # itself writes: a record can say "server A" while a different tree answers the port. The
  # boot launches "$PY $TREE/serve/server.py --engine strata --config .. --port <port>", so
  # the tree's own path in a live argv is the evidence, not the record.
  local srv_pid=""
  # ps -eo pid= already suppresses the header, so there is nothing to skip here
  for cand in $(ps -eo pid=); do
    cand_cmd=$(tr '\000' ' ' < "/proc/$cand/cmdline" 2>/dev/null || true)
    case "$cand_cmd" in
      *"$want_srv/serve/server.py"*)
        case "$cand_cmd" in
          *"--port $want_port"*) srv_pid=$cand; break ;;
        esac ;;
    esac
  done
  if [ -z "$srv_pid" ]; then
    say "    no live server runs '$want_srv/serve/server.py --port $want_port'"
    bad=$((bad + 1))
  fi

  IDENTITY_FOUND="boot-log engine ${blew_sha:0:12} helper ${blew_helper:0:8} | engine-log ${elog_exe:-none} | record engine ${rec_sha:0:12} server '${rec_srv_arm:-?}' '${rec_srv_tree:-?}' helper ${rec_helper:0:8} port ${rec_port:-?} | live engine ${pid:-none} ${live_exe:-none} | live server ${srv_pid:-none} $want_srv"
  return "$bad"
}

# Fire every shape of this boot in ONE foreground call, then judge each record.
#
# Why foreground: port_ab_legs.py --run is a sequential subprocess.call per shape and returns a
# real exit code, so waiting on it needs no process-name polling. The previous version polled
# pgrep and its first poll ran before port_ab_legs had spawned ab9_driver: "not there yet" read
# as "finished", the record looked missing, and the runner stopped its own pair mid-prefill -
# manufacturing the resumed-checkpoint leg this script exists to prevent. Passing all shapes in
# one call also removes the window in which a second call could match the first driver's
# still-reaping process.
fire_leg() {   # KEY WIN ARM BOOT REP SINCE SHAPE [SHAPE ...] -> 0 all measured, 1 any invalid
  local key=$1 win=$2 arm=$3 group=$4 rep=$5 since=$6; shift 6
  local shapes=("$@") shape announce=() verdict rc bad=0
  for shape in "${shapes[@]}"; do announce+=(--shape "$shape"); done
  say "  firing ${shapes[*]}"
  timeout 3600 $PY "$LEGS" --window "$win" --arm "$arm" --boot "$group" --rep "$rep" \
    "${announce[@]}" --run > "$LOGD/$key-leg.log" 2>&1
  rc=$?
  # A non-zero exit means AT LEAST ONE shape's subprocess failed, not that all did. The
  # records are still judged below: a shape that ran to completion leaves a valid record and
  # must be counted, or a half-failed boot would silently discard good evidence. Only a
  # timeout (124) aborts judging, because the call was killed and the run is truncated.
  if [ "$rc" -eq 124 ]; then
    say "  LEG-TIMEOUT $key after 3600s - records are still judged below, but the run is truncated"
    CELLS_FAILED=$((CELLS_FAILED + 1))
  elif [ "$rc" -ne 0 ]; then
    say "  leg command reported failure for $key (exit $rc) - judging per-shape records anyway"
    say "    see $LOGD/$key-leg.log"
    CELLS_FAILED=$((CELLS_FAILED + 1))
  fi
  say '  leg finished; cold-read check:'
  tr -d '\000' < "$OUT/logs/$key.engine.log" 2>/dev/null \
    | grep -aE 'prompt [0-9]+ tokens = ' | tail -2 | sed 's/^/    /' | cut -c1-150
  for shape in "${shapes[@]}"; do
    verdict=$(leg_coldness "$OUT/records/$key/$arm-$shape.json" "$arm" "$win" "$shape" "$since")
    case "$verdict" in
      VALID*)
        say "  LEG VALID $key/$shape: $verdict"
        LEGS_MEASURED=$((LEGS_MEASURED + 1)) ;;
      *)
        say "  LEG INVALID $key/$shape: ${verdict#INVALID } - not counted"
        LEGS_INVALID=$((LEGS_INVALID + 1)); bad=1 ;;
    esac
  done
  return "$bad"
}

one_boot() {   # WIN ARM GROUP REP SHAPE [SHAPE2 ...] - server arm from the schedule
  local win=$1 arm=$2 group=$3 rep=$4; shift 4
  local shapes=("$@")
  local key="$win-$arm-$group-r$rep"
  local srv_arm i h since shape
  if ! srv_arm=$(cell_spec "$win" "$arm" "$group" "$rep"); then
    say "REFUSING cell $key: the window's schedule has no such boot (engine $arm, boot $group, rep $rep)"
    CELLS_FAILED=$((CELLS_FAILED + 1)); return 1
  fi
  if [ "$DRY" = 1 ]; then
    local d_sha d_exe d_helper d_tree d_port s_bytes s_tok s_cap s_kind
    read -r d_sha d_exe d_helper d_tree d_port < <(arm_identity "$arm")
    : "$d_tree"   # field order is arm_identity's; the server tree printed below is the one used
    say "  DRY $key: engine $arm ${d_sha:0:12} $d_exe + helper ${d_helper:0:8}"
    say "      server $srv_arm $(server_tree_of "$srv_arm") (passes --server-arm $srv_arm), port $d_port"
    for shape in "${shapes[@]}"; do
      read -r s_bytes s_tok s_cap s_kind < <(shape_info "$win" "$shape")
      say "      shape $shape ($s_kind: $s_bytes B corpus ~$s_tok tokens, max_tokens $s_cap)"
    done
    return 0
  fi
  say "boot $key (engine $arm, server $srv_arm)"
  wait_vram || { say "  VRAM held; not booting $key"; CELLS_FAILED=$((CELLS_FAILED + 1)); return 1; }
  since=$(date +%s)
  local boot_args=("$win" "$arm" "$group" "$rep" --server-arm "$srv_arm")
  [ "$WAIVE" -eq 1 ] && boot_args+=(--waive-nonengine)
  setsid nohup bash "$BOOT" "${boot_args[@]}" > "$LOGD/$key-boot.log" 2>&1 < /dev/null &
  h=""
  for i in $(seq 1 45); do
    h=$(curl -fsS -m 2 http://127.0.0.1:8009/health 2>/dev/null | head -c 12 || true)
    [ -n "$h" ] && { say "  healthy after ~$((i*10))s"; break; }
    sleep 10
  done
  if [ -z "${h:-}" ]; then
    say "  NOT HEALTHY - skipping"; tail -3 "$LOGD/$key-boot.log"
    CELLS_FAILED=$((CELLS_FAILED + 1)); return 1
  fi
  if ! identity_check "$key" "$arm" "$since" "$srv_arm"; then
    say "BOOT-IDENTITY-MISMATCH $key expected=[$IDENTITY_EXPECTED] found=[$IDENTITY_FOUND]"
    bash "$STOP" "$win" "$arm" "$group" "$rep" 2>&1 | tail -2 | sed 's/^/    /'
    CELLS_FAILED=$((CELLS_FAILED + 1)); return 1
  fi
  say "  identity ok: $IDENTITY_FOUND"
  fire_leg "$key" "$win" "$arm" "$group" "$rep" "$since" "${shapes[@]}"
  bash "$STOP" "$win" "$arm" "$group" "$rep" 2>&1 | tail -2 | sed 's/^/    /'
}

run_plan() {
  say 'W4 cells 3-4 (rep 2 of each cross cell)'
  one_boot W4 B dec 2 dec144k
  one_boot W4 A dec 2 dec144k
  say 'W3s span pair: discarded warmup r0, then claim r1, both shapes each boot'
  one_boot W3s A spans 0 dec144k-spans pp239k-spans
  one_boot W3s B spans 0 dec144k-spans pp239k-spans
  one_boot W3s A spans 1 dec144k-spans pp239k-spans
  one_boot W3s B spans 1 dec144k-spans pp239k-spans
  say 'note: W3s r2 exists in the schedule and is NOT run here, so W3s is n=1/arm (descriptive)'
}

# --check-leg: judge one existing leg record with the same guard the runner uses. This exists
# so the guard itself can be audited against known-cold and known-warm records without a GPU.
if [ -n "$CHECK_LEG" ]; then
  if [ "${#CHECK_ARGS[@]}" -ne 4 ]; then
    echo "--check-leg needs RECORD ARM WINDOW SHAPE SINCE_EPOCH; got ${#CHECK_ARGS[@]} of 4" >&2
    exit 2
  fi
  [ -r "$CHECK_LEG" ] || { echo "cannot read $CHECK_LEG" >&2; exit 2; }
  verdict=$(leg_coldness "$CHECK_LEG" "${CHECK_ARGS[0]}" "${CHECK_ARGS[1]}" "${CHECK_ARGS[2]}" "${CHECK_ARGS[3]}")
  echo "$verdict"
  case "$verdict" in VALID*) exit 0 ;; *) exit 1 ;; esac
fi

if [ "$DRY" != 1 ]; then take_lock; fi
prereq_gate
gate=$?
if [ "$DRY" = 1 ]; then
  say 'DRY RUN: prerequisite check and argument parsing only; no boot, no leg, no process started'
  run_plan
  if [ "$gate" -eq 0 ]; then say 'DRY RUN: the gate would open the window'; exit 0; fi
  say 'DRY RUN: the gate does not open a window - a real run would boot nothing and exit with that code'
  exit "$gate"
fi
[ "$gate" -eq 0 ] || exit "$gate"

run_plan
say "done: $LEGS_MEASURED leg(s) measured cold, $LEGS_INVALID leg(s) INVALID, $CELLS_FAILED failure(s)"
$PY "$LEGS" --check W4 2>&1 | tail -12
$PY "$LEGS" --check W3s 2>&1 | tail -8
if [ "$CELLS_FAILED" -ne 0 ] || [ "$LEGS_INVALID" -ne 0 ]; then
  say "exiting non-zero: $CELLS_FAILED cell failure(s), $LEGS_INVALID invalid leg(s)"
  exit 1
fi
exit 0
