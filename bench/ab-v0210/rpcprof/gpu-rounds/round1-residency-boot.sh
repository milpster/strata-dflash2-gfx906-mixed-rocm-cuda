#!/usr/bin/env bash
# Round 1: flip prod to full residency on the register-last binary and verify the boot.
# PREPARED 2026-11-14 — run only when lane A landed + reviewer PASS.
# Usage: ./round1-residency-boot.sh [BIN]   (default: lane-A build in the worktree)
set -euo pipefail

STRATA=/mnt/srcds/Strata
CFG="$STRATA/prod-config-swift15-iq3s-int8kv-dflash-w4-mixed.json"
LAUNCH="$STRATA/launch-prod-swift15-iq3s-int8kv-dflash-w4-mixed.sh"
PIN_DIR="$STRATA/v0233/build-mixed-gpu/rpcw-hip"
ELOG="$STRATA/logs/engine-prod-iq3s-v0233.log"
OUT="$STRATA/bench/ab-v0210/rpcprof/out/gpu-rounds-20261114"
RESERVE_MIB="${RESERVE_MIB:-2304}"   # post-boot request path needs ~1.5 GiB (weight arena) + margin
BIN="${1:-/mnt/srcds/Strata-wt-integ/build-mixed-gpu/rpcw-hip/strata-final-all-lanes-fc5f89cc3d}"
mkdir -p "$OUT"

[ -x "$BIN" ] || { echo "REFUSE: no binary at $BIN"; exit 1; }
[ -f "$OUT/round1.fail" ] && { echo "REFUSE: previous round1 failed — read $OUT/round1.fail.log first"; exit 1; }

restore_relief() {
  echo "RESTORING RELIEF CONFIG"
  bash "$STRATA/stop-all.sh" >/dev/null 2>&1 || true; sleep 3
  cp "$OUT/config.pre-residency.bak" "$CFG"
  cp "$OUT/strata.pre-residency.bak" "$PIN_DIR/strata.new" && mv -f "$PIN_DIR/strata.new" "$PIN_DIR/strata"
  (cd "$PIN_DIR" && sha256sum strata > SHA256SUMS)
  (cd "$STRATA" && git add prod-config-swift15-iq3s-int8kv-dflash-w4-mixed.json && \
    git commit -q -m "revert(prod): restore relief config after failed residency boot" || true)
  setsid nohup "$LAUNCH" > "$STRATA/logs/prod-v0233-relief-restore.log" 2>&1 &
  echo "round1 FAILED $(date +%H:%M:%S)" > "$OUT/round1.fail"
}
trap 'restore_relief' ERR

echo "== 1. stop prod (sanctioned tool only)"
bash "$STRATA/stop-all.sh" 2>&1 | tail -1

echo "== 1b. kill stragglers and wait until NOTHING executes the pin binary"
pkill -9 -f 'pair_supervisor[.]py' 2>/dev/null || true; pkill -9 -f 'launch-prod-[s]wift15' 2>/dev/null || true
for i in $(seq 1 20); do
  if ! fuser "$PIN_DIR/strata" >/dev/null 2>&1 && ! pgrep -f 'v0233/build-mixed-gpu/rpcw-hip/strat[a]' >/dev/null 2>&1; then
    echo "pin binary clear after ~$((i*3))s"; break
  fi
  if [ $((i % 5)) = 0 ]; then pkill -9 -f 'v0233/build-mixed-gpu/rpcw-hip/strat[a]' 2>/dev/null || true; fi
  sleep 3
  [ "$i" = 20 ] && { echo "ABORT: a process still holds the pin binary after 60s"; pgrep -af 'v0233/build-mixed-gpu/rpcw-hip/strata' | head -3; false; }
done

echo "== 2. backup + config: drop the four relief args (keep reserves 576, env knobs, startup-timeout 1200)"
cp "$CFG" "$OUT/config.pre-residency.bak"
cp "$PIN_DIR/strata" "$OUT/strata.pre-residency.bak"
RESERVE_MIB="$RESERVE_MIB" python3 - "$CFG" << 'EOF'
import json, os, sys
p = sys.argv[1]
c = json.load(open(p))
RELIEF = {"--expert-helper-ram-relief", "--expert-helper-ram-relief-packs",
          "--expert-helper-relief-hot-mib", "--expert-helper-relief-arena-cap-mib"}
a, out = c["args"], []
i = 0
while i < len(a):
    if a[i] in RELIEF:
        i += 2 if i + 1 < len(a) and not a[i+1].startswith("--") else 1
    else:
        out.append(a[i]); i += 1
c["args"] = out
a = out
for i, f in enumerate(a):
    if f in ("--vram-reserve-mib", "--vram-reserve-later-mib"):
        a[i+1] = os.environ.get("RESERVE_MIB", "2304")
json.dump(c, open(p, "w"), indent=2)
print("args now:", len(a), "; reserves:", [a[i+1] for i, f in enumerate(a) if f in ("--vram-reserve-mib", "--vram-reserve-later-mib")])
EOF

echo "== 3. pin binary + refresh SHA256SUMS"
cp "$BIN" "$PIN_DIR/strata.new" && mv -f "$PIN_DIR/strata.new" "$PIN_DIR/strata"
(cd "$PIN_DIR" && sha256sum strata > SHA256SUMS.new && grep -v ' strata$' SHA256SUMS >> SHA256SUMS.new 2>/dev/null || true; mv SHA256SUMS.new SHA256SUMS)
(cd "$STRATA" && git add prod-config-swift15-iq3s-int8kv-dflash-w4-mixed.json && \
  git commit -q -m "feat(prod): full expert residency on register-last binary - relief args dropped, reserves 576") || echo "(config already committed - idempotent rerun)"

