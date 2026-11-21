#!/usr/bin/env bash
# Managed-RAM-pool trial capture (2026-11-20, fixer).  Records the engine's locked-RAM claim at
# one instant, from /proc only (no GPU work, no writes outside the evidence dir).
# Usage: ./capture.sh <label>   ->  <label>.txt in this directory
set -uo pipefail
E="$(cd "$(dirname "$0")" && pwd)"
STATE=/mnt/srcds/Strata/run/trial-managed-supervisor.json
LABEL=${1:?label}
OUT="$E/${LABEL}.txt"
PID=$(python3 -c "import json;print(json.load(open('$STATE')).get('engine_pid',0))" 2>/dev/null)
{
  echo "# label $LABEL  $(date -Is)"
  echo "engine_pid=$PID"
  if [ "${PID:-0}" -gt 0 ] && [ -d "/proc/$PID" ]; then
    echo "--- /proc/$PID/status (Vm*)"
    grep -E '^(Name|VmPeak|VmSize|VmLck|VmPin|VmHWM|VmRSS|RssAnon|RssFile|RssShmem):' "/proc/$PID/status"
    echo "--- /proc/$PID/smaps_rollup"
    awk '/^(Rss|Pss|Anonymous|AnonHugePages|Private_Clean|Private_Dirty):/ {print}' "/proc/$PID/smaps_rollup"
    echo "--- /proc/$PID/maps: VMAs >= 1 GiB (start-end size_kb perms path)"
    awk '{split($1,a,"-"); sz=(strtonum("0x" a[2])-strtonum("0x" a[1]))/1024;
          if (sz >= 1048576) printf "%s %8.1f MiB %s %s\n", $1, sz/1024, $2, (NF>5?$6:"[anon]")}' "/proc/$PID/maps"
    echo "--- /proc/$PID/maps: total anon mapping bytes"
    awk '{split($1,a,"-"); sz=(strtonum("0x" a[2])-strtonum("0x" a[1]));
          if (NF==5) tot+=sz} END {printf "anon_total_mib %.1f\n", tot/1048576}' "/proc/$PID/maps"
    echo "--- /proc/$PID/maps: file mappings of experts.bin"
    grep -c "experts.bin" "/proc/$PID/maps" || true
  else
    echo "(no live engine pid)"
  fi
  echo "--- /proc/meminfo"
  grep -E '^(MemTotal|MemFree|MemAvailable|SwapTotal|SwapFree|SwapCached|Committed_AS|Locked|Mlocked):' /proc/meminfo
} > "$OUT" 2>&1
cat "$OUT"
