#!/usr/bin/env bash
# b6_prime.sh — B6 queue-priming A/B on a NEW engine build (needs the B6 code lane:
# gfx906 #if lifted + STRATA_HIP_QUEUE_PRIME). 8 interleaved boots (4 prime-on,
# 4 ctrl), F7c per-phase boot-line fields captured per boot to a TSV; then ONE judge
# pair (fill80k+tg80k) on a fresh prime-on boot and a fresh ctrl boot (unique arms).
# Pass metric: boot h2d phase rate distribution (prime vs ctrl); F7c fields make each
# boot self-attributing (plan F7c §B6 interaction).
set -uo pipefail
R=/mnt/srcds/Strata
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
BIN=$1   # e.g. /mnt/srcds/Strata/v0234p0
OUT=$R/bench/ab-v0210/rpcprof/out/gpu-rounds-20261116
TSV=$OUT/b6-prime.tsv
mkdir -p $OUT; : > $TSV
echo -e "boot\tarm\tpack_read_s\th2d_s\tpinned_mb\tmbs" >> $TSV

bash $GR/make_engine_variant.sh b6prime "$BIN"
python3 - << 'PY'
import json
p="/mnt/srcds/Strata/prod-config-b6prime.json"; c=json.load(open(p))
c["env"]["STRATA_HIP_QUEUE_PRIME"]="1"; json.dump(c,open(p,"w"),indent=2)
PY
bash $GR/make_engine_variant.sh b6ctrl "$BIN"

boot_fields () {  # boot_fields <launcher> <tag> <arm>
  local L=$1 TAG=$2 ARM=$3
  local ELOG=/mnt/srcds/Strata/logs/engine-prod-ud-iq3xxs-v0233.log  # config "log": append-only
  local OFF0=$(stat -c%s "$ELOG" 2>/dev/null || echo 0)
  echo "=== B6 boot $ARM $TAG ($(date +%T)) ==="
  bash $GR/lane_boot.sh "$L" "$TAG" || { echo -e "$TAG\t$ARM\tBOOT-FAIL\t-\t-\t-" >> $TSV; return 1; }
  tail -c +"$OFF0" "$ELOG" | tr -d '\0' > "$OUT/$TAG-elog.txt"
  local F=$(grep -a 'pre-filled .* slots' "$OUT/$TAG-elog.txt" | tail -1 | \
    grep -oE 'pack_read_s [0-9.]+|h2d_s [0-9.]+|pinned_mb [0-9]+|\([0-9.]+ MB/s\)' | tr '\n' ' ')
  local PR=$(echo "$F" | grep -oE 'pack_read_s [0-9.]+' | awk '{print $2}')
  local H2=$(echo "$F" | grep -oE 'h2d_s [0-9.]+' | awk '{print $2}')
  local PM=$(echo "$F" | grep -oE 'pinned_mb [0-9]+' | awk '{print $2}')
  local MB=$(echo "$F" | grep -oE '\([0-9.]+ MB/s\)' | tr -d '( MB/s)')
  echo -e "$TAG\t$ARM\t${PR:-?}\t${H2:-?}\t${PM:-?}\t${MB:-?}" >> $TSV
  echo "  fields: $F"
}

for i in 1 2 3 4; do
  boot_fields "$R/launch-b6ctrl.sh"  b6ctrl-$i  CTRL
  boot_fields "$R/launch-b6prime.sh" b6prime-$i PRIME
done

echo "=== B6 deciding-pair judges ($(date +%T)) ==="
bash $GR/lane_boot.sh "$R/launch-b6prime.sh" b6pfin  && bash $GR/lane_judges.sh B6PRIME 1
bash $GR/lane_boot.sh "$R/launch-b6ctrl.sh"  b6cfin  && bash $GR/lane_judges.sh B6CTRL 1
bash $GR/lane_restore.sh
echo "=== B6 COMPLETE ($(date +%T)) — see $TSV ==="
cat $TSV
