#!/usr/bin/env bash
# a1_ple_mmap.sh — A1: --ple-io mmap vs the direct baseline (our boot probe measures ~13.5K rows/s
# = ~54 MB/s, under the 15K slow bar; suspected driver of the fill-wall bimodality + slow boundary
# re-reads). 3 boots x (fresh fill + tg pair); knob check = no "direct measures slow" WARNING.
# Decision after: adopt into prod config (bit-exact per engine parity tests) or not.
set -euo pipefail
R=/mnt/srcds/Strata
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
bash $GR/make_variant.sh a1mmap
python3 - << 'PY'
import json
p = "/mnt/srcds/Strata/prod-config-a1mmap.json"
c = json.load(open(p))
c["args"] += ["--ple-io", "mmap"]
json.dump(c, open(p, "w"), indent=2)
PY
for boot in 1 2 3; do
  echo "=== A1 boot $boot/3: --ple-io mmap ($(date +%T)) ==="
  bash $GR/lane_boot.sh $R/launch-a1mmap.sh a1mmap-$boot
  S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log | grep -v -- '-helper' | head -1)
  echo "--- PLE lines boot $boot (expect NO 'direct measures slow' WARNING, no probe):"
  tr -d '\0' < "$S" | grep -ai 'ple' | grep -aiv 'expert' | head -4
  bash $GR/lane_judges.sh A1MMAP$boot 1
done
bash $GR/lane_restore.sh
echo "=== A1 COMPLETE ($(date +%T)) ==="
