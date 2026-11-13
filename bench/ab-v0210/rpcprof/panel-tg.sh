#!/usr/bin/env bash
# Lean TG panel for a trial arm (operator spec: ~5 decode runs, no filler).
# Contexts are TOKEN-scaled: --ctx is prompt BYTES at ~5.527 B/token —
# 88400 B ≈ 16k tokens, 354000 B ≈ 64k tokens, 995000 B = exactly T1's
# 180k-token infill prompt (comparable with its 620 tok/s PP record).
# Each tg leg runs twice in-driver (fresh prefill + cached rerun) — that
# read+gen pair is the driver's unit. ~6-8 min per arm.
# usage: panel-tg.sh ARM OUTDIR ELOG HLOG [URL]
set -euo pipefail
RPC=/mnt/srcds/Strata/bench/ab-v0210/rpcprof
PY=/mnt/srcds/Strata/.venv/bin/python3
ARM=$1 OUT=$2 ELOG=$3 HLOG=$4 URL=${5:-http://127.0.0.1:8019/v1/chat/completions}
mkdir -p "$OUT"

run() { # leg kind ctx mt
  echo "=== [$(date +%H:%M:%S)] $ARM $1 (kind=$2 ctx=$3 mt=$4)"
  "$PY" "$RPC/ab9_driver.py" --arm "$ARM" --leg "$1" --kind "$2" --ctx "$3" --max-tokens "$4" \
    --out "$OUT/$ARM-$1.json" --elog "$ELOG" --hlog "$HLOG" --url "$URL"
}

# every leg marker also lands in a stable file watch-panel.sh can tail (operator view)
exec > >(tee -a "$OUT/panel-progress.log") 2>&1

run warm            warm    512     64
run tg16k           tg      88400   512
run tg64k           tg      354000  512
run tg180k          tg      995000  512

echo "=== panel done $(date +%H:%M:%S); records:"
"$PY" "$RPC/records.py" --update "$OUT"
