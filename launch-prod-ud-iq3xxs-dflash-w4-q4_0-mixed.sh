#!/usr/bin/env bash
# Foreground mixed HIP/CUDA supervisor. Detach with setsid nohup when needed.
# Uses the tested window helper and the opt-in prompt tier.
# TUNED config (ab2-r1 PASS verdict 2026-10-31, validated on the swift15/iq3xxs combo;
# this iq3xxs-ud variant carries the same mechanism, not yet window-validated for its own pack):
# rpcw candidate engine+helper, helper second channel (--socket2), prompt tier on
# (STRATA_PREFILL_HELPER=1 via the config env), occupancy pinned (--fill-mib 6400;
# currently inert: the 4000-slot cap binds first at 6.07 GiB).
set -euo pipefail
ROOT=/mnt/user/Strata
WORK="$ROOT/v0210"
PY="$ROOT/.venv/bin/python"
CONFIG="$ROOT/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json"
SOCKET="$ROOT/run/prod-iq3xxs-ud-mixed.sock"
SOCKET2="$ROOT/run/prod-ud-iq3xxs-mixed2.sock"
HELPER="$WORK/build-mixed-gpu/rpcw-cuda/strata-expert-helper"
case "${1:-}" in
  --pair-child)
    [[ -n ${STRATA_PAIR_RECORD:-} ]] || { echo 'REFUSING unowned pair child.' >&2; exit 2; } ;;
  --single-run) unset STRATA_PAIR_RECORD ;;
  ''|--recover)
    exec "$PY" "$ROOT/tools/mixed_gpu/pair_supervisor.py" run --launcher "$0" --config "$CONFIG" \
      --state "$ROOT/run/prod-ud-iq3xxs-mixed-supervisor.json" --lock "$ROOT/run/mixed-supervisor.lock" \
      --socket "$SOCKET" --socket "$SOCKET2" --nice 1 ;;
  *) echo 'Usage: launcher [--recover|--single-run]' >&2; exit 2 ;;
esac
command -v ss >/dev/null
[[ -z $(ss -H -ltn 'sport = :8009') ]] || { echo 'REFUSING: port 8009 occupied.' >&2; exit 1; }
[[ ! -e "$SOCKET" && ! -L "$SOCKET" ]] || { echo "REFUSING: existing socket $SOCKET" >&2; exit 1; }
[[ ! -e "$SOCKET2" && ! -L "$SOCKET2" ]] || { echo "REFUSING: existing socket $SOCKET2" >&2; exit 1; }
for file in "$PY" "$HELPER" "$WORK/build-mixed-gpu/rpcw-hip/strata"; do
  [[ -x "$file" ]] || { echo "Missing executable: $file" >&2; exit 2; }
done
[[ -f "$WORK/serve/server.py" && -f /mnt/user/dflash2/artifacts/inco-dflash2-q4_0.gguf ]]
"$PY" -m json.tool "$CONFIG" >/dev/null
while IFS= read -r name; do
  case "$name" in STRATA_DF_*|STRATA_DFLASH_*|STRATA_DUMP_*|STRATA_WINDOW_*|STRATA_PREFILL_HELPER) unset "$name" ;; esac
done < <(compgen -e)
mkdir -p "$ROOT/run" "$ROOT/logs"
LOG="$ROOT/logs/prod-ud-iq3xxs-dflash-w4-q4_0-mixed-$(date +%Y%m%d-%H%M%S)-$$.log"
HLOG="${LOG%.log}-helper.log"
helper_pid='' server_pid=''
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
env -i PATH="$PATH" HOME="$HOME" TERM="${TERM:-dumb}" "$HELPER" \
  --pack /mnt/user/ai/iq3xxs-ud \
  --experts ../Qwen3.8-Flash-Next-UD-IQ3_XXS-00001-of-00004.gguf \
  --persist \
  --device 0 --slots 4600 --reserve-mib 300 --timeout 63115200000 \
  --begin-timeout 1800000 --socket "$SOCKET" --fill-mib 6796 --socket2 "$SOCKET2" \
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
  LD_LIBRARY_PATH=/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib \
  "$PY" "$WORK/serve/server.py" --engine strata --config "$CONFIG" --port 8009 \
  >>"$LOG" 2>&1 < /dev/null &
server_pid=$!
if [[ -n ${STRATA_PAIR_RECORD:-} ]]; then
  "$PY" "$ROOT/tools/mixed_gpu/pair_supervisor.py" record "$helper_pid" "$server_pid" "$LOG" "$HLOG"
fi
wait "$server_pid"
