#!/usr/bin/env bash
# L2 (STRATA_HELPER_STREAMS) build: configure FRESH rpcw-cuda-l2 by replicating the
# rpcw-cuda-nmse2 tree's STRATA_*/GGML_*/CMAKE_* cache entries, then make -j11 and
# ctest -R helper. The protected trees (rpcw-cuda, rpcw-hip, engine-cuda,
# r4-dflash-after) are only ever READ here.
#
# L2 changes ONLY the CUDA helper (tools/mixed_gpu/sidecar/helper_main.cpp); no HIP
# source is touched, so the HIP engine binary is byte-reused from
# build-mixed-gpu/rpcw-hip-nmse2/strata (232de17, production-validated) for the A/B
# window. The helper binary hash WILL differ from the rpcw-cuda-nmse2 baseline
# (fb12b49...); the OFF arm's BEHAVIOUR is the identity claim (env gate unset ->
# the single-mutex, single-stream path, exactly the pre-L2 code), per the L3
# STRATA_RPC_PROFILE precedent; the window's request-SHA parity check guards it.
# Usage: build_l2.sh          (builds rpcw-cuda-l2 from rpcw-cuda-nmse2)
set -euo pipefail
ROOT=/mnt/user/Strata/worktrees/cuda-helper
cd "$ROOT"
src=rpcw-cuda-nmse2
dst=rpcw-cuda-l2
echo "=== $(date -Is) configure $dst (replicating $src cache entries) ==="
# Derive trees are ALWAYS configured fresh: a reused cache would keep stale
# find_library results.
rm -rf "build-mixed-gpu/$dst"
# One cache line = one array element: spaced values must stay a single argument
# or cmake dies with "Unknown argument" (l3-hip-rebuild.log, 2026-11-01).
mapfile -t args < <(grep -E '^(STRATA_|GGML_|CMAKE_BUILD_TYPE|CMAKE_CUDA|CMAKE_C)' "build-mixed-gpu/$src/CMakeCache.txt" \
        | grep -v ADVANCED | sed 's/^/-D/')
echo "cmake args (${#args[@]}):"
printf '  %s\n' "${args[@]}"
cmake -S . -B "build-mixed-gpu/$dst" "${args[@]}"
echo "=== $(date -Is) build $dst (-j11) ==="
cmake --build "build-mixed-gpu/$dst" -j 11
echo "=== $(date -Is) ctest $dst (-R helper) ==="
(cd "build-mixed-gpu/$dst" && ctest -R helper --output-on-failure)
echo "=== $(date -Is) hashes ==="
sha256sum "build-mixed-gpu/$dst/strata-expert-helper" "build-mixed-gpu/$src/strata-expert-helper" || true
echo "ALL L2 BUILDS GREEN"
