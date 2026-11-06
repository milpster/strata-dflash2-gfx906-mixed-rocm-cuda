"""CPU tests for tools/mixed_gpu/pool_p2_reports.py (pooled verdict numbers)."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/mixed_gpu"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/mixed_gpu/p2_measure"))

import pool_p2_reports as pooler                                   # noqa: E402
import p2_report_audit as audit                                    # noqa: E402


def _row(index, layer, max_abs, max_rel, sse, ssr):
    return {"fixture": "f", "fixture_index": index, "layer": layer, "expert": 1,
            "token": index, "pair": 0, "gu_type": 1, "d_type": 2,
            "a_vs_b": {"n": 1, "nonfinite": False, "ref_zero": ssr == 0,
                       "sum_sq_err": sse, "sum_sq_ref": ssr,
                       "max_abs": max_abs, "row_amax": 1.0, "max_rel": max_rel,
                       "mean_abs": max_abs, "l2_rel": max_rel,
                       "a_amax": 1.0, "b_amax": 1.0}}


def _write_report(path, lo, hi, rows):
    path.write_text(json.dumps({"schema": audit.SCHEMA_V2, "summary_only": False,
                                "eps": 1e-9, "layer_window": [lo, hi],
                                "rows": [_row(i, *r) for i, r in enumerate(rows)]}))


def _cli(paths, json_out=None):
    argv = [sys.executable, str(Path(pooler.__file__).resolve()), *[str(p) for p in paths]]
    if json_out:
        argv += ["--json-out", str(json_out)]
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


def test_pool_merges_disjoint_windows(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    _write_report(a, 32, 33, [(32, 0.1, 0.5, 2.0, 100.0), (33, 0.2, 0.7, 3.0, 100.0)])
    _write_report(b, 34, 35, [(34, 0.3, 0.9, 5.0, 100.0), (35, 0.4, 1.1, 7.0, 100.0)])
    result = pooler.pool([str(a), str(b)])
    assert result["rows_pooled"] == 4
    assert result["layers_pooled"] == [32, 33, 34, 35]
    # audit method: sorted, index ceil(q*(n-1)); rel values [0.5,0.7,0.9,1.1]
    assert result["stats"]["max_rel"]["max"] == pytest.approx(1.1)
    assert result["stats"]["max_rel"]["p999"] == pytest.approx(1.1)   # ceil(.999*3)=3
    assert result["stats"]["max_rel"]["p50"] == pytest.approx(0.9)    # ceil(.5*3)=2
    assert result["stats"]["max_abs"]["max"] == pytest.approx(0.4)
    # energies sum exactly like one report: (2+3+5+7)/(100*4)
    assert result["nmse"]["state"] == "ok"
    assert result["nmse"]["energy_error"] == pytest.approx(17.0)
    assert result["nmse"]["energy_ref"] == pytest.approx(400.0)
    assert result["nmse"]["nmse"] == pytest.approx(17.0 / 400.0)


def test_pool_single_report_matches_itself(tmp_path):
    a = tmp_path / "a.json"
    rows = [(32, 0.05 * i, 0.02 * i, 1.0 * i, 50.0) for i in range(1, 6)]
    _write_report(a, 32, 32, rows)
    result = pooler.pool([str(a)])
    rels = sorted(r[2] for r in rows)
    assert result["stats"]["max_rel"]["max"] == pytest.approx(rels[-1])
    assert result["stats"]["max_rel"]["p999"] == pytest.approx(rels[-1])
    assert result["nmse"]["nmse"] == pytest.approx(sum(r[3] for r in rows) / sum(r[4] for r in rows))


def test_pool_refuses_duplicate_identity(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    # both declare 32-47; a measures layer 32, b re-measures the same row on layer 32
    _write_report(a, 32, 47, [(32, 0.1, 0.1, 1.0, 10.0)])
    _write_report(b, 32, 47, [(32, 0.1, 0.1, 1.0, 10.0)])
    with pytest.raises(audit.AuditError):
        pooler.pool([str(a), str(b)])
    rc = _cli([a, b])
    assert rc.returncode == 2 and "already in" in rc.stderr


def test_pool_allows_declared_overlap_with_disjoint_rows(tmp_path):
    # att4 shape: one report declares the full 32-47 window but its helper only
    # measured rows on layers 32-35; the window reports own the rest.
    a, b = tmp_path / "att4.json", tmp_path / "w1.json"
    _write_report(a, 32, 47, [(32, 0.1, 0.2, 1.0, 10.0), (35, 0.2, 0.4, 1.0, 10.0)])
    _write_report(b, 36, 39, [(36, 0.3, 0.6, 1.0, 10.0)])
    result = pooler.pool([str(a), str(b)])
    assert result["rows_pooled"] == 3
    assert result["layers_pooled"] == [32, 35, 36]
    assert result["reports"][0]["layers_measured"] == [32, 35]
    assert result["nmse"]["nmse"] == pytest.approx(3.0 / 30.0)


def test_pool_refuses_bad_schema(tmp_path):
    a = tmp_path / "a.json"
    a.write_text(json.dumps({"schema": "strata.p2_row_measure.v1", "rows": []}))
    with pytest.raises(audit.AuditError):
        pooler.pool([str(a)])
    rc = _cli([a])
    assert rc.returncode == 2


def test_pool_missing_file_is_file_error(tmp_path):
    rc = _cli([tmp_path / "nope.json"])
    assert rc.returncode == 3


def test_pool_cli_json_out_never_overwrites(tmp_path):
    a, out = tmp_path / "a.json", tmp_path / "pooled.json"
    _write_report(a, 36, 39, [(36, 0.1, 0.2, 1.0, 10.0)])
    rc = _cli([a], out)
    assert rc.returncode == 0 and "pooled max_rel" in rc.stdout
    assert json.loads(out.read_text())["rows_pooled"] == 1
    rc = _cli([a], out)                                              # refuse overwrite
    assert rc.returncode == 3
