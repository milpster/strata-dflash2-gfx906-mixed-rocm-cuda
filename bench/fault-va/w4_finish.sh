#!/usr/bin/env bash
# w4_finish.sh - the remaining W4 2x2 boots (cells 3-4) plus the W3s span pair.
#
# Why a script and setsid: an aborted orchestrator call killed a leg mid-prefill and
# the retry silently resumed from a parked checkpoint (reused 131072) instead of doing
# a cold read. Every step here is detached and every leg's reuse is asserted.
#
# Four guards, each for a failure this runner has already had:
#   1. prerequisite 0 runs ONCE before any boot. A refusal prints its FAIL/WAIVED lines and
#      exits 3. On 2026-11-25 a 2.03 GiB process and a backup process refused every boot in
#      about a second; the runner looped over the cells, logging "boot <cell>" and looking
#      alive for an hour while nothing ever reached the GPU.
#   2. one run at a time: an flock on out/w4-finish.lock. A held lock prints the holder and
#      exits 2 (a second runner would boot into the first one's sockets and port).
#   3. per-boot identity, checked after the health probe and BEFORE the leg: the boot log, the
#      engine log and the boot record must name the arm's engine sha256 (ARMS in
#      port_ab_configs.py), the arm's engine path and the expected port. A boot can otherwise
#      be served by a stale launcher from an interrupted boot, and the leg would then measure
#      the wrong binary tuple. A mismatch is BOOT-IDENTITY-MISMATCH: stop, fail the cell, go on.
#   4. per-leg coldness: the leg record's judge line(s) must read "0 reused". A killed leg
#      leaves a parked checkpoint and the retry silently resumes it; such a leg is INVALID,
#      its reused count is logged, and it is not counted as measured.
#
# --dry-run prints the planned cells in order (engine identity, port, prompt shape) and runs
# only the prerequisite check: it boots nothing, starts nothing and is safe with no GPU.
set -uo pipefail
cd /mnt/srcds/Strata || exit 1
ROOT=/mnt/srcds/Strata
PY=python3
LEGS=bench/ab-v0210/rpcprof/port_ab_legs.py
OUT=bench/ab-v0210/rpcprof/out/port-ab-20261123
LOGD=bench/fault-va/out/logs
LOCK=bench/fault-va/out/w4-finish.lock
DRY=0
CELLS_FAILED=0
LEGS_MEASURED=0
LEGS_INVALID=0
IDENTITY_EXPECTED=""
IDENTITY_FOUND=""

case "${1:-}" in
  --dry-run) DRY=1 ;;
  "") ;;
  -h|--help) echo "usage: w4_finish.sh [--dry-run]"; exit 0 ;;
  *) echo "w4_finish: unknown argument: $1" >&2; echo "usage: w4_finish.sh [--dry-run]" >&2; exit 2 ;;
esac

mkdir -p "$LOGD"
say() { echo "[$(date +%H:%M:%S)] $*"; }

arm_identity() {   # ARM -> "engine_sha256 engine_path server_tree port" (from port_ab_configs)
  $PY - "$ROOT" "$1" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/bench/ab-v0210/rpcprof")
import port_ab_configs as pc
spec = pc.ARMS[sys.argv[2]]
print(spec["engine_sha256"], spec["engine"], spec["server_tree"], pc.TUPLE["port"])
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
# OPEN, OPEN (WAIVED: nonengine_rss by operator) or REFUSED - it never prints "PASS", so an
# open verdict is what a run needs here.
prereq_gate() {
  local log verdict=""
  log="$LOGD/prereq-w4run-$(date +%Y%m%d-%H%M%S).log"
  say "prerequisite 0 (pre-boot gate) -> $log"
  $PY bench/ab-v0210/rpcprof/port_ab_prereq0.py \
    --out bench/fault-va/out/prereq-w4run \
    --cache-dir bench/ab-v0210/rpcprof/out/port-ab-20261123 > "$log" 2>&1
  verdict=$(grep -a '^VERDICT: ' "$log" | tail -1)
  case "$verdict" in
    "VERDICT: OPEN"*|"VERDICT: PASS"*)
      say "  gate $verdict"
      return 0 ;;
  esac
  if [ -n "$verdict" ]; then say "  gate $verdict"; else say "  gate: no VERDICT line (the precheck did not finish)"; fi
  say "  REFUSING to boot any cell - the gate's FAIL/WAIVED lines:"
  grep -aE 'FAIL:|WAIVED' "$log" | sed 's/^ */    /' || say "    (none printed; read $log)"
  say "  full snapshot and output: $log"
  say "  (this gate is the strict form; the boot below runs with --waive-nonengine, so export"
  say "   PREREQ0_WAIVE_NONENGINE=1 if the operator's desktop RSS sum is the waived baseline)"
  return 3
}

