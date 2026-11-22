#!/bin/sh
# Copy the fork-only files listed in PORT-MANIFEST.txt into this port tree.
# Usage: sh tools-port/apply-manifest.sh [FORK_TREE] [MANIFEST]
# Idempotent: re-running refreshes the copies from the fork tree.
set -eu
PORT="$(cd "$(dirname "$0")/.." && pwd)"
FORK="${1:-/mnt/srcds/Strata-wt-merge-ipc}"
MAN="${2:-$PORT/PORT-MANIFEST.txt}"
n=0
while IFS="$(printf '\t')" read -r src dst; do
	[ -n "$src" ] || continue
	if [ ! -f "$FORK/$src" ]; then
		echo "MISSING in fork tree: $src" >&2
		exit 1
	fi
	mkdir -p "$PORT/$(dirname "$dst")"
	cp "$FORK/$src" "$PORT/$dst"
	n=$((n + 1))
done < "$MAN"
echo "copied $n files from $FORK"
