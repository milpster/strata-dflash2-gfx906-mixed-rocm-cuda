#!/usr/bin/env bash
# One T-lane arm (short-prompt PP): helper up -> serve up -> probe -> teardown.
# Usage: run_shortpp_arm.sh T1|T2a|T2b|T3|T4|T5 <round>   (one arm at a time; waits for VRAM drain)
# Mirrors run-arm.sh's serial-lifecycle policy and V9 helper case; drive = probe_shortpp.py.
set -euo pipefail
ARM=$1; ROUND=${2:-1}
ROOT=/home/user/dev/Strata; B=$ROOT/bench/ab-v0210; SP=$B/shortpp
PY=$ROOT/.venv/bin/python
case $ARM in
  T1|T2a|T2b|T3|T4|T5) ENGINE=$ROOT/build-v0210/rpcw-hip/strata; HELPER=$ROOT/build-v0210/rpcw-cuda/strata-expert-helper; SERVE=$ROOT/serve/server.py; CD=$ROOT ;;
  *) echo "arm T1|T2a|T2b|T3|T4|T5" >&2; exit 2 ;;
esac
RUNCFG=$SP/run/arm$ARM-r$ROUND.json
mkdir -p "$SP/run" "$SP/out/$ARM-r$ROUND" "$B/logs"
sed "s#logs/$ARM-rROUND-engine.log#$B/logs/$ARM-r$ROUND-engine.log#" "$SP/arm$ARM.json" > "$RUNCFG"
SOCK=$B/run/bench-mixed.sock; SOCK2=$B/run/bench-mixed2.sock
LOG=$B/logs/$ARM-r$ROUND-serve.log; HLOG=$B/logs/$ARM-r$ROUND-helper.log
ELOG=$B/logs/$ARM-r$ROUND-engine.log
PATTERN="$SP/run/arm$ARM-r$ROUND.json"
[[ -n $HELPER ]] && PATTERN="$PATTERN|$HELPER"
rm -f "$SOCK" "$SOCK2"
# strict serial lifecycle (user directive 2026-11-04): fully kill the previous instance first.
pkill -9 -f '[r]pcw-(hip|cuda)/' 2>/dev/null && echo "== killed leftover engine/helper" || true
pkill -9 -f '/serve/server[.]py' 2>/dev/null && echo "== killed leftover serve" || true
for i in $(seq 1 30); do ss -ltn 2>/dev/null | grep -q ':8019 ' || break; echo "$(date +%T) waiting for :8019"; sleep 2; done
for i in $(seq 1 30); do
  avail_mib=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)
  (( avail_mib >= 30000 )) && break; echo "$(date +%T) waiting MemAvailable ${avail_mib} MiB"; sleep 2
done
for i in $(seq 1 30); do
  vram_b=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card[01]/{u+=$3} END{print u+0}')
  (( vram_b < 2000000000 )) && break; echo "$(date +%T) waiting VII VRAM drain: $vram_b bytes"; sleep 2
done
HPID=; SPID=
cleanup() {
  trap - EXIT INT TERM
  for pid in ${SPID:-} ${HPID:-}; do [[ -n $pid ]] && kill -INT "$pid" 2>/dev/null || true; done
  for i in $(seq 1 20); do pgrep -f "$PATTERN" >/dev/null 2>&1 || break; sleep 5; done
  for pid in ${SPID:-} ${HPID:-}; do [[ -n $pid ]] && kill -TERM "$pid" 2>/dev/null || true; done
  pkill -TERM -f "$ENGINE --serve" 2>/dev/null || true; sleep 5
  pkill -KILL -f "$ENGINE --serve" 2>/dev/null || true
  for i in $(seq 1 60); do
    used=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card[01]/{s+=$3} END{print s+0}')
    (( used < 200000000 )) && break; echo "$(date +%T) waiting VRAM drain: $used bytes"; sleep 5
  done
}
trap cleanup EXIT
trap 'exit 130' INT
export LD_LIBRARY_PATH=/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib
echo "== arm $ARM round $ROUND: helper up ($HELPER)"
HSLOTS=4600; HFILL=7000
PACK=/mnt/user/ai/iq3s; EXPERTS=../Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S.gguf
env -i PATH="$PATH" HOME="$HOME" TERM="${TERM:-dumb}" LD_LIBRARY_PATH="$LD_LIBRARY_PATH" \
  "$HELPER" --pack "$PACK" \
    --experts "$EXPERTS" \
  --device 0 --slots "$HSLOTS" --reserve-mib 300 --timeout 7200000 \
  --begin-timeout 1800000 --socket "$SOCK" --fill-mib "$HFILL" --socket2 "$SOCK2" \
  >>"$HLOG" 2>&1 </dev/null &
