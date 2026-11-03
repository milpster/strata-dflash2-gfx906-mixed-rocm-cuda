#!/usr/bin/env python3
"""Aggregate the A/B rounds into the comparison table.

Reads out/<arm>-r<round>.json (driver walls/usage/shas) plus the engine judge
lines (out/<arm>-r<round>-judge.txt) and prints per-arm medians for:
  probe sha parity, PP fresh read tok/s, TG cached decode tok/s, 80k infill read tok/s.
Verdict rules (the box's discipline): parity mismatch = correctness failure,
perf claims only on >=2 kept runs per arm, medians not means.
"""
import json
import re
import statistics as st
from pathlib import Path

B = Path(__file__).parent
ARMS = ["A", "B"]
LINE = re.compile(r"prompt (\d+) tokens = (\d+) reused \+ (\d+) read in ([\d.]+) ms \(([\d.]+) tok/s\), (\d+) generated in ([\d.]+) ms \(([\d.]+) tok/s\)")

res = {a: {"pp": [], "tg": [], "infill": [], "probe": [], "walls": []} for a in ARMS}
for a in ARMS:
    for j in sorted((B / "out").glob(f"{a}-r*-judge.txt")):
        rnd = j.stem.split("-r")[1].split("-")[0]
        for ln in j.read_text().splitlines():
            m = LINE.search(ln)
            if not m:
                continue
            read, gen = int(m.group(3)), int(m.group(6))
            rd_tps, gen_tps = float(m.group(5)), float(m.group(8))
            if read > 10000 and gen < 100:
                res[a]["pp"].append((f"r{rnd}", read, rd_tps))
            elif read > 40000:
                res[a]["infill"].append((f"r{rnd}", read, rd_tps))
            elif read < 400 and gen > 400:
                res[a]["tg"].append((f"r{rnd}:{gen}t", gen_tps))

    for o in sorted((B / "out").glob(f"{a}-r*.json")):
        d = json.loads(o.read_text())
        res[a]["probe"].append((o.stem, d["probe"]["sha"]))
        res[a]["walls"].append((o.stem, d["pp16384"]["wall"], d["infill80k"]["wall"]))


def med(v):
    return st.median(v) if v else float("nan")


print("== probe parity (temp-0 first request on fresh boot)")
shas = {a: {s for _, s in res[a]["probe"]} for a in ARMS}
for a in ARMS:
    print(f"  arm {a}: {' '.join(sorted(s[:16] for s in shas[a])) or '-'}")
verdict = "MATCH" if shas["A"] == shas["B"] and shas["A"] else "*** MISMATCH (correctness failure) ***"
print(f"  -> {verdict}")
print("\n== PP fresh read (tok/s, >10k read)")
for a in ARMS:
    for r, n, t in res[a]["pp"]:
        print(f"  arm {a} {r}: {n} read @ {t:.1f}")
    if res[a]["pp"]:
        print(f"  arm {a} median: {med([t for _, _, t in res[a]['pp']]):.1f}")
print("\n== TG cached decode (tok/s, steady segments gen>400)")
for a in ARMS:
    for r, t in res[a]["tg"]:
        print(f"  arm {a} {r}: {t:.1f}")
    if res[a]["tg"]:
        print(f"  arm {a} median: {med([t for _, t in res[a]['tg']]):.1f}")
print("\n== 80k deep infill read (tok/s, >40k read)")
for a in ARMS:
    for r, n, t in res[a]["infill"]:
        print(f"  arm {a} {r}: {n} read @ {t:.1f}")
    if res[a]["infill"]:
        print(f"  arm {a} median: {med([t for _, _, t in res[a]['infill']]):.1f}")
