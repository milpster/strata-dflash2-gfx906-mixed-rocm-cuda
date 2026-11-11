#!/usr/bin/env bash
# Trial launcher for the split-40 allocator/spill A/B (2026-11-10): exact copy of the
# swift15 prod launcher shape, with BIN pinned to the spill-guard-fix build (v0217,
# commit 089b6efcf on helper-ipc-diet), a TRIAL_CONFIG-selected config (spill40 off/on)
# and trial-own state/socket/log names so the prod state JSON stays untouched.
# Usage: TRIAL_CONFIG=/mnt/srcds/Strata/bench/ab-v0210/rpcprof/trial-spill40-off.json ./launch-trial-spill40.sh
set -euo pipefail
ROOT=${STRATA_PROD_TREE:-/mnt/srcds/Strata}
WORK="$ROOT/v0215"
BIN="${STRATA_BIN:-$ROOT/v0219/build-mixed-gpu}"
PY="$ROOT/.venv/bin/python"
CONFIG=${TRIAL_CONFIG:?TRIAL_CONFIG required}
SOCKET="$ROOT/run/trial-spill40.sock"
SOCKET2="$ROOT/run/trial-spill40-2.sock"
HELPER="$BIN/rpcw-cuda/strata-expert-helper-patient"
FILL_MIB=${STRATA_FILL_MIB:-6796}
HELPER_SLOTS=${STRATA_HELPER_SLOTS:-4600}
PORT=${STRATA_PORT:-8009}
case "${1:-}" in
  --pair-child)
    [[ -n ${STRATA_PAIR_RECORD:-} ]] || { echo 'REFUSING unowned pair child.' >&2; exit 2; } ;;
  --single-run) unset STRATA_PAIR_RECORD ;;
  ''|--recover)
    exec "$PY" "$ROOT/tools/mixed_gpu/pair_supervisor.py" run --launcher "$0" --config "$CONFIG" \
      --state "$ROOT/run/trial-spill40-supervisor.json" --lock "$ROOT/run/trial-spill40.lock" \
      --socket "$SOCKET" --socket "$SOCKET2" --nice 1 \
      --stage-cpus 3,5,7,9,11,13,15 \
      --stall-timeout 600 ;;
  *) echo 'Usage: launcher [--recover|--single-run]' >&2; exit 2 ;;
esac
command -v ss >/dev/null
[[ -z $(ss -H -ltn "sport = :${PORT:-8009}") ]] || { echo "REFUSING: port ${PORT:-8009} occupied." >&2; exit 1; }
[[ ! -e "$SOCKET" && ! -L "$SOCKET" ]] || { echo "REFUSING: existing socket $SOCKET" >&2; exit 1; }
[[ ! -e "$SOCKET2" && ! -L "$SOCKET2" ]] || { echo "REFUSING: existing socket $SOCKET2" >&2; exit 1; }
for file in "$PY" "$HELPER" "$BIN/rpcw-hip/strata"; do
  [[ -x "$file" ]] || { echo "Missing executable: $file" >&2; exit 2; }
done
[[ -f "$WORK/serve/server.py" && -f ${STRATA_DFLASH2_DIR:-/mnt/srcds/dflash2}/artifacts/inco-dflash2-q4_0.gguf ]]
"$PY" -m json.tool "$CONFIG" >/dev/null
while IFS= read -r name; do
  case "$name" in STRATA_DF_*|STRATA_DFLASH_*|STRATA_DUMP_*|STRATA_WINDOW_*|STRATA_PREFILL_HELPER) unset "$name" ;;
  esac
done < <(compgen -e)
mkdir -p "$ROOT/run" "$ROOT/logs"
LOG="$ROOT/logs/trial-spill40-$(date +%Y%m%d-%H%M%S)-$$.log"
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
helper_env=(STRATA_HELPER_REPLY_TIMEOUT_MS=600000 PATH="$PATH" HOME="$HOME" TERM="${TERM:-dumb}")
for v in STRATA_RPC_PROFILE STRATA_VERIFY_PROFILE STRATA_HELPER_REPLY_TIMEOUT_MS; do
  [[ -n ${!v:-} ]] && helper_env+=("$v=${!v}")
done
env -i "${helper_env[@]}" "$HELPER" \
  --pack ${STRATA_AI_DIR:-/mnt/srcds/ai}/iq3xxs-15 \
  --experts ../Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf \
  --device 0 --slots "$HELPER_SLOTS" --reserve-mib "${STRATA_HELPER_RESERVE:-300}" --timeout 63115200000 \
  --begin-timeout 63115200000 --socket "$SOCKET" --fill-mib "$FILL_MIB" --socket2 "$SOCKET2" \
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
grep -q 'expert-helper: listening on' "$HLOG" || { echo "Helper readiness timed out." >&2; exit 1; }
for ((i=0; i<60; i++)); do
  kill -0 "$helper_pid" 2>/dev/null || { echo "Helper exited; see $HLOG" >&2; exit 1; }
  if [[ -S "$SOCKET" && -S "$SOCKET2" ]]; then break; fi
  sleep 1
done
[[ -S "$SOCKET" && -S "$SOCKET2" ]] || { echo "Second channel socket missing: $SOCKET2" >&2; exit 1; }
echo "Starting mixed backend on 127.0.0.1:${PORT:-8009}; log: $LOG; helper: $HLOG"
cd "$WORK"
env -u CUDA_VISIBLE_DEVICES -u STRATA_ARENA_MMAP -u GPU_PINNED_MIN_XFER_SIZE \
  -u HSA_USERPTR_FOR_PAGED_MEM -u LD_PRELOAD \
  LD_LIBRARY_PATH=${STRATA_ROCM_ROOT:-/home/user/rocm-gfx906-xnack}/lib:/opt/rocm-6.1.0/lib \
  "$PY" "$WORK/serve/server.py" --engine strata --config "$CONFIG" --port "${PORT:-8009}" \
  >>"$LOG" 2>&1 < /dev/null &
server_pid=$!
if [[ -n ${STRATA_PAIR_RECORD:-} ]]; then
  "$PY" "$ROOT/tools/mixed_gpu/pair_supervisor.py" record "$helper_pid" "$server_pid" "$LOG" "$HLOG"
fi
wait "$server_pid"
