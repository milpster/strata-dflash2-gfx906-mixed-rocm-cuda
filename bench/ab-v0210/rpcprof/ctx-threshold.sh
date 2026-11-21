#!/usr/bin/env bash
# ctx-threshold.sh — one leg against the running prod engine, classified by outcome.
#
# Why this exists: the 240k leg produced non-finite logits twice in a row on 2026-11-21 and we then
# needed to know WHERE along the context length that starts. One leg per invocation, one summary
# line, and the engine's own words kept next to it.
#
# Usage: ctx-threshold.sh TARGET_PROMPT_TOKENS [MAX_TOKENS] [SEED]
#   TARGET_PROMPT_TOKENS is longctx-stress's --prompt-tokens (target text length, not the token
#   count the engine reports; observed ratio ≈ 0.742, i.e. 323200 → 239,677).
#   MAX_TOKENS defaults to 8: this harness asks "does the first decode step produce finite logits",
#   not "how fast does it generate", so the prefill is the whole cost.
#
# Exit status: 0 = finite output produced, 2 = non-finite logits, 3 = no output for another reason.
#
# Reads the engine's log, never writes to it; never touches processes. Stop/boot stay in stop-all.sh
# and the launcher (bench/ab-v0210/rpcprof/OPERATOR-DIRECTIVES.md).
set -uo pipefail
cd "$(dirname "$0")/../../.." || exit 3

TARGET=${1:?usage: ctx-threshold.sh TARGET_PROMPT_TOKENS [MAX_TOKENS] [SEED]}
MAXTOK=${2:-8}
SEED=${3:-7}
PY=bench/ab-v0210/rpcprof/longctx-stress.py
ELOG=${STRATA_ELOG:-logs/engine-prod-ud-iq3xxs-v0233.log}   # override when the boot logs elsewhere
OUT=/tmp/opencode/ctx-threshold-last.txt

[[ -f $PY ]] || { echo "missing $PY" >&2; exit 3; }
[[ -f $ELOG ]] || { echo "missing $ELOG" >&2; exit 3; }
ss -H -ltn 'sport = :8009' | grep -q LISTEN || { echo "nothing is serving on :8009" >&2; exit 3; }

S=$(tr -d '\0' < "$ELOG" | grep -n "engine started:" | tail -1 | cut -d: -f1)
S=${S:-1}   # a console log with no 'engine started:' line still classifies from its first line
START=$(date +%s)
echo "=== leg: target $TARGET tokens, max_tokens $MAXTOK, seed $SEED, elog slice from line $S @ $(date +%H:%M:%S)"
"$PY" --prompt-tokens "$TARGET" --max-tokens "$MAXTOK" --seed "$SEED" --timeout 5400 \
  --ask "Read the whole text above, then state in one sentence what it is about."
RC=$?
WALL=$(( $(date +%s) - START ))

tr -d '\0' < "$ELOG" | tail -n +"$S" > "$OUT"
READ=$(grep -a "prompt [0-9]* tokens =" "$OUT" | tail -1)
NONFINITE=$(grep -ac "non-finite logits" "$OUT")
DONE=$(grep -a "done:" "$OUT" | tail -1)

echo "--- harness exit $RC after ${WALL}s"
[[ -n $READ ]] && echo "--- $READ" | cut -c1-220
[[ -n $DONE ]] && echo "--- $DONE" | cut -c1-200
echo "--- non-finite logits lines in this slice: $NONFINITE"

if [[ $NONFINITE -gt 0 ]]; then
  echo "RESULT: NON-FINITE at this context (request refused after the second attempt)"
  exit 2
elif [[ -n $DONE ]]; then
  echo "RESULT: finite output produced"
  exit 0
else
  echo "RESULT: no output for another reason (see $OUT)"
  exit 3
fi
