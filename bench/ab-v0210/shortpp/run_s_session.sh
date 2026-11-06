#!/usr/bin/env bash
# S-lane bench session: stop prod -> arms serially -> ALWAYS restore prod.
# Watchable: every arm's probe and judge lines stream here as they run.
set -u
SP=${STRATA_DEV_TREE:-/home/user/dev/Strata}/bench/ab-v0210/shortpp
PROD=${STRATA_PROD_TREE:-/mnt/user/Strata}
echo "=== S-SESSION start $(date +%T): stopping prod (GPU window opens) ==="
bash "$PROD/stop-prod.sh"
echo "=== warming bench-pack page cache (session 1 paid cold-disk: S214 cold 201s, S18k stages 731s; arms converged as the cache warmed) ==="
for f in ${STRATA_AI_DIR:-/mnt/user/ai}/iq3s/*; do
  [[ -f "$f" ]] || continue
  s=$SECONDS; cat "$f" > /dev/null 2>&1; echo "warm: $(basename "$f") $((SECONDS - s))s"
done
EXPERTS=$(find /home/user/dev ${STRATA_AI_DIR:-/mnt/user/ai} -maxdepth 2 -name 'Swift-Qwen3.8*IQ3_S*.gguf' 2>/dev/null | head -1)
if [[ -n "$EXPERTS" ]]; then s=$SECONDS; cat "$EXPERTS" > /dev/null 2>&1; echo "warm: $EXPERTS $((SECONDS - s))s"; fi
awk '/^MemAvailable/{print "MemAvailable after warmup:", int($2/1024), "MiB"}' /proc/meminfo
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
