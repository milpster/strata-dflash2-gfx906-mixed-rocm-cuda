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
  golden="$ROOT/tools/mixed_gpu/golden/$src.cmake"
  if [[ -f $golden ]]; then
    echo "=== $(date -Is) configure $dst from GOLDEN $golden (frozen conf - no template drift) ==="
  else
    echo "=== $(date -Is) configure $dst (replicating $src cache entries; NO golden yet - freeze one with freeze_build_conf.sh after proving the tree) ==="
  fi
  # Derive trees are ALWAYS configured fresh: a reused cache would keep the
  # find_library results (and the HIP compiler) of whatever ROCm it saw before.
  rm -rf "build-mixed-gpu/$dst"
  if [[ -f $golden ]]; then
    args=(-C "$golden")   # frozen conf wins; only the ROCm pins below are appended
  else
    # One cache line = one array element: spaced values (CMAKE_HIP_FLAGS_*=-O3
    # -DNDEBUG) must stay a single argument or cmake sees a bare '-g'/'-O3' and
    # dies with "Unknown argument" (l3-hip-rebuild.log, 2026-11-01).
    mapfile -t args < <(grep -E '^(STRATA_|GGML_|CMAKE_BUILD_TYPE|CMAKE_HIP_|CMAKE_CUDA_ARCHITECTURES)' "build-mixed-gpu/$src/CMakeCache.txt" \
          | grep -v ADVANCED | sed -e 's/^/-D/' -e 's/:UNINITIALIZED=/:STRING=/')
    # CMAKE_CUDA_ARCHITECTURES added 2026-11-09: the qa2 caches store it as :UNINITIALIZED=86,
    # the replication grep missed it, and the diet tree defaulted to sm_120 - the helper's
    # fatbin had no sm_86 image and died on the 3080's first kernel ("no kernel image is
    # available", ab9 window attempt 4, arm D). UNINITIALIZED is normalized to STRING because
    # -DVAR:UNINITIALIZED=86 is not a cmake-legal type spec.
  fi
  # Pin ROCm resolution to the runtime's libraries; never let CMake wander into a
  # newer /opt/rocm (the unversioned AMDDeviceLibs_DIR of the old cache is NOT
  # replicated on purpose — it resolves through CMAKE_PREFIX_PATH instead).
  # The leading "-" before "-DCMAKE_PREFIX_PATH" (sic) made bash emit "--DCMAKE_PREFIX_PATH"
  # and cmake abort with "Unknown argument" - single dash fixed 2026-11-09 (first run off
  # /home/user; the bug is unconditional, so this line could never have worked as written).
  args+=("-DCMAKE_PREFIX_PATH=${STRATA_ROCM_ROOT:-/home/srcds/rocm-gfx906-xnack};/opt/rocm-6.1.0"
         -DROCM_PATH=/opt/rocm-6.1.0
         -DHIP_PATH=/opt/rocm-6.1.0
         -DHIP_ROOT_DIR=/opt/rocm-6.1.0)
  echo "cmake args (${#args[@]}):"
  printf '  %s\n' "${args[@]}"
  cmake -S . -B "build-mixed-gpu/$dst" "${args[@]}"
  echo "=== $(date -Is) build $dst (-j11) ==="
  cmake --build "build-mixed-gpu/$dst" -j 11
  # POST-BUILD BACKEND GUARD (2026-11-09 lesson): ctest stays green in a wrong-backend
  # build - the fatbin, the section table and the linkage are the truth. That night the
  # freeze regex dropped STRATA_HIP_GFX906 (a digit var), the "HIP" tree configured as
  # pure CUDA sm_120 (libcudart.so.13, no gfx906 anywhere), and only the arm-D boot
  # caught it - the parking check saw n_dev=1 because real cudaGetDeviceCount only
  # sees the 3080. Nothing wrong-backend passes this line again.
  guard_arch() { cuobjdump --list-elf "$1" 2>/dev/null | grep -oE 'sm_[0-9]+' | sort -u; }
  if [[ $dst == *hip* ]]; then
    grep -q '^STRATA_HIP_GFX906:BOOL=ON' "build-mixed-gpu/$dst/CMakeCache.txt" \
      || { echo "GUARD FAIL $dst: STRATA_HIP_GFX906 not ON - wrong backend configured"; exit 9; }
    if grep -q '^CMAKE_CUDA_COMPILER:' "build-mixed-gpu/$dst/CMakeCache.txt"; then
      echo "GUARD FAIL $dst: nvcc entered a HIP tree's cache"; exit 9
    fi
    ldd "build-mixed-gpu/$dst/strata" 2>/dev/null | grep -q libamdhip64 \
      || { echo "GUARD FAIL $dst: engine does not link libamdhip64"; exit 9; }
    objdump -h "build-mixed-gpu/$dst/strata" | grep -q '\.hip_fatbin' \
      || { echo "GUARD FAIL $dst: no .hip_fatbin section - not a HIP build"; exit 9; }
    if [[ $(strings -a "build-mixed-gpu/$dst/strata" | grep -c gfx906) -eq 0 ]]; then
      echo "GUARD FAIL $dst: no gfx906 target strings in the engine"; exit 9
    fi
    echo "=== guard $dst PASS: gfx906 HIP (libamdhip64 + .hip_fatbin + STRATA_HIP_GFX906) ==="
  else
    guard_arch "build-mixed-gpu/$dst/strata-expert-helper" | grep -q '^sm_86$' \
      || { echo "GUARD FAIL $dst: helper fatbin lacks sm_86 ($(guard_arch "build-mixed-gpu/$dst/strata-expert-helper" | tr '\n' ' '))"; exit 9; }
    # rm -rf of derive trees wipes the launcher's manual -patient copy (bit us 2026-11-09 twice:
    # window arms boot-fail with 'No such file or directory'). The alias is a plain copy - make it.
    cp "build-mixed-gpu/$dst/strata-expert-helper" "build-mixed-gpu/$dst/strata-expert-helper-patient"
    echo "=== guard $dst PASS: helper fatbin sm_86; -patient alias refreshed ==="
  fi
  echo "=== $(date -Is) ctest $dst (-R helper) ==="
  (cd "build-mixed-gpu/$dst" && ctest -R helper --output-on-failure)
  echo "=== $(date -Is) $dst OK ==="
done
echo "ALL L3 BUILDS GREEN"
