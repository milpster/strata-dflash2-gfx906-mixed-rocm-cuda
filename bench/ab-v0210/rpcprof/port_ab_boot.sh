#!/usr/bin/env bash
# port_ab_boot.sh WINDOW ARM BOOT REP [--server-arm A|B] [--waive-nonengine]
#   - boot ONE Phase-2 boot's pair.
#
# The measurement boots are the operator's; this script exists because the prod launchers
# hardcode the prod sockets, state file and port, while the contract requires trial sockets
# and a fresh cache dir per boot. It boots through the same pair supervisor prod uses (so a
# crash is a recorded restart, which the leg-validity gate discards) with per-boot state,
# lock, sockets and logs.
#
# BOOT is the boot group (W1/W3: pp, W2: dec or sht, W4: dec). It is part of the boot key:
# W2's decode and short boots must never share a cache dir, sockets or state file.
#
# Refuses, loudly, when: the derived config is not byte-identical to the frozen manifest pin;
# an engine/helper sha left the manifest; prerequisite 0 fails; a prod pair or this boot's
# predecessor looks live; the boot's sockets exist; the port is taken.
#
# --waive-nonengine (or PREREQ0_WAIVE_NONENGINE=1) waives prerequisite 0's non-engine RSS SUM
# gate for THIS invocation only, recording WAIVED (operator) in the snapshot; it cannot waive
# memory, per-process, stage-core, serving or disk.
#
# Does NOT fire a leg and does NOT stop itself: legs are fired by port_ab_legs.py --run,
# stopping is port_ab_stop.sh (stop and boot are separate invocations).
set -euo pipefail
ROOT=${STRATA_PROD_TREE:-/mnt/srcds/Strata}
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"
SUPERVISOR="$ROOT/tools/mixed_gpu/pair_supervisor.py"

# ---- child mode: pair_supervisor re-execs this script with --pair-child ----------------
if [[ ${1:-} == --pair-child ]]; then
  : "${PORT_AB_PLAN:?pair child without PORT_AB_PLAN: refusing}"
  # shellcheck disable=SC1090
  source "$PORT_AB_PLAN"
  [[ ! -e $P_SOCK && ! -L $P_SOCK ]] || { echo "REFUSING: existing socket $P_SOCK" >&2; exit 1; }
  [[ ! -e $P_SOCK2 && ! -L $P_SOCK2 ]] || { echo "REFUSING: existing socket $P_SOCK2" >&2; exit 1; }
  [[ -z $(ss -H -ltn "sport = :$P_PORT") ]] || { echo "REFUSING: port $P_PORT occupied." >&2; exit 1; }
  for f in "$PY" "$P_HELPER" "$P_ENGINE" "$P_WORK/serve/server.py" "$P_CONFIG"; do
    [[ -e $f ]] || { echo "Missing: $f" >&2; exit 2; }
  done
  while IFS= read -r name; do
    case "$name" in STRATA_DF_*|STRATA_DFLASH_*|STRATA_DUMP_*|STRATA_WINDOW_*|STRATA_PREFILL_HELPER) unset "$name" ;; esac
  done < <(compgen -e)

  mkdir -p "$(dirname "$P_HLOG")" "$(dirname "$P_BLOG")"
  # helper first: the engine attaches to its socket at start. Its whole argv and env come
  # from the frozen manifest (port_ab_boot_plan.py), never from a default in this file.
  env -i "${P_HELPER_ENV[@]}" "${P_HELPER_ARGV[@]}" >>"$P_HLOG" 2>&1 < /dev/null &
  helper_pid=$!
  for _ in $(seq 1 300); do
    kill -0 "$helper_pid" 2>/dev/null || { echo "Helper exited; see $P_HLOG" >&2; exit 1; }
    grep -q 'expert-helper: listening on' "$P_HLOG" && break
    sleep 1
  done
  grep -q 'expert-helper: listening on' "$P_HLOG" || { echo "Helper readiness timed out." >&2; exit 1; }
  for _ in $(seq 1 60); do
    [[ -S $P_SOCK && -S $P_SOCK2 ]] && break
    sleep 1
  done
  [[ -S $P_SOCK && -S $P_SOCK2 ]] || { echo "second channel socket missing" >&2; exit 1; }
  # Publish the helper peer: without a record the supervisor never latches healthy and kills
  # the group at --startup-timeout (it reads peers ONLY from $STRATA_PAIR_RECORD).
  if [[ -n ${STRATA_PAIR_RECORD:-} ]]; then
    "$PY" "$SUPERVISOR" record "$helper_pid" 0 "$P_BLOG" "$P_HLOG"
  else
    echo "WARNING: STRATA_PAIR_RECORD is unset (not booted by the supervisor?)" >&2
  fi

  cd "$P_WORK"
  env -u CUDA_VISIBLE_DEVICES -u STRATA_ARENA_MMAP -u GPU_PINNED_MIN_XFER_SIZE \
      -u HSA_USERPTR_FOR_PAGED_MEM -u LD_PRELOAD \
      HSA_OVERRIDE_GFX_VERSION=9.0.6 GPU_MAX_ALLOC_PERCENT=100 \
      LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib \
      "$PY" "$P_WORK/serve/server.py" --engine strata --config "$P_CONFIG" --port "$P_PORT" \
      >>"$P_BLOG" 2>&1 < /dev/null &
  server_pid=$!
  if [[ -n ${STRATA_PAIR_RECORD:-} ]]; then
    "$PY" "$SUPERVISOR" record "$helper_pid" "$server_pid" "$P_BLOG" "$P_HLOG"
  fi
  rc=0
  wait "$server_pid" || rc=$?
  exit "$rc"
