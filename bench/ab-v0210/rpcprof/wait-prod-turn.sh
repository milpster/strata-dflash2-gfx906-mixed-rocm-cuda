#!/usr/bin/env bash
# wait-prod-turn.sh — sleep in bounded slices and come back early if the prod pair restarts.
#
# Unattended runs need a heartbeat that does not rely on notifications from a lane that may
# itself be dying: this script blocks, watches the pair supervisor's generation counter, and
# exits either when the counter moves (a crash happened) or when the window elapses. Its exit
# is the wake-up. It touches nothing: read-only on the supervisor state and the ledgers.
#
# Usage: ./wait-prod-turn.sh [minutes] [poll_seconds]
# Exit 0 = generation changed or window elapsed; the printed summary says which.

set -uo pipefail
ROOT=${STRATA_PROD_TREE:-/mnt/srcds/Strata}
MINUTES=${1:-10}
POLL=${2:-15}
STATE="$ROOT/run/prod-swift15-f16kv-mixed-supervisor.json"
LEDGER="$ROOT/run/prod-crash-ledger.jsonl"

gen_of() {
  python3 - "$STATE" <<'PY' 2>/dev/null
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("generation", ""))
except Exception:
    print("")
PY
}

start_gen=$(gen_of)
deadline=$(( $(date +%s) + MINUTES * 60 ))
changed=no
while (( $(date +%s) < deadline )); do
  sleep "$POLL"
  cur=$(gen_of)
  if [[ -n $cur && "$cur" != "$start_gen" ]]; then changed=yes; break; fi
done

echo "wait-prod-turn: ${MINUTES}min window, generation ${start_gen:-none} -> ${cur:-?} (changed=$changed) at $(date '+%H:%M:%S')"
if ss -H -ltn 'sport = :8009' | grep -q LISTEN; then echo "8009 LISTENING"; else echo "8009 DOWN"; fi
E=$(ps -eo pid,args | grep 'rpcw-hip/strata --serve' | grep -v grep | awk '{print $1}' | head -1)
if [[ -n ${E:-} ]]; then
  echo "engine pid $E uptime $(ps -o etime= -p "$E" | tr -d ' ')"
  tr '\0' ' ' < "/proc/$E/cmdline" | grep -oE -- '--vram-reserve-mib [0-9]+'
fi
if [[ -f $LEDGER ]]; then
  echo "--- last ledger line ---"
  tail -1 "$LEDGER" | python3 -c 'import sys,json;r=json.loads(sys.stdin.read());print(r["at"],r["generation"],"|",r.get("error","")[:120],"|",r.get("failure_class",""))' 2>/dev/null
fi
exit 0
