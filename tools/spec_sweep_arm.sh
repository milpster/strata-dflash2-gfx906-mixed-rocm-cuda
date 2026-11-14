#!/usr/bin/env bash
# Spec-sweep arm runner (2026-11-14, ud-iq3_xxs): edit ONLY --spec/--spec-min-p
# in the prod config, commit, reboot prod via the sanctioned stop-all + launcher
# pair, wait for the 'ready: http://' line in the newest non-helper server log,
# then run fill + tg x2 legs (identical --leg tg prompts via separate --out dirs
# so the sha256 determinism guard compares like with like).
# Usage: spec_sweep_arm.sh SPEC MINP NAME [--attach]
#   --attach: skip edit/commit/stop/launch; wait on the already-running boot.
# NOTE: no `grep -q` in pipes (pipefail SIGPIPE race) — use grep -c / set lookups.
set -uo pipefail
ROOT=/mnt/srcds/Strata
CFG="$ROOT/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json"
LAUNCHER="$ROOT/launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh"
DRIVER="$ROOT/bench/ab-v0210/rpcprof/ab9_driver.py"
ELOG="$ROOT/logs/engine-prod-ud-iq3xxs-v0233.log"
SPEC=$1 MINP=$2 NAME=$3
ATTACH=${4:-}
OUT="$ROOT/bench/ab-v0210/rpcprof/out/spec-sweep-20261114/$NAME"
CTX=442400

newest_server_log() { ls -t "$ROOT"/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-*.log 2>/dev/null | grep -v -- -helper | head -1; }
ready() { [ "$(tr -d '\0' < "$1" | grep -ac 'ready: http://')" -gt 0 ]; }

echo "=== arm $NAME spec=$SPEC minp=$MINP attach=${ATTACH:-no} start $(date +%T) ==="

if [ -z "$ATTACH" ]; then
  python3 - "$CFG" "$SPEC" "$MINP" <<'PY'
import json, sys
path, spec, minp = sys.argv[1:4]
cfg = json.load(open(path))
args = cfg["args"]
args[args.index("--spec") + 1] = spec
args[args.index("--spec-min-p") + 1] = minp
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
cfg = json.load(open(path))
print("config now --spec", cfg["args"][cfg["args"].index("--spec") + 1],
      "--spec-min-p", cfg["args"][cfg["args"].index("--spec-min-p") + 1])
PY
  git -C "$ROOT" add "$CFG"
  git -C "$ROOT" commit -q -m "bench(prod): xxs spec arm $SPEC-$MINP" || echo "WARN: commit failed (continuing)"

  echo "--- stop prod $(date +%T)"
  bash "$ROOT/stop-all.sh"
  sleep 3
  E0=$(stat -c %s "$ELOG")
  declare -A SEEN=()
  while IFS= read -r f; do SEEN[$f]=1; done < <(ls -1 "$ROOT"/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-*.log 2>/dev/null | grep -v -- -helper)

  echo "--- launch $(date +%T)"
  T0=$(date +%s)
  setsid nohup bash "$LAUNCHER" > "$ROOT/logs/spec-arm-$NAME.log" 2>&1 < /dev/null &
  BOOTLOG=
  for i in $(seq 1 120); do
    sleep 10
    if [ "$(grep -ac 'REFUSING' "$ROOT/logs/spec-arm-$NAME.log" 2>/dev/null || true)" != "0" ]; then
      echo "LAUNCH REFUSED:"; tr -d '\0' < "$ROOT/logs/spec-arm-$NAME.log" | tail -5; exit 3
    fi
    while IFS= read -r L; do
      [ -z "${SEEN[$L]:-}" ] || continue
      ready "$L" && { BOOTLOG=$L; break; }
    done < <(ls -t "$ROOT"/logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-*.log 2>/dev/null | grep -v -- -helper)
    [ -n "$BOOTLOG" ] && break
    [ $((i % 6)) -eq 0 ] && echo "  ...still waiting ($((i * 10))s) $(date +%T)"
  done
  if [ -z "$BOOTLOG" ]; then echo "BOOT TIMEOUT after 20min"; exit 4; fi
  T1=$(date +%s)
else
  E0=$(stat -c %s "$ELOG")
  BOOTLOG=$(newest_server_log)
  T0=$(stat -c %Y "$BOOTLOG")
  for i in $(seq 1 120); do
    ready "$BOOTLOG" && break
    sleep 10
    [ $((i % 6)) -eq 0 ] && echo "  ...still waiting ($((i * 10))s) $(date +%T)"
  done
  ready "$BOOTLOG" || { echo "ATTACH: ready line never appeared in $BOOTLOG"; exit 4; }
  T1=$(date +%s)
fi
echo "--- READY: $BOOTLOG"
tr -d '\0' < "$BOOTLOG" | grep -a 'ready: http://' | tail -1
echo "boot-to-ready: $((T1 - T0))s"
HLOG="${BOOTLOG%.log}-helper.log"
sleep 5

mkdir -p "$OUT"
echo "--- fill leg $(date +%T)"
python3 "$DRIVER" --arm "$NAME" --leg fill --kind prefill --ctx $CTX \
  --out "$OUT" --elog "$ELOG" --hlog "$HLOG" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); print("fill walls", r["walls"], "sha", r["sha256"][:16], r["content_from"])'
echo "--- tg run1 $(date +%T)"
python3 "$DRIVER" --arm "$NAME" --leg tg --kind tg --ctx $CTX --max-tokens 1024 \
  --out "$OUT/run1" --elog "$ELOG" --hlog "$HLOG" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); print("tg1 walls", r["walls"], "sha", r["sha256"][:16], r["content_from"])'
echo "--- tg run2 $(date +%T)"
python3 "$DRIVER" --arm "$NAME" --leg tg --kind tg --ctx $CTX --max-tokens 1024 \
  --out "$OUT/run2" --elog "$ELOG" --hlog "$HLOG" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); print("tg2 walls", r["walls"], "sha", r["sha256"][:16], r["content_from"])'

echo "--- determinism guard"
python3 - "$OUT/run1/$NAME-tg.json" "$OUT/run2/$NAME-tg.json" <<'PY'
import json, sys
a = json.load(open(sys.argv[1])); b = json.load(open(sys.argv[2]))
print("sha run1", a["sha256"])
print("sha run2", b["sha256"])
print("DETERMINISM", "PASS" if a["sha256"] == b["sha256"] else "FAIL")
PY

echo "--- judge lines (arm $NAME)"
tail -c +$((E0 + 1)) "$ELOG" | tr -d '\0' | grep -a 'strata serve:'
echo "=== arm $NAME done $(date +%T) ==="
