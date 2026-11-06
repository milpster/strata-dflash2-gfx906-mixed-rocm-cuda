# tools/mixed_gpu/cuda_prep/CMakeLists.txt - Phase-2 compile-only verification assets.
#
# A STANDALONE project: it never add_subdirectory()s the root Strata project and never builds the root engine.
# One configure per backend into its own build dir (separate CUDA and HIP projects, exactly as the static map
# requires - CUDA and wave32-HIP in one project is refused by the root too, CMakeLists.txt:155-157):
#
#   cmake -S tools/mixed_gpu/cuda_prep -B build-mixed-gpu/cuda-prep/cpu  -DSTRATA_PREP_BACKEND=CPU \
#         -DSTRATA_GGML_DIR=/mnt/user/Strata/build-906-new/_deps/strata_llamacpp-src
#   cmake -S tools/mixed_gpu/cuda_prep -B build-mixed-gpu/cuda-prep/cuda -DSTRATA_PREP_BACKEND=CUDA -DCMAKE_CUDA_ARCHITECTURES=86
#   cmake -S tools/mixed_gpu/cuda_prep -B build-mixed-gpu/cuda-prep/hip  -DSTRATA_PREP_BACKEND=HIP -DCMAKE_HIP_ARCHITECTURES=gfx906
#
# Read-only sources: the worktree's src/kernels/cuda/{iq_kernels,native_moe}.cu and src/kernels/cpu/* (CPU
# backend only) and a pinned llama.cpp checkout for ggml.  The root CMakeLists.txt is never touched.
# No FetchContent, no downloads: the CPU backend refuses an empty STRATA_GGML_DIR.
cmake_minimum_required(VERSION 3.22)
project(strata_prep LANGUAGES CXX)

set(STRATA_PREP_BACKEND "CPU" CACHE STRING "Selected backend of this configure: CPU | CUDA | HIP")
set_property(CACHE STRATA_PREP_BACKEND PROPERTY STRINGS CPU CUDA HIP)

set(STRATA_GGML_DIR "" CACHE PATH "llama.cpp checkout ROOT to take ggml from (\${DIR}/ggml), like the root project")
set(STRATA_PREP_GGML_NOTE "\"pinned local checkout\"" CACHE STRING
    "Provenance note baked into fixture manifests (the checkout path + HEAD the parent verified)")
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

get_filename_component(STRATA_ROOT "${CMAKE_CURRENT_LIST_DIR}/../../.." ABSOLUTE)
if(NOT EXISTS ${STRATA_ROOT}/CMakeLists.txt)
  message(FATAL_ERROR "STRATA_ROOT ${STRATA_ROOT} has no CMakeLists.txt: run this project inside the Strata worktree")
endif()

set(_prep_new "${CMAKE_CURRENT_LIST_DIR}")
add_compile_definitions(STRATA_Q6K_EXPERTS=1)  # root default, CMakeLists.txt:139-141 (kernel instances depend on it)

if(STRATA_PREP_BACKEND STREQUAL "CUDA")
  # NVIDIA, sm_86 explicit (the root's accepted explicit value, CMakeLists.txt:230-231).  No native/all arch,
  # no autodetection: an empty CMAKE_CUDA_ARCHITECTURES here is a configure error, not a guess.
  if(NOT DEFINED CMAKE_CUDA_ARCHITECTURES OR CMAKE_CUDA_ARCHITECTURES STREQUAL "")
    message(FATAL_ERROR "CUDA build needs -DCMAKE_CUDA_ARCHITECTURES=86 (explicit; native/all are refused)")
  endif()
  foreach(_a IN LISTS CMAKE_CUDA_ARCHITECTURES)
    string(REGEX REPLACE "-.*$" "" _base "${_a}")
    if(_base STREQUAL "native" OR _base STREQUAL "all" OR _base STREQUAL "all-major")
      message(FATAL_ERROR "Explicit architectures only: '${_a}' is refused")
    endif()
  endforeach()
  enable_language(CUDA)
  set(CMAKE_CUDA_STANDARD 17)
  find_package(CUDAToolkit REQUIRED)
  set(_prep_gpu_runtime CUDA::cudart)
  set(_prep_gpu_lang CUDA)
  set(_prep_backend_def STRATA_PREP_CUDA=1)
  set(_prep_lib_name strata_prep_cuda)
