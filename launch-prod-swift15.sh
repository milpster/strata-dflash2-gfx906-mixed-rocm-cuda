#!/usr/bin/env bash
# Strata prod launcher - Swift 1.5 Qwen3.8 Flash Next IQ3_XXS (ukisai GSQ-RCO, merged shards).
# Same engine/flags as launch-prod.sh (2x Radeon VII gfx906, 262144 ctx, thinking "high",
# sampling 0.6/0.95/20) plus CPU vision (mmproj-Swift-Qwen3.8-Flash-Next-BF16).
# Config: prod-config-swift15.json (prod-config.json is left as it was).
# Logs: logs/prod-swift15-<date>.log (wrapper), logs/engine-swift15.log (engine).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs
LOG="logs/prod-swift15-$(date +%Y%m%d-%H%M%S).log"
echo "starting prod (swift 1.5) on :8009, log: $LOG"
exec env -u CUDA_VISIBLE_DEVICES -u STRATA_ARENA_MMAP -u GPU_PINNED_MIN_XFER_SIZE \
  -u HSA_USERPTR_FOR_PAGED_MEM \
  LD_LIBRARY_PATH=${STRATA_ROCM_ROOT:-/home/user/rocm-gfx906-xnack}/lib:/opt/rocm-6.1.0/lib \
  setsid nohup ./.venv/bin/python serve/server.py \
  --engine strata --config prod-config-swift15.json --port 8009 \
  >> "$LOG" 2>&1 < /dev/null
