#!/usr/bin/env bash
# run-s34-killer-window.sh — one prod-down window that finishes the split matrix:
#   1. kill the stale S34 wait-chain (harmless if already gone)
#   2. teardown prod (frees the shared 3080 for the trial helper)
#   3. boot S34 (:8019, v0219, split-34, 6796/4600/512) — killer144k as FIRST leg
#   4. teardown S34
#   5. reboot prod (fresh boot: empty conversation RAM tier for the next operator msg)
# Operator note: :8009 refuses connections between steps 2 and 5 (~12 min).
set -euo pipefail
cd /mnt/srcds/Strata
RPC=/mnt/srcds/Strata/bench/ab-v0210/rpcprof

echo "window: start $(date +%H:%M:%S)"
pkill -f 'S34 fresh boot ready' 2>/dev/null || true
sleep 2

teardown() {
  local sup
  sup=$(jq -r '.supervisor_pid' "$1")
  kill -TERM "$sup" 2>/dev/null || true
  timeout 90 tail --pid="$sup" -f /dev/null || true
}

echo "window: tearing down prod"
teardown run/prod-swift15-f16kv-mixed-supervisor.json
rm -f run/prod-swift15-f16kv-mixed.sock run/prod-swift15-f16kv-mixed2.sock

echo "window: booting S34"
TRIAL_CONFIG=$RPC/trial-s34.json \
STRATA_BIN=/mnt/srcds/Strata/v0219/build-mixed-gpu \
STRATA_FILL_MIB=6796 STRATA_HELPER_SLOTS=4600 STRATA_HELPER_RESERVE=512 STRATA_PORT=8019 \
  setsid nohup "$RPC/launch-trial-spill40.sh" > /mnt/srcds/Strata/logs/s34-killer-window-20261111.log 2>&1 < /dev/null &

for i in $(seq 1 60); do
  curl -s --max-time 3 -o /dev/null http://127.0.0.1:8019/v1/chat/completions && break
  sleep 10
done
curl -s --max-time 5 http://127.0.0.1:8019/health | jq -e '.status == "ok" and .loaded == true' >/dev/null || { echo 'window: S34 NEVER CAME UP'; exit 1; }
echo "window: S34 up $(date +%H:%M:%S) — killer144k first leg"

ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
mkdir -p $RPC/out/ab9-split-retest-s34-20261111
boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
before=$(sed -n "${boot},\$p" "$ELOG" | grep -acE 'WARNING|#2065' || true)
/mnt/srcds/Strata/.venv/bin/python $RPC/ab9_driver.py --arm S34 --leg killer144k-tg1024 --kind prefill --ctx 786800 --max-tokens 1024 \
  --out $RPC/out/ab9-split-retest-s34-20261111 --elog "$ELOG" --hlog "$HLOG" --url http://127.0.0.1:8019/v1/chat/completions
after=$(sed -n "${boot},\$p" "$ELOG" | grep -acE 'WARNING|#2065' || true)
echo "window: S34 killer144k #2065 during leg: $((after - before))"
sed -n "${boot},\$p" "$ELOG" | grep -a 'prompt 14[0-9]\{4\} tokens' | tail -1 | cut -c1-160

echo "window: tearing down S34"
teardown run/trial-spill40-supervisor.json
rm -f run/trial-spill40.sock run/trial-spill40-2.sock

echo "window: rebooting prod"
setsid nohup ./launch-prod-swift15-f16kv-dflash-w4-q4_0-mixed.sh > logs/prod-post-window-20261111.log 2>&1 < /dev/null &
for i in $(seq 1 60); do
  curl -s --max-time 3 -o /dev/null http://127.0.0.1:8009/v1/chat/completions && break
  sleep 10
done
curl -s --max-time 5 http://127.0.0.1:8009/health | jq -e '.status == "ok" and .loaded == true' >/dev/null || { echo 'window: PROD NEVER CAME UP'; exit 1; }
ELOG=$(jq -r '.engine_stderr_log' run/prod-swift15-f16kv-mixed-supervisor.json)
boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
sed -n "${boot},\$p" "$ELOG" | grep -aE 'of VRAM free with everything|mirrored' | head -2 | cut -c1-130
echo "window: DONE $(date +%H:%M:%S) — prod fresh and serving"
