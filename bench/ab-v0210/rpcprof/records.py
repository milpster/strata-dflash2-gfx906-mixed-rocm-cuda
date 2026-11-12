#!/usr/bin/env python3
"""records.py — consolidated leg-metrics ledger + best-ever comparison.

Scans ab9_driver json legs under out/ dirs, extracts judge metrics from each
leg's elog byte range, and emits one consolidated table. With --update it
also maintains records.json (best-ever per leg+metric with full provenance)
and flags NEW RECORD / REGRESSION vs it. Deterministic prompts mean legs are
comparable across arms/windows ONLY when their sha256 matches — mismatches
are surfaced, never silently compared.

Usage:
  records.py [dirs ...]            # consolidated table (default: all out/*)
  records.py --update [dirs ...]   # also refresh records.json + verdicts
Comparability note: boot-order/disk-tier state is a known confound (the 80k
leg measured 56–244 tps across boots of identical config). The table keeps
per-arm rows so readers see the spread, not just the best.
"""
import json
import re
import sys
from pathlib import Path

RPC = Path(__file__).resolve().parent
OUT_ROOT = RPC / "out"
RECORDS = RPC / "records.json"

JUDGE = re.compile(
    r"prompt (\d+) tokens = (\d+) reused \+ (\d+)(?: of (\d+))? read in "
    r"(\d+) ms \(([\d.]+) tok/s\), (\d+) generated in (\d+) ms \(([\d.]+) tok/s\)"
)


def leg_metrics(rec: dict) -> dict:
    """Parse judge line(s) from the leg's elog byte range."""
    elog = Path(rec["elog"])
    if not elog.exists():
        return {}
    with elog.open("rb") as f:
        f.seek(rec["elog_start"])
        blob = f.read(rec["elog_end"] - rec["elog_start"]).replace(b"\0", b"").decode("utf-8", "replace")
    out = {}
    for m in JUDGE.finditer(blob):
        g = m.groups()
        row = {
            "prompt_tok": int(g[0]), "reused": int(g[1]), "read": int(g[2]),
            "read_ms": int(g[4]), "read_tps": float(g[5]),
            "gen_tok": int(g[6]), "gen_ms": int(g[7]), "gen_tps": float(g[8]),
        }
        key = f"{int(g[6])}gen" if int(g[6]) > 0 else "0gen"
        out.setdefault(key, []).append(row)
    return out


def load_legs(dirs):
    legs = []
    for d in dirs:
        for jf in sorted(d.glob("*.json")):
            name = jf.name
            if name.startswith("conf-"):
                continue
            try:
                rec = json.loads(jf.read_text())
            except (json.JSONDecodeError, OSError):
                continue  # in-flight window writing a partial line
            if "leg" not in rec or "wall_s" not in rec:
                continue
            legs.append({
                "dir": d.name, "arm": rec.get("arm", "?"), "leg": rec["leg"],
                "walls": rec.get("walls", []), "wall_s": rec["wall_s"],
                "sha": rec.get("sha256", "?")[:12], "judge": leg_metrics(rec),
            })
    return legs


def best_judge(judge: dict, key: str, field: str):
    rows = judge.get(key)
    if not rows:
        return None
    return max(r[field] for r in rows) if field.endswith("tps") else None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    update = "--update" in sys.argv
    dirs = [Path(a) for a in args] or sorted(p for p in OUT_ROOT.iterdir() if p.is_dir())
    legs = load_legs(dirs)
    old = json.loads(RECORDS.read_text()) if RECORDS.exists() else {}
    records = dict(old) if update else old
    verdicts = []

    print(f"{'leg':<22}{'arm':<18}{'wall_s':>8}  {'rd_tps':>7}{'gen_tps':>8}  sha        dir")
    for lg in sorted(legs, key=lambda x: (x["leg"], x["wall_s"])):
        rd = best_judge(lg["judge"], next(iter(lg["judge"]), ""), "read_tps") if lg["judge"] else None
        rd = max((best_judge(lg["judge"], k, "read_tps") or 0 for k in lg["judge"]), default=None)
        gen = max((best_judge(lg["judge"], k, "gen_tps") or 0 for k in lg["judge"]), default=None)
        print(f"{lg['leg']:<22}{lg['arm']:<18}{lg['wall_s']:>8.1f}  "
              f"{(f'{rd:7.1f}' if rd else '      -')}{(f'{gen:8.1f}' if gen else '       -')}  "
              f"{lg['sha']:<10} {lg['dir']}")

        if not update:
            continue
        for field, agg, better in (("wall_s", "min", "lower"), ("read_tps", "max", "higher"), ("gen_tps", "max", "higher")):
            val = {"wall_s": lg["wall_s"], "read_tps": rd, "gen_tps": gen}[field]
            if val is None:
                continue
            # records are sha-guarded classes: same leg name with a different
            # prompt sha is a different measurement class, never compared
            key = f"{lg['leg']}[{lg['sha']}]:{field}"
            cur = records.get(key)
            if not cur:
                records[key] = {"value": val, "arm": lg["arm"], "dir": lg["dir"], "sha": lg["sha"]}
                verdicts.append(f"FIRST {key} = {val:.1f} ({lg['arm']}/{lg['dir']})")
            else:
                is_better = val < cur["value"] - max(0.03 * cur["value"], 0.5) if better == "lower" \
                    else val > cur["value"] + max(0.03 * cur["value"], 0.5)
                if is_better:
                    verdicts.append(f"NEW RECORD {key}: {cur['value']:.1f} ({cur['arm']}/{cur['dir']}) -> {val:.1f} ({lg['arm']}/{lg['dir']})")
                    records[key] = {"value": val, "arm": lg["arm"], "dir": lg["dir"], "sha": lg["sha"]}
                elif better == "lower" and val > cur["value"] * 1.15:
                    verdicts.append(f"REGRESSION {key}: best {cur['value']:.1f} ({cur['arm']}/{lg['dir']}), this {val:.1f} ({lg['arm']})")

    if update:
        RECORDS.write_text(json.dumps(records, indent=1, sort_keys=True))
        print("\n--- verdicts (3% guard band, sha-guarded):")
        for v in verdicts or ["(none this run — holdings unchanged)"]:
            print(" ", v)


if __name__ == "__main__":
    main()
