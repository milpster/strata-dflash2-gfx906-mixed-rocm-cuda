#!/usr/bin/env bash
# make_variant.sh <tag> — fresh variant config+launcher pair from the prod pair (idempotent).
# Lane scripts then edit prod-config-<tag>.json (args/env) for their single knob and boot launch-<tag>.sh.
set -euo pipefail
R=/mnt/srcds/Strata
[ $# = 1 ] || { echo "usage: make_variant.sh <tag>"; exit 2; }
T=$1
cp "$R/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json" "$R/prod-config-$T.json"
sed "s#^CONFIG=.*#CONFIG=\"$R/prod-config-$T.json\"#" \
  "$R/launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh" > "$R/launch-$T.sh"
chmod +x "$R/launch-$T.sh"
echo "variant $T: prod-config-$T.json + launch-$T.sh"
