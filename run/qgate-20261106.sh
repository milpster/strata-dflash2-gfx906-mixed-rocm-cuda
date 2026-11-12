#!/usr/bin/env bash
# qgate-20261106.sh - quality gate for the merged (v0.2.10-upmerge) tree, modernized from
# quality/ab6_qgate.sh: prompt-mode first logits, merged fork (qqB) vs stock v0.2.10 (qqC),
# same pack, pcompare verdict. No sha-asserts (those pinned October binaries); shas are
# recorded for the journal instead. Prod is stopped (supervisor FIRST) and restored at the
# end on the CURRENT lane (swift15 f16-KV iq3xxs - relaunch-pair-supervised.sh still boots
# the old iq3_s lane, so it is not used).
set -u
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"; Q=$ROOT/bench/ab-v0210/quality
export STRATA_PROD_TREE=$ROOT
PY=$ROOT/.venv/bin/python
log(){ echo "=== $* $(date +%T) ==="; }

# Local path translation (committed configs stay neutral; this working tree carries real
# paths, same convention as the prod configs/launchers).
sed -i 's#/home/user/#/home/srcds/#g; s#/mnt/user/#/mnt/srcds/#g' "$Q/qqB.json" "$Q/qqC.json"
grep -o '"exe": *"[^"]*"' "$Q/qqB.json" "$Q/qqC.json"

log "QGATE: stopping prod (supervisor first)"
bash "$ROOT/stop-prod.sh"
pkill -9 -f '[r]pcw-(hip|cuda)/' 2>/dev/null; pkill -9 -f '/serve/[s]erver[.]py' 2>/dev/null
for i in $(seq 1 60); do
  vram_b=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card[01]/{u+=$3} END{print u+0}')
  (( vram_b < 2000000000 )) && break; echo "$(date +%T) waiting VII drain: $vram_b bytes"; sleep 5
done
nvidia-smi --query-gpu=memory.used --format=csv,noheader | head -1

log "binary provenance (recorded, not gated)"
sha256sum "$ROOT/build-v0210/rpcw-hip/strata" /home/srcds/dev/strata-stock-v0210/build/rpcw-hip/strata | awk '{print substr($1,1,16), $2}'

cd "$ROOT"
for a in B C; do
  log "arm $a: prompt-mode logits"
  env -u CUDA_VISIBLE_DEVICES "$PY" tools/quant_quality.py prompt --config "$Q/qq$a.json" --out "$Q/out/prompt-merged-$a" \
    || { echo "ARM $a FAILED"; }
done
log "gate: merged fork (B) vs stock (C) - first-logit divergence"
"$PY" tools/quant_quality.py pcompare "$Q/out/prompt-merged-B" "$Q/out/prompt-merged-C" || true

log "restoring prod (swift15 f16-KV iq3xxs lane, supervised)"
setsid "$ROOT/launch-prod-swift15-f16kv-dflash-w4-q4_0-mixed.sh" >/dev/null 2>&1 &
for i in $(seq 1 40); do
  c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8009/v1/models 2>/dev/null)
  [ "$c" = 200 ] && break; sleep 6
done
echo "prod health: $c"
log "QGATE end"
