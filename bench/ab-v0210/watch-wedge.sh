#!/usr/bin/env bash
# Watch a serve log for a wedged prompt read; when the same "N of M tokens" progress
# key repeats THR times, snapshot THP/compaction counters + meminfo + engine smaps
# every 30 s into OUT. Read-only; exits when the engine process goes away.
# Usage: watch-wedge.sh <serve-log> <out-file> [repeat-threshold]
set -u
LOG=$1; OUT=$2; THR=${3:-6}
last=""; same=0; wedged=0
while true; do
  if [[ ! -e $LOG ]]; then echo "$(date +%T) log gone; watcher exit" >> "$OUT" 2>/dev/null; exit 0; fi
  line=$(grep 'reading the prompt' "$LOG" 2>/dev/null | tail -1)
  if [[ -z $line ]]; then sleep 10; continue; fi
  key=$(echo "$line" | grep -oE '[0-9,]+ of [0-9,]+ tokens')
  if [[ $key == "$last" ]]; then same=$((same+1)); else same=0; last=$key; fi
  if (( same >= THR )); then
    wedged=1
    p=$(pgrep -f 'rpcw-hip/strata --serve' | head -1)
    {
      echo "=== $(date +%T) WEDGE at $key | $line"
      grep -E '^(compact_stall|compact_fail|compact_success|compact_pages_migrated|pgmigrate_fail|pgmigrate_success|thp_fault_alloc|thp_fault_fallback|thp_collapse_alloc) ' /proc/vmstat
      grep -E '^(MemFree|MemAvailable|AnonHugePages):' /proc/meminfo
      if [[ -n $p ]]; then
        grep -E '^(Rss|Pss|Anonymous|AnonHugePages|Private_Dirty|Shared_Clean):' "/proc/$p/smaps_rollup" 2>/dev/null
        ps -o etime=,stat=,pcpu=,rss= -p "$p" 2>/dev/null
      else
        echo "engine process gone"
      fi
    } >> "$OUT"
    [[ -z $p ]] && { echo "$(date +%T) engine gone; watcher exit" >> "$OUT"; exit 0; }
    sleep 30
  else
    if (( wedged )); then echo "$(date +%T) UNWEDGED ($key)" >> "$OUT"; wedged=0; fi
    sleep 10
  fi
done
