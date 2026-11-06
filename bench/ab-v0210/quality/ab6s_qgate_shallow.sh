#!/usr/bin/env bash
# SHALLOW quality gate, fp16 (the yardstick's pin): the decisive memo comparison only.
#   PMs = memo binary 66446e21, flags OFF      PXs = same + STRATA_PROMPT_STAGE_MEMO=1
# Shallow lens (256,512) = 10 requests/arm.  Gate: pcompare PMs PXs must be ~0.
# HISTORY: the int8-kv variant (PMKs) WEDGED post-load (one core, 12+ min, zero requests,
# stderr frozen after "loaded 46.84 GiB") - killed; this fp16 run doubles as the
# discriminator (fp16 P0 on the old binary served fine in this exact shape).
# Dropped vs the full windows (ab6/ab6b, parked): the P0 identity arm and the long lens.
set -u
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"; Q=$ROOT/bench/ab-v0210/quality; PROD=${STRATA_PROD_TREE:-/mnt/user/Strata}
PY=$ROOT/.venv/bin/python
log(){ echo "=== $* $(date +%T) ==="; }
sha16(){ sha256sum "$1" | awk '{print substr($1,1,16)}'; }
log "SHALLOW QGATE (fp16+gpu-experts) start: stopping prod (GPU window: 2 shallow arms + 1 compare; experts in VRAM - host RAM lean)"
bash "$PROD/stop-prod.sh"
pkill -9 -f '[r]pcw-(hip|cuda)/' 2>/dev/null; pkill -9 -f '/serve/[s]erver[.]py' 2>/dev/null
for i in $(seq 1 60); do
  vram_b=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card[01]/{u+=$3} END{print u+0}')
  (( vram_b < 2000000000 )) && break; echo "$(date +%T) waiting VII drain: $vram_b bytes"; sleep 5
done
log "binary provenance (sha-gated)"
[[ $(sha16 "$ROOT/build-memo2/rpcw-hip/strata") == 66446e2181bd5243 ]] || { echo 'SHA FAIL: memo2 binary'; bash "$PROD/relaunch-pair-supervised.sh"; exit 1; }
cd "$ROOT"
for a in PM PX; do
  log "arm ${a}s: prompt-mode logits, fp16 pin, --gpu-experts, --lens 256,512 (shallow, VRAM experts)"
  "$PY" tools/quant_quality.py prompt --config "$Q/qq$a.json" --out "$Q/out/prompt-${a}s" --lens 256,512 --gpu-experts \
    || { echo "ARM ${a}s FAILED - restoring prod"; bash "$PROD/relaunch-pair-supervised.sh"; exit 1; }
done
log "GATE: PMs vs PXs - the memo's delta at the fp16 pin, shallow (must be ~0)"
"$PY" tools/quant_quality.py pcompare "$Q/out/prompt-PMs" "$Q/out/prompt-PXs" || true
log "restoring prod"
bash "$PROD/relaunch-pair-supervised.sh" || echo 'PROD RESTORE FAILED - MANUAL ATTENTION'
log "SHALLOW QGATE end"
