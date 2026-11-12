#!/usr/bin/env bash
# run-arm2-window.sh — packs-lever window (operator-approved 15:0x, ~20 min, :8009 down).
# Fix5 boot with --expert-helper-ram-relief-packs under adaptive:
#   - 80k lane leg (PP check), longtg + killer144kL (decode), RAM map, judge +
#     swap-in rate lines + checkpoint-gate lines + #2065 counts.
# Then prod reboots fresh on :8009.
# NOTE: every possibly-empty grep pipeline ends in `|| true` (pipefail trap fixed).
set -euo pipefail
cd /mnt/srcds/Strata
RPC=/mnt/srcds/Strata/bench/ab-v0210/rpcprof
BIN=/mnt/srcds/Strata-wt-helper-ipc/build-mixed-gpu/fix5-run
PY=/mnt/srcds/Strata/.venv/bin/python

teardown() {
  local sup
  sup=$(jq -r '.supervisor_pid' "$1")
  kill -TERM "$sup" 2>/dev/null || true
  timeout 90 tail --pid="$sup" -f /dev/null || true
}

echo "arm2: start $(date +%H:%M:%S) — tearing down prod"
teardown run/prod-swift15-f16kv-mixed-supervisor.json
rm -f run/prod-swift15-f16kv-mixed.sock run/prod-swift15-f16kv-mixed2.sock
teardown run/trial-spill40-supervisor.json 2>/dev/null || true
rm -f run/trial-spill40.sock run/trial-spill40-2.sock

echo "arm2: booting fix5 PACKS (split-34)"
TRIAL_CONFIG="$RPC/trial-fix5-s34-packs.json" STRATA_BIN="$BIN" \
STRATA_FILL_MIB=6796 STRATA_HELPER_SLOTS=4600 STRATA_HELPER_RESERVE=512 STRATA_PORT=8019 \
  setsid nohup "$RPC/launch-trial-spill40.sh" > /mnt/srcds/Strata/logs/arm2-window-20261111.log 2>&1 < /dev/null &
for i in $(seq 1 60); do
  curl -s --max-time 3 -o /dev/null http://127.0.0.1:8019/v1/chat/completions && break
  sleep 10
done
curl -s --max-time 5 http://127.0.0.1:8019/health | jq -e '.status == "ok" and .loaded == true' >/dev/null || { echo 'arm2: NEVER CAME UP'; exit 1; }
echo "arm2: up $(date +%H:%M:%S)"

export AB9_URL=http://127.0.0.1:8019/v1/chat/completions
ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
mkdir -p $RPC/out/fix5-arm2-20261111

echo "arm2: 80k lane leg"
"$PY" "$RPC/ab9_driver.py" --arm A2 --leg 80000-1 --kind prefill --ctx 80000 --max-tokens 8 \
  --out $RPC/out/fix5-arm2-20261111 --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL"

echo "arm2: longtg"
"$RPC/run_longtg.sh" A2 $RPC/out/fix5-arm2-20261111

echo "arm2: killer144kL $(date +%H:%M:%S)"
ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
"$PY" "$RPC/ab9_driver.py" --arm A2 --leg killer144kL-tg1024 --kind tg --ctx 786801 --max-tokens 1024 \
  --out $RPC/out/fix5-arm2-20261111 --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL"

echo "arm2: RAM map:"
"$RPC/ram_map.sh" 2>/dev/null | head -14 || true

boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
echo "--- arm2 judge lines:"
sed -n "${boot},\$p" "$ELOG" | grep -aE 'strata serve: prompt' | tail -10 | cut -c1-150 || true
echo "--- arm2 swap-in lines:"
sed -n "${boot},\$p" "$ELOG" | grep -aE 'adapt swap-in|swap-ins could not' | tail -6 | cut -c1-160 || true
echo "--- arm2 checkpoint-gate lines:"
sed -n "${boot},\$p" "$ELOG" | grep -aE 'checkpoint at [0-9]+ (evicted|skipped)' | tail -5 | cut -c1-150 || true
echo "--- arm2 relief/drop lines:"
sed -n "${boot},\$p" "$ELOG" | grep -aiE 'relief|drop' | tail -6 | cut -c1-150 || true
echo "--- arm2 #2065 total:"
sed -n "${boot},\$p" "$ELOG" | grep -acE 'WARNING|#2065' || true

echo "arm2: teardown; rebooting prod $(date +%H:%M:%S)"
teardown run/trial-spill40-supervisor.json
rm -f run/trial-spill40.sock run/trial-spill40-2.sock
setsid nohup ./launch-prod-swift15-f16kv-dflash-w4-q4_0-mixed.sh > logs/prod-post-arm2-20261111.log 2>&1 < /dev/null &
for i in $(seq 1 60); do
  curl -s --max-time 3 -o /dev/null http://127.0.0.1:8009/v1/chat/completions && break
  sleep 10
done
curl -s --max-time 5 http://127.0.0.1:8009/health | jq -e '.status == "ok" and .loaded == true' >/dev/null || { echo 'arm2: PROD NEVER CAME UP'; exit 1; }
ELOG=$(jq -r '.engine_stderr_log' run/prod-swift15-f16kv-mixed-supervisor.json)
boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
sed -n "${boot},\$p" "$ELOG" | grep -aE 'of VRAM free with everything|mirrored' | head -2 | cut -c1-130 || true
echo "arm2: DONE $(date +%H:%M:%S) — prod back"
