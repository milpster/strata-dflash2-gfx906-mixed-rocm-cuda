#!/usr/bin/env bash
# port_ab_boot.sh WINDOW ARM REP - boot ONE Phase-2 leg's pair on the frozen tuple.
#
# The measurement boots are the operator's; this script exists because the prod launchers
# hardcode the prod sockets, state file and port, while the contract requires trial sockets
# and a fresh cache dir per leg. It boots through the same pair supervisor prod uses (so a
# crash is a recorded restart, which the leg-validity gate discards) with per-leg state,
# lock, sockets and logs.
#
# Refuses, loudly, when: prerequisite 0 fails; a prod pair looks live; the leg's sockets or
# cache dir are not fresh; the config on disk is not the frozen tuple; an engine/helper
# binary's sha left the manifest.
#
# Does NOT fire a leg and does NOT stop itself: legs are fired by port_ab_legs.py --exec,
# stopping is port_ab_stop.sh (stop and boot are separate invocations).
set -euo pipefail
ROOT=${STRATA_PROD_TREE:-/mnt/srcds/Strata}
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"

# ---- child mode: pair_supervisor re-execs this script with --pair-child ----------------
if [[ ${1:-} == --pair-child ]]; then
  : "${PORT_AB_KEY:?pair child without PORT_AB_KEY: refusing}"
  CFG="${PORT_AB_CONFIG:?}"
  SOCK="${PORT_AB_SOCKET:?}"
  SOCK2="${PORT_AB_SOCKET2:?}"
  WORK="${PORT_AB_WORK:?}"
  BIN="${PORT_AB_BIN:?}"
  HELPER="${PORT_AB_HELPER:?}"     # absolute path to this arm's helper binary
  ELOG="${PORT_AB_ELOG:?}"
  HLOG="${PORT_AB_HLOG:?}"
  BLOG="${PORT_AB_BOOTLOG:?}"
  PORT="${PORT_AB_PORT:-8009}"
  FILL="${PORT_AB_FILL_MIB:-7000}"
  SLOTS="${PORT_AB_SLOTS:-4600}"
  RESERVE="${PORT_AB_RESERVE_MIB:-0}"

  [[ ! -e $SOCK && ! -L $SOCK ]] || { echo "REFUSING: existing socket $SOCK" >&2; exit 1; }
  [[ ! -e $SOCK2 && ! -L $SOCK2 ]] || { echo "REFUSING: existing socket $SOCK2" >&2; exit 1; }
  [[ -z $(ss -H -ltn "sport = :$PORT") ]] || { echo "REFUSING: port $PORT occupied." >&2; exit 1; }
  for f in "$PY" "$HELPER" "$BIN/rpcw-hip/strata" "$WORK/serve/server.py" "$CFG"; do
    [[ -e $f ]] || { echo "Missing: $f" >&2; exit 2; }
  done
  while IFS= read -r name; do
    case "$name" in STRATA_DF_*|STRATA_DFLASH_*|STRATA_DUMP_*|STRATA_WINDOW_*|STRATA_PREFILL_HELPER) unset "$name" ;; esac
  done < <(compgen -e)

  mkdir -p "$(dirname "$HLOG")" "$(dirname "$BLOG")"
  # helper first: the engine attaches to its socket at start (clean env, as the launchers do)
  env -i STRATA_HELPER_REPLY_TIMEOUT_MS=600000 PATH="$PATH" HOME="$HOME" TERM="${TERM:-dumb}" \
    "$HELPER" --pack /mnt/srcds/ai/iq3xxs-ud \
      --experts ../Qwen3.8-Flash-Next-UD-IQ3_XXS-00002-of-00004.gguf \
      --device 0 --slots "$SLOTS" --reserve-mib "$RESERVE" --timeout 63115200000 \
      --begin-timeout 63115200000 --socket "$SOCK" --fill-mib "$FILL" --socket2 "$SOCK2" \
      >>"$HLOG" 2>&1 < /dev/null &
  helper_pid=$!
  for _ in $(seq 1 300); do
    kill -0 "$helper_pid" 2>/dev/null || { echo "Helper exited; see $HLOG" >&2; exit 1; }
    grep -q 'expert-helper: listening on' "$HLOG" && break
    sleep 1
  done
  grep -q 'expert-helper: listening on' "$HLOG" || { echo "Helper readiness timed out." >&2; exit 1; }
  for _ in $(seq 1 60); do
    [[ -S $SOCK && -S $SOCK2 ]] && break
    sleep 1
  done
  [[ -S $SOCK && -S $SOCK2 ]] || { echo "second channel socket missing" >&2; exit 1; }

  cd "$WORK"
  env -u CUDA_VISIBLE_DEVICES -u STRATA_ARENA_MMAP -u GPU_PINNED_MIN_XFER_SIZE \
      -u HSA_USERPTR_FOR_PAGED_MEM -u LD_PRELOAD \
      HSA_OVERRIDE_GFX_VERSION=9.0.6 GPU_MAX_ALLOC_PERCENT=100 \
      LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib \
      "$PY" "$WORK/serve/server.py" --engine strata --config "$CFG" --port "$PORT" \
      >>"$BLOG" 2>&1 < /dev/null
  exit $?
