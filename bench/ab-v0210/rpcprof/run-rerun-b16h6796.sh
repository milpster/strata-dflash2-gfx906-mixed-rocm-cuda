#!/usr/bin/env bash
# run-rerun-b16h6796.sh — rerun the b16-h6796 arm after the GPU hiccup (operator 18:0x):
# v0216 + helper 6796/4600/512, legs longtg + killer144k only (the two that aborted).
set -uo pipefail
cd /mnt/srcds/Strata
RPC=/mnt/srcds/Strata/bench/ab-v0210/rpcprof
PY=/mnt/srcds/Strata/.venv/bin/python
OUT=$RPC/out/iq3xxs-sweep-20261111
mkdir -p "$OUT"

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
    > logs/prod-post-rerun-20261111.log 2>&1 < /dev/null &
  for _ in $(seq 1 60); do
    curl -s --max-time 3 http://127.0.0.1:8009/health \
      | jq -e '.status=="ok" and .loaded==true' >/dev/null 2>&1 && return 0
    sleep 10
  done
  return 1
}

echo "rerun: start $(date +%H:%M:%S) — tearing down prod"
teardown run/prod-swift15-f16kv-mixed-supervisor.json
teardown run/trial-spill40-supervisor.json

jq --arg exe /mnt/srcds/Strata/v0216/build-mixed-gpu/rpcw-hip/strata '.exe = $exe' \
  "$RPC/trial-s34.json" > "$OUT/conf-b16-h6796-r.json"
echo "rerun: booting b16-h6796 (v0216, 6796/4600/512) $(date +%H:%M:%S)"
TRIAL_CONFIG="$OUT/conf-b16-h6796-r.json" STRATA_BIN=/mnt/srcds/Strata/v0216/build-mixed-gpu \
STRATA_FILL_MIB=6796 STRATA_HELPER_SLOTS=4600 STRATA_HELPER_RESERVE=512 STRATA_PORT=8019 \
  setsid nohup "$RPC/launch-trial-spill40.sh" > logs/sweep-b16-h6796-rerun-20261111.log 2>&1 < /dev/null &
for _ in $(seq 1 60); do
  curl -s --max-time 3 -o /dev/null http://127.0.0.1:8019/v1/chat/completions && break
  sleep 10
done
curl -s --max-time 5 http://127.0.0.1:8019/health | jq -e '.status=="ok" and .loaded==true' >/dev/null || {
  echo 'rerun: BOOT FAILED'
  teardown run/trial-spill40-supervisor.json
  prod_up && echo 'rerun: prod back up' || echo 'rerun: PROD BOOT FAILED'
  exit 1
}

export AB9_URL=http://127.0.0.1:8019/v1/chat/completions
ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
"$RPC/run_longtg.sh" b16-h6796-r "$OUT" || echo 'rerun: longtg FAILED again'
ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
"$PY" "$RPC/ab9_driver.py" --arm b16-h6796-r --leg killer144k-tg1024 --kind tg --ctx 786801 --max-tokens 1024 \
  --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo 'rerun: killer144k FAILED again'

boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
echo '--- rerun judge lines:'
sed -n "${boot},\$p" "$ELOG" | grep -a 'strata serve: prompt' | tail -8 | cut -c1-145 || true
echo '--- rerun #2065 count:'
sed -n "${boot},\$p" "$ELOG" | grep -acE 'WARNING|#2065' || true

echo "rerun: teardown; booting prod $(date +%H:%M:%S)"
teardown run/trial-spill40-supervisor.json
prod_up && echo "rerun: DONE $(date +%H:%M:%S) — prod back on v0220" \
             || echo 'rerun: PROD BOOT FAILED — manual action'
