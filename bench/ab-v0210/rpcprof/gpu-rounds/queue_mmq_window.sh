#!/usr/bin/env bash
# queue_mmq_window.sh — wait for the split_arm.sh 30 runner, then:
# stop prod -> MMQ ctest gates + mmq_expert_bench on free GPUs -> mmq_ab.sh OFF arm -> ON arm.
# Launch detached: setsid nohup bash queue_mmq_window.sh > /mnt/srcds/Strata/logs/queue-mmq-window.log 2>&1 & disown
R=/mnt/srcds/Strata
WT=/mnt/srcds/Strata-wt-integ
G=$WT/build-mixed-gpu/rpcw-hip-mmq
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds

while pgrep -f 'split_arm\.sh 30' >/dev/null 2>&1; do sleep 20; done
sleep 5
echo "=== stopping prod for GPU test window ($(date +%H:%M:%S)) ==="
bash $R/stop-all.sh >/dev/null 2>&1
sleep 4
export HSA_OVERRIDE_GFX_VERSION=9.0.6

echo "=== CTEST GATES ==="
if ctest --test-dir $G -N 2>/dev/null | grep -qE 'prefill_mmq_v31_test|mmq_ids_margin'; then
  ctest --test-dir $G -R "prefill_mmq_v31_test|prefill_mmq_scatter_test|prefill_mmq_kquant_test|mmq_ids_margin" --output-on-failure 2>&1 | tail -25
else
  echo "(ctest registry empty - running test binaries directly)"
  for t in prefill_mmq_v31_test prefill_mmq_scatter_test prefill_mmq_kquant_test mmq_ids_margin_test; do
    timeout 900 $G/$t > /tmp/opencode/$t.log 2>&1 && echo "$t PASS" || echo "$t FAIL (log: /tmp/opencode/$t.log)"
  done
fi

echo "=== MMQ_EXPERT_BENCH ($(date +%H:%M:%S)) ==="
timeout 900 $G/mmq_expert_bench 2>&1 | tail -40

echo "=== MMQ AB: OFF ARM ($(date +%H:%M:%S)) ==="
bash $GR/mmq_ab.sh 0 > $R/logs/mmq-ab-off.log 2>&1
tail -40 $R/logs/mmq-ab-off.log

echo "=== MMQ AB: ON ARM ($(date +%H:%M:%S)) ==="
bash $GR/mmq_ab.sh 1 > $R/logs/mmq-ab-on.log 2>&1
tail -40 $R/logs/mmq-ab-on.log

echo "=== MMQ WINDOW COMPLETE ($(date +%H:%M:%S)) ==="
