#!/usr/bin/env bash
# Round-1 env prep for the mlock full-residency test (kernel 7.3.1 + STRATA_ARENA_MLOCK=1).
# Edits the prod config ENV only (round1-residency-boot.sh owns the args surgery):
#   - add STRATA_ARENA_MLOCK=1        (new knob: mlock registered arena spans after cudaHostRegister)
#   - drop STRATA_ARENA_PIN_GIB       (parked 18t capped-pin setting; register-last banners must print)
# Keeps a pre-edit backup beside the round-1 backup so restore-prod.sh stays honest.
set -euo pipefail

STRATA=/mnt/srcds/Strata
CFG="$STRATA/prod-config-swift15-iq3s-int8kv-dflash-w4-mixed.json"
OUT="$STRATA/bench/ab-v0210/rpcprof/out/gpu-rounds-20261114"
mkdir -p "$OUT"

cp "$CFG" "$OUT/config.pre-mlock-env.bak"

python3 - "$CFG" << 'EOF'
import json, sys
p = sys.argv[1]
c = json.load(open(p))
env = c.get("env", {})
env.pop("STRATA_ARENA_PIN_GIB", None)
env["STRATA_ARENA_MLOCK"] = "1"
c["env"] = env
json.dump(c, open(p, "w"), indent=2)
print("env now has MLOCK:", env.get("STRATA_ARENA_MLOCK"), "| PIN_GIB:", env.get("STRATA_ARENA_PIN_GIB", "<removed>"))
EOF

(cd "$STRATA" && git add "$CFG" && git commit -q -m "feat(prod): arm STRATA_ARENA_MLOCK=1, drop parked PIN_GIB for residency test") || echo "(already committed)"
echo "prep done — run: PIN_CAP='' RESERVE_MIB=2304 $STRATA/bench/ab-v0210/rpcprof/gpu-rounds/round1-residency-boot.sh <mlock-binary>"
