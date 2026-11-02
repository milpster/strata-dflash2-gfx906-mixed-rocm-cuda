#!/usr/bin/env bash
# Resume of the interrupted P2 measurement window.
# Incident 2026-10-29T02:23: the host killed the window bash tree mid-capture (69/128 ok);
# its EXIT trap fired but died before reaching the launcher; the capture python died with it;
# the in-flight pair's wrapper+engine survived as orphans and were TERM'd by the parent at 02:29.
# Preconditions (parent-verified 02:29): production DOWN (authorized downtime), no wrapper/engine/
# helper/harness processes, checkpoint.tsv 69 ok. This script resumes capture, measures, RESTORES.
set -uo pipefail

W=/mnt/user/Strata/worktrees/cuda-helper
OUT=/tmp/agent/p2win
EVID=$W/build-mixed-gpu/p2-measure-window1
LAUNCHER=/mnt/user/Strata/launch-prod-swift15-dflash-w4-q4_0.sh
ENGINE=$W/build-mixed-gpu/r4-dflash-after/bin/strata-hip
HELPER_BIN=$W/build-mixed-gpu/engine-cuda/strata-expert-helper
HARNESS=$W/build-mixed-gpu/engine-cuda/p2_measure_harness
SOCK=$OUT/helper.sock

log() { printf '%s %s\n' "$(date --iso-8601=seconds)" "$*" | tee -a "$EVID/resume.log"; }

# preconditions: production down, nothing running, checkpoint present
if ss -H -ltnp 'sport = :8009' | grep -q LISTEN; then
  log "ABORT: :8009 has a listener - production is UP; use run_measure_window.sh instead"; exit 2
fi
if pgrep -f 'serve/server\.py|strata-hip --serve|strata-expert-helper|p2_measure_harness' >/dev/null; then
  log "ABORT: leftover window/engine processes - clean them first"; exit 2
fi
[ -f "$OUT/moe/checkpoint.tsv" ] || { log "ABORT: no checkpoint to resume from"; exit 2; }

RESTORED_FLAG=$OUT/.restored
restore_prod() {
  [ -e "$RESTORED_FLAG" ] && return 0
  touch "$OUT/.restore-attempted"
  log "RESTORE: killing window processes"
  pkill -TERM -f 'capture_window\.py' 2>/dev/null
  pkill -TERM -f 'p2_measure_harness' 2>/dev/null
  sleep 1
  pkill -TERM -f 'strata-expert-helper' 2>/dev/null
  sleep 2
  pkill -KILL -f 'strata-expert-helper' 2>/dev/null
  for p in $(pgrep -f 'serve/server\.py --engine strata --config /tmp/agent/p2win' 2>/dev/null); do
    kill -TERM "$p" 2>/dev/null
  done
  sleep 3
  for p in $(pgrep -f "$ENGINE --serve" 2>/dev/null); do kill -TERM "$p" 2>/dev/null; done
  sleep 7
  for p in $(pgrep -f "$ENGINE --serve" 2>/dev/null); do kill -KILL "$p" 2>/dev/null; done
  log "RESTORE: canonical launcher"
  bash "$LAUNCHER" >> "$EVID/launcher-resume.log" 2>&1
  for i in $(seq 1 180); do
    curl -s --max-time 5 http://127.0.0.1:8009/health 2>/dev/null | grep -q '"loaded": *true' && break
    sleep 10
  done
  curl -s --max-time 5 http://127.0.0.1:8009/health > "$EVID/health-after-resume.txt" 2>&1
  ss -H -ltnp 'sport = :8019' > "$EVID/8019-after-resume.txt" || true
  if grep -q '"loaded": *true' "$EVID/health-after-resume.txt"; then
    touch "$RESTORED_FLAG"; log "RESTORE VERIFIED"
  else
    log "RESTORE HEALTH CHECK FAILED - INSPECT health-after-resume.txt"
  fi
}
trap restore_prod EXIT

log "RESUME START (capture continues from checkpoint)"
python3 "$W/tools/mixed_gpu/p2_measure/capture_window.py" \
  --out "$OUT/moe" --engine "$ENGINE" >> "$EVID/capture.log" 2>&1
CAPTURE_RC=$?
OKC=$(awk -F'\t' '$5=="ok"' "$OUT/moe/checkpoint.tsv" | wc -l)
log "CAPTURE rc=$CAPTURE_RC ok=$OKC/128"

: > "$EVID/helper.log"
"$HELPER_BIN" --pack /mnt/user/ai/iq3xxs-15 --socket "$SOCK" --device 0 \
  --begin-timeout 600000 --timeout 600000 >> "$EVID/helper.log" 2>&1 &
HELPER_PID=$!
log "HELPER pid=$HELPER_PID waiting for socket"
for i in $(seq 1 120); do
  [ -S "$SOCK" ] && grep -q 'listening' "$EVID/helper.log" 2>/dev/null && break
  kill -0 "$HELPER_PID" 2>/dev/null || { log "HELPER died early"; break; }
  sleep 5
done
if [ -S "$SOCK" ]; then log "HELPER READY"; else log "HELPER NOT READY - harness will typed-refuse"; fi

log "HARNESS start"
"$HARNESS" --pack /mnt/user/ai/iq3xxs-15 --socket "$SOCK" \
  --activations "$OUT/moe" \
  --out "$W/build-mixed-gpu/p2-measure-harness/measure.json" >> "$EVID/harness.log" 2>&1
HARNESS_RC=$?
log "HARNESS rc=$HARNESS_RC"

log "HELPER STOP: SIGTERM to $HELPER_PID (P1 item0 live test)"
kill -TERM "$HELPER_PID"
HELPER_EXITED=no
for i in $(seq 1 12); do
  kill -0 "$HELPER_PID" 2>/dev/null || { HELPER_EXITED=yes; break; }
  sleep 5
done
if [ "$HELPER_EXITED" = yes ]; then
  log "HELPER STOP: exited within 60s - P1 ITEM0 LIVE TEST PASS"
else
  log "HELPER STOP: SURVIVED SIGTERM 60s - ITEM0 LIVE TEST FAIL - SIGKILL"
  kill -KILL "$HELPER_PID" 2>/dev/null
fi

restore_prod
trap - EXIT

cp -a "$OUT/moe/checkpoint.tsv" "$EVID/" 2>/dev/null
cp -a "$W/build-mixed-gpu/p2-measure-harness/measure.json" "$EVID/" 2>/dev/null
cp -a "$W/build-mixed-gpu/p2-measure-harness/measure.json.table.txt" "$EVID/" 2>/dev/null
{
  echo "capture_rc=$CAPTURE_RC ok=$OKC/128 harness_rc=$HARNESS_RC helper_sigterm_exit=$HELPER_EXITED"
  echo "measure.json bytes: $(stat -c%s "$EVID/measure.json" 2>/dev/null || echo 0)"
} > "$EVID/reconcile.txt"
log "RESUME END (band application stays parent-only)"
exit $(( HARNESS_RC != 0 ? 1 : 0 ))
