#!/usr/bin/env bash
# S-lane bench session: stop prod -> arms serially -> ALWAYS restore prod.
# Watchable: every arm's probe and judge lines stream here as they run.
set -u
SP=/home/user/dev/Strata/bench/ab-v0210/shortpp
PROD=/mnt/user/Strata
echo "=== S-SESSION start $(date +%T): stopping prod (GPU window opens) ==="
bash "$PROD/stop-prod.sh"
FAILED=0
for ARM in S0 S1 S2 S3 S4 SP; do
  echo "=== arm $ARM begin $(date +%T) ==="
  if bash "$SP/run_shortpp_arm.sh" "$ARM" 1; then
    echo "=== arm $ARM done $(date +%T) ==="
  else
    echo "=== arm $ARM FAILED rc=$? $(date +%T) ==="
    FAILED=$((FAILED+1))
  fi
done
echo "=== arms complete, $FAILED failed. Restoring prod $(date +%T) ==="
if bash "$PROD/relaunch-pair-supervised.sh"; then
  echo '=== prod restored (supervised) ==='
else
  echo '=== PROD RESTORE FAILED - MANUAL ATTENTION NEEDED ==='
fi
echo "=== S-SESSION end $(date +%T): failed arms = $FAILED ==="
