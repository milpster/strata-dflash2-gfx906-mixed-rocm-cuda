#!/usr/bin/env bash
# run-iq3s-diet.sh — phase-1 diet validation: iq3_s split-34 on v0221 with the
# arena cap (20000) + hot 15000 + packs + prompt-window-retry. The diet tuple
# that should make iq3_s servable again (was 55.1 GiB RSS / 8.6 avail / 7.4
# swap when we rolled it back). Panel + memory sampling + abort greps.
# Window ~45-60 min; prod (iq3xxs v0221 diet) auto-returns at end.
set -uo pipefail
cd /mnt/srcds/Strata
RPC=/mnt/srcds/Strata/bench/ab-v0210/rpcprof
PY=/mnt/srcds/Strata/.venv/bin/python
OUT=$RPC/out/iq3s-diet-20261111
mkdir -p "$OUT"

memsnap() {  # TAG
  echo "diet-mem $1: $(free -m | awk 'NR==2{printf "avail=%s MiB free=%s", $7, $4}') swap=$(free -m | awk 'NR==3{print $3}') | engine RSS: $(ps -eo rss,args --sort=-rss | grep -a 'strata --serve' | grep -av grep | head -1 | awk '{printf "%.1f GiB", $1/1048576}')"
}

teardown() {
  local sup
  sup=$(jq -r '.supervisor_pid' "$1" 2>/dev/null) || return 0
  [ "$sup" = "null" ] && return 0
  kill -TERM "$sup" 2>/dev/null || true
  timeout 90 tail --pid="$sup" -f /dev/null || true
  rm -f run/trial-spill40.sock run/trial-spill40-2.sock \
        run/prod-swift15-f16kv-mixed.sock run/prod-swift15-f16kv-mixed2.sock
}
prod_up() {
  setsid nohup ./launch-prod-swift15-f16kv-dflash-w4-q4_0-mixed.sh \
    > logs/prod-post-iq3s-diet-20261111.log 2>&1 < /dev/null &
  for _ in $(seq 1 90); do
    curl -s --max-time 3 http://127.0.0.1:8009/health \
      | jq -e '.status=="ok" and .loaded==true' >/dev/null 2>&1 && return 0
    sleep 10
  done
  return 1
}

echo "iq3s-diet: start $(date +%H:%M:%S) — tearing down prod (iq3xxs v0221 diet)"
teardown run/prod-swift15-f16kv-mixed-supervisor.json

jq '.layer_split = "34" | .exe = "/mnt/srcds/Strata/v0221/build-mixed-gpu/rpcw-hip/strata" |
    .args += ["--expert-helper-ram-relief-packs",
              "--expert-helper-relief-hot-mib", "15000",
              "--expert-helper-relief-arena-cap-mib", "20000",
              "--prompt-window-retry"]' \
  "$RPC/trial-iq3s-s36.json" > "$OUT/conf-diet-s34.json"
echo "iq3s-diet: booting diet-s34 (split 34, v0221, 7400/5000/300, cap 20000, hot 15000, retry) $(date +%H:%M:%S)"
TRIAL_CONFIG="$OUT/conf-diet-s34.json" STRATA_BIN=/mnt/srcds/Strata/v0221/build-mixed-gpu \
STRATA_FILL_MIB=7400 STRATA_HELPER_SLOTS=5000 STRATA_HELPER_RESERVE=300 STRATA_PORT=8019 \
  setsid nohup "$RPC/launch-trial-spill40.sh" > logs/iq3s-diet-s34-20261111.log 2>&1 < /dev/null &
for _ in $(seq 1 150); do
  curl -s --max-time 3 -o /dev/null http://127.0.0.1:8019/v1/chat/completions && break
  sleep 10
done
if ! curl -s --max-time 5 http://127.0.0.1:8019/health | jq -e '.status=="ok" and .loaded==true' >/dev/null 2>&1; then
  echo "iq3s-diet: diet-s34 BOOT FAILED"
  teardown run/trial-spill40-supervisor.json
  prod_up && echo "iq3s-diet: prod returned" || echo "iq3s-diet: PROD BOOT FAILED — manual action"
  exit 1
fi
export AB9_URL=http://127.0.0.1:8019/v1/chat/completions
ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
echo "iq3s-diet: diet-s34 up $(date +%H:%M:%S)"
echo "iq3s-diet: diet boot lines:"
sed -n "${boot},\$p" "$ELOG" | grep -aE 'arena cap:|ram-relief: [0-9]+ pairs|stage drop split|relief_packs_live' | head -5 | cut -c1-135
memsnap post-boot

"$PY" "$RPC/ab9_driver.py" --arm diet-s34 --leg warm --kind warm --ctx 512 --out "$OUT" \
  --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "iq3s-diet: warm FAILED"
for seq in 1 2 3; do
  "$PY" "$RPC/ab9_driver.py" --arm diet-s34 --leg "8000-$seq" --kind prefill --ctx 8000 --max-tokens 8 \
    --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "iq3s-diet: 8k-$seq FAILED"
done
"$PY" "$RPC/ab9_driver.py" --arm diet-s34 --leg 80000-1 --kind prefill --ctx 80000 --max-tokens 8 \
  --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "iq3s-diet: 80k FAILED"
memsnap post-80k
"$RPC/run_longtg.sh" diet-s34 "$OUT" || echo "iq3s-diet: longtg FAILED"
ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
"$PY" "$RPC/ab9_driver.py" --arm diet-s34 --leg killer144k-tg1024 --kind tg --ctx 786801 --max-tokens 1024 \
  --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "iq3s-diet: killer144k FAILED/TIMED-OUT"
memsnap post-killer

boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
echo "iq3s-diet: judge lines (tail):"
sed -n "${boot},\$p" "$ELOG" | grep -a 'strata serve: prompt' | tail -8 | cut -c1-145
echo "iq3s-diet: ABORT CHECK: zero-gen count = $(sed -n "${boot},\$p" "$ELOG" | grep -ac '0 generated in 0 ms' || true)"
echo "iq3s-diet: RETRY LINES (want >=0, means fix path armed):"
sed -n "${boot},\$p" "$ELOG" | grep -a 'tail retry' | tail -3 | cut -c1-135
echo "iq3s-diet: slow-read context lines:"
sed -n "${boot},\$p" "$ELOG" | grep -a 'slow read context' | tail -3 | cut -c1-135

teardown run/trial-spill40-supervisor.json
echo "iq3s-diet: teardown; booting prod (iq3xxs v0221 diet) $(date +%H:%M:%S)"
prod_up && echo "iq3s-diet: DONE $(date +%H:%M:%S) — prod back on iq3xxs v0221 diet" \
             || echo "iq3s-diet: PROD BOOT FAILED — manual action"
