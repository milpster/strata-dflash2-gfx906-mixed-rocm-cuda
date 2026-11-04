#!/usr/bin/env bash
# probe_relief_tg.sh ARM ROUND [PERIOD_S] — run one arm while sampling the ENGINE's
# /proc counters (io + context switches) every PERIOD_S (default 2).  Near-zero overhead:
# safe on a perf-measuring boot (never strace those).
#
# Bottleneck discrimination (see JOURNAL 2026-11-04 19:4x):
#   M1 pack reads at decode  -> read_bytes keeps RISING during tg1024 legs (after load ends)
#   M2 futex convoy only     -> read_bytes flat during tg1024 legs, ctxt switches spike
# Usage: STRATA_DRIVE_STOP_AFTER= (unset) bash probe_relief_tg.sh V4 8
set -u
ARM=${1:?arm}; ROUND=${2:?round}; PER=${3:-2}
cd "$(dirname "$0")"
CSV="out/${ARM}-r${ROUND}-io.csv"
echo "t_unix,pid,read_bytes_kib,write_bytes_kib,vol_ctxt,invol_ctxt" > "$CSV"

sampler() {
  local pid=""
  # wait for the engine process (up to 12 min: load alone is ~5)
  for _ in $(seq 1 144); do
    pid=$(pgrep -f 'rpcw-hip/strata --serve' | head -1)
    [ -n "$pid" ] && break
    sleep 5
  done
  [ -z "$pid" ] && { echo "sampler: engine pid never found" >> "$CSV"; return 1; }
  echo "# sampler attached pid $pid $(date +%T)" >> "$CSV"
  while kill -0 "$pid" 2>/dev/null; do
    local io ct vol invol
    io=$(awk '/^read_bytes/{r=$2}/^write_bytes/{w=$2}END{print r" "w}' "/proc/$pid/io" 2>/dev/null) || break
    ct=$(awk '/^voluntary_ctxt/{v=$2}/^nonvoluntary_ctxt/{n=$2}END{print v" "n}' "/proc/$pid/status" 2>/dev/null) || break
    echo "$(date +%s) $pid $io $ct" | tr ' ' ',' >> "$CSV"
    sleep "$PER"
  done
  echo "# sampler detached $(date +%T)" >> "$CSV"
}

sampler &
env -u STRATA_ARENA_COLLAPSE -u STRATA_DRIVE_STOP_AFTER -u STRATA_ARENA_MMAP \
  bash run-arm.sh "$ARM" "$ROUND" 3
rc=$?
wait
echo "probe_relief_tg: arm rc=$rc, samples in $CSV"
exit "$rc"
