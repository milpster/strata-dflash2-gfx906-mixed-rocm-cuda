#!/usr/bin/env python3
"""Summarize a panel out-dir: per-leg read/gen tps from the engine elog byte ranges.

The driver records elog_start/elog_end as BYTE offsets. Usage:
  panel_summary.py OUTDIR [OUTDIR...]
"""
import json
import re
import sys
from pathlib import Path

LINE = re.compile(
    r"prompt (\d+) tokens = (\d+) reused \+ (\d+) read in ([\d.]+) \w+ \(([\d.]+) tok/s\), "
    r"(\d+) generated in ([\d.]+) \w+ \(([\d.]+) tok/s\)"
)


def legs(directory: Path):
    for f in sorted(directory.iterdir()):
        real = f / f.name if f.is_dir() else f  # the driver wraps each leg: X.json/X.json
        if not real.is_file():
            continue
        try:
            d = json.loads(real.read_text())
        except json.JSONDecodeError:
            continue
        if "leg" not in d or "elog_start" not in d:
            continue
        blob = Path(d["elog"]).read_bytes()[d["elog_start"]:d["elog_end"]].replace(b"\0", b"")
        passes = [
            (int(read), float(pp), int(gen), float(tg))
            for _, _, read, _, pp, gen, _, tg in LINE.findall(blob.decode("utf-8", "replace"))
        ]
        yield d["leg"], passes, d.get("mem") or {}


def main() -> None:
    for arg in sys.argv[1:]:
        directory = Path(arg)
        print(f"== {directory}")
        for leg, passes, mem in legs(directory):
            cells = " | ".join(
                f"{'cold' if i == 0 else 'warm'}: read {r} @{pp:.1f} gen {g} @{tg:.1f}"
                for i, (r, pp, g, tg) in enumerate(passes[-2:])
            )
            rss = mem.get("max_rss_mib")
            avl = mem.get("min_avail_mib")
            tail = f"  [rss {rss/1024:.1f}G avail {avl/1024:.1f}G]" if rss and avl else ""
            print(f"  {leg:<22} {cells}{tail}")


if __name__ == "__main__":
    main()
