#!/usr/bin/env bash
# Swift 1.5 IQ3_XXS production with DFlash2 width 4.
# The historical w3 launcher/config filenames remain the production entrypoint.
# 2026-10-24: engine swapped to build-dfsample-1 (DFlash drafting for sampled
# requests too); the config JSON's "env" block sets STRATA_DFLASH_SAMPLED=1.
# Original launch-prod-swift15.sh and prod-config-swift15.json are unchanged.
set -euo pipefail

ROOT=${STRATA_PROD_TREE:-/mnt/user/Strata}
CLONE=${STRATA_DFLASH2_DIR:-/mnt/user/dflash2}/Strata-integ
PY="$ROOT/.venv/bin/python"
CONFIG="$ROOT/prod-config-swift15-dflash-w3.json"
LOGDIR="$ROOT/logs"

command -v ss >/dev/null || { echo 'Missing ss; cannot safely check port 8009.' >&2; exit 2; }
listeners=$(ss -H -ltn 'sport = :8009')
if [ -n "$listeners" ]; then
  echo 'REFUSING TO START: port 8009 is occupied. Stop the existing service first.' >&2
  exit 1
fi

[ -x "$PY" ] || { echo "Missing Python: $PY" >&2; exit 2; }
[ -x "$CLONE/build-dfsample-1/strata" ] || { echo 'Missing DFlash engine.' >&2; exit 2; }
[ -f "$CLONE/serve/server.py" ] || { echo 'Missing merged server.' >&2; exit 2; }
[ -f ${STRATA_DFLASH2_DIR:-/mnt/user/dflash2}/artifacts/inco-dflash2-bf16.gguf ] || { echo 'Missing DFlash2 model.' >&2; exit 2; }
"$PY" -m json.tool "$CONFIG" >/dev/null

# Do not carry test-window probes or dump hooks into the production trial.
while IFS= read -r name; do
  case "$name" in
    STRATA_DF_*|STRATA_DFLASH_*|STRATA_DUMP_*|STRATA_WINDOW_*) unset "$name" ;;
  esac
done < <(compgen -e)

mkdir -p "$LOGDIR"
LOG="$LOGDIR/prod-swift15-dflash-w4-$(date +%Y%m%d-%H%M%S)-$$.log"
echo "Starting Swift 1.5 DFlash2 w4 on 127.0.0.1:8009; log: $LOG"
cd "$CLONE"
exec env -u CUDA_VISIBLE_DEVICES -u STRATA_ARENA_MMAP -u GPU_PINNED_MIN_XFER_SIZE \
  -u HSA_USERPTR_FOR_PAGED_MEM \
  LD_LIBRARY_PATH=${STRATA_ROCM_ROOT:-/home/user/rocm-gfx906-xnack}/lib:/opt/rocm-6.1.0/lib \
  setsid nohup "$PY" serve/server.py \
  --engine strata --config "$CONFIG" --port 8009 \
  >> "$LOG" 2>&1 < /dev/null
