#!/usr/bin/env bash
# faultdiff_override_selftest.sh [--leg NAME]
#
# Why: STRATA_FAULTDIFF_ENGINE / STRATA_FAULTDIFF_ENGINE_SHA256 let one fault leg run a different
# engine binary. This checks the contract cases with --dry-run ONLY: nothing is booted, no GPU is
# touched, no process is started. Exit 0 = the contract holds.
#
#   1. no env            -> the pinned engine, as the harness printed it before the override existed
#   2. both, real file   -> the WARNING line and the dry-run engine line
#   3. path only         -> refusal naming STRATA_FAULTDIFF_ENGINE_SHA256
#   4. sha only          -> refusal naming STRATA_FAULTDIFF_ENGINE
#   5. both, wrong sha   -> refusal naming both shas
set -uo pipefail
ROOT=/mnt/srcds/Strata
cd "$ROOT"
PY=.venv/bin/python
RUN=bench/ab-v0210/rpcprof/faultdiff_run.py
LEG=parking-off
[ "${1:-}" = "--leg" ] && LEG="${2:?--leg needs a leg name}"
PATCHED=/mnt/srcds/Strata/run/port0218-nohuge-bin/rpcw-hip/strata
PATCHED_SHA=15d330ea4d0eec4ca64ef3973e52d332253a981c25dde42b70567bf8b3ae7577
WRONG_SHA=dededededededededededededededededededededededededededededededede
fail=0

# check <name> <expected exit> <expected-regex> <env pairs...> -- <argv...>
check() {
  local name=$1 want_rc=$2 pattern=$3; shift 3
  local out rc
  out=$(env -u STRATA_FAULTDIFF_ENGINE -u STRATA_FAULTDIFF_ENGINE_SHA256 "$@" 2>&1); rc=$?
  if [ "$rc" -ne "$want_rc" ]; then
    echo "FAIL $name: exit $rc, wanted $want_rc"; echo "$out" | sed "s/^/    /"; fail=1; return
  fi
  if ! grep -qE -- "$pattern" <<<"$out"; then
    echo "FAIL $name: no line matching /$pattern/"; echo "$out" | sed "s/^/    /"; fail=1; return
  fi
  echo "OK   $name (exit $rc)"
  grep -E -- "$pattern" <<<"$out" | sed "s/^/     | /"
}

check "no env: the pinned rig" 0 "engine 936efcc798aab395, helper 58c3ae020a7479a1" \
  $PY "$RUN" --leg "$LEG" --boot --dry-run
check "both set: the WARNING" 0 "WARNING: the rig engine pin is OVERRIDDEN" \
  env STRATA_FAULTDIFF_ENGINE=$PATCHED STRATA_FAULTDIFF_ENGINE_SHA256=$PATCHED_SHA \
  $PY "$RUN" --leg "$LEG" --boot --dry-run
check "both set: the dry-run engine line" 0 "engine $PATCHED .OVERRIDE of the rig pin" \
  env STRATA_FAULTDIFF_ENGINE=$PATCHED STRATA_FAULTDIFF_ENGINE_SHA256=$PATCHED_SHA \
  $PY "$RUN" --leg "$LEG" --boot --dry-run
check "path only: refused, sha variable named" 1 "REFUSING: STRATA_FAULTDIFF_ENGINE_SHA256 is not set" \
  env STRATA_FAULTDIFF_ENGINE=$PATCHED $PY "$RUN" --leg "$LEG" --boot --dry-run
check "sha only: refused, path variable named" 1 "REFUSING: STRATA_FAULTDIFF_ENGINE is not set" \
  env STRATA_FAULTDIFF_ENGINE_SHA256=$PATCHED_SHA $PY "$RUN" --leg "$LEG" --boot --dry-run
check "wrong sha: refused, both shas named" 1 "is sha256 $PATCHED_SHA, but STRATA_FAULTDIFF_ENGINE_SHA256 says $WRONG_SHA" \
  env STRATA_FAULTDIFF_ENGINE=$PATCHED STRATA_FAULTDIFF_ENGINE_SHA256=$WRONG_SHA \
  $PY "$RUN" --leg "$LEG" --boot --dry-run
check "configs --check (no env)" 0 "faultdiff_configs: OK" \
  $PY bench/ab-v0210/rpcprof/faultdiff_configs.py --check

[ "$fail" -eq 0 ] && echo "override selftest: OK (dry-run only; nothing booted)" || \
  echo "override selftest: FAILED"
exit "$fail"
