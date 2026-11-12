#!/usr/bin/env python3
"""Summarize an r7 arm's legs from the engine log: per-request TG/PP + the
decode-tier swap-in delta line that precedes each summary."""
import re
import sys

path = sys.argv[1]
reqs = []  # (tg, swap_line_or_None)
for line in open(path, errors="replace"):
    m = re.search(r"prompt (\d+) tokens = (\d+) reused \+ (\d+) read in ([\d.]+) ms \(([\d.]+) tok/s\), (\d+) generated in ([\d.]+) ms \(([\d.]+) tok/s\)", line)
    if m:
        reqs.append({
            "ctx": int(m.group(1)) + int(m.group(6)) , "fresh": int(m.group(3)),
            "pp": float(m.group(5)), "gen": int(m.group(6)), "tg": float(m.group(8)),
            "swap": None,
        })
        continue
    s = re.search(r"adapt swap-in: (\d+) swaps, (\d+) batches, (\d+) MiB, (\d+) ms file read", line)
    if s and reqs:
        # decode-tier delta lines print after DONE, before the summary: attach to the NEXT unresolved req
        for r in reversed(reqs):
            if r["swap"] is None:
                r["swap"] = (int(s.group(1)), int(s.group(3)), int(s.group(4)))
                break
print(f"{'leg':>3} {'ctx':>7} {'fresh':>7} {'PP':>7} {'TG':>6} {'gen':>5}  decode-swap (pairs/MiB/ms)")
for i, r in enumerate(reqs):
    sw = r["swap"] or (0, 0, 0)
    print(f"{i:>3} {r['ctx']:>7} {r['fresh']:>7} {r['pp']:>7.1f} {r['tg']:>6.1f} {r['gen']:>5}  {sw[0]}/{sw[1]}/{sw[2]}")
