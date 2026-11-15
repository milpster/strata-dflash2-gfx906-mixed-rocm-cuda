#!/usr/bin/env bash
# reserve64_lane.sh — reserves reduced 64 MiB each (ROCm 192->128, helper 220->156):
# boot, banner slots delta, fill/TG judges, leave serving on the new reserves.
R=/mnt/srcds/Strata
RPC=$R/bench/ab-v0210/rpcprof
OUT=$RPC/out/gpu-rounds-20261114
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log

echo "=== BOOT with reserves 128/156 ($(date +%T)) ==="
bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
( cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-res64.log 2>&1 < /dev/null & )
RD=""
for i in $(seq 1 90); do
  S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log 2>/dev/null | grep -v -- '-helper' | head -1)
  RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
  PORT=$(curl -s -m 5 http://127.0.0.1:8009/v1/models 2>/dev/null | head -c 20)
  [ -n "$RD" ] && [ -n "$PORT" ] && break
  sleep 10
done
echo "ready: ${RD:-TIMEOUT} ($(date +%T))"
[ -z "$RD" ] && { echo "BOOT FAIL"; exit 1; }
S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log | grep -v -- '-helper' | head -1)
echo "--- banner (slots vs 4983/9.67 GiB baseline; VRAM-free warning line):"
tr -d '\0' < "$S" | grep -aiE 'expert cache|VRAM free|LOW' | head -6 | cut -c1-160

echo "=== JUDGE LEGS ($(date +%T)) ==="
H=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*helper.log | head -1)
python3 $RPC/ab9_driver.py --arm RES64 --leg fill80k --kind prefill --ctx 442400 --out $OUT/RES64-fill80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
python3 $RPC/ab9_driver.py --arm RES64 --leg tg80k --kind tg --ctx 442400 --max-tokens 1024 --out $OUT/RES64-tg80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
echo "--- judges RES64:"
tr -d '\0' < $ELOG | grep -aE 'tokens = ' | tail -4 | cut -c1-200
echo "--- stall scan (post-legs):"
tr -d '\0' < "$S" | grep -aiE 'stall|watchdog|timed out|OOM|fault' | grep -av 'ple-io direct\|VRAM free' | head -5
echo "=== RES64 LANE COMPLETE ($(date +%T)) — serving on 128/156 ==="
