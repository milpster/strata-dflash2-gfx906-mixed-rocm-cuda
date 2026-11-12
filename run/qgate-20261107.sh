#!/usr/bin/env bash
# qgate-20261107.sh - parity gate for the v0.2.11-merged build (build-qa2 via build-v0210
# symlink): prompt-mode first logits fork (qqB) vs stock (qqC), pcompare. Prod stays DOWN
# (this runs inside the bench window); the cutover+relaunch happens in the parent lane after.
set -u
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"; Q=$ROOT/bench/ab-v0210/quality
PY=$ROOT/.venv/bin/python
log(){ echo "=== $* $(date +%T) ==="; }
mkdir -p /tmp/agent

# qqB/qqC already carry real paths locally (translated in the 2026-11-06 round).
log "binary provenance"
sha256sum "$ROOT/build-v0210/rpcw-hip/strata" /home/srcds/dev/strata-stock-v0210/build/rpcw-hip/strata | awk '{print substr($1,1,16), $2}'

cd "$ROOT"
for a in B C; do
  log "arm $a: prompt-mode logits (v0.2.11 merged)"
  env -u CUDA_VISIBLE_DEVICES "$PY" tools/quant_quality.py prompt --config "$Q/qq$a.json" --out "$Q/out/prompt-v0211-$a" \
    || { echo "ARM $a FAILED"; }
done
log "gate: v0.2.11 fork (B) vs stock (C) - first-logit divergence"
"$PY" tools/quant_quality.py pcompare "$Q/out/prompt-v0211-B" "$Q/out/prompt-v0211-C" || true
log "QGATE-0211 end"
