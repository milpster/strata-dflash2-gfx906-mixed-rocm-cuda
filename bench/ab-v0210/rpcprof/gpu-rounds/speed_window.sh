#!/usr/bin/env bash
# speed_window.sh — F2 xq-ring speed A/B on v0238 (quality already proven bit-exact,
# gates 18cm). 2x baseline vs 2x ring legs, qqNIGHT-v0238 corpus (same as tonight's
# gates legs for comparability). Extracts per-segment walls + judge-line tok/s.
# Ends with prod restore on launch-v0238.sh (ring OFF; prod-side ring flip is a
# separate post-window step). Usage: speed_window.sh
set -uo pipefail
R=/mnt/srcds/Strata
Q=$R/bench/ab-v0210/quality
OUT=$R/bench/ab-v0210/rpcprof/out/gpu-rounds-20261116
cd /mnt/srcds/Strata-wt-integ
WD="STRATA_WATCHDOG_S=120 STRATA_MEMORY_LIMIT_MIB=51200"

leg () {  # leg <label> <env...> -- ; one shot, gates-proven binary, retry once
  local LABEL=$1; shift
  local -a ENVV=()
  while [ "${1:-}" != "--" ]; do ENVV+=("$1"); shift; done; shift
  local RUNLOG=$OUT/$LABEL-run.log TSVF=$Q/out/$LABEL.tsv TRY
  for TRY in 1 2; do
    bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
    echo "=== SPD $LABEL (try $TRY) ($(date +%T)) ==="
    env "${ENVV[@]}" $WD systemd-run --user --scope -p MemoryMin=48G \
      python3 tools/quant_quality.py run --config $Q/qqNIGHT-v0238.json --out $TSVF > "$RUNLOG" 2>&1
    tr -d '\0' < "$RUNLOG" | grep -a '^engine:' | head -1 | cut -c1-70
    local DEATHS ROWS
    DEATHS=$(tr -d '\0' < "$RUNLOG" | grep -ac "answered None\|timed out" || true)
    ROWS=$([ -f "$TSVF" ] && wc -l < "$TSVF" || echo 0)
    [ "${ROWS:-0}" -gt 100 ] && [ "${DEATHS:-1}" -eq 0 ] && break
    cp -f $Q/out/$LABEL.tsv.stderr.txt $Q/out/$LABEL.try$TRY.stderr.txt 2>/dev/null
    echo "--- $LABEL death class ($ROWS rows, $DEATHS deaths) — $([ $TRY = 1 ] && echo retry || echo GIVING UP)"
  done
  # extract per-segment walls + judge lines
  tr -d '\0' < "$RUNLOG" | grep -a 'positions,' | sed "s/^/SPD $LABEL seg /" | tee -a $OUT/spd-summary.csv
  tr -d '\0' < $Q/out/$LABEL.tsv.stderr.txt 2>/dev/null | grep -a 'tokens =\|generated in' | tail -10 | sed "s/^/SPD $LABEL judge /" | tee -a $OUT/spd-summary.csv
}

echo "=== SPD phase: F2 ring A/B x2+2 ($(date +%T)) ==="
leg SPD-BASE-1 X=1 --
leg SPD-RING-1 STRATA_HELPER_XQ_RING=1 --
leg SPD-BASE-2 X=1 --
leg SPD-RING-2 STRATA_HELPER_XQ_RING=1 --

echo "=== SPD restoring prod on v0238 (ring off for now) ($(date +%T)) ==="
bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
bash $R/launch-v0238.sh > $OUT/spd-prod-restore.log 2>&1 &
echo "=== SPD WINDOW COMPLETE ($(date +%T)) — prod restore running; adjudicate spd-summary.csv ==="
