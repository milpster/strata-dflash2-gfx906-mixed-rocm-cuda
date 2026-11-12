#!/usr/bin/env bash
# Foreground mixed HIP/CUDA supervisor. Detach with setsid nohup when needed.
# Uses the tested window helper and the opt-in prompt tier.
# TUNED config (ab2-r1 PASS verdict 2026-10-31: long decode +7.7%, xl +6.5%, prompt tier live):
# rpcw candidate engine+helper, helper second channel (--socket2), prompt tier on
# (STRATA_PREFILL_HELPER=1 via the config env), occupancy pinned (slots4c sweep
# 2026-11-04: V7-squeezed promoted (08f5e690): fill-mib 6700 binds first at 4318 slots (bench r1+r2 + prod boot)
# 6.25 GiB; +13.6% decode median vs the old 4000-slot cap, read rate unchanged).
# 2026-11-05 pair fix: --begin-timeout 1800000 -> 63115200000 (the 30-min idle wait killed the
# helper after every idle half-hour; engine death still exits at once on socket close), and prod
# boots THROUGH the supervisor by default (--recover remains an alias),
# any peer death restarts BOTH (either both run or both reset).
set -euo pipefail
ROOT=${STRATA_PROD_TREE:-/mnt/srcds/Strata}
WORK="$ROOT/v0215"
# Keep the serving wrapper stable; pin the latest built engine/helper separately.
BIN="$ROOT/v0219/build-mixed-gpu"
PY="$ROOT/.venv/bin/python"
CONFIG="$ROOT/prod-config-swift15-iq3s-int8kv-dflash-w4-mixed.json"
SOCKET="$ROOT/run/prod-swift15-iq3s-mixed.sock"
SOCKET2="$ROOT/run/prod-swift15-iq3s-mixed2.sock"
HELPER="$BIN/rpcw-cuda/strata-expert-helper-patient"
# KSM is OPT-IN here (SPEC-stage2 lever 4): measured general_profit -109.9 MB on this pair
# (2026-11-08 17:22 baseline; -636 MB by 2026-11-09 11:01, pages_sharing 141 -> 0, ksmd ~0.1-0.2 %
# CPU) - the merge costs more than it saves, so the default is OFF and STRATA_KSM=1 opts in
# explicitly at the next natural pair start. The other five launchers keep the old default.
if [[ ${STRATA_KSM:-0} == 1 && ${STRATA_KSM_BOOTSTRAPPED:-0} != 1 ]]; then
  export STRATA_KSM_BOOTSTRAPPED=1
  exec "$PY" -c '
import ctypes
import os
import sys
libc = ctypes.CDLL(None, use_errno=True)
libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
if libc.prctl(67, 1, 0, 0, 0) != 0:  # PR_SET_MEMORY_MERGE
    error = ctypes.get_errno()
    raise OSError(error, os.strerror(error))
print("KSM enabled for model launcher and children", flush=True)
os.execv(sys.argv[1], sys.argv[1:])
' "$BASH" "$0" "$@"
fi
case "${1:-}" in
  --pair-child)
    [[ -n ${STRATA_PAIR_RECORD:-} ]] || { echo 'REFUSING unowned pair child.' >&2; exit 2; } ;;
  --single-run) unset STRATA_PAIR_RECORD ;;
  ''|--recover)
    exec "$PY" "$ROOT/tools/mixed_gpu/pair_supervisor.py" run --launcher "$0" --config "$CONFIG" \
      --state "$ROOT/run/prod-swift15-f16kv-mixed-supervisor.json" --lock "$ROOT/run/mixed-supervisor.lock" \
      --socket "$SOCKET" --socket "$SOCKET2" --nice 1 \
      --stage-cpus 3,5,7,9,11,13,15 \
      --stall-timeout 600 ;;  # a wedged in-flight read rebuilds the pair after 10 min
  *) echo 'Usage: launcher [--recover|--single-run]' >&2; exit 2 ;;
esac
command -v ss >/dev/null
[[ -z $(ss -H -ltn 'sport = :8009') ]] || { echo 'REFUSING: port 8009 occupied.' >&2; exit 1; }
[[ ! -e "$SOCKET" && ! -L "$SOCKET" ]] || { echo "REFUSING: existing socket $SOCKET" >&2; exit 1; }
[[ ! -e "$SOCKET2" && ! -L "$SOCKET2" ]] || { echo "REFUSING: existing socket $SOCKET2" >&2; exit 1; }
for file in "$PY" "$HELPER" "$BIN/rpcw-hip/strata"; do
  [[ -x "$file" ]] || { echo "Missing executable: $file" >&2; exit 2; }
