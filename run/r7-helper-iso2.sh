#!/usr/bin/env bash
# Bisect the r7 helper failure: the launcher's EXACT helper argv, standalone.
set -euo pipefail
HELPER=/mnt/srcds/Strata/v0225/build-mixed-gpu/rpcw-cuda/strata-expert-helper
helper_env=(STRATA_HELPER_REPLY_TIMEOUT_MS=600000 PATH="$PATH" HOME="$HOME" TERM="${TERM:-dumb}")
SOCKET=/tmp/opencode/r7t5.sock
SOCKET2=/tmp/opencode/r7t5-2.sock
timeout 8 env -i "${helper_env[@]}" "$HELPER" \
  --pack /mnt/srcds/ai/iq3xxs-15 \
  --experts ../Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf \
  --device 0 --slots 4600 --reserve-mib 512 --timeout 63115200000 \
  --begin-timeout 63115200000 --socket "$SOCKET" --fill-mib 6796 --socket2 "$SOCKET2" \
  2>&1 | head -4
