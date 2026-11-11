#!/usr/bin/env bash
# run_longtg.sh — TG remeasure using YESTERDAY'S method (comparability per operator, 2026-11-11).
# longtg leg is defined exactly as ab9_pcie_ctx.sh drives it:
#   --kind tg --ctx 160000 --max-tokens 384   (fresh + cached rerun, ~29k-token residency)
# plus the standard tg1024 control (ctx 2048, x2) for lane comparability.
# Usage: run_longtg.sh ARM OUTDIR   (pair live on :8009; caller owns boots)
set -euo pipefail
ARM=${1:?ARM name required}
OUT=${2:?OUT dir required}
D=$(cd "$(dirname "$0")" && pwd)
PY=/mnt/srcds/Strata/.venv/bin/python
STATE=${AB9_STATE:-/mnt/srcds/Strata/run/trial-spill40-supervisor.json}
ELOG=$(jq -r '.engine_stderr_log' "$STATE")
HLOG=$(jq -r '.helper_log' "$STATE")
URL=${AB9_URL:-http://127.0.0.1:8009/v1/chat/completions}
mkdir -p "$OUT"
echo "longtg: arm=$ARM out=$OUT elog=$ELOG"
curl --fail --silent --show-error --max-time 5 "${AB9_HEALTH:-${URL%/v1/*}/health}" > /dev/null

boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
warn_count() { sed -n "${boot},\$p" "$ELOG" | grep -acE 'WARNING|#2065' || true; }

before=$(warn_count)
"$PY" "$D/ab9_driver.py" --arm "$ARM" --leg longtg --kind tg --ctx 160000 --max-tokens 384 \
  --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$URL"
after=$(warn_count)
echo "longtg: leg done (#2065 warnings during leg: $((after - before)))"

before=$after
"$PY" "$D/ab9_driver.py" --arm "$ARM" --leg tg1024 --kind tg --ctx 2048 --max-tokens 1024 \
  --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$URL"
after=$(warn_count)
echo "longtg: tg1024 done (#2065 warnings during leg: $((after - before)))"

echo "longtg: judge lines this boot:"
sed -n "${boot},\$p" "$ELOG" | grep -aE 'prompt 29[0-9]{3} tokens|prompt 579 tokens' | tail -4
echo "longtg: done arm=$ARM"
