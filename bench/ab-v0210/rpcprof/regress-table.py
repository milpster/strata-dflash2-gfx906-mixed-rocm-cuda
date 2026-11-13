#!/usr/bin/env python3
"""Cross-boot regression table: pack-read speed (ms/slot), warmup cost, RSS.

Reads every engine log in /mnt/srcds/Strata/logs plus panel records in the
rpcprof out dirs and prints one row per boot: binary, knobs, warmup wall,
warm-leg RSS, and the 'adapt swap-in' ms/slot distribution.  Read-only; safe
to run during a panel.

Usage: regress-table.py [logs...]   (defaults to engine-*.log sorted by mtime)
"""
import json
import os
import re
import sys
from pathlib import Path

LOGS = Path("/mnt/srcds/Strata/logs")
OUT = Path("/mnt/srcds/Strata/bench/ab-v0210/rpcprof/out")

# boot identity by log name convention: engine-<tag>.log where tag encodes arm
BINARY_RE = re.compile(r"v02(\d+)")
SWAPIN_RE = re.compile(r"adapt swap-in.*?([0-9.]+) ms/slot")
RR_MAX_RE = re.compile(r"RERANK_MAX=(\d+)")
TAG_RE = re.compile(r"PHASE_TAG=(\d)")


def boot_rows():
    rows = []
    for log in sorted(LOGS.glob("engine-*.log"), key=lambda p: p.stat().st_mtime):
        tag = log.stem.removeprefix("engine-")
        raw = log.read_bytes().replace(b"\x00", b"").decode("utf-8", "replace")
        ms = [float(m) for m in SWAPIN_RE.findall(raw)]
        if not ms and "serve" not in raw:
            continue
        v = BINARY_RE.search(tag)
        rows.append({
            "tag": tag,
            "ver": "v02" + v.group(1) if v else "?",
            "ms_all": ms,
            "n": len(ms),
        })
    return rows


def panel_warm(out_dir: Path):
    """Best-effort: newest warm-leg record json in an out dir."""
    best = None
    for rec in sorted(out_dir.rglob("*warm*.json"), key=lambda p: p.stat().st_mtime):
        try:
            d = json.loads(rec.read_text())
        except Exception:
            continue
        if not isinstance(d, dict) or "walls" not in d or "mem" not in d:
            # records are wrapper dirs X.json/X.json — try inner
            continue
        best = (rec, d)
    return best


def main():
    logs = [Path(a) for a in sys.argv[1:]] or None
    rows = boot_rows()
    print(f"{'log tag':<34} {'ver':<6} {'n':>4} {'ms/slot p50':>11} {'p90':>6} {'min':>6} {'max':>6}")
    for r in rows:
        ms = sorted(r["ms_all"])
        if not ms:
            print(f"{r['tag']:<34} {r['ver']:<6} {0:>4} {'-':>11}")
            continue
        p50 = ms[len(ms) // 2]
        p90 = ms[int(len(ms) * 0.9)]
        print(f"{r['tag']:<34} {r['ver']:<6} {r['n']:>4} {p50:>11.2f} {p90:>6.2f} {ms[0]:>6.2f} {ms[-1]:>6.2f}")

    # warm-leg walls + RSS from every out dir's records
    print("\nwarm-leg records (wall_s, max_rss GiB, min_avail GiB, swap GiB):")
    for out_dir in sorted(OUT.glob("*"), key=lambda p: p.stat().st_mtime):
        if not out_dir.is_dir():
            continue
        for rec in sorted(out_dir.rglob("*warm*.json"), key=lambda p: p.stat().st_mtime):
            try:
                d = json.loads(rec.read_text())
            except Exception:
                continue
            if not isinstance(d, dict) or "walls" not in d or "mem" not in d:
                continue
            m = d["mem"]
            print(f"  {out_dir.name:<28} {rec.parent.name:<28} wall {d['wall_s']:>7.1f}s  rss {m['max_rss_mib']/1024:>5.1f}G  avail {m['min_avail_mib']/1024:>5.1f}G  swap {m['max_swap_used_mib']/1024:>5.1f}G")


if __name__ == "__main__":
    main()