fi

# ---- owner mode ------------------------------------------------------------------------
WINDOW=${1:?usage: port_ab_boot.sh WINDOW ARM REP [SERVER_ARM]}
ARM=${2:?usage: port_ab_boot.sh WINDOW ARM REP [SERVER_ARM]}
REP=${3:?usage: port_ab_boot.sh WINDOW ARM REP [SERVER_ARM]}
SERVER_ARM=${4:-$ARM}   # W4 cells: the engine arm boots A's/B's config, the OTHER server tree runs
OUT=${PORT_AB_OUT:-$HERE/out/port-ab-20261123}
KEY="$WINDOW-$ARM-r$REP"

# the leg's paths and identities come from the frozen generator, not from this script
read -r CFG SOCK SOCK2 ELOG HLOG BLOG WORK BIN HELPER PORT CACHE < <(
  cd "$HERE" && "$PY" - "$WINDOW" "$ARM" "$REP" "$OUT" "$SERVER_ARM" <<'PYEOF'
import sys, pathlib
import port_ab_configs as pc
cfg, deltas, paths = pc.derive(sys.argv[2], window=sys.argv[1], rep=int(sys.argv[3]),
                               out=pathlib.Path(sys.argv[4]))
if pc.check_tuple(cfg, sys.argv[2]):
    raise SystemExit("; ".join(pc.check_tuple(cfg, sys.argv[2])))
pathlib.Path(paths["config"]).parent.mkdir(parents=True, exist_ok=True)
pathlib.Path(paths["config"]).write_text(__import__("json").dumps(cfg, indent=1, sort_keys=True) + "\n")
spec = pc.ARMS[sys.argv[2]]
server = pc.ARMS[sys.argv[5] if len(sys.argv) > 5 else sys.argv[2]]
print(paths["config"], paths["socket"], paths["socket2"], cfg["log"], paths["helper_log"],
      paths["boot_log"], str(pc.ROOT / server["server_tree"]), str(pc.ROOT / spec["engine"]),
      str(pc.ROOT / spec["helper"]), str(cfg["port"]), paths["cache_dir"])
PYEOF
)
[[ $ARM == A || $ARM == B ]] || { echo "bad ARM" >&2; exit 2; }
ARM_HELPER_DIR=$(dirname "$HELPER")

# prerequisite 0 (pre-boot, engine down). Refuses the boot; never overridable.
"$PY" "$HERE/port_ab_prereq0.py" --out "$OUT" --cache-dir "$OUT" || {
  echo "REFUSING: prerequisite 0 failed (see above). The gate is pre-boot and not overridable." >&2; exit 1; }

