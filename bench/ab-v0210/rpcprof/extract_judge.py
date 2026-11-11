#!/usr/bin/env python3
"""extract_judge.py — pull the engine's judge lines for every leg JSON in a lane dir.

Slices the engine log between each leg's elog_start/elog_end byte offsets and prints
the prefill (prompt N tokens = ... in ... ms (... tok/s)) and decode (N generated in
... ms (... tok/s)) numbers, plus per-leg-kind medians for quick lane comparison.
"""
import json
import re
import statistics
import sys
from pathlib import Path

PROMPT_RE = re.compile(r"prompt (\d+) tokens = \d+ reused \+ \d+ read in ([\d.]+) ms \(([\d.]+) tok/s\)")
GEN_RE = re.compile(r"(\d+) generated in ([\d.]+) ms \(([\d.]+) tok/s\)")


def main() -> None:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    legs = sorted(out_dir.glob("*.json"))
    legs = [p for p in legs if re.match(r"^[A-Za-z0-9]+-(warm|8000|80000|tg|longtg|infill)", p.name)]
    pp_by_kind: dict[str, list[float]] = {}
    tg_by_kind: dict[str, list[float]] = {}
    for leg in legs:
        try:
            rec = json.loads(leg.read_text())
        except json.JSONDecodeError:
            continue
        elog = rec.get("elog")
        start, end = rec.get("elog_start"), rec.get("elog_end")
        if not elog or start is None or end is None:
            continue
        with open(elog, "rb") as fh:
            fh.seek(start)
            blob = fh.read(end - start).decode("utf-8", "replace")
        prompts = PROMPT_RE.findall(blob)
        gens = GEN_RE.findall(blob)
        pp = float(prompts[-1][2]) if prompts else float("nan")
        tg = float(gens[-1][2]) if gens else float("nan")
        name = leg.stem
        kind = rec.get("kind", "?")
        print(f"{name:12s} kind={kind:7s} pp={pp:8.1f} tg={tg:7.1f} wall={rec.get('wall_s', float('nan')):7.2f}")
        if prompts:
            pp_by_kind.setdefault(kind, []).append(pp)
        if gens:
            tg_by_kind.setdefault(kind, []).append(tg)
    for kind, vals in pp_by_kind.items():
        print(f"median[{kind} pp] = {statistics.median(vals):.1f} tok/s  (n={len(vals)})")
    for kind, vals in tg_by_kind.items():
        print(f"median[{kind} tg] = {statistics.median(vals):.1f} tok/s  (n={len(vals)})")


if __name__ == "__main__":
    main()
