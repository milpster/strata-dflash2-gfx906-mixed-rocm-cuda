#!/usr/bin/env bash
# L3 (STRATA_RPC_PROFILE) builds: configure FRESH rpcw-cuda-l3 / rpcw-hip-l3 by
# replicating each validated tree's STRATA_*/GGML_*/CMAKE_HIP_* cache entries,
# then make -j11 and ctest -R helper. The protected trees (rpcw-cuda, rpcw-hip,
# engine-cuda, r4-dflash-after) are only ever READ here.
# Toolchain pin (l3w1 lesson, 2026-11-01): the HIP build MUST resolve ROCm libraries
# against the user's custom gfx906-xnack dir + /opt/rocm-6.1.0 (libhipblas.so.2 /
# libamdhip64.so.6) — exactly what the canonical launcher's LD_LIBRARY_PATH provides
# at runtime. A bare configure lets CMake find a newer ROCm and links .so.3/.so.7,
# which the engine cannot load (exit 127; l3w1-lib-diagnosis.log).
# Usage: build_l3.sh ["rpcw-hip:rpcw-hip-l3"]  (default: both trees)
set -euo pipefail
ROOT=${STRATA_HELPER_WT:-${STRATA_PROD_TREE:-/mnt/user/Strata}/worktrees/cuda-helper}
cd "$ROOT"
TREES=${1:-"rpcw-cuda:rpcw-cuda-l3 rpcw-hip:rpcw-hip-l3"}
for spec in $TREES; do
  src=${spec%%:*}
  dst=${spec##*:}
  echo "=== $(date -Is) configure $dst (replicating $src cache entries) ==="
  # Derive trees are ALWAYS configured fresh: a reused cache would keep the
  # find_library results (and the HIP compiler) of whatever ROCm it saw before.
  rm -rf "build-mixed-gpu/$dst"
  # One cache line = one array element: spaced values (CMAKE_HIP_FLAGS_*=-O3
  # -DNDEBUG) must stay a single argument or cmake sees a bare '-g'/'-O3' and
  # dies with "Unknown argument" (l3-hip-rebuild.log, 2026-11-01).
  mapfile -t args < <(grep -E '^(STRATA_|GGML_|CMAKE_BUILD_TYPE|CMAKE_HIP_)' "build-mixed-gpu/$src/CMakeCache.txt" \
          | grep -v ADVANCED | sed 's/^/-D/')
  # Pin ROCm resolution to the runtime's libraries; never let CMake wander into a
  # newer /opt/rocm (the unversioned AMDDeviceLibs_DIR of the old cache is NOT
  # replicated on purpose — it resolves through CMAKE_PREFIX_PATH instead).
  args+=(-"-DCMAKE_PREFIX_PATH=${STRATA_ROCM_ROOT:-/home/user/rocm-gfx906-xnack};/opt/rocm-6.1.0"
         -DROCM_PATH=/opt/rocm-6.1.0
         -DHIP_PATH=/opt/rocm-6.1.0
         -DHIP_ROOT_DIR=/opt/rocm-6.1.0)
  echo "cmake args (${#args[@]}):"
  printf '  %s\n' "${args[@]}"
  cmake -S . -B "build-mixed-gpu/$dst" "${args[@]}"
  echo "=== $(date -Is) build $dst (-j11) ==="
  cmake --build "build-mixed-gpu/$dst" -j 11
  echo "=== $(date -Is) ctest $dst (-R helper) ==="
  (cd "build-mixed-gpu/$dst" && ctest -R helper --output-on-failure)
  echo "=== $(date -Is) $dst OK ==="
done
echo "ALL L3 BUILDS GREEN"
