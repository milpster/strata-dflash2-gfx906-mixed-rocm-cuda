#!/usr/bin/env bash
# a2_arm.sh <ARM> — ONE A2 arm, self-contained: variant config (env STRATA_<ARM>=1), boot, judge
# pair. BASE = plain variant copy (same-night baseline). Launch ORPHANED:
#   setsid --fork bash a2_arm.sh Q8_PACKES > log 2>&1
# (an orphaned script also dodges the session-context killer if that is the ready-time
# killer's actual vector — observed: lane bash processes die right at serving-start when
# their variant boots; legs-only invocations survive 3/3.)
set -euo pipefail
R=/mnt/srcds/Strata
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds
ARM=${1:?arm: BASE|Q8_PACKED|SH_STREAM|TSUM|LFUSE|PLE_BATCH|MMVF_ROWS}
TAG=a2$(echo "$ARM" | tr 'A-Z' 'a-z' | tr -d '_')
bash $GR/make_variant.sh "$TAG"
if [ "$ARM" != BASE ]; then
  TAG="$TAG" SW="$ARM" python3 - << 'PY'
import json, os
p = f"/mnt/srcds/Strata/prod-config-{os.environ['TAG']}.json"
c = json.load(open(p))
c["env"]["STRATA_" + os.environ["SW"]] = "1"
json.dump(c, open(p, "w"), indent=2)
PY
  echo "arm $ARM: env STRATA_$ARM=1"
else
  echo "arm BASE: plain prod copy (same-night baseline)"
fi
bash $GR/lane_boot.sh "$R/launch-$TAG.sh" "$TAG"
bash $GR/lane_judges.sh "A2$ARM" 1
echo "=== A2 ARM $ARM COMPLETE ($(date +%T)) ==="
