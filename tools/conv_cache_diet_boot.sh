#!/usr/bin/env bash
# Round-5 diet-lane boot for the conversation-cache bound soak (v0224).  Boots NOTHING by
# itself: it only WRITES the soak engine config (prod config + the three diet flags) and
# prints the exact serve command.  The operator boots it against the pinned v0224 pair.
#
# The soak boots its OWN engine on a soak port (default 8019), never :8009, and parks into
# its OWN conversation-cache folder so parked-file page-cache deltas are measured clean.
#
# Diet flags added over the prod config:
#   --conversation-cache-evict-page-cache   bound 1: release parked/restore/sidecar file pages
#   --conversation-cache-ram-mib N          bound 2: LRU park+evict over the RAM ceiling
#   --kv-resident-mib N                     bound 3: budget the resident KV (only lowers asked)
set -euo pipefail
WT=${STRATA_SOAK_WT:-/mnt/srcds/Strata-wt-helper-ipc}
PROD_CONFIG=${PROD_CONFIG:-/mnt/srcds/Strata/prod-config-swift15-f16kv-dflash-w4-q4_0-mixed.json}
RAM_MIB=${RAM_MIB:-4096}        # conversation-cache-ram-mib (parked snapshots + retained KV)
KV_RES_MIB=${KV_RES_MIB:-4096}  # kv-resident-mib (keeps a 130k active conversation resident)
PORT=${PORT:-8019}
CACHE_DIR=${CACHE_DIR:-$WT/run/diet-soak-cache}
OUT=${OUT:-$WT/run/diet-soak-config.json}
mkdir -p "$CACHE_DIR"
python3 - "$PROD_CONFIG" "$OUT" "$RAM_MIB" "$KV_RES_MIB" "$CACHE_DIR" <<'PY'
import json, sys
src, out, ram, kvres, cache = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
cfg = json.load(open(src))
args = cfg["args"]
def setflag(name, value):
    for i, a in enumerate(args):
        if a == name:
            args[i + 1] = str(value); return
    args.extend([name, str(value)])
def addflag(name):
    if name not in args: args.append(name)
setflag("--conversation-cache-dir", cache)
setflag("--conversation-cache-ram-mib", ram)
setflag("--kv-resident-mib", kvres)
addflag("--conversation-cache-evict-page-cache")
json.dump(cfg, open(out, "w"), indent=2)
print("wrote", out)
print("  conversation-cache-dir      ", cache)
print("  conversation-cache-ram-mib  ", ram)
print("  kv-resident-mib             ", kvres)
print("  conversation-cache-evict-page-cache  ON")
PY
echo
echo "=== boot the soak engine against the pinned v0224 pair (operator; NOT :8009) ==="
echo "  \$PY \$WORK/serve/server.py --engine strata --config $OUT --port $PORT"
echo "then drive it:  python3 $WT/tools/conv_cache_diet_soak.py --url http://127.0.0.1:$PORT/v1/chat/completions \\"
echo "                   --elog <serve stderr log> --cache-dir $CACHE_DIR"
