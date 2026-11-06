#!/usr/bin/env bash
# ab7: helper-threading measurement window (user order: prod down, test whether helper
# multithreading would help; lineage = swift-iq3_s per user, NOT ud).
# Boots the iq3_s pair manually with STRATA_RPC_PROFILE=1 exported (both engine and helper
# children inherit it), drives serial then concurrent load, then collects:
#   - helper queue_us  = head-of-line wait for the shared device lock (the threading answer)
#   - engine reply-span/finish_prompt lines (helper queue+compute+transfer as the engine saw it)
# Then stops the manual pair and restores prod supervised.
set -u
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"; PROD=${STRATA_PROD_TREE:-/mnt/user/Strata}; PY=$ROOT/.venv/bin/python
D=$ROOT/bench/ab-v0210/rpcprof
log(){ echo "=== $* $(date +%T) ==="; }
log "ab7 start: stopping prod (GPU window: profiled iq3_s pair + load driver; both VIIs; est ~8-12 min)"
bash "$PROD/stop-prod.sh"
for i in $(seq 1 60); do
  vram_b=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card[01]/{u+=$3} END{print u+0}')
  (( vram_b < 2000000000 )) && break; echo "$(date +%T) drain: $vram_b bytes"; sleep 5
done
log "ensuring port 8009 is free (first attempt died to an orphan holding it)"
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
if ss -ltn 2>/dev/null | grep -q ':8009 '; then
  echo 'PORT 8009 STILL OCCUPIED - aborting, restoring prod'
  bash "$PROD/relaunch-pair-supervised.sh"; exit 1
fi
echo 'port 8009 free'
log "booting iq3_s pair with STRATA_RPC_PROFILE=1 (manual, no supervisor)"
export STRATA_RPC_PROFILE=1
bash "$PROD/launch-prod-swift-iq3_s-dflash-w4-q4_0-mixed.sh" > /tmp/agent/ab7-launch.log 2>&1 &
LPID=$!
up=0
for i in $(seq 1 72); do
  curl -sf -m 2 -o /dev/null http://127.0.0.1:8009/health 2>/dev/null && { up=1; break; }; sleep 5
done
if (( ! up )); then
  echo "PAIR BOOT FAILED - tail of launcher log:"; tail -8 /tmp/agent/ab7-launch.log
  kill "$LPID" 2>/dev/null; bash "$PROD/relaunch-pair-supervised.sh"; exit 1
fi
log "pair up; driving load (serial then concurrent)"
"$PY" "$D/ab7_driver.py" || { echo "DRIVER FAILED"; bash "$PROD/stop-prod.sh"; bash "$PROD/relaunch-pair-supervised.sh"; exit 1; }
log "collecting profile lines"
HLOG=$(ls -t "$PROD"/logs/prod-swift-iq3_s-dflash-w4-q4_0-mixed-*-helper.log 2>/dev/null | head -1)
ELOG="$PROD/logs/engine-swift-iq3_s-dflash-w4-q4_0-mixed.log"
echo "--- helper queue_us distribution (newest helper log: $HLOG):"
grep -ho 'queue_us=[0-9]*' "$HLOG" | cut -d= -f2 | sort -n | "$PY" -c "
import sys
v = [int(x) for x in sys.stdin]
if v:
    n = len(v); p = lambda q: v[min(n - 1, int(n * q))]
    print(f'n={n} mean={sum(v)//n}us p50={p(.5)}us p90={p(.9)}us p99={p(.99)}us max={v[-1]}us')
else:
    print('no queue_us lines found')"
echo "--- helper queue line samples:"
grep -h 'queue_us' "$HLOG" | tail -5 | cut -c1-160
echo "--- engine profile lines (reply span / finish_prompt):"
tail -400 "$ELOG" | grep -iE 'reply span|finish_prompt|queue' | tail -5 | cut -c1-160
log "stopping manual pair + restoring prod supervised"
bash "$PROD/stop-prod.sh"
bash "$PROD/relaunch-pair-supervised.sh" || echo 'PROD RESTORE FAILED - MANUAL ATTENTION'
log "ab7 end"
