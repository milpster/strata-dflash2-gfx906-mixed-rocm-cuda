#!/usr/bin/env bash
# build-hip.sh - build strata-vision (tools/vision/strata_vision.cpp, SAME source as the CUDA
# variant) against llama.cpp's ggml with the HIP backend for gfx906 (Radeon VII), to measure the
# vision encoder on the MI50s. Mirrors the flags the engine's rpcw-hip tree uses
# (CMAKE_HIP_COMPILER=/opt/rocm-6.1.0/llvm/bin/clang++, CMAKE_HIP_ARCHITECTURES=gfx906,
# CMAKE_PREFIX_PATH=/opt/rocm-6.1.0, Release, GGML_NATIVE=ON via STRATA_PORTABLE=OFF).
#
#   JOBS=5 ./build-hip.sh          # bounded parallelism: prod serves live agents on these CPUs
#
# Binary: build-vision-hip/bin/strata-vision (build dir stays untracked).
#
# RUNTIME (the parent's benchmark window):
#   LD_LIBRARY_PATH=/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib HSA_OVERRIDE_GFX_VERSION=9.0.6 \
#     strata-vision --mmproj <mmproj> --model <model> --gpu
#
# NOTE (no source change): strata_vision.cpp's CPU-mode guard sets CUDA_VISIBLE_DEVICES=-1, which
# the CUDA runtime honors; the HIP/ROCR runtime IGNORES it. A --gpu-less run of THIS build can
# still open a small context on a card - keep the encoder off the cards with the env the ROCm
# stack honors instead: HIP_VISIBLE_DEVICES="" ROCR_VISIBLE_DEVICES="" strata-vision ... (no --gpu).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="$ROOT/build-vision-hip"
JOBS="${JOBS:-5}"
export ROCM_PATH=/opt/rocm-6.1.0
cmake -S "$ROOT/tools/vision" -B "$BUILD" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLAMA_DIR="$ROOT/third_party/llama.cpp" \
  -DSTRATA_VISION_CUDA=OFF \
  -DSTRATA_PORTABLE=OFF \
  -DGGML_HIP=ON \
  -DCMAKE_HIP_COMPILER=/opt/rocm-6.1.0/llvm/bin/clang++ \
  -DCMAKE_HIP_ARCHITECTURES=gfx906 \
  -DCMAKE_PREFIX_PATH=/opt/rocm-6.1.0
cmake --build "$BUILD" --target strata-vision -j "$JOBS"
echo "strata-vision (HIP/gfx906): $BUILD/bin/strata-vision"
