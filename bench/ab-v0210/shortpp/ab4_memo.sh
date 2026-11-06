#!/usr/bin/env bash
# A/B #4: STRATA_PROMPT_STAGE_MEMO (commits 375183eb dedup + b8470d07 memo; binary built in
# build-memo/rpcw-hip, default OFF). One window, two full arms on the SAME binary
# (sha-gated swap; the bench binary f5fb0738 is restored afterwards):
#   M0  memo binary, flags OFF          parity vs S0-r1 (f5fb0738) + same-binary control
#   M1  M0 + STRATA_PROMPT_STAGE_MEMO=1 the fix under test
# Legs: S214,S1k,S4k,S18k (the session-3 S0 baseline legs; S214 is the overhead-sensitive
# point for an opt-in feature that mostly pays off on many-chunk prompts).
# RAM gate + fail-fast relief/proxy gates live inside run_shortpp_arm.sh (exit 3 refuses
# a tired host; arms run one at a time and wait for VRAM drain between).
set -u
SP=${STRATA_DEV_TREE:-/home/user/dev/Strata}/bench/ab-v0210/shortpp
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
PROD=${STRATA_PROD_TREE:-/mnt/user/Strata}
BIN=$ROOT/build-v0210/rpcw-hip/strata
KEEP=$BIN.f5fb0738-aside            # the canonical bench binary (sha f5fb0738218485a2)
MEMO=$ROOT/build-memo/rpcw-hip/strata   # sha16 95059fae184f6953
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
log "A/B4 start: stopping prod (GPU window ~25-35 min: 2 arms x 4 legs + restores; cards: both VIIs engine + 3080 helper)"
bash "$PROD/stop-prod.sh"
pkill -9 -f '[r]pcw-(hip|cuda)/' 2>/dev/null; pkill -9 -f '/serve/[s]erver[.]py' 2>/dev/null
for i in $(seq 1 60); do
  vram_b=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card[01]/{u+=$3} END{print u+0}')
  (( vram_b < 2000000000 )) && break; echo "$(date +%T) waiting VII drain: $vram_b bytes"; sleep 5
done
log "cell 1: M0 - memo binary 95059fae, flags OFF (parity vs S0 + same-binary control)"
swap_in "$MEMO" memo-95059fae
arm M0 6
log "cell 2: M1 - STRATA_PROMPT_STAGE_MEMO=1 (same binary)"
arm M1 7
log "restoring the bench binary + prod"
swap_in "$KEEP" f5fb0738
bash "$PROD/relaunch-pair-supervised.sh" || echo 'PROD RESTORE FAILED - MANUAL ATTENTION'
log "A/B4 end"
