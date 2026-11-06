#!/usr/bin/env bash
# Reuse only named, unprotected validation trees. Never erase a build or fetch deps.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd -- "$ROOT"
if [[ $# != 1 ]]; then
  printf 'usage: bash tools/mixed_gpu/build_p2_diagnostics.sh EVIDENCE_DIR\n' >&2
  exit 2
fi
EV=$1
HIP=build-mixed-gpu/rpcw-hip-nmse2
CUDA=build-mixed-gpu/rpcw-cuda-nmse2
test -d "$EV"; test ! -L "$EV"
test ! -e "$EV/build.log"
test ! -L "$HIP"; test ! -L "$CUDA"
test -f "$HIP/CMakeCache.txt"; test -f "$CUDA/CMakeCache.txt"
grep -Fx 'STRATA_HIP_GFX906:BOOL=ON' "$HIP/CMakeCache.txt"
grep -Fx 'STRATA_HIP_GFX906:BOOL=OFF' "$CUDA/CMakeCache.txt"
export LD_LIBRARY_PATH=${STRATA_ROCM_ROOT:-/home/user/rocm-gfx906-xnack}/lib:/opt/rocm-6.1.0/lib
export PATH=/usr/local/cuda/bin:/opt/rocm-6.1.0/bin:$PATH
{
  date -Is
  sha256sum -c "$EV/sources-before-build.sha256"
  sha256sum -c build-mixed-gpu/nmse-tooling1/parent-build1-protected-before.sha256
  printf '\n=== DEFAULT-OFF HIP BUILD ===\n'
  cmake -S . -B "$HIP" -DSTRATA_P2_DIAGNOSTICS=OFF \
    -"-DCMAKE_PREFIX_PATH=${STRATA_ROCM_ROOT:-/home/user/rocm-gfx906-xnack};/opt/rocm-6.1.0" \
    -DFETCHCONTENT_FULLY_DISCONNECTED=ON -DFETCHCONTENT_UPDATES_DISCONNECTED=ON
  cmake --build "$HIP" -j 11
  printf '\n=== DEFAULT CUDA BUILD (INCLUDING HARNESS) ===\n'
  cmake -S . -B "$CUDA" -DSTRATA_P2_DIAGNOSTICS=OFF \
    -DFETCHCONTENT_FULLY_DISCONNECTED=ON -DFETCHCONTENT_UPDATES_DISCONNECTED=ON
  cmake --build "$CUDA" -j 11
  printf '\n=== DEFAULT EXECUTABLE IDENTITY ===\n'
  sha256sum -c build-mixed-gpu/nmse-tooling2/default-before-affordance.sha256
  printf '\n=== OPT-IN GFX906 BUILD ===\n'
  cmake -S . -B "$HIP" -DSTRATA_P2_DIAGNOSTICS=ON \
    -"-DCMAKE_PREFIX_PATH=${STRATA_ROCM_ROOT:-/home/user/rocm-gfx906-xnack};/opt/rocm-6.1.0"
  cmake --build "$HIP" -j 11
  printf '\n=== CPU HELPER/REPORT TESTS ===\n'
  for tree in "$HIP" "$CUDA"; do
    ctest --test-dir "$tree" -R 'helper|^p2_measure_test$' --no-tests=error --output-on-failure
  done
  STRATA_P2_MEASURE_TEST="$ROOT/$HIP/p2_measure_test" python3 -m pytest -q --tb=short \
    tests/terminal/test_p2_diagnostics.py tests/terminal/test_p2_report_json.py \
    --basetemp "$ROOT/$EV/pytest-post-build"
  printf '\n=== REFERENCE HELP/LINKS (NO GPU INITIALIZATION) ===\n'
  "$HIP/p2_measure_hip_reference" --help
  ldd "$HIP/p2_measure_hip_reference" | tee "$EV/reference-libs.log"
  if grep -E 'not found|libcudart|libcuda\.so' "$EV/reference-libs.log"; then exit 1; fi
  grep -F 'libamdhip64.so.6 => /opt/rocm-6.1.0/' "$EV/reference-libs.log"
  ldd "$HIP/strata" | tee "$EV/engine-libs.log"
  if grep -F 'not found' "$EV/engine-libs.log"; then exit 1; fi
  grep -F "librocblas.so.4 => ${STRATA_ROCM_ROOT:-/home/user/rocm-gfx906-xnack}/lib/" "$EV/engine-libs.log"
  grep -F 'libhipblas.so.2 => /opt/rocm-6.1.0/' "$EV/engine-libs.log"
  printf '\n=== FINAL INTEGRITY ===\n'
  sha256sum -c "$EV/sources-before-build.sha256"
  sha256sum -c build-mixed-gpu/nmse-tooling1/parent-build1-protected-before.sha256
  sha256sum "$HIP/strata" "$HIP/p2_measure_hip_reference" "$CUDA/strata-expert-helper" \
    "$CUDA/p2_measure_harness" > "$EV/binaries.sha256"
  git diff --check
  printf '\nP2_DIAGNOSTICS_BUILD_GATE_PASSED\n'
  date -Is
} 2>&1 | tee -- "$EV/build.log"
