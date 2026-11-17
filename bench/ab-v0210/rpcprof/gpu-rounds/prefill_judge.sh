#!/usr/bin/env bash
# prefill_judge.sh <label> [n] — varied-prompt prefill A/B against prod (:8009).
# Sends n requests whose prompts are unique ~10k-char wikitext-derived slices
# (fresh expert routing every request — exercises the file-tier fetch path),
# max_tokens 60 (decode kept minimal; the prompt phase is the metric).
# Parses the engine stderr log's last-n `serve: prompt` lines into
# prefill-judge.csv: label,total,reused,read,prompt_ms,read_toks,pcie_fetch_ms
# Compare arms on medians of read_toks (tokens/s read) and pcie_fetch_ms.
set -u
LABEL=${1:?label}; N=${2:-8}
SRC=${SRC:-/mnt/srcds/Strata/bench/ab-v0210/quality/out/WIKI-233V7.tsv}
ELOG=/mnt/srcds/Strata/logs/engine-prod-ud-iq3xxs-v0233.log
OUT=/mnt/srcds/Strata/bench/ab-v0210/rpcprof/out/gpu-rounds-20261116
[ -s "$SRC" ] || { echo "prompt source missing: $SRC"; exit 1; }
PJ=/tmp/opencode/prefill-judge-$$.json
for i in $(seq 1 "$N"); do
  SEED=$((RANDOM * 32768 + i))
  python3 - "$SRC" "$SEED" > "$PJ" <<'PY'
import json, random, sys
text = open(sys.argv[1], encoding="utf-8", errors="ignore").read()
rng = random.Random(int(sys.argv[2]))
start = rng.randrange(0, max(1, len(text) - 12000))
slice_ = " ".join(text[start:start + 11000].split())
print(json.dumps({"model": "ud-iq3_xxs", "messages": [{"role": "user", "content": "Continue this text exactly as given, then stop: " + slice_}], "max_tokens": 60}))
PY
  curl -s -m 400 http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' -d @"$PJ" -o /dev/null
  sleep 1
done
rm -f "$PJ"
sleep 2
tr -d '\0' < "$ELOG" | grep -a 'serve: prompt' | tail -"$((N * 2))" | python3 -c "
import sys, re, csv
label = '$LABEL'
w = csv.writer(open('$OUT/prefill-judge.csv', 'a'), lineterminator='\n')
for line in sys.stdin:
    m = re.search(r'prompt (\d+) tokens = (\d+) reused \+ (\d+) read in (\d+) ms \(([\d.]+) tok/s\)', line)
    f = re.search(r'pcie_fetch_ms ([\d.]+)', line)
    if m and f and int(m.group(3)) >= 1000:
        row = [label, m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), f.group(1)]
        w.writerow(row)
        print(f'{label}: read {m.group(3)} tok in {m.group(4)} ms ({m.group(5)} tok/s), pcie_fetch {f.group(1)} ms')
"
