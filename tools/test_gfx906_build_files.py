"""#3172 #3271: the gfx906 (HIP wave64 compat) build stays buildable. The real check is a compile for gfx906 (done on a box with ROCm: the
`strata` target links); these cover what a text check can: the pieces of CMakeLists.txt a 0.1.44+ gfx906 build was missing."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8").replace("\r\n", "\n")
HIP = (ROOT / "include" / "strata" / "platform" / "hip_compat" / "strata_hip.h").read_text(encoding="utf-8").replace("\r\n", "\n")


class Gfx906CMake(unittest.TestCase):
    def test_qwen35_is_not_built_for_gfx906(self):
        # q36_engine.cu calls cublasSgemm / cublasGemmStridedBatchedEx, which the wave64 compat layer does not map; main() guards every use
        # with STRATA_HAVE_QWEN35, so leaving the library out gives a gfx906 build that links
        block = re.search(r"if\(NOT STRATA_HIP_GFX906\)\n  add_library\(strata_qwen35.*?target_compile_definitions\(strata PRIVATE STRATA_HAVE_QWEN35=1\)\n  endif\(\)", TEXT, re.S)
        self.assertIsNotNone(block)
        self.assertIn("add_executable(strata-q36 ", block.group(0))
        self.assertIn("target_link_libraries(strata PRIVATE strata_qwen35)", block.group(0))

    def test_every_use_of_qwen35_in_the_engine_is_guarded(self):
        text = (ROOT / "src" / "program" / "generate.cpp").read_text(encoding="utf-8").replace("\r\n", "\n")
        for match in re.finditer(r"qwen35::|strata/qwen35/engine\.hpp", text):
            before = text[:match.start()]
            self.assertGreater(before.rfind("#if defined(STRATA_HAVE_QWEN35)"), before.rfind("#endif"), match.group(0))

    def test_the_w4a4_target_gets_the_hip_definition(self):
        self.assertRegex(TEXT, r"if\(TARGET strata_mmq_w4a4\)\n\s+target_compile_definitions\(strata_mmq_w4a4 PRIVATE GGML_USE_HIP\)")

    def test_overlay_sources_and_the_f16_stub_are_in_the_gfx906_build(self):
        tail = TEXT[TEXT.index("# ---- HIP gfx906: every CUDA source"):]
        self.assertIn("${_strata_tq_overlay_dir}/quantize.cu", tail)
        self.assertIn("mmq-instance-${_t}.cu", tail)
        self.assertIn("target_sources(strata_mmq PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src/prefill/moe_f16_kq.cu)", tail)
        self.assertLess(tail.index("moe_f16_kq.cu"), tail.index("set_source_files_properties(${_strata_cu} PROPERTIES LANGUAGE HIP)"))

    # ---- DFlash2 on gfx906 (docs/DFLASH2_INCO.md): the gate, the HIP language, the OFF path ----
    def test_dflash2_gate_permits_gfx906_and_refuses_wave32_hip(self):
        self.assertRegex(TEXT, r"if\(STRATA_ENABLE_DFLASH2 AND \(NOT STRATA_ENABLE_CUDA OR STRATA_ENABLE_HIP\)\)\n  message\(FATAL_ERROR \"DFlash2 requires the CUDA sources")
        # SYCL stays refused by its own gate
        self.assertRegex(TEXT, r"if\(STRATA_ENABLE_SYCL\)\n\s+message\(FATAL_ERROR \"DFlash2 has no SYCL runtime yet\"\)")

    def test_dflash2_cu_is_globbed_into_the_hip_sources(self):
        tail = TEXT[TEXT.index("# ---- HIP gfx906: every CUDA source"):]
        self.assertIn("GLOB_RECURSE _strata_cu CONFIGURE_DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/src/*.cu", tail)
        self.assertIn("set_source_files_properties(${_strata_cu} PROPERTIES LANGUAGE HIP)", tail)

    def test_dflash2_off_leaves_the_source_inventory_untouched(self):
        # dflash_inco.cu enters strata_kernels only under the option: OFF = default tree unchanged
        self.assertRegex(TEXT, r"if\(STRATA_ENABLE_DFLASH2\)\n\s+target_sources\(strata_kernels PRIVATE src/kernels/cuda/dflash_inco.cu\)")
        # the parity target sits under the same option, with its rename note between the two lines
        self.assertRegex(TEXT, r"if\(STRATA_ENABLE_DFLASH2\)\n(?:[^\n]*\n)*?\s+add_executable\(dflash_inco_parity src/kernels/dflash2_parity.cpp\)")

    def test_the_two_dflash2_runtimes_cannot_be_built_together(self):
        # Upstream v0.2.3 defines STRATA_HAVE_DFLASH2 unconditionally in the GPU builds; here it is an option,
        # default ON, and a build with both tap producers is refused at configure time (one tap producer per
        # verifier), with the compiled runtime(s) printed so no build infers it from a flag file.
        self.assertRegex(TEXT, r"option\(STRATA_HAVE_DFLASH2 .*ON\)")
        self.assertRegex(TEXT, r"if\(STRATA_HAVE_DFLASH2 AND STRATA_ENABLE_DFLASH2\)\n\s+message\(FATAL_ERROR ")
        self.assertRegex(TEXT, r"if\(STRATA_HAVE_DFLASH2\)\n\s+target_compile_definitions\(strata_core PUBLIC STRATA_HAVE_DFLASH2=1\)")
        self.assertRegex(TEXT, r"message\(STATUS \"DFlash2 runtimes compiled: \$\{_strata_dflash2_runtimes\}\"\)")
        # upstream's own bench target keeps its name and stays out of the default build
        self.assertIn("add_executable(dflash2_parity EXCLUDE_FROM_ALL src/core/dflash2_parity.cpp)", TEXT)

    def test_the_runtime_print_matches_the_sources_that_are_compiled(self):
        # The print names upstream's runtime, so the option must gate the SOURCES too: with STRATA_HAVE_DFLASH2=OFF
        # no translation unit of upstream's runtime may enter any target (the e1h0 build proved the source lists were
        # untruthful against the print: src/core/dflash2.cpp compiled and failed on two unmapped CUDA names).
        self.assertRegex(TEXT, r"if\(STRATA_HAVE_DFLASH2\)\n\s+target_sources\(strata_kernels PRIVATE src/kernels/cuda/dflash2\.cu\)")
        self.assertRegex(TEXT, r"if\(STRATA_HAVE_DFLASH2\)\n\s+target_sources\(strata_engine PRIVATE src/core/dflash2\.cpp\)")
        self.assertRegex(TEXT, r"if\(STRATA_HAVE_DFLASH2\)\n\s+add_executable\(dflash2_parity EXCLUDE_FROM_ALL")
        # no source list may name them outside their gate: every mention is a comment, an option/message string,
        # the runtime-print line, or its gated block
        for src in ("src/kernels/cuda/dflash2.cu", "src/core/dflash2.cpp"):
            for line in TEXT.split("\n"):
                if src not in line:
                    continue
                s = line.strip()
                allowed = (s.startswith("#") or s.startswith("option(") or s.startswith("message(")
                           or s.startswith("set(") or s.startswith('"')
                           or s == f"target_sources(strata_kernels PRIVATE {src})"
                           or s == f"target_sources(strata_engine PRIVATE {src})"
                           or s.startswith(f"set_property(SOURCE {src} "))
        # the source must never sit inside a plain source list: those lines end the source with a comma or a ')'
                self.assertTrue(allowed, f"{src} appears in an ungated place: {line}")
        # the gfx906 scheme (STRATA_ENABLE_CUDA=ON, STRATA_ENABLE_HIP=OFF) defines no STRATA_USE_HIP, and the wave32
        # HIP build gets it from strata_hip_runtime: upstream's drafter must be told it is on HIP here, or it takes
        # CUDA's stream-priority and stream-fork branches on an AMD card
        self.assertRegex(TEXT, r"if\(STRATA_HAVE_DFLASH2 AND STRATA_HIP_GFX906\)\n\s+set_property\(SOURCE src/core/dflash2\.cpp "
                               r"APPEND PROPERTY COMPILE_DEFINITIONS STRATA_USE_HIP=1\)")
        # and the compat layer maps the two names the drafter asks for, with HIP's own signatures
        self.assertIn("#define cudaStreamCreateWithPriority hipStreamCreateWithPriority", HIP)
        self.assertIn("#define cudaDeviceGetStreamPriorityRange hipDeviceGetStreamPriorityRange", HIP)

    def test_qwen36_engine_stays_out_of_the_gfx906_kernel_tree(self):
        # the q36 engine (cuBLAS entry points the compat layer does not map) is built only when NOT gfx906
        block = re.search(r"if\(NOT STRATA_HIP_GFX906\)\n  add_library\(strata_qwen35.*?endif\(\)", TEXT, re.S)
        self.assertIsNotNone(block)
        self.assertIn("q36_engine.cu", block.group(0))


if __name__ == "__main__":
    unittest.main()
