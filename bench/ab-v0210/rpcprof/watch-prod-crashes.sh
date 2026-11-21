#!/usr/bin/env bash
# watch-prod-crashes.sh — attribute every prod pair restart while the operator is away.
#
# The pair supervisor respawns the launcher whenever either peer dies, and it bumps
# `generation` in its state JSON. This watcher polls that counter (cheap, no engine, no
# GPU work, never touches :8009) and, on every bump, writes one JSON line to the ledger
# with the evidence for the death: which request, which error, which card, how much VRAM
# was free and what the pool/helper were doing at that moment.
#
# It changes nothing. The +64 margin step is applied by hand from the ledger, because a
# margin edit is a config change that has to be committed with its attribution.
#
# Usage: ./watch-prod-crashes.sh [interval_seconds] [hours]
# Ledger: run/prod-crash-ledger.jsonl (append-only, one line per detected restart)

set -uo pipefail
ROOT=${STRATA_PROD_TREE:-/mnt/srcds/Strata}
INTERVAL=${1:-20}
HOURS=${2:-12}
STATE="$ROOT/run/prod-swift15-f16kv-mixed-supervisor.json"
LEDGER="$ROOT/run/prod-crash-ledger.jsonl"
TMP="$ROOT/run/.watch-prod-crashes.$$"
trap 'rm -f "$TMP"' EXIT

mkdir -p "$ROOT/run"
gen_of() {
  python3 - "$STATE" <<'PY' 2>/dev/null || echo ""
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
print(d.get("generation", ""))
PY
}

last_gen=$(gen_of)
echo "watch-prod-crashes: start, generation=${last_gen:-none}, interval=${INTERVAL}s, hours=${HOURS}" >&2

end=$(( $(date +%s) + HOURS * 3600 ))
while (( $(date +%s) < end )); do
  sleep "$INTERVAL"
  cur=$(gen_of)
  [[ -n $cur ]] || continue
  [[ "$cur" == "$last_gen" ]] && continue

  # A bump means the previous generation ended. The boot log of the generation that just
  # died is the newest prod log older than the one holding the current generation.
  cur_log=$(python3 - "$STATE" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
print(d.get("log", ""))
PY
)
  dead_log=$(ls -t "$ROOT"/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-*.log 2>/dev/null \
             | grep -v helper | grep -v -F "${cur_log##*/}" | head -1)
  err=""; prompt=""; vram=""; park=""; restore=""; gen_txt=""
  if [[ -n $dead_log && -f $dead_log ]]; then
    err=$(tr -d '\0' < "$dead_log" | grep -m1 -E 'the engine reported an error|refilling a lent slot failed|No buffer space available|the expert source could not produce a blob|SIGABRT|Aborted' | sed 's/^\[strata\] //')
    prompt=$(tr -d '\0' < "$dead_log" | grep -m1 -E 'reading the prompt: [0-9,]+ tokens' | sed 's/^\[strata\] //')
    gen_txt=$(tr -d '\0' < "$dead_log" | grep -m1 -E 'done: [0-9]+ tokens.*error' | sed 's/^\[strata\] //')
  fi
  elog="$ROOT/logs/engine-prod-ud-iq3xxs-v0233.log"
  [[ -f $elog ]] || elog=$(ls -t "$ROOT"/logs/engine-*.log 2>/dev/null | head -1)
  if [[ -n ${elog:-} && -f $elog ]]; then
    vram=$(tr -d '\0' < "$elog" | grep -E 'VRAM free at the start of this request|VRAM free with everything loaded' | tail -2 | tr '\n' '|' | cut -c1-400)
    park=$(tr -d '\0' < "$elog" | grep -E 'conversation cache: (parked|disk park streamed|restored)' | tail -2 | tr '\n' '|' | cut -c1-400)
  fi
  python3 - "$LEDGER" "$last_gen" "$cur" "${dead_log##*/}" "$err" "$prompt" "$gen_txt" "$vram" "$park" <<'PY'
import json, sys, datetime
ledger, old, new, log, err, prompt, gen_txt, vram, park = sys.argv[1:10]
rec = {
    "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    "generation": [old, new],
    "dead_log": log,
    "error": err,
    "failed_request": prompt,
    "request_outcome": gen_txt,
    "vram_tail": vram,
    "cache_tail": park,
}
with open(ledger, "a") as f:
    f.write(json.dumps(rec) + "\n")
print("attributed generation %s->%s: %s" % (old, new, err or "no error line found"), file=sys.stderr)
PY
  last_gen=$cur
done
echo "watch-prod-crashes: done" >&2
