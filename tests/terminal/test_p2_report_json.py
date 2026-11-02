"""CPU-only regression tests for the P2 measurement report's JSON: a REAL PARSER, not a substring check.

WHY THESE TESTS EXIST (numerical-acceptance.md lines 15-16, 28-29): the harness's v1 document was not
valid JSON at all - the bucket emitter wrote `{"key": "...", "max_abs": "v": {...}}` - and every
byte-stability test it had passed, because a substring/byte-stability check never parses.  The retained
raw reports (build-mixed-gpu/p2-measure-window1/measure.json, build-mixed-gpu/p2-measure-harness/
measure.json) keep that shape and stay byte-unchanged: the audit reads them, it never repairs them.

WHAT IS TESTED HERE, all on CPU with no GPU, no helper, no pack, no network:
  1. the harness's own emitted document parses with `json.loads` and NO NaN/Infinity token is accepted,
  2. the tensor NMSE block carries the numerator, the denominator, the counts and a NAMED state, and
     the number equals the hand-computed sum(energy)/sum(reference-energy) - and differs from an
     unweighted mean of the per-row ratios (the rows have deliberately unequal reference energies),
  3. every non-numeric state is explicit: empty -> null, zero reference + nonzero error -> null,
     zero reference + zero error -> 0 with its name, nonfinite input -> null with its count,
  4. `tools/mixed_gpu/p2_measure/p2_report_audit.py` audits a v2 document clean, recovers a legacy
     malformed document by strict section boundaries, says `unavailable` for NMSE when the raw energies
     are absent, never derives a global NMSE from `l2_rel`, refuses a corrupted/nonfinite/count-mismatched
     file with its typed exit 2, and never writes to the file it audits,
  5. the retained raw v1 evidence, when present, audits clean with the ORIGINAL absolute/relative
     percentiles recomputed from its own rows.

The executable seam: the CPU test binary `p2_measure_test` (the CMake target that compiles
p2_measure.cpp with no GPU code) run as `--emit-selftest <variant> <path>`.  Point the suite at it with
STRATA_P2_MEASURE_TEST=/path/to/p2_measure_test; without it the suite SKIPS with that instruction,
because this tree has no ggml source to build the target from.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / "tools" / "mixed_gpu" / "p2_measure" / "p2_report_audit.py"
VARIANTS = ("ok", "empty", "zero-ref-nonzero", "zero-ref-exact", "nonfinite", "legacy")

sys.path.insert(0, str(AUDIT.parent))
import p2_report_audit as audit_mod                                            # noqa: E402


def _refuse(name: str):
    raise ValueError(f"non-finite JSON token {name}")


def _emit(variant: str, out: Path) -> None:
    rc = subprocess.run([_exe(), "--emit-selftest", variant, str(out)],
                        capture_output=True, text=True, timeout=120)
    assert rc.returncode == 0, f"--emit-selftest {variant} rc={rc.returncode}: {rc.stderr[:400]}"


def _exe() -> str:
    """The CPU seam: an executable supporting `--emit-selftest`.  Explicit env first, then the build
    trees.  A binary that cannot emit is refused with the instruction, never a silent skip."""
    cand = []
    env = os.environ.get("STRATA_P2_MEASURE_TEST")
    if env:
        cand.append(Path(env))
    for name in ("p2_measure_test", "nmse_probe"):
        cand += sorted(ROOT.glob(f"build*/**/{name}"))[:10]
    for c in cand:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    pytest.skip("no CPU-built p2_measure_test found: build it (cmake --build <dir> "
                "--target p2_measure_test) and set STRATA_P2_MEASURE_TEST=<path>")


@pytest.fixture(scope="module")
def docs(tmp_path_factory) -> dict:
    d = tmp_path_factory.mktemp("p2-report-json")
    out = {}
    for v in VARIANTS:
        p = d / f"selftest-{v}.json"
        _emit(v, p)
        assert p.stat().st_size > 0
        out[v] = p
    return out


# ---------- 1. the strict parser accepts the fixed document ----------

def test_v2_documents_parse_strictly(docs):
    for v in ("ok", "empty", "zero-ref-nonzero", "zero-ref-exact", "nonfinite"):
        text = docs[v].read_text(encoding="utf-8")
        doc = json.loads(text, parse_constant=_refuse)          # NaN/Infinity REFUSED
        assert doc["schema"] == "strata.p2_row_measure.v2", v
        assert "nmse" in doc, v
        # the v1 defect is a key-shaped sequence; a parsed document proves it is gone
        for b in doc["buckets"]:
            assert isinstance(b["max_abs"], dict) and "count" in b["max_abs"], v
            assert isinstance(b["max_rel"], dict) and "count" in b["max_rel"], v
        assert 'max_abs": "v"' not in text, v
        for r in doc["rows"]:
            for side in ("a_vs_b", "arm_vs_a"):
                assert {"n", "sum_sq_err", "sum_sq_ref", "ref_zero", "nonfinite"} <= set(r[side]), v


def test_the_emitter_is_byte_stable(docs):
    again = docs["ok"].parent / "again.json"
    _emit("ok", again)
    assert again.read_bytes() == docs["ok"].read_bytes()


def test_no_nonfinite_token_in_any_v2_document(docs):
    for v in ("ok", "empty", "zero-ref-nonzero", "zero-ref-exact", "nonfinite"):
        text = docs[v].read_text(encoding="utf-8")
        # `inf`/`nan` as a bare token is not JSON: a strict parse above already proves it, and the
        # harness's emitter prints null for a non-finite number.
        json.loads(text, parse_constant=_refuse)


# ---------- 2. the emitted NMSE is the hand-computed energy-weighted ratio ----------

# The "ok" document's rows (see make_cpu_selftest_report in p2_measure.cpp):
#   r0 {1,2,3,4} vs {1,2,3,4}  -> err 0      ref 1+4+9+16 = 30
#   r1 {10,0,0,0} vs {11,0,0,0} -> err 1     ref 100
#   r2 {0,1,0,0} vs {0,1.5,0,0} -> err 0.25  ref 1
# sum(err) 1.25 ; sum(ref) 131 ; nmse = 1.25/131 ; unweighted mean of l2_rel^2 = (0+0.01+0.25)/3.
def test_mmq_vs_helper_nmse_is_the_sum_of_energies(docs):
    doc = json.loads(docs["ok"].read_text(encoding="utf-8"), parse_constant=_refuse)
    n = doc["nmse"]["mmq_vs_helper"]
    assert n["state"] == "ok" and n["rows"] == 3
    assert math.isclose(n["energy_error"], 1.25, rel_tol=1e-12)
    assert math.isclose(n["energy_ref"], 131.0, rel_tol=1e-12)
    assert math.isclose(n["nmse"], 1.25 / 131.0, rel_tol=1e-12)
    # the reference side is named, and the definition is the ratio of sums, not of ratios
    assert "side a" in n["reference"]
    assert "not a mean of per-row ratios" in doc["nmse"]["definition"]
    # the unweighted mean of the squared per-row ratios is a DIFFERENT number (9.08x)
    per_row = [r["a_vs_b"]["l2_rel"] for r in doc["rows"]]
    unweighted = sum(v * v for v in per_row) / len(per_row)
    assert math.isclose(unweighted, (0.0 + 0.01 + 0.25) / 3.0, rel_tol=1e-12)
    assert abs(unweighted - n["nmse"]) > 5e-2


def test_arm_population_nmse(docs):
    doc = json.loads(docs["ok"].read_text(encoding="utf-8"), parse_constant=_refuse)
    a = doc["nmse"]["mmq_arm_vs_mmq"]
    assert a["state"] == "ok" and a["rows"] == 1
    assert math.isclose(a["energy_error"], 1.0, rel_tol=1e-12)
    assert math.isclose(a["energy_ref"], 4.0, rel_tol=1e-12)
    assert math.isclose(a["nmse"], 0.25, rel_tol=1e-12)


def test_percentiles_stay_the_original_row_percentiles(docs):
    """The v1 max_abs/max_rel aggregates are untouched by the new fields: recomputed from the rows by
    the pinned nearest-rank rule, they equal what the document emits."""
    doc = json.loads(docs["ok"].read_text(encoding="utf-8"), parse_constant=_refuse)
    ab = sorted(r["a_vs_b"]["max_abs"] for r in doc["rows"])
    rel = sorted(r["a_vs_b"]["max_rel"] for r in doc["rows"])
    for name, vals in (("max_abs", ab), ("max_rel", rel)):
        em = doc["aggregate"]["mmq_vs_helper"][name]
        assert em["count"] == len(vals)
        for q, key in ((0.50, "p50"), (0.90, "p90"), (0.99, "p99"), (0.999, "p999")):
            idx = min(len(vals) - 1, max(0, math.ceil(q * (len(vals) - 1))))
            assert math.isclose(em[key], vals[idx], rel_tol=1e-9), (name, key)
        assert math.isclose(em["max"], vals[-1], rel_tol=1e-12)
        assert math.isclose(em["mean"], sum(vals) / len(vals), rel_tol=1e-12)


# ---------- 3. the non-numeric states are named, never a silent 0 ----------

def test_empty_comparison_is_null_not_zero(docs):
    doc = json.loads(docs["empty"].read_text(encoding="utf-8"), parse_constant=_refuse)
    for pop in ("mmq_vs_helper", "mmq_arm_vs_mmq"):
        n = doc["nmse"][pop]
        assert n["rows"] == 0
        assert n["nmse"] is None
        assert n["state"] == "empty"


def test_zero_reference_with_nonzero_error_is_null(docs):
    doc = json.loads(docs["zero-ref-nonzero"].read_text(encoding="utf-8"), parse_constant=_refuse)
    n = doc["nmse"]["mmq_vs_helper"]
    assert n["state"] == "zero_reference_nonzero_error_unbounded"
    assert n["nmse"] is None
    assert n["energy_ref"] == 0.0 and n["energy_error"] == 55.0
    assert n["rows_ref_zero"] == 2
    for r in doc["rows"]:
        assert r["a_vs_b"]["ref_zero"] is True
        # the row's max_rel is 1 (row_amax takes the larger side), its l2_rel is v1's 0, and ref_zero
        # is what says that 0 is an UNDEFINED ratio
        assert math.isclose(r["a_vs_b"]["max_rel"], 1.0, rel_tol=1e-12)
        assert r["a_vs_b"]["l2_rel"] == 0.0


def test_zero_reference_with_zero_error_is_a_named_zero(docs):
    doc = json.loads(docs["zero-ref-exact"].read_text(encoding="utf-8"), parse_constant=_refuse)
    n = doc["nmse"]["mmq_vs_helper"]
    assert n["state"] == "zero_reference_exact_match"
    assert n["nmse"] == 0.0 and n["energy_ref"] == 0.0 and n["energy_error"] == 0.0


def test_nonfinite_inputs_are_refused_and_counted(docs):
    doc = json.loads(docs["nonfinite"].read_text(encoding="utf-8"), parse_constant=_refuse)
    n = doc["nmse"]["mmq_vs_helper"]
    assert n["state"] == "invalid_nonfinite_rows"
    assert n["nmse"] is None
    assert n["rows_nonfinite"] == 2
    flagged = [r for r in doc["rows"] if r["a_vs_b"]["nonfinite"]]
    assert len(flagged) == 2


# ---------- 4. the audit tool ----------

def _audit(path: Path):
    r = subprocess.run([sys.executable, str(AUDIT), str(path)], capture_output=True, text=True,
                       timeout=300)
    return r


def test_audit_clean_on_every_v2_document(docs):
    for v in ("ok", "empty", "zero-ref-nonzero", "zero-ref-exact", "nonfinite"):
        r = _audit(docs[v])
        assert r.returncode == 0, f"{v}: {r.stderr[:400]}"
        assert "all equal" in r.stdout, v
        assert "no pass/fail" in r.stdout, v


def test_audit_reports_nmse_availability_per_population(docs):
    r = _audit(docs["ok"])
    assert "nmse mmq_vs_helper" in r.stdout and "energy_ref" in r.stdout
    r2 = _audit(docs["empty"])
    assert "state empty nmse -" in r2.stdout


def test_legacy_document_is_not_valid_json_and_the_audit_recovers_it(docs):
    text = docs["legacy"].read_text(encoding="utf-8")
    with pytest.raises(Exception):
        json.loads(text, parse_constant=_refuse)          # the v1 defect: NOT JSON
    r = _audit(docs["legacy"])
    assert r.returncode == 0, r.stderr[:400]
    assert "v1 legacy" in r.stdout
    assert "UNAVAILABLE" in r.stdout                       # no energies in v1 rows
    assert "NOT a tensor NMSE" in r.stdout                 # and l2_rel is never converted into one
    assert "layers with rows: 2" in r.stdout
    assert "format pairs" in r.stdout and "IQ3_S+Q2_0" in r.stdout
    assert "fixtures        : 2" in r.stdout


def test_audit_refuses_a_count_mismatch(docs, tmp_path):
    bad = tmp_path / "count-mismatch.json"
    bad.write_bytes(docs["ok"].read_bytes().replace(
        b'"rows_retained": 3', b'"rows_retained": 4', 1))
    r = _audit(bad)
    assert r.returncode == 2
    assert "rows_retained" in r.stderr


def test_audit_refuses_a_corrupted_row(docs, tmp_path):
    bad = tmp_path / "corrupt-row.json"
    bad.write_bytes(docs["ok"].read_bytes().replace(
        b'"fixture": "decode-list"', b'"fixture": "invented-fixture"', 1))
    r = _audit(bad)
    assert r.returncode == 2, r.stderr[:400]


def test_audit_refuses_a_nonfinite_token(docs, tmp_path):
    bad = tmp_path / "nonfinite.json"
    bad.write_bytes(docs["ok"].read_bytes().replace(b'"p999": 1,', b'"p999": inf,', 1))
    r = _audit(bad)
    assert r.returncode == 2, r.stderr[:400]
    # a v2 document prints a non-finite number as null; a bare `inf` token is not JSON, and the audit
    # refuses it as corruption rather than reading a number out of it
    assert "strict JSON parse failed" in r.stderr, r.stderr[:400]


def test_audit_never_writes_to_the_report_it_audits(docs):
    before = hashlib.sha256(docs["legacy"].read_bytes()).hexdigest()
    assert _audit(docs["legacy"]).returncode == 0
    assert hashlib.sha256(docs["legacy"].read_bytes()).hexdigest() == before
    res = audit_mod.audit(docs["legacy"].read_text(encoding="utf-8"), str(docs["legacy"]))
    assert res["mode"].startswith("v1 legacy")


# ---------- 5. the retained raw v1 evidence, read-only ----------

@pytest.mark.parametrize("raw", [
    ROOT / "build-mixed-gpu/p2-measure-window1/measure.json",
    ROOT / "build-mixed-gpu/p2-measure-harness/measure.json",
])
def test_retained_raw_v1_reports_audit_clean(raw: Path):
    if not raw.is_file():
        pytest.skip(f"{raw} is retained evidence from a measurement window; it is not in this tree")
    text = raw.read_text(encoding="utf-8")
    digest_before = hashlib.sha256(raw.read_bytes()).hexdigest()
    with pytest.raises(Exception):
        json.loads(text, parse_constant=_refuse)          # the raw file stays malformed, unchanged
    res = audit_mod.audit(text, str(raw))
    assert res["schema"] == "strata.p2_row_measure.v1"
    assert res["nmse"]["available"] is False
    assert "l2_rel" in res["nmse"]["l2_rel_is_not_nmse"]
    assert res["coverage"]["counts"]["rows_retained"] > 0
    assert res["coverage"]["layers_measured"], "a retained report with no rows is not quantitative evidence"
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == digest_before
    r = _audit(raw)
    assert r.returncode == 0, r.stderr[:400]
    assert "all equal" in r.stdout                        # rows recompute the emitted percentiles


def test_audit_module_is_read_only_by_construction():
    src = AUDIT.read_text(encoding="utf-8")
    assert 'open(args.report, "rb")' in src                 # the report is opened read-only
    assert '"x"' in src and "json_out" in src               # output creation is exclusive, never truncates
    assert shutil.which("python3") is not None


def test_json_output_cannot_overwrite_its_input(docs, tmp_path):
    report = tmp_path / "report.json"
    before = docs["ok"].read_bytes()
    report.write_bytes(before)
    result = subprocess.run([sys.executable, str(AUDIT), str(report),
                             "--json-out", str(report)], capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 3, result.stderr
    assert report.read_bytes() == before
