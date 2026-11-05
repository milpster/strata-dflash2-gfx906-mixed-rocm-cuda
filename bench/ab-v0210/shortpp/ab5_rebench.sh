#!/usr/bin/env bash
# A/B #5: the M-lane re-bench after the fallback fix (14ba81f3: transient pairs assemble in the
# CPU fallback instead of 400ing) and the raised reply deadline (both arms
# STRATA_PREFILL_HELPER_TIMEOUT_MS=120000, so a 100-240 s slow phase no longer kills the tier).
# One window, two full arms on the SAME sha-gated binary (bench binary f5fb0738 restored after):
#   M0b  fixed binary, flags OFF + raised timeout   control (vs S0-r1 baseline + parity)
#   M1b  M0b + STRATA_PROMPT_STAGE_MEMO=1           the memo under test
# Legs S214/S1k/S4k/S18k x3. RAM gate + fail-fast gates live in run_shortpp_arm.sh.
set -u
SP=/home/user/dev/Strata/bench/ab-v0210/shortpp
ROOT=/home/user/dev/Strata
PROD=/mnt/user/Strata
BIN=$ROOT/build-v0210/rpcw-hip/strata
KEEP=$BIN.f5fb0738-aside            # the canonical bench binary (sha f5fb0738218485a2)
MEMO2=$ROOT/build-memo2/rpcw-hip/strata   # shortpp-src + memo + dedup + fallback fix (sha-gated below)
log(){ echo "=== $* $(date +%T) ==="; }
sha16(){ sha256sum "$1" | awk '{print substr($1,1,16)}'; }
swap_in(){   # swap_in <source> <label>; on repeated failure restores bench binary + prod and aborts
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
arm(){   # arm <name> <round>: full-leg arm; leg walls print regardless of rc
  STRATA_PROBE_LEGS=S214,S1k,S4k,S18k bash "$SP/run_shortpp_arm.sh" "$1" "$2" \
    || echo "=== $1-r$2 FAILED rc=$? (leg walls above still count) ==="
}
log "A/B5 start: stopping prod (GPU window ~25-35 min: 2 arms x 4 legs + restores; cards: both VIIs engine + 3080 helper)"
bash "$PROD/stop-prod.sh"
pkill -9 -f '[r]pcw-(hip|cuda)/' 2>/dev/null; pkill -9 -f '/serve/[s]erver[.]py' 2>/dev/null
for i in $(seq 1 60); do
  vram_b=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card[01]/{u+=$3} END{print u+0}')
  (( vram_b < 2000000000 )) && break; echo "$(date +%T) waiting VII drain: $vram_b bytes"; sleep 5
done
log "cell 1: M0b - fixed binary $(sha16 "$MEMO2"), flags OFF + 120 s reply timeout (control)"
swap_in "$MEMO2" memo2-fixed
arm M0b 8
log "cell 2: M1b - + STRATA_PROMPT_STAGE_MEMO=1 (same binary, same timeout)"
arm M1b 9
log "restoring the bench binary + prod"
swap_in "$KEEP" f5fb0738
bash "$PROD/relaunch-pair-supervised.sh" || echo 'PROD RESTORE FAILED - MANUAL ATTENTION'
log "A/B5 end"
