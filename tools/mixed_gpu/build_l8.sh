#!/usr/bin/env bash
# L8 (bf16 draft taps, kDraftCtxBf16/kDraftReqBf16) builds: configure FRESH
# rpcw-cuda-l8 (from rpcw-cuda-nmse2's cache) and rpcw-hip-l8 (from rpcw-hip-nmse2's
# cache), then make -j11 and ctest -R helper in each.  L8 touches the SHARED protocol
# header, the CUDA helper (helper_main.cpp) AND the HIP engine client
# (expert_helper_socket.cpp), so BOTH backends build.  The protected trees (rpcw-cuda,
# rpcw-hip, engine-cuda, r4-dflash-after, rpcw-cuda-l2, rpcw-hip-l2) are only ever
# READ here.
# Toolchain pin (l3w1 lesson, 2026-11-01): the HIP build MUST resolve ROCm libraries
# against the user's custom gfx906-xnack dir + /opt/rocm-6.1.0 - exactly what the
# canonical launcher's LD_LIBRARY_PATH provides at runtime.  Run this script under the
# canonical env (CUDA_VISIBLE_DEVICES=0,1 HSA_OVERRIDE_GFX_VERSION=9.0.6 HIP_FORCE_P2P=1,
# LD_LIBRARY_PATH=/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib) so a bare
# find_library cannot link a newer ROCm (libhipblas.so.3/.so.7 -> engine exit 127).
# Usage: build_l8.sh ["rpcw-hip-nmse2:rpcw-hip-l8"]   (default: both trees)
set -euo pipefail
ROOT=/mnt/user/Strata/worktrees/cuda-helper
cd "$ROOT"
for spec in ${1:-"rpcw-cuda-nmse2:rpcw-cuda-l8 rpcw-hip-nmse2:rpcw-hip-l8"}; do
  src=${spec%%:*}
  dst=${spec##*:}
  echo "=== $(date -Is) configure $dst (replicating $src cache entries) ==="
  # Derive trees are ALWAYS configured fresh: a reused cache would keep stale
  # find_library results.  One cache line = one array element (l3-hip-rebuild.log).
  rm -rf "build-mixed-gpu/$dst"
  if [[ $dst == rpcw-hip-* ]]; then
    # HIP trees: EXACTLY the l3 pattern (build_l3.sh:26) plus its ROCm pins — without
    # the pins a bare configure resolves HIP includes from a NEWER /opt/rocm and dies
    # in amd_device_functions.h ("builtin functions must be directly called",
    # l8-build.log 2026-11-02; the l3w1 lesson again).
    mapfile -t args < <(grep -E '^(STRATA_|GGML_|CMAKE_BUILD_TYPE|CMAKE_HIP_)' \
          "build-mixed-gpu/$src/CMakeCache.txt" | grep -v ADVANCED | sed 's/^/-D/')
    args+=('-DCMAKE_PREFIX_PATH=/home/user/rocm-gfx906-xnack;/opt/rocm-6.1.0'
           -DROCM_PATH=/opt/rocm-6.1.0
           -DHIP_PATH=/opt/rocm-6.1.0
           -DHIP_ROOT_DIR=/opt/rocm-6.1.0)
  else
    mapfile -t args < <(grep -E '^(STRATA_|GGML_|CMAKE_BUILD_TYPE|CMAKE_CUDA|CMAKE_C)' \
          "build-mixed-gpu/$src/CMakeCache.txt" | grep -v ADVANCED | sed 's/^/-D/')
  fi
  echo "cmake args (${#args[@]}):"
  printf '  %s\n' "${args[@]}"
  cmake -S . -B "build-mixed-gpu/$dst" "${args[@]}"
  echo "=== $(date -Is) build $dst (-j11) ==="
  cmake --build "build-mixed-gpu/$dst" -j 11
  echo "=== $(date -Is) ctest $dst (-R helper) ==="
  (cd "build-mixed-gpu/$dst" && ctest -R helper --output-on-failure)
done
echo "=== $(date -Is) hashes ==="
sha256sum build-mixed-gpu/rpcw-cuda-l8/strata-expert-helper \
          build-mixed-gpu/rpcw-hip-l8/strata \
          build-mixed-gpu/rpcw-cuda-nmse2/strata-expert-helper \
          build-mixed-gpu/rpcw-hip-nmse2/strata || true
echo "ALL L8 BUILDS GREEN"
