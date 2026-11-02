"""Compile/run the GPU-free input writer, and pin its opt-in build boundary."""
from pathlib import Path
import os
import shutil
import struct
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_dump_writer(tmp_path):
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("C++ compiler unavailable")
    source = tmp_path / "writer.cpp"
    source.write_text('''
#include "strata/prefill/moe_input_dump.hpp"
#include <climits>
int main(int argc, char** argv) {
    const float x[] = {1, -2, 3, 4};
    const int32_t ids[] = {7, 9};
    using strata::prefill::write_moe_input_dump;
    if (argc != 2) return 1;
    if (write_moe_input_dump(argv[1], 0, 2, 1, x, ids)) return 2;
    if (write_moe_input_dump(argv[1], INT64_MAX, INT64_MAX, 1, x, ids)) return 3;
    if (write_moe_input_dump(argv[1], 2, 2, 1, nullptr, ids)) return 4;
    if (!write_moe_input_dump(argv[1], 2, 2, 1, x, ids)) return 5;
    if (write_moe_input_dump(argv[1], 2, 2, 1, x, ids)) return 6;
    return 0;
}
''')
    exe = tmp_path / "writer"
    subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(ROOT / "include"),
                    str(source), "-o", str(exe)], check=True)
    dest = tmp_path / "capture.bin"
    subprocess.run([str(exe), str(dest)], check=True)
    assert dest.read_bytes() == struct.pack("=3q4f2i", 2, 2, 1, 1, -2, 3, 4, 7, 9)
    before = dest.read_bytes()
    assert subprocess.run([str(exe), str(dest)], check=False).returncode == 5
    assert dest.read_bytes() == before


def test_opt_in_boundary():
    cmake = (ROOT / "CMakeLists.txt").read_text()
    assert 'option(STRATA_P2_DIAGNOSTICS ' in cmake
    assert 'multi-layer input capture (measurement only)" OFF)' in cmake
    assert "target_compile_definitions(strata_prefill PRIVATE STRATA_P2_DIAGNOSTICS=1)" in cmake
    source = (ROOT / "src/prefill/prefill.cpp").read_text()
    hook = source.index('std::getenv("STRATA_P2_DUMP_PREFIX")')
    assert source.rfind("#ifdef STRATA_P2_DIAGNOSTICS", 0, hook) > source.rfind("#endif", 0, hook)
    assert "reference_backend=gfx906-HIP" in (ROOT / "tools/mixed_gpu/p2_measure/p2_measure_main.cpp").read_text()
