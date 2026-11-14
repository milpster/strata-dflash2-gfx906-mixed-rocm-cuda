#!/usr/bin/env bash
# Round 2: killer-shape PP on the residency boot — 995,000 fresh bytes, 0 reused, temp 0,
# with the sha determinism guard (same leg twice; register-last is transport-only).
# PREPARED 2026-11-14 — run only after round1.ok exists. Needs the engine UP on :8009.
set -euo pipefail

STRATA=/mnt/srcds/Strata
RPC="$STRATA/bench/ab-v0210/rpcprof"
ELOG="$STRATA/logs/engine-prod-iq3s-v0233.log"
OUT="$RPC/out/gpu-rounds-20261114"
mkdir -p "$OUT"
[ -f "$OUT/round1.ok" ] || { echo "REFUSE: round1.ok missing"; exit 1; }
curl -s -m 3 http://127.0.0.1:8009/v1/models | grep -q swift || { echo "REFUSE: engine not up"; exit 1; }

echo "== killer prefill: run 1 (fresh, 0-reused — THE measurement)"
python3 "$RPC/ab9_driver.py" --arm KILL --leg killer180k --kind prefill --ctx 995000 \
  --out "$OUT/KILL-killer180k-r1.json" --elog "$ELOG" | tee "$OUT/round2.run1.txt"

echo "== killer prefill: run 2 (same arm+leg -> same deterministic prompt; sha must equal run 1)"
python3 "$RPC/ab9_driver.py" --arm KILL --leg killer180k --kind prefill --ctx 995000 \
  --out "$OUT/KILL-killer180k-r2.json" --elog "$ELOG" | tee "$OUT/round2.run2.txt"

SHA1=$(python3 -c "import json;print(json.load(open('$OUT/KILL-killer180k-r1.json'))['sha256'])")
SHA2=$(python3 -c "import json;print(json.load(open('$OUT/KILL-killer180k-r2.json'))['sha256'])")
echo "sha run1: ${SHA1:0:16}…  run2: ${SHA2:0:16}…"

# tok/s from the run-1 judge line: "N tokens = 0 reused + M read in X ms"
J=$(tr -d '\0' < "$ELOG" | grep -a 'tokens = 0 reused' | tail -1)
echo "judge: $J"
READ=$(echo "$J" | grep -oE '0 reused \+ [0-9]+ read' | grep -oE '[0-9]+' | tail -1)
MS=$(echo "$J" | grep -oE 'in [0-9]+ ms' | grep -oE '[0-9]+' | head -1)
TPS=$(python3 -c "print(f'{$READ/$MS:.1f}')")
SHA_STATE=$([ "$SHA1" = "$SHA2" ] && echo OK || echo MISMATCH)

{
  echo "KILLER_PP: $TPS tok/s  read=$READ  ms=$MS  sha=$SHA_STATE"
  echo "reference: T1 180k full-read 620 tok/s (290.6 s); R3A-era killer 466.7"
} | tee "$OUT/round2.verdict.txt"

if [ "$SHA_STATE" = "MISMATCH" ]; then
  echo "CORRECTNESS STOP: temp-0 shas differ — quarantine register-last (STRATA_REGISTER_LAST=0) and run a fix round"
  echo "round2 sha MISMATCH $(date +%H:%M:%S)" > "$OUT/round2.sha-fail"
  exit 2
fi
echo "round2 OK $TPS tok/s $(date +%H:%M:%S)" > "$OUT/round2.ok"
