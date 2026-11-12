#!/usr/bin/env bash
# Isolate the r7 helper failure: run the launcher's exact env -i helper line standalone.
set -euo pipefail
HELPER=/mnt/srcds/Strata/v0225/build-mixed-gpu/rpcw-cuda/strata-expert-helper
helper_env=(STRATA_HELPER_REPLY_TIMEOUT_MS=600000 PATH="$PATH" HOME="$HOME" TERM="${TERM:-dumb}")
timeout 8 env -i "${helper_env[@]}" "$HELPER" \
  --pack /mnt/srcds/ai/iq3xxs-15 \
  --experts ../Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf \
  --device 0 --slots 32 --reserve-mib 512 --timeout 2000 --begin-timeout 2000 \
  --socket /tmp/opencode/r7t4.sock --fill-mib 32 2>&1 | head -4
