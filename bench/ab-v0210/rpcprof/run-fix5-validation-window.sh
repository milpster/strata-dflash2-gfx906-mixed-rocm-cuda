#!/usr/bin/env bash
# run-fix5-validation-window.sh — validate fix5 (engine f9b6cccb…, helper 946e0aba…)
# on real hardware, prod down (~40 min, operator-approved 14:32).
#   Arm 1 STALL-REPRO: fix5 boot, accumulate (longtg + tg1024), then killer144kL —
#     pre-fix this froze at 32,768 every time; must complete now. Checkpoint-gate
#     lines (evicted/skipped) expected under the accumulated tier.
#   Arm 1b ULTIMATE: killer250k fresh read on the SAME accumulated boot — the
#     harshest shape (pre-fix: guaranteed death).
#   Arm 2 PACKS LEVER: fresh fix5 boot with --expert-helper-ram-relief-packs under
#     adaptive — swap-in rate lines + RAM relief + decode/PP vs arm 1.
#   Then prod reboots fresh on :8009.
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

boot_trial() {
  local cfg=$1
  teardown run/trial-spill40-supervisor.json 2>/dev/null || true
  rm -f run/trial-spill40.sock run/trial-spill40-2.sock
  TRIAL_CONFIG="$cfg" STRATA_BIN="$BIN" \
  STRATA_FILL_MIB=6796 STRATA_HELPER_SLOTS=4600 STRATA_HELPER_RESERVE=512 STRATA_PORT=8019 \
    setsid nohup "$RPC/launch-trial-spill40.sh" > /mnt/srcds/Strata/logs/fix5-window-$(basename "$cfg" .json).log 2>&1 < /dev/null &
  for i in $(seq 1 60); do
    curl -s --max-time 3 -o /dev/null http://127.0.0.1:8019/v1/chat/completions && break
    sleep 10
  done
  curl -s --max-time 5 http://127.0.0.1:8019/health | jq -e '.status == "ok" and .loaded == true' >/dev/null
}

judge() {
  local tag=$1
  local ELOG boot
  ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
  boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
  echo "--- $tag judge lines:"
  sed -n "${boot},\$p" "$ELOG" | grep -aE 'strata serve: prompt' | tail -8 | cut -c1-150 || true
  echo "--- $tag checkpoint-gate lines:"
  sed -n "${boot},\$p" "$ELOG" | grep -aE 'checkpoint at [0-9]+ (evicted|skipped)' | tail -5 | cut -c1-150 || true
  echo "--- $tag swap-in lines:"
  sed -n "${boot},\$p" "$ELOG" | grep -aE 'adapt swap-in|swap-ins could not' | tail -5 | cut -c1-150 || true
  echo "--- $tag #2065 total this boot:"
  sed -n "${boot},\$p" "$ELOG" | grep -acE 'WARNING|#2065' || true
}

export AB9_URL=http://127.0.0.1:8019/v1/chat/completions
echo "window: start $(date +%H:%M:%S) — tearing down prod"
teardown run/prod-swift15-f16kv-mixed-supervisor.json
rm -f run/prod-swift15-f16kv-mixed.sock run/prod-swift15-f16kv-mixed2.sock

echo "window: arm1 boot (fix5 split-34 base)"
boot_trial "$RPC/trial-fix5-s34-base.json"
echo "window: arm1 up $(date +%H:%M:%S) — accumulating (longtg + tg1024)"
mkdir -p $RPC/out/fix5-arm1-20261111
"$RPC/run_longtg.sh" A1 $RPC/out/fix5-arm1-20261111

echo "window: arm1 killer144kL (the stall repro) $(date +%H:%M:%S)"
ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
before=$(sed -n "${boot},\$p" "$ELOG" | grep -acE 'WARNING|#2065' || true)
"$PY" "$RPC/ab9_driver.py" --arm A1 --leg killer144kL-tg1024 --kind tg --ctx 786801 --max-tokens 1024 \
  --out $RPC/out/fix5-arm1-20261111 --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL"
after=$(sed -n "${boot},\$p" "$ELOG" | grep -acE 'WARNING|#2065' || true)
echo "window: arm1 killer144kL done, #2065 delta $((after - before))"

echo "window: arm1b killer250k on the SAME accumulated boot $(date +%H:%M:%S)"
"$PY" "$RPC/ab9_driver.py" --arm A1 --leg killer250k-tg1024 --kind prefill --ctx 1365950 --max-tokens 1024 \
  --out $RPC/out/fix5-arm1-20261111 --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL"
judge arm1

echo "window: arm2 boot (fix5 split-34 PACKS) $(date +%H:%M:%S)"
boot_trial "$RPC/trial-fix5-s34-packs.json"
mkdir -p $RPC/out/fix5-arm2-20261111
echo "window: arm2 legs: 80k lane + longtg + killer144kL"
"$PY" "$RPC/ab9_driver.py" --arm A2 --leg 80000-1 --kind prefill --ctx 80000 --max-tokens 8 \
  --out $RPC/out/fix5-arm2-20261111 --elog "$ELOG" --hlog "$(jq -r '.helper_log' run/trial-spill40-supervisor.json)" --url "$AB9_URL"
"$RPC/run_longtg.sh" A2 $RPC/out/fix5-arm2-20261111
ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
"$PY" "$RPC/ab9_driver.py" --arm A2 --leg killer144kL-tg1024 --kind tg --ctx 786801 --max-tokens 1024 \
  --out $RPC/out/fix5-arm2-20261111 --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL"
echo "--- arm2 RAM map:"
"$RPC/ram_map.sh" 2>/dev/null | head -12 || true
judge arm2

echo "window: teardown arm2; rebooting prod $(date +%H:%M:%S)"
teardown run/trial-spill40-supervisor.json
rm -f run/trial-spill40.sock run/trial-spill40-2.sock
setsid nohup ./launch-prod-swift15-f16kv-dflash-w4-q4_0-mixed.sh > logs/prod-post-fix5-window-20261111.log 2>&1 < /dev/null &
for i in $(seq 1 60); do
  curl -s --max-time 3 -o /dev/null http://127.0.0.1:8009/v1/chat/completions && break
  sleep 10
done
curl -s --max-time 5 http://127.0.0.1:8009/health | jq -e '.status == "ok" and .loaded == true' >/dev/null || { echo 'window: PROD NEVER CAME UP'; exit 1; }
ELOG=$(jq -r '.engine_stderr_log' run/prod-swift15-f16kv-mixed-supervisor.json)
boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
sed -n "${boot},\$p" "$ELOG" | grep -aE 'of VRAM free with everything|mirrored' | head -2 | cut -c1-130
echo "window: DONE $(date +%H:%M:%S) — prod back"
