#!/usr/bin/env bash
# a2_gate.sh <SWITCH> [more...] — teacher-forced quality gate for winning A2 arms.
# Runs the qqNIGHT harness with STRATA_<SWITCH>=1 (parent env flows to the harness engine child);
# gate = KL 0.00000 + identical PPL vs NIGHT.tsv (the switches are documented bit-identical, so
# any nonzero KL is a defect and the arm is dropped).
set -euo pipefail
R=/mnt/srcds/Strata
Q=$R/bench/ab-v0210/quality
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
WT=/mnt/srcds/Strata-wt-integ
[ $# -ge 1 ] || { echo "usage: a2_gate.sh <SWITCH>..."; exit 2; }
cd $WT
for SW in "$@"; do
  OUT=$Q/out/A2G-$SW.tsv
  echo "=== A2 GATE $SW ($(date +%T)) ==="
  bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
  env "STRATA_$SW=1" python3 tools/quant_quality.py run --config $Q/qqNIGHT.json --out $OUT 2>&1 | tail -2
  echo "--- NIGHT vs A2G-$SW (gate: KL 0.00000, PPL identical):"
  python3 tools/quant_quality.py compare $Q/out/NIGHT.tsv $OUT 2>&1 | tail -4
done
bash $GR/lane_restore.sh
echo "=== A2 GATE COMPLETE ($(date +%T)) ==="
