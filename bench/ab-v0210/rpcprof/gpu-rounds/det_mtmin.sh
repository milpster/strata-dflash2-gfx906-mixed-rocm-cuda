#!/usr/bin/env bash
# det_mtmin.sh — determinism probe: STRATA_IQ_MT_MIN=1 (multi-token CPU expert kernels for every
# group; native_expert.cpp:351-368 #152 — removes the drafting-dependent rounding, measured
# -1..-3% decode upstream). repro_pin shape: 3 cold boots x greedy sha (warm x2 on boot 3)
# + judge pair on boot 3 to size the tax here.
# Baseline without the knob (journal 18ar-era repro pin): 7 distinct shas from 8 generations.
# Pass: every sha identical across all boots and warm repeats. Tax: tg80k warm vs band.
set -euo pipefail
R=/mnt/srcds/Strata
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
bash $GR/make_variant.sh detmt1
python3 - << 'PY'
import json
p = "/mnt/srcds/Strata/prod-config-detmt1.json"
c = json.load(open(p))
c["env"]["STRATA_IQ_MT_MIN"] = "1"
json.dump(c, open(p, "w"), indent=2)
PY
PROMPT='Explain how a transformer attention head works, step by step, covering the query, key and value projections, the softmax over scores, and the output projection.'
gen_sha() {
  curl -s -m 240 http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' \
    -d "{\"model\":\"ud-iq3_xxs\",\"messages\":[{\"role\":\"user\",\"content\":\"$PROMPT\"}],\"max_tokens\":512,\"temperature\":0}" | python3 -c "
import sys, json, hashlib
d = json.load(sys.stdin)
c = d['choices'][0]['message']['content']
print('$1:', len(c.split()), 'words sha16=' + hashlib.sha256(c.encode()).hexdigest()[:16], 'finish=' + str(d['choices'][0].get('finish_reason')))"
}
for boot in 1 2 3; do
  echo "=== DET boot $boot/3: STRATA_IQ_MT_MIN=1 ($(date +%T)) ==="
  bash $GR/lane_boot.sh $R/launch-detmt1.sh detmt1-$boot
  gen_sha "cold$boot"
  if [ "$boot" = 3 ]; then
    sleep 2; gen_sha "warm3a"
    sleep 2; gen_sha "warm3b"
    bash $GR/lane_judges.sh DETMT1 1
  fi
done
bash $GR/lane_restore.sh
echo "=== DET COMPLETE ($(date +%T)) ==="
