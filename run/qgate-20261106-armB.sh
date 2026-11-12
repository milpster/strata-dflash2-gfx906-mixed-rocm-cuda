#!/usr/bin/env bash
# qgate-20261106-armB.sh - finish the quality gate: arm B (merged fork) only, then pcompare
# against the already-banked arm C (stock) output, then restore prod on the swift15 lane.
# Follow-up to qgate-20261106.sh whose arm B hit a stale qqB repo-root path (now fixed).
set -u
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"; Q=$ROOT/bench/ab-v0210/quality
export STRATA_PROD_TREE=$ROOT
PY=$ROOT/.venv/bin/python
log(){ echo "=== $* $(date +%T) ==="; }

log "stopping prod (supervisor first)"
bash "$ROOT/stop-prod.sh"
pkill -9 -f '[r]pcw-(hip|cuda)/' 2>/dev/null; pkill -9 -f '/serve/[s]erver[.]py' 2>/dev/null
for i in $(seq 1 60); do
  vram_b=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card[01]/{u+=$3} END{print u+0}')
  (( vram_b < 2000000000 )) && break; echo "$(date +%T) waiting VII drain: $vram_b bytes"; sleep 5
done

cd "$ROOT"
log "arm B: prompt-mode logits (merged fork)"
env -u CUDA_VISIBLE_DEVICES "$PY" tools/quant_quality.py prompt --config "$Q/qqB.json" --out "$Q/out/prompt-merged-B"
log "gate: merged fork (B) vs stock (C) - first-logit divergence"
"$PY" tools/quant_quality.py pcompare "$Q/out/prompt-merged-B" "$Q/out/prompt-merged-C" || true

log "restoring prod (swift15 f16-KV iq3xxs lane, supervised)"
setsid "$ROOT/launch-prod-swift15-f16kv-dflash-w4-q4_0-mixed.sh" >/dev/null 2>&1 &
for i in $(seq 1 40); do
  c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8009/v1/models 2>/dev/null)
  [ "$c" = 200 ] && break; sleep 6
done
echo "prod health: $c"
log "QGATE-B end"
