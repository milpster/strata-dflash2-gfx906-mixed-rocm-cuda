#!/usr/bin/env bash
# prod_judge.sh — send N chat requests to prod (:8009), then extract the engine's
# per-request "tokens =" lines from the newest prod log. Appends label,read_tok_s,gen_tok_s,drafts
# to spd-prod-judge.csv. Usage: prod_judge.sh <label> [n_requests]
set -uo pipefail
R=/mnt/srcds/Strata
LABEL=${1:?label}; N=${2:-6}
OUT=$R/bench/ab-v0210/rpcprof/out/gpu-rounds-20261116
LOG=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-*.log 2>/dev/null | head -1)
[ -n "$LOG" ] || { echo "no prod log found"; exit 1; }
BASE=$(grep -ac 'tokens =' "$LOG" 2>/dev/null || echo 0)

PROMPT="Summarize the following. $(head -c 2200 /mnt/srcds/Strata/bench/ab-v0210/rpcprof/gpu-rounds/diag267_drive.py 2>/dev/null || echo 'The quick brown fox jumps over the lazy dog. ') Also list three risks."
for i in $(seq 1 "$N"); do
  curl -s -m 120 http://127.0.0.1:8009/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"ud-iq3_xxs\",\"messages\":[{\"role\":\"user\",\"content\":$(python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "$PROMPT")}],\"max_tokens\":500}" \
    -o /dev/null
  sleep 1
done

sleep 2
tr -d '\0' < "$LOG" | grep -a '\[strata\] done:' | tail -"$N" | python3 -c "
import sys, re, csv
label = '$LABEL'
w = csv.writer(open('$OUT/spd-prod-judge.csv', 'a'), lineterminator='\n')
for line in sys.stdin:
    m = re.search(r'done: (\d+) tokens in ([\d.]+) s \(([\d.]+) tok/s\).*?expert cache ([\d.]+)% hit', line)
    if m:
        w.writerow([label, m.group(1), m.group(3), m.group(4)])
        print(f'{label}: {m.group(1)} tokens, decode {m.group(3)} tok/s, expert cache {m.group(4)}% hit')
"
