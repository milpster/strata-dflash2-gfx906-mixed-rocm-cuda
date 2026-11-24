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
  # PER-VMA huge pages for the big anonymous mappings. Why: smaps_rollup is
  # process-wide, so it cannot say whether the 32 GiB pool VMA itself was collapsed -
  # which is exactly the question that decides whether MADV_NOHUGEPAGE is enough.
  # Only VMAs over 1 GiB are recorded (the pool, the arena); that keeps the file small
  # and is where a THP collapse would matter.
  { echo "# pid=$PID ts=$ts per-vma huge pages (>1 GiB anon mappings)"
    awk '/^[0-9a-f]+-[0-9a-f]+ /{split($0,a,"-"); split(a[2],b," "); sz=(strtonum("0x" b[1])-strtonum("0x" a[1]));
         if (sz>1073741824) {vma=$1; perms=$2; big=1; ah=0; flags=""} else big=0}
         big && /^AnonHugePages:/{ah=$2}
         big && /^VmFlags:/{for(i=2;i<=NF;i++) flags=flags $i " "; printf "%s %s anonhuge_kb=%s flags=%s\n", vma, perms, ah, flags; big=0}' "/proc/$PID/smaps" 2>/dev/null
  } > "$OUT/huge-$ts.txt" 2>/dev/null || true
  n=$((n+1))
  if [ "$n" -gt "$KEEP" ]; then rm -f "$(find "$OUT" -maxdepth 1 -name 'maps-*.txt' -print -quit)" "$(find "$OUT" -maxdepth 1 -name 'rollup-*.txt' -print -quit)" "$(find "$OUT" -maxdepth 1 -name 'huge-*.txt' -print -quit)" 2>/dev/null || true; fi
  sleep "$IV"
done
echo "sampler: pid $PID gone; $n snapshots in $OUT"
