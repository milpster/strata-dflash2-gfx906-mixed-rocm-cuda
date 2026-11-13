#!/usr/bin/env bash
# Round-6 diet-lane boot for the conversation-cache bound soak.  Boots NOTHING by
# itself: it only WRITES the soak engine config (prod config + the diet flags) and prints the
# exact serve command.  The operator boots it against the pinned round-6 pair.
#
# The soak boots its OWN engine on a soak port (default 8019), never :8009, and parks into its
# OWN conversation-cache folder so parked-file page-cache deltas are measured clean.
#
# Diet flags added over the prod config:
#   --conversation-cache-evict-page-cache   bound 1: release parked/restore/sidecar file pages
#   --conversation-cache-ram-mib N          bound 2: the RAM tier's ceiling.  A park whose snapshot is
#                                           over it goes to the disk tier ONLY (no host copy built);
#                                           over it the published LRU entries drop to disk.  Round 5
#                                           set this equal to --conversation-cache-mib (4096), so it
#                                           never bit: the tier filled with 4 whole snapshots of one
#                                           conversation (2.48 GiB at 19k tokens) and RSS ratcheted.
#   --kv-resident-mib N                     bound 3: budget the resident KV (only lowers asked)
#
# The ceiling arithmetic for the gate (this model, 12 QSA layers, fp16 K/V): a parked snapshot
# costs ~40-47 KB/token (measured v0224: 894 MiB at 19010 tokens, 1140 MiB at 28430).  512 MiB
# keeps ~11-13k tokens in RAM - enough for the LRU pass's small conversations - and a 120-130k
# conversation is disk-only, so the tier adds ~0 MiB of RSS at the gate's context.
set -euo pipefail
WT=${STRATA_SOAK_WT:-/mnt/srcds/Strata-wt-helper-ipc}
PROD_CONFIG=${PROD_CONFIG:-/mnt/srcds/Strata/prod-config-swift15-f16kv-dflash-w4-q4_0-mixed.json}
RAM_MIB=${RAM_MIB:-512}          # conversation-cache-ram-mib (the RAM tier's ceiling)
KV_RES_MIB=${KV_RES_MIB:-4096}   # kv-resident-mib (resident KV budget: 4096 MiB = 174762 cells)
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
echo "=== boot the soak engine against the pinned round-6 pair (operator; NOT :8009) ==="
echo "  \$PY \$WORK/serve/server.py --engine strata --config $OUT --port $PORT"
echo "then drive it:  python3 $WT/tools/conv_cache_diet_soak.py --url http://127.0.0.1:$PORT/v1/chat/completions \\"
echo "                   --elog <serve stderr log> --cache-dir $CACHE_DIR --ram-ceiling-mib $RAM_MIB"
echo
echo "=== the boot lines the judge greps (exact, with the round-6 numbers) ==="
echo "  strata generate: --kv-resident-mib $KV_RES_MIB: 24576 bytes per resident KV cell over 12 QSA layers ->"
echo "                   resident 174762 of 262144 asked cells (4.0 GiB resident ..., --kv-resident 262144)"
echo "  strata serve: conversation cache: RAM ceiling $RAM_MIB MiB (parked snapshots plus the retained K/V);"
echo "                   a snapshot over it parks to disk only, ... (the active conversation is never dropped)"
echo "  strata serve: conversation cache: file page-cache release ON (park writes, restore reads and ring"
echo "                   sidecars hand their cached pages back after their flush; the parked file's"
echo "                   page-cache footprint stays ~0)"
