"""Toolchain tripwire: mixed-GPU HIP binaries must link the ROCm the runtime serves.
import os

Lesson (2026-11-01, family l3w1): a bare cmake configure resolves ROCm against a
newer /opt/rocm and links libhipblas.so.3 / libamdhip64.so.7. The canonical
launcher serves LD_LIBRARY_PATH=/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib
(sonames .so.2 / .so.6; the custom dir supplies the gfx906-tuned rocBLAS that wins
library-search order), so a binary with the wrong sonames dies with exit 127 at
launch. build_l3.sh pins CMAKE_PREFIX_PATH; these tests catch a forgotten pin
before a measurement window burns on it. Everything skips when this box lacks
the artifacts, so the suite stays green elsewhere.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build-mixed-gpu"
LAUNCHER = Path(os.environ.get("STRATA_PROD_TREE", "/mnt/user/Strata") + "/launch-prod-swift15-dflash-w4-q4_0-mixed.sh")

RIGHT = ("libhipblas.so.2", "libamdhip64.so.6")
WRONG = ("libhipblas.so.3", "libamdhip64.so.7")


def _needed(engine: Path) -> str:
    out = subprocess.run(
        ["readelf", "-d", str(engine)], capture_output=True, text=True, check=True
    ).stdout
    return "".join(line for line in out.splitlines() if "NEEDED" in line)


@pytest.mark.parametrize("tree", ["rpcw-hip", "rpcw-hip-l3"])
def test_hip_engine_links_the_runtime_rocm(tree: str) -> None:
    if shutil.which("readelf") is None:
        pytest.skip("readelf not available")
    engine = BUILD / tree / "strata"
    if not engine.is_file():
        pytest.skip(f"{engine} not built on this box")
    needed = _needed(engine)
    for soname in RIGHT:
        assert soname in needed, f"{tree}: {soname} missing from NEEDED"
    for soname in WRONG:
        assert soname not in needed, (
            f"{tree} linked {soname} from a newer /opt/rocm: repin CMAKE_PREFIX_PATH "
            "to /home/user/rocm-gfx906-xnack;/opt/rocm-6.1.0 (see build_l3.sh; "
            "evidence cap1-validation/l3w1-lib-diagnosis.log)"
        )


def test_launcher_puts_custom_rocm_first() -> None:
    if not LAUNCHER.is_file():
        pytest.skip("canonical launcher not present")
    line = next(
        (
            ln
            for ln in LAUNCHER.read_text().splitlines()
            if "LD_LIBRARY_PATH" in ln and "rocm-gfx906-xnack" in ln
        ),
        None,
    )
    assert line is not None, (
        "launcher no longer sets LD_LIBRARY_PATH with /home/user/rocm-gfx906-xnack"
    )
    xnack = line.find("rocm-gfx906-xnack")
    stock = line.find("rocm-6.1.0")
    assert 0 <= xnack < stock, (
        "custom gfx906-xnack libs must precede rocm-6.1.0 in LD_LIBRARY_PATH "
        "(the custom rocBLAS must win library-search order)"
    )
