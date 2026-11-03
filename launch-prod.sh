#!/usr/bin/env bash
# Strata prod launcher - 2x Radeon VII (gfx906), Swift Qwen3.8 Flash Next IQ3_XXS.
# Best measured config (bench/logs/2026-10-16-iq3xxs-2x-radeonvii): TG 27.1 t/s
# @ 169k ctx, PP ~677 t/s, 262144 ctx, zero hangs.
# Thinking: effort "high" (the config max), reasoning_budget_tokens 0 = no cap;
# the web app preserves reasoning in history when the template supports it
# (auto-detected capability, no server flag).
# Sampling: Qwen3 thinking-mode recommendation (0.6 / 0.95 / 20).
# Logs: logs/prod-<date>.log (wrapper), logs/engine.log (engine).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs
LOG="logs/prod-$(date +%Y%m%d-%H%M%S).log"
echo "starting prod on :8009, log: $LOG"
exec env -u CUDA_VISIBLE_DEVICES -u STRATA_ARENA_MMAP -u GPU_PINNED_MIN_XFER_SIZE \
  -u HSA_USERPTR_FOR_PAGED_MEM \
  LD_LIBRARY_PATH=/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib \
  setsid nohup ./.venv/bin/python v0210/serve/server.py \
  --engine strata --config prod-config.json --port 8009 \
  >> "$LOG" 2>&1 < /dev/null