elseif(STRATA_PREP_BACKEND STREQUAL "HIP")
  # AMD gfx906 through the root's wave64 compat layer (root CMakeLists.txt:192-211), ROCm 6.1.0 toolchain.
  # Same rule as CUDA: an explicit arch or a configure error.
  if(NOT DEFINED CMAKE_HIP_ARCHITECTURES OR CMAKE_HIP_ARCHITECTURES STREQUAL "")
    message(FATAL_ERROR "HIP build needs -DCMAKE_HIP_ARCHITECTURES=gfx906 (explicit)")
  endif()
  set(ROCM_ROOT "/opt/rocm-6.1.0" CACHE PATH "ROCm root for the gfx906 companion")
  enable_language(HIP)
  find_package(hip REQUIRED HINTS ${ROCM_ROOT}/lib/cmake/hip ${ROCM_ROOT})
  add_library(CUDA::cudart INTERFACE IMPORTED)
  target_link_libraries(CUDA::cudart INTERFACE hip::host)
  target_compile_definitions(CUDA::cudart INTERFACE STRATA_HIP_GFX906=1 __HIP_PLATFORM_AMD__=1)
  target_include_directories(CUDA::cudart BEFORE INTERFACE ${STRATA_ROOT}/include/strata/platform/hip_compat)
  set(CMAKE_CUDA_TOOLKIT_INCLUDE_DIRECTORIES ${STRATA_ROOT}/include/strata/platform/hip_compat)
  include_directories(BEFORE ${STRATA_ROOT}/include/strata/platform/hip_compat)
  add_compile_definitions(STRATA_HIP_GFX906=1)
  set(_prep_gpu_runtime CUDA::cudart)
  set(_prep_gpu_lang HIP)
  set(_prep_backend_def STRATA_PREP_HIP=1)
  set(_prep_lib_name strata_prep_hip)
else()
  # CPU: no CUDA, no HIP, no hip_compat, no GPU language - and ggml from the pinned local checkout, static,
  # every GPU backend off, no plugin loader, no fetch.  The same switches the root project forces (1716-1722).
  if(STRATA_PREP_BACKEND STREQUAL "CPU")
    set(_prep_ok 1)
  else()
    message(FATAL_ERROR "STRATA_PREP_BACKEND must be CPU, CUDA or HIP")
  endif()
endif()

# ---- the shared validation core (plain host code, no GPU runtime; compiled into every backend) ----
add_library(strata_prep_abi_validate STATIC ${_prep_new}/abi_validate.cpp)
target_include_directories(strata_prep_abi_validate PUBLIC ${_prep_new})
target_compile_options(strata_prep_abi_validate PRIVATE -fvisibility=hidden -fvisibility-inlines-hidden)

if(NOT STRATA_PREP_BACKEND STREQUAL "CPU")
  # ---- the backend library: the UNMODIFIED kernels + the v1 wrapper, hidden visibility, explicit exports ----
  set(_prep_cu ${STRATA_ROOT}/src/kernels/cuda/iq_kernels.cu ${STRATA_ROOT}/src/kernels/cuda/native_moe.cu)
  add_library(${_prep_lib_name} SHARED
    ${_prep_new}/abi.cpp ${_prep_new}/abi_validate.cpp ${_prep_cu})
  set_property(SOURCE ${_prep_cu} PROPERTY LANGUAGE ${_prep_gpu_lang})
  set_property(SOURCE ${_prep_cu} PROPERTY POSITION_INDEPENDENT_CODE ON)
  target_include_directories(${_prep_lib_name} PRIVATE
    ${STRATA_ROOT}/include ${STRATA_ROOT}/third_party/ggml ${STRATA_ROOT}/src/kernels/cuda
    ${CMAKE_CUDA_TOOLKIT_INCLUDE_DIRECTORIES})
  target_compile_definitions(${_prep_lib_name} PRIVATE ${_prep_backend_def})
  target_compile_options(${_prep_lib_name} PRIVATE -fvisibility=hidden -fvisibility-inlines-hidden)
  target_link_libraries(${_prep_lib_name} PRIVATE ${_prep_gpu_runtime})
  # The root's numeric contract for native_moe.cu: --use_fast_math on CUDA, none on the gfx906 compat build
  # (root CMakeLists.txt:588-602).  Replicated, not re-decided.
  set_source_files_properties(${STRATA_ROOT}/src/kernels/cuda/native_moe.cu PROPERTIES
    COMPILE_OPTIONS "$<IF:$<STREQUAL:${STRATA_PREP_BACKEND},CUDA>,--use_fast_math,>")
  # Only strata_prep_v1_* is exported (version script + hidden visibility); engine/kernel/C++ symbols stay local.
  target_link_options(${_prep_lib_name} PRIVATE
    "LINKER:--version-script=${_prep_new}/strata_prep_v1.exports")