fi

# ---- owner mode ------------------------------------------------------------------------
WINDOW=${1:?usage: port_ab_boot.sh WINDOW ARM BOOT REP [--server-arm A|B]}
ARM=${2:?usage: port_ab_boot.sh WINDOW ARM BOOT REP [--server-arm A|B]}
BOOT=${3:?usage: port_ab_boot.sh WINDOW ARM BOOT REP [--server-arm A|B]}
REP=${4:?usage: port_ab_boot.sh WINDOW ARM BOOT REP [--server-arm A|B]}
shift 4
SERVER_ARM=$ARM
WAIVE_NONENGINE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --server-arm) SERVER_ARM=${2:?--server-arm needs A or B}; shift 2 ;;
    --waive-nonengine) WAIVE_NONENGINE=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
# prerequisite 0's non-engine RSS SUM gate is waived only when this invocation asks for it
# (--waive-nonengine here, or PREREQ0_WAIVE_NONENGINE=1 in the caller's environment). The
# operator directive and its journal record live in port_ab_prereq0.WAIVER; every other gate
# still refuses the boot.
case "${PREREQ0_WAIVE_NONENGINE:-}" in
  1|true|TRUE|yes|on) WAIVE_NONENGINE=1 ;;
esac
[[ $ARM == A || $ARM == B ]] || { echo "bad ARM $ARM" >&2; exit 2; }
[[ $SERVER_ARM == A || $SERVER_ARM == B ]] || { echo "bad --server-arm $SERVER_ARM" >&2; exit 2; }
OUT=${PORT_AB_OUT:-$HERE/out/port-ab-20261123}
KEY="$WINDOW-$ARM-$BOOT-r$REP"
PLAN="$OUT/run/$KEY.env"
mkdir -p "$OUT/run" "$OUT/logs" "$OUT/boots"

