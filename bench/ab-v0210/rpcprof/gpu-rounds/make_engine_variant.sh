#!/usr/bin/env bash
# make_engine_variant.sh <tag> <bin_dir> — variant config+launcher pair pointing at a
# DIFFERENT engine build (BIN swapped). <bin_dir> must contain rpcw-hip/ (new engine)
# and rpcw-cuda/ (helper; copy/symlink from v0233 if the new build has none — P0 items
# do not touch the helper). Lane scripts then edit prod-config-<tag>.json env/args.
# Usage: make_variant first, then this, or call this directly (it also copies config).
set -euo pipefail
R=/mnt/srcds/Strata
[ $# = 2 ] || { echo "usage: make_engine_variant.sh <tag> <bin_dir>"; exit 2; }
T=$1; BIN=$2
[ -x "$BIN/rpcw-hip/strata" ] || { echo "missing $BIN/rpcw-hip/strata"; exit 1; }
[ -x "$BIN/rpcw-cuda/strata-expert-helper-patient" ] || { echo "missing helper under $BIN/rpcw-cuda (symlink v0233's)"; exit 1; }
cp "$R/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json" "$R/prod-config-$T.json"
sed -e "s#^CONFIG=.*#CONFIG=\"$R/prod-config-$T.json\"#" \
    -e "s#^BIN=.*#BIN=\"$BIN\"#" \
    "$R/launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh" > "$R/launch-$T.sh"
chmod +x "$R/launch-$T.sh"
echo "engine variant $T: BIN=$BIN + prod-config-$T.json + launch-$T.sh"
