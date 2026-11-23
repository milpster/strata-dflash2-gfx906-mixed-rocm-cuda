# Standalone repro, not part of the Strata build. Machine recipe for the ROCm
# 6.1.0 gfx906 userspace (never link or run against /opt/rocm 7.x):
#
#   PATH=/opt/rocm-6.1.0/bin:$PATH cmake -S . -B build -G Ninja \
#     -DCMAKE_HIP_COMPILER=/opt/rocm-6.1.0/llvm/bin/clang++ \
#     -DCMAKE_HIP_ARCHITECTURES=gfx906 -DCMAKE_PREFIX_PATH=/opt/rocm-6.1.0
#   PATH=/opt/rocm-6.1.0/bin:$PATH cmake --build build -j
#
# Runtime (xnack gfx906 rocBLAS + 6.1.0 userspace, as prod uses):
#   LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib
#   HSA_OVERRIDE_GFX_VERSION=9.0.6
#
# STRATA_HIP_GFX906=ON is a Strata CMake option, not part of this standalone
# project; the equivalent here is CMAKE_HIP_ARCHITECTURES=gfx906 plus the 6.1.0
# prefix above.
#
# Expected ldd (the misbuild check from AGENTS.md): libamdhip64.so.6 and
# libhipblas.so.2 from /opt/rocm-6.1.0/lib, librocblas.so.4 from the xnack
# tree. A libamdhip64.so.7 entry means the link picked /opt/rocm 7.x.

cmake_minimum_required(VERSION 3.21)

# Host-compiler and header details, neither of them a GPU one. Applied only when
# the user did not set CMAKE_HIP_FLAGS:
# - ROCm 6.1 ships clang 17, whose cuda_wrappers/cmath cannot parse this box's
#   default GCC 16 libstdc++ headers; point it at GCC 13.
# - /usr/include/hip belongs to the installed 7.x tree; the 6.1.0 headers must
#   come first or the build mixes 7.x headers with 6.1.0 runtime libs.
if(NOT CMAKE_HIP_FLAGS)
  set(_hf_flags "-isystem /opt/rocm-6.1.0/include")
  if(EXISTS /usr/lib/gcc/x86_64-linux-gnu/13)
    set(_hf_flags "--gcc-install-dir=/usr/lib/gcc/x86_64-linux-gnu/13 ${_hf_flags}")
  endif()
  set(CMAKE_HIP_FLAGS "${_hf_flags}" CACHE STRING "HIP compile flags")
endif()

project(hip_fault_repro LANGUAGES CXX HIP)
enable_language(HIP)
find_package(hip REQUIRED)

# ROCm 6.1's hip-config defaults AMDGPU_TARGETS to gfx900 and that wins over a
# cache entry, so the arch is forced after find_package.
if(CMAKE_HIP_ARCHITECTURES STREQUAL "" OR CMAKE_HIP_ARCHITECTURES STREQUAL "gfx900")
  set(CMAKE_HIP_ARCHITECTURES gfx906)
endif()

# CXX is enabled only because hip-config's FindThreads dependency needs it; the
# one .cpp must be compiled by the ROCm clang, which is what HIP language does.
set_source_files_properties(hip-fault-repro.cpp PROPERTIES LANGUAGE HIP)
add_executable(hip-fault-repro hip-fault-repro.cpp)
target_compile_options(hip-fault-repro PRIVATE -Wall -Wextra -std=c++17)

# --hip-link adds an implicit -lamdhip64. On this box /lib/x86_64-linux-gnu
# carries a 7.x libamdhip64.so, so the search path must hit 6.1.0 first or the
# binary ends up with both .so.6 and .so.7 NEEDED.
target_link_directories(hip-fault-repro BEFORE PRIVATE /opt/rocm-6.1.0/lib)
target_link_options(hip-fault-repro PRIVATE -L/opt/rocm-6.1.0/lib)

# ROCm 6.1 hip-config exports hip::device; keep a host-only fallback for
# variants that export only hip::host.
if(TARGET hip::device)
  target_link_libraries(hip-fault-repro PRIVATE hip::device)
else()
  target_link_libraries(hip-fault-repro PRIVATE hip::host ${HIP_LIBRARIES})
endif()
