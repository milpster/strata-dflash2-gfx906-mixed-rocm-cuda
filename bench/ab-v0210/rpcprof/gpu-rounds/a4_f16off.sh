#!/usr/bin/env bash
# a4_f16off.sh — A4: size the STRATA_HIP_PROMPT_F16 speed win (live in prod env; engine-documented
# "not bit-identical"). Boot with F16 OFF, 2 fresh fills + tg leg, restore.
# Read: fills vs the F16-ON band 432-455 tok/s = the win we'd give up by removing the flag.
set -euo pipefail
R=/mnt/srcds/Strata
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
bash $GR/make_variant.sh a4nof16
python3 - << 'PY'
import json
p = "/mnt/srcds/Strata/prod-config-a4nof16.json"
c = json.load(open(p))
c["env"]["STRATA_HIP_PROMPT_F16"] = "0"
json.dump(c, open(p, "w"), indent=2)
PY
echo "=== A4: boot STRATA_HIP_PROMPT_F16=0 ($(date +%T)) ==="
bash $GR/lane_boot.sh $R/launch-a4nof16.sh a4nof16
S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log | grep -v -- '-helper' | head -1)
echo "--- knob check (the '=1:' banner must be ABSENT):"
tr -d '\0' < "$S" | grep -ai 'PROMPT_F16' | head -3 || echo "(no PROMPT_F16 lines - knob off confirmed)"
bash $GR/lane_judges.sh A4NOF16 2
bash $GR/lane_restore.sh
echo "=== A4 COMPLETE ($(date +%T)) ==="