# no prod pair may be live
for st in "$ROOT"/run/prod*-supervisor.json; do
  [[ -f $st ]] || continue
  case "${st##*/}" in *affinity*|*retry*) continue ;; esac
  sup=$("$PY" - "$st" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(0)
if d.get("state") == "stopped":
    raise SystemExit(0)
print(" ".join(str(d[k]) for k in ("supervisor_pid", "launcher_pid", "helper_pid", "server_pid") if d.get(k)))
PYEOF
)
  for pid in $sup; do
    [[ -d /proc/$pid ]] && { echo "REFUSING: prod pair looks live (${st##*/}:$pid). Stop prod first." >&2; exit 1; }
  done
done

# fresh cache dir per leg (>= 40 GiB free is prerequisite 0's disk gate)
case "$CACHE" in */out/port-ab-*) ;; *) echo "REFUSING: cache dir is not under the harness tree: $CACHE" >&2; exit 1 ;; esac
if [[ -n $(ls -A "$CACHE" 2>/dev/null) ]]; then
  echo "REFUSING: cache dir not empty (stale checkpoint would be served as a fresh prefill): $CACHE" >&2; exit 1
fi
rm -rf "$CACHE"; mkdir -p "$CACHE"
[[ -z $(ls -A "$CACHE") ]] || { echo "REFUSING: cache dir not empty after clear" >&2; exit 1; }
[[ ! -e $SOCK && ! -L $SOCK && ! -e $SOCK2 && ! -L $SOCK2 ]] || { echo "REFUSING: stale socket" >&2; exit 1; }
[[ -z $(ss -H -ltn "sport = :$PORT") ]] || { echo "REFUSING: port $PORT occupied." >&2; exit 1; }

mkdir -p "$OUT/run" "$OUT/logs" "$OUT/boots"
STATE="$OUT/run/$KEY.supervisor.json"
LOCK="$OUT/run/$KEY.lock"
CFG_SHA=$(sha256sum "$CFG" | cut -d' ' -f1)
ENG_SHA=$(sha256sum "$BIN/rpcw-hip/strata" | cut -d' ' -f1)
HEL_SHA=$(sha256sum "$HELPER" | cut -d' ' -f1)
cat > "$OUT/boots/$KEY.json" <<JSON
{"key":"$KEY","window":"$WINDOW","arm":"$ARM","rep":$REP,
 "started_at":"$(date -Is)","config":"$CFG","config_sha256":"$CFG_SHA",
 "engine":"$BIN/rpcw-hip/strata","engine_sha256":"$ENG_SHA",
 "helper":"$HELPER","helper_sha256":"$HEL_SHA",
 "server_tree":"$WORK","sockets":["$SOCK","$SOCK2"],"port":$PORT,
 "cache_dir":"$CACHE","cache_empty_at_boot":true,
 "prereq0_snapshot":"$(ls -t "$OUT"/prereq0-*.json 2>/dev/null | head -1)",
 "loadavg":"$(cut -d' ' -f1-3 /proc/loadavg)","supervisor_state":"$STATE"}
JSON

export PORT_AB_KEY="$KEY" PORT_AB_CONFIG="$CFG" PORT_AB_SOCKET="$SOCK" PORT_AB_SOCKET2="$SOCK2"
export PORT_AB_WORK="$WORK" PORT_AB_BIN="$BIN" PORT_AB_HELPER="$HELPER" PORT_AB_ELOG="$ELOG"
export PORT_AB_HLOG="$HLOG" PORT_AB_BOOTLOG="$BLOG" PORT_AB_PORT="$PORT"
echo "port_ab_boot: $KEY engine $ENG_SHA helper $HEL_SHA config $CFG_SHA"
exec "$PY" "$ROOT/tools/mixed_gpu/pair_supervisor.py" run --launcher "$0" --config "$CFG" \
  --state "$STATE" --lock "$LOCK" --socket "$SOCK" --socket "$SOCK2" --nice 1 \
  --stage-cpus 3,5,7,9,11,13,15 --stall-timeout 600 --startup-timeout 900
