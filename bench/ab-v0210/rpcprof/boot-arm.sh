#!/usr/bin/env bash
# boot-arm.sh — boot one trial arm and start its panel. NO stop logic:
# killing engines is stop-all.sh's job exclusively.
# usage: boot-arm.sh <ARM_NAME> <STRATA_BIN_DIR> <ENGINE_LOG_TAG> [ENV=V ...]
set -uo pipefail
cd /mnt/srcds/Strata
RPC=/mnt/srcds/Strata/bench/ab-v0210/rpcprof
ARM=$1; BIN=$2; TAG=$3; shift 3
NOPANEL=0; [ "${1:-}" = "--nopanel" ] && { NOPANEL=1; shift; }

C=/mnt/srcds/Strata-wt-helper-ipc/run/iq3s-int8-config.json
jq --arg exe "$BIN/rpcw-hip/strata" --arg log "/mnt/srcds/Strata/logs/engine-$TAG.log" \
   '.exe = $exe | .log = $log' "$C" > /tmp/opencode/arm.json && cp /tmp/opencode/arm.json "$C"
rm -f run/trial-spill40.sock run/trial-spill40-2.sock

# Spec-decode sweep knobs (empty = keep the arm config's own values):
#   STRATA_SPEC / STRATA_SPEC_MIN_P / STRATA_VRAM_RESERVE_LATER_MIB
env TRIAL_CONFIG=$C STRATA_PACK=iq3s \
  STRATA_EXPERTS=../Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S.gguf \
  STRATA_BIN=$BIN STRATA_FILL_MIB="${STRATA_FILL_MIB:-6796}" \
  STRATA_HELPER_SLOTS="${STRATA_HELPER_SLOTS:-4600}" \
  STRATA_HELPER_RESERVE="${STRATA_HELPER_RESERVE:-512}" STRATA_PORT=8019 \
  STRATA_SPEC="${STRATA_SPEC:-}" STRATA_SPEC_MIN_P="${STRATA_SPEC_MIN_P:-}" \
  STRATA_VRAM_RESERVE_LATER_MIB="${STRATA_VRAM_RESERVE_LATER_MIB:-}" "$@" \
  setsid nohup "$RPC/launch-trial-spill40.sh" > "logs/${TAG}-boot-$(date +%H%M).log" 2>&1 < /dev/null &

for i in $(seq 1 90); do
  curl -s --max-time 3 http://127.0.0.1:8019/health | jq -e '.status=="ok" and .loaded==true' >/dev/null 2>&1 \
    && { echo "$ARM up at $(date +%H:%M:%S)"; break; }
  [ $((i % 6)) -eq 0 ] && echo "  ...booting"
  sleep 10
done
curl -s --max-time 3 http://127.0.0.1:8019/health | jq -e '.loaded==true' >/dev/null 2>&1 || { echo "$ARM FAILED TO BOOT"; exit 1; }

if [ $NOPANEL -eq 1 ]; then
  echo "$ARM up, no panel (custom legs via ab9_driver.py --url http://127.0.0.1:8019)"
  exit 0
fi
HLOG=$(ls -t /mnt/srcds/Strata/logs/trial-spill40-*-helper.log | head -1)
cd $RPC && setsid nohup ./panel.sh "$ARM" "$RPC/out/ab9-window-20261113" \
  "/mnt/srcds/Strata/logs/engine-$TAG.log" "$HLOG" > /tmp/opencode/panel-$TAG.log 2>&1 < /dev/null &
echo "panel $ARM started (live view: watch-panel.sh; progress: out/ab9-window-20261113/panel-progress.log)"
