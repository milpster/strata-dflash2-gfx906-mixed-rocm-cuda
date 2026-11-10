#!/usr/bin/env bash
# run_lane.sh — drive the ab9 D-lane against a LIVE server on :8009 (no window, no restart).
# One variable per invocation: the caller boots the pair (e.g. supervised prod) and passes
# ARM + OUT; the legs mirror the historical D lane exactly so results stay comparable:
#   warm(512,max64)  8000-1..3(prefill,max8)  80000-1..3(prefill,max8)  tg1024(ctx2048,max1024,x2)
# Engine/helper logs are resolved LIVE from the production supervisor state so the
# collector's byte slices always point at the current boot's logs.
# Usage: run_lane.sh ARM OUTDIR   (e.g. run_lane.sh S1 out/ab9-spill1-split36-20261110)
set -euo pipefail
ARM=${1:?ARM name required}
OUT=${2:?OUT dir required}
D=$(cd "$(dirname "$0")" && pwd)
PY=/mnt/srcds/Strata/.venv/bin/python
STATE=/mnt/srcds/Strata/run/prod-swift15-f16kv-mixed-supervisor.json
ELOG=$(jq -r '.engine_stderr_log' "$STATE")
HLOG=$(jq -r '.helper_log' "$STATE")
URL=${AB9_URL:-http://127.0.0.1:8009/v1/chat/completions}
mkdir -p "$OUT"
echo "lane: arm=$ARM out=$OUT"
echo "lane: elog=$ELOG"
echo "lane: hlog=$HLOG"
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8009/health > /dev/null
"$PY" "$D/ab9_driver.py" --arm "$ARM" --leg warm --kind warm --ctx 512 --out "$OUT" \
  --elog "$ELOG" --hlog "$HLOG" --url "$URL"
for seq in 1 2 3; do
  "$PY" "$D/ab9_driver.py" --arm "$ARM" --leg "8000-$seq" --kind prefill --ctx 8000 --max-tokens 8 --out "$OUT" \
    --elog "$ELOG" --hlog "$HLOG" --url "$URL"
done
for seq in 1 2 3; do
  "$PY" "$D/ab9_driver.py" --arm "$ARM" --leg "80000-$seq" --kind prefill --ctx 80000 --max-tokens 8 --out "$OUT" \
    --elog "$ELOG" --hlog "$HLOG" --url "$URL"
done
"$PY" "$D/ab9_driver.py" --arm "$ARM" --leg tg1024 --kind tg --ctx 2048 --max-tokens 1024 --out "$OUT" \
  --elog "$ELOG" --hlog "$HLOG" --url "$URL"
echo "lane: done arm=$ARM"
