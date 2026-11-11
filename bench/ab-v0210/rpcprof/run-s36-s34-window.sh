#!/usr/bin/env bash
# run-s36-s34-window.sh — finish the split matrix at 144k in one prod-down window.
# Per arm (S36 then S34): fresh-boot, killer144k as FIRST request at ctx 786801
# (one byte longer than the earlier legs -> different deterministic prompt, no cache
# rescue), kind tg = fresh read + cached rerun (two decode samples, longer generation).
# Then prod reboots fresh (empty conversation RAM tier for the next operator message).
# Operator note: :8009 refuses connections for the whole window (~25 min).
set -euo pipefail
cd /mnt/srcds/Strata
RPC=/mnt/srcds/Strata/bench/ab-v0210/rpcprof

teardown() {
  local sup
  sup=$(jq -r '.supervisor_pid' "$1")
  kill -TERM "$sup" 2>/dev/null || true
  timeout 90 tail --pid="$sup" -f /dev/null || true
}

run_arm() {
  local arm=$1 split=$2
  echo "window: tearing down current pair for $arm"
  teardown run/prod-swift15-f16kv-mixed-supervisor.json 2>/dev/null || true
  teardown run/trial-spill40-supervisor.json 2>/dev/null || true
  rm -f run/prod-swift15-f16kv-mixed.sock run/prod-swift15-f16kv-mixed2.sock \
        run/trial-spill40.sock run/trial-spill40-2.sock
  echo "window: booting $arm (split-$split) $(date +%H:%M:%S)"
  TRIAL_CONFIG=$RPC/trial-s$split.json \
  STRATA_BIN=/mnt/srcds/Strata/v0219/build-mixed-gpu \
  STRATA_FILL_MIB=6796 STRATA_HELPER_SLOTS=4600 STRATA_HELPER_RESERVE=512 STRATA_PORT=8019 \
    setsid nohup "$RPC/launch-trial-spill40.sh" > /mnt/srcds/Strata/logs/s$split-killer-window-20261111.log 2>&1 < /dev/null &
  for i in $(seq 1 60); do
    curl -s --max-time 3 -o /dev/null http://127.0.0.1:8019/v1/chat/completions && break
    sleep 10
  done
  curl -s --max-time 5 http://127.0.0.1:8019/health | jq -e '.status == "ok" and .loaded == true' >/dev/null || { echo "window: $arm NEVER CAME UP"; return 1; }
  echo "window: $arm up $(date +%H:%M:%S) — killer144k first leg (ctx 786801, tg x2)"
  local ELOG HLOG boot before after
  ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
  HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
  mkdir -p $RPC/out/ab9-split-retest-s$split-20261111
  boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
  before=$(sed -n "${boot},\$p" "$ELOG" | grep -acE 'WARNING|#2065' || true)
  /mnt/srcds/Strata/.venv/bin/python $RPC/ab9_driver.py --arm "$arm" --leg killer144kL-tg1024 --kind tg --ctx 786801 --max-tokens 1024 \
    --out $RPC/out/ab9-split-retest-s$split-20261111 --elog "$ELOG" --hlog "$HLOG" --url http://127.0.0.1:8019/v1/chat/completions
  after=$(sed -n "${boot},\$p" "$ELOG" | grep -acE 'WARNING|#2065' || true)
  echo "window: $arm killer144kL #2065 during leg: $((after - before))"
  sed -n "${boot},\$p" "$ELOG" | grep -a 'prompt 14[0-9]\{4\} tokens' | tail -2 | cut -c1-160
}

echo "window: start $(date +%H:%M:%S)"
run_arm S36 36
run_arm S34 34

echo "window: tearing down S34; rebooting prod"
teardown run/trial-spill40-supervisor.json
rm -f run/trial-spill40.sock run/trial-spill40-2.sock
setsid nohup ./launch-prod-swift15-f16kv-dflash-w4-q4_0-mixed.sh > logs/prod-post-window2-20261111.log 2>&1 < /dev/null &
for i in $(seq 1 60); do
  curl -s --max-time 3 -o /dev/null http://127.0.0.1:8009/v1/chat/completions && break
  sleep 10
done
curl -s --max-time 5 http://127.0.0.1:8009/health | jq -e '.status == "ok" and .loaded == true' >/dev/null || { echo 'window: PROD NEVER CAME UP'; exit 1; }
ELOG=$(jq -r '.engine_stderr_log' run/prod-swift15-f16kv-mixed-supervisor.json)
boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
sed -n "${boot},\$p" "$ELOG" | grep -aE 'of VRAM free with everything|mirrored' | head -2 | cut -c1-130
echo "window: DONE $(date +%H:%M:%S) — prod fresh and serving"
