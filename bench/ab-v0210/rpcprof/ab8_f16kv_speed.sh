#!/usr/bin/env bash
# ab8: iq3xxs(swift15) + --kv f16 SPEED test vs the live iq3_s+int8 pair, in the user's
# traffic shape (one ~25k-token cold prompt + warm follow-up). RUN ON USER GO ONLY.
# Phase 0 (prod UP, no downtime): baseline driver run against the live pair.
# Phase 1 (window): stop prod -> boot the f16kv-XXS pair manually (port-guarded) ->
# identical driver run -> restore prod supervised -> print both sides + engine serve lines.
set -u
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"; PROD=${STRATA_PROD_TREE:-/mnt/user/Strata}; PY=$ROOT/.venv/bin/python
D=$ROOT/bench/ab-v0210/rpcprof
log(){ echo "=== $* $(date +%T) ==="; }
log "ab8 phase 0: BASELINE against live prod (iq3_s + int8 kv) - no downtime"
"$PY" "$D/ab8_driver.py" baseline || { echo 'BASELINE DRIVER FAILED - prod untouched'; exit 1; }
echo "--- baseline serve line (iq3_s engine log, last 3):"
grep 'strata serve: prompt' "$PROD/logs/engine-swift-iq3_s-dflash-w4-q4_0-mixed.log" | tail -3 | cut -c1-150

log "ab8 phase 1: stopping prod (GPU window: f16kv-XXS pair + identical probes; est 12-18 min total)"
bash "$PROD/stop-prod.sh"
for i in $(seq 1 60); do
  vram_b=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card[01]/{u+=$3} END{print u+0}')
  (( vram_b < 2000000000 )) && break; echo "$(date +%T) drain: $vram_b bytes"; sleep 5
done
for i in $(seq 1 24); do
  if ss -ltn 2>/dev/null | grep -q ':8009 '; then
    for pid in $(ss -ltnp 2>/dev/null | grep ':8009 ' | grep -oP 'pid=\K[0-9]+' | sort -u); do
      echo "port 8009 held by pid $pid - killing"; kill -9 "$pid" 2>/dev/null
    done
    sleep 2
  else
    break
  fi
done
ss -ltn 2>/dev/null | grep -q ':8009 ' && { echo 'PORT STILL HELD - aborting, restoring prod'; bash "$PROD/relaunch-pair-supervised.sh"; exit 1; }
echo 'port 8009 free'
log "booting f16kv-XXS pair (manual, no supervisor)"
bash "$PROD/launch-prod-swift15-f16kv-dflash-w4-q4_0-mixed.sh" > /tmp/agent/ab8-launch.log 2>&1 &
LPID=$!
up=0
for i in $(seq 1 144); do
  curl -sf -m 2 -o /dev/null http://127.0.0.1:8009/health 2>/dev/null && { up=1; break; }; sleep 5
done
if (( ! up )); then
  echo "F16KV PAIR BOOT FAILED - launcher log tail:"; tail -10 /tmp/agent/ab8-launch.log
  kill "$LPID" 2>/dev/null; bash "$PROD/stop-prod.sh"; bash "$PROD/relaunch-pair-supervised.sh"; exit 1
fi
log "f16kv pair up; identical probes"
"$PY" "$D/ab8_driver.py" f16kv-xxs || { echo 'TEST DRIVER FAILED'; bash "$PROD/stop-prod.sh"; bash "$PROD/relaunch-pair-supervised.sh"; exit 1; }
echo "--- f16kv serve lines (swift15-f16kv engine log, last 3):"
grep 'strata serve: prompt' "$PROD/logs/engine-swift15-f16kv-dflash-w4-q4_0-mixed.log" | tail -3 | cut -c1-150
log "stopping test pair + restoring prod supervised (iq3_s)"
bash "$PROD/stop-prod.sh"
bash "$PROD/relaunch-pair-supervised.sh" || echo 'PROD RESTORE FAILED - MANUAL ATTENTION'
log "ab8 end"