echo "== 4. boot and watch banners (startup-timeout 1200s is the hard stop)"
: > "$ELOG"
setsid nohup "$LAUNCH" > "$STRATA/logs/prod-v0233-residency-boot.log" 2>&1 &
declare -A SEEN=()
for i in $(seq 1 160); do  # 160 x 15s = 40 min ceiling (cold boots + range registration ~16 min; server patience now 2400s)
  sleep 15
  L=$(tr -d '\0' < "$ELOG" 2>/dev/null || true)
  [[ "$L" == *"host-callback thread started"* ]] && SEEN[warm]=1
  [[ "$L" == *"expert cache auto"* ]] && SEEN[cache]=1
  [[ "$L" == *"registering the whole arena"*"last boot step"* ]] && SEEN[reg]=1
  [[ "$L" == *"registration ok in"* ]] && SEEN[regok]=1
  if curl -s -m 3 http://127.0.0.1:8009/v1/models 2>/dev/null | grep -q swift && \
     [ -n "${SEEN[regok]:-}" ]; then SEEN[up]=1; fi
  # banner ORDER check: registration must come after the cache fill (register-LAST)
  if [ -n "${SEEN[reg]:-}" ] && [ -z "${SEEN[cache]:-}" ]; then
    echo "ORDER VIOLATION: registration banner before expert-cache line"; false
  fi
  [ -n "${SEEN[regok]:-}" ] && [ -n "${SEEN[up]:-}" ] && break
done
PIN_CAP=$(python3 -c "import json;print(json.load(open('$CFG'))['env'].get('STRATA_ARENA_PIN_GIB',''))")
[ -n "${SEEN[warm]:-}" ] || { echo "MISSING banner: host-callback warm-up"; false; }
if [ -z "$PIN_CAP" ]; then
  [ -n "${SEEN[reg]:-}" ]  || { echo "MISSING banner: whole-arena registration (register-last not armed?)"; false; }
  [ -n "${SEEN[regok]:-}" ] || { echo "MISSING banner: registration ok"; false; }
else
  echo "capped pin (STRATA_ARENA_PIN_GIB=$PIN_CAP): register-last banners not expected (sliced path)"
fi
[ -n "${SEEN[up]:-}" ]   || { echo "Engine never came up on :8009"; false; }
tr -d '\0' < "$ELOG" | grep -aE 'whole arena|host-callback|expert cache auto|loaded [0-9.]+ GiB' | head -8 | tee "$OUT/round1.banners.txt"

if [ -z "$PIN_CAP" ]; then
echo "== 4b. range-count gate (review): a degenerate count means the 18n single-range spin — abort BEFORE the first request"
RANGES=$(tr -d '\0' < "$ELOG" | grep -a 'registration ok in' | tail -1 | grep -oE '[0-9]+ range' | grep -oE '[0-9]+' | head -1)
echo "registered ranges: ${RANGES:-none-parsed}"
[ -n "$RANGES" ] || { echo "ABORT: could not parse range count from the ok banner"; false; }
[ "$RANGES" -ge 100 ] || { echo "ABORT: only $RANGES ranges — shape degraded toward the single-range spin (review note)"; false; }
fi

echo "== 5. first-request smoke (reviewer note E): retries until the engine is truly READY"
echo "   (registration-ok + /v1/models is NOT readiness: post-registration init takes minutes;"
echo "    early requests are dropped and the supervisor may rebuild once — retrying covers both)"
MID=$(curl -s -m 3 http://127.0.0.1:8009/v1/models | python3 -c "import json,sys;print(json.load(sys.stdin)['data'][0]['id'])")
SMOKE=""
for i in $(seq 1 20); do
  sleep 30
  SMOKE=$(curl -s -m 240 http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MID\",\"messages\":[{\"role\":\"user\",\"content\":\"Say OK.\"}],\"max_tokens\":8,\"temperature\":0}") || SMOKE=""
  if echo "$SMOKE" | grep -q '"content"'; then echo "smoke content on attempt $i (~$((i*30))s)"; break; fi
  echo "attempt $i: no content yet ($(echo "$SMOKE" | head -c 60))"
done
echo "$SMOKE" | head -c 200; echo
echo "$SMOKE" | grep -q '"content"' || { echo "SMOKE FAILED after 20 attempts (~10 min): engine never served a completion"; false; }
NS=$(tr -d '\0' < "$ELOG" | grep -acE 'verify: timed out|stopped unexpectedly') || NS=0
[ "$NS" -eq 0 ] || { echo "$NS fatal lines after smoke request"; false; }
echo "$SMOKE" > "$OUT/round1.smoke.json"

echo "== 6. survival watch: 8 min of live traffic (old spiral died <5 min)"
sleep 480
curl -s -m 3 http://127.0.0.1:8009/v1/models >/dev/null || { echo "Engine died inside survival window"; false; }
N=$(tr -d '\0' < "$ELOG" | grep -acE 'verify: timed out|stopped unexpectedly') || N=0
[ "$N" -eq 0 ] || { echo "$N fatal lines inside survival window"; false; }

trap - ERR
echo "round1 OK $(date +%H:%M:%S)" > "$OUT/round1.ok"
echo "ROUND 1 PASS: residency boot verified — banners ordered, engine up, 8 min survival, 0 fatals"
