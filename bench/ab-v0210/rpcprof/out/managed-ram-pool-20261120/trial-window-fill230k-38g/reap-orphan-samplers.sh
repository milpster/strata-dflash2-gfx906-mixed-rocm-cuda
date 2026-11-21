#!/usr/bin/env bash
# reap-orphan-samplers.sh — stop sampler loops left behind by a cancelled window.
# The pattern text lives in THIS file, never in the invoking command line, so the
# scan cannot match the shell that runs it. Prints pids only; kills samplers whose
# cmdline names one of the sampler scripts.
set -u
found=0
for d in /proc/[0-9]*; do
  pid=${d#/proc/}
  [ "$pid" = "$$" ] && continue
  cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
  case "$cmd" in
    *trial-managed-sampler.sh*|*vram-sample.sh*) ;;
    *) continue ;;
  esac
  case "$cmd" in *reap-orphan-samplers*) continue ;; esac
  echo "TERM $pid: ${cmd:0:100}"
  kill -TERM "$pid" 2>/dev/null && found=1
done
sleep 2
for d in /proc/[0-9]*; do
  pid=${d#/proc/}
  [ "$pid" = "$$" ] && continue
  cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
  case "$cmd" in
    *trial-managed-sampler.sh*|*vram-sample.sh*) ;;
    *) continue ;;
  esac
  case "$cmd" in *reap-orphan-samplers*) continue ;; esac
  echo "KILL $pid"
  kill -KILL "$pid" 2>/dev/null || true
done
[ "$found" -eq 0 ] && echo "no orphan samplers found"
echo done
