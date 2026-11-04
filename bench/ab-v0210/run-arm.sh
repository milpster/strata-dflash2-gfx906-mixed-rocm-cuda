#!/usr/bin/env bash
# One bench arm of the v0.2.10 A/B(/C stock) comparison: helper up -> serve up -> drive -> teardown.
# Usage: run-arm.sh A|B|C|D|E <round> [tg_runs]   (one arm at a time; waits for VRAM drain)
#  A: the prod worktree build (frozen copy)   B: the merged v0.2.10 + our mods   C: stock v0.2.10 (no mods, no helper)
#  D: B + STRATA_PREFILL_HELPER=0 (helper prompt tier off)   E: B minus --dflash (tap collection / drafter off)
set -euo pipefail
ARM=$1; ROUND=$2; TG_RUNS=${3:-3}
ROOT=/home/user/dev/Strata; B=$ROOT/bench/ab-v0210
STOCK=/home/user/dev/strata-stock-v0210
PY=$ROOT/.venv/bin/python
case $ARM in
  A) ENGINE=$B/armA/bin/strata; HELPER=$B/armA/bin/strata-expert-helper; SERVE=$B/armA/serve/server.py; CD=$B ;;
  B|D|E|F0|K1|K2|K3|K4|P0|S1|S2|S3|S4|X0|R0|V0|V1|V2|V2b|V3|V4|V5|V6|V7|V8|H1) ENGINE=$ROOT/build-v0210/rpcw-hip/strata; HELPER=$ROOT/build-v0210/rpcw-cuda/strata-expert-helper; SERVE=$ROOT/serve/server.py; CD=$ROOT ;;
  F1off|F1|F2|F2only|P1) ENGINE=$ROOT/build-tap-overlap/rpcw-hip/strata; HELPER=$ROOT/build-v0210/rpcw-cuda/strata-expert-helper; SERVE=$ROOT/serve/server.py; CD=$ROOT ;;
  C) ENGINE=$STOCK/build/rpcw-hip/strata; HELPER=; SERVE=$STOCK/serve/server.py; CD=$STOCK ;;
  *) echo "arm A|B|C|D|E|F0|F1off|F1|F2|F2only|K1|K2|K3|K4|P0|P1|S1|S2|S3|S4|X0|R0|V0|V1|V2|V2b|V3" >&2; exit 2 ;;
esac
RUNCFG=$B/run/arm$ARM-r$ROUND.json
mkdir -p "$B/run" "$B/logs" "$B/out"
sed "s#logs/$ARM-rROUND-engine.log#$B/logs/$ARM-r$ROUND-engine.log#" "$B/arm$ARM.json" > "$RUNCFG"
SOCK=$B/run/bench-mixed.sock; SOCK2=$B/run/bench-mixed2.sock
LOG=$B/logs/$ARM-r$ROUND-serve.log; HLOG=$B/logs/$ARM-r$ROUND-helper.log
ELOG=$B/logs/$ARM-r$ROUND-engine.log
PATTERN="$B/run/arm$ARM-r$ROUND.json"
[[ -n $HELPER ]] && PATTERN="$PATTERN|$HELPER"
rm -f "$SOCK" "$SOCK2"
# pre-boot policy (user directive 2026-11-04): strict serial lifecycle - fully kill any previous
# instance before booting the next; no grace periods. The bounded waits below are only for the
# KERNEL to release the killed instance's resources (port, anon, VRAM), never for processes.
pkill -9 -f '[r]pcw-(hip|cuda)/' 2>/dev/null && echo "== arm $ARM round $ROUND: killed leftover engine/helper" || true
pkill -9 -f '/serve/server[.]py' 2>/dev/null && echo "== killed leftover serve" || true
pkill -9 -f 'drive[.]py' 2>/dev/null || true
pkill -9 -f 'watch-wedge[.]sh' 2>/dev/null || true
for i in $(seq 1 30); do
  ss -ltn 2>/dev/null | grep -q ':8019 ' || break
  echo "$(date +%T) waiting for :8019 to release"
  sleep 2
done
for i in $(seq 1 30); do
  avail_mib=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)
  (( avail_mib >= 30000 )) && break
  echo "$(date +%T) waiting for the previous engine's memory to release: MemAvailable ${avail_mib} MiB"
  sleep 2
done
for i in $(seq 1 30); do
  vram_b=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card[01]/{u+=$3} END{print u+0}')
  (( vram_b < 2000000000 )) && break
  echo "$(date +%T) waiting VII VRAM drain: $vram_b bytes"
  sleep 2
