#!/usr/bin/env bash
# tps panel for a trial arm — same leg set as the S34 floor protocol.
# usage: panel.sh ARM OUTDIR ELOG HLOG [URL]
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

run warm                 warm    512     64
run killer144k-tg1024    prefill 786800  1024
run killer144kL-tg1024   tg      786801  1024
run longtg               tg      160000  384
run tg1024               tg      2048    1024

echo "=== panel done $(date +%H:%M:%S); records:"
"$PY" "$RPC/records.py" --update "$OUT"
