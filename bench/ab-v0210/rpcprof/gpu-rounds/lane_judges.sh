#!/usr/bin/env bash
# lane_judges.sh <arm> [nfill=1] — the standard judge pair: nfill fresh 80k fills + 1 tg leg
# (the tg leg's own prefill doubles as the boundary re-read leg). Prints driver lines + elog judges.
# Comparable to every campaign leg: same driver, same ctx 442400 shape, same elog classes.
set -u
R=/mnt/srcds/Strata
RPC=$R/bench/ab-v0210/rpcprof
OUT=${STRATA_LANE_OUT:-$RPC/out/gpu-rounds-20261115}
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
ARM=${1:?arm}; NFILL=${2:-1}
mkdir -p $OUT
H=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*helper.log 2>/dev/null | head -1)
for i in $(seq 1 "$NFILL"); do
  LEG=$([ "$i" = 1 ] && echo fill80k || echo "fill80k$i")
  python3 $RPC/ab9_driver.py --arm "$ARM" --leg "$LEG" --kind prefill --ctx 442400 \
    --out "$OUT/$ARM-$LEG.json" --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
done
python3 $RPC/ab9_driver.py --arm "$ARM" --leg tg80k --kind tg --ctx 442400 --max-tokens 1024 \
  --out "$OUT/$ARM-tg80k.json" --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
echo "--- judges $ARM (fresh fill / boundary re-read / warm tg):"
tr -d '\0' < "$ELOG" | grep -aE 'tokens = ' | tail -$((NFILL + 3)) | cut -c1-200