endif()

if(STRATA_PREP_BACKEND STREQUAL "CPU")
  # ---- CPU ggml: the pinned local checkout, static, every GPU backend off (root 1706-1722), no fetch ----
  enable_language(C)
  if(NOT STRATA_GGML_DIR)
    message(FATAL_ERROR
      "CPU build needs -DSTRATA_GGML_DIR=<llama.cpp checkout root> (the pinned local tree, e.g. "
      "/mnt/user/Strata/build-906-new/_deps/strata_llamacpp-src); the parent forbids a FetchContent fallback")
  endif()
  if(NOT EXISTS ${STRATA_GGML_DIR}/ggml/CMakeLists.txt)
    message(FATAL_ERROR "${STRATA_GGML_DIR}/ggml/CMakeLists.txt not found: pass the llama.cpp checkout ROOT")
  endif()
  set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
  set(GGML_STATIC ON CACHE BOOL "" FORCE)
  set(GGML_CUDA OFF CACHE BOOL "" FORCE)
  set(GGML_MUSA OFF CACHE BOOL "" FORCE)
  set(GGML_HIP OFF CACHE BOOL "" FORCE)
  set(GGML_METAL OFF CACHE BOOL "" FORCE)
  set(GGML_SYCL OFF CACHE BOOL "" FORCE)
  set(GGML_VULKAN OFF CACHE BOOL "" FORCE)
  set(GGML_WEBGPU OFF CACHE BOOL "" FORCE)
  set(GGML_ZDNN OFF CACHE BOOL "" FORCE)
  set(GGML_RPC OFF CACHE BOOL "" FORCE)
  set(GGML_OPENCL OFF CACHE BOOL "" FORCE)
  set(GGML_OPENMP OFF CACHE BOOL "" FORCE)
  set(GGML_BACKEND_DL OFF CACHE BOOL "" FORCE)
  set(GGML_CPU_ALL_VARIANTS OFF CACHE BOOL "" FORCE)
  set(GGML_BUILD_TESTS OFF CACHE BOOL "" FORCE)
  set(GGML_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
  set(GGML_LLAMAFILE OFF CACHE BOOL "" FORCE)
  set(GGML_CCACHE OFF CACHE BOOL "" FORCE)
  set(GGML_LTO OFF CACHE BOOL "" FORCE)
  # Portable CPU path, per scope: native detection OFF, explicit x86-64 baseline features ON
  # (mirrors the root's portable branch, CMakeLists.txt:1716-1722; -march=native is never used).
  set(GGML_NATIVE OFF CACHE BOOL "" FORCE)
  set(GGML_AVX ON CACHE BOOL "" FORCE)
  set(GGML_AVX2 ON CACHE BOOL "" FORCE)
  set(GGML_FMA ON CACHE BOOL "" FORCE)
  set(GGML_F16C ON CACHE BOOL "" FORCE)
  set(GGML_BMI2 ON CACHE BOOL "" FORCE)
  add_subdirectory(${STRATA_GGML_DIR}/ggml ${CMAKE_BINARY_DIR}/ggml EXCLUDE_FROM_ALL)

  # ---- strata_kernels_cpu, replicated from the root (CMakeLists.txt:1657-1672, 1763-1812): same sources,
  # same AVX-512/AVX2/AVX1 flags, same AVX-VNNI probe, same vendored-header include, same ggml link ----
  find_package(Threads REQUIRED)
  add_library(strata_prep_kernels_cpu STATIC
    ${STRATA_ROOT}/src/kernels/cpu/expert.cpp ${STRATA_ROOT}/src/kernels/cpu/pool.cpp
    ${STRATA_ROOT}/src/kernels/cpu/expert_layout.cpp ${STRATA_ROOT}/src/kernels/cpu/q2_avx2.cpp
    ${STRATA_ROOT}/src/kernels/cpu/hadamard.cpp ${STRATA_ROOT}/src/kernels/cpu/native_expert.cpp
    ${STRATA_ROOT}/src/kernels/cpu/iq_avx512.cpp ${STRATA_ROOT}/src/kernels/cpu/iq_avx2.cpp
    ${STRATA_ROOT}/src/kernels/cpu/iq_avx1.cpp ${STRATA_ROOT}/src/kernels/cpu/kq_avx2.cpp
    ${STRATA_ROOT}/src/kernels/cpu/kq_avx1.cpp ${STRATA_ROOT}/src/kernels/cpu/q2_avx1.cpp
    ${STRATA_ROOT}/src/kernels/cpu/nvfp4_avx512.cpp ${STRATA_ROOT}/src/kernels/cpu/nvfp4_avx2.cpp
    ${STRATA_ROOT}/src/kernels/cpu/tq_cpu.cpp ${STRATA_ROOT}/src/kernels/cpu/tq_avx2.cpp
    ${STRATA_ROOT}/src/kernels/cpu/tq_avx512.cpp)
  target_include_directories(strata_prep_kernels_cpu PUBLIC ${STRATA_ROOT}/include)
  target_include_directories(strata_prep_kernels_cpu PRIVATE ${STRATA_ROOT}/third_party/ggml)
  target_link_libraries(strata_prep_kernels_cpu PUBLIC ggml-cpu ggml-base Threads::Threads)
  target_compile_definitions(strata_prep_kernels_cpu PUBLIC STRATA_NATIVE_EXPERTS=1)
  set_source_files_properties(${STRATA_ROOT}/src/kernels/cpu/expert.cpp PROPERTIES COMPILE_OPTIONS
    "-mavx512f;-mavx512bw;-mavx512vl;-mavx512dq;-mavx512vnni;-mavx512vbmi;-mf16c;-mfma")
  set_source_files_properties(${STRATA_ROOT}/src/kernels/cpu/q2_avx2.cpp PROPERTIES COMPILE_OPTIONS
    "-mavx2;-mfma;-mf16c")
  set_source_files_properties(${STRATA_ROOT}/src/kernels/cpu/iq_avx512.cpp
    ${STRATA_ROOT}/src/kernels/cpu/nvfp4_avx512.cpp PROPERTIES COMPILE_OPTIONS
    "-mavx512f;-mavx512bw;-mavx512vl;-mavx512dq;-mavx512vnni;-mavx512vbmi;-mf16c;-mfma")
  set_source_files_properties(${STRATA_ROOT}/src/kernels/cpu/iq_avx2.cpp
    ${STRATA_ROOT}/src/kernels/cpu/kq_avx2.cpp ${STRATA_ROOT}/src/kernels/cpu/nvfp4_avx2.cpp
    ${STRATA_ROOT}/src/kernels/cpu/tq_avx2.cpp PROPERTIES COMPILE_OPTIONS "-mavx2;-mfma;-mf16c")
  set_source_files_properties(${STRATA_ROOT}/src/kernels/cpu/tq_avx512.cpp PROPERTIES COMPILE_OPTIONS
    "-mavx512f;-mavx512bw;-mavx512vl;-mavx512vnni;-mavx512vbmi;-mavx2;-mfma;-mf16c")
  set_source_files_properties(${STRATA_ROOT}/src/kernels/cpu/kq_avx1.cpp PROPERTIES COMPILE_OPTIONS "-mavx")
  set_source_files_properties(${STRATA_ROOT}/src/kernels/cpu/q2_avx1.cpp PROPERTIES COMPILE_OPTIONS
    "-msse4.2;-mavx;-mno-avx2;-mno-fma;-mno-f16c")
  set_source_files_properties(${STRATA_ROOT}/src/kernels/cpu/iq_avx1.cpp PROPERTIES COMPILE_OPTIONS
    "-msse4.2;-mavx;-mno-avx2;-mno-fma;-mno-f16c")
  include(CheckCXXSourceCompiles)
  set(CMAKE_REQUIRED_FLAGS "-mavx2 -mfma -mf16c")
  check_cxx_source_compiles([=[
#include <immintrin.h>
#if defined(__GNUC__) || defined(__clang__)
__attribute__((target("avxvnni")))
#endif
static __m256i dots(__m256i a, __m256i b) { return _mm256_dpbusd_avx_epi32(_mm256_dpwssd_avx_epi32(a, a, b), a, b); }
int main() {
    volatile int x = 1;
    return _mm256_extract_epi32(dots(_mm256_set1_epi32(x), _mm256_set1_epi32(x)), 0) == 0;
}
]=] STRATA_HAVE_AVXVNNI_INTRINSICS)
  unset(CMAKE_REQUIRED_FLAGS)
  if(STRATA_HAVE_AVXVNNI_INTRINSICS)
    set(_prep_avxvnni STRATA_AVXVNNI=1)
  else()
    set(_prep_avxvnni STRATA_AVXVNNI=0)
  endif()
  set_property(SOURCE ${STRATA_ROOT}/src/kernels/cpu/q2_avx2.cpp APPEND PROPERTY COMPILE_DEFINITIONS ${_prep_avxvnni})
  set_property(SOURCE ${STRATA_ROOT}/src/kernels/cpu/iq_avx2.cpp APPEND PROPERTY COMPILE_DEFINITIONS ${_prep_avxvnni})

  # ---- the CPU-only assets: the SHA-256 helper, the fixture exporter, the ABI guard test, the compile-only
  # parity driver (compiled, never run: it dlopens the GPU libraries) ----
  add_library(strata_prep_sha256 STATIC ${_prep_new}/sha256.cpp)
  target_include_directories(strata_prep_sha256 PUBLIC ${_prep_new})

  # The CPU-safe SHA-256 selftest runner (R1): published FIPS 180-4 vectors through the same sha256.cpp the
  # exporters use.  Plain host code, no ggml, no GPU runtime, no dlopen - safe to run on a CPU-only gate.
  add_executable(sha256_selftest ${_prep_new}/sha256_selftest.cpp)
  target_link_libraries(sha256_selftest PRIVATE strata_prep_sha256)

  add_library(strata_prep_manifest STATIC ${_prep_new}/manifest.cpp)
  target_include_directories(strata_prep_manifest PUBLIC ${_prep_new})
  target_link_libraries(strata_prep_manifest PUBLIC strata_prep_abi_validate)

  add_executable(fixture_export ${_prep_new}/fixture_export.cpp)
  target_link_libraries(fixture_export PRIVATE strata_prep_kernels_cpu strata_prep_abi_validate strata_prep_sha256)
  target_compile_definitions(fixture_export PRIVATE
    "STRATA_PREP_GGML_NOTE=${STRATA_PREP_GGML_NOTE}")

  # The REAL-blob exporter (S2): the model's own bytes sliced from a real GGUF pack, the same CPU oracle
  # path fixture_export uses, synthetic=0 labels.  CPU-only links (ggml-cpu + strata_kernels_cpu + the
  # manifest/sha256 helpers); it reads two files and writes a fixture, and loads no GPU anything.
  add_executable(fixture_export_real ${_prep_new}/fixture_export_real.cpp)
  target_link_libraries(fixture_export_real PRIVATE
    strata_prep_kernels_cpu strata_prep_abi_validate strata_prep_manifest strata_prep_sha256)
  target_compile_definitions(fixture_export_real PRIVATE
    "STRATA_PREP_GGML_NOTE=${STRATA_PREP_GGML_NOTE}")

  add_executable(abi_guard_test ${_prep_new}/abi_guard_test.cpp)
  target_link_libraries(abi_guard_test PRIVATE
    strata_prep_abi_validate strata_prep_manifest strata_prep_kernels_cpu strata_prep_sha256)

  add_executable(cuda_prep_check ${_prep_new}/cuda_prep_check.cpp)
  target_link_libraries(cuda_prep_check PRIVATE strata_prep_sha256 strata_prep_manifest)
  target_link_options(cuda_prep_check PRIVATE "LINKER:-ldl")
  target_compile_definitions(cuda_prep_check PRIVATE STRATA_PREP_DRIVER_COMPILE_ONLY=1)

  enable_testing()
  add_test(NAME abi_guard_test
           COMMAND abi_guard_test ${STRATA_ROOT}/tests/data/native_experts)
  set_tests_properties(abi_guard_test PROPERTIES TIMEOUT 120)
  # The parity driver is a compile target only.  It is deliberately NOT registered as a test and MUST NOT be
  # executed in this phase (not even --help): it dlopens the CUDA/HIP libraries.
endif()

message(STATUS "strata_prep: backend ${STRATA_PREP_BACKEND}, new sources ${_prep_new}, root ${STRATA_ROOT} (read-only)")
