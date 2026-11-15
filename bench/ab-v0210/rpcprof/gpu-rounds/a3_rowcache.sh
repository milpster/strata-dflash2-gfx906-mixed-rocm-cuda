#!/usr/bin/env bash
# a3_rowcache.sh — A3: --ple-row-cache 1048576 -> 2097152 rows (+90 MB host; generate.cpp:684
# default 1<<20). Boundary re-read insurance on top of whatever PLE mode A1 decided.
# NOTE: run AFTER the A1 verdict is applied to the prod config (make_variant copies prod).
set -euo pipefail
R=/mnt/srcds/Strata
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
bash $GR/make_variant.sh a3rc2m
python3 - << 'PY'
import json
p = "/mnt/srcds/Strata/prod-config-a3rc2m.json"
c = json.load(open(p))
c["args"] += ["--ple-row-cache", "2097152"]
json.dump(c, open(p, "w"), indent=2)
PY
echo "=== A3: boot --ple-row-cache 2097152 ($(date +%T)) ==="
bash $GR/lane_boot.sh $R/launch-a3rc2m.sh a3rc2m
bash $GR/lane_judges.sh A3RC2M 1
bash $GR/lane_restore.sh
echo "=== A3 COMPLETE ($(date +%T)) ==="
