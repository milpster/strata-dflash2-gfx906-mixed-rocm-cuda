#!/usr/bin/env python3
"""p2_report_audit.py - READ-ONLY audit of the P2 deviation harness's report files.

WHAT THIS IS: a narrow, reusable, file-only audit of a `p2_measure` report.  It opens the file, parses
it, and reports

  * the run's COVERAGE: the layers it measured, the (gate/up + down) format pairs it measured, the
    fixtures it measured, the rows each carries, and the layers/formats the run's own window names with
    ZERO rows (an empty plan stays visible as zero, never as covered),
  * the ORIGINAL per-row ABSOLUTE and RELATIVE percentiles, recomputed from the retained rows by the
    harness's pinned rule (ascending, index = ceil(q*(n-1)) clamped) and compared with the document's
    emitted aggregate and bucket numbers, so a corrupted row, a truncated row section, a mis-sorted
    bucket or a mismatched count is a typed ERROR and not a quiet difference,
  * the tensor-NMSE AVAILABILITY: v2 rows carry the per-row energies, so the audit recomputes
    sum(error energy)/sum(reference energy) and compares it with the emitted value; v1 rows carry no
    energies at all, so the audit prints `unavailable` and says so.

WHAT THIS IS NOT:
  * NOT a repair tool.  It never writes to, rewrites, normalizes or re-emits the file it audits.  The
    malformed v1 documents are retained raw evidence (numerical-acceptance.md line 16) and stay
    byte-unchanged; this script reads them, it does not convert them.
  * NOT a verifier of the measurement.  It compares the document with ITSELF (rows vs aggregates vs
    counts).  It has no row floats, no pack, no GPU, no helper.  It applies NO tolerance and prints NO
    verdict, and it never says a run passed.
  * NOT a legacy NMSE source.  The v1 `l2_rel` column is a per-row ratio (and 0 for a zero-reference
    row); an unweighted mean of it is a DIFFERENT number from sum(energy)/sum(energy).  The audit
    never derives a global NMSE from `l2_rel`, and says `unavailable` when the energies are absent.

FORMATS IT ACCEPTS
  strata.p2_row_measure.v2  - strict `json.loads`.  NaN/Infinity tokens are refused (a v2 document
                              prints them as `null` with a named state, so a bare `inf` is corruption).
  strata.p2_row_measure.v1  - the retained raw files are NOT valid JSON: the v1 bucket emitter wrote
                              `"max_abs": "v": {...}`.  The audit recovers them by STRICT SECTION
                              BOUNDARIES: each top-level section is decoded on its own, the row sections
                              entry by entry, and a v1 bucket entry is recovered from the label plus the
                              two aggregate objects that follow the `"v":` prefix.  Every span must
                              decode on its own and the leftovers must be empty; anything else is a
                              typed error naming the offset.  No rewrite, ever.

EXIT: 0 audit clean | 2 typed audit error | 3 usage / unreadable file
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

PROG = "p2_report_audit.py"
SCHEMA_V1 = "strata.p2_row_measure.v1"
SCHEMA_V2 = "strata.p2_row_measure.v2"

ROW_KEYS = ("fixture", "fixture_index", "layer", "expert", "token", "pair", "gu_type", "d_type")
DIFF_KEYS = ("max_abs", "row_amax", "max_rel", "mean_abs", "l2_rel", "a_amax", "b_amax")
ENERGY_KEYS = ("n", "sum_sq_err", "sum_sq_ref", "ref_zero", "nonfinite")
QS = (0.50, 0.90, 0.99, 0.999)


class AuditError(Exception):
    """A typed refusal: the file is not auditable as what it claims to be."""


def _refuse_constant(name: str) -> Any:
    raise AuditError(f"non-finite JSON token '{name}' in the report (a v2 document prints null)")


def _decoder() -> json.JSONDecoder:
    return json.JSONDecoder(parse_constant=_refuse_constant)


# ---------------- the percentile rule, pinned by the C++ header ----------------

def percentile(values: List[float], q: float) -> Optional[float]:
    """ascending sort, index = ceil(q*(n-1)) clamped to [0, n-1]; n == 0 -> None (no rows, never 0)."""
    if not values:
        return None
    s = sorted(values)
    f = q * (len(s) - 1)
    idx = max(0, min(len(s) - 1, math.ceil(f)))
    return s[idx]


def stats(values: List[float]) -> Dict[str, Any]:
    """The aggregate the harness emits: percentiles by the pinned rule, plus max and mean.  An
    aggregate over NO rows prints count 0 and zeros (the harness's own Agg defaults) - never a
    percentile of nothing."""
    if not values:
        return {"count": 0, "p50": 0.0, "p90": 0.0, "p99": 0.0, "p999": 0.0, "max": 0.0, "mean": 0.0}
    out: Dict[str, Any] = {"count": len(values)}
    for q, name in zip(QS, ("p50", "p90", "p99", "p999")):
        out[name] = percentile(values, q)
    out["max"] = max(values)
    out["mean"] = sum(values) / len(values)
    return out


# ---------------- strict span scanning ----------------

def _scan_value(text: str, at: int, what: str) -> Tuple[Any, int]:
    """Decode ONE JSON value at `at`; return (value, end).  A typed error when it does not decode."""
    try:
        value, end = _decoder().raw_decode(text, at)
    except AuditError:
        raise
    except Exception as exc:                                  # noqa: BLE001 - typed refusal below
        raise AuditError(f"{what}: no JSON value at offset {at}: {exc}") from exc
    return value, end


def _skip_ws(text: str, at: int) -> int:
    while at < len(text) and text[at] in " \t\r\n":
        at += 1
    return at


def _find_key(text: str, key: str) -> int:
    """The offset of a top-level `"key":` at the document's two-space indent, or -1."""
    m = re.search(rf'^  "{re.escape(key)}": ', text, re.M)
    return -1 if m is None else m.end()


def _top_level(text: str, skip: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """Decode every top-level section of a document by its own strict boundaries.  `skip` names
    sections the caller recovers itself (the v1 bucket array is not a JSON value at all)."""
    out: Dict[str, Any] = {}
    for m in re.finditer(r'^  "([a-z_0-9]+)": ', text, re.M):
        key, at = m.group(1), m.end()
        if key in skip:
            continue
        value, end = _scan_value(text, at, f"section '{key}'")
        nxt = _skip_ws(text, end)
        if nxt < len(text) and text[nxt] not in ",}":
            raise AuditError(f"section '{key}': trailing junk at offset {nxt}: {text[nxt:nxt+40]!r}")
        out[key] = value
    return out


def _split_array_entries(text: str, at: int, what: str) -> Tuple[List[str], int]:
    """Split the array that STARTS at `at` ('[') into its top-level entry texts (brace counting,
    string-aware).  Works on a malformed v1 bucket array: its entries are individually split, the
    entries themselves are decoded by the caller."""
    if at >= len(text) or text[at] != "[":
        raise AuditError(f"{what}: expected '[' at offset {at}")
    entries: List[str] = []
    i, depth, start, in_str, esc = at + 1, 0, None, False, False
    while i < len(text):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    entries.append(text[start:i + 1])
                    start = None
            elif c == "]" and depth == 0:
                return entries, i + 1
            elif c == "]" and depth > 0:
                raise AuditError(f"{what}: ']' inside an unbalanced entry at offset {i}")
        i += 1
    raise AuditError(f"{what}: array never closes")


# ---------------- the report's shape ----------------

def _row_dict(entry: Any, what: str, index: int, sides: Tuple[str, ...]) -> Dict[str, Any]:
    """One row of a row section.  A v2 document hands us a parsed dict; a v1 file hands us its own
    text, which must decode on its own and use ALL of itself."""
    if isinstance(entry, dict):
        row = entry
    else:
        row, end = _scan_value(entry, 0, f"{what} row #{index}")
        if _skip_ws(entry, end) != len(entry):
            raise AuditError(f"{what} row #{index}: trailing junk in its own text")
    if not isinstance(row, dict):
        raise AuditError(f"{what} row #{index}: not an object")
    for k in ROW_KEYS:
        if k not in row:
            raise AuditError(f"{what} row #{index}: key '{k}' missing")
    for k in sides:
        d = row.get(k)
        if not isinstance(d, dict):
            raise AuditError(f"{what} row #{index}: '{k}' is not an object")
        for dk in DIFF_KEYS:
            if dk not in d:
                raise AuditError(f"{what} row #{index}: '{k}.{dk}' missing")
    return row


def _check_finite(row: Dict[str, Any], where: str, sides: Tuple[str, ...]) -> None:
    """Every number the audit reads must be finite.  A v1 file with a non-finite number is
    corruption (strict parsing refused it already).  A v2 row that SAYS it is nonfinite must show it:
    at least one of its measures is null, and it never reports a full finite measurement."""
    for k in sides:
        d = row[k]
        for dk, dv in d.items():
            if isinstance(dv, bool) or dv is None:
                continue
            if isinstance(dv, (int, float)) and not math.isfinite(dv):
                raise AuditError(f"{where}: {k}.{dk} is non-finite ({dv})")
        if d.get("nonfinite") is True:
            shown = [d.get(x) for x in ("max_abs", "max_rel", "mean_abs", "l2_rel",
                                         "sum_sq_err", "sum_sq_ref")]
            if all(x is not None for x in shown):
                raise AuditError(f"{where}: {k} says nonfinite=true but reports a full finite "
                                 "measurement - a nonfinite row is never reported as agreement")
        elif "nonfinite" in d:
            for ek in ("max_abs", "max_rel", "mean_abs", "l2_rel"):
                if d.get(ek) is None:
                    raise AuditError(f"{where}: {k}.{ek} is null on a row not marked nonfinite")


def _nmse_from(rows: List[Dict[str, Any]], pop: str) -> Dict[str, Any]:
    """Recompute sum(err energy)/sum(ref energy) from the retained per-row energies, exactly the way
    the harness does: a nonfinite row REFUSES the whole population (its energies are NaN, so a partial
    sum would be a made-up number); a zero denominator with a zero numerator is a named 0; a zero
    denominator with a nonzero numerator is unbounded (null)."""
    for i, row in enumerate(rows):
        d = row["a_vs_b"] if pop == "a_vs_b" else row["arm_vs_a"]
        for k in ENERGY_KEYS:
            if k not in d:
                raise AuditError(f"{pop}: row #{i} carries no '{k}' - the energies are absent")
    out = {"population": pop, "rows": len(rows), "rows_ref_zero": 0, "rows_nonfinite": 0,
           "energy_error": 0.0, "energy_ref": 0.0, "nmse": None, "state": "empty"}
    if not rows:
        return out
    out["rows_nonfinite"] = sum(1 for r in rows
                                if r["a_vs_b" if pop == "a_vs_b" else "arm_vs_a"]["nonfinite"])
    if out["rows_nonfinite"]:
        out["state"] = "invalid_nonfinite_rows"
        return out
    num = 0.0
    den = 0.0
    for r in rows:
        d = r["a_vs_b" if pop == "a_vs_b" else "arm_vs_a"]
        if d["ref_zero"]:
            out["rows_ref_zero"] += 1
        num += float(d["sum_sq_err"])
        den += float(d["sum_sq_ref"])
    out["energy_error"] = num
    out["energy_ref"] = den
    if den == 0.0:
        if num == 0.0:
            out["state"] = "zero_reference_exact_match"
            out["nmse"] = 0.0
        else:
            out["state"] = "zero_reference_nonzero_error_unbounded"
        return out
    out["state"] = "ok"
    out["nmse"] = num / den
    return out


def _close(a: Optional[float], b: Optional[float], tol: float = 1e-6) -> bool:
    if a is None or b is None:
        return a is b
    if a == b:
        return True
    scale = max(abs(a), abs(b), 1e-300)
    return abs(a - b) <= tol * scale


def _percentile_report(rows: List[Dict[str, Any]], pop: str) -> Dict[str, Any]:
    """The percentiles recomputed from the retained rows.  A group whose rows carry null measures
    (v2 rows marked nonfinite) is NOT recomputable: the audit says so and compares nothing, rather
    than inventing finite percentiles out of a refused measurement."""
    vals = [r[pop] for r in rows]
    ab = [d["max_abs"] for d in vals]
    rel = [d["max_rel"] for d in vals]
    if any(v is None for v in ab + rel):
        n = len(rows)
        skip = {"count": n, "p50": None, "p90": None, "p99": None, "p999": None, "max": None,
                "mean": None, "recomputable": False,
                "why": "the group's rows carry null measures (nonfinite rows marked on the row)"}
        return {"max_abs": dict(skip), "max_rel": dict(skip), "recomputable": False}
    return {"max_abs": stats(ab), "max_rel": stats(rel), "recomputable": True}


def _group(rows: List[Dict[str, Any]], key_fn) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(key_fn(r), []).append(r)
    return out


def audit(text: str, path: str) -> Dict[str, Any]:
    m = re.search(r'^  "schema": "([^"]*)"', text, re.M)
    if not m:
        raise AuditError(f"{path}: no top-level \"schema\" key - not a p2_measure report")
    schema = m.group(1)
    if schema == SCHEMA_V1:
        top = _top_level_legacy(text)
        mode = "v1 legacy (strict section-boundary recovery; the file is NOT valid JSON)"
    elif schema == SCHEMA_V2:
        try:
            top = json.loads(text, parse_constant=_refuse_constant)
        except AuditError as exc:
            raise AuditError(f"{path}: {exc}") from exc
        except (json.JSONDecodeError, ValueError) as exc:
            # a v2 document must be valid JSON: a bare inf/nan token, a stray quote, a truncated tail
            raise AuditError(f"{path}: strict JSON parse failed ({type(exc).__name__}): {exc}") from exc
        mode = "v2 (strict json parse)"
    else:
        raise AuditError(f"{path}: schema {schema!r} is neither {SCHEMA_V1!r} nor {SCHEMA_V2!r}")
    return _audit_top(top, path, mode, schema == SCHEMA_V1)


def _top_level_legacy(text: str) -> Dict[str, Any]:
    """Decode a v1 report section by section, recovering the malformed bucket entries."""
    top = _top_level(text, skip=("buckets",))
    at = _find_key(text, "buckets")
    if at < 0:
        return top
    entries, _end = _split_array_entries(text, at, "buckets")
    recovered: List[Dict[str, Any]] = []
    for n, ent in enumerate(entries):
        m = re.match(r'^\{"key": "([^"]*)", "max_abs": "v": ', ent)
        if not m:
            raise AuditError(f"buckets entry #{n}: unsupported shape, not a recoverable v1 "
                             f"defect: {ent[:80]!r}")
        abs_agg, e1 = _scan_value(ent, m.end(), f"buckets entry #{n} max_abs")
        m2 = re.compile(r'^, "max_rel": "v": ').match(ent[e1:])
        if not m2:
            raise AuditError(f"buckets entry #{n}: the max_rel field is not where the v1 defect "
                             f"puts it (offset {e1})")
        rel_agg, e2 = _scan_value(ent, e1 + m2.end(), f"buckets entry #{n} max_rel")
        if _skip_ws(ent, e2) != len(ent) - 1 or ent[_skip_ws(ent, e2)] != "}":
            raise AuditError(f"buckets entry #{n}: trailing text after the two aggregates: "
                             f"{ent[e2:][:80]!r}")
        recovered.append({"key": m.group(1), "max_abs": abs_agg, "max_rel": rel_agg,
                          "recovered": "v1 bucket defect ('max_abs': 'v': {...})"})
    top["buckets"] = recovered
    top["_buckets_were_malformed"] = True
    return top


def _audit_top(top: Dict[str, Any], path: str, mode: str, v1: bool) -> Dict[str, Any]:
    counts = top.get("counts")
    if not isinstance(counts, dict):
        raise AuditError(f"{path}: the 'counts' section is missing")
    for k in ("rows_expected", "rows_measured", "rows_skipped", "rows_retained"):
        if not isinstance(counts.get(k), int):
            raise AuditError(f"{path}: counts.{k} is missing")
    summary_only = top.get("summary_only")
    rows = top.get("rows")
    recompose = top.get("recompose")
    if summary_only:
        rows = rows or []
        recompose = recompose or []
    if not isinstance(rows, list) or not isinstance(recompose, list):
        raise AuditError(f"{path}: the row sections are missing (a full run retains rows)")
    rows = [_row_dict(r, "rows", i, ("a_vs_b", "arm_vs_a")) for i, r in enumerate(rows)]
    rec = [_row_dict(r, "recompose", i, ("arm_vs_a",)) for i, r in enumerate(recompose)]
    for r in rows:
        _check_finite(r, f"rows[{r['fixture']}/L{r['layer']}/p{r['pair']}]", ("a_vs_b", "arm_vs_a"))
    for r in rec:
        _check_finite(r, f"recompose[{r['fixture']}/L{r['layer']}/p{r['pair']}]", ("arm_vs_a",))

    # the counts the document states must be the rows it carries
    problems: List[str] = []
    if len(rows) != counts["rows_retained"]:
        problems.append(f"counts.rows_retained {counts['rows_retained']} != {len(rows)} row entries")
    if len(rec) != counts["recompose_retained"]:
        problems.append(f"counts.recompose_retained {counts['recompose_retained']} != {len(rec)} "
                        f"recompose entries")
    if counts["rows_measured"] < len(rows):
        problems.append(f"counts.rows_measured {counts['rows_measured']} < retained {len(rows)}")
    if counts["rows_expected"] < counts["rows_measured"]:
        problems.append(f"counts.rows_expected {counts['rows_expected']} < rows_measured "
                        f"{counts['rows_measured']}")
    if counts["rows_skipped"] < 0:
        problems.append("counts.rows_skipped is negative")

    # the format names come from the document's OWN formats section (its own ggml_type_name output);
    # the table in _fmt_name is only the fallback for a type the section does not name.
    type_names: Dict[int, str] = {}
    for f in top.get("formats") or []:
        if isinstance(f, dict) and isinstance(f.get("gu_type"), int) and isinstance(f.get("gu"), str):
            type_names[int(f["gu_type"])] = str(f["gu"])
        if isinstance(f, dict) and isinstance(f.get("d_type"), int) and isinstance(f.get("d"), str):
            type_names[int(f["d_type"])] = str(f["d"])

    def fmt_pair(r: Dict[str, Any]) -> str:
        gu, d = int(r["gu_type"]), int(r["d_type"])
        for t in (gu, d):
            if t not in type_names:
                raise AuditError(f"{path}: row {r['fixture']}/L{r['layer']}/p{r['pair']} names "
                                 f"format type {t}, which the formats section does not declare")
        return f"{type_names[gu]}+{type_names[d]}"

    # coverage
    lo_hi = top.get("layer_window") or [None, None]
    layers_measured = sorted({int(r["layer"]) for r in rows})
    fmt_pairs: Dict[str, int] = {}
    for r in rows:
        key = fmt_pair(r)
        fmt_pairs[key] = fmt_pairs.get(key, 0) + 1
    fixtures: Dict[str, int] = {}
    for r in rows:
        fixtures[r["fixture"]] = fixtures.get(r["fixture"], 0) + 1
    window_layers = []
    if isinstance(lo_hi, list) and len(lo_hi) == 2 and all(isinstance(x, int) for x in lo_hi):
        window_layers = [l for l in range(lo_hi[0], lo_hi[1] + 1)]
    gap_layers = [l for l in window_layers if l not in layers_measured]

    # the ORIGINAL percentiles, recomputed from the retained rows
    per_pop = {"mmq_vs_helper": _percentile_report(rows, "a_vs_b")}
    if rec:
        per_pop["mmq_arm_vs_mmq"] = _percentile_report(rec, "arm_vs_a")
    emitted = top.get("aggregate") or {}
    checks: List[Dict[str, Any]] = []
    for pop, mine in per_pop.items():
        for metric in ("max_abs", "max_rel"):
            em = (emitted.get(pop) or {}).get(metric)
            if not isinstance(em, dict):
                problems.append(f"{pop}.{metric}: no emitted aggregate to compare with")
                continue
            if not mine.get("recomputable", True):
                checks.append({"pop": pop, "metric": metric, "skipped": mine.get("why"),
                               "emitted": em})
                continue
            for k, want in em.items():
                got = mine[metric].get(k)
                ok = _close(got, want)
                checks.append({"pop": pop, "metric": metric, "field": k,
                               "emitted": want, "recomputed": got, "match": ok})
                if not ok:
                    problems.append(f"{pop}.{metric}.{k}: emitted {want} != recomputed {got}")

    # the per-layer / per-format buckets: the document's numbers vs the rows' own values
    buckets = top.get("buckets") or []
    groups = []
    for fn in (lambda r: f"layer:{r['layer']}", lambda r: "format:" + fmt_pair(r),
               lambda r: f"fixture:{r['fixture']}"):
        for key, group_rows in _group(rows, fn).items():
            groups.append((key, group_rows))
    by_key = {b.get("key"): b for b in buckets if isinstance(b, dict)}
    for key, group_rows in groups:
        mine = _percentile_report(group_rows, "a_vs_b")
        b = by_key.get(key)
        if not isinstance(b, dict):
            problems.append(f"bucket '{key}' is absent from the document's buckets")
            continue
        if not mine.get("recomputable", True):
            continue          # the group's rows refuse a finite percentile; nothing to compare
        for metric in ("max_abs", "max_rel"):
            em = b.get(metric)
            if not isinstance(em, dict):
                problems.append(f"bucket '{key}'.{metric} is absent")
                continue
            for k, want in em.items():
                got = mine[metric].get(k)
                if not _close(got, want):
                    problems.append(f"bucket '{key}'.{metric}.{k}: emitted {want} != recomputed {got}")
    for key in by_key:
        if key not in {k for k, _ in groups} and not key.startswith(("recompose:", "recompose-layer:")):
            problems.append(f"bucket '{key}' names a group the retained rows do not contain")

    # NMSE availability
    nmse_report: Dict[str, Any]
    if v1:
        nmse_report = {
            "available": False,
            "reason": ("the v1 rows retain no per-row energies (no sum_sq_err / sum_sq_ref), so "
                       "sum(error energy)/sum(reference energy) cannot be reconstructed from this "
                       "file; the raw evidence is kept unchanged and is not rewritten"),
            "l2_rel_is_not_nmse": ("the v1 l2_rel column is a per-row ratio (0 when the reference "
                                   "norm is 0), and an unweighted mean of it is NOT a global NMSE; "
                                   "this audit derives no global NMSE from it"),
            "global_unweighted_mean_of_l2_rel_squared": {
                pop: (lambda vals: (sum(v * v for v in vals) / len(vals)) if vals else None)(
                    [r["a_vs_b"]["l2_rel"] for r in (rows if pop == "mmq_vs_helper" else rec)])
                for pop in ("mmq_vs_helper", "mmq_arm_vs_mmq")},
        }
    else:
        nmse_report = {"available": True, "populations": {}}
        for pop, key in (("mmq_vs_helper", "a_vs_b"), ("mmq_arm_vs_mmq", "arm_vs_a")):
            mine = _nmse_from(rows if pop == "mmq_vs_helper" else rec, key)
            em = (top.get("nmse") or {}).get(pop)
            entry = dict(mine)
            if not isinstance(em, dict):
                problems.append(f"nmse.{pop}: the document emits no {pop} population")
            else:
                entry["emitted"] = em
                if em.get("state") != mine["state"]:
                    problems.append(f"nmse.{pop}.state: emitted {em.get('state')!r} != recomputed "
                                    f"{mine['state']!r}")
                if em.get("rows") != mine["rows"]:
                    problems.append(f"nmse.{pop}.rows: emitted {em.get('rows')} != recomputed "
                                    f"{mine['rows']}")
                ev, mv = em.get("energy_error"), mine["energy_error"]
                if not _close(ev, mv):
                    problems.append(f"nmse.{pop}.energy_error: emitted {ev} != recomputed {mv}")
                ev, mv = em.get("energy_ref"), mine["energy_ref"]
                if not _close(ev, mv):
                    problems.append(f"nmse.{pop}.energy_ref: emitted {ev} != recomputed {mv}")
                if not _close(em.get("nmse"), mine["nmse"]):
                    problems.append(f"nmse.{pop}.nmse: emitted {em.get('nmse')} != recomputed "
                                    f"{mine['nmse']}")
            nmse_report["populations"][pop] = entry
        nmse_report["note"] = ("energy-weighted ratio over each population, reference = side a; "
                               "a null value is a named non-numeric state, never agreement")

    if problems:
        raise AuditError("; ".join(problems[:12]) + ("" if len(problems) <= 12 else
                                                    f" (+{len(problems) - 12} more)"))
    return {"path": path, "mode": mode, "schema": top.get("schema"),
            "harness_version": top.get("harness_version"),
            "summary_only": bool(summary_only),
            "coverage": {"layer_window": lo_hi, "layers_measured": layers_measured,
                         "layers_zero_rows": gap_layers, "format_pairs": fmt_pairs,
                         "fixtures": fixtures,
                         "counts": counts,
                         "formats_declared": len(top.get("formats") or []),
                         "notes": top.get("notes") or []},
            "percentiles": per_pop, "aggregate_checks": checks,
            "nmse": nmse_report,
            "verdict": "none: this audit applies no tolerance and prints no pass/fail"}


# ---------------- the human report ----------------

def _fmt(v: Any) -> str:
    return "-" if v is None else (f"{v:.12g}" if isinstance(v, float) else str(v))


def print_report(a: Dict[str, Any]) -> None:
    c = a["coverage"]
    print(f"report          : {a['path']}")
    print(f"parse mode      : {a['mode']}")
    print(f"schema          : {a['schema']}  harness {a['harness_version']}")
    print(f"summary_only    : {c['counts'].get('rows_retained') and a['summary_only']}")
    print(f"layer window    : {c['layer_window']}  layers with rows: "
          f"{len(c['layers_measured'])} ({', '.join(str(l) for l in c['layers_measured'])})")
    print(f"layers 0 rows   : {len(c['layers_zero_rows'])} "
          f"({', '.join(str(l) for l in c['layers_zero_rows'][:16])}"
          f"{' ...' if len(c['layers_zero_rows']) > 16 else ''})")
    print(f"format pairs    : {len(c['format_pairs'])}")
    for k in sorted(c["format_pairs"]):
        print(f"  {k:24s} rows {c['format_pairs'][k]}")
    print(f"fixtures        : {len(c['fixtures'])}")
    for k in sorted(c["fixtures"]):
        print(f"  {k:24s} rows {c['fixtures'][k]}")
    print(f"counts          : expected {c['counts']['rows_expected']} measured "
          f"{c['counts']['rows_measured']} skipped {c['counts']['rows_skipped']} retained "
          f"{c['counts']['rows_retained']} recompose {c['counts'].get('recompose_retained')}")
    n_cmp = sum(1 for c in a["aggregate_checks"] if "skipped" not in c)
    n_skip = len(a["aggregate_checks"]) - n_cmp
    print(f"aggregate checks: {n_cmp} emitted-vs-recomputed comparisons, all equal"
          + (f"; {n_skip} skipped (rows refuse a finite percentile)" if n_skip else ""))
    for pop, p in a["percentiles"].items():
        for metric in ("max_abs", "max_rel"):
            s = p[metric]
            print(f"percentiles {pop} {metric:8s}: n {s['count']} p50 {_fmt(s['p50'])} "
                  f"p90 {_fmt(s['p90'])} p99 {_fmt(s['p99'])} p99.9 {_fmt(s['p999'])} "
                  f"max {_fmt(s['max'])} mean {_fmt(s['mean'])}")
    nm = a["nmse"]
    if not nm["available"]:
        print("nmse            : UNAVAILABLE - " + nm["reason"])
        print("                  " + nm["l2_rel_is_not_nmse"])
        for pop, v in nm["global_unweighted_mean_of_l2_rel_squared"].items():
            print(f"                  reference-only unweighted mean of l2_rel^2 ({pop}): {_fmt(v)}"
                  "  [NOT a tensor NMSE, printed only to show it is a different number]")
    else:
        for pop, e in nm["populations"].items():
            print(f"nmse {pop:16s}: state {e['state']} nmse {_fmt(e['nmse'])} "
                  f"(energy_error {_fmt(e['energy_error'])} / energy_ref {_fmt(e['energy_ref'])}, "
                  f"rows {e['rows']}, ref-zero rows {e['rows_ref_zero']}, "
                  f"nonfinite rows {e['rows_nonfinite']})")
    print(f"verdict         : {a['verdict']}")


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog=PROG, description=__doc__.splitlines()[0])
    ap.add_argument("report", help="the report file to audit (read-only)")
    ap.add_argument("--json-out", help="write the audit result here (a NEW file, never the report)")
    args = ap.parse_args(argv)
    try:
        with open(args.report, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8")
    except OSError as exc:
        print(f"{PROG}: cannot read {args.report}: {exc}", file=sys.stderr)
        return 3
    except UnicodeDecodeError as exc:
        print(f"{PROG}: {args.report} is not UTF-8 text: {exc}", file=sys.stderr)
        return 3
    try:
        a = audit(text, args.report)
    except AuditError as exc:
        print(f"{PROG}: AUDIT ERROR {exc}", file=sys.stderr)
        return 2
    print_report(a)
    if args.json_out:
        try:
            with open(args.json_out, "x", encoding="utf-8") as f:
                json.dump(a, f, indent=2, sort_keys=True)
                f.write("\n")
        except OSError as exc:
            print(f"{PROG}: cannot write {args.json_out}: {exc}", file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
