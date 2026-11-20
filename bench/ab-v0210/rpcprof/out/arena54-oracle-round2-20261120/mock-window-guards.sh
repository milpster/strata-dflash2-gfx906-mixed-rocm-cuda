#!/usr/bin/env bash
# Logic walk of the arena54 window's round-2 guards, without booting or stopping anything.
# The pieces under test are EXTRACTED VERBATIM from the window script (not paraphrased) and
# run against a sandbox: no stop-all.sh, no launcher, no port, no state file of a real lane.
# The destructive or slow parts (restore_prod, curl, sleep, the 46 GiB sha256) are stubbed.
# Usage: mock-window-guards.sh [path-to-window-script]
set -uo pipefail
W=${1:-/mnt/srcds/Strata/bench/ab-v0210/rpcprof/gpu-rounds/arena54-window-20261120.sh}
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
OUT=$D/out; mkdir -p "$OUT" "$D/logs"
EXPERTS_BIN_BYTES=4096          # stand-in for 50294988800; the comparison is the same one

# --- shared stubs ----------------------------------------------------------------------------
restore_prod() { echo "    [stub restore_prod] would stop the lanes and verify prod"; }
curl()         { [ "${PORT_UP:-1}" = 1 ] && echo '{"models":["ud-iq3_xxs"]}' || true; }   # no port is touched
sleep()        { :; }
sup_field()    { python3 -c "import json;print(json.load(open('$1')).get('$2',''))" 2>/dev/null || echo ""; }
size_of()      { stat -c %s "$1" 2>/dev/null || echo 0; }
echo "extracting the pieces under test from: $W"

