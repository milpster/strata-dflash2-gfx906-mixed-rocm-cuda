#!/usr/bin/env bash
# a1_boot_legs.sh <boot> — ONE A1 boot, self-contained: (re)generate the mmap variant, boot it
# (patched lane_boot), print PLE knob evidence, run the standard judge pair. Per-boot one-shots
# replace the long-lived lane script (the 15:33 incident: a lane bash died mid-wait during model
# load and lane_boot hung in do_wait on the reparented pair supervisor).
set -euo pipefail
R=/mnt/srcds/Strata
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
BOOT=${1:?boot number}
bash $GR/make_variant.sh a1mmap
python3 - << 'PY'
import json
p = "/mnt/srcds/Strata/prod-config-a1mmap.json"
c = json.load(open(p))
c["args"] += ["--ple-io", "mmap"]
json.dump(c, open(p, "w"), indent=2)
PY
echo "=== A1 boot $BOOT: --ple-io mmap ($(date +%T)) ==="
bash $GR/lane_boot.sh $R/launch-a1mmap.sh a1mmap-$BOOT
S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log | grep -v -- '-helper' | head -1)
echo "--- PLE knob lines boot $BOOT (expect NO probe line, NO direct-warning):"
{ tr -d '\0' < "$S" | grep -ai 'ple-io\|PLE direct\|measures slow\|ple table' | head -4; } || echo "(no PLE-mode lines: explicit mmap, no probe)"
bash $GR/lane_judges.sh "A1MMAP$BOOT" 1
echo "=== A1 BOOT $BOOT LEGS COMPLETE ($(date +%T)) ==="
