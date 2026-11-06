#!/usr/bin/env bash
# Relaunch prod under the pair supervisor: idempotent full stop, then the --recover boot.
# On-disk on purpose: a wrapper's own cmdline must never contain the launcher path while
# stop-prod's pkill 'bash .*launch-prod-' is live — that self-match killed a bg wrapper once.
# This file's process cmdline is 'bash ./relaunch-pair-supervised.sh' (no match), and the
# launcher only starts AFTER stop-prod.sh has fully returned.
set -u
ROOT=${STRATA_PROD_TREE:-/mnt/user/Strata}
cd "$ROOT"
bash stop-prod.sh
nohup bash "$ROOT/launch-prod-swift-iq3s-f16kv-dflash-w4-q4_0-mixed.sh" --recover \
  > /tmp/agent/prod-pair-supervised.log 2>&1 &
echo "supervisor launcher pid $!"
for i in $(seq 1 40); do
  if curl -sf -m 2 -o /dev/null http://127.0.0.1:8009/health 2>/dev/null; then
    echo "health 200 after ~$((i*15))s"
    echo '=== supervisor state ==='
    cat run/prod-swift-iq3s-f16kv-mixed-supervisor.json 2>/dev/null
    exit 0
  fi
  sleep 15
done
echo 'BOOT FAILED - no health in 10 min'
tail -15 /tmp/agent/prod-pair-supervised.log
cat run/prod-swift-iq3s-f16kv-mixed-supervisor.json 2>/dev/null
exit 1
