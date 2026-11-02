#!/usr/bin/env python3
"""Read-only consistency audit of full strata.p2_row_measure.v2 reports.

Strict JSON only: duplicate keys and nonfinite numbers are refused. Legacy v1
reports are not recovered, repaired or rewritten; absent energies cannot be
reconstructed from l2_rel. Summary-only reports cannot be audited without rows.

Checks retained rows, declared coverage, both populations, every percentile and
bucket, and energy-weighted NMSE. This checks a document against itself, not the
measurement, reference backend or model quality. No numerical acceptance limit
or pass/fail verdict is applied. Exit: 0 consistent, 2 audit error, 3 file error.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
import sys
from typing import Any

PROG = "p2_report_audit.py"
SCHEMA_V2 = "strata.p2_row_measure.v2"
DIFF_KEYS = ("max_abs", "row_amax", "max_rel", "mean_abs", "l2_rel", "a_amax", "b_amax")
STAT_KEYS = ("p50", "p90", "p99", "p999", "max", "mean")
COUNT_KEYS = ("rows_expected", "rows_measured", "rows_skipped", "rows_retained", "recompose_retained")


class AuditError(Exception):
    """The report cannot be audited as the document it claims to be."""


def _object(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise AuditError(f"{where}: expected an object")
    return value


def _array(value: Any, where: str) -> list:
    if not isinstance(value, list):
        raise AuditError(f"{where}: expected an array")
    return value


def _integer(value: Any, where: str, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= (1 << 63) - 1:
        raise AuditError(f"{where}: expected an integer >= {minimum} within int64")
    return value


def _boolean(value: Any, where: str) -> bool:
    if type(value) is not bool:
        raise AuditError(f"{where}: expected a boolean")
    return value


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise AuditError(f"{where}: expected a nonempty string")
    return value


def _number(value: Any, where: str, nullable: bool = False) -> Any:
    if value is None and nullable:
        return None
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise AuditError(f"{where}: expected a finite nonnegative number" + (" or null" if nullable else ""))
    return value


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuditError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _refuse_constant(name: str) -> Any:
    raise AuditError(f"nonfinite JSON token {name!r}")


def _parse_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise AuditError(f"JSON number overflows finite float: {text!r}")
    return value


def percentile(values: list, q: float) -> Any:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(q * (len(ordered) - 1))))]


def stats(values: list) -> dict:
    result = {"count": len(values)}
    if not values:
        return dict(result, **dict.fromkeys(STAT_KEYS, 0.0))  # preserve the emitter's empty Agg defaults
    if any(value is None for value in values):
        return dict(result, **dict.fromkeys(STAT_KEYS))
    ordered = sorted(values)
    for q, key in zip((0.5, 0.9, 0.99, 0.999), STAT_KEYS):
        result[key] = ordered[math.ceil(q * (len(ordered) - 1))]
    result["max"] = ordered[-1]
    result["mean"] = sum(values) / len(values)
    return result


def _close(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right
    # Serialization consistency only (the emitter retains 12 significant digits), not an accuracy band.
    return math.isclose(left, right, rel_tol=1e-8, abs_tol=1e-300)


def _diff(value: Any, where: str, active: bool) -> dict:
    diff = _object(value, where)
    if not {"n", "nonfinite", "ref_zero", "sum_sq_err", "sum_sq_ref", *DIFF_KEYS} <= diff.keys():
        raise AuditError(f"{where}: missing row measurement fields")
    n = _integer(diff.get("n"), f"{where}.n", 1 if active else 0)
    nonfinite = _boolean(diff.get("nonfinite"), f"{where}.nonfinite")
    ref_zero = _boolean(diff.get("ref_zero"), f"{where}.ref_zero")
    for key in DIFF_KEYS:
        number = _number(diff.get(key), f"{where}.{key}", nullable=nonfinite)
        if nonfinite and number is not None:
            raise AuditError(f"{where}.{key}: nonfinite input must not report a finite metric")
    error = _number(diff.get("sum_sq_err"), f"{where}.sum_sq_err", nullable=nonfinite)
    reference = _number(diff.get("sum_sq_ref"), f"{where}.sum_sq_ref", nullable=nonfinite)
    if nonfinite and error is not None:
        raise AuditError(f"{where}: nonfinite input must have null error energy")
    if ref_zero != (reference == 0):
        raise AuditError(f"{where}.ref_zero contradicts reference energy")
    if not active and (n != 0 or nonfinite or error != 0 or reference != 0 or
                       any(diff[key] != 0 for key in DIFF_KEYS)):
        raise AuditError(f"{where}: inactive arm must be the empty zero-length record")
    return diff


def _nmse_from(diffs: list) -> dict:
    result = {"rows": len(diffs), "rows_ref_zero": sum(d["ref_zero"] for d in diffs),
              "rows_nonfinite": sum(d["nonfinite"] for d in diffs),
              "energy_error": 0.0, "energy_ref": 0.0, "nmse": None, "state": "empty"}
    if not diffs:
        return result
    if result["rows_nonfinite"]:
        result.update(state="invalid_nonfinite_rows", energy_error=None, energy_ref=None)
        return result
    error = sum(d["sum_sq_err"] for d in diffs)
    reference = sum(d["sum_sq_ref"] for d in diffs)
    ratio = error / reference if reference else None
    if not math.isfinite(error) or not math.isfinite(reference) or (ratio is not None and not math.isfinite(ratio)):
        result.update(state="invalid_energy", energy_error=None, energy_ref=None)
        return result
    result.update(energy_error=error, energy_ref=reference)
    if reference == 0:
        result["state"] = "zero_reference_nonzero_error_unbounded" if error else "zero_reference_exact_match"
        result["nmse"] = None if error else 0.0
    else:
        result.update(state="ok", nmse=ratio)
    return result


def _compare_stats(emitted: Any, expected: dict, where: str, checks: list) -> None:
    emitted = _object(emitted, where)
    if set(emitted) != {"count", *STAT_KEYS}:
        raise AuditError(f"{where}: incomplete or unknown aggregate fields")
    if _integer(emitted["count"], f"{where}.count") != expected["count"]:
        raise AuditError(f"{where}.count differs from retained rows")
    for key in STAT_KEYS:
        value = _number(emitted[key], f"{where}.{key}", nullable=expected[key] is None)
        if not _close(value, expected[key]):
            raise AuditError(f"{where}.{key}: emitted {value} != recomputed {expected[key]}")
        checks.append({"field": f"{where}.{key}", "match": True})


def audit(text: str, path: str) -> dict:
    try:
        top = json.loads(text, parse_constant=_refuse_constant, parse_float=_parse_float,
                         object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as exc:
        raise AuditError(f"{path}: strict JSON parse failed: {exc}; legacy reports are not recovered") from exc
    top = _object(top, "report")
    if top.get("schema") != SCHEMA_V2:
        raise AuditError(f"unsupported schema {top.get('schema')!r}; legacy reports are not recovered")
    if _boolean(top.get("summary_only"), "summary_only"):
        raise AuditError("summary-only report has no retained population to audit")
    _number(top.get("eps"), "eps")
    window = _array(top.get("layer_window"), "layer_window")
    if len(window) != 2:
        raise AuditError("layer_window: expected two bounds")
    lo, hi = (_integer(value, "layer_window") for value in window)
    if hi < lo or hi - lo >= 4096:
        raise AuditError("layer_window: reversed or too wide to audit")

    counts = _object(top.get("counts"), "counts")
    for key in COUNT_KEYS:
        _integer(counts.get(key), f"counts.{key}")
    rows = _array(top.get("rows"), "rows")
    rec = _array(top.get("recompose"), "recompose")
    if counts["rows_retained"] != len(rows) or counts["rows_measured"] != len(rows):
        raise AuditError("counts.rows_retained/rows_measured differ from retained row entries")
    if counts["recompose_retained"] != len(rec):
        raise AuditError("counts.recompose_retained differs from retained entries")
    if counts["rows_expected"] != counts["rows_measured"] + counts["rows_skipped"]:
        raise AuditError("counts.rows_expected != rows_measured + rows_skipped")

    formats = {}
    type_names = {}
    for index, value in enumerate(_array(top.get("formats"), "formats")):
        entry = _object(value, f"formats[{index}]")
        layer = _integer(entry.get("layer"), "formats.layer")
        gu = _integer(entry.get("gu_type"), "formats.gu_type")
        down = _integer(entry.get("d_type"), "formats.d_type")
        for number, key in ((gu, "gu"), (down, "d")):
            name = _string(entry.get(key), f"formats.{key}")
            if number in type_names and type_names[number] != name:
                raise AuditError(f"format type {number}: contradictory names")
            type_names[number] = name
        key = (layer, gu, down)
        if key in formats or not lo <= layer <= hi:
            raise AuditError("formats: duplicate entry or layer outside window")
        formats[key] = f"{entry['gu']}+{entry['d']}"

    fixture_info = {}
    for value in _array(top.get("fixtures"), "fixtures"):
        entry = _object(value, "fixtures[]")
        name = _string(entry.get("id"), "fixtures.id")
        if name in fixture_info:
            raise AuditError(f"duplicate fixture {name!r}")
        _integer(entry.get("tokens"), "fixtures.tokens")
        fixture_info[name] = _integer(entry.get("rows"), "fixtures.rows")

    groups = defaultdict(list)
    populations = {"mmq_vs_helper": [], "mmq_arm_vs_mmq": []}
    fixture_counts = Counter()
    format_counts = Counter()
    measured_layers = set()
    for section, entries, side, population in (("rows", rows, "a_vs_b", "mmq_vs_helper"),
                                                ("recompose", rec, "arm_vs_a", "mmq_arm_vs_mmq")):
        seen = set()
        for index, value in enumerate(entries):
            where = f"{section}[{index}]"
            row = _object(value, where)
            fixture = _string(row.get("fixture"), f"{where}.fixture")
            for key in ("fixture_index", "layer", "expert", "token", "pair", "gu_type", "d_type"):
                _integer(row.get(key), f"{where}.{key}")
            if fixture not in fixture_info or not lo <= row["layer"] <= hi:
                raise AuditError(f"{where}: undeclared fixture or layer outside window")
            format_pair = formats.get((row["layer"], row["gu_type"], row["d_type"]))
            if format_pair is None:
                raise AuditError(f"{where}: undeclared layer/format pair")
            identity = tuple(row[key] for key in ("fixture", "layer", "expert", "token", "pair"))
            if identity in seen:
                raise AuditError(f"{where}: duplicate retained row")
            seen.add(identity)
            diff = _diff(row.get(side), f"{where}.{side}", True)
            populations[population].append(diff)
            if section == "rows":
                has_arm = _boolean(row.get("has_arm"), f"{where}.has_arm")
                _diff(row.get("arm_vs_a"), f"{where}.arm_vs_a", has_arm)
                fixture_counts[fixture] += 1
                format_counts[format_pair] += 1
                measured_layers.add(row["layer"])
                keys = (f"layer:{row['layer']}", f"format:{format_pair}", f"fixture:{fixture}")
            else:
                keys = (f"recompose-layer:{row['layer']}", f"recompose:{format_pair}")
            for key in keys:
                groups[key].append(diff)
    if any(fixture_counts[name] != count for name, count in fixture_info.items()):
        raise AuditError("fixtures.rows differs from the retained fixture population")

    aggregate = _object(top.get("aggregate"), "aggregate")
    nmse = _object(top.get("nmse"), "nmse")
    percentiles, diagnostics, checks = {}, {}, []
    for population, diffs in populations.items():
        emitted = _object(aggregate.get(population), f"aggregate.{population}")
        percentiles[population] = {}
        for metric in ("max_abs", "max_rel"):
            expected = stats([diff[metric] for diff in diffs])
            _compare_stats(emitted.get(metric), expected, f"{population}.{metric}", checks)
            percentiles[population][metric] = expected
        expected = _nmse_from(diffs)
        emitted = _object(nmse.get(population), f"nmse.{population}")
        if not set(expected) <= emitted.keys():
            raise AuditError(f"nmse.{population}: missing diagnostic fields")
        for key in ("rows", "rows_ref_zero", "rows_nonfinite"):
            if _integer(emitted.get(key), f"nmse.{population}.{key}") != expected[key]:
                raise AuditError(f"nmse.{population}.{key} differs from retained rows")
        if emitted.get("state") != expected["state"]:
            raise AuditError(f"nmse.{population}.state differs from retained rows")
        for key in ("energy_error", "energy_ref", "nmse"):
            value = _number(emitted.get(key), f"nmse.{population}.{key}", nullable=expected[key] is None)
            if not _close(value, expected[key]):
                raise AuditError(f"nmse.{population}.{key} differs from retained energies")
        prefix = "rows[].a_vs_b" if population == "mmq_vs_helper" else "recompose[].arm_vs_a"
        if emitted.get("population") != prefix:
            raise AuditError(f"nmse.{population}.population names the wrong rows")
        expected["reference"] = _string(emitted.get("reference"), f"nmse.{population}.reference")
        diagnostics[population] = expected

    buckets = {}
    for value in _array(top.get("buckets"), "buckets"):
        entry = _object(value, "buckets[]")
        key = _string(entry.get("key"), "buckets.key")
        if key in buckets:
            raise AuditError(f"duplicate bucket {key!r}")
        buckets[key] = entry
    if set(buckets) != set(groups):
        raise AuditError("buckets: missing or undeclared retained-population group")
    for key, diffs in groups.items():
        for metric in ("max_abs", "max_rel"):
            _compare_stats(buckets[key].get(metric), stats([d[metric] for d in diffs]),
                           f"bucket[{key}].{metric}", checks)

    return {"path": path, "mode": "v2 (strict JSON; no legacy recovery)", "schema": SCHEMA_V2,
            "coverage": {"layer_window": window, "layers_measured": sorted(measured_layers),
                         "layers_zero_rows": [layer for layer in range(lo, hi + 1) if layer not in measured_layers],
                         "format_pairs": dict(format_counts), "fixtures": dict(fixture_counts),
                         "format_pairs_zero_rows": sorted(set(formats.values()) - set(format_counts)),
                         "counts": counts}, "percentiles": percentiles, "aggregate_checks": checks,
            "nmse": {"available": True, "populations": diagnostics},
            "verdict": "none: document consistency only; no numerical acceptance limit or pass/fail"}


def _fmt(value: Any) -> str:
    return "-" if value is None else f"{value:.12g}"


def print_report(result: dict) -> None:
    coverage = result["coverage"]
    print(f"report          : {result['path']}")
    print(f"parse mode      : {result['mode']}")
    print(f"layer window    : {coverage['layer_window']}; layers with rows: {len(coverage['layers_measured'])}")
    print(f"layers 0 rows   : {coverage['layers_zero_rows']}")
    print(f"format pairs    : {coverage['format_pairs']}; zero rows: {coverage['format_pairs_zero_rows']}")
    print(f"fixtures        : {coverage['fixtures']}")
    print(f"counts          : {coverage['counts']}")
    print(f"aggregate checks: {len(result['aggregate_checks'])} emitted-vs-recomputed fields, all equal")
    for population, metrics in result["percentiles"].items():
        for metric, values in metrics.items():
            print(f"percentiles {population} {metric}: n {values['count']} p99.9 {_fmt(values['p999'])} "
                  f"max {_fmt(values['max'])}")
    for population, entry in result["nmse"]["populations"].items():
        print(f"nmse {population}: state {entry['state']} nmse {_fmt(entry['nmse'])} "
              f"energy_error {_fmt(entry['energy_error'])} / energy_ref {_fmt(entry['energy_ref'])}; "
              f"rows {entry['rows']}, ref-zero {entry['rows_ref_zero']}, nonfinite {entry['rows_nonfinite']}; "
              f"reference {entry['reference']}")
    print(f"verdict         : {result['verdict']}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__.splitlines()[0])
    parser.add_argument("report", help="full v2 report (read-only; legacy is not recovered)")
    parser.add_argument("--json-out", help="write the audit result to a NEW file, never overwrite")
    args = parser.parse_args(argv)
    try:
        with open(args.report, "rb") as file:
            text = file.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"{PROG}: cannot read {args.report}: {exc}", file=sys.stderr)
        return 3
    try:
        result = audit(text, args.report)
    except AuditError as exc:
        print(f"{PROG}: AUDIT ERROR {exc}", file=sys.stderr)
        return 2
    print_report(result)
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
