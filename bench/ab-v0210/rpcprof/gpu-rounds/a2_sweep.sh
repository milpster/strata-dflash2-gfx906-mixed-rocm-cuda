#!/usr/bin/env bash
# a2_sweep.sh — A2: gfx906 decode-switch sweep. gfx906 ships an EMPTY arch-defaults table
# (arch_defaults.cpp:63) while gfx1151/gfx12 run measured bit-identical decode switches.
# Same-night BASELINE boot first (plain variant copy of prod), then one boot per switch (env-only).
# Switches: Q8_PACKED (+2.3% gfx1151), SH_STREAM (+1.8%), TSUM, LFUSE, PLE_BATCH, MMVF_ROWS.
# Winners -> a2_gate.sh (teacher-forced KL 0.00000 vs NIGHT.tsv; sha is NOT a gate on this box).
set -euo pipefail
R=/mnt/srcds/Strata
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
for ARM in BASE Q8_PACKED SH_STREAM TSUM LFUSE PLE_BATCH MMVF_ROWS; do
  TAG=a2$(echo "$ARM" | tr 'A-Z' 'a-z' | tr -d '_')
  bash $GR/make_variant.sh "$TAG"
  if [ "$ARM" != BASE ]; then
    TAG="$TAG" SW="$ARM" python3 - << 'PY'
import json, os
p = f"/mnt/srcds/Strata/prod-config-{os.environ['TAG']}.json"
c = json.load(open(p))
c["env"]["STRATA_" + os.environ["SW"]] = "1"
json.dump(c, open(p, "w"), indent=2)
PY
  fi
  echo "=== A2 arm $ARM ($(date +%T)) ==="
  bash $GR/lane_boot.sh "$R/launch-$TAG.sh" "$TAG"
  bash $GR/lane_judges.sh "A2$ARM" 1
done
bash $GR/lane_restore.sh
echo "=== A2 SWEEP COMPLETE ($(date +%T)) ==="
