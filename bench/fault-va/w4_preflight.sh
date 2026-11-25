#!/usr/bin/env bash
# w4_preflight.sh - is this box ready for a w4_finish window, and what exactly starts it?
#
# Read-only: /proc, ps, ss, statvfs, rocm-smi, and prerequisite 0 itself (which is also
# read-only). It starts nothing, stops nothing, signals nothing, and touches no GPU.
#
# Why it exists: on 2026-11-25 a runner was launched onto a box that prerequisite 0 was
# refusing, and looked alive for an hour while nothing reached a GPU. The refusal had three
# causes and every one of them belonged to the operator: a borg backup, a game, and the agent
# host's own MCP python at 2.03 GiB - and that last one is the unwaivable per-process gate,
# which refuses while the very session doing the measuring is alive.
#
# This script answers the only two questions that matter before a window:
#   1. what, if anything, still blocks it - named, with sizes, and whose it is;
#   2. the exact command to run once it is clear.
#
# Exit 0 = ready (prerequisite 0 opens with the operator's waiver); 1 = not ready.
set -uo pipefail
cd /mnt/srcds/Strata || exit 1
PY=python3
OUT=bench/ab-v0210/rpcprof/out/port-ab-20261123
LOGD=bench/fault-va/out/logs
PORT=$(python3 -c 'import sys;sys.path.insert(0,"bench/ab-v0210/rpcprof");import port_ab_configs as p;print(p.TUPLE["port"])' 2>/dev/null || echo 8009)
READY=0

say() { echo "[$(date +%H:%M:%S)] $*"; }
fail() { say "  BLOCKED: $*"; READY=1; }

say "w4_preflight - window readiness for the W4 cells 3-4 + W3s span pair"

# 1. Nothing of the serving lane may be alive: the gate is PRE-BOOT.
# ps+grep rather than pgrep -f on purpose: a pgrep -f pattern appears in the caller's own
# command line and matches itself (four incidents on 2026-11-12). The [p] bracket trick keeps
# this grep from matching its own argv instead.
# shellcheck disable=SC2009
gpu=$(ps -eo pid=,args= | grep -cE '[p]air_supervisor|[r]pcw-hip/strata --serve')
if [ "$gpu" -ne 0 ]; then
  fail "$gpu serving process(es) are alive; the window opens pre-boot only"
  # shellcheck disable=SC2009
  ps -eo pid=,etimes=,args= | grep -E '[p]air_supervisor|[r]pcw-hip/strata --serve' | head -4 | sed 's/^/      /' | cut -c1-120
else
  say "  serving lane: none alive (correct for a pre-boot window)"
fi

# 2. The port must be free, and the GPUs must have been released by the last boot.
if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  fail "port $PORT is already listening"
else
  say "  port $PORT: free"
fi
vram=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card[0-9]+/{s+=$3} END{print s+0}')
if [ "${vram:-0}" -ge 2000000000 ]; then
  fail "VRAM still held: $((vram / 1048576)) MiB across cards; the engine would see one GPU"
else
  say "  VRAM: released ($(( ${vram:-0} / 1048576 )) MiB held)"
fi

# 3. Disk where the leg caches are written.
free_gib=$(df -BG --output=avail "$OUT" 2>/dev/null | tail -1 | tr -dc '0-9')
if [ -z "${free_gib:-}" ] || [ "$free_gib" -lt 40 ]; then
  fail "disk: ${free_gib:-unknown} GiB free on $OUT (gate needs >= 40)"
else
  say "  disk: ${free_gib} GiB free on $OUT (gate needs >= 40)"
fi

# 4. prerequisite 0 itself, with the operator's recorded waiver. The waiver is passed because
#    every boot in this harness passes it and the desktop's baseline RSS (12.5-13.3 GiB) is
#    above the 8 GiB sum gate; it covers the SUM only, never the per-process or stage-core rule.
log="$LOGD/preflight-$(date +%Y%m%d-%H%M%S).log"
mkdir -p "$LOGD"
say "  prerequisite 0 (with --waive-nonengine, as the boots run) -> $log"
$PY bench/ab-v0210/rpcprof/port_ab_prereq0.py --out bench/fault-va/out/prereq-preflight \
  --cache-dir "$OUT" --waive-nonengine > "$log" 2>&1
gate_rc=$?
verdict=$(grep -aE '^[[:space:]]*VERDICT:' "$log" | tail -1 | sed 's/^[[:space:]]*//')
case "$verdict|$gate_rc" in
  "VERDICT: OPEN"*"|0") say "  gate: $verdict" ;;
  *) fail "gate: ${verdict:-no VERDICT line} (exit $gate_rc)"; READY=1 ;;
esac
if [ "$READY" -ne 0 ]; then
  say "  the gate's own reasons:"
  grep -aE 'FAIL:' "$log" | sed 's/^ */      /' | cut -c1-140
  # The per-process rule is the one nobody can waive, and on this box it is usually the agent
  # host measuring itself. Name those processes and their sizes so the owner can decide.
  say "  the unwaivable per-process rule (>= 2 GiB) is about these, if listed above:"
  ps -eo pid=,rss=,etimes=,args= | awk '$2 >= 2097152 {printf "      pid %s  %.2f GiB  up %.0f min  %s\n", $1, $2/1048576, $3/60, substr($0, index($0,$4), 60)}' | head -6
fi

if [ "$READY" -eq 0 ]; then
  say "READY: the window can open."
else
  say "NOT READY: nothing will be booted until the blockers above are cleared. Each one belongs"
  say "  to the operator: a backup or a game can be closed, and the agent host's own MCP python"
  say "  sits inside the unwaivable per-process rule because the session doing the measuring is"
  say "  itself a 2 GiB process. Closing that is the operator's call, not a harness decision."
fi

# 5. The command, printed either way so it is never reconstructed from memory.
echo
say "the window command (detached, logs to $LOGD/w4-finish.log):"
echo "  setsid nohup ./bench/fault-va/w4_finish.sh --waive-nonengine \\"
echo "      > $LOGD/w4-finish.log 2>&1 < /dev/null &"
echo
say "then watch it in this view:  tail -f $LOGD/w4-finish.log"
say "it plans 8 cells: W4 rep-2 cross pairs (B engine+server A, A engine+server B) and the W3s"
say "span pair (r0 warmup + r1 claim per arm, both shapes each)."
exit "$READY"