# prerequisite 0 (pre-boot, engine down). Refuses the boot. The ONE overridable gate is the
# non-engine RSS sum, and only per invocation with --waive-nonengine or
# PREREQ0_WAIVE_NONENGINE=1: the snapshot then records WAIVED (operator) with the reading and
# the journal, and the call still exits 0 only if every other gate passed. Memory, per-process,
# stage-core, serving-process and disk gates have no override.
PREREQ0_ARGS=(--out "$OUT" --cache-dir "$OUT")
[[ $WAIVE_NONENGINE == "1" ]] && PREREQ0_ARGS+=(--waive-nonengine)
"$PY" "$HERE/port_ab_prereq0.py" "${PREREQ0_ARGS[@]}" || {
  echo "REFUSING: prerequisite 0 failed (see above). Only the non-engine RSS sum gate is" >&2
  echo "          waivable, and only per invocation (--waive-nonengine / PREREQ0_WAIVE_NONENGINE=1);" >&2
  echo "          memory, per-process, stage-core, serving and disk gates always refuse." >&2
  exit 1; }

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

# The plan is the frozen boot: config sha, identities, the helper's full argv and env.
"$PY" "$HERE/port_ab_boot_plan.py" --plan --window "$WINDOW" --arm "$ARM" --boot "$BOOT" \
  --rep "$REP" --server-arm "$SERVER_ARM" --out "$OUT" > "$PLAN.tmp"
mv "$PLAN.tmp" "$PLAN"
# shellcheck disable=SC1090
source "$PLAN"
[[ $P_BOOT_ID == "$KEY" ]] || { echo "REFUSING: plan boot id $P_BOOT_ID != $KEY" >&2; exit 1; }

# this boot's predecessor must be stopped (a live supervisor would respawn against us)
STATE="$OUT/run/$KEY.supervisor.json"
if [[ -f $STATE ]]; then
  live=$("$PY" - "$STATE" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(0)
print(d.get("supervisor_pid") or "")
PYEOF
)
  if [[ -n ${live:-} ]] && kill -0 "$live" 2>/dev/null; then
    echo "REFUSING: this boot's supervisor ($live) is still alive; run port_ab_stop.sh $WINDOW $ARM $BOOT $REP" >&2
    exit 1
  fi
fi

# fresh cache dir per boot (>= 40 GiB free is prerequisite 0's disk gate)
case "$P_CACHE" in */out/port-ab-*) ;; *) echo "REFUSING: cache dir is not under the harness tree: $P_CACHE" >&2; exit 1 ;; esac
rm -rf "$P_CACHE"; mkdir -p "$P_CACHE"
[[ -z $(ls -A "$P_CACHE") ]] || { echo "REFUSING: cache dir not empty after clear" >&2; exit 1; }
[[ ! -e $P_SOCK && ! -L $P_SOCK && ! -e $P_SOCK2 && ! -L $P_SOCK2 ]] || { echo "REFUSING: stale socket" >&2; exit 1; }
[[ -z $(ss -H -ltn "sport = :$P_PORT") ]] || { echo "REFUSING: port $P_PORT occupied." >&2; exit 1; }

# the boot record: cache empty, loadavg, page cache, helper argv/env, identities
"$PY" "$HERE/port_ab_boot_plan.py" --record --window "$WINDOW" --arm "$ARM" --boot "$BOOT" \
  --rep "$REP" --server-arm "$SERVER_ARM" --out "$OUT" > "$PLAN.tmp"
mv "$PLAN.tmp" "$PLAN"
# shellcheck disable=SC1090
source "$PLAN"

export PORT_AB_PLAN="$PLAN"
echo "port_ab_boot: $KEY engine $P_ENGINE_SHA helper $P_HELPER_SHA config $P_CONFIG_SHA"
exec "$PY" "$SUPERVISOR" run --launcher "$0" --config "$P_CONFIG" \
  --state "$STATE" --lock "$OUT/run/$KEY.lock" --socket "$P_SOCK" --socket "$P_SOCK2" --nice 1 \
  --stage-cpus 3,5,7,9,11,13,15 --stall-timeout 600 --startup-timeout 900
