#!/usr/bin/env bash
# knob-leg.sh — one stop -> boot -> leg cycle on tuple B with env overrides, classified by the engine's own words.
#
# Why this exists: once the 240k failure was reproduced at a 3,058-token prompt (deterministic, 17 s of
# prefill), each candidate cause can be tested in about two minutes instead of a 20-minute leg. The
# candidates are the configuration keys that the merge made live: the branch engine ignored some env
# keys that main implements (STRATA_GRAPH_FAMILY_TRIM, STRATA_FS_HELPER_SHADOW), so a key that is inert
# on a506e620 can change behaviour on 5a699be1 without any code we wrote.
#
# Usage: knob-leg.sh LABEL TARGET_PROMPT_TOKENS [MAX_TOKENS] [KEY=VALUE ...]
#   The config is generated from prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json (tuple B,
#   5a699be1 + helper 9d16742d) into bench/ab-v0210/rpcprof/gen/<LABEL>.json, so every trial is kept.
#
# Exit status is ctx-threshold.sh's: 0 finite, 2 non-finite logits, 3 no output for another reason.
#
# Teardown discipline (bench/ab-v0210/rpcprof/OPERATOR-DIRECTIVES.md): stop and boot are separate
# invocations inside this script - ./stop-all.sh, then the launcher - and this script's own command line
# carries neither the launcher path nor an engine pattern, so stop-all's sweep cannot match it.
set -uo pipefail
cd "$(dirname "$0")/../../.." || exit 3

LABEL=${1:?usage: knob-leg.sh LABEL TARGET_PROMPT_TOKENS [MAX_TOKENS] [KEY=VALUE ...]}
TARGET=${2:?usage: knob-leg.sh LABEL TARGET_PROMPT_TOKENS [MAX_TOKENS] [KEY=VALUE ...]}
MAXTOK=${3:-8}
if [ "$#" -ge 3 ]; then shift 3; else shift 2; fi

BASE=prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json
GEN=bench/ab-v0210/rpcprof/gen
CFG=$GEN/$LABEL.json
mkdir -p "$GEN"

python3 - "$BASE" "$CFG" "$LABEL" "$@" <<'PY'
import json, pathlib, sys
base, out, label = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.loads(pathlib.Path(base).read_text())
d['env'] = dict(d.get('env', {}))
for kv in sys.argv[4:]:
    k, _, v = kv.partition('='); d['env'][k] = v
d['log'] = f'/mnt/srcds/Strata/logs/engine-{label}.log'
d['trial_note'] = f'knob leg {label}: ' + (' '.join(sys.argv[4:]) or 'no override (baseline)')
pathlib.Path(out).write_text(json.dumps(d, indent=2) + '\n')
print(f'config {out} | overrides: ' + (' '.join(sys.argv[4:]) or 'none'))
PY

ELOG=$(python3 -c "import json;print(json.load(open('$CFG'))['log'])")
EXE=$(python3 -c "import json;print(json.load(open('$CFG'))['exe'])")
EXEDIR=$(dirname "$EXE")
EXPECT=$(sha256sum "$EXE" | cut -c1-16)

echo "=== stop ==="
./stop-all.sh | tail -1

echo "=== boot $LABEL ($(date +%H:%M:%S)) ==="
setsid nohup env STRATA_PROD_CONFIG="$PWD/$CFG" ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh \
  >> "logs/prod-console-$(date +%Y%m%d-%H%M%S).log" 2>&1 < /dev/null &
for i in $(seq 1 45); do
  ss -H -ltn 'sport = :8009' | grep -q LISTEN && { echo "port up after ~$((i*10))s"; break; }
  sleep 10
done

E=$(ps -eo pid,args --no-headers | grep -F "$EXEDIR/" | grep -v grep | awk '{print $1}' | head -1)
GOT=$( [ -n "$E" ] && sha256sum "/proc/$E/exe" | cut -c1-16 )
echo "engine sha=$GOT expected=$EXPECT $([ "$GOT" = "$EXPECT" ] && echo OK || echo MISMATCH)"
if [ "$GOT" != "$EXPECT" ]; then
  echo "RESULT: engine running is not the engine this config names - no leg fired"; exit 3
fi

echo "=== leg ($(date +%H:%M:%S)) ==="
STRATA_ELOG="$ELOG" bench/ab-v0210/rpcprof/ctx-threshold.sh "$TARGET" "$MAXTOK"
RC=$?

echo "=== stop ($(date +%H:%M:%S)) ==="
./stop-all.sh | tail -1
echo "VERDICT label=$LABEL exit=$RC  $(date +%H:%M:%S)"
exit $RC
