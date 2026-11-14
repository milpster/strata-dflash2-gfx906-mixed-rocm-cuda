#!/usr/bin/env bash
# concurrent_prefill.sh — 2x80k prefill fired simultaneously: does the loan pool collide?
R=/mnt/srcds/Strata
RPC=$R/bench/ab-v0210/rpcprof
OUT=$RPC/out/gpu-rounds-20261114
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
# 1. wait for the quality retry to finish restoring prod
while ! grep -q 'QUALITY RETRY COMPLETE' $R/logs/quality-retry2.log 2>/dev/null; do sleep 20; done
# 2. wait for genuine serving readiness
for i in $(seq 1 90); do
  PORT=$(curl -s -m 5 http://127.0.0.1:8009/v1/models 2>/dev/null | head -c 20)
  [ -n "$PORT" ] && break
  sleep 10
done
echo "=== CONCURRENT PREFILL TEST: 2x80k simultaneous ($(date +%T)) ==="
H=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*helper.log | head -1)
S0=$(date +%s)
python3 $RPC/ab9_driver.py --arm PB1 --leg fill80k --kind prefill --ctx 442400 --out $OUT/PB1-fill80k.json --elog "$ELOG" --hlog "$H" > /tmp/opencode/pb1.out 2>&1 &
P1=$!
python3 $RPC/ab9_driver.py --arm PB2 --leg fill80k --kind prefill --ctx 442400 --out $OUT/PB2-fill80k.json --elog "$ELOG" --hlog "$H" > /tmp/opencode/pb2.out 2>&1 &
P2=$!
wait $P1; echo "PB1 done at +$(( $(date +%s) - S0 ))s"
wait $P2; echo "PB2 done at +$(( $(date +%s) - S0 ))s"
echo "--- PB1/PB2 driver tails:"
tail -1 /tmp/opencode/pb1.out | cut -c1-130; tail -1 /tmp/opencode/pb2.out | cut -c1-130
echo "--- loan/shrink/oom lines (post-test):"
tr -d '\0' < $ELOG | grep -aiE 'borrows|shrinking|do not fit|out of memory|timed out|prompt path' | tail -6 | cut -c1-160
echo "--- judges (last 4):"
tr -d '\0' < $ELOG | grep -aE 'tokens = ' | tail -4 | cut -c1-220
echo "=== CONCURRENT PREFILL COMPLETE ($(date +%T)) ==="
