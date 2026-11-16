#!/usr/bin/env bash
# p0_gates.sh — Phase B quality/validation gates on the NEW engine (v0234p0):
# 1) build-window ctests (CUDA context; expert_cache*, sampler_mapped_parity, penalty_hist_stage)
# 2) fresh knob-off baseline (NIGHT-v0234) on the new binary
# 3) KL gates: F8 (STRATA_PENALTY_MAPPED), F1 (STRATA_SAMPLER_IN_GRAPH),
#    F6 (STRATA_BOOT_FILL_QUEUED, belt-and-braces), MMVF_ROWS x2 (adoption confirmations)
# Gate: KL 0.00000 + PPL identical vs the fresh baseline. One lane_restore at the end.
set -uo pipefail
R=/mnt/srcds/Strata
Q=$R/bench/ab-v0210/quality
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
WT=/mnt/srcds/Strata-wt-integ
OUT=$R/bench/ab-v0210/rpcprof/out/gpu-rounds-20261116
mkdir -p $OUT

# v0234 wrapper + config variant (once)
if [ ! -x $Q/strata-ldp-v0234.sh ]; then
  sed 's#/mnt/srcds/Strata/v0233/build-mixed-gpu/rpcw-hip/strata#/mnt/srcds/Strata/v0234p0/rpcw-hip/strata#' \
    $Q/strata-ldp.sh > $Q/strata-ldp-v0234.sh && chmod +x $Q/strata-ldp-v0234.sh
  python3 - << 'PY'
import json
q="/mnt/srcds/Strata/bench/ab-v0210/quality"
c=json.load(open(q+"/qqNIGHT.json"))
c["args"]=[q+"/strata-ldp-v0234.sh" if a.endswith("strata-ldp.sh") else a for a in c["args"]]
json.dump(c,open(q+"/qqNIGHT-v0234.json","w"),indent=2)
PY
fi

echo "=== P0 ctests ($(date +%T)) ==="
cd $WT/build-mixed-gpu/f1-tests && ctest -R 'expert_cache|sampler_mapped_parity|penalty_hist_stage' --output-on-failure 2>&1 | tail -15 | tee $OUT/p0-ctests.log

cd $WT
run_gate () {  # run_gate <label> <env assignments...>
  local LABEL=$1; shift
  bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
  echo "=== P0 gate run $LABEL ($(date +%T)) ==="
  env "$@" python3 tools/quant_quality.py run --config $Q/qqNIGHT-v0234.json --out $Q/out/$LABEL.tsv 2>&1 | tail -2
}
run_gate NIGHT-v0234
for G in "F8G-PENALTY:STRATA_PENALTY_MAPPED=1" "F1G-SAMPLER:STRATA_SAMPLER_IN_GRAPH=1" \
         "F6G-BOOTFILL:STRATA_BOOT_FILL_QUEUED=1" "MMVF2:STRATA_MMVF_ROWS=1" "MMVF3:STRATA_MMVF_ROWS=1"; do
  L=${G%%:*}; E=${G#*:}
  run_gate "$L" "$E"
  echo "--- NIGHT-v0234 vs $L:"
  python3 tools/quant_quality.py compare $Q/out/NIGHT-v0234.tsv $Q/out/$L.tsv 2>&1 | tail -4
done
bash $GR/lane_restore.sh
echo "=== P0 GATES COMPLETE ($(date +%T)) ==="
