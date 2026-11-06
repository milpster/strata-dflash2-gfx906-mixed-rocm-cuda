#!/usr/bin/env bash
# gpu-sbr.sh — Secondary Bus Reset (SBR) of a wedged GPU via raw setpci config writes.
#
# For the case where the card is wedged BELOW the driver: TTM kworkers in D-state,
# `echo 1 > .../remove` hangs, amdgpu_gpu_recover fails with -19, rmmod impossible.
# Raw config writes never wait on the stuck driver.
#
# It picks the NEAREST ancestor bridge whose BRIDGE_CONTROL register is actually
# readable (a wedged card can take its own on-card switch ports' config space down
# with it — seen on Vega 20, where 14a0/14a1 switch ports sit between the root port
# and the GPU), verifying that NOTHING except the target card hangs below it.
#
# Usage:   sudo ${STRATA_PROD_TREE:-/mnt/user/Strata}/gpu-sbr.sh 0000:09:00.0     (GPU bdf, not a bridge!)
# After:   watch dmesg; if revived, gpu-reset.sh --skip-start (a previously-hung
#          gpu-reset.sh may resume on its own — let it finish, don't double-start).
set -euo pipefail

[ $# -eq 1 ] || { echo "usage: sudo $0 <gpu-bdf>  (e.g. 0000:09:00.0)"; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "needs root (setpci + dmesg)"; exit 1; }
DEV="/sys/bus/pci/devices/$1"
[ -d "$DEV" ] || { echo "no such device: $1"; exit 1; }
[ "$(cat "$DEV/class")" = "0x060400" ] && { echo "$1 is a BRIDGE, not the GPU — pass the GPU's own BDF. Nothing was written."; exit 2; }

# ancestors of the target, nearest first (07:00.0, 08:00.0, 00:02.2, ... up to the host)
ANCHORS=()
CUR="$DEV"
while :; do
  P="$(basename "$(realpath "$CUR/..")")"
  [ -d "/sys/bus/pci/devices/$P" ] || break
  ANCHORS+=("$P")
  [ "$P" = "0000:00:00.0" ] && break
  CUR="/sys/bus/pci/devices/$P"
done

# nearest ancestor bridge with a readable BRIDGE_CONTROL (= header type 1, alive)
SEL=""
for A in "${ANCHORS[@]}"; do
  [ "$(cat "/sys/bus/pci/devices/$A/class" 2>/dev/null || true)" = "0x060400" ] || continue
  if setpci -s "$A" BRIDGE_CONTROL >/dev/null 2>&1; then SEL="$A"; break; fi
  echo "note: $A is a bridge but not config-readable (wedged switch port?) — skipping it"
done
[ -n "$SEL" ] || { echo "FAILED: no healthy ancestor bridge with BRIDGE_CONTROL — nothing written"; exit 1; }

# isolation: everything below SEL must be the target card itself (its own functions
# plus the card's on-card switch ports); anything else means collateral risk.
SELPATH="$(realpath "/sys/bus/pci/devices/$SEL")"
ALLOWED="${ANCHORS[*]}"
BAD=""
for d in /sys/bus/pci/devices/*; do
  case "$(realpath "$d")" in "$SELPATH"/*) ;; *) continue ;; esac
  b="$(basename "$d")"
  case " $ALLOWED " in *" $b "*) continue ;; esac
  case "$b" in "${1%.*}".*) ALLOWED="$ALLOWED $b" ;; *) BAD="$BAD $b" ;; esac
done
[ -z "$BAD" ] || { echo "FAILED: bridge $SEL also serves:$BAD — aborting, nothing written"; exit 1; }

echo "target  : $1  ($(lspci -s "$1" | sed 's/^.*: //'))"
echo "reset via: $SEL (nearest healthy bridge; only this card is below it)"
OLD="$(setpci -s "$SEL" BRIDGE_CONTROL)"
echo "bridge BRIDGE_CONTROL = 0x$OLD; asserting secondary bus reset (bit 6) for 2 s ..."
setpci -s "$SEL" "BRIDGE_CONTROL=$(printf '%04x' $(( 0x$OLD | 0x0040 )))"
sleep 2
setpci -s "$SEL" "BRIDGE_CONTROL=$OLD"
echo "reset released; waiting 8 s for the card/driver to settle ..."
sleep 8

echo "--- lspci ---";      lspci -Dnn -s "$1" || echo "card NOT visible after SBR"
echo "--- dmesg tail ---"; dmesg | grep -iE "amdgpu|$1" | tail -15
echo "--- stuck TTM workers (want: fewer or gone) ---"
ps -eo pid,stat,cmd | awk '$2 ~ /^D/ && /ttm/ {c++} END {print c+0 " TTM kworkers in D"}'
echo "done. If the card is back: gpu-reset.sh --skip-start (or let a hung one finish)."
echo "If nothing changed: cold power cycle — sudo sh -c 'echo s > /proc/sysrq-trigger; echo u > /proc/sysrq-trigger; echo o > /proc/sysrq-trigger' (orderly shutdown will hang on the same kworkers)."
