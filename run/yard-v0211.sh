#!/usr/bin/env bash
# yard-v0211.sh - full teacher-forced yardstick (5x999 positions, PPL + KL p99 + top-1) for
# the v0.2.11 build vs stock, then prod restored. Prod-down window (~15 min).
set -u
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"; Q=$ROOT/bench/ab-v0210/quality
export STRATA_PROD_TREE=$ROOT
PY=$ROOT/.venv/bin/python
IDS=$ROOT/data/quant_quality/v1/set.ids.json
log(){ echo "=== $* $(date +%T) ==="; }

log "stopping prod (supervisor first)"
bash "$ROOT/stop-prod.sh"
pkill -9 -f '[r]pcw-(hip|cuda)/' 2>/dev/null; pkill -9 -f '/serve/[s]erver[.]py' 2>/dev/null
for i in $(seq 1 60); do
  vram_b=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card[01]/{u+=$3} END{print u+0}')
  (( vram_b < 2000000000 )) && break; echo "$(date +%T) waiting VII drain: $vram_b bytes"; sleep 5
done
mkdir -p /tmp/agent

cd "$ROOT"
log "arm B: v0.2.11 build, teacher-forced 4995 positions"
env -u CUDA_VISIBLE_DEVICES "$PY" tools/quant_quality.py run --config "$Q/qqB.json" --ids "$IDS" --out "$Q/out/yard-v0211-B.tsv" \
  || echo "ARM B FAILED"
log "arm C: stock, teacher-forced 4995 positions"
env -u CUDA_VISIBLE_DEVICES "$PY" tools/quant_quality.py run --config "$Q/qqC.json" --ids "$IDS" --out "$Q/out/yard-stock-C.tsv" \
  || echo "ARM C FAILED"
log "compare: stock = reference, v0.2.11 build = run"
"$PY" tools/quant_quality.py compare "$Q/out/yard-stock-C.tsv" "$Q/out/yard-v0211-B.tsv" || true

log "restoring prod (swift15 f16-KV iq3xxs lane, supervised)"
setsid "$ROOT/launch-prod-swift15-f16kv-dflash-w4-q4_0-mixed.sh" >/dev/null 2>&1 &
for i in $(seq 1 40); do
  c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8009/v1/models 2>/dev/null)
  [ "$c" = 200 ] && break; sleep 6
done
echo "prod health: $c"
log "YARD end"
