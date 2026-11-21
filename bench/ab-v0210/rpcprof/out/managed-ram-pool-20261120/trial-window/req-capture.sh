#!/usr/bin/env bash
# Reusable request capture for the managed RAM-pool trial legs.
# usage: req-capture.sh <evdir> <tag> [engine_pid]
# writes <tag>-*.{json,meta} + mem snapshot before/after each request.
set -u
E=${1:?evdir}
TAG=${2:?tag}
EP=${3:-}
PORT=8019
BASE=http://127.0.0.1:$PORT

snap() { # snap <label>
  local lbl=$1 avail swap_tot swap_free sw_used lck rss
  avail=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
  swap_tot=$(awk '/^SwapTotal:/{print $2}' /proc/meminfo)
  swap_free=$(awk '/^SwapFree:/{print $2}' /proc/meminfo)
  sw_used=$(( (swap_tot - swap_free) / 1024 ))
  lck=NA; rss=NA
  if [ -n "$EP" ] && [ -r /proc/$EP/status ]; then
    lck=$(awk '/^VmLck:/{print int($2/1024)}' /proc/$EP/status)
    rss=$(awk '/^VmRSS:/{print int($2/1024)}' /proc/$EP/status)
  fi
  printf '%s,%s,%s,%s,%s,%s\n' "$lbl" "$((avail/1024))" "${sw_used}" "$lck" "$rss" "$(date +%H:%M:%S)" >> "$E/$TAG-snap.csv"
}

post() { # post <name> <jsonfile>
  local name=$1 payload=$2 t0 t1
  snap "pre-$name"
  t0=$(date +%s.%N)
  curl -s --max-time 120 -o "$E/$TAG-$name.json" -w '%{http_code} %{time_total}\n' \
    -H 'Content-Type: application/json' -X POST "$BASE/v1/chat/completions" -d @"$payload" \
    > "$E/$TAG-$name.http" 2>&1
  t1=$(date +%s.%N)
  echo "wall=$(awk -v a=$t0 -v b=$t1 'BEGIN{printf "%.3f", b-a}')" >> "$E/$TAG-$name.http"
  snap "post-$name"
}

MODEL=$(curl -s --max-time 30 "$BASE/v1/models" | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"][0]["id"])')
echo "model=$MODEL" > "$E/$TAG-model.txt"

echo '[{"role":"user","content":"Reply with the single word: ready"}]' > /dev/null
python3 - "$MODEL" > "$E/$TAG-req-short.json" <<'PY'
import json,sys
m=sys.argv[1]
print(json.dumps({"model":m,"messages":[{"role":"user","content":"Reply with the single word: ready"}],"max_tokens":32,"temperature":0}))
PY

python3 - "$MODEL" > "$E/$TAG-req-long.json" <<'PY'
import json,sys
m=sys.argv[1]
print(json.dumps({"model":m,"messages":[{"role":"user","content":"Count from 1 to 120 in English words, all on one line, separated by commas."}],"max_tokens":256,"temperature":0}))
PY

python3 - "$MODEL" > "$E/$TAG-req-tool.json" <<'PY'
import json,sys
m=sys.argv[1]
print(json.dumps({"model":m,"messages":[{"role":"user","content":"What is the current time? Use the get_time tool."}],
 "max_tokens":128,"temperature":0,
 "tools":[{"type":"function","function":{"name":"get_time","description":"Get the current time",
   "parameters":{"type":"object","properties":{"tz":{"type":"string"}},"required":[]}}}],
 "tool_choice":"auto"}))
PY

post short "$E/$TAG-req-short.json"
post long  "$E/$TAG-req-long.json"
post tool  "$E/$TAG-req-tool.json"
snap "final"
echo done
