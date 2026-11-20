#!/usr/bin/env bash
# trial-managed-sampler.sh — memory sampler for the managed-RAM-pool trial (2026-11-20).
# Read-only: appends one CSV line every INTERVAL seconds until stopped (SIGTERM/KILL).
# Columns: ts, mem_avail_mib, mem_free_mib, swap_used_mib, engine_pid, vmlck_mib,
#          rss_mib, anonhuge_mib, anon_mib (engine smaps_rollup Anonymous)
# Usage: trial-managed-sampler.sh <out.csv> [interval_s]
set -u
OUT=$1; INTERVAL=${2:-5}
[ -s "$OUT" ] || echo 'ts,mem_avail_mib,mem_free_mib,swap_used_mib,engine_pid,vmlck_mib,rss_mib,anonhuge_mib,anon_mib' >> "$OUT"
while true; do
  ts=$(date +%H:%M:%S)
  read -r avail free swapused < <(awk '/^MemAvailable:/{a=int($2/1024)} /^MemFree:/{f=int($2/1024)} /^SwapTotal:/{st=int($2/1024)} /^SwapFree:/{sf=int($2/1024)} END{print a, f, st-sf}' /proc/meminfo)
  # Find the engine by its exe basename plus --serve in argv, never by pattern: a
  # `pgrep -f` pattern also matches any shell whose command line carries that text.
  pid=""
  for d in /proc/[0-9]*; do
    exe=$(readlink "$d/exe" 2>/dev/null) || continue
    case ${exe##*/} in strata) ;; *) continue ;; esac
    cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
    case "$cmd" in *--serve*) pid=${d#/proc/}; break ;; esac
  done
  vmlck=0; rss=0; ahp=0; anon=0
  if [ -n "${pid:-}" ] && [ -r "/proc/$pid/status" ]; then
    read -r vmlck rss ahp < <(awk '/^VmLck:/{l=int($2/1024)} /^VmRSS:/{r=int($2/1024)} /^AnonHugePages:/{h=int($2/1024)} END{print l, r, h}' "/proc/$pid/status")
    anon=$(awk '/^Anonymous:/{print int($2/1024)}' "/proc/$pid/smaps_rollup" 2>/dev/null || echo 0)
  fi
  echo "$ts,$avail,$free,${swapused:-},${pid:-},$vmlck,$rss,$ahp,$anon" >> "$OUT"
  sleep "$INTERVAL"
done
