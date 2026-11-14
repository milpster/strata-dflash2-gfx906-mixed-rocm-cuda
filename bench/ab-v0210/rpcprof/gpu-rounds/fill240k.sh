#!/usr/bin/env bash
# fill240k.sh — 240k-token prefill stress (~1.33 MB prompt; deepest prompt-path loan) on the live boot.
R=/mnt/srcds/Strata
RPC=$R/bench/ab-v0210/rpcprof
OUT=$RPC/out/gpu-rounds-20261114
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
H=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*helper.log | head -1)
echo "=== FILL 240k ($(date +%T)) ==="
python3 $RPC/ab9_driver.py --arm F240 --leg fill240k --kind prefill --ctx 1327200 --out $OUT/F240-fill240k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
echo "=== LOAN/OOM LINES ==="
tr -d '\0' < $ELOG | grep -aiE 'borrows|shrinking|do not fit|out of memory|timed out' | tail -5 | cut -c1-150
echo "=== JUDGE ==="
tr -d '\0' < $ELOG | grep -aE 'tokens = ' | tail -2 | cut -c1-190
echo "=== FILL240K COMPLETE ($(date +%T)) ==="
