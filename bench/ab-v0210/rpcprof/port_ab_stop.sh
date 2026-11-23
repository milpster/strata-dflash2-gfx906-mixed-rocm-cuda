#!/usr/bin/env bash
# port_ab_stop.sh WINDOW ARM BOOT REP - stop ONE Phase-2 boot's pair through its state file.
#
# Stop and boot are separate invocations (the operator directive after the 2026-11-12
# incidents), and no kill pattern lives in a command line: the supervisor is found through
# its per-boot state file, its identity is checked in /proc before the signal, and the
# survivors named in that state are TERMed by recorded pid. Quiet is verified for 20 s.
#
# BOOT is the boot group (part of the boot key: W2-A-dec-r1 vs W2-A-sht-r1).
#
# At stop time it also writes the boot's end into boots/<KEY>.json: ended_epoch (the first stop
# wins) and the final supervisor generation. The gate checker bounds a boot's kernel-journal
# fingerprint by that end, so a fault during a LATER boot is never charged to an earlier one.
set -uo pipefail
ROOT=${STRATA_PROD_TREE:-/mnt/srcds/Strata}
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"
WINDOW=${1:?usage: port_ab_stop.sh WINDOW ARM BOOT REP}
ARM=${2:?usage: port_ab_stop.sh WINDOW ARM BOOT REP}
BOOT=${3:?usage: port_ab_stop.sh WINDOW ARM BOOT REP}
REP=${4:?usage: port_ab_stop.sh WINDOW ARM BOOT REP}
OUT=${PORT_AB_OUT:-$HERE/out/port-ab-20261123}
KEY="$WINDOW-$ARM-$BOOT-r$REP"
STATE="$OUT/run/$KEY.supervisor.json"
SOCK="$OUT/run/pa-$KEY.sock"
SOCK2="$OUT/run/pa-$KEY-2.sock"

term_pid() {
  local pid=$1
  kill -TERM "$pid" 2>/dev/null || return 0
  timeout 60 tail --pid="$pid" -f /dev/null 2>/dev/null || true
}

if [[ -f $STATE ]]; then
  sup=$("$PY" - "$STATE" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(0)
print(d.get("supervisor_pid") or "")
PYEOF
)
  if [[ -n ${sup:-} ]] && kill -0 "$sup" 2>/dev/null && grep -qa 'pair_supervisor[.]py' "/proc/$sup/cmdline" 2>/dev/null; then
    echo "port_ab_stop: TERM supervisor $sup"
    term_pid "$sup"
  fi
  for pid in $("$PY" - "$STATE" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(0)
for k in ("launcher_pid", "server_pid", "helper_pid", "engine_pid"):
    v = d.get(k)
    if isinstance(v, int) and v > 0:
        print(v)
PYEOF
); do
    if [[ -d /proc/$pid ]]; then
      cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
      case "$cmd" in
        *"$ROOT"*) echo "port_ab_stop: TERM $pid ${cmd:0:70}"; term_pid "$pid" ;;
        *) echo "port_ab_stop: pid $pid was recycled to another program, leaving it alone" ;;
      esac
    fi
  done
else
  echo "port_ab_stop: no state file $STATE (was this boot started?)"
fi

# Record where the boot ended: the checker bounds the kernel-journal fingerprint by this
# epoch (never by the window's end, which would attribute a later boot's fault to this one).
# The first stop wins; the writer is idempotent and also stores the final supervisor generation.
"$PY" "$HERE/port_ab_legs.py" --bookend "$KEY" --out-dir "$OUT" 2>&1 ||
  echo "port_ab_stop: could not bookend boots/$KEY.json" >&2

rm -f "$SOCK" "$SOCK2"
echo "port_ab_stop: verifying 20 s of quiet for $KEY"
sleep 20
alive=0
for d in /proc/[0-9]*; do
  pid=${d#/proc/}
  exe=$(readlink "$d/exe" 2>/dev/null) || continue
  case "$exe" in
    "$ROOT"/run/fsclamp-bin/rpcw-hip/strata|"$ROOT"/run/port0218-bin/rpcw-hip/strata)
      # only count it when this boot's socket is on its command line
      if tr '\0' '\n' < "$d/cmdline" 2>/dev/null | grep -qxF "$SOCK"; then
        echo "  still alive: engine $pid"; alive=1
      fi ;;
  esac
done
[[ -S $SOCK || -S $SOCK2 ]] && alive=1
if [[ $alive -eq 0 ]]; then echo "port_ab_stop: QUIET"; exit 0; fi
echo "port_ab_stop: NOT QUIET - run again or inspect $STATE" >&2
exit 1
