#!/usr/bin/env bash
# after the reserve-256 arm completes: push card1 FS slots up to a 180 MiB floor (cap +4), one boot
R=/mnt/srcds/Strata
LOG=$R/logs/reserve-arm-256.log
while ! grep -q 'COMPLETE' $LOG 2>/dev/null; do sleep 20; done
sleep 8
FREE_B=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | sed -n '3p' | cut -d',' -f3)
TOTAL_B=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | sed -n '3p' | cut -d',' -f2)
if [ -z "$FREE_B" ] || [ -z "$TOTAL_B" ]; then echo "rocm-smi parse failed"; exit 1; fi
FREE_MIB=$(( (TOTAL_B - FREE_B) / 1048576 ))
SLOT_MIB=35; FLOOR_MIB=180; CAP=4
EXTRA=$(( (FREE_MIB - FLOOR_MIB) / SLOT_MIB ))
[ $EXTRA -gt $CAP ] && EXTRA=$CAP
NEW=8
if [ $EXTRA -ge 1 ]; then NEW=$((8 + EXTRA)); fi
echo "card1 free ${FREE_MIB} MiB -> extra slots ${EXTRA} -> FS 2,${NEW}"
if [ $EXTRA -ge 1 ]; then
  bash $R/bench/ab-v0210/rpcprof/gpu-rounds/fs_arm.sh 2,${NEW} > $R/logs/fs-arm-2b.log 2>&1
  tail -8 $R/logs/fs-arm-2b.log
else
  echo "no safe headroom after reserve arm - keeping 2,8"
fi
echo "=== FS2 CHAIN COMPLETE ($(date +%H:%M:%S)) ==="
