#!/usr/bin/env bash
# check-quiet.sh — report whether any Strata serving process survives, WITHOUT
# putting a kill/sweep pattern into the invoking shell's own command line.
# Why: stop-all.sh sweeps by pattern text, so any command line that carries that
# text (e.g. a verification pgrep) matches the sweep and gets TERMed mid-run.
# This script reads /proc/<pid>/comm and the exe basename instead, and prints
# pids only; it kills nothing.
# Usage: check-quiet.sh   (exit 0 = quiet, 1 = something is running)
set -u
found=0
for d in /proc/[0-9]*; do
  pid=${d#/proc/}
  [ "$pid" = "$$" ] && continue
  exe=$(readlink "$d/exe" 2>/dev/null) || continue
  base=${exe##*/}            # e.g. "strata", "strata-expert-helper-patient", "python3"
  case "$base" in
    strata|strata-*|mpv) ;;   # mpv is the desktop player, not ours
    python3|python)
      cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
      case "$cmd" in *serve/server.py*) echo "server $pid: ${cmd:0:90}"; found=1 ;; esac
      continue ;;
    *) continue ;;
  esac
  cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)
  case "$base" in
    mpv) continue ;;
  esac
  echo "$base $pid: ${cmd:0:90}"
  found=1
done
for sock in /mnt/srcds/Strata/run/*.sock; do
  [ -S "$sock" ] || continue
  echo "socket left: $sock"
done
for port in 8009 8019; do
  line=$(ss -H -ltn "sport = :$port" 2>/dev/null)
  [ -n "$line" ] && { echo "port $port LISTENING: $line"; found=1; }
done
if [ "$found" -eq 0 ]; then echo "QUIET: no engine, helper, server or listening port"; exit 0; fi
exit 1
