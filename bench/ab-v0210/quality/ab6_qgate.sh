#!/usr/bin/env bash
# Quality gate for the M-lane promotion (STRATA_PROMPT_STAGE_MEMO): quant_quality.py PROMPT
# mode - the batched prompt path the memo hooks - full-vocab first logits, pcompare across arms.
#   P0 = pre-memo bench binary f5fb0738     PM = memo binary 66446e21, flags OFF
#   PX = PM + STRATA_PROMPT_STAGE_MEMO=1
# Gates: PM-vs-PX (the memo's own delta: same binary, same pack, only the env - ~0 required)
# and P0-vs-PM (the promoted binary's default path vs the pre-memo binary, measured identity).
# Prod is down for the window and restored supervised at the end.
set -u
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"; Q=$ROOT/bench/ab-v0210/quality; PROD=${STRATA_PROD_TREE:-/mnt/user/Strata}
PY=$ROOT/.venv/bin/python
log(){ echo "=== $* $(date +%T) ==="; }
sha16(){ sha256sum "$1" | awk '{print substr($1,1,16)}'; }
log "QGATE start: stopping prod (GPU window: 3 prompt-mode arms + compares; both VIIs, CPU experts)"
bash "$PROD/stop-prod.sh"
pkill -9 -f '[r]pcw-(hip|cuda)/' 2>/dev/null; pkill -9 -f '/serve/[s]erver[.]py' 2>/dev/null
for i in $(seq 1 60); do
  vram_b=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card[01]/{u+=$3} END{print u+0}')
  (( vram_b < 2000000000 )) && break; echo "$(date +%T) waiting VII drain: $vram_b bytes"; sleep 5
done
log "binary provenance (sha-gated)"
[[ $(sha16 "$ROOT/build-v0210/rpcw-hip/strata.f5fb0738-aside") == f5fb0738218485a2 ]] || { echo 'SHA FAIL: f5fb0738-aside'; bash "$PROD/relaunch-pair-supervised.sh"; exit 1; }
[[ $(sha16 "$ROOT/build-memo2/rpcw-hip/strata") == 66446e2181bd5243 ]] || { echo 'SHA FAIL: memo2 binary'; bash "$PROD/relaunch-pair-supervised.sh"; exit 1; }
cd "$ROOT"
for a in P0 PM PX; do
  log "arm $a: prompt-mode logits"
  "$PY" tools/quant_quality.py prompt --config "$Q/qq$a.json" --out "$Q/out/prompt-$a" \
    || { echo "ARM $a FAILED - restoring prod"; bash "$PROD/relaunch-pair-supervised.sh"; exit 1; }
done
log "gate 1: PM vs PX - the memo's own delta (same binary+pack, only the env; must be ~0)"
"$PY" tools/quant_quality.py pcompare "$Q/out/prompt-PM" "$Q/out/prompt-PX" || true
log "gate 2: P0 vs PM - promoted binary flags-off vs pre-memo binary"
"$PY" tools/quant_quality.py pcompare "$Q/out/prompt-P0" "$Q/out/prompt-PM" || true
log "restoring prod"
bash "$PROD/relaunch-pair-supervised.sh" || echo 'PROD RESTORE FAILED - MANUAL ATTENTION'
log "QGATE end"
