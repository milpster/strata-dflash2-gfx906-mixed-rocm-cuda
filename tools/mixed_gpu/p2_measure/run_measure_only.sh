#!/usr/bin/env bash
# Measure-only re-run (NO outage): helper + harness use only the CUDA card (RTX 3080, 8 GB);
# production on the HIP GPUs stays UP throughout.
# Fixes vs the failed 03:34 window attempt:
#   1. clean env (env -i) — the inherited HSA_*/ROCm shell env broke CUDA enumeration
#      ("no such CUDA device"); a clean-env probe enumerated fine.
#   2. --experts <native gguf> — the pack has no experts.bin; the frozen driver's precedent
#      is HELPER_EXPERTS="../Swift-…gguf" resolved from the pack cwd (absolute form used here).
#   3. VRAM budgeted for 8 GB: helper --reserve-mib 4000, harness --max-device-mib 3000.
set -uo pipefail

W=/mnt/user/Strata/worktrees/cuda-helper
OUT=/tmp/agent/p2win
EVID=$W/build-mixed-gpu/p2-measure-window1
HELPER_BIN=$W/build-mixed-gpu/engine-cuda/strata-expert-helper
HARNESS=${HARNESS_BIN:-$W/build-mixed-gpu/engine-cuda/p2_measure_harness}
SOCK=$OUT/measure.sock
CLEANENV="env -i PATH=$PATH HOME=$HOME TERM=dumb"

log() { printf '%s %s\n' "$(date --iso-8601=seconds)" "$*" | tee -a "$EVID/measure-only.log"; }

HELPER_PID=""
cleanup() {
  if [ -n "$HELPER_PID" ]; then
    log "CLEANUP: TERM helper $HELPER_PID"
    kill -TERM "$HELPER_PID" 2>/dev/null
    for i in 1 2 3 4 5 6; do kill -0 "$HELPER_PID" 2>/dev/null || break; sleep 5; done
    kill -0 "$HELPER_PID" 2>/dev/null && kill -KILL "$HELPER_PID" 2>/dev/null
  fi
  log "CLEANUP done"
}
trap cleanup EXIT

log "MEASURE-ONLY START (production stays UP)"
rm -f "$SOCK"
: > "$EVID/helper2.log"
$CLEANENV "$HELPER_BIN" --pack /mnt/user/ai/iq3xxs-15 \
  --experts ../Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf \
  --socket "$SOCK" --device 0 --slots 3400 --reserve-mib 4000 \
  --begin-timeout 600000 --timeout 600000 >> "$EVID/helper2.log" 2>&1 &
HELPER_PID=$!
log "HELPER pid=$HELPER_PID (clean env, native-gguf experts, slots 3400, reserve 4000)"
for i in $(seq 1 120); do
  [ -S "$SOCK" ] && grep -q 'listening' "$EVID/helper2.log" 2>/dev/null && break
  kill -0 "$HELPER_PID" 2>/dev/null || { log "HELPER died early:"; tail -5 "$EVID/helper2.log"; break; }
  sleep 5
done
if [ -S "$SOCK" ] && grep -q 'listening' "$EVID/helper2.log" 2>/dev/null; then
  log "HELPER READY"
else
  log "HELPER NOT READY - aborting (typed evidence in helper2.log)"
  exit 6
fi

log "HARNESS start (max-device-mib 3000, slots 3400, reserve 4000)"
# ASAN harness runs need the CUDA shadow-gap workaround (probe-verified 2026-10-29: without it
# cudaGetDeviceCount sees no device; protect_shadow_gap=0 fixes enumeration); inert for normal binaries.
$CLEANENV ${ASAN_PASSTHROUGH:+ASAN_OPTIONS=$ASAN_PASSTHROUGH} "$HARNESS" --pack /mnt/user/ai/iq3xxs-15 \
  --socket "$SOCK" --activations "$OUT/moe" \
  --out "$W/build-mixed-gpu/p2-measure-harness/measure.json" \
  --slots 3400 --reserve-mib 4000 \
  --max-device-mib 3000 >> "$EVID/harness2.log" 2>&1
HARNESS_RC=$?
log "HARNESS rc=$HARNESS_RC"

log "HELPER STOP: SIGTERM (repeat live test)"
kill -TERM "$HELPER_PID"
HELPER_EXITED=no
for i in $(seq 1 12); do
  kill -0 "$HELPER_PID" 2>/dev/null || { HELPER_EXITED=yes; break; }
  sleep 5
done
if [ "$HELPER_EXITED" = yes ]; then
  log "HELPER STOP: PASS (exited within 60s)"
else
  log "HELPER STOP: FAIL - SIGKILL"
  kill -KILL "$HELPER_PID" 2>/dev/null
fi
HELPER_PID=""
trap - EXIT

cp -a "$W/build-mixed-gpu/p2-measure-harness/measure.json" "$EVID/" 2>/dev/null
cp -a "$W/build-mixed-gpu/p2-measure-harness/measure.json.table.txt" "$EVID/" 2>/dev/null
{
  echo "measure_only harness_rc=$HARNESS_RC helper_sigterm_exit=$HELPER_EXITED"
  echo "measure.json bytes: $(stat -c%s "$EVID/measure.json" 2>/dev/null || echo 0)"
} >> "$EVID/reconcile.txt"
log "MEASURE-ONLY END (band application stays parent-only)"
exit $(( HARNESS_RC != 0 ? 1 : 0 ))
