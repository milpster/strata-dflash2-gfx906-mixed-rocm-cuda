#!/usr/bin/env python3
"""ab9 collector: parses the per-leg log slices, derives the SPEC-ab9 table.

Reads <dir>/<arm>-<leg>.json leg records (engine/helper log byte offsets baked in by
ab9_driver.py), opens ONLY the sliced byte ranges, and extracts:
  engine: judge line  prompt N tokens = R reused + K read in T ms (P tok/s)
          claim line  rpcprofile: claim layer= rows= sel= d2h_us= begin_us= reply_us=
                      img_kb= reply_kb= fallback=
          dbg line    prefill helper: layer N, M rows (helper|cpu fallback)
          confound    "below 512 MiB"
  helper: serve line  rpcprofile: serve tier=p layer= rows= queue_us= lock_us= reply_us= rows_kb=
  dmon:   rxpci/txpci MB/s (median + p90) from the sidecar file.

Derived per (ctx, arm):
  transfer_share        = (Σbegin_us + Σreply_us + Σd2h_us) / prompt_wall
  helper_side           = Σlock_us + Σqueue_us   vs engine-observed Σreply_us
  gain                  = PP(B)/PP(A) - 1        (per ctx)
  pipelining_recovery   = (PP(C)-PP(B)) / (PP(A)-PP(B))
  rows_per_claim        = Σrows / claims
  fixed_cost_per_claim  = (Σbegin_us + Σreply_us) / claims
Stage-2 arms get drift_vs_C = PP(arm)/PP(C) (gate .9..1.1).

Modes:
  --gates  exit 7 if participation counts (DBG lines) mismatch across B/C per ctx (prints
           both lists); exit 8 if the 8k parity sha256 differs across arms; prints
           sub-512-MiB leg flags (gate 6, non-fatal). exit 0 when gates pass.
  default  write --tsv + --md (results table with the derived columns).
"""
import argparse
import json
import pathlib
import re
import statistics
import subprocess
import sys

JUDGE = re.compile(r"prompt (\d+) tokens = (\d+) reused \+ (\d+) read in ([\d.]+) ms \(([\d.]+) tok/s\)")
CLAIM = re.compile(r"rpcprofile: claim layer=(\d+) rows=(\d+) sel=(\d+) d2h_us=(\d+) begin_us=(\d+) "
                   r"reply_us=(\d+) img_kb=(\d+) reply_kb=(\d+) fallback=(\d+)")
SERVE = re.compile(r"rpcprofile: serve tier=p layer=(\d+) rows=(\d+) queue_us=(\d+) lock_us=(\d+) "
                   r"reply_us=(\d+) rows_kb=(\d+)")
DBG = re.compile(r"prefill helper: layer \d+, \d+ rows \((helper|cpu fallback)\)")
LOW_VRAM = "below 512 MiB"

STAGE1 = ["A", "B", "C"]


def read_slice(path: str, a: int, b: int) -> str:
    p = pathlib.Path(path)
    if not p.exists():
        return ""
    with open(p, "rb") as f:
        f.seek(a)
        return f.read(max(0, b - a)).decode("utf-8", errors="replace")


def dmon_stats(path: str | None) -> dict:
    """median/p90 of rxpci/txpci MB/s -> GB/s (decimal) from a 'nvidia-smi dmon' sidecar."""
    out: dict[str, float] = {}
    if not path or not pathlib.Path(path).exists():
        return out
    lines = pathlib.Path(path).read_text(errors="replace").splitlines()
    header_i = next((i for i, l in enumerate(lines) if l.startswith("# gpu")), None)
    if header_i is None:
        return out
    # dmon prints the column names prefixed by '#': the data rows carry ONE FEWER token than
    # split() of the header, so drop the '#' and align 'gpu' with the leading id field.
    cols = lines[header_i].split()[1:]
    rows = []
    for l in lines[header_i + 2:]:
        parts = l.split()
        if len(parts) == len(cols) and all(re.fullmatch(r"-?\d+", x) for x in parts[1:]):
            rows.append(dict(zip(cols, (int(x) for x in parts))))
    for name in ("rxpci", "txpci"):
        vals = sorted(r[name] for r in rows if name in r)
        if vals:
            out[f"{name}_gbps_median"] = round(statistics.median(vals) / 1000, 4)
            out[f"{name}_gbps_p90"] = round(vals[min(len(vals) - 1, int(len(vals) * 0.9))] / 1000, 4)
    return out


def git(*args: str) -> str:
    root = pathlib.Path(__file__).resolve().parents[3]
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True).stdout.strip()


