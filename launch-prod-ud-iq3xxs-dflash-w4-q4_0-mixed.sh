#!/usr/bin/env bash
# Foreground mixed HIP/CUDA supervisor. Detach with setsid nohup when needed.
# 2026-11-22: always-on Foresight swap (operator order, quality first): clamped engine
# 4b8251e9 in run/fsclamp-bin, reserves 600/600 so the clamp grants the full 2,8 ask
# (CUDA1 548 MiB free after the 293 MiB allocation; measured end-to-end at 240k in the
# fson-rsv600 window, 0 non-finite). Default BIN and CONFIG moved to the -fson tuple.
# TUNED config (ab2-r1 PASS verdict 2026-10-31: long decode +7.7%, xl +6.5%, prompt tier live):
# rpcw candidate engine+helper, helper second channel (--socket2), prompt tier on
# (STRATA_PREFILL_HELPER=1 via the config env), occupancy pinned (slots4c sweep
# 2026-11-04: V7-squeezed promoted (08f5e690): fill-mib 6700 binds first at 4318 slots (bench r1+r2 + prod boot)
# 6.25 GiB; +13.6% decode median vs the old 4000-slot cap, read rate unchanged).
# 2026-11-05 pair fix: --begin-timeout 1800000 -> 63115200000 (the 30-min idle wait killed the
# helper after every idle half-hour; engine death still exits at once on socket close), and prod
# boots THROUGH the supervisor by default (--recover remains an alias),
# any peer death restarts BOTH (either both run or both reset).
# Champion (2026-11-21): this is the base every new window, trial or fix derives from.
# See CHAMPION.md / CHAMPION.json. Pool 38 GiB, CUDA0 margin 320 (one +64 step past the
# 256 that was measured), helper reserve 0 / fill 7400 / shard 00002 of the UD pack.
set -euo pipefail
ROOT=${STRATA_PROD_TREE:-/mnt/srcds/Strata}
WORK="$ROOT/v0215"
# Keep the serving wrapper stable; pin the latest built engine/helper separately.
# 2026-11-13: v0233 promoted (ARM1_SHADOW champion, journal 18i): v0233-candidate engine+helper
# (all 8 lanes landed, default-off) + foresight shadow stack in the config env
# (STRATA_FS_SLOTS=3,2 STRATA_FS_HELPER_SHADOW=1 STRATA_FS_ADMIT=2 STRATA_GRAPH_FAMILY_TRIM=8
# STRATA_SWAPIN_DEPTH=64) + helper reserve 512->320 (R3A: fill binds at 6796 either way).
# Measured vs v0227 split pin (R3A lean panel): 180k warm TG 18.8-23.8 -> 29.0-31.5;
# PP 64k 359.9 -> 521.5; PP 180k 529.5 -> 578.7.
# 2026-11-21: pinned to the managed-host-pool build that set the UD-only series and the
# 230k-token window (engine rpcw-hip/strata sha256 d534e72a7369..., helper
# rpcw-cuda/strata-expert-helper-patient sha256 553f3bf48219...; both symlinks into the
# helper-ipc-diet worktree build-906 and v0233). Pool config: STRATA_RAM_BUDGET_MIB 43008
# -> 38 GiB pool + 4 GiB outside area, CUDA0 VRAM margin 256 (one +64 step after the
# 40 GiB run exhausted the card at 7 MiB free), CUDA1 192, helper reserve 0 (the component
# clamps it to 64), fill 7400 MiB, relief hot 2048 / arena cap 4096.
BIN=${STRATA_PROD_BIN:-"$ROOT/run/fsclamp-bin"}
export STRATA_RAM_BUDGET_MIB=${STRATA_RAM_BUDGET_MIB:-43008}
PY="$ROOT/.venv/bin/python"
CONFIG=${STRATA_PROD_CONFIG:-"$ROOT/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed-fson.json"}
SOCKET="$ROOT/run/prod-iq3xxs-ud-mixed.sock"
SOCKET2="$ROOT/run/prod-iq3xxs-ud-mixed2.sock"
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
      --stall-timeout 600 \
      --startup-timeout 1200 ;;  # 2026-11-13: full residency (no ram-relief) init takes ~6-8 min single-threaded; the 360 s default killed every generation before the helper attached/filled
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
# 2026-11-21: fill-mib 0 = no cap, the helper's own budget governs (free − reserve − 16 MiB
# context headroom), and reserve-mib 0 is now honoured literally (the helper's 64 MiB floor was
# removed): the 3080 is dedicated to the helper, so the fill is expected to end with ~16 MiB free.
# Measured before this change: budget 7436.4 MiB, fill stopped on the 7400 cap at 3861 slots.
env -i "${helper_env[@]}" "$HELPER" \
  --pack ${STRATA_AI_DIR:-/mnt/srcds/ai}/iq3xxs-ud \
  --experts ../Qwen3.8-Flash-Next-UD-IQ3_XXS-00002-of-00004.gguf \
  --device 0 --slots ${STRATA_PROD_HELPER_SLOTS:-4600} --reserve-mib ${STRATA_PROD_HELPER_RESERVE_MIB:-0} --timeout 63115200000 \
  --begin-timeout 63115200000 --socket "$SOCKET" --fill-mib ${STRATA_PROD_HELPER_FILL_MIB:-0} --socket2 "$SOCKET2" \
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
  STRATA_ENGINE_READY_S=2400 \
  LD_LIBRARY_PATH=${STRATA_ROCM_ROOT:-/home/srcds/rocm-gfx906-xnack}/lib:/opt/rocm-6.1.0/lib \
  "$PY" "$WORK/serve/server.py" --engine strata --config "$CONFIG" --port 8009 \
  >>"$LOG" 2>&1 < /dev/null &
server_pid=$!
if [[ -n ${STRATA_PAIR_RECORD:-} ]]; then
  "$PY" "$ROOT/tools/mixed_gpu/pair_supervisor.py" record "$helper_pid" "$server_pid" "$LOG" "$HLOG"
fi
wait "$server_pid"
