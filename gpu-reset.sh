#!/usr/bin/env bash
# gpu-reset.sh — recover a wedged amdgpu compute card (Radeon VII) WITHOUT rebooting.
#
# Does, in order:
#   1. stops the strata stack (server.py supervisor, engine, vision) as $SVC_USER
#   2. PCI detaches + rescans the card so amdgpu re-initializes it from scratch
#   3. verifies the card is back (lspci / rocm-smi)
#   4. restarts the strata prod server and waits for :8009/health
#
# Usage:
#   sudo ${STRATA_PROD_TREE:-/mnt/user/Strata}/gpu-reset.sh [--bdf 0000:09:00.0] [--skip-stop] [--no-start]
#
# Notes:
# - Never reboots/suspends. If the card does not come back after rescan, it needs a
#   cold power cycle; the script says so and exits non-zero.
# - Only strata processes are signalled. Desktop/X/other GPUs are never touched.
set -uo pipefail

BDF="0000:09:00.0"
SKIP_STOP=0
NO_START=0
SVC_USER="${STRATA_USER:-user}"
STRATA_DIR="${STRATA_PROD_TREE:-/mnt/user/Strata}"
LAUNCH="$STRATA_DIR/launch-prod-swift15.sh"
PORT=8009
LOGDIR="/tmp/agent"

while [ $# -gt 0 ]; do
  case "$1" in
    --bdf) BDF="$2"; shift 2 ;;
    --skip-stop) SKIP_STOP=1; shift ;;
    --no-start) NO_START=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run me with sudo (sysfs writes need root)"; exit 1; }
mkdir -p "$LOGDIR"
LOG="$LOGDIR/gpu-reset-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { log "FAILED: $* (full log: $LOG)"; exit 1; }
as_user() { runuser -u "$SVC_USER" -- "$@"; }

log "=== GPU reset of $BDF starting (no reboot) ==="

# --- 1. stop the strata stack so nothing races the reset or holds VRAM ---
if [ "$SKIP_STOP" -eq 1 ]; then
  log "skip-stop requested: not touching strata processes"
else
  log "stopping strata supervisor (TERM)"
  as_user pkill -TERM -f 'serve/server\.py --engine strata' || true
  for _ in $(seq 1 10); do
    as_user pgrep -f 'serve/server\.py --engine strata' >/dev/null || break
    sleep 1
  done
  if as_user pgrep -f 'serve/server\.py --engine strata' >/dev/null; then
    log "supervisor still up, KILL"
    as_user pkill -KILL -f 'serve/server\.py --engine strata' || true
  fi
  log "stopping engine + vision (KILL; D-state stragglers may linger, that is ok)"
  as_user pkill -KILL -f '${STRATA_PROD_TREE:-/mnt/user/Strata}-2966/build-2966/strata ' || true
  as_user pkill -KILL -f '${STRATA_PROD_TREE:-/mnt/user/Strata}/build-vision/bin/strata-vision' || true
  for _ in $(seq 1 15); do
    [ -z "$(as_user fuser /dev/kfd 2>/dev/null)" ] && break
    log "waiting for /dev/kfd to be released ..."
    sleep 1
  done
  as_user fuser -v /dev/kfd 2>/dev/null && log "WARN: /dev/kfd still held (D-state process?); continuing, PCI remove still works"
fi

# --- 2. snapshot, then PCI remove + rescan ---
log "pre-reset state:"
lspci -Dnn -s "$BDF" || true
dmesg | tail -5 || true

log "PCI remove $BDF"
[ -d "/sys/bus/pci/devices/$BDF" ] || log "device dir absent already (lost from bus), continuing"
if ! echo 1 > "/sys/bus/pci/devices/$BDF/remove" 2>/dev/null; then
  log "remove write failed (device already gone?) — trying rescan anyway"
fi
sleep 2
log "PCI rescan"
echo 1 > /sys/bus/pci/rescan || die "rescan write failed"

# --- 3. wait for the card to come back ---
BACK=0
for _ in $(seq 1 30); do
  if [ -d "/sys/bus/pci/devices/$BDF" ] && lspci -s "$BDF" >/dev/null 2>&1; then BACK=1; break; fi
  sleep 1
done
[ "$BACK" -eq 1 ] || die "card $BDF did NOT return after rescan — it needs a cold power cycle (power off), not more software resets"
log "card is back on the bus:"
lspci -Dnn -s "$BDF"
log "fresh amdgpu init lines (dmesg):"
dmesg | grep -iE "amdgpu.*$BDF" | tail -10 || true

ROCM_SMI="$(command -v rocm-smi || echo /opt/rocm/bin/rocm-smi)"
log "rocm-smi check:"
timeout 30 as_user "$ROCM_SMI" --showid --showmemuse || die "rocm-smi still fails after reset — card not healthy, do not start the engine"

# --- 4. restart strata and wait for health ---
if [ "$NO_START" -eq 1 ]; then
  log "no-start requested: card is healthy, engine left down"
  log "=== done (card recovered, engine NOT started) ==="
  exit 0
fi
[ -x "$LAUNCH" ] || die "launcher missing: $LAUNCH"
log "starting strata: $LAUNCH"
as_user bash -c "cd '$STRATA_DIR' && setsid nohup '$LAUNCH' >> '$LOGDIR/strata-launch-'\"\$(date +%H%M%S)\".log 2>&1 < /dev/null &"

HEALTH=""
for i in $(seq 1 96); do   # up to 8 min: weights load takes a few minutes
  HEALTH="$(curl -sf -m 5 "http://127.0.0.1:$PORT/health" 2>/dev/null)" && break
  [ $((i % 12)) -eq 0 ] && log "still waiting for :$PORT/health ($((i*5))s) ..."
  sleep 5
done
[ -n "$HEALTH" ] || die "engine did not become healthy within 8 min — check $STRATA_DIR/logs/"
log "engine healthy: $HEALTH"
log "post-start amdgpu errors (should be empty):"
dmesg | grep -iE "amdgpu.*(error|fail)" | tail -5 || log "(none)"
log "=== done: $BDF recovered, strata serving on :$PORT, no reboot ==="
