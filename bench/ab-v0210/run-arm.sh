#!/usr/bin/env bash
# One bench arm of the v0.2.10 A/B: helper up -> serve up -> drive -> teardown.
# Usage: run-arm.sh A|B <round> [tg_runs]     (one arm at a time; waits for VRAM drain)
set -euo pipefail
ARM=$1; ROUND=$2; TG_RUNS=${3:-3}
ROOT=/home/user/dev/Strata; B=$ROOT/bench/ab-v0210
PY=$ROOT/.venv/bin/python
case $ARM in
  A) ENGINE=$B/armA/bin/strata; HELPER=$B/armA/bin/strata-expert-helper; SERVE=$B/armA/serve/server.py; CD=$B ;;
  B) ENGINE=$ROOT/build-v0210/rpcw-hip/strata; HELPER=$ROOT/build-v0210/rpcw-cuda/strata-expert-helper; SERVE=$ROOT/serve/server.py; CD=$ROOT ;;
  *) echo "arm A|B" >&2; exit 2 ;;
esac
RUNCFG=$B/run/arm$ARM-r$ROUND.json
mkdir -p "$B/run" "$B/logs" "$B/out"
sed "s#logs/$ARM-rROUND-engine.log#logs/$ARM-r$ROUND-engine.log#" "$B/arm$ARM.json" > "$RUNCFG"
SOCK=$B/run/bench-mixed.sock; SOCK2=$B/run/bench-mixed2.sock
LOG=$B/logs/$ARM-r$ROUND-serve.log; HLOG=$B/logs/$ARM-r$ROUND-helper.log
ELOG=$B/logs/$ARM-r$ROUND-engine.log
rm -f "$SOCK" "$SOCK2"
HPID=; SPID=
cleanup() {
  trap - EXIT INT TERM
  for pid in ${SPID:-} ${HPID:-}; do
    [[ -n $pid ]] && kill -INT "$pid" 2>/dev/null || true
  done
  for i in $(seq 1 9); do
    pgrep -f "$B/run/arm$ARM-r$ROUND.json|$HELPER" >/dev/null 2>&1 || break
    sleep 3
  done
  for pid in ${SPID:-} ${HPID:-}; do
    [[ -n $pid ]] && kill -TERM "$pid" 2>/dev/null || true
  done
  pkill -TERM -f "$ENGINE --serve" 2>/dev/null || true
  sleep 5
  pkill -KILL -f "$ENGINE --serve" 2>/dev/null || true
  # VRAM drain: next arm cannot boot on half-freed cards
  for i in $(seq 1 60); do
    used=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/GPU\[0\]|GPU\[1\]/{s+=$2} END{print s+0}')
    (( used < 200000000 )) && break
    echo "$(date +%T) waiting VRAM drain: $used bytes"
    sleep 5
  done
}
trap cleanup EXIT
trap 'exit 130' INT
export LD_LIBRARY_PATH=/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib
echo "== arm $ARM round $ROUND: helper up ($HELPER)"
env -i PATH="$PATH" HOME="$HOME" TERM="${TERM:-dumb}" LD_LIBRARY_PATH="$LD_LIBRARY_PATH" \
  "$HELPER" --pack /mnt/user/ai/iq3xxs-15 \
  --experts /mnt/user/ai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf \
  --device 0 --slots 4200 --reserve-mib 300 --timeout 7200000 \
  --begin-timeout 1800000 --socket "$SOCK" --fill-mib 6400 --socket2 "$SOCK2" \
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
for i in $(seq 1 2400); do   # model load can take ~15 min; poll visibly
  kill -0 "$SPID" 2>/dev/null || { echo "server exited; tail $LOG:"; tail -15 "$LOG"; exit 1; }
  curl -sf http://127.0.0.1:8019/health >/dev/null 2>&1 && break
  (( i % 30 == 0 )) && echo "$(date +%T) still loading ($i s)"
  sleep 1
done
curl -sf http://127.0.0.1:8019/health >/dev/null || { echo "server readiness timed out"; exit 1; }
echo "== arm $ARM round $ROUND: driving"
"$PY" "$B/drive.py" "$ARM" "$ROUND" "$TG_RUNS"
echo "== arm $ARM round $ROUND: judge lines"
grep -E 'PP tok/s|generated in .* tok/s' "$ELOG" | tail -40 | tee "$B/out/$ARM-r$ROUND-judge.txt"
echo "== arm $ARM round $ROUND: done"
