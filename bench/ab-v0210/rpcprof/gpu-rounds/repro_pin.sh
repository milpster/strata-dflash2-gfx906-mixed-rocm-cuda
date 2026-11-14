#!/usr/bin/env bash
# repro_pin.sh — pin the temp-0 nondeterminism: 3 cold boots x 1 request (cold shas)
# + within boot 3: warm repeat x2 (cold-vs-warm KV reuse). Runs after the prefill sweep.
R=/mnt/srcds/Strata
PROMPT='Explain how a transformer attention head works, step by step, covering the query, key and value projections, the softmax over scores, and the output projection.'
gen_sha() {  # gen_sha <label>
  curl -s -m 240 http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' \
    -d "{\"model\":\"ud-iq3_xxs\",\"messages\":[{\"role\":\"user\",\"content\":\"$PROMPT\"}],\"max_tokens\":512,\"temperature\":0}" | python3 -c "
import sys, json, hashlib
d = json.load(sys.stdin)
c = d['choices'][0]['message']['content']
print('$1:', len(c.split()), 'words sha16=' + hashlib.sha256(c.encode()).hexdigest()[:16], 'finish=' + str(d['choices'][0].get('finish_reason')))"
}
while ! grep -q 'PREFILL SWEEP COMPLETE' $R/logs/prefill-sweep.log 2>/dev/null; do sleep 20; done
for boot in 1 2 3; do
  echo "=== COLD BOOT $boot ($(date +%T)) ==="
  bash $R/stop-all.sh >/dev/null 2>&1; sleep 4
  ( cd $R && setsid nohup ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh > logs/prod-repro-$boot.log 2>&1 < /dev/null & )
  RD=""
  for i in $(seq 1 90); do
    S=$(ls -t $R/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-2026111*.log 2>/dev/null | grep -v -- '-helper' | head -1)
    RD=$(tr -d '\0' < "$S" 2>/dev/null | grep -a 'ready: http://' | tail -1)
    PORT=$(curl -s -m 5 http://127.0.0.1:8009/v1/models 2>/dev/null | head -c 20)
    [ -n "$RD" ] && [ -n "$PORT" ] && break
    RD=""; sleep 10
  done
  [ -z "$RD" ] && { echo "BOOT FAIL $boot"; break; }
  sleep 3
  gen_sha "cold$boot"
  if [ "$boot" = "3" ]; then
    sleep 2; gen_sha "warm3a"
    sleep 2; gen_sha "warm3b"
  fi
done
echo "=== REPRO PIN COMPLETE ($(date +%T)) ==="
