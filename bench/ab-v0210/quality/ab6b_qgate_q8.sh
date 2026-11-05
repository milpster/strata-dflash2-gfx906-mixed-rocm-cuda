#!/usr/bin/env bash
# Quality gate, part B: the SAME three arms under PROD'S OWN KV MODE (--kv int8), because the
# user's prod bring-up is iq3_s + int8 kv. Same binaries, same pack; only the kv differs
# from ab6_qgate.sh (fp16 = the yardstick's low-noise pin).
#   P0K = pre-memo bench binary f5fb0738     PMK = memo binary 66446e21, flags OFF
#   PXK = PMK + STRATA_PROMPT_STAGE_MEMO=1
# Gates: PMK-vs-PXK (memo delta under prod kv, ~0 required) and P0K-vs-PMK (promoted binary
# default-path identity under prod kv). Informational: PM-vs-PMK (what int8 kv itself costs).
# Run ONLY after ab6_qgate.sh has fully completed (GPU window is exclusive). Prod down, restored
# supervised at the end.
set -u
ROOT=/home/user/dev/Strata; Q=$ROOT/bench/ab-v0210/quality; PROD=/mnt/user/Strata
PY=$ROOT/.venv/bin/python
log(){ echo "=== $* $(date +%T) ==="; }
sha16(){ sha256sum "$1" | awk '{print substr($1,1,16)}'; }
log "QGATE-B (int8 kv) start: stopping prod (GPU window: 3 prompt-mode arms + compares; both VIIs, CPU experts)"
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
  log "arm ${a}K: prompt-mode logits, --kv int8 (prod mode)"
  "$PY" tools/quant_quality.py prompt --config "$Q/qq$a.json" --out "$Q/out/prompt-${a}K" --kv int8 \
    || { echo "ARM ${a}K FAILED - restoring prod"; bash "$PROD/relaunch-pair-supervised.sh"; exit 1; }
done
log "gate 1: PMK vs PXK - the memo's own delta under prod kv (must be ~0)"
"$PY" tools/quant_quality.py pcompare "$Q/out/prompt-PMK" "$Q/out/prompt-PXK" || true
log "gate 2: P0K vs PMK - promoted binary flags-off vs pre-memo, under prod kv"
"$PY" tools/quant_quality.py pcompare "$Q/out/prompt-P0K" "$Q/out/prompt-PMK" || true
if [[ -d "$Q/out/prompt-PM" ]]; then
  log "informational: PM vs PMK - what int8 kv itself costs on the same binary"
  "$PY" tools/quant_quality.py pcompare "$Q/out/prompt-PM" "$Q/out/prompt-PMK" || true
fi
log "restoring prod"
bash "$PROD/relaunch-pair-supervised.sh" || echo 'PROD RESTORE FAILED - MANUAL ATTENTION'
log "QGATE-B end"
