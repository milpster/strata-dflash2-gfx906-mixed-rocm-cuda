#!/usr/bin/env bash
# bisect_bootloop.sh [N=6] — reboot-loop the champion config N times, scoring
# each boot's judge pair (fresh fill / boundary / warm TG + warm sha16).
# The champion boots through a byte-identical variant launcher (make_variant
# champion) so no live cmdline ever carries the prod launcher path (stop-all
# pattern sweep). One line per boot is appended live to bisect-summary.tsv;
# per-boot boot-phase state stays in the elog (offsets recorded in arm jsons).
# No perf, no subagents: the box stays quiet for clean TG (18be lesson).
# Ends with the canonical restore.
set -euo pipefail
R=/mnt/srcds/Strata
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
OUT=$R/bench/ab-v0210/rpcprof/out/gpu-rounds-20261115
N=${1:-6}
mkdir -p "$OUT"
SUM=$OUT/bisect-summary.tsv
bash $GR/make_variant.sh champion >/dev/null
[ -f "$SUM" ] || printf 'boot\tengine_sha8\tfill_tok_s\tboundary_tok_s\twarm_tok_s\twarm_sha16\ttg_walls_s\n' > "$SUM"
for i in $(seq 1 "$N"); do
  ARM=BISECT$i
  echo "=== BISECT $i/$N: boot $(date +%T) ==="
  if ! bash $GR/lane_boot.sh "$R/launch-champion.sh" "bisect-$i"; then
    printf '%s\tBOOT-FAIL\t-\t-\t-\t-\t-\n' "$i" | tee -a "$SUM"
    continue
  fi
  E=$(jq -r .engine_pid "$R/run/prod-swift15-f16kv-mixed-supervisor.json")
  SHA8=$(sha256sum "/proc/$E/exe" | cut -c1-8)
  if ! bash $GR/lane_judges.sh "$ARM" 1 > "$OUT/$ARM-judges.log" 2>&1; then
    printf '%s\t%s\tJUDGES-FAIL\t-\t-\t-\t-\n' "$i" "$SHA8" | tee -a "$SUM"
    continue
  fi
  python3 - "$OUT/$ARM-judges.log" "$OUT/$ARM-tg80k.json/$ARM-tg80k.json" "$SUM" "$i" "$SHA8" <<'PY'
import json, re, sys
judges, tgjson, summary, boot, sha8 = sys.argv[1:6]
lines = [l for l in open(judges, errors="replace").read().splitlines() if "tokens = " in l]
read = re.compile(r"read in ([\d.]+) ms \(([\d.]+) tok/s\)")
gen = re.compile(r"(\d+) generated in ([\d.]+) ms \(([\d.]+) tok/s\)")
fill = boundary = warm = None
for l in lines[-4:]:
    if "0 reused" in l:
        m = read.search(l);  fill = m.group(2) if m else "-"
    elif "reused + 14" in l:
        m = read.search(l);  boundary = m.group(2) if m else "-"
    elif " + 5 read" in l:
        m = gen.search(l);   warm = m.group(3) if m else "-"
j = json.load(open(tgjson))
row = f'{boot}\t{sha8}\t{fill or "-"}\t{boundary or "-"}\t{warm or "-"}\t{j["sha256"][:16]}\t{"/".join(str(w) for w in j["walls"])}'
open(summary, "a").write(row + "\n")
print("BISect row: " + row)
PY
done
echo "=== BISECT LOOP COMPLETE — restoring canonical prod $(date +%T) ==="
bash $GR/lane_restore.sh
echo "=== BISECT DONE $(date +%T) ==="
