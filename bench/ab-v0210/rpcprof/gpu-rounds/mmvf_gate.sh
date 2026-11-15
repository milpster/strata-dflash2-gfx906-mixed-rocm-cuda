#!/usr/bin/env bash
# mmvf_gate.sh — MMVF_ROWS adoption gate with a FRESH knob-off baseline.
# NIGHT.tsv (2026-11-14) predates WT HEAD 84fc595dd9 (2026-11-15), so the stored
# baseline cannot be proven current: run baseline + knob back-to-back on the same
# harness engine tonight and compare those two. Gate: KL 0.00000 + identical PPL.
# Original NIGHT.tsv left untouched as the record baseline. Ends with lane_restore.
set -euo pipefail
R=/mnt/srcds/Strata
Q=$R/bench/ab-v0210/quality
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
WT=/mnt/srcds/Strata-wt-integ
cd $WT
bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
echo "=== MMVF GATE baseline run ($(date +%T)) ==="
python3 tools/quant_quality.py run --config $Q/qqNIGHT.json --out $Q/out/NIGHT-fresh.tsv 2>&1 | tail -2
echo "=== MMVF GATE knob run STRATA_MMVF_ROWS=1 ($(date +%T)) ==="
bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
env STRATA_MMVF_ROWS=1 python3 tools/quant_quality.py run --config $Q/qqNIGHT.json --out $Q/out/A2G-MMVF_ROWS.tsv 2>&1 | tail -2
echo "--- fresh baseline vs knob (gate: KL 0.00000, PPL identical):"
python3 tools/quant_quality.py compare $Q/out/NIGHT-fresh.tsv $Q/out/A2G-MMVF_ROWS.tsv 2>&1 | tail -4
bash $GR/lane_restore.sh
echo "=== MMVF GATE COMPLETE ($(date +%T)) ==="
