#!/usr/bin/env bash
# run-lever2-window.sh — packs lever ROUND-2 validation (7638ca74b prefill routing
# + b749c246c cold-only drop). Informational verdict: prod stays on v0220 regardless;
# no config mutation. Acceptance: prefill `adapt swap-in:` lines fire with ms/slot
# << 52, 80k PP back toward 305, drop split ~7 GiB hot / 8-10 GiB cold, killer completes.
set -uo pipefail   # no -e: a failed leg must not skip teardown/prod boot
cd /mnt/srcds/Strata
RPC=/mnt/srcds/Strata/bench/ab-v0210/rpcprof
BIN=/mnt/srcds/Strata-wt-helper-ipc/build-mixed-gpu/fix5-run
PY=/mnt/srcds/Strata/.venv/bin/python

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
    > logs/prod-v0220-post-lever2-20261111.log 2>&1 < /dev/null &
  for _ in $(seq 1 60); do
    curl -s --max-time 3 http://127.0.0.1:8009/health \
      | jq -e '.status=="ok" and .loaded==true' >/dev/null 2>&1 && return 0
    sleep 10
  done
  return 1
}

echo "lever2 window: start $(date +%H:%M:%S) — tearing down prod (v0220)"
teardown run/prod-swift15-f16kv-mixed-supervisor.json
teardown run/trial-spill40-supervisor.json

echo "lever2: booting round-2 arm (b749c246c, packs ON, hot-mib default 7000) $(date +%H:%M:%S)"
TRIAL_CONFIG="$RPC/trial-fix5-s34-packs.json" STRATA_BIN="$BIN" \
STRATA_FILL_MIB=6796 STRATA_HELPER_SLOTS=4600 STRATA_HELPER_RESERVE=512 STRATA_PORT=8019 \
  setsid nohup "$RPC/launch-trial-spill40.sh" > logs/lever2-arm-20261111.log 2>&1 < /dev/null &
for _ in $(seq 1 60); do
  curl -s --max-time 3 -o /dev/null http://127.0.0.1:8019/v1/chat/completions && break
  sleep 10
done
curl -s --max-time 5 http://127.0.0.1:8019/health | jq -e '.status=="ok" and .loaded==true' >/dev/null || {
  echo 'lever2: ARM NEVER CAME UP'
  teardown run/trial-spill40-supervisor.json
  prod_up && echo "lever2: prod back up (v0220)" || echo 'lever2: PROD BOOT FAILED — manual action'
  exit 1
}
echo "lever2: arm up $(date +%H:%M:%S)"

export AB9_URL=http://127.0.0.1:8019/v1/chat/completions
ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
mkdir -p $RPC/out/lever2-20261111

"$PY" "$RPC/ab9_driver.py" --arm L2 --leg 80000-1 --kind prefill --ctx 80000 --max-tokens 8 \
  --out $RPC/out/lever2-20261111 --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo 'lever2: 80k leg FAILED'
"$RPC/run_longtg.sh" L2 $RPC/out/lever2-20261111 || echo 'lever2: longtg FAILED'
ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
"$PY" "$RPC/ab9_driver.py" --arm L2 --leg killer144kL-tg1024 --kind tg --ctx 786801 --max-tokens 1024 \
  --out $RPC/out/lever2-20261111 --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo 'lever2: killer FAILED'

boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
echo '--- drop split (defect 2):'
sed -n "${boot},\$p" "$ELOG" | grep -a 'stage drop split' | cut -c1-160 || true
echo '--- prefill swap-in lines (defect 1, THE acceptance signal):'
sed -n "${boot},\$p" "$ELOG" | grep -a 'adapt swap-in' | head -10 | cut -c1-160 || true
echo '--- refusal check:'
sed -n "${boot},\$p" "$ELOG" | grep -aE 'relief-packs.*(cannot open|no unbuffered)' | head -2 || echo 'none (silent arming = OK)'
echo '--- judge lines:'
sed -n "${boot},\$p" "$ELOG" | grep -a 'strata serve: prompt' | tail -6 | cut -c1-150 || true
echo '--- #2065 count:'
sed -n "${boot},\$p" "$ELOG" | grep -acE 'WARNING|#2065' || true

echo "lever2: teardown; booting prod v0220 $(date +%H:%M:%S)"
teardown run/trial-spill40-supervisor.json
prod_up && echo "lever2: DONE $(date +%H:%M:%S) — prod back on v0220" \
             || echo 'lever2: PROD BOOT FAILED — manual action'
