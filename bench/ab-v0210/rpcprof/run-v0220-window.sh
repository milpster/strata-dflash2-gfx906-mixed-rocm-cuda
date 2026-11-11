#!/usr/bin/env bash
# run-v0220-window.sh — lever validation + v0220 promotion in one window
# (operator-approved 15:4x). Lever arm on :8019 (fix5+c604afde6, packs flag ON):
#   legs 80k + longtg + killer144kL, then swap-in rates / refusal / judge / #2065.
# Verdict: swap-in lines present AND no lever refusal AND killer json landed.
#   PASS -> prod boots on the already-pinned v0220 (no packs flag on prod yet).
#   FAIL -> config exe reverts to v0219 and prod boots on the proven build.
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

echo "window: start $(date +%H:%M:%S) — tearing down prod"
teardown run/prod-swift15-f16kv-mixed-supervisor.json
rm -f run/prod-swift15-f16kv-mixed.sock run/prod-swift15-f16kv-mixed2.sock
teardown run/trial-spill40-supervisor.json 2>/dev/null || true
rm -f run/trial-spill40.sock run/trial-spill40-2.sock

echo "window: booting LEVER arm (fix5+c604afde6, packs ON) $(date +%H:%M:%S)"
TRIAL_CONFIG="$RPC/trial-fix5-s34-packs.json" STRATA_BIN="$BIN" \
STRATA_FILL_MIB=6796 STRATA_HELPER_SLOTS=4600 STRATA_HELPER_RESERVE=512 STRATA_PORT=8019 \
  setsid nohup "$RPC/launch-trial-spill40.sh" > /mnt/srcds/Strata/logs/v0220-window-lever-20261111.log 2>&1 < /dev/null &
for i in $(seq 1 60); do
  curl -s --max-time 3 -o /dev/null http://127.0.0.1:8019/v1/chat/completions && break
  sleep 10
done
curl -s --max-time 5 http://127.0.0.1:8019/health | jq -e '.status == "ok" and .loaded == true' >/dev/null || { echo 'window: LEVER ARM NEVER CAME UP'; exit 1; }
echo "window: lever arm up $(date +%H:%M:%S)"

export AB9_URL=http://127.0.0.1:8019/v1/chat/completions
ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
mkdir -p $RPC/out/v0220-lever-20261111

"$PY" "$RPC/ab9_driver.py" --arm LV --leg 80000-1 --kind prefill --ctx 80000 --max-tokens 8 \
  --out $RPC/out/v0220-lever-20261111 --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL"
"$RPC/run_longtg.sh" LV $RPC/out/v0220-lever-20261111
ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
"$PY" "$RPC/ab9_driver.py" --arm LV --leg killer144kL-tg1024 --kind tg --ctx 786801 --max-tokens 1024 \
  --out $RPC/out/v0220-lever-20261111 --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL"

boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
echo "--- lever judge lines:"
sed -n "${boot},\$p" "$ELOG" | grep -aE 'strata serve: prompt' | tail -10 | cut -c1-150 || true
echo "--- lever swap-in lines (THE metric):"
sed -n "${boot},\$p" "$ELOG" | grep -aE 'adapt swap-in|swap-ins could not' | tail -8 | cut -c1-165 || true
echo "--- lever arming/refusal lines:"
sed -n "${boot},\$p" "$ELOG" | grep -aiE 'relief-packs|pdirect|relief_packs_live|relief:' | tail -8 | cut -c1-155 || true
echo "--- lever #2065 total:"
sed -n "${boot},\$p" "$ELOG" | grep -acE 'WARNING|#2065' || true

verdict=PASS
sed -n "${boot},\$p" "$ELOG" | grep -aq 'adapt swap-in' || verdict=FAIL
sed -n "${boot},\$p" "$ELOG" | grep -aqE 'relief-packs.*(no unbuffered|cannot open)' && verdict=FAIL
[ -f $RPC/out/v0220-lever-20261111/LV-killer144kL-tg1024.json ] || verdict=FAIL
echo "window: VERDICT $verdict"

if [ "$verdict" = FAIL ]; then
  jq --arg exe /mnt/srcds/Strata/v0219/build-mixed-gpu/rpcw-hip/strata '.exe = $exe' \
    prod-config-swift15-f16kv-dflash-w4-q4_0-mixed.json > /tmp/opencode/fallback.json
  mv /tmp/opencode/fallback.json prod-config-swift15-f16kv-dflash-w4-q4_0-mixed.json
  echo "window: FALLBACK — prod config reverted to v0219"
fi

echo "window: teardown lever; booting prod $(date +%H:%M:%S)"
teardown run/trial-spill40-supervisor.json
rm -f run/trial-spill40.sock run/trial-spill40-2.sock
setsid nohup ./launch-prod-swift15-f16kv-dflash-w4-q4_0-mixed.sh > logs/prod-v0220-20261111.log 2>&1 < /dev/null &
for i in $(seq 1 60); do
  curl -s --max-time 3 -o /dev/null http://127.0.0.1:8009/v1/chat/completions && break
  sleep 10
done
curl -s --max-time 5 http://127.0.0.1:8009/health | jq -e '.status == "ok" and .loaded == true' >/dev/null || { echo 'window: PROD NEVER CAME UP'; exit 1; }
ELOG=$(jq -r '.engine_stderr_log' run/prod-swift15-f16kv-mixed-supervisor.json)
boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
sed -n "${boot},\$p" "$ELOG" | grep -aE 'engine started|of VRAM free with everything|mirrored' | head -3 | cut -c1-125 || true
echo "window: DONE $(date +%H:%M:%S) — prod up on $(jq -r '.exe' prod-config-swift15-f16kv-dflash-w4-q4_0-mixed.json | grep -o 'v021[0-9]')"
