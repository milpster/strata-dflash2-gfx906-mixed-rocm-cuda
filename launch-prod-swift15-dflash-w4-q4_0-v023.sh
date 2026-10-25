#!/usr/bin/env bash
# Swift 1.5 IQ3_XXS production — MERGED v0.2.3 build (strata-v023-merge), Q4_0 draft.
# Copied line-for-line from launch-prod-swift15-dflash-w4-q4_0.sh with:
#   CLONE  -> the v023 merge worktree (serve code matched to the merged engine)
#   engine -> build-906-inco/strata (STRATA_ENABLE_DFLASH2 build, ours drafter)
#   config -> lane-d-logs/prod-config-ours.json (exe + log patched by mk_cutover_config.py)
# Validate speed against the incumbent launcher BEFORE switching prod to this one.
set -euo pipefail

ROOT=/mnt/user/Strata
CLONE=/mnt/user/Strata/worktrees/v023-merge
PY="$ROOT/.venv/bin/python"
CONFIG="$CLONE/lane-d-logs/prod-config-ours.json"
LOGDIR="$ROOT/logs"

command -v ss >/dev/null || { echo 'Missing ss; cannot safely check port 8009.' >&2; exit 2; }
listeners=$(ss -H -ltn 'sport = :8009')
if [ -n "$listeners" ]; then
  echo 'REFUSING TO START: port 8009 is occupied. Stop the existing service first.' >&2
  exit 1
fi

[ -x "$PY" ] || { echo "Missing Python: $PY" >&2; exit 2; }
[ -x "$CLONE/build-906-inco/strata" ] || { echo 'Missing merged DFlash engine (build-906-inco).' >&2; exit 2; }
[ -f "$CLONE/serve/server.py" ] || { echo 'Missing merged server.' >&2; exit 2; }
[ -f /mnt/user/dflash2/artifacts/inco-dflash2-q4_0.gguf ] || { echo 'Missing DFlash2 Q4_0 draft model.' >&2; exit 2; }
"$PY" -m json.tool "$CONFIG" >/dev/null

# Do not carry test-window probes or dump hooks into the production trial.
while IFS= read -r name; do
  case "$name" in
    STRATA_DF_*|STRATA_DFLASH_*|STRATA_DUMP_*|STRATA_WINDOW_*) unset "$name" ;;
  esac
done < <(compgen -e)

mkdir -p "$LOGDIR"
LOG="$LOGDIR/prod-swift15-dflash-w4-q4_0-v023-$(date +%Y%m%d-%H%M%S)-$$.log"
echo "Starting Swift 1.5 DFlash2 w4 Q4_0 (v0.2.3 merged) on 127.0.0.1:8009; log: $LOG"
cd "$CLONE"
exec env -u CUDA_VISIBLE_DEVICES -u STRATA_ARENA_MMAP -u GPU_PINNED_MIN_XFER_SIZE \
  -u HSA_USERPTR_FOR_PAGED_MEM \
  LD_LIBRARY_PATH=/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib \
  setsid nohup "$PY" serve/server.py \
  --engine strata --config "$CONFIG" --port 8009 \
  >> "$LOG" 2>&1 < /dev/null
