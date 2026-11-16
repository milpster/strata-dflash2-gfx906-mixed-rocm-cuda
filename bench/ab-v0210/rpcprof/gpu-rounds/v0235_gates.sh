#!/usr/bin/env bash
# v0235_gates.sh — engine-gate window on the merged HEAD (F7c fix + #267 instrumentation
# + F7ab SORT/ODIRECT + F2 xq ring). Phases:
#  1) F2 parity prerequisites on GPU: xq_triple_parity + gr_parity --selftest (from the f2 worktree build)
#  2) qq quality gates: NIGHT-v0235 baseline + F2G x3 (STRATA_HELPER_XQ_RING=1); helper reqs/frame check
#  3) F7ab distribution: 8 interleaved boots (CTRL/SORT/ODIRECT/SORT+ODIRECT x2), F7c fields + fill wall
#     per boot; then deciding judge pair (best arm vs CTRL)
#  4) #267 arms: HSA_USERPTR_FOR_PAGED_MEM=0 boot + judge; STRATA_VERIFY_TRACE=1 KL leg
#  5) wikitext continuity: WIKI-234V5 on the new binary
# One lane_restore at the end. Usage: v0235_gates.sh <bin_dir>   (e.g. /mnt/srcds/Strata/v0235p0)
set -uo pipefail
R=/mnt/srcds/Strata
Q=$R/bench/ab-v0210/quality
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
WT=/mnt/srcds/Strata-wt-integ
F2B=/mnt/srcds/Strata-wt-f2/build-mixed-gpu/f2-tests
BIN=${1:?bin dir}
OUT=$R/bench/ab-v0210/rpcprof/out/gpu-rounds-20261116
WIKIIDS=$R/data/quant_quality/wikitext2/set.ids.json
mkdir -p $OUT
export LDP="env LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib HSA_OVERRIDE_GFX_VERSION=9.0.6"

# --- qq wrapper + config for v0235 (once)
if [ ! -x $Q/strata-ldp-v0235.sh ]; then
  sed 's#/mnt/srcds/Strata/v0234p1/rpcw-hip/strata#'"$BIN"'/rpcw-hip/strata#' \
    $Q/strata-ldp-v0234p1.sh > $Q/strata-ldp-v0235.sh && chmod +x $Q/strata-ldp-v0235.sh
  python3 - << 'PY'
import json
q="/mnt/srcds/Strata/bench/ab-v0210/quality"
c=json.load(open(q+"/qqNIGHT-v0234p1.json"))
c["args"]=[q+"/strata-ldp-v0235.sh" if a.endswith("strata-ldp-v0234p1.sh") else a for a in c["args"]]
json.dump(c,open(q+"/qqNIGHT-v0235.json","w"),indent=2)
PY
fi

echo "=== V5 phase 1: F2 parity binaries on GPU ($(date +%T)) ==="
bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
$LDP $F2B/xq_triple_parity 2>&1 | tail -5 | tee $OUT/v5-xq-triple-parity.log
echo "xq_triple_parity exit: $?"
$LDP $F2B/gr_parity --selftest 2>&1 | tail -5 | tee $OUT/v5-gr-parity.log
echo "gr_parity exit: $?"

cd $WT
run_qq () {  # run_qq <label> <env...> -- [extra harness args]
  local LABEL=$1; shift
  local -a ENVV=()
  while [ "$1" != "--" ]; do ENVV+=("$1"); shift; done; shift
  bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
  echo "=== V5 qq $LABEL ($(date +%T)) ==="
  env "${ENVV[@]}" python3 tools/quant_quality.py run --config $Q/qqNIGHT-v0235.json --out $Q/out/$LABEL.tsv "$@" 2>&1 | tail -2
}

echo "=== V5 phase 2: F2 KL gates x3 ($(date +%T)) ==="
run_qq NIGHT-v0235 X=1 --
for P in 1 2 3; do
  run_qq F2G-P$P STRATA_HELPER_XQ_RING=1 --
  echo "--- NIGHT-v0235 vs F2G-P$P:"
  python3 tools/quant_quality.py compare $Q/out/NIGHT-v0235.tsv $Q/out/F2G-P$P.tsv 2>&1 | tail -4
done
echo "--- helper reqs/frame (F2G boots; expect ~37/window, unchanged):"
for P in 1 2 3; do
  tr -d '\0' < $Q/out/F2G-P$P.tsv.stderr.txt | grep -aoE 'helper=reqs=[0-9]+' | sort | uniq -c | sort -rn | head -3 | sed "s/^/F2G-P$P /"
done | tee $OUT/v5-f2-reqs.log

