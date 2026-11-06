#!/usr/bin/env bash
# P2 measurement window (parent-owned; runbook: .slim/deepwork/mixed-cuda-finish/measurement-window-runbook.md)
# freeze -> prod down -> capture (8 fixtures x 16 layers) -> helper -> harness -> SIGTERM helper
# (live test of the P1 item-0 fix) -> RESTORE PROD FIRST -> archive evidence.
# The restore trap makes production restoration unconditional: it fires on normal exit AND on interruption.
set -uo pipefail

W=/mnt/user/Strata/worktrees/cuda-helper
OUT=/tmp/agent/p2win
EVID=$W/build-mixed-gpu/p2-measure-window1
LAUNCHER=/mnt/user/Strata/launch-prod-swift15-dflash-w4-q4_0.sh
ENGINE=$W/build-mixed-gpu/r4-dflash-after/bin/strata-hip
HELPER_BIN=$W/build-mixed-gpu/engine-cuda/strata-expert-helper
HARNESS=$W/build-mixed-gpu/engine-cuda/p2_measure_harness
SOCK=$OUT/helper.sock

mkdir -p "$OUT" "$EVID"
: > "$EVID/window.log"
log() { printf '%s %s\n' "$(date --iso-8601=seconds)" "$*" | tee -a "$EVID/window.log"; }

log "WINDOW START"
# 1. freeze identities + hashes
ss -H -ltnp 'sport = :8009' | tee "$EVID/prod-before.txt"
ss -H -ltnp 'sport = :8019' | tee "$EVID/8019-before.txt" || true
sha256sum "$ENGINE" "$HELPER_BIN" "$HARNESS" \
  "$W/tools/mixed_gpu/p2_measure/capture_window.py" \
  "$W/tools/mixed_gpu/p2_measure/p2_measure.cpp" \
  "$W/.slim/deepwork/mixed-cuda-finish/measurement-window-runbook.md" > "$EVID/window-hashes.txt" 2>&1

