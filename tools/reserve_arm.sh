#!/usr/bin/env bash
# Reserve-sweep arm runner (2026-11-14 evening, ud-iq3_xxs): edit ONLY
# --vram-reserve-mib, commit, reboot prod, record the 'expert cache auto' banner
# (slots = occupancy), smoke, ONE tg leg, then a 5-minute light-traffic survival
# watch grepping for the fatal 'verify: timed out|stopped unexpectedly' lines.
# Spec knobs stay champion (5/0.5) and --vram-reserve-later-mib stays 576.
# Usage: reserve_arm.sh MIB
# Exit: 0 = arm survived; 3 = launch refused; 4 = boot/no-banner timeout; 5 = smoke fail; 6 = fatal line seen.
set -uo pipefail
ROOT=/mnt/srcds/Strata
CFG="$ROOT/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json"
DRIVER="$ROOT/bench/ab-v0210/rpcprof/ab9_driver.py"
ELOG="$ROOT/logs/engine-prod-ud-iq3xxs-v0233.log"
OUTD="$ROOT/bench/ab-v0210/rpcprof/out/gpu-rounds-20261114"
MIB=$1
ARM="XXS-resv$MIB"
CTX=442400

fatals() { tail -c +$((E0 + 1)) "$ELOG" | tr -d '\0' | grep -acE 'verify: timed out|stopped unexpectedly'; }

echo "=== arm $ARM start $(date +%T) ==="
python3 - "$CFG" "$MIB" <<'PY'
import json, sys
path, mib = sys.argv[1:4][0], sys.argv[2]
cfg = json.load(open(path))
args = cfg["args"]
args[args.index("--vram-reserve-mib") + 1] = mib
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
cfg = json.load(open(path))
a = cfg["args"]
def val(k): return a[a.index(k) + 1]
print("config now --vram-reserve-mib", val("--vram-reserve-mib"),
      "| guards: --spec", val("--spec"), "--spec-min-p", val("--spec-min-p"),
      "--vram-reserve-later-mib", val("--vram-reserve-later-mib"))
assert val("--spec") == "5" and val("--spec-min-p") == "0.5" and val("--vram-reserve-later-mib") == "576"
PY
git -C "$ROOT" add "$CFG"
git -C "$ROOT" commit -q -m "bench(prod): xxs reserve arm $MIB" || echo "WARN: commit failed (continuing)"

echo "--- stop prod $(date +%T)"
bash "$ROOT/stop-all.sh"
sleep 3
E0=$(stat -c %s "$ELOG")
declare -A SEEN=()
while IFS= read -r f; do SEEN[$f]=1; done < <(ls -1 "$ROOT"/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-*.log 2>/dev/null | grep -v -- -helper)

echo "--- launch $(date +%T)"
T0=$(date +%s)
setsid nohup bash "$ROOT/launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh" > "$ROOT/logs/reserve-arm-$MIB.log" 2>&1 < /dev/null &
BOOTLOG=
for i in $(seq 1 120); do
  sleep 10
  if [ "$(grep -ac 'REFUSING' "$ROOT/logs/reserve-arm-$MIB.log" 2>/dev/null || true)" != "0" ]; then
    echo "LAUNCH REFUSED:"; tr -d '\0' < "$ROOT/logs/reserve-arm-$MIB.log" | tail -5; exit 3
  fi
  while IFS= read -r L; do
    [ -z "${SEEN[$L]:-}" ] || continue
    if [ "$(tr -d '\0' < "$L" | grep -ac 'ready: http://')" -gt 0 ]; then BOOTLOG=$L; break; fi
  done < <(ls -t "$ROOT"/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-*.log 2>/dev/null | grep -v -- -helper)
  [ -n "$BOOTLOG" ] && break
  [ $((i % 6)) -eq 0 ] && echo "  ...still waiting ($((i * 10))s) $(date +%T)"
done
[ -n "$BOOTLOG" ] || { echo "BOOT TIMEOUT after 20min"; exit 4; }
echo "--- READY after $(( $(date +%s) - T0 ))s: $BOOTLOG"
tr -d '\0' < "$BOOTLOG" | grep -a 'ready: http://' | tail -1
HLOG="${BOOTLOG%.log}-helper.log"
sleep 5

echo "--- occupancy banner"
BANNER=$(tail -c +$((E0 + 1)) "$ELOG" | tr -d '\0' | grep -a 'expert cache auto' | tail -1)
echo "${BANNER:-<NO BANNER>}"
[ -n "$BANNER" ] || { echo "BREAK: no expert-cache banner"; exit 4; }

echo "--- smoke (chat completion, up to 8 x 15s)"
SMOKE=
for t in $(seq 1 8); do
  R=$(curl -s -m 15 http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' \
    -d '{"messages":[{"role":"user","content":"Reply with the single word: ok"}],"max_tokens":8,"temperature":0,"enable_thinking":false}')
  if echo "$R" | grep -q '"choices"'; then SMOKE=$t; echo "smoke OK on attempt $t: $(echo "$R" | head -c 160)"; break; fi
  echo "  smoke attempt $t failed: $(echo "$R" | head -c 120)"
  sleep 3
done
[ -n "$SMOKE" ] || { echo "BREAK: smoke failed 8x"; exit 5; }

echo "--- tg leg $(date +%T)"
python3 "$DRIVER" --arm "$ARM" --leg tg --kind tg --ctx $CTX --max-tokens 1024 \
  --out "$OUTD" --elog "$ELOG" --hlog "$HLOG" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); print("tg walls", r["walls"], "sha", r["sha256"][:16], r["content_from"])' || { echo "BREAK: tg leg failed"; exit 6; }

echo "--- survival watch 5 x 1/min $(date +%T)"
for w in 1 2 3 4 5; do
  sleep 60
  R=$(curl -s -m 15 http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' \
    -d '{"messages":[{"role":"user","content":"Say ok"}],"max_tokens":8,"temperature":0,"enable_thinking":false}')
  OK=$(echo "$R" | grep -c '"choices"')
  F=$(fatals)
  echo "watch $w: completion_ok=$OK fatals=$F $(date +%T)"
  [ "$F" != "0" ] && { echo "BREAK: fatal line(s) during survival watch"; exit 6; }
  [ "$OK" != "0" ] || { echo "BREAK: survival completion failed: $(echo "$R" | head -c 120)"; exit 6; }
done

echo "--- free-at-fill warning (#2065)"
tail -c +$((E0 + 1)) "$ELOG" | tr -d '\0' | grep -a 'MiB of VRAM free at the start' | tail -2 || true
echo "--- judge lines (arm $ARM)"
tail -c +$((E0 + 1)) "$ELOG" | tr -d '\0' | grep -a 'strata serve: prompt' 
echo "=== arm $ARM SURVIVED $(date +%T) ==="