done
[[ -f "$WORK/serve/server.py" && -f ${STRATA_DFLASH2_DIR:-/mnt/srcds/dflash2}/artifacts/inco-dflash2-q4_0.gguf ]]
"$PY" -m json.tool "$CONFIG" >/dev/null
while IFS= read -r name; do
  case "$name" in STRATA_DF_*|STRATA_DFLASH_*|STRATA_DUMP_*|STRATA_WINDOW_*|STRATA_PREFILL_HELPER) unset "$name" ;; esac
done < <(compgen -e)
mkdir -p "$ROOT/run" "$ROOT/logs"
LOG="$ROOT/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-$(date +%Y%m%d-%H%M%S)-$$.log"
HLOG="${LOG%.log}-helper.log"
helper_pid= server_pid=
cleanup() {
  trap - EXIT INT TERM
  for pid in "$server_pid" "$helper_pid"; do
    if [[ -n "$pid" ]]; then kill -TERM "$pid" 2>/dev/null || true; fi
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
# Clean helper env; helper-side profile knobs opt in from the parent (env -i is last-wins,
# so a parent STRATA_HELPER_REPLY_TIMEOUT_MS overrides the 600000 default).
helper_env=(STRATA_HELPER_REPLY_TIMEOUT_MS=600000 PATH="$PATH" HOME="$HOME" TERM="${TERM:-dumb}")
for v in STRATA_RPC_PROFILE STRATA_VERIFY_PROFILE STRATA_HELPER_REPLY_TIMEOUT_MS; do
  [[ -n ${!v:-} ]] && helper_env+=("$v=${!v}")
done
env -i "${helper_env[@]}" "$HELPER" \
  --pack /mnt/srcds/ai/iq3s \
  --experts ../Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S.gguf \
  --device 0 --slots 4600 --reserve-mib 512 --timeout 63115200000 \
  --begin-timeout 63115200000 --socket "$SOCKET" --fill-mib 6796 --socket2 "$SOCKET2" \
  >>"$HLOG" 2>&1 < /dev/null &
helper_pid=$!
if [[ -n ${STRATA_PAIR_RECORD:-} ]]; then
  "$PY" "$ROOT/tools/mixed_gpu/pair_supervisor.py" record "$helper_pid" 0 "$LOG" "$HLOG"
fi
for ((i=0; i<300; i++)); do
  kill -0 "$helper_pid" 2>/dev/null || { echo "Helper exited; see $HLOG" >&2; exit 1; }
  if grep -q 'expert-helper: listening on' "$HLOG"; then break; fi
  sleep 1
done
grep -q 'expert-helper: listening on' "$HLOG" || { echo 'Helper readiness timed out.' >&2; exit 1; }
for ((i=0; i<60; i++)); do
  kill -0 "$helper_pid" 2>/dev/null || { echo "Helper exited; see $HLOG" >&2; exit 1; }
  if [[ -S "$SOCKET" && -S "$SOCKET2" ]]; then break; fi
  sleep 1
done
[[ -S "$SOCKET" && -S "$SOCKET2" ]] || { echo "Second channel socket missing: $SOCKET2" >&2; exit 1; }
echo "Starting mixed backend on 127.0.0.1:8009; log: $LOG; helper: $HLOG"
cd "$WORK"
env -u CUDA_VISIBLE_DEVICES -u STRATA_ARENA_MMAP -u GPU_PINNED_MIN_XFER_SIZE \
  -u HSA_USERPTR_FOR_PAGED_MEM -u LD_PRELOAD \
  HSA_OVERRIDE_GFX_VERSION=9.0.6 GPU_MAX_ALLOC_PERCENT=100 \
  LD_LIBRARY_PATH=${STRATA_ROCM_ROOT:-/home/srcds/rocm-gfx906-xnack}/lib:/opt/rocm-6.1.0/lib \
  "$PY" "$WORK/serve/server.py" --engine strata --config "$CONFIG" --port 8009 \
  >>"$LOG" 2>&1 < /dev/null &
server_pid=$!
if [[ -n ${STRATA_PAIR_RECORD:-} ]]; then
  "$PY" "$ROOT/tools/mixed_gpu/pair_supervisor.py" record "$helper_pid" "$server_pid" "$LOG" "$HLOG"
fi
wait "$server_pid"
