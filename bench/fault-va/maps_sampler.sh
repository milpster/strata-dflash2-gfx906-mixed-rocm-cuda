#!/usr/bin/env bash
# maps_sampler.sh PID OUTDIR [INTERVAL_S] [KEEP]
#
# Why: the engine dies on an amdgpu permission fault whose kernel line names the
# faulting host VA, but the process is gone by the time anyone looks. A periodic
# /proc/<pid>/maps snapshot lets that VA be resolved offline to the mapping it lived
# in (file-backed pack mapping, anon arena, pinned ring, ...), which decides between
# the competing explanations without any root-only tooling.
set -euo pipefail
PID=${1:?pid}
OUT=${2:?outdir}
IV=${3:-3}
KEEP=${4:-400}
mkdir -p "$OUT"
n=0
while kill -0 "$PID" 2>/dev/null; do
  ts=$(date +%s)
  { echo "# pid=$PID ts=$ts iso=$(date -Is)"; cat "/proc/$PID/maps" 2>/dev/null; } > "$OUT/maps-$ts.txt" 2>/dev/null || true
  # AnonHugePages/ShmemHugePages are the THP evidence: the journal found the 1.27 GiB
  # fault-time figure was uncorroborated because this list omitted them.
  { echo "# pid=$PID ts=$ts"; grep -E '^(Rss|Pss|Anonymous|AnonHugePages|ShmemHugePages|Shared_Clean|Shared_Dirty|Private_Clean|Private_Dirty|Swap):' "/proc/$PID/smaps_rollup" 2>/dev/null; } > "$OUT/rollup-$ts.txt" 2>/dev/null || true
  n=$((n+1))
  if [ "$n" -gt "$KEEP" ]; then rm -f "$(find "$OUT" -maxdepth 1 -name 'maps-*.txt' -print -quit)" "$(find "$OUT" -maxdepth 1 -name 'rollup-*.txt' -print -quit)" 2>/dev/null || true; fi
  sleep "$IV"
done
echo "sampler: pid $PID gone; $n snapshots in $OUT"