wait_vram() {
  local i v
  for i in $(seq 1 40); do
    v=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card0|^card1/{s+=$3} END{print s+0}')
    [ "$v" -lt 2000000000 ] && { say "  VRAM clear (${i}0s)"; return 0; }
    sleep 10
  done
  say '  WARNING: VRAM still held'; return 1
}

# The leg's own judge lines, read inside each request's recorded engine-log window. A judge
# line is "strata serve: prompt N tokens = R reused + K read in ..."; the first request of a
# leg must read R = 0 (a cold read), and a prefill leg has no warm request at all.
leg_coldness() {   # RECORD -> "VALID <reused per request>" or "INVALID <reason>"
  $PY - "$1" <<'PY'
import json, re, sys
JUDGE = re.compile(r"prompt (\d+) tokens = (\d+) reused \+ (\d+)")
path = sys.argv[1]
try:
    rec = json.load(open(path))
except (OSError, ValueError):
    print("INVALID no leg record at " + path)
    raise SystemExit(0)
try:
    blob = open(rec["elog"], "rb").read() if rec.get("elog") else b""
except OSError:
    blob = b""
counts = []
for req in rec.get("requests") or []:
    start, end = req.get("elog_start"), req.get("elog_end")
    seg = blob[start:end] if isinstance(start, int) and isinstance(end, int) else blob
    counts += [int(m[1]) for m in JUDGE.findall(seg.replace(b"\0", b"").decode("utf-8", "replace"))]
if not counts:
    print("INVALID no judge line in the leg's engine-log window (" + path + ")")
    raise SystemExit(0)
kind = rec.get("kind") or "-"
joined = ",".join(str(c) for c in counts)
if counts[0] != 0:
    print("INVALID reused " + str(counts[0]) + " on the first request != 0 (per-request " + joined + ")")
    raise SystemExit(0)
if kind != "tg" and any(c != 0 for c in counts):
    print("INVALID reused " + joined + " on a " + kind + " leg (every request must be cold)")
    raise SystemExit(0)
print("VALID reused " + joined)
PY
}

