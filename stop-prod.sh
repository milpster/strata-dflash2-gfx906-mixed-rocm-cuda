#!/usr/bin/env bash
# stop-prod.sh - clean prod stop (pair supervisor FIRST, then launchers, engine, helper), socket cleanup, VRAM report.
# On-disk on purpose: an inline kill must never share a command body with the patterns it
# greps for (self-match killed an inline attempt once). pgrep scans command lines, not this file.
set -u
echo '=== stop-prod: pair supervisor (first - it would restart what we kill below) ==='
pgrep -af 'pair_supervisor.py' | head -2
pkill -TERM -f 'pair_supervisor.py' 2>/dev/null && echo 'supervisor TERMed (its clean stop reaps the whole pair)' || echo '(no supervisor matched)'
sleep 5
echo '=== stop-prod: launchers + wrapper ==='
pgrep -af 'bash .*launch-prod-|/bin/bash -c rm -f .*launch-prod' | head -4
pkill -TERM -f 'bash .*launch-prod-' 2>/dev/null && echo 'launchers TERMed' || echo '(no launcher matched)'
sleep 4
echo '=== stop-prod: engine + helper ==='
pgrep -af 'v0210/build-mixed-gpu' | head -4
pkill -TERM -f 'v0210/build-mixed-gpu' 2>/dev/null && echo 'engine/helper TERMed' || echo '(none matched)'
for i in $(seq 1 15); do pgrep -f 'v0210/build-mixed-gpu' >/dev/null 2>&1 || break; sleep 2; done
if pgrep -f 'v0210/build-mixed-gpu' >/dev/null 2>&1; then
  echo 'still alive; SIGKILL'; pkill -KILL -f 'v0210/build-mixed-gpu' 2>/dev/null; sleep 3
fi
pgrep -af 'v0210/build-mixed-gpu' | head -2 || echo '(all prod processes stopped)'
rm -f ${STRATA_PROD_TREE:-/mnt/user/Strata}/run/prod-swift-iq3_s-mixed.sock ${STRATA_PROD_TREE:-/mnt/user/Strata}/run/prod-swift-iq3_s-mixed2.sock
rm -f ${STRATA_PROD_TREE:-/mnt/user/Strata}/run/prod-mixed.sock ${STRATA_PROD_TREE:-/mnt/user/Strata}/run/prod-mixed2.sock
echo "3080 VRAM: $(nvidia-smi --query-gpu=memory.used --format=csv,noheader 2>/dev/null | head -1)"
echo 'stop-prod: done'
