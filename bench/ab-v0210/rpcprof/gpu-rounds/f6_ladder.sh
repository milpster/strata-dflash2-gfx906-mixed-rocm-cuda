#!/usr/bin/env bash
# f6_ladder.sh <bin_dir> — F6 engine validation ladder on a new engine build (all P0
# commits): gate-OFF baseline boot + 3 gate-ON boots, per-boot F7c fill fields +
# STRATA_VERIFY_COMPLEMENT all-slots checksum capture (gate-on vs gate-off must be
# EQUAL), then judges (fill80k+tg80k) on fresh F6ON and F6OFF boots (unique arms).
# DoD (plan F6): H2D phase >= 2x on 3 clean gate-ON boots; complements equal;
# verify_slot pass every boot; fill80k leg unchanged.
set -uo pipefail
R=/mnt/srcds/Strata
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
BIN=${1:?bin_dir}
OUT=$R/bench/ab-v0210/rpcprof/out/gpu-rounds-20261116
TSV=$OUT/f6-ladder.tsv
mkdir -p $OUT; : > $TSV
echo -e "boot\tgate\tpack_read_s\th2d_s\tpinned_mb\tmbs\tcomplement_sha" >> $TSV

bash $GR/make_engine_variant.sh f6off "$BIN"
bash $GR/make_engine_variant.sh f6on  "$BIN"
python3 - << 'PY'
import json
p="/mnt/srcds/Strata/prod-config-f6on.json"; c=json.load(open(p))
c["env"]["STRATA_BOOT_FILL_QUEUED"]="1"; json.dump(c,open(p,"w"),indent=2)
PY

boot_capture () {  # boot_capture <launcher> <tag> <gate-label>
  local L=$1 TAG=$2 G=$3
  local ELOG=/mnt/srcds/Strata/logs/engine-prod-ud-iq3xxs-v0233.log  # config "log": append-only, all boots
  local OFF0=$(stat -c%s "$ELOG" 2>/dev/null || echo 0)
  echo "=== F6 boot $G ($TAG) ($(date +%T)) ==="
  bash $GR/lane_boot.sh "$L" "$TAG" || { echo -e "$TAG\t$G\tBOOT-FAIL\t-\t-\t-\t-"; return 1; }
  tail -c +"$OFF0" "$ELOG" | tr -d '\0' > "$OUT/$TAG-elog.txt"
  local F=$(grep -a 'pre-filled .* slots' "$OUT/$TAG-elog.txt" | tail -1 | \
    grep -oE 'pack_read_s [0-9.]+|h2d_s [0-9.]+|pinned_mb [0-9]+|\([0-9.]+ MB/s\)' | tr '\n' ' ')
  local PR H2 PM MB
  PR=$(echo "$F" | grep -oE 'pack_read_s [0-9.]+' | awk '{print $2}')
  H2=$(echo "$F" | grep -oE 'h2d_s [0-9.]+' | awk '{print $2}')
  PM=$(echo "$F" | grep -oE 'pinned_mb [0-9]+' | awk '{print $2}')
  MB=$(echo "$F" | grep -oE '\([0-9.]+ MB/s\)' | tr -d '( MB/s)')
  # complement vector: every slot-hash/checksum-looking line this boot printed
  local CS=none
  grep -aiE 'complement|slot [0-9]+ .*(sha|checksum|hash)|verify.*slot.*(sum|hash)' "$OUT/$TAG-elog.txt" \
    | sort > "$OUT/$TAG-complement.txt"
  [ -s "$OUT/$TAG-complement.txt" ] && CS=$(sha256sum "$OUT/$TAG-complement.txt" | cut -c1-16)
  echo -e "$TAG\t$G\t${PR:-?}\t${H2:-?}\t${PM:-?}\t${MB:-?}\t$CS" >> $TSV
  echo "  $F complement=$CS"
}

# baseline + 3 gate-on boots, complement ON in both configs (variant env add)
for V in f6off f6on; do
  python3 - "$V" << 'PY'
import json,sys
p=f"/mnt/srcds/Strata/prod-config-{sys.argv[1]}.json"; c=json.load(open(p))
c["env"]["STRATA_VERIFY_COMPLEMENT"]="1"; json.dump(c,open(p,"w"),indent=2)
PY
done
boot_capture "$R/launch-f6off.sh" f6off-base  OFF
for i in 1 2 3; do boot_capture "$R/launch-f6on.sh" "f6on-$i" ON; done

echo "=== complement equality (gate-on vs gate-off):"
OFFC=$(awk -F'\t' '$2=="OFF"{print $7}' $TSV)
for i in 1 2 3; do
  ONC=$(awk -F'\t' -v b="f6on-$i" '$1==b{print $7}' $TSV)
  [ "$ONC" = "$OFFC" ] && echo "f6on-$i: EQUAL" || echo "f6on-$i: DIFFER ($ONC vs $OFFC)"
done

echo "=== judges on fresh F6ON + F6OFF ($(date +%T)) ==="
bash $GR/lane_boot.sh "$R/launch-f6on.sh"  f6on-j  && bash $GR/lane_judges.sh F6ON1 1
bash $GR/lane_boot.sh "$R/launch-f6off.sh" f6off-j && bash $GR/lane_judges.sh F6OFF1 1
bash $GR/lane_restore.sh
echo "=== F6 LADDER COMPLETE ($(date +%T)) ==="
cat $TSV
