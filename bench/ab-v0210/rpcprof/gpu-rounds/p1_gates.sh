#!/usr/bin/env bash
# p1_gates.sh — REAL v0234p1 quality window. FIX over the first attempt (and over p0_gates.sh):
# the harness execs cfg["exe"] — the earlier "variant" configs edited args (a no-op), so all
# "v0234" qq runs silently used the v0233 wrapper. Here: exe is rewritten, and every run's
# "engine:" stderr line is GUARD-CHECKED for the expected wrapper before its result is used.
# Runs: NIGHT-P1V baseline, F8G x3, F6G x3, F1G x2 (STRATA_SAMPLER_IN_GRAPH), MMVF x1
# (belt-and-braces; v0233-side 3/3 stands), WIKI-234P1V + WIKI-233 (wikitext-2-raw absolute PPL),
# F7c boot-line field check (nonzero pack_read_s/h2d_s). One lane_restore at the end.
set -uo pipefail
R=/mnt/srcds/Strata
Q=$R/bench/ab-v0210/quality
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
WT=/mnt/srcds/Strata-wt-integ
OUT=$R/bench/ab-v0210/rpcprof/out/gpu-rounds-20261116
WIKIIDS=$R/data/quant_quality/wikitext2/set.ids.json
EXPECT="strata-ldp-v0234p1.sh"
mkdir -p $OUT

# --- engine variant assembly (once)
if [ ! -x $R/v0234p1/rpcw-hip/strata ]; then
  [ -x $WT/build-mixed-gpu/f7cfix/strata ] || { echo "FATAL: f7cfix build missing"; exit 9; }
  mkdir -p $R/v0234p1/rpcw-hip
  cp $WT/build-mixed-gpu/f7cfix/strata $R/v0234p1/rpcw-hip/strata
  sha256sum $R/v0234p1/rpcw-hip/strata > $R/v0234p1/rpcw-hip/strata.pin-sha16
  ln -sfn ../v0233/build-mixed-gpu/rpcw-cuda $R/v0234p1/rpcw-cuda
fi
# --- wrapper + config with exe FIXED to the p1 wrapper (once)
if [ ! -x $Q/strata-ldp-v0234p1.sh ]; then
  sed 's#/mnt/srcds/Strata/v0234p0/rpcw-hip/strata#/mnt/srcds/Strata/v0234p1/rpcw-hip/strata#' \
    $Q/strata-ldp-v0234.sh > $Q/strata-ldp-v0234p1.sh && chmod +x $Q/strata-ldp-v0234p1.sh
fi
python3 - << 'PY'
import json
q="/mnt/srcds/Strata/bench/ab-v0210/quality"
c=json.load(open(q+"/qqNIGHT.json"))
c["exe"]=q+"/strata-ldp-v0234p1.sh"          # THE FIX: exe is the engine command
json.dump(c,open(q+"/qqNIGHT-v0234p1.json","w"),indent=2)
PY
# fix the older v0234 config too (was silently v0233)
python3 - << 'PY'
import json
q="/mnt/srcds/Strata/bench/ab-v0210/quality"
c=json.load(open(q+"/qqNIGHT-v0234.json")); c["exe"]=q+"/strata-ldp-v0234.sh"
json.dump(c,open(q+"/qqNIGHT-v0234.json","w"),indent=2)
PY

cd $WT
CONFIG=$Q/qqNIGHT-v0234p1.json
run_gate () {  # run_gate <label> <env...> -- [extra harness args]; uses $CONFIG, guards on $EXPECT
  local LABEL=$1; shift
  local -a ENVV=()
  while [ "${1:-}" != "--" ]; do ENVV+=("$1"); shift; done; shift
  bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
  echo "=== P1V gate run $LABEL ($(date +%T)) ==="
  local RUNLOG=$OUT/$LABEL-run.log
  env "${ENVV[@]}" python3 tools/quant_quality.py run --config "$CONFIG" --out $Q/out/$LABEL.tsv "$@" > "$RUNLOG" 2>&1
  tr -d '\0' < "$RUNLOG" | grep -a '^engine:' | head -1 | cut -c1-100
  tr -d '\0' < "$RUNLOG" | tail -2
  # ENGINE GUARD: the harness's own "engine:" stdout line must name the expected wrapper
  if ! tr -d '\0' < "$RUNLOG" | grep -aq "$EXPECT"; then
    echo "FATAL: $LABEL did not run $EXPECT — aborting window (config exe bug)"; bash $GR/lane_restore.sh; exit 9
  fi
}

CFG=$Q/qqNIGHT-v0234p1.json
run_gate NIGHT-P1V X=1 --
for G in "F8G-P1V-1:STRATA_PENALTY_MAPPED=1" "F8G-P1V-2:STRATA_PENALTY_MAPPED=1" "F8G-P1V-3:STRATA_PENALTY_MAPPED=1" \
         "F6G-P1V-1:STRATA_BOOT_FILL_QUEUED=1" "F6G-P1V-2:STRATA_BOOT_FILL_QUEUED=1" "F6G-P1V-3:STRATA_BOOT_FILL_QUEUED=1" \
         "F1G-P1V-1:STRATA_SAMPLER_IN_GRAPH=1" "F1G-P1V-2:STRATA_SAMPLER_IN_GRAPH=1" \
         "MMVF-P1V-1:STRATA_MMVF_ROWS=1"; do
  L=${G%%:*}; E=${G#*:}
  run_gate "$L" "$E" --
  echo "--- NIGHT-P1V vs $L:"
  python3 tools/quant_quality.py compare $Q/out/NIGHT-P1V.tsv $Q/out/$L.tsv 2>&1 | tail -4
done

# --- wikitext-2 absolute PPL: p1 binary + v0233 champion (qqNIGHT.json exe = strata-ldp.sh = v0233, correct there)
run_gate WIKI-234P1V X=1 -- --ids $WIKIIDS
CONFIG=$Q/qqNIGHT.json; EXPECT="strata-ldp.sh"
run_gate WIKI-233 X=1 -- --ids $WIKIIDS
echo "--- wikitext cross-binary (champion v0233 vs p1):"
python3 tools/quant_quality.py compare $Q/out/WIKI-233.tsv $Q/out/WIKI-234P1V.tsv 2>&1 | tail -4

echo "=== F7c boot-line fields (expect nonzero pack_read_s/h2d_s):"
for L in NIGHT-P1V F8G-P1V-1 F6G-P1V-1 WIKI-234P1V; do
  F=$(tr -d '\0' < $Q/out/$L.tsv.stderr.txt 2>/dev/null | grep -a 'pack_read_s' | tail -1)
  echo "$L: ${F:-NO-LINE}"
done | tee $OUT/p1v-f7c-bootlines.log

bash $GR/lane_restore.sh
echo "=== P1V GATES COMPLETE ($(date +%T)) ==="