def collect(dirpath: pathlib.Path) -> dict:
    legs = []
    for f in sorted(dirpath.glob("*.json")):
        rec = json.loads(f.read_text())
        if rec.get("kind") == "warm":
            warm = rec | {"elog_slice": read_slice(rec["elog"], rec["elog_start"], rec["elog_end"]),
                          "hlog_slice": read_slice(rec["hlog"], rec["hlog_start"], rec["hlog_end"])}
            legs.append(warm)
            continue
        rec["elog_slice"] = read_slice(rec["elog"], rec["elog_start"], rec["elog_end"])
        rec["hlog_slice"] = read_slice(rec["hlog"], rec["hlog_start"], rec["hlog_end"])
        rec["dmon"] = dmon_stats(str(dirpath / f"{rec['arm']}-{rec['leg']}-dmon.txt"))
        legs.append(rec)
    return {"legs": legs}


def agg(legs: list[dict], arm: str, ctx: int, kind: str = "prefill") -> dict | None:
    sel = [l for l in legs if l["arm"] == arm and l["kind"] == kind and l["ctx"] == ctx]
    if not sel:
        return None
    pp: list[float] = []
    reused_bad = 0
    claims = rows = img_kb = reply_kb = d2h = begin = reply = q = lock = hlock_reply = 0
    low = 0
    for l in sel:
        for m in JUDGE.finditer(l["elog_slice"]):
            if int(m.group(2)) == 0:
                pp.append(float(m.group(5)))
            else:
                reused_bad += 1
        for m in CLAIM.finditer(l["elog_slice"]):
            claims += 1
            rows += int(m.group(2))
            d2h += int(m.group(4))
            begin += int(m.group(5))
            reply += int(m.group(6))
            img_kb += int(m.group(7))
            reply_kb += int(m.group(8))
        for m in SERVE.finditer(l["hlog_slice"]):
            q += int(m.group(3))
            lock += int(m.group(4))
            hlock_reply += int(m.group(5))
        low += l["elog_slice"].count(LOW_VRAM)
    wall = sum(l["wall_s"] for l in sel)
    pp_med = statistics.median(pp) if pp else 0.0
    pp_spread = (max(pp) - min(pp)) if len(pp) > 1 else 0.0
    n_claims = claims or 1
    out = {
        "arm": arm, "ctx": ctx, "legs": len(sel), "pp_med": pp_med,
        "pp_spread": round(pp_spread, 2), "fresh_judge_ok": reused_bad == 0 and len(pp) > 0,
        "claims": claims, "rows": rows, "img_kb": img_kb, "reply_kb": reply_kb,
        "sum_d2h_us": d2h, "sum_begin_us": begin, "sum_reply_us": reply,
        "sum_lock_us": lock, "sum_queue_us": q, "sum_hreply_us": hlock_reply,
        "wall_s": round(wall, 2),
        "transfer_share": round((begin + reply + d2h) / (wall * 1e6), 6) if wall else 0.0,
        "helper_side_us": lock + q, "engine_side_us": reply,
        "rows_per_claim": round(rows / n_claims, 2),
        "fixed_cost_per_claim_us": round((begin + reply) / n_claims, 1),
        "low_vram_lines": low,
        "rxpci_gbps_median": max((l.get("dmon", {}).get("rxpci_gbps_median", 0.0) for l in sel), default=0.0),
        "txpci_gbps_median": max((l.get("dmon", {}).get("txpci_gbps_median", 0.0) for l in sel), default=0.0),
        "rxpci_gbps_p90": max((l.get("dmon", {}).get("rxpci_gbps_p90", 0.0) for l in sel), default=0.0),
        "txpci_gbps_p90": max((l.get("dmon", {}).get("txpci_gbps_p90", 0.0) for l in sel), default=0.0),
    }
    return out


def build_table(data: dict) -> tuple[list[dict], list[dict]]:
    legs = data["legs"]
    rows: list[dict] = []
    for ctx in sorted({l["ctx"] for l in legs if l["kind"] == "prefill"}):
        pp = {}
        for arm in STAGE1 + [a for a in {l["arm"] for l in legs} if a.startswith("m")]:
            a = agg(legs, arm, ctx)
            if not a:
                continue
            pp[arm] = a["pp_med"]
            if arm == "B" and "A" in pp and pp["A"]:
                a["gain_vs_A"] = round(pp["B"] / pp["A"] - 1, 4)
            if arm == "C" and {"A", "B"} <= pp.keys() and pp["A"] != pp["B"]:
                a["pipelining_recovery"] = round((pp["C"] - pp["B"]) / (pp["A"] - pp["B"]), 4)
            if arm.startswith("m") and pp.get("C"):
                a["drift_vs_C"] = round(a["pp_med"] / pp["C"], 4)
            rows.append(a)
    tgs = [l for l in legs if l["kind"] == "tg"]
    return rows, tgs


TSV_COLS = ["arm", "ctx", "legs", "pp_med", "pp_spread", "fresh_judge_ok", "claims", "rows",
            "img_kb", "reply_kb", "sum_d2h_us", "sum_begin_us", "sum_reply_us", "sum_lock_us",
            "sum_queue_us", "wall_s", "transfer_share", "helper_side_us", "engine_side_us",
            "rows_per_claim", "fixed_cost_per_claim_us", "gain_vs_A", "pipelining_recovery",
            "drift_vs_C", "rxpci_gbps_median", "txpci_gbps_median", "rxpci_gbps_p90",
            "txpci_gbps_p90", "low_vram_lines"]


