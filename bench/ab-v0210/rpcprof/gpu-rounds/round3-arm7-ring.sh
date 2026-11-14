#!/usr/bin/env bash
# Round 3: ARM7 — STRATA_FS_RING=64/128 sweep against the champion, sha-guarded.
# PREPARED 2026-11-14 — run only after round2.ok (or round2.sha-fail -> do not run).
# Takes prod down per arm via boot-arm.sh, restores at the end.
set -euo pipefail

STRATA=/mnt/srcds/Strata
RPC="$STRATA/bench/ab-v0210/rpcprof"
OUT="$RPC/out/gpu-rounds-20261114"
mkdir -p "$OUT"
[ -f "$OUT/round2.ok" ] || { echo "REFUSE: round2.ok missing (or sha failed)"; exit 1; }
cd "$RPC"

for R in 64 128; do
  echo "== ARM7_R$R: boot champion env + STRATA_FS_RING=$R"
  bash boot-arm.sh ARM7_R$R "STRATA_FS_RING=$R" 2>&1 | tee "$OUT/arm7-r$R-boot.txt" | tail -3
  HLOG=$(ls -t "$STRATA"/logs/trial-*helper.log 2>/dev/null | head -1)
  echo "== panel"
  ./panel-tg.sh ARM7_R$R "$OUT" "$(ls -t "$STRATA"/logs/engine-arm7-r$R-*.log | head -1)" "$HLOG" \
    2>&1 | tee "$OUT/arm7-r$R-panel.txt" | tail -5
done

echo "== verdicts: per-leg fresh-vs-rerun sha (within arm) + tps vs champion (ARM1 records)"
python3 - << 'EOF'
import json, glob, os
out = os.environ.get("OUT", ".")
champ = {}
for f in glob.glob("/mnt/srcds/Strata/bench/ab-v0210/rpcprof/out/ab9-window-20261113/ARM1_*tg*.json"):
    r = json.load(open(f))
    champ[os.path.basename(f).split("-", 1)[1].replace(".json", "")] = r.get("tps") or r.get("tok_s")
for R in (64, 128):
    for f in sorted(glob.glob(f"{out}/ARM7_R{R}-*.json")):
        r = json.load(open(f))
        leg = os.path.basename(f).split("-", 1)[1].replace(".json", "")
        sha = r.get("sha256", "?")[:12]
        print(f"ARM7_R{R} {leg:22} tps={r.get('tps') or r.get('tok_s')} sha={sha} champ={champ.get(leg, 'n/a')}")
EOF
echo "round3 done $(date +%H:%M:%S)" > "$OUT/round3.done"
echo "Promotion rule: ring wins only if warm-180k TG >= champion AND all within-arm sha verdicts OK"
