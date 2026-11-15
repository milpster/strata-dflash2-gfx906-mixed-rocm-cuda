#!/usr/bin/env bash
# lane_boot.sh <launcher> <tag> — stop-all, boot the given launcher, gate on a fresh ready line
# PLUS a /v1/models port probe (log-mtime alone passes stale logs). Prints the ready line.
set -u
R=/mnt/srcds/Strata
L=${1:?launcher}; TAG=${2:-boot}
bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
# setsid --fork: the launcher is orphaned at once (new session, reparented to the subreaper) —
# never a waitable child of this script. The 15:33 incident: a plain `( ... & )` left the pair
# supervisor reparented under lane_boot and the script hung in do_wait forever.
( cd $R && setsid --fork "$L" > logs/lane-$TAG.log 2>&1 < /dev/null )
RD=""
for i in $(seq 1 90); do
  S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log 2>/dev/null | grep -v -- '-helper' | head -1)
  RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
  PORT=$(curl -s -m 5 http://127.0.0.1:8009/v1/models 2>/dev/null | head -c 20)
  [ -n "$RD" ] && [ -n "$PORT" ] && break
  RD=""; sleep 10
done
if [ -z "$RD" ]; then echo "BOOT FAIL ($TAG) ($(date +%T))"; exit 1; fi
echo "ready[$TAG]: $RD ($(date +%T))"
