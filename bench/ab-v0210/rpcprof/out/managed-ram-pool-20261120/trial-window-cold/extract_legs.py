#!/usr/bin/env python3
"""Split the trial engine log into per-leg blocks and pull the measurement lines.

Usage: extract_legs.py <elog-clean.txt> <out.txt> <leg-name> <first-prompt-tokens> [<leg-name> <n> ...]

A leg starts at the elog line carrying `prompt <n> tokens` with the leg's own prompt
token count; it runs to the next leg's start (so an internal continuation request the
server issues for the same conversation lands inside its own leg's block).
"""
import re
import sys

elog, out = sys.argv[1], sys.argv[2]
spec = sys.argv[3:]
legs = [(spec[i], int(spec[i + 1])) for i in range(0, len(spec), 2)]

lines = open(elog, encoding="utf-8", errors="replace").read().splitlines()

KEYS = (
    "prompt ", "generated in", "decode expert cache hit rate", "more read by the GPU",
    "no blob", "refill failed", "no_buffer_space", "WARNING: only", "rewind",
    "restored", "parked", "conversation cache", "foresight swap", "max tokens while still thinking",
    "helper=",
)


def prompt_tokens(line):
    m = re.search(r"prompt (\d+) tokens", line)
    return int(m.group(1)) if m else None


# Assign the k-th occurrence of each prompt-token count to the k-th leg that asks for it,
# in log order (L2 and L3 share the same 80k prompt, so a plain lookup for the first match
# would hand L3 the L2 block).
done = [0] * len(lines)
starts = []
cursor = 0
for name, n in legs:
    idx = next((i for i in range(cursor, len(lines))
                if prompt_tokens(lines[i]) == n and "generated in" in lines[i]), None)
    if idx is not None:
        done[idx] = 1
        cursor = idx + 1
    starts.append((name, n, idx))

with open(out, "w", encoding="utf-8") as fh:
    fh.write("# per-leg engine lines, split by prompt token count\n")
    for k, (name, n, idx) in enumerate(starts):
        fh.write(f"\n=== {name} (prompt {n} tokens, elog line {idx}) ===\n")
        if idx is None:
            fh.write("!! leg prompt line not found\n")
            continue
        end = len(lines)
        for _, _, j in starts[k + 1:]:
            if j is not None and j > idx:
                end = j
                break
        for ln in lines[idx:end]:
            if any(kk in ln for kk in KEYS) and "captur" not in ln:
                fh.write(ln + "\n")
print("wrote", out)
