#!/usr/bin/env bash
# leg-230k.sh — the one measured leg of the 38 GiB pool rerun: one cold ~230k prefill
# plus max_tokens 10000, streamed (no short client timeout; infill_measure.py itself
# allows 7200 s). Writes L1-230k.txt/.json and tees everything to leg-230k.log.
set -euo pipefail
cd "$(dirname "$0")"
PROMPT=/mnt/srcds/Strata/bench/ab-v0210/rpcprof/out/managed-ram-pool-20261120/trial-window-fill230k/prompt-230k.txt
exec python3 /mnt/srcds/Strata/bench/ab-v0210/rpcprof/infill_measure.py \
  http://127.0.0.1:8009 "$PROMPT" 10000 "$PWD/L1-230k" 2>&1 | tee -a leg-230k.log
