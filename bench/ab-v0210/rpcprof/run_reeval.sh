#!/usr/bin/env bash
# run_reeval.sh — max-safe-occupancy reevaluation (2026-11-11, after the serving TG collapse).
# The lane gate had a blind spot: tg1024 ran at ctx 2048 and infill250k generated ~6 tokens,
# so SUSTAINED DECODE AT HIGH RESIDENCY was never measured. This runs the missing legs:
#   killer250k-tg1024  fresh ~250k-token infill (ctx 1365950 B) WITH 1024 tokens generated
#                      at that residency — the serving shape (135k+ ctx, hundreds-thousands gen)
#   tgctl              the standard tg1024 control (ctx 2048, x2) for lane comparability
# Per leg: judge line + #2065/LOW warning delta since this boot's start.
# Usage: run_reeval.sh ARM OUTDIR   (pair must be live on :8009; caller owns boots)
set -euo pipefail
ARM=${1:?ARM name required}
OUT=${2:?OUT dir required}
D=$(cd "$(dirname "$0")" && pwd)
PY=/mnt/srcds/Strata/.venv/bin/python
STATE=${AB9_STATE:-/mnt/srcds/Strata/run/prod-swift15-f16kv-mixed-supervisor.json}
ELOG=$(jq -r '.engine_stderr_log' "$STATE")
HLOG=$(jq -r '.helper_log' "$STATE")
URL=${AB9_URL:-http://127.0.0.1:8009/v1/chat/completions}
mkdir -p "$OUT"
echo "reeval: arm=$ARM out=$OUT"
echo "reeval: elog=$ELOG"
curl --fail --silent --show-error --max-time 5 "${AB9_HEALTH:-${URL%/v1/*}/health}" > /dev/null

boot=$(grep -n 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
warn_count() { sed -n "${boot},\$p" "$ELOG" | grep -cE 'WARNING|#2065' || true; }

before=$(warn_count)
"$PY" "$D/ab9_driver.py" --arm "$ARM" --leg killer144k-tg1024 --kind prefill --ctx 786800 --max-tokens 1024 \
  --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$URL"
after=$(warn_count)
echo "reeval: killer144k-tg1024 done (#2065 warnings during leg: $((after - before)))"

before=$after
"$PY" "$D/ab9_driver.py" --arm "$ARM" --leg killer250k-tg1024 --kind prefill --ctx 1365950 --max-tokens 1024 \
  --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$URL"
after=$(warn_count)
echo "reeval: killer250k-tg1024 done (#2065 warnings during leg: $((after - before)))"

before=$after
"$PY" "$D/ab9_driver.py" --arm "$ARM" --leg tgctl --kind tg --ctx 2048 --max-tokens 1024 \
  --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$URL"
after=$(warn_count)
echo "reeval: tgctl done (#2065 warnings during leg: $((after - before)))"

echo "reeval: judge lines this boot:"
sed -n "${boot},\$p" "$ELOG" | grep -E 'prompt 24[0-9]{4} tokens|prompt 579 tokens' | tail -4
echo "reeval: done arm=$ARM"
