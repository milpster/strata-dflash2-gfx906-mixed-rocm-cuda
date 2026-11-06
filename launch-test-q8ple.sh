#!/usr/bin/env bash
# Q8_0 PLE A/B test launcher - byte-identical to launch-prod-swift15.sh except the config:
# prod-config-swift15-q8ple.json points --ple-gguf at Delgon's standalone Q8_0 PLE table
# (and pins --native-dense-gguf to the Swift model file so the dense-projection scan does
# not walk the PLE-only sidecar). Same engine binary, flags, env, port 8009, vision stack.
#
# The 2x 16 GB cards hold ONE engine: stop the prod stack before starting this.
# Switch the NORMAL prod launcher over with ple-q8-default.sh q8_0 (later, if adopted).
# Evidence and method: /mnt/user/ple-q8-upgrade/report.md
set -euo pipefail
cd "$(dirname "$0")"

if pgrep -f 'serve/server\.py --engine strata --config prod-config-swift15\.json' >/dev/null; then
  echo "refusing: the prod stack (original PLE) is still running; two engines do not fit the cards." >&2
  echo "stop it with your usual procedure, e.g.:" >&2
  echo "  pkill -f 'serve/server.py --engine strata --config prod-config-swift15.json'" >&2
  exit 1
fi
PLE=${STRATA_AI_DIR:-/mnt/user/ai}/Qwen3.8-Flash-Next-PLE-Q8_0.gguf
if [ ! -f "$PLE" ]; then
  echo "missing $PLE - download it first (see /mnt/user/ple-q8-upgrade/report.md)" >&2
  exit 1
fi

mkdir -p logs
LOG="logs/prod-swift15-q8ple-$(date +%Y%m%d-%H%M%S).log"
echo "starting Q8_0 PLE test stack on :8009, log: $LOG"
echo "verify in logs/engine-swift15-q8ple.log: 'PLE on, table 320001536 rows of $PLE'"
exec env -u CUDA_VISIBLE_DEVICES -u STRATA_ARENA_MMAP -u GPU_PINNED_MIN_XFER_SIZE \
  -u HSA_USERPTR_FOR_PAGED_MEM \
  LD_LIBRARY_PATH=${STRATA_ROCM_ROOT:-/home/user/rocm-gfx906-xnack}/lib:/opt/rocm-6.1.0/lib \
  setsid nohup ./.venv/bin/python serve/server.py \
  --engine strata --config prod-config-swift15-q8ple.json --port 8009 \
  >> "$LOG" 2>&1 < /dev/null
