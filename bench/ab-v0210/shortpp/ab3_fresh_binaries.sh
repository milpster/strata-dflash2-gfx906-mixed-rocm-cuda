#!/usr/bin/env bash
# A/B #3: place the 3-5x short-prompt regression in ONE window with three S1k-mini arms
# (STRATA_PROBE_LEGS=S1k: 3 legs; the fast 6.2-6.5s and regressed 8.6-29s ranges are disjoint):
#   cell 1  f5fb0738 (build-v0210, the regression binary)    anchor - expect S1k slow
#   cell 2  fresh f4e0bc3d build (build-fresh-f4e0)          H2 toolchain drift - fast => H2 dead
#   cell 3  fresh HEAD build (build-fresh-head)              H3 the hunks - slow => hunks guilty
# Swaps are sha-gated: the copy is verified against its source (retries "Text file busy").
set -u
SP=/home/user/dev/Strata/bench/ab-v0210/shortpp
ROOT=/home/user/dev/Strata
PROD=/mnt/user/Strata
BIN=$ROOT/build-v0210/rpcw-hip/strata
KEEP=$BIN.f5fb0738-aside            # the regression binary (sha f5fb0738218485a2)
F4E0=$ROOT/build-fresh-f4e0/rpcw-hip/strata
HEADB=$ROOT/build-fresh-head/rpcw-hip/strata
log(){ echo "=== $* $(date +%T) ==="; }
sha16(){ sha256sum "$1" | awk '{print substr($1,1,16)}'; }
swap_in(){   # swap_in <source> <label>; on repeated failure restores prod and aborts
  local src=$1 want got i
  want=$(sha16 "$src")
  for i in $(seq 1 6); do
    if cp "$src" "$BIN" 2>/dev/null; then
      got=$(sha16 "$BIN")
      if [[ "$got" == "$want" ]]; then echo "swapped $2 in: sha $got (source $src)"; return 0; fi
      echo "swap $2: sha mismatch $got != $want, retrying"
    else
      echo "swap $2: cp busy/failed, retrying"
    fi
    sleep 5
  done
  echo "SWAP $2 FAILED - restoring binary + prod and aborting"
  cp "$KEEP" "$BIN" 2>/dev/null
  bash "$PROD/relaunch-pair-supervised.sh"
  exit 1
}
arm(){   # arm <name> <round>: one S1k-mini arm; leg walls print regardless of rc
  STRATA_PROBE_LEGS=S1k bash "$SP/run_shortpp_arm.sh" "$1" "$2" \
    || echo "=== $1-r$2 mini-arm FAILED rc=$? (leg walls above still count) ==="
}
log "A/B3 start: stopping prod (GPU window ~25-35 min: 3 mini arms + restores)"
bash "$PROD/stop-prod.sh"
pkill -9 -f '[r]pcw-(hip|cuda)/' 2>/dev/null; pkill -9 -f '/serve/[s]erver[.]py' 2>/dev/null
for i in $(seq 1 60); do
  vram_b=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card[01]/{u+=$3} END{print u+0}')
  (( vram_b < 2000000000 )) && break; echo "$(date +%T) waiting VII drain: $vram_b bytes"; sleep 5
done
log "cell 1: the regression binary f5fb0738 (anchor)"
swap_in "$KEEP" f5fb0738
arm S0 3
log "cell 2: fresh f4e0bc3d build (H2: today's toolchain)"
swap_in "$F4E0" fresh-f4e0bc3d
arm S0 4
log "cell 3: fresh HEAD build (H3: the 10 hunks)"
swap_in "$HEADB" fresh-head
arm S0 5
log "restoring the regression binary + prod"
swap_in "$KEEP" f5fb0738
bash "$PROD/relaunch-pair-supervised.sh" || echo 'PROD RESTORE FAILED - MANUAL ATTENTION'
log "A/B3 end"
