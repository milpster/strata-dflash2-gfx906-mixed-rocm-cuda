#!/usr/bin/env bash
# v0238_retest.sh — retest window for everything #267 killed, on the FIXED engine
# (v0238p0 @ e262fc7bed: window-reset recovery + recovery-aware watchdog; boots run
# with STRATA_WATCHDOG_S=120 so the ~63 s self-resolving launch stall survives).
# Phases:
#  1) F2 parity prerequisites on GPU: xq_triple_parity + gr_parity --selftest
#  2) qq KL gates: NIGHT-v0238 baseline + F2G x3 (STRATA_HELPER_XQ_RING=1), retry-once
#  3) wikitext continuity: WIKI-234V5 + WIKI-233 (both died to #267 on 13-11-16)
# Ends with prod restored on launch-v0238.sh (fixed binary + champion config).
# Usage: v0238_retest.sh
set -uo pipefail
R=/mnt/srcds/Strata
Q=$R/bench/ab-v0210/quality
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
WT=/mnt/srcds/Strata-wt-integ
F2B=/mnt/srcds/Strata-wt-f2/build-mixed-gpu/f2-tests
OUT=$R/bench/ab-v0210/rpcprof/out/gpu-rounds-20261116
WIKIIDS=$R/data/quant_quality/wikitext2/set.ids.json
mkdir -p $OUT
export LDP="env LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib HSA_OVERRIDE_GFX_VERSION=9.0.6"
WD="STRATA_WATCHDOG_S=120 STRATA_MEMORY_LIMIT_MIB=51200"

# --- qq wrapper + config for v0238 (exe = the engine command; p0_gates lesson)
if [ ! -x $Q/strata-ldp-v0238.sh ]; then
  sed 's#/mnt/srcds/Strata/v0236p0/rpcw-hip/strata#/mnt/srcds/Strata/v0238p0/rpcw-hip/strata#' \
    $Q/strata-ldp-v0236.sh > $Q/strata-ldp-v0238.sh && chmod +x $Q/strata-ldp-v0238.sh
fi
python3 - << 'PY'
import json
q="/mnt/srcds/Strata/bench/ab-v0210/quality"
c=json.load(open(q+"/qqNIGHT.json"))
c["exe"]=q+"/strata-ldp-v0238.sh"
json.dump(c,open(q+"/qqqqNIGHT-v0238.json","w"),indent=2)
PY

echo "=== R7 phase 1: F2 parity binaries on GPU ($(date +%T)) ==="
bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
if [ -x $F2B/xq_triple_parity ]; then
  $LDP $F2B/xq_triple_parity 2>&1 | tail -5 | tee $OUT/r7-xq-triple-parity.log
  echo "xq_triple_parity exit: $?"
else
  echo "xq_triple_parity MISSING at $F2B"
fi
if [ -x $F2B/gr_parity ]; then
  $LDP $F2B/gr_parity --selftest 2>&1 | tail -5 | tee $OUT/r7-gr-parity.log
  echo "gr_parity exit: $?"
else
  echo "gr_parity MISSING at $F2B — build in Strata-wt-f2 first"
fi

cd $WT
run_qq () {  # run_qq <label> <env...> -- [extra harness args]; engine-line guard + retry-once
  local LABEL=$1; shift
  local -a ENVV=()
  while [ "${1:-}" != "--" ]; do ENVV+=("$1"); shift; done; shift
  local RUNLOG=$OUT/$LABEL-run.log TSVF=$Q/out/$LABEL.tsv TRY
  for TRY in 1 2; do
    bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
    echo "=== R7 qq $LABEL (try $TRY) ($(date +%T)) ==="
    env "${ENVV[@]}" $WD systemd-run --user --scope -p MemoryMin=48G \
      python3 tools/quant_quality.py run --config $Q/qqqqNIGHT-v0238.json --out $TSVF "$@" > "$RUNLOG" 2>&1
    tr -d '\0' < "$RUNLOG" | grep -a '^engine:' | head -1 | cut -c1-100
    tr -d '\0' < "$RUNLOG" | tail -2
    if ! tr -d '\0' < "$RUNLOG" | grep -aq "strata-ldp-v0238.sh"; then
      echo "FATAL: $LABEL did not run strata-ldp-v0238.sh — aborting (config exe bug)"
      bash $R/launch-v0238.sh >/dev/null 2>&1 & exit 9
    fi
    DEATHS=$(tr -d '\0' < "$RUNLOG" | grep -ac "answered None\|timed out" || true)
    ROWS=$([ -f "$TSVF" ] && wc -l < "$TSVF" || echo 0)
    [ "${ROWS:-0}" -gt 100 ] && [ "${DEATHS:-1}" -eq 0 ] && break
    cp -f $Q/out/$LABEL.tsv.stderr.txt $Q/out/$LABEL.try$TRY.stderr.txt 2>/dev/null
    echo "--- $LABEL produced no TSV (death class) — $([ $TRY = 1 ] && echo retrying || echo GIVING UP)"
  done
}

echo "=== R7 phase 2: F2 KL gates x3 ($(date +%T)) ==="
run_qq NIGHT-v0238 X=1 --
for P in 1 2 3; do
  run_qq F2G-P$P STRATA_HELPER_XQ_RING=1 --
  echo "--- NIGHT-v0238 vs F2G-P$P:"
  python3 tools/quant_quality.py compare $Q/out/NIGHT-v0238.tsv $Q/out/F2G-P$P.tsv 2>&1 | tail -4
done
echo "--- helper reqs/frame (F2G boots; expect ~37/window, unchanged):"
for P in 1 2 3; do
  [ -s $Q/out/F2G-P$P.tsv.stderr.txt ] && tr -d '\0' < $Q/out/F2G-P$P.tsv.stderr.txt | grep -aoE 'helper=reqs=[0-9]+' | sort | uniq -c | sort -rn | head -3 | sed "s/^/F2G-P$P /"
done | tee $OUT/r7-f2-reqs.log

echo "=== R7 phase 3: wikitext continuity ($(date +%T)) ==="
run_qq WIKI-234V7 X=1 -- --ids $WIKIIDS
python3 tools/quant_quality.py compare $Q/out/WIKI-234P1V.tsv $Q/out/WIKI-234V7.tsv 2>&1 | tail -3
run_qq WIKI-233V7 X=1 -- --ids $WIKIIDS
python3 tools/quant_quality.py compare $Q/out/WIKI-233.tsv $Q/out/WIKI-233V7.tsv 2>&1 | tail -3

echo "=== R7 restoring prod on v0238 (fixed binary + champion config) ($(date +%T)) ==="
bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
bash $R/launch-v0238.sh > $OUT/r7-prod-restore.log 2>&1 &
echo "=== R7 RETEST COMPLETE ($(date +%T)) — prod restore running in background ==="
