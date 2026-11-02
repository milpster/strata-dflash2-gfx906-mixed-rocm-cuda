#!/usr/bin/env python3
"""Pool strictly audited strata.p2_row_measure.v2 reports into one verdict.

Each report is re-parsed with the same strict rules as p2_report_audit.py
(duplicate keys and nonfinite numbers are refused; no legacy recovery). A row
identity (fixture, fixture_index, layer, expert, token, pair) may enter the
pool exactly once, so two reports may declare overlapping layer windows as
long as their measured rows are disjoint (a report whose helper held only the
first layers declares the full window it asked for). Row percentiles use the
audit's method (sorted, index ceil(q*(n-1))); NMSE sums the row energies,
exactly like a single report would have. No acceptance limit is applied here:
the pooled numbers are printed and the caller keeps the gate. Exit: 0 pooled,
2 pool error, 3 file error.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Any

_HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "p2_measure")
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import p2_report_audit as audit  # noqa: E402  (same directory, no package)

PROG = "pool_p2_reports.py"


def _load(path: str) -> dict:
    try:
        with open(path, "rb") as file:
            text = file.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise FileNotFoundError(f"{PROG}: cannot read {path}: {exc}")
    try:
        top = json.loads(text, parse_constant=audit._refuse_constant,
                         parse_float=audit._parse_float,
                         object_pairs_hook=audit._unique_object)
    except (ValueError, RecursionError) as exc:
        raise audit.AuditError(f"{path}: strict JSON parse failed: {exc}")
    if not isinstance(top, dict) or top.get("schema") != audit.SCHEMA_V2:
        raise audit.AuditError(f"{path}: unsupported schema; legacy reports are not recovered")
    return top


def pool(paths: list) -> dict:
    per_report, seen_identity, measured_layers = [], set(), set()
    pooled: list = []
    for path in paths:
        top = _load(path)
        window = audit._array(top.get("layer_window"), f"{path}: layer_window")
        lo, hi = (audit._integer(v, f"{path}: layer_window") for v in window)
        rows = audit._array(top.get("rows"), f"{path}: rows")
        diffs, layers_here = [], set()
        for index, value in enumerate(rows):
            where = f"{path}: rows[{index}]"
            row = audit._object(value, where)
            fixture = audit._string(row.get("fixture"), f"{where}.fixture")
            for key in ("fixture_index", "layer", "expert", "token", "pair"):
                audit._integer(row.get(key), f"{where}.{key}")
            if not lo <= row["layer"] <= hi:
                raise audit.AuditError(f"{where}: layer outside the declared window")
            identity = (fixture, row["fixture_index"], row["layer"], row["expert"],
                        row["token"], row["pair"])
            if identity in seen_identity:
                raise audit.AuditError(f"{where}: row identity {identity} is already in "
                                       "the pool from an earlier report")
            seen_identity.add(identity)
            layers_here.add(row["layer"])
            diffs.append(audit._diff(row.get("a_vs_b"), f"{where}.a_vs_b", True))
        measured_layers |= layers_here
        pooled.extend(diffs)
        per_report.append({"path": path, "layer_window": [lo, hi],
                           "layers_measured": sorted(layers_here), "rows": len(diffs),
                           "stats": {m: audit.stats([d[m] for d in diffs]) for m in ("max_abs", "max_rel")},
                           "nmse": audit._nmse_from(diffs)})
    return {"reports": per_report, "rows_pooled": len(pooled),
            "layers_pooled": sorted(measured_layers),
            "stats": {m: audit.stats([d[m] for d in pooled]) for m in ("max_abs", "max_rel")},
            "nmse": audit._nmse_from(pooled)}


def _fmt(value: Any) -> str:
    return "-" if value is None else f"{value:.12g}"


def print_pool(result: dict) -> None:
    for report in result["reports"]:
        print(f"report : {report['path']} declared {report['layer_window'][0]}-"
              f"{report['layer_window'][1]} measured {report['layers_measured']} "
              f"rows {report['rows']} "
              f"rel p99.9 {_fmt(report['stats']['max_rel']['p999'])} "
              f"max {_fmt(report['stats']['max_rel']['max'])}")
    for metric, values in result["stats"].items():
        print(f"pooled {metric}: n {values['count']} p50 {_fmt(values['p50'])} "
              f"p90 {_fmt(values['p90'])} p99 {_fmt(values['p99'])} p99.9 {_fmt(values['p999'])} "
              f"max {_fmt(values['max'])} mean {_fmt(values['mean'])}")
    entry = result["nmse"]
    print(f"pooled nmse: state {entry['state']} nmse {_fmt(entry['nmse'])} "
          f"energy_error {_fmt(entry['energy_error'])} / energy_ref {_fmt(entry['energy_ref'])}; "
          f"rows {entry['rows']}, ref-zero {entry['rows_ref_zero']}, nonfinite {entry['rows_nonfinite']}")
    print(f"layers : {result['layers_pooled']}")
    print("verdict: none: pooled numbers only; the acceptance gate stays with the caller")


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__.splitlines()[0])
    parser.add_argument("reports", nargs="+", help="full v2 reports with disjoint layer windows")
    parser.add_argument("--json-out", help="write the pooled result to a NEW file, never overwrite")
    args = parser.parse_args(argv)
    try:
        result = pool(args.reports)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 3
    except audit.AuditError as exc:
        print(f"{PROG}: POOL ERROR {exc}", file=sys.stderr)
        return 2
    print_pool(result)
    if args.json_out:
        try:
            with open(args.json_out, "x", encoding="utf-8") as file:
                json.dump(result, file, indent=2, sort_keys=True, allow_nan=False)
                file.write("\n")
        except OSError as exc:
            print(f"{PROG}: cannot write {args.json_out}: {exc}", file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