# The expected tuple, proven from three places: the boot wrapper's own identity line (the plan
# it sourced after prerequisite 0 passed), the engine log's "engine started:" path (the binary
# actually started) and the boot record (sha, path, port, and that this boot wrote it at all).
identity_check() {   # KEY ARM BOOT_EPOCH -> 0 when the boot is the expected tuple
  local key=$1 arm=$2 since=$3
  local want_sha="" want_exe="" want_srv="" want_port=""
  local blew_sha="" elog_exe="" rec_sha="" rec_exe="" rec_port="" rec_started=0
  local blo="$LOGD/$key-boot.log" elog="$OUT/logs/$key.engine.log" rec="$OUT/boots/$key.json"
  local bad=0
  read -r want_sha want_exe want_srv want_port < <(arm_identity "$arm")
  want_exe="$ROOT/$want_exe"
  IDENTITY_EXPECTED="engine $arm sha256 ${want_sha:0:12} $want_exe + server $want_srv port $want_port"

  if [ -f "$blo" ]; then
    blew_sha=$(grep -a -m1 "^port_ab_boot: $key engine " "$blo" | awk '{print $4}')
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

  if [ -f "$elog" ]; then
    elog_exe=$(tr -d '\000' < "$elog" | grep -a 'engine started: ' | tail -1 \
      | sed 's/.*engine started: //' | awk '{print $1}')
  fi
  [ "$elog_exe" = "$want_exe" ] || {
    say "    engine log's last 'engine started:' path '${elog_exe:-none}' != expected '$want_exe'"
    bad=$((bad + 1)); }

  if [ -f "$rec" ]; then
    read -r rec_sha rec_exe rec_port rec_started < <($PY -c 'import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    d = {}
print(d.get("engine_sha256") or "-", d.get("engine") or "-", d.get("port") or "-", d.get("started_epoch") or 0)' "$rec")
  fi
  [ "$rec_sha" = "$want_sha" ] || {
    say "    boot record engine_sha256 '${rec_sha:-missing}' != expected '$want_sha'"
    bad=$((bad + 1)); }
  [ "$rec_exe" = "$want_exe" ] || {
    say "    boot record engine '${rec_exe:-missing}' != expected '$want_exe'"
    bad=$((bad + 1)); }
  [ "$rec_port" = "$want_port" ] || {
    say "    boot record port '${rec_port:-missing}' != expected '$want_port'"
    bad=$((bad + 1)); }
  awk -v started="$rec_started" -v since="$since" 'BEGIN{exit !(started + 5 >= since)}' || {
    say "    boot record started_epoch ${rec_started} is older than this boot ($since): the boot never reached its record step, so something else is answering"
    bad=$((bad + 1)); }

  IDENTITY_FOUND="boot-log '${blew_sha:0:12}' engine-log '${elog_exe:-none}' record '${rec_sha:0:12}' path '${rec_exe:-none}' port '${rec_port:-none}' started ${rec_started}"
  return "$bad"
}

one_boot() {
  local win=$1 arm=$2 group=$3 rep=$4 shape=$5
  local key="$win-$arm-$group-r$rep"
  local i h since
  if [ "$DRY" = 1 ]; then
    local d_sha="" d_exe="" d_srv="" d_port="" s_bytes="" s_tok="" s_cap="" s_kind=""
    read -r d_sha d_exe d_srv d_port < <(arm_identity "$arm")
    read -r s_bytes s_tok s_cap s_kind < <(shape_info "$win" "$shape")
    say "  DRY $key: engine $arm ${d_sha:0:12} $d_exe + server $d_srv, port $d_port,"
    say "      shape $shape ($s_kind: $s_bytes B corpus ~$s_tok tokens, max_tokens $s_cap)"
    return 0
  fi
  say "boot $key"
  wait_vram
  since=$(date +%s)
  setsid nohup bash bench/ab-v0210/rpcprof/port_ab_boot.sh "$win" "$arm" "$group" "$rep" --waive-nonengine \
    > "$LOGD/$key-boot.log" 2>&1 < /dev/null &
  for i in $(seq 1 45); do
    h=$(curl -fsS -m 2 http://127.0.0.1:8009/health 2>/dev/null | head -c 12 || true)
    [ -n "$h" ] && { say "  healthy after ~$((i*10))s"; break; }
    sleep 10
  done
  if [ -z "${h:-}" ]; then
    say "  NOT HEALTHY - skipping"; tail -3 "$LOGD/$key-boot.log"
    CELLS_FAILED=$((CELLS_FAILED + 1)); return 1
  fi
  if ! identity_check "$key" "$arm" "$since"; then
    say "BOOT-IDENTITY-MISMATCH $key expected=[$IDENTITY_EXPECTED] found=[$IDENTITY_FOUND]"
    bash bench/ab-v0210/rpcprof/port_ab_stop.sh "$win" "$arm" "$group" "$rep" 2>&1 | tail -2 | sed 's/^/    /'
    CELLS_FAILED=$((CELLS_FAILED + 1)); return 1
  fi
  say "  identity ok: $IDENTITY_FOUND"
  say "  firing $shape"
  setsid nohup $PY "$LEGS" --window "$win" --arm "$arm" --boot "$group" --rep "$rep" --shape "$shape" --run \
    > "$LOGD/$key-leg.log" 2>&1 < /dev/null &
  local _d
  for _d in $(seq 1 90); do
    pgrep -f "ab9_driver.py --arm $arm" >/dev/null || break
    sleep 10
  done
  say '  leg finished; cold-read check:'
  tr -d '\000' < "$OUT/logs/$key.engine.log" 2>/dev/null \
    | grep -aE 'prompt [0-9]+ tokens = ' | tail -3 | sed 's/^/    /' | cut -c1-150
  local verdict
  verdict=$(leg_coldness "$OUT/records/$key/$arm-$shape.json")
  case "$verdict" in
    VALID*)
      say "  LEG VALID $key/$shape: first-request reused 0 ($verdict)"
      LEGS_MEASURED=$((LEGS_MEASURED + 1)) ;;
    *)
      say "  LEG INVALID $key/$shape: ${verdict#INVALID } - not a cold read, not counted"
      LEGS_INVALID=$((LEGS_INVALID + 1)) ;;
  esac
  bash bench/ab-v0210/rpcprof/port_ab_stop.sh "$win" "$arm" "$group" "$rep" 2>&1 | tail -2 | sed 's/^/    /'
}

if [ "$DRY" != 1 ]; then take_lock; fi
prereq_gate
gate=$?
if [ "$DRY" = 1 ]; then
  say 'DRY RUN: prerequisite check + argument parsing only; no boot, no leg, no process started'
  say 'W4 cells 3-4'
  one_boot W4 B dec 2 dec144k
  one_boot W4 A dec 2 dec144k
  say 'W3s span pair (144k, instrumented)'
  one_boot W3s B spans 1 dec144k-spans
  one_boot W3s A spans 1 dec144k-spans
  if [ "$gate" -eq 0 ]; then say 'DRY RUN: the gate would open the window'; exit 0; fi
  say 'DRY RUN: the gate refuses - a real run would boot nothing and exit 3'
  exit "$gate"
fi
[ "$gate" -eq 0 ] || exit "$gate"

say 'W4 cells 3-4'
one_boot W4 B dec 2 dec144k
one_boot W4 A dec 2 dec144k
say 'W3s span pair (144k, instrumented)'
one_boot W3s B spans 1 dec144k-spans
one_boot W3s A spans 1 dec144k-spans
say "done: $LEGS_MEASURED leg(s) measured cold, $LEGS_INVALID leg(s) INVALID, $CELLS_FAILED cell(s) failed"
$PY "$LEGS" --check W4 2>&1 | tail -12
