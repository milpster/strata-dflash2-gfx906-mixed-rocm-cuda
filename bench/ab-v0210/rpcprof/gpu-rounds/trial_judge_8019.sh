#!/usr/bin/env bash
# Trial probes on :8019 (arena-resize trial). Same shape as prod_judge.sh but
# PORT=8019 and the newest trial-arena24 engine log. Appends to its own csv.
set -uo pipefail
R=/mnt/srcds/Strata
G=$R/bench/ab-v0210/rpcprof/gpu-rounds
LABEL=${1:?label}; N=${2:-6}; PORT=${PORT:-8019}
OUT=$R/bench/ab-v0210/rpcprof/out/arena-resize-trial-20261120
mkdir -p "$OUT"
LOG=$(ls -t $R/logs/trial-arena24-mixed-*.log 2>/dev/null | grep -v -- '-helper' | head -1)
[ -n "$LOG" ] || { echo "no trial log found"; exit 1; }
BASE=$(tr -d '\0' < "$LOG" | grep -ac 'tokens =' || echo 0)
PROMPT="Summarize the following. $(head -c 2200 $G/diag267_drive.py 2>/dev/null || echo 'The quick brown fox jumps over the lazy dog. ') Also list three risks."
for i in $(seq 1 "$N"); do
  curl -s -m 120 "http://127.0.0.1:$PORT/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"ud-iq3_xxs\",\"messages\":[{\"role\":\"user\",\"content\":$(python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "$PROMPT")}],\"max_tokens\":500}" \
    -o /dev/null
  sleep 1
done
sleep 2
tr -d '\0' < "$LOG" | grep -a '\[strata\] done:' | tail -"$N" | python3 -c "
import sys, re, csv
label = '$LABEL'
w = csv.writer(open('$OUT/probes.csv', 'a'), lineterminator='\n')
for line in sys.stdin:
    m = re.search(r'done: (\d+) tokens in ([\d.]+) s \(([\d.]+) tok/s\).*?expert cache ([\d.]+)% hit', line)
    if m:
        w.writerow([label, m.group(1), m.group(3), m.group(4)])
        print(f'{label}: {m.group(1)} tokens, decode {m.group(3)} tok/s, expert cache {m.group(4)}% hit')
"
