#!/usr/bin/env bash
# Trial launcher: the arena-54 arm on :8019. Binaries are the frozen pin
# /mnt/srcds/Strata/v0239 (rpcw-hip/strata + rpcw-cuda/strata-expert-helper-patient;
# hashes in v0239/SHA256SUMS.txt), not a live build tree.
# Design: the shared expert arena becomes a file-backed mmap of the pack's experts.bin
# (STRATA_ARENA_MMAP=1) and only that one span gets mlocked (STRATA_NATIVE_MLOCK=arena),
# so one consecutive chunk of page-cache pages holds the engine's hot expert world and
# survives memory reclaim instead of being dropped and re-read. Both variables come from
# the config's "env" block (trial-config-arena54.json), which serve/server.py child_env()
# puts into the engine's environment; the -u STRATA_ARENA_MMAP further down clears it only
# from this host process, so it does not stop the engine from getting it.
# Memory wall (oracle ruling, 2026-11-20): the arena is a fixed 46.84 GiB, so 54G left no
# margin - the cgroup gets MemoryMax=56G, MemoryMin=51G, MemorySwapMax=4G (a bounded
# thrash ceiling rather than an unbounded one), and the engine self-limits at
# STRATA_MEMORY_LIMIT_MIB=55296 (54 GiB), i.e. 2 GiB below the wall. All three props are
# env-tunable: STRATA_TRIAL_MEMORY_MAX / STRATA_TRIAL_MEMORY_MIN / STRATA_TRIAL_SWAP_MAX,
# and the value "none" leaves that -p prop out altogether (a first boot on a pack that has
# no experts.bin yet needs the cap lifted, or the write of a 50 GiB file gets OOM-killed).
# FIRST RUN: that uncapped boot is the arena pre-generation boot - it loads the anonymous
# arena and writes the file; it is NOT a measured boot. bench/ab-v0210/rpcprof/gpu-rounds/
# arena54-window-20261120.sh runs it as its pre-gen phase and asserts the file's size.
# Prod boots ONLY through launch-prod-swift15.sh; never this one.
set -euo pipefail
ROOT=${STRATA_PROD_TREE:-/mnt/srcds/Strata}
WORK="$ROOT/v0215"
BIN="/mnt/srcds/Strata/v0239"
PY="$ROOT/.venv/bin/python"
CONFIG="/mnt/srcds/Strata/trial-config-arena54.json"
SOCKET="$ROOT/run/trial-arena54.sock"
SOCKET2="$ROOT/run/trial-arena54-2.sock"
HELPER="$BIN/rpcw-cuda/strata-expert-helper-patient"
case "${1:-}" in
  --pair-child) [[ -n ${STRATA_PAIR_RECORD:-} ]] || { echo 'REFUSING unowned pair child.' >&2; exit 2; } ;;
  --single-run) unset STRATA_PAIR_RECORD ;;
  ''|--recover)
    exec "$PY" "$ROOT/tools/mixed_gpu/pair_supervisor.py" run --launcher "$0" --config "$CONFIG" \
      --state "$ROOT/run/trial-arena54-supervisor.json" --lock "$ROOT/run/trial-arena54-supervisor.lock" \
      --socket "$SOCKET" --socket "$SOCKET2" --nice 1 \
      --stage-cpus 3,5,7,9,11,13,15 \
      --stall-timeout 600 \
      --startup-timeout "${STRATA_TRIAL_STARTUP_TIMEOUT_S:-1200}" ;;
  *) echo 'Usage: launcher [--recover|--single-run]' >&2; exit 2 ;;
esac
command -v ss >/dev/null
[[ -z $(ss -H -ltn 'sport = :8019') ]] || { echo 'REFUSING: port 8019 occupied.' >&2; exit 1; }
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
LOG="$ROOT/logs/trial-arena54-mixed-$(date +%Y%m%d-%H%M%S)-$$.log"
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
  --pack ${STRATA_AI_DIR:-/mnt/srcds/ai}/iq3xxs-ud \
  --experts ../Qwen3.8-Flash-Next-UD-IQ3_XXS-00001-of-00004.gguf \
  --device 0 --slots 4600 --reserve-mib 156 --timeout 63115200000 \
  --begin-timeout 63115200000 --socket "$SOCKET" --fill-mib 7100 --socket2 "$SOCKET2" \
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
# Cgroup memory props (defaults: the 56G wall, the 51G floor, a 4G swap ceiling).
# STRATA_TRIAL_MEMORY_MAX / _MIN / STRATA_TRIAL_SWAP_MAX override each one; the value
# "none" omits that -p prop entirely, which is what the arena pre-generation boot needs
# (writing a 50 GiB experts.bin under a 56G cap would race the same memory it reserves).
mem_max=${STRATA_TRIAL_MEMORY_MAX-56G}
mem_min=${STRATA_TRIAL_MEMORY_MIN-51G}
mem_swap=${STRATA_TRIAL_SWAP_MAX-4G}
cgroup_props=()
for prop in "MemoryMax=$mem_max" "MemoryMin=$mem_min" "MemorySwapMax=$mem_swap"; do
  [[ -z ${prop#*=} || ${prop#*=} == none ]] || cgroup_props+=("-p" "$prop")   # empty or "none": omit the prop
done
echo "Starting TRIAL mixed backend (arena-54 world: mmap arena + mlock; cgroup props: ${cgroup_props[*]:-none}) on 127.0.0.1:8019; log: $LOG; helper: $HLOG"
cd "$WORK"
env -u CUDA_VISIBLE_DEVICES -u STRATA_ARENA_MMAP -u GPU_PINNED_MIN_XFER_SIZE \
  -u HSA_USERPTR_FOR_PAGED_MEM -u LD_PRELOAD \
  HSA_OVERRIDE_GFX_VERSION=9.0.6 GPU_MAX_ALLOC_PERCENT=100 STRATA_WATCHDOG_S=120 \
  STRATA_MEMORY_LIMIT_MIB=55296 \
  STRATA_ENGINE_READY_S=2400 \
  LD_LIBRARY_PATH=${STRATA_ROCM_ROOT:-/home/srcds/rocm-gfx906-xnack}/lib:/opt/rocm-6.1.0/lib \
  systemd-run --user --scope ${cgroup_props[@]+"${cgroup_props[@]}"} \
  "$PY" "$WORK/serve/server.py" --engine strata --config "$CONFIG" --port 8019 \
  >>"$LOG" 2>&1 < /dev/null &
server_pid=$!
if [[ -n ${STRATA_PAIR_RECORD:-} ]]; then
  "$PY" "$ROOT/tools/mixed_gpu/pair_supervisor.py" record "$helper_pid" "$server_pid" "$LOG" "$HLOG"
fi
wait "$server_pid"
