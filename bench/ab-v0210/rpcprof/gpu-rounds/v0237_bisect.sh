#!/usr/bin/env bash
# v0237_bisect.sh — find which build introduced the first-verify-window stall
# (#267 GPU-side: "timed out at layer 0, cudaStreamQuery 600, device not ready").
# Known good: v0234 (bit-exact twice tonight). Failing: v0237.
# Order: v0236 (adjacent); if it FAILS also test v0235. Plain env — binary is the only variable.
set -uo pipefail
R=/mnt/srcds/Strata
Q=$R/bench/ab-v0210/quality
OUT=$R/bench/ab-v0210/rpcprof/out/gpu-rounds-20261116
cd /mnt/srcds/Strata-wt-integ

mkcfg () {  # mkcfg <variant> -> writes qqNIGHT-<variant>.json with exe=strata-ldp-<variant>.sh
  python3 - "$1" << 'PY'
import json, sys
v = sys.argv[1]
q = "/mnt/srcds/Strata/bench/ab-v0210/quality"
c = json.load(open(q + "/qqNIGHT.json"))
c["exe"] = q + f"/strata-ldp-{v}.sh"
json.dump(c, open(q + f"/qqNIGHT-{v}.json", "w"), indent=2)
PY
}

leg () {  # leg <variant> -> PASS/FAIL; full 5-segment run = PASS, else FAIL
  local V=$1
  mkcfg $V
  bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
  echo "=== BISECT $V ($(date +%T)) ==="
  python3 tools/quant_quality.py run --config $Q/qqNIGHT-$V.json --out $Q/out/BISECT-$V.tsv \
    > $OUT/BISECT-$V-run.log 2>&1
  tr -d '\0' < $OUT/BISECT-$V-run.log | grep -a '^engine:' | head -1 | cut -c1-80
  tr -d '\0' < $OUT/BISECT-$V-run.log | grep -a "positions\|answered" | tail -6
  local DEATHS ROWS
  DEATHS=$(tr -d '\0' < $OUT/BISECT-$V-run.log | grep -ac "answered None\|timed out" || true)
  ROWS=$([ -f $Q/out/BISECT-$V.tsv ] && wc -l < $Q/out/BISECT-$V.tsv || echo 0)
  if [ "${ROWS:-0}" -gt 100 ] && [ "${DEATHS:-1}" -eq 0 ]; then
    echo "=== BISECT $V: PASS (${ROWS} rows) ==="; return 0
  fi
  echo "=== BISECT $V: FAIL (${ROWS} rows, ${DEATHS} death markers) ==="; return 1
}

if leg v0236; then
  echo "=== boundary: v0236 PASS -> stall introduced by the v0236->v0237 delta (recovery commits) ==="
elif leg v0235; then
  echo "=== boundary: v0235 PASS -> stall introduced between v0235 and v0236 ==="
else
  echo "=== boundary: v0235 FAIL -> stall already in v0235 (merged F2/F7ab/instrumentation) ==="
fi
echo "=== BISECT DONE ($(date +%T)) ==="
