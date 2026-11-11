#!/usr/bin/env bash
# run_split_arm.sh — split-retest leg runner (2026-11-11): yesterday's split experiments
# re-measured with BOTH methodologies so the pick is comparable old-way and right new-way:
#   1. run_lane.sh S1    — the sweep's exact lane (warm/8000x3/80000x3/tg1024)
#   2. run_longtg.sh     — yesterday's longtg tie-breaker (ctx 160000, max 384, x2)
#   3. killer144k        — today's serving-shape gate (ctx 786800, max 1024) at the
#                          operator's actual serving residency (135-144k conversations)
# Usage: run_split_arm.sh ARM OUTDIR   (pair live on :8009; caller owns boots)
set -euo pipefail
ARM=${1:?ARM name required}
OUT=${2:?OUT dir required}
D=$(cd "$(dirname "$0")" && pwd)
export AB9_STATE=${AB9_STATE:-/mnt/srcds/Strata/run/trial-spill40-supervisor.json}
mkdir -p "$OUT"
echo "split-arm: $ARM start $(date +%H:%M:%S)"
"$D/run_lane.sh" S1 "$OUT"
"$D/run_longtg.sh" "$ARM" "$OUT"
PY=/mnt/srcds/Strata/.venv/bin/python
STATE=${AB9_STATE:-/mnt/srcds/Strata/run/trial-spill40-supervisor.json}
ELOG=$(jq -r '.engine_stderr_log' "$STATE")
HLOG=$(jq -r '.helper_log' "$STATE")
boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
before=$(sed -n "${boot},\$p" "$ELOG" | grep -acE 'WARNING|#2065' || true)
"$PY" "$D/ab9_driver.py" --arm "$ARM" --leg killer144k-tg1024 --kind prefill --ctx 786800 --max-tokens 1024 \
  --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "${AB9_URL:-http://127.0.0.1:8009/v1/chat/completions}"
after=$(sed -n "${boot},\$p" "$ELOG" | grep -acE 'WARNING|#2065' || true)
echo "split-arm: killer144k done (#2065 warnings during leg: $((after - before)))"
echo "split-arm: 144k judge line:"
sed -n "${boot},\$p" "$ELOG" | grep -aE 'prompt 14[0-9]{4} tokens' | tail -1
echo "split-arm: $ARM done $(date +%H:%M:%S)"
