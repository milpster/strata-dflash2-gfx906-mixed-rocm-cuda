#!/usr/bin/env bash
# ram_map.sh — snapshot a running pair's RAM footprint from its supervisor state JSON.
# Prints process RSS, the engine's largest resident mappings (arena vs file-backed),
# the W1 relief accounting lines from the engine log, and system free/available.
# Usage: ram_map.sh <state.json> [label]
set -euo pipefail
STATE=${1:?state json required}
LABEL=${2:-$(basename "$STATE" .json)}
ENGINE=$(jq -r '.engine_pid' "$STATE")
HELPER=$(jq -r '.helper_pid' "$STATE")
ELOG=$(jq -r '.engine_stderr_log' "$STATE")
echo "== $LABEL (engine $ENGINE, helper $HELPER) =="
ps -o pid,rss,etime -p "$ENGINE" --no-headers | awk '{printf "engine rss=%.2f GiB up=%s\n", $2/1048576, $3}'
ps -o pid,rss,etime -p "$HELPER" --no-headers | awk '{printf "helper rss=%.2f GiB up=%s\n", $2/1048576, $3}'
python3 - "$ENGINE" <<'EOF'
import sys, re
pid = sys.argv[1]
rows, cur = [], {}
with open(f"/proc/{pid}/smaps") as fh:
    for line in fh:
        if re.match(r"^[0-9a-f]+-[0-9a-f]+ ", line):
            if cur.get("Rss", 0) > 51200:
                rows.append(cur)
            parts = line.split()
            cur = {"Rss": 0, "Anon": 0, "name": parts[-1] if len(parts) > 5 else "[anon]"}
        elif line.startswith("Rss:"): cur["Rss"] = int(line.split()[1])
        elif line.startswith("Anonymous:"): cur["Anon"] = int(line.split()[1])
if cur.get("Rss", 0) > 51200:
    rows.append(cur)
rows.sort(key=lambda r: -r["Rss"])
for r in rows[:5]:
    kind = "ANON" if r["Anon"] > r["Rss"] / 2 else "file"
    print(f"  {r['Rss']/1048576:6.2f} GiB {kind:4s} {r['name'][:70]}")
EOF
grep -E 'ram-relief.*dropped|join the drop set|ram-relief:.*total' "$ELOG" | tail -4
free -g | sed -n '2p' | awk '{printf "sys: total=%s used=%s free=%s buff/cache=%s available=%s\n", $2, $3, $4, $5, $6}'
