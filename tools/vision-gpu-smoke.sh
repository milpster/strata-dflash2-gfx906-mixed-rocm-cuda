#!/usr/bin/env bash
# vision-gpu-smoke.sh - one-shot proof whether strata-vision --gpu works on this box's CUDA
# card (the helper's RTX 3080, sm_86), without touching prod config.
#
# Parent-run prod-down window (the fixer/agents ride prod, so only the parent executes this):
#   1. stop the prod stack by PID (helper exits -> the whole 8 GiB card is free)
#   2. run strata-vision --gpu standalone: model load -> 2048^2 GPU warmup (the VRAM peak:
#      ~0.9 GB weights + 0.4-0.7 GB CUDA context + ~1 GB work buffers) -> READY n_embd
#   3. feed one ENC of run/vision-gpu-test.png -> require "OK <n> <nx> <ny> <ms>"
#   4. ALWAYS restore prod (swift15-f16kv launcher) and verify /v1/models + a completion
# Final line: VERDICT: GPU-VISION-OK | VERDICT: GPU-VISION-FAILED (prod restored either way)
set -euo pipefail
ROOT=/mnt/user/Strata
PY="$ROOT/.venv/bin/python"
VISION="$ROOT/build-vision/bin/strata-vision"
MMPROJ="/mnt/user/ai/mmproj-Swift-Qwen3.8-Flash-Next-BF16.gguf"
MODEL="/mnt/user/ai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf"
IMG="$ROOT/run/vision-gpu-test.png"
OUT="$ROOT/run/vision-gpu-test.sve"
LAUNCH="$ROOT/launch-prod-swift15-f16kv-dflash-w4-q4_0-mixed.sh"
STAMP="$(date +%Y%m%d-%H%M%S)"
SLOG="$ROOT/logs/vision-gpu-smoke-$STAMP.log"
VLOG="$ROOT/logs/vision-gpu-standalone-$STAMP.log"
ev() { printf '%s\n' "$*" | tee -a "$SLOG"; }

[[ -x "$VISION" && -f "$MMPROJ" && -f "$MODEL" && -f "$IMG" && -x "$LAUNCH" ]] || { ev "ABORT: missing file"; exit 2; }

stop_prod() {
  local pids
  pids=$(ps -eo pid,cmd | grep -E "bash \./launch-prod-|serve/server.py --engine strata|rpcw-hip/strata --serve|rpcw-cuda/strata-expert-helper|build-vision/bin/strata-vision" | grep -v grep | awk '{print $1}')
  [[ -z "$pids" ]] && { ev "stop: prod already down"; return 0; }
  ev "stop: TERM $pids"
  for p in $pids; do kill -TERM "$p" 2>/dev/null || true; done
  for _ in $(seq 1 15); do
    ps -eo pid,cmd | grep -E "serve/server.py --engine strata|rpcw-hip/strata --serve|rpcw-cuda/strata-expert-helper" | grep -qv grep || break
    sleep 2
  done
  for p in $pids; do kill -0 "$p" 2>/dev/null && kill -KILL "$p" 2>/dev/null || true; done
  rm -f "$ROOT"/run/prod-*.sock
  ev "stop: prod down, sockets unlinked"
}

restore_prod() {
  ev "restore: launching $LAUNCH"
  setsid nohup "$LAUNCH" >> "$ROOT/logs/launch-swift15-f16kv-postvision-$STAMP.log" 2>&1 < /dev/null &
  local n=0
  while (( n < 420 )); do
    curl -s -m 3 http://127.0.0.1:8009/v1/models 2>/dev/null | grep -q swift-1.5-iq3_xxs-f16kv && break
    sleep 6; n=$((n+6))
  done
  if curl -s -m 3 http://127.0.0.1:8009/v1/models 2>/dev/null | grep -q swift-1.5-iq3_xxs-f16kv; then
    ev "restore: prod serving swift-1.5-iq3_xxs-f16kv"
  else
    ev "CRITICAL: restore FAILED - :8009 not serving; see logs/launch-swift15-f16kv-postvision-$STAMP.log"
    exit 1
  fi
}

ev "== vision gpu smoke, run $STAMP"
stop_prod
ev "test: starting strata-vision --gpu (log: $VLOG)"
RC=0
printf 'ENC %s %s\nQUIT\n' "$IMG" "$OUT" | timeout -k 10 480 env LD_LIBRARY_PATH=/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib \
  "$VISION" --mmproj "$MMPROJ" --model "$MODEL" --gpu --max-tokens 1024 >> "$VLOG" 2>&1 <&0 || RC=$?
OKLINE=$(grep -m1 '^OK ' "$VLOG" || true)
ERRLINE=$(grep -m1 -E '^ERR |CUDA|failed|error|no kernel' "$VLOG" || true)
GPU_MEM=$(nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader 2>/dev/null || true)
ev "vision rc=$RC; ok=[$OKLINE]; err=[$ERRLINE]; card=$GPU_MEM"
[[ -s "$OUT" ]] && ev "output file: $(stat -c '%s bytes' "$OUT") (SVE1 header + embeddings)"
restore_prod
if [[ -n "$OKLINE" && -s "$OUT" ]]; then
  ev "VERDICT: GPU-VISION-OK ($(echo "$OKLINE" | cut -d' ' -f2-) tokens/grid/ms; standalone log $VLOG)"
else
  ev "VERDICT: GPU-VISION-FAILED (see $VLOG)"
  exit 1
fi