# --- extract a balanced if/fi block, or a shell function, verbatim, from a literal line ------
# A literal -F lookup picks the line, then awk walks to the matching 'fi' (or '}') counting
# nesting, so what runs here is byte-for-byte what the window script runs.
extract_from() { awk -v n="$2" -v end="$3" '
    NR >= n { began = 1 }
    began {
      print
      if (end == "brace") { if ($0 == "}") exit; next }
      m = split($0, w, /[^A-Za-z_]+/)             # tokenise: gawk reads /\bif\b/ as backspace, so count words
      for (i = 1; i <= m; i++) { if (w[i] == "if") d++; else if (w[i] == "fi") d-- }
      if (d <= 0) exit
    }' "$1"; }
line_of() { grep -n -F "$2" "$1" | head -1 | cut -d: -f1; }

L=$(line_of "$W" 'wait_ready() {');              WAIT_READY=$(extract_from "$W" "$L" brace)
L=$(line_of "$W" 'if [ -f "$EXPERTS_BIN" ] && [ "$BIN_BYTES" != "$EXPERTS_BIN_BYTES" ]; then')
PREFLIGHT=$(extract_from "$W" "$L" fi)
L=$(line_of "$W" 'if [ -f "$EXPERTS_BIN" ]; then');  SKIPBLOCK=$(extract_from "$W" "$L" fi)
for name in WAIT_READY PREFLIGHT SKIPBLOCK; do
  [ -n "${!name}" ] || { echo "FAIL: could not extract $name from $W"; exit 1; }
done
echo "extracted: wait_ready ($(printf '%s\n' "$WAIT_READY" | wc -l) lines), pre-flight ($(printf '%s\n' "$PREFLIGHT" | wc -l) lines), skip branch ($(printf '%s\n' "$SKIPBLOCK" | wc -l) lines)"

# ============================ #3 stale-log false-ready =======================================
STATE=$D/state.json
STALE=$D/logs/prod-swift15-f16kv-mixed-20261119-210335.log   # previous boot, already "ready"
NEW=$D/logs/prod-swift15-f16kv-mixed-20261120-070000.log     # this boot
printf 'an earlier run\nready: http://127.0.0.1:8009 (old boot)\n' > "$STALE"
printf 'engine starting...\n' > "$NEW"
eval "$WAIT_READY"

echo
echo "--- #3 A: the state file still names the PREVIOUS boot's log, which already contains"
echo "          'ready: http://', and that is also the pre-boot snapshot -> must refuse."
printf '{"engine_log": "%s"}\n' "$STALE" > "$STATE"
READY_LINE=""
if wait_ready "$STATE" "$STALE" 8009 3; then echo "  RESULT: accepted -> BUG (false ready)"; else echo "  RESULT: refused; READY_LINE='$READY_LINE' -> correct"; fi

echo
echo "--- #3 B: this boot recorded a NEW log path, ready and answering -> must accept."
printf 'engine starting...\nready: http://127.0.0.1:8009 (this boot)\n' > "$NEW"
printf '{"engine_log": "%s"}\n' "$NEW" > "$STATE"
if wait_ready "$STATE" "$STALE" 8009 3; then echo "  RESULT: accepted: READY_LINE='$READY_LINE' READY_LOG=$READY_LOG -> correct"; else echo "  RESULT: refused -> BUG"; fi

echo
echo "--- #3 C: new path with a ready line, but the port does not answer -> must refuse."
PORT_UP=0
if wait_ready "$STATE" "$STALE" 8009 2; then echo "  RESULT: accepted -> BUG"; else echo "  RESULT: refused (ready line AND a live port are both required) -> correct"; fi
PORT_UP=1

# ============================ #7 wrong-size pre-flight =======================================
echo
echo "--- #7 pre-flight, three states of the arena file (expected $EXPERTS_BIN_BYTES B):"
EXPERTS_BIN=$D/experts.bin
run_preflight() { ( BIN_BYTES=$(size_of "$EXPERTS_BIN"); eval "$PREFLIGHT"; echo "    (continued past the pre-flight)" ); echo "    status=$?"; }
rm -f "$EXPERTS_BIN";  echo "  (a) absent:";      run_preflight
head -c "$EXPERTS_BIN_BYTES" /dev/zero > "$EXPERTS_BIN"
echo "  (b) exact size:";                         run_preflight
head -c $((EXPERTS_BIN_BYTES / 4)) /dev/zero > "$EXPERTS_BIN"
echo "  (c) wrong size $(size_of "$EXPERTS_BIN") B (the iq_pack.py --experts-bin shape):"; run_preflight

# ============================ #6 experts.bin content record ==================================
echo
echo "--- #6 skip branch, three states of the content record:"
BIN_SHA_FILE=$D/experts-bin.sha256
echo "    record file: $BIN_SHA_FILE"
rm -f "$BIN_SHA_FILE"
head -c "$EXPERTS_BIN_BYTES" /dev/zero > "$EXPERTS_BIN"
run_skip() { ( BIN_SHA_PENDING=1; eval "$SKIPBLOCK"; echo "    (reached the end, BIN_SHA_PENDING=$BIN_SHA_PENDING)" ); echo "    status=$?"; }
echo "  (a) no record yet (deferred until the boot gate passes):";                run_skip
sha256sum "$EXPERTS_BIN" | sed 's| .*|  reference|' > "$BIN_SHA_FILE"
echo "  (b) record matches the file:";                                            run_skip
printf '1' | dd of="$EXPERTS_BIN" bs=1 seek=10 conv=notrunc status=none
echo "  (c) same size, different bytes -> content changed:";                      run_skip

echo
echo "--- #4 engine pid assert, four states of ENG_PID (kill -0 only probes; nothing is signalled):"
TRIAL_STATE=$D/trial-arena54-supervisor.json; printf '{"engine_pid": 1}' > "$TRIAL_STATE"
ENGASSERT=$(extract_from "$W" "$(line_of "$W" 'if [ "${ENG_PID:-0}" = "0" ] || ! kill -0 "$ENG_PID" 2>/dev/null \')" fi)
[ -n "$ENGASSERT" ] || { echo "FAIL: could not extract the engine-pid assert"; exit 1; }
run_eng() { ( ENG_PID=$1; eval "$ENGASSERT"; echo "    (passed the assert, ENG_PID=$ENG_PID)" ); echo "    status=$?"; }
# one live process whose command line does NOT mention this trial, and one that does
bash -c 'sleep 12' >/dev/null 2>&1 & FOREIGN=$!
python3 -c 'import time; time.sleep(12)' "$D/run/trial-arena54.sock" >/dev/null 2>&1 & TRIALENG=$!
command sleep 1   # the stubbed sleep() is a no-op inside wait_ready; give the dummies a moment here
echo "  (a) pid 0 (state file holds no engine_pid):";                  run_eng 0
echo "  (b) a dead pid:";                                              run_eng 999999
echo "  (c) live pid, wrong process $FOREIGN: [$(tr '\0' ' ' < /proc/$FOREIGN/cmdline)]"; run_eng "$FOREIGN"
echo "  (d) live pid carrying the trial marker $TRIALENG: [$(tr '\0' ' ' < /proc/$TRIALENG/cmdline)]"; run_eng "$TRIALENG"

echo
echo "walk finished; the sandbox is removed and nothing on this box was stopped, booted or probed."
