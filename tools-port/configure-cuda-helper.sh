#!/bin/sh
# Configure the CUDA build directory that carries `strata-expert-helper` - the sidecar this box runs
# on the RTX 3080 while the engine runs on the two Radeon VIIs.
#
# Why a second build directory: engine/cmake/strata_gpu_engine.cmake REFUSES the helper under
# STRATA_HIP_GFX906 (its own message: "the sidecar is CUDA-only (NVIDIA). Build it in a CUDA tree; the
# HIP engine runs its proxy client unchanged").  It is a refusal on purpose, not a skip, so the gfx906
# tree can never quietly lose the sidecar - and it means the helper cannot be built in build-906 or
# build-lane.  This directory is the CUDA tree, and it builds nothing else that matters to the engine.
#
# What each option is for:
#
#   -DSTRATA_ENABLE_CUDA=ON -DSTRATA_HIP_GFX906=OFF -DSTRATA_ENABLE_HIP=OFF
#       the NVIDIA toolchain (cmake/backends/cuda.cmake), no ROCm.  Nothing here touches the gfx906
#       engine or the card it serves from.
#   -DCMAKE_CUDA_ARCHITECTURES=86
#       sm_86 is the RTX 3080 in this box.  CMake's own default for CUDA 13 is sm_75, and the engine's
#       release build is a fat 75;86;89;120 - this build needs the 3080 only.
#   -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc
#   -DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++ -DCMAKE_CXX_COMPILER=/usr/bin/g++
#       g++ as nvcc's host, always.  clang-as-nvcc-host is separately broken on this box (unrelated to
#       this port), and /usr/bin/c++ happens to be g++-15 here, so the host compiler is spelled out
#       rather than trusted to the default.
#   -DSTRATA_NATIVE_EXPERTS=ON
#       the helper computes native (IQ) expert blobs; CMake refuses the helper without it.
#   -DSTRATA_BUILD_EXPERT_HELPER=ON
#       the opt-in itself.  OFF, nothing of it is configured at all.
#   -DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF
#       the lane's DFlash2 pair - the same pair build-lane and this box's serving engine use, so the
#       libraries the helper links (strata_engine, strata_kernels_cpu) are configured as they are in
#       the process it talks to.  Both ON is a FATAL_ERROR by design: one verifier, one tap producer.
#   -DSTRATA_BUILD_TESTS=ON  - NOT to build tests: to keep ggml.
#       Tests OFF with Strata's own CPU pool (the release shape) makes upstream's own rule skip ggml
#       entirely - "Strata: no ggml in this build (own CPU pool, no MMQ, tests off)",
#       engine/CMakeLists.txt:681 (`if(STRATA_BUILD_TESTS OR NOT STRATA_OWN_CPU_QUANT)`).  The lane's
#       helper and its IPC client are not upstream's files, and three of them include ggml.h for the
#       Q8_K / q8_1 wire's type constants: src/core/remote_experts.cpp:6,
#       src/core/expert_helper_socket.cpp:9 and tools/mixed_gpu/sidecar/helper_main.cpp:62.  Measured
#       2026-11-22: with tests OFF those three TUs die on `fatal error: ggml.h: No such file or
#       directory`.  Tests ON brings ggml back (as in build-906 and build-lane, which are both
#       configured this way); the test programs themselves are still not built, because only
#       `ninja ... strata-expert-helper` is asked for.  The alternative -
#       -DSTRATA_OWN_CPU_QUANT=OFF - would change the CPU quantisation path this box serves with, so
#       it is not the one to reach for.
#
#       The lane's CPU test `helper_microbench` (the same sidecar directory, the framed channel's
#       CPU-only cost) is registered by cmake/strata_lane_tests.cmake and runs in the gfx906 lane
#       suite (tools-port/lane-tests.sh), which is where it was measured.
#
# This box's CUDA 13.1 toolkit needed a one-line patch to
# /usr/local/cuda-13.1/targets/x86_64-linux/include/crt/math_functions.h for glibc 2.43 (rsqrt /
# rsqrtf collide with the C++ standard's own names).  The operator applied it on 2026-11-22; without
# it, CMake's compiler-id test fails inside CUDA's own header and the configure never starts.
#
# Usage: sh tools-port/configure-cuda-helper.sh [BUILD_DIR] [-- extra cmake options]
set -eu
PORT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="build-cuda-helper"
EXTRA=""
while [ $# -gt 0 ]; do
	case "$1" in
	--) shift; EXTRA="$*"; break ;;
	*)  BUILD="$1"; shift ;;
	esac
done
CUDA=/usr/local/cuda

[ -n "$EXTRA" ] || EXTRA="-DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF"

echo "configuring $PORT/engine -> $BUILD (CUDA, sm_86, g++ host, the helper opt-in)"
exec cmake -S "$PORT/engine" -B "$PORT/$BUILD" -G Ninja \
  -DSTRATA_ENABLE_CUDA=ON \
  -DSTRATA_HIP_GFX906=OFF \
  -DSTRATA_ENABLE_HIP=OFF \
  -DCMAKE_CUDA_COMPILER="$CUDA/bin/nvcc" \
  -DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++ \
  -DCMAKE_CXX_COMPILER=/usr/bin/g++ \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CUDA_ARCHITECTURES=86 \
  -DSTRATA_NATIVE_EXPERTS=ON \
  -DSTRATA_BUILD_EXPERT_HELPER=ON \
  -DSTRATA_BUILD_TESTS=ON \
  $EXTRA