HPID=$!
for i in $(seq 1 300); do
  kill -0 "$HPID" 2>/dev/null || { echo "helper exited; tail $HLOG:"; tail -5 "$HLOG"; exit 1; }
  grep -q 'expert-helper: listening on' "$HLOG" && break
  sleep 1
done
grep -q 'expert-helper: listening on' "$HLOG" || { echo "helper readiness timed out"; exit 1; }
echo "== arm $ARM round $ROUND: serve up on :8019 (engine log $ELOG)"
cd "$CD"
env -u CUDA_VISIBLE_DEVICES -u STRATA_ARENA_MMAP -u GPU_PINNED_MIN_XFER_SIZE \
  -u HSA_USERPTR_FOR_PAGED_MEM -u LD_PRELOAD \
  LD_LIBRARY_PATH="$LD_LIBRARY_PATH" \
  "$PY" "$SERVE" --engine strata --config "$RUNCFG" --port 8019 \
  >>"$LOG" 2>&1 </dev/null &
SPID=$!
for i in $(seq 1 2400); do
  kill -0 "$SPID" 2>/dev/null || { echo "server exited; tail $LOG:"; tail -15 "$LOG"; exit 1; }
  curl -sf http://127.0.0.1:8019/health >/dev/null 2>&1 && break
  (( i % 30 == 0 )) && echo "$(date +%T) still loading ($i s)"
  sleep 1
done
curl -sf http://127.0.0.1:8019/health >/dev/null || { echo "server readiness timed out"; exit 1; }
# fail-fast gates (relief arm + helper proxy, as in run-arm.sh)
if ! grep -Eq -- "--expert-helper-ram-relief: [1-9][0-9]* of the helper's [0-9]+ pairs dropped from the arena" "$ELOG"; then
  echo "== GATE-FAIL: RAM relief dropped no helper pairs"; kill -9 "$SPID" 2>/dev/null; pkill -9 -f "$ENGINE --serve" 2>/dev/null; pkill -9 -f "$HELPER" 2>/dev/null; exit 3
fi
grep -q 'expert-helper proxy' "$ELOG" 2>/dev/null || { echo "== GATE-FAIL: helper fill report missing"; kill -9 "$SPID" 2>/dev/null; pkill -9 -f "$ENGINE --serve" 2>/dev/null; pkill -9 -f "$HELPER" 2>/dev/null; exit 3; }
pgrep -f "$ENGINE --serve" >/dev/null 2>&1 || { echo "== GATE-FAIL: engine not running at probe start"; exit 3; }
echo "== arm $ARM round $ROUND: probing (short prompts)"
: > "$SP/out/probe.jsonl"
"$PY" "$SP/probe_shortpp.py" | tee "$SP/out/$ARM-r$ROUND/probe.txt"
cp "$SP/out/probe.jsonl" "$SP/out/$ARM-r$ROUND/probe.jsonl"
echo "== arm $ARM round $ROUND: judge lines"
{ grep -E 'DONE |reading the prompt|admission of slot|strata trace: (read|lent|refilled|request|prompt|window|checkpoint)' "$ELOG" | tail -80; } | tee "$SP/out/$ARM-r$ROUND-judge.txt"
echo "== arm $ARM round $ROUND: done"