echo "=== V5 phase 3: F7ab distribution ($(date +%T)) ==="
TSV=$OUT/v0235-f7ab.tsv; : > $TSV
echo -e "boot\tarm\tpack_read_s\th2d_s\tpinned_mb\tmbs" >> $TSV
for A in ctrl sort odirect sortodi; do
  bash $GR/make_engine_variant.sh f7ab-$A "$BIN" >/dev/null
  python3 - $A << 'PY'
import json,sys,shlex
p=f"/mnt/srcds/Strata/prod-config-f7ab-{sys.argv[1]}.json"; c=json.load(open(p))
for k,v in [s.split("=",1) for s in shlex.split({"ctrl":"X=1","sort":"STRATA_BOOT_FILL_SORT=1","odirect":"STRATA_BOOT_FILL_ODIRECT=1","sortodi":"STRATA_BOOT_FILL_SORT=1 STRATA_BOOT_FILL_ODIRECT=1"}[sys.argv[1]]) if s!="X=1"]:
    c["env"][k]=v
json.dump(c,open(p,"w"),indent=2)
PY
done
boot_fields () {  # boot_fields <tag> <arm>
  local TAG=$1 ARM=$2
  local ELOG=/mnt/srcds/Strata/logs/engine-prod-ud-iq3xxs-v0233.log
  local OFF0=$(stat -c%s "$ELOG" 2>/dev/null || echo 0)
  echo "=== V5 F7ab boot $ARM $TAG ($(date +%T)) ==="
  bash $GR/lane_boot.sh "$R/launch-f7ab-$ARM.sh" "$TAG" || { echo -e "$TAG\t$ARM\tBOOT-FAIL\t-\t-\t-" >> $TSV; return 1; }
  tail -c +"$OFF0" "$ELOG" | tr -d '\0' > "$OUT/$TAG-elog.txt"
  local F=$(grep -a 'pre-filled .* slots' "$OUT/$TAG-elog.txt" | tail -1 | \
    grep -oE 'pack_read_s [0-9.]+|h2d_s [0-9.]+|pinned_mb [0-9]+|\([0-9.]+ MB/s\)' | tr '\n' ' ')
  local PR=$(echo "$F" | grep -oE 'pack_read_s [0-9.]+' | awk '{print $2}')
  local H2=$(echo "$F" | grep -oE 'h2d_s [0-9.]+' | awk '{print $2}')
  local PM=$(echo "$F" | grep -oE 'pinned_mb [0-9]+' | awk '{print $2}')
  local MB=$(echo "$F" | grep -oE '\([0-9.]+ MB/s\)' | tr -d '( MB/s)')
  echo -e "$TAG\t$ARM\t${PR:-?}\t${H2:-?}\t${PM:-?}\t${MB:-?}" >> $TSV
  echo "  fields: $F"
  grep -a 'STRATA_BOOT_FILL' "$OUT/$TAG-elog.txt" | head -2
}
for i in 1 2; do for A in ctrl sort odirect sortodi; do boot_fields f7ab-$A-$i $A; done; done
echo "=== V5 F7ab deciding judges: sortodi vs ctrl ($(date +%T)) ==="
bash $GR/lane_boot.sh "$R/launch-f7ab-sortodi.sh" f7abfin-s && bash $GR/lane_judges.sh F7ABSORTODI 1
bash $GR/lane_boot.sh "$R/launch-f7ab-ctrl.sh"   f7abfin-c && bash $GR/lane_judges.sh F7ABCTRL 1

echo "=== V5 phase 4: #267 arms ($(date +%T)) ==="
bash $GR/make_engine_variant.sh h267userptr "$BIN" >/dev/null
python3 - << 'PY'
import json
p="/mnt/srcds/Strata/prod-config-h267userptr.json"; c=json.load(open(p))
c["env"]["HSA_USERPTR_FOR_PAGED_MEM"]="0"; json.dump(c,open(p,"w"),indent=2)
PY
bash $GR/lane_boot.sh "$R/launch-h267userptr.sh" h267up-1 && bash $GR/lane_judges.sh H267USERPTR 1
run_qq H267TRACE STRATA_VERIFY_TRACE=1 --
python3 tools/quant_quality.py compare $Q/out/NIGHT-v0235.tsv $Q/out/H267TRACE.tsv 2>&1 | tail -3

echo "=== V5 phase 5: wikitext continuity ($(date +%T)) ==="
run_qq WIKI-234V5 X=1 -- --ids $WIKIIDS
python3 tools/quant_quality.py compare $Q/out/WIKI-234P1.tsv $Q/out/WIKI-234V5.tsv 2>&1 | tail -3

bash $GR/lane_restore.sh
echo "=== V5 GATES COMPLETE ($(date +%T)) ==="
cat $TSV
