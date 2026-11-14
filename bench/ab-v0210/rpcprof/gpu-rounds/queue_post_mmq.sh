#!/usr/bin/env bash
# queue_post_mmq.sh — wait for the mmq_ab.sh ON-arm runner, then:
# stop prod -> env-fixed ctest device gates + mmq_expert_bench -> prod back up.
# Launch detached: setsid nohup bash queue_post_mmq.sh > /mnt/srcds/Strata/logs/queue-post-mmq.log 2>&1 & disown
R=/mnt/srcds/Strata
WT=/mnt/srcds/Strata-wt-integ
G=$WT/build-mixed-gpu/rpcw-hip-mmq

while pgrep -f 'mmq_ab\.sh 1' >/dev/null 2>&1; do sleep 20; done
sleep 5
echo "=== stopping prod for env-fixed test re-run ($(date +%H:%M:%S)) ==="
bash $R/stop-all.sh >/dev/null 2>&1
sleep 4

# clean visibility env, full prod-style HSA override set
unset CUDA_VISIBLE_DEVICES HIP_VISIBLE_DEVICES ROCR_VISIBLE_DEVICES AMD_VISIBLE_DEVICES GPU_DEVICE_ORDINAL
export HSA_OVERRIDE_GFX_VERSION=9.0.6
echo "--- visibility env: $(env | grep -iE 'visible|override' | tr '\n' ' ')"
echo "=== CTEST DEVICE GATES (env-fixed) ==="
for t in prefill_mmq_scatter_test prefill_mmq_kquant_test prefill_mmq_v31_test; do
  timeout 900 $G/$t > /tmp/opencode/$t.log 2>&1 && echo "$t PASS" || { echo "$t FAIL:"; tail -5 /tmp/opencode/$t.log; }
done
echo "=== MMQ_EXPERT_BENCH (env-fixed, $(date +%H:%M:%S)) ==="
timeout 900 $G/mmq_expert_bench 2>&1 | tail -40

echo "=== prod back up ($(date +%H:%M:%S)) ==="
cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-ud-xxs-post-mmq-tests.log 2>&1 < /dev/null &
sleep 10
RD=""
for i in $(seq 1 60); do
  S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log | grep -v -- '-helper' | head -1)
  RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
  TS=$(stat -c %Y "$S" 2>/dev/null)
  [ -n "$RD" ] && [ $(( $(date +%s) - TS )) -lt 150 ] && break
  sleep 10
done
echo "READY: ${RD:-TIMEOUT}"
echo "=== POST-MMQ QUEUE COMPLETE ($(date +%H:%M:%S)) ==="
