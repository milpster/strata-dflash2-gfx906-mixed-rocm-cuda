#!/usr/bin/env bash
# SHALLOW quality gate (user-ordered fast version): the decisive memo comparison only.
#   PMKs = memo binary 66446e21, flags OFF      PXKs = same + STRATA_PROMPT_STAGE_MEMO=1
# Prod-exact kv (--kv int8), shallow lens (256,512) = 10 requests/arm.
# Gate: pcompare PMKs PXKs must be ~0 (same binary, same pack, one env var apart).
# Dropped from the full windows (ab6/ab6b, parked for an overnight run if wanted): the P0
# old-binary identity arm (covered this morning by run-mode A-vs-REF + A/B5's M0b legs) and
# the long prompts (the 1000-token ones cost 857 s each on the pinned CPU path).
set -u
ROOT=/home/user/dev/Strata; Q=$ROOT/bench/ab-v0210/quality; PROD=/mnt/user/Strata
PY=$ROOT/.venv/bin/python
log(){ echo "=== $* $(date +%T) ==="; }
sha16(){ sha256sum "$1" | awk '{print substr($1,1,16)}'; }
log "SHALLOW QGATE start: stopping prod (GPU window: 2 shallow arms + 1 compare; both VIIs, CPU experts)"
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
  log "arm ${a}Ks: prompt-mode logits, --kv int8 --lens 256,512 (prod mode, shallow)"
  "$PY" tools/quant_quality.py prompt --config "$Q/qq$a.json" --out "$Q/out/prompt-${a}Ks" --kv int8 --lens 256,512 \
    || { echo "ARM ${a}Ks FAILED - restoring prod"; bash "$PROD/relaunch-pair-supervised.sh"; exit 1; }
done
log "GATE: PMKs vs PXKs - the memo's delta under prod kv, shallow (must be ~0)"
"$PY" tools/quant_quality.py pcompare "$Q/out/prompt-PMKs" "$Q/out/prompt-PXKs" || true
log "restoring prod"
bash "$PROD/relaunch-pair-supervised.sh" || echo 'PROD RESTORE FAILED - MANUAL ATTENTION'
log "SHALLOW QGATE end"
