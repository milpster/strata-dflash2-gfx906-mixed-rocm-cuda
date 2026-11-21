#!/usr/bin/env bash
# park-restore-window.sh LABEL — one boot of the champion engine with the FS clamp, one 240k infill that
# generates 1,024 tokens, a disk park, then the same conversation again so the restore is exercised and
# measured. Single request at a time, no concurrency (operator rule).
#
# Usage: DRYRUN=1 knob-leg.sh fsclamp-champion 323200 1024 EXE=run/fsclamp-bin    # writes the config
#        bench/ab-v0210/rpcprof/park-restore-window.sh fsclamp-champion
#
# Teardown goes through stop-all.sh; this script's own command line carries no launcher or engine pattern.
set -uo pipefail
cd "$(dirname "$0")/../../.." || exit 3

LABEL=${1:-fsclamp-champion}
CFG=bench/ab-v0210/rpcprof/gen/$LABEL.json
PY=bench/ab-v0210/rpcprof/longctx-stress.py
BIN=run/fsclamp-bin
[ -f "$CFG" ] || { echo "missing $CFG — generate it with: DRYRUN=1 knob-leg.sh $LABEL 323200 1024 EXE=$BIN"; exit 3; }
ELOG=$(python3 -c "import json;print(json.load(open('$CFG'))['log'])")

echo "=== stop ==="
./stop-all.sh | tail -1

# A cold conversation cache is what makes the park the only source for the second request.
# NOCLEAR=1 keeps the cache instead, so a fresh boot must restore from the disk tier (cross-boot restore).
CONVDIR=$(python3 -c "import json;a=json.load(open('$CFG')).get('args') or [];print(a[a.index('--conversation-cache-dir')+1] if '--conversation-cache-dir' in a else '')")
case "$CONVDIR" in
  */run/*) if [[ -n ${NOCLEAR:-} ]]; then
            echo "keeping the conversation cache for a cross-boot restore: $CONVDIR ($(ls "$CONVDIR" 2>/dev/null | wc -l) files)"
          else
            echo "clearing the conversation cache: $CONVDIR"; rm -rf "$CONVDIR"; mkdir -p "$CONVDIR"
          fi ;;
  *) echo "no conversation cache dir in the config — nothing cleared"; exit 3 ;;
esac

echo "=== boot ($(date +%H:%M:%S)) ==="
setsid nohup env STRATA_PROD_BIN="$PWD/$BIN" STRATA_PROD_CONFIG="$PWD/$CFG" \
  ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh >> "logs/prod-console-$(date +%Y%m%d-%H%M%S).log" 2>&1 < /dev/null &
for i in $(seq 1 45); do
  ss -H -ltn 'sport = :8009' | grep -q LISTEN && { echo "port up after ~$((i*10))s"; break; }
  sleep 10
done
sleep 5

echo "=== request 1: infill 240k + 1024 generated ($(date +%H:%M:%S)) ==="
"$PY" --prompt-tokens 323200 --max-tokens 1024 --seed 7 --timeout 5400 2>&1 | tail -4

if [[ -n ${ONE:-} ]]; then
  echo "=== ONE=1: skipping the second same-boot request ($(date +%H:%M:%S)) ==="
else
echo "=== park check (idle 90 s) ==="
sleep 90
tr -d '\0' < "$ELOG" | grep -a "disk park\|parked" | tail -2 | cut -c1-170

echo "=== request 2: the same conversation, expecting a restore ($(date +%H:%M:%S)) ==="
"$PY" --prompt-tokens 323200 --max-tokens 1024 --seed 7 --timeout 5400 2>&1 | tail -4
fi

echo "=== engine's own lines ==="
tr -d '\0' < "$ELOG" | grep -a "prompt .* tokens =\|done:\|disk park\|restore" | tail -8 | cut -c1-180
echo "=== clamp and margins ==="
tr -d '\0' < "$ELOG" | grep -a "foresight swap\|CUDA1: .*VRAM free with" | cut -c1-180
echo "=== stop ($(date +%H:%M:%S)) ==="
./stop-all.sh | tail -1
echo "WINDOW label=$LABEL end=$(date +%H:%M:%S)"
