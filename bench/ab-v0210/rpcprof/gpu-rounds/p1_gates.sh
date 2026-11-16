#!/usr/bin/env bash
# p1_gates.sh — v0234p1 window (engine = golden build f7cfix @ 9d5cb763f4, F7c units fix):
# 1) assemble v0234p1 variant dir (rpcw-hip from f7cfix + v0233 helper symlink) + qq wrapper/config
# 2) fresh baseline NIGHT-v0234p1
# 3) F8G-P2/P3 (STRATA_PENALTY_MANGED=1), F6G-P2/P3 (STRATA_BOOT_FILL_QUEUED=1) vs fresh baseline
#    -> closes the 3-clean-pass convention for F8 and F6 on the p1 binary
# 4) WIKI-234P1 + WIKI-233: wikitext-2-raw absolute PPL (5x999 positions) on new binary and champion
# 5) F7c boot-line check: pack_read_s/h2d_s must now be nonzero (units fix verification)
# One lane_restore at the end.
set -uo pipefail
R=/mnt/srcds/Strata
Q=$R/bench/ab-v0210/quality
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
WT=/mnt/srcds/Strata-wt-integ
OUT=$R/bench/ab-v0210/rpcprof/out/gpu-rounds-20261116
WIKIIDS=$R/data/quant_quality/wikitext2/set.ids.json
mkdir -p $OUT

# --- v0234p1 assembly (once)
if [ ! -x $R/v0234p1/rpcw-hip/strata ]; then
  [ -x $WT/build-mixed-gpu/f7cfix/strata ] || { echo "FATAL: f7cfix build missing"; exit 9; }
  mkdir -p $R/v0234p1/rpcw-hip
  cp $WT/build-mixed-gpu/f7cfix/strata $R/v0234p1/rpcw-hip/strata
  sha256sum $R/v0234p1/rpcw-hip/strata > $R/v0234p1/rpcw-hip/strata.pin-sha16
  ln -sfn ../v0233/build-mixed-gpu/rpcw-cuda $R/v0234p1/rpcw-cuda
fi

# --- qq wrapper + config for p1 (once)
if [ ! -x $Q/strata-ldp-v0234p1.sh ]; then
  sed 's#/mnt/srcds/Strata/v0234p0/rpcw-hip/strata#/mnt/srcds/Strata/v0234p1/rpcw-hip/strata#' \
    $Q/strata-ldp-v0234.sh > $Q/strata-ldp-v0234p1.sh && chmod +x $Q/strata-ldp-v0234p1.sh
  python3 - << 'PY'
import json
q="/mnt/srcds/Strata/bench/ab-v0210/quality"
c=json.load(open(q+"/qqNIGHT-v0234.json"))
c["args"]=[q+"/strata-ldp-v0234p1.sh" if a.endswith("strata-ldp-v0234.sh") else a for a in c["args"]]
json.dump(c,open(q+"/qqNIGHT-v0234p1.json","w"),indent=2)
PY
fi

cd $WT
run_gate () {  # run_gate <label> <out-tail> <config> [env assignments...] -- extra args appended
  local LABEL=$1 CFG=$2 OUTF=$3; shift 3
  local -a ENVV=()
  while [ "$1" != "--" ]; do ENVV+=("$1"); shift; done; shift
  bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
  echo "=== P1 gate run $LABEL ($(date +%T)) ==="
  env "${ENVV[@]}" python3 tools/quant_quality.py run --config "$CFG" --out $Q/out/$OUTF.tsv "$@" 2>&1 | tail -2
}

CFG=$Q/qqNIGHT-v0234p1.json
run_gate NIGHT-v0234p1 $CFG NIGHT-v0234p1 X=1 -- 
run_gate F8G-P2 $CFG F8G-P2 STRATA_PENALTY_MAPPED=1 --
run_gate F8G-P3 $CFG F8G-P3 STRATA_PENALTY_MAPPED=1 --
run_gate F6G-P2 $CFG F6G-P2 STRATA_BOOT_FILL_QUEUED=1 --
run_gate F6G-P3 $CFG F6G-P3 STRATA_BOOT_FILL_QUEUED=1 --
for L in F8G-P2 F8G-P3 F6G-P2 F6G-P3; do
  echo "--- NIGHT-v0234p1 vs $L:"
  python3 tools/quant_quality.py compare $Q/out/NIGHT-v0234p1.tsv $Q/out/$L.tsv 2>&1 | tail -4
done

# --- wikitext-2 absolute PPL (future-PPL standard): p1 binary + v0233 champion
run_gate WIKI-234P1 $CFG WIKI-234P1 X=1 -- --ids $WIKIIDS
run_gate WIKI-233 $Q/qqNIGHT.json WIKI-233 X=1 -- --ids $WIKIIDS
echo "--- wikitext cross-binary (champion v0233 vs p1):"
python3 tools/quant_quality.py compare $Q/out/WIKI-233.tsv $Q/out/WIKI-234P1.tsv 2>&1 | tail -4

# --- F7c units-fix verification: boot lines must show nonzero pack_read_s / h2d_s
echo "=== F7c boot-line fields (expect nonzero pack_read_s/h2d_s):"
for L in NIGHT-v0234p1 F8G-P2 F8G-P3 F6G-P2 F6G-P3 WIKI-234P1 WIKI-233; do
  F=$(tr -d '\0' < $Q/out/$L.tsv.stderr.txt 2>/dev/null | grep -a 'pack_read_s' | tail -1)
  echo "$L: ${F:-NO-LINE}"
done | tee $OUT/p1-f7c-bootlines.log

bash $GR/lane_restore.sh
echo "=== P1 GATES COMPLETE ($(date +%T)) ==="
