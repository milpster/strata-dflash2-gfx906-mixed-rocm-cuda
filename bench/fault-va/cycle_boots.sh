#!/usr/bin/env bash
# cycle_boots.sh N [--minutes-load M]
#
# Why: the amdgpu permission fault killed ~3 of 6 boots at the 58,423-token
# prefill. Checking whether a candidate fix stops it needs repeated FRESH boots
# (the fault appears on the first big prefill of a boot, when the pool is newly
# filled), not repeats inside one boot (the second prefill is a cache hit).
#
# Each cycle: stop the previous leg -> clear its state -> boot -> wait for health
# -> fire one cold 58,423-token prefill -> record faults and memory -> keep going.
# Live output: it prints one block per cycle and per-30 s progress while booting.
set -euo pipefail
ROOT=/mnt/srcds/Strata
cd "$ROOT"
PY=python3
RUN=bench/ab-v0210/rpcprof/faultdiff_run.py
LEG=${LEG:-default-margins}
OUT=bench/ab-v0210/rpcprof/out/faultdiff-r1
CORPUS=bench/ab-v0210/rpcprof/out/port-ab-20261123/corpus/pp239k.ctx323200.bin
PROMPT_SHA=210b16b32b8658e68b863442d3c2d9ebde7eb3c9d50b0cea8c28e7d045c0ea68
TEMPLATE=bench/ab-v0210/rpcprof/out/port-ab-20261123/request-template.json
TEMPLATE_SHA=1367e0c041b3e7e6abceef27587f9ffbb8ff1d8b2ea4cc7d0141d9e945a18abd
N=${1:?usage: cycle_boots.sh N}
say() { echo "[$(date +%H:%M:%S)] cycle_boots: $*"; }
faults_since() { journalctl -k --since "$1" --no-pager 2>/dev/null | grep -cE 'no-retry page fault' || true; }

stop_leg() {
  $PY "$RUN" --leg "$LEG" --stop >/dev/null 2>&1 || true
  for p in $(ps -eo pid=,args= | awk '$2 ~ /rpcw-hip\/strata/ && $3=="--serve" {print $1}'); do kill -TERM "$p" 2>/dev/null || true; done
  sleep 6
  $PY - <<'PY' || true
import json, pathlib
p = pathlib.Path('bench/ab-v0210/rpcprof/out/faultdiff-r1/run/' + __import__('os').environ.get('LEG','default-margins') + '.state.json')
if p.is_file():
    s = json.loads(p.read_text())
    for k in ('server_pid','server_start','engine_pid','engine_start','helper_pid','helper_start'):
        s[k] = None
    p.write_text(json.dumps(s, indent=1))
PY
}

for i in $(seq 1 "$N"); do
  say "cycle $i/$N: stopping previous"
  stop_leg
  T0=$(date '+%Y-%m-%d %H:%M:%S')
  say "booting ($T0)"
  $PY "$RUN" --leg "$LEG" --boot --waive-nonengine > "/tmp/opencode/cb-boot-$i.log" 2>&1 &
  BP=$!
  ok=0
  for _ in $(seq 1 45); do
    H=$(curl -fsS -m 2 http://127.0.0.1:8009/health 2>/dev/null | head -c 12 || true)
    if [ -n "$H" ]; then ok=1; break; fi
    kill -0 "$BP" 2>/dev/null || break
    sleep 10
  done
  if [ "$ok" -ne 1 ]; then say "boot did not come up; see /tmp/opencode/cb-boot-$i.log"; tail -3 "/tmp/opencode/cb-boot-$i.log" | cut -c1-140; continue; fi
  EP=$(./bench/fault-va/engine_pid.sh) || { say 'no engine pid found'; continue; }
  say "engine $EP up; firing the cold prefill"
  $PY bench/ab-v0210/rpcprof/ab9_driver.py --arm B --leg pp239k \
    --kind prefill --ctx 323200 --max-tokens 8 \
    --out "$OUT/records/cb-$i" \
    --elog "$OUT/logs/$LEG.engine.log" --hlog "$OUT/logs/$LEG.helper.log" \
    --url http://127.0.0.1:8009/v1/chat/completions \
    --prompt-file "$CORPUS" --prompt-sha256 "$PROMPT_SHA" \
    --request-template "$TEMPLATE" --request-template-sha256 "$TEMPLATE_SHA" \
    --timeout 5400 --engine-pid "$EP" 2>&1 | tail -2 || true
  F=$(faults_since "$T0")
  JUDGE=$(tr -d '\000' < "$OUT/logs/$LEG.engine.log" | tail -300 | grep -a 'prompt 58423 tokens' | tail -1 | sed -E 's/.*(prompt 58423 tokens = [^)]*).*/\1/' || true)
  HUGE=$(grep -i anonhugepages "/proc/$EP/smaps_rollup" 2>/dev/null | awk '{print $2, $3}' || echo 'gone')
  RSS=$(awk '/VmRSS/{printf "%.1f GiB", $2/1048576}' "/proc/$EP/status" 2>/dev/null || echo gone)
  say "cycle $i done: faults=$F rss=$RSS anonhuge=$HUGE"
  echo "    judge: $JUDGE"
done
say 'all cycles done; leaving the last boot running'