done
HPID=; SPID=
cleanup() {
  trap - EXIT INT TERM
  for pid in ${SPID:-} ${HPID:-}; do
    [[ -n $pid ]] && kill -INT "$pid" 2>/dev/null || true
  done
  for i in $(seq 1 20); do
    pgrep -f "$PATTERN" >/dev/null 2>&1 || break
    sleep 5
  done
  for pid in ${SPID:-} ${HPID:-}; do
    [[ -n $pid ]] && kill -TERM "$pid" 2>/dev/null || true
  done
  pkill -TERM -f "$ENGINE --serve" 2>/dev/null || true
  sleep 5
  pkill -KILL -f "$ENGINE --serve" 2>/dev/null || true
  # VRAM drain: next arm cannot boot on half-freed cards
  for i in $(seq 1 60); do
    used=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card[01]/{s+=$3} END{print s+0}')
    (( used < 200000000 )) && break
    echo "$(date +%T) waiting VRAM drain: $used bytes"
    sleep 5
  done
}
trap cleanup EXIT
trap 'exit 130' INT
export LD_LIBRARY_PATH=/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib
if [[ -n $HELPER ]]; then
  echo "== arm $ARM round $ROUND: helper up ($HELPER)"
  HSLOTS=4200; HFILL=6400
  case $ARM in V0) HSLOTS=5100; HFILL=7000 ;; V1) HSLOTS=9000; HFILL=0 ;; V4|V5|V6) HSLOTS=4200; HFILL=6400 ;; V7|V8) HSLOTS=4600; HFILL=6700 ;; esac
  PACK=/mnt/user/ai/iq3xxs-15; EXPERTS=../Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf
  case $ARM in V6|V8) PACK=/mnt/user/ai/iq3s; EXPERTS=../Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S.gguf ;; esac
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
else
  echo "== arm $ARM round $ROUND: no helper (stock baseline)"
fi
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
# fail-fast gate (user directive 2026-11-04): a verdict that is knowable at boot must not burn the
# ~25 min drive. Three checks, each one a void run: a COLLAPSE arm whose sweep collapsed nothing,
# a helper arm whose fill report never arrived (a helperless run measures the wrong thing), and an
# engine that is already gone. Teardown matches the normal one; exit 3 so the wrapper knows.
gate_fail() {
  echo "== arm $ARM round $ROUND: GATE-FAIL: $1"
  kill -9 "$SPID" 2>/dev/null
  pkill -9 -f "$ENGINE --serve" 2>/dev/null
  [[ -n "$HELPER" ]] && pkill -9 -f "$HELPER" 2>/dev/null
  sleep 2
  exit 3
}
grep -q 'STRATA_ARENA_COLLAPSE: 0 of' "$ELOG" 2>/dev/null && gate_fail "arena collapse sweep collapsed 0 chunks (void for a collapse arm)"
if [[ $ARM == V4 || $ARM == V5 || $ARM == V6 || $ARM == V7 || $ARM == V8 ]] && ! grep -Eq -- "--expert-helper-ram-relief: [1-9][0-9]* of the helper's [0-9]+ pairs dropped from the arena" "$ELOG"; then
  gate_fail "RAM relief dropped no helper pairs (void for a relief arm)"
fi
[[ -n "$HELPER" ]] && ! grep -q 'expert-helper proxy' "$ELOG" 2>/dev/null && gate_fail "helper fill report never arrived (helperless run)"
pgrep -f "$ENGINE --serve" >/dev/null 2>&1 || gate_fail "engine not running at drive start"
# Preserve post-registration THP evidence even if a later request fails.
if [[ $ARM == H1 ]]; then
  for pid in $(pgrep -f "$ENGINE --serve"); do
    cp -- "/proc/$pid/smaps" "$B/out/$ARM-r$ROUND-pid$pid-smaps.txt"
    grep -E '^(Name|Pid|THP_enabled|VmRSS|VmLck):' "/proc/$pid/status" \
      > "$B/out/$ARM-r$ROUND-pid$pid-status.txt"
    echo "== arm $ARM round $ROUND: post-registration THP snapshot captured (pid $pid)"
  done
fi
echo "== arm $ARM round $ROUND: driving"
"$PY" "$B/drive.py" "$ARM" "$ROUND" "$TG_RUNS"
echo "== arm $ARM round $ROUND: judge lines"
grep -E 'PP tok/s|generated in .* tok/s' "$ELOG" | tail -40 | tee "$B/out/$ARM-r$ROUND-judge.txt"
# RAM-relief ladder: bank the pair's host-RAM footprint at end-of-arm (after the drive,
# before teardown) so every rung's RSS delta is measured identically.
pids=$(pgrep -f "$ENGINE --serve" 2>/dev/null)
[[ -n $HELPER ]] && pids="$pids $(pgrep -f "$HELPER" 2>/dev/null)"
{
  echo "== $(date +%T) $ARM r$ROUND RSS snapshot"
  for pid in $pids; do
    [[ -d /proc/$pid ]] || continue
    echo "pid $pid $(tr '\0' ' ' < /proc/$pid/cmdline | cut -c1-70)"
    grep -E '^(Rss|Pss|Anonymous|Pss_File):' /proc/$pid/smaps_rollup 2>/dev/null
  done
  free -g | sed -n '2p'
} | tee "$B/out/$ARM-r$ROUND-rss.txt"
echo "== arm $ARM round $ROUND: done"
