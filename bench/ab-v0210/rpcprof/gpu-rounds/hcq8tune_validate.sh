#!/usr/bin/env bash
# hcq8tune_validate.sh — after build rpcw-hip-hcq8tune2 completes:
# flip pin -> Boot A (HC_Q8 on, TUNE default) -> Boot B (TUNE=0, kernel A/B)
# -> teacher-forced bit-exact vs HCQ8.tsv -> Boot C (normal serving + dflash block 7 = final config)
R=/mnt/srcds/Strata
RPC=$R/bench/ab-v0210/rpcprof
OUT=$RPC/out/gpu-rounds-20261114
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
Q=$R/bench/ab-v0210/quality
WT=/mnt/srcds/Strata-wt-integ
PIN_DST=$R/v0233/build-mixed-gpu/rpcw-hip/strata
PIN_NEW=$WT/build-mixed-gpu/rpcw-hip-hcq8tune2/strata

while [ ! -s "$WT/build-mixed-gpu/rpcw-hip-hcq8tune2/strata.sha256" ]; do sleep 20; done
sleep 5

echo "=== 0. FLIP PIN -> tuned ($(date +%T)) ==="
bash $R/stop-all.sh >/dev/null 2>&1; sleep 2
cp "$PIN_DST" "$R/v0233/build-mixed-gpu/rpcw-hip/strata.bak-$(sha256sum "$PIN_DST" | cut -c1-8)"
cp "$PIN_NEW" "$PIN_DST" && chmod +x "$PIN_DST"
sha256sum "$PIN_DST" | cut -c1-16 > $R/v0233/build-mixed-gpu/rpcw-hip/strata.pin-sha16
echo "pin: $(cat $R/v0233/build-mixed-gpu/rpcw-hip/strata.pin-sha16)"

boot() {  # $1 = tag, $2 = env-prefix string
  bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
  ( cd $R && env $2 setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-hc8t-$1.log 2>&1 < /dev/null & )
  for i in $(seq 1 90); do
    S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log 2>/dev/null | grep -v -- '-helper' | head -1)
    RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
    PORT=$(curl -s -m 5 http://127.0.0.1:8009/v1/models 2>/dev/null | head -c 20)
    [ -n "$RD" ] && [ -n "$PORT" ] && { echo "ready $1 ($(date +%T))"; return 0; }
    sleep 10
  done
  echo "BOOT FAIL $1"; return 1
}
legs() {
  H=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*helper.log | head -1)
  python3 $RPC/ab9_driver.py --arm "$1" --leg fill80k --kind prefill --ctx 442400 --out $OUT/$1-fill80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
  python3 $RPC/ab9_driver.py --arm "$1" --leg tg80k --kind tg --ctx 442400 --max-tokens 1024 --out $OUT/$1-tg80k.json --elog "$ELOG" --hlog "$H" 2>&1 | tail -1 | cut -c1-120
  echo "--- judges $1:"; tr -d '\0' < $ELOG | grep -aE 'tokens = ' | tail -2 | cut -c1-200
}

echo "=== 1. BOOT A: HC_Q8 on, TUNE default (ld32) ($(date +%T)) ==="
boot A "STRATA_HC_Q8=1 STRATA_HC_Q8_INJECT=1" && legs H8TA

echo "=== 2. BOOT B: HC_Q8 on, TUNE=0 (upstream kernels) ($(date +%T)) ==="
boot B "STRATA_HC_Q8=1 STRATA_HC_Q8_INJECT=1 STRATA_HC_Q8_TUNE=0" && legs H8TB

echo "=== 3. TEACHER-FORCED BIT-EXACT (tuned binary, TUNE on) ($(date +%T)) ==="
bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
cd $WT
python3 tools/quant_quality.py run --config $Q/qqHCQ8.json --out $Q/out/HCQ8T.tsv 2>&1 | tail -3
echo "--- bit-exact check: HCQ8 (upstream kernel) vs HCQ8T (tuned kernel) — must be EXACT:"
python3 tools/quant_quality.py compare $Q/out/HCQ8.tsv $Q/out/HCQ8T.tsv 2>&1 | tail -6
echo "--- context: NIGHT (knob off) vs HCQ8T:"
python3 tools/quant_quality.py compare $Q/out/NIGHT.tsv $Q/out/HCQ8T.tsv 2>&1 | tail -4

echo "=== 4. BOOT C: normal serving + dflash block 7 (FINAL config) ($(date +%T)) ==="
python3 - << 'PYEOF'
import json
p = '/mnt/srcds/Strata/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json'
c = json.load(open(p))
i = c['args'].index('--dflash-block'); old = c['args'][i+1]; c['args'][i+1] = '7'
json.dump(c, open(p, 'w'), indent=2)
print(f'dflash-block: {old} -> 7 (adopted champion)')
PYEOF
boot C "" && legs H8TC

echo "=== HCQ8TUNE VALIDATE COMPLETE ($(date +%T)) — prod on tuned pin, block 7, no HC_Q8 env ==="
