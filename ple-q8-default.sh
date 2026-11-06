#!/usr/bin/env bash
# Select which PLE table the NORMAL prod launcher (launch-prod-swift15.sh) uses.
#   ple-q8-default.sh original : the model file's own IQ4_NL table (the shipped state)
#   ple-q8-default.sh q8_0     : Delgon's standalone Q8_0 sidecar
#
# Takes effect at the NEXT server restart; a running engine is never touched, model
# files and every other flag stay as they are. The launcher hardcodes the config
# name prod-config-swift15.json, so switching = copying the chosen variant over it
# (a .bak-YYYYMMDD-HHMMSS copy follows the repo's existing backup convention).
# Pristine variants: prod-config-swift15-original.json / prod-config-swift15-q8ple.json
set -euo pipefail
cd "$(dirname "$0")"

case "${1:-}" in
  original) SRC=prod-config-swift15-original.json ;;
  q8_0)     SRC=prod-config-swift15-q8ple.json ;;
  *) echo "usage: $0 original|q8_0" >&2; exit 2 ;;
esac
[ -f "$SRC" ] || { echo "missing $SRC" >&2; exit 1; }
if [ "$SRC" = "prod-config-swift15-q8ple.json" ]; then
  PLE=${STRATA_AI_DIR:-/mnt/user/ai}/Qwen3.8-Flash-Next-PLE-Q8_0.gguf
  [ -f "$PLE" ] || { echo "missing $PLE - not switching (download and verify it first)" >&2; exit 1; }
fi
if ! cmp -s prod-config-swift15.json "$SRC"; then
  cp -a prod-config-swift15.json "prod-config-swift15.json.bak-$(date +%Y%m%d-%H%M%S)"
  cp "$SRC" prod-config-swift15.json
fi
echo "prod config now uses the '$1' PLE - active at the next server restart"