def gates(data: dict) -> int:
    legs = data["legs"]
    # gate 3: participation identity — DBG helper-row lines per (ctx) equal across B and C
    counts: dict[tuple[str, int], int] = {}
    for l in legs:
        if l.get("kind") != "prefill":
            continue
        counts[(l["arm"], l["ctx"])] = counts.get((l["arm"], l["ctx"]), 0) + len(DBG.findall(l["elog_slice"]))
    mismatch = False
    print("gate3 participation (DBG helper lines per arm+ctx):")
    for (arm, ctx), n in sorted(counts.items()):
        print(f"  {arm} ctx={ctx}: {n}")
    for ctx in {c for _, c in counts}:
        b, c = counts.get(("B", ctx), 0), counts.get(("C", ctx), 0)
        if b != c:
            print(f"GATE3 FAIL ctx={ctx}: B={b} C={c}")
            mismatch = True
    a_lines = sum(v for (arm, _), v in counts.items() if arm == "A")
    if a_lines:
        print(f"GATE3 FAIL: arm A (helper off) shows {a_lines} helper lines")
        mismatch = True
    if mismatch:
        return 7
    # gate 4: byte parity — FIRST 8k prefill leg per arm (seq 1), temp 0, thinking off
    shas = {}
    for l in legs:
        if l.get("kind") == "prefill" and l["ctx"] == 8000 and l["leg"].startswith("8000-1"):
            shas[l["arm"]] = l["sha256"]
    print(f"gate4 parity shas (8k, first leg): {shas}")
    if len(set(shas.values())) > 1:
        print("GATE4 FAIL: completion sha256 differs across arms - abort the perf verdict")
        return 8
    # gate 6: sub-512 MiB flags (non-fatal, printed as confounds)
    flags = [(l["arm"], l["leg"], l["elog_slice"].count(LOW_VRAM)) for l in legs if l.get("kind") == "prefill"]
    flags = [f for f in flags if f[2]]
    print("gate6 sub-512-MiB VRAM flags:", flags if flags else "none")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--gates", action="store_true")
    ap.add_argument("--tsv")
    ap.add_argument("--md")
    a = ap.parse_args()
    data = collect(pathlib.Path(a.dir))
    if a.gates:
        return gates(data)
    rows, tgs = build_table(data)
    cols = TSV_COLS
    tsv_path = a.tsv or str(pathlib.Path(a.dir) / "results.tsv")
    md_path = a.md or str(pathlib.Path(a.dir) / "results.md")
    with open(tsv_path, "w") as f:
        f.write("\t".join(cols) + "\n")
        for r in rows:
            f.write("\t".join(str(r.get(c, "")) for c in cols) + "\n")
    with open(md_path, "w") as f:
        f.write("# ab9 results\n\n")
        f.write(f"git: `{git('rev-parse','--abbrev-ref','HEAD')} {git('log','--oneline','-1')}`\n\n")
        uname = subprocess.run(["uname", "-r"], capture_output=True, text=True).stdout.strip()
        nv = subprocess.run("nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader",
                            shell=True, capture_output=True, text=True).stdout.strip()
        f.write(f"host: `{uname}`; nvidia: `{nv}`\n\n")
        f.write("| arm | ctx | legs | PP med | spread | claims | rows | Σbegin_us | Σreply_us | "
                "Σd2h_us | Σlock_us | Σqueue_us | wall_s | transfer_share | helper_side_us | "
                "rows/claim | fixed_cost/claim | gain | pipe_recovery | drift_vs_C | rxpci GB/s |\n")
        f.write("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        for r in rows:
            f.write("| {arm} | {ctx} | {legs} | {pp_med} | {pp_spread} | {claims} | {rows} | "
                    "{sum_begin_us} | {sum_reply_us} | {sum_d2h_us} | {sum_lock_us} | {sum_queue_us} | "
                    "{wall_s} | {transfer_share} | {helper_side_us} | {rows_per_claim} | "
                    "{fixed_cost_per_claim_us} | {gain_vs_A} | {pipelining_recovery} | {drift_vs_C} | "
                    "{rxpci_gbps_median} |\n".format(**{c: r.get(c, "") for c in TSV_COLS}))
        f.write("\n## TG sanity (cached rerun; walls = run1/run2 s)\n\n")
        for l in tgs:
            f.write(f"- {l['arm']} {l['leg']} ctx={l['ctx']} max_tokens={l['max_tokens']} walls={l['walls']}\n")
        f.write("\nFresh-judge invariant (0 reused on every fresh leg): "
                f"{all(r['fresh_judge_ok'] for r in rows)}\n")
        f.write(f"Confound flags (leg slices carrying 'below 512 MiB'): "
                f"{[(r['arm'], r['ctx'], r['low_vram_lines']) for r in rows if r['low_vram_lines']]}\n")
    print(f"wrote {tsv_path} and {md_path}; rows={len(rows)} tg-legs={len(tgs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