WPID=$(sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' "$EVID/prod-before.txt" | head -1)
if [ -z "$WPID" ]; then log "ABORT: no :8009 listener (production not up?)"; exit 2; fi
WEXE=$(readlink -f "/proc/$WPID/exe")
log "prod wrapper pid=$WPID exe=$WEXE"

RESTORED_FLAG=$OUT/.restored
restore_prod() {
  if [ -e "$RESTORED_FLAG" ]; then return 0; fi
  log "RESTORE: killing leftover window processes (if any)"
  pkill -TERM -f 'p2_measure_harness' 2>/dev/null; sleep 1
  pkill -TERM -f 'strata-expert-helper' 2>/dev/null; sleep 3
  pkill -KILL -f 'strata-expert-helper' 2>/dev/null
  for p in $(pgrep -f 'serve/server.py --engine strata --config /tmp/agent/p2win' 2>/dev/null); do
    kill -TERM "-$(ps -o pgid= -p "$p" | tr -d ' ')" 2>/dev/null
  done
  sleep 5
  for p in $(pgrep -f "$ENGINE --serve" 2>/dev/null); do kill -TERM "$p" 2>/dev/null; done
  sleep 10
  for p in $(pgrep -f "$ENGINE --serve" 2>/dev/null); do kill -KILL "$p" 2>/dev/null; done
  log "RESTORE: canonical launcher"
  bash "$LAUNCHER" >> "$EVID/launcher.log" 2>&1
  for i in $(seq 1 120); do
    if curl -s --max-time 5 http://127.0.0.1:8009/health | grep -q '"loaded": *true'; then
      log "RESTORE: health loaded:true after ${i}0s-ish polls"
      break
    fi
    sleep 10
  done
  curl -s --max-time 5 http://127.0.0.1:8009/health > "$EVID/health-after.txt" 2>&1
  ss -H -ltnp 'sport = :8019' > "$EVID/8019-after.txt" || true
  grep -q '"loaded": *true' "$EVID/health-after.txt" && touch "$RESTORED_FLAG" \
    && log "RESTORE VERIFIED" || log "RESTORE HEALTH CHECK FAILED - INSPECT health-after.txt"
}
trap restore_prod EXIT

# 2. take production down (authorized)
log "TAKEDOWN: TERM wrapper $WPID"
kill -TERM "$WPID"
for i in $(seq 1 24); do
  kill -0 "$WPID" 2>/dev/null || break
  sleep 5
done
if kill -0 "$WPID" 2>/dev/null; then
  log "TAKEDOWN: wrapper survived TERM 120s - SIGKILL"
  kill -KILL "$WPID"; sleep 5
fi
for p in $(pgrep -f 'build-dfsample-1/strata --serve|'"$ENGINE"' --serve' 2>/dev/null); do
  log "TAKEDOWN: leftover engine pid=$p - TERM"
  kill -TERM "$p"
done
sleep 15
for p in $(pgrep -f 'build-dfsample-1/strata --serve|'"$ENGINE"' --serve' 2>/dev/null); do
  log "TAKEDOWN: engine pid=$p survived TERM - SIGKILL (identity-verified by argv pattern)"
  kill -KILL "$p"
done
sleep 5
ss -H -ltnp 'sport = :8009' > "$EVID/8009-down.txt" || true
ss -H -ltnp 'sport = :8019' > "$EVID/8019-down.txt" || true
log "TAKEDOWN COMPLETE: $(grep -c LISTEN "$EVID/8009-down.txt" || true) listeners on 8009"

# 3. capture loop (checkpointed, resumable)
log "CAPTURE: starting 8 fixtures x layers 32-47"
python3 "$W/tools/mixed_gpu/p2_measure/capture_window.py" \
  --out "$OUT/moe" --engine "$ENGINE" \
  >> "$EVID/capture.log" 2>&1
CAPTURE_RC=$?
log "CAPTURE: rc=$CAPTURE_RC ($(grep -c '	ok$' "$OUT/moe/checkpoint.tsv" 2>/dev/null || echo 0) pairs ok)"

# 4. helper on device 0 (single CUDA GPU; handoff said --device 1 - impossible here)
: > "$EVID/helper.log"
"$HELPER_BIN" --pack /mnt/user/ai/iq3xxs-15 --socket "$SOCK" --device 0 \
  --begin-timeout 600000 --timeout 600000 >> "$EVID/helper.log" 2>&1 &
HELPER_PID=$!
log "HELPER: pid=$HELPER_PID waiting for socket"
for i in $(seq 1 120); do
  [ -S "$SOCK" ] && grep -q 'listening' "$EVID/helper.log" 2>/dev/null && break
  kill -0 "$HELPER_PID" 2>/dev/null || { log "HELPER: died early"; break; }
  sleep 5
done
[ -S "$SOCK" ] && log "HELPER READY" || log "HELPER NOT READY - harness will typed-refuse"

# 5. the measurement
log "HARNESS: starting"
"$HARNESS" --pack /mnt/user/ai/iq3xxs-15 --socket "$SOCK" \
  --activations "$OUT/moe" \
  --out "$W/build-mixed-gpu/p2-measure-harness/measure.json" \
  >> "$EVID/harness.log" 2>&1
HARNESS_RC=$?
log "HARNESS: rc=$HARNESS_RC"

# 6. SIGTERM helper - LIVE TEST of the P1 item-0 fix
log "HELPER STOP: sending SIGTERM to $HELPER_PID (P1 item-0 live test)"
kill -TERM "$HELPER_PID"
HELPER_EXITED=no
for i in $(seq 1 12); do
  kill -0 "$HELPER_PID" 2>/dev/null || { HELPER_EXITED=yes; break; }
  sleep 5
done
if [ "$HELPER_EXITED" = yes ]; then
  log "HELPER STOP: exited within 60s of SIGTERM - P1 ITEM0 LIVE TEST PASS"
else
  log "HELPER STOP: SURVIVED SIGTERM 60s - P1 ITEM0 LIVE TEST FAIL - SIGKILL"
  kill -KILL "$HELPER_PID"
fi

# 7. restore production FIRST, then archive
restore_prod
trap - EXIT

cp -a "$OUT/moe/checkpoint.tsv" "$EVID/" 2>/dev/null
cp -a "$EVID"/../p2-measure-harness/measure.json "$EVID/" 2>/dev/null
cp -a "$EVID"/../p2-measure-harness/measure.json.table.txt "$EVID/" 2>/dev/null
{
  echo "capture_rc=$CAPTURE_RC harness_rc=$HARNESS_RC helper_sigterm_exit=$HELPER_EXITED"
  echo "dump files: $(ls "$OUT/moe"/*.bin 2>/dev/null | wc -l)"
  echo "measure.json bytes: $(stat -c%s "$EVID/measure.json" 2>/dev/null || echo 0)"
} > "$EVID/reconcile.txt"
log "WINDOW END (band application is the parent's next step; no band lives in any script)"
exit $(( HARNESS_RC != 0 ? 1 : 0 ))
