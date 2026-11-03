#!/usr/bin/env bash
# Diagnostic targets only, in new unprotected trees; no server, helper or inference.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd -- "$ROOT"
if [[ $# != 1 ]]; then
  printf 'usage: bash tools/mixed_gpu/build_quantizer_diagnostic.sh EVIDENCE_DIR\n' >&2
  exit 2
fi
EV=$1
HIP=build-mixed-gpu/repro-q81-hip
CUDA=build-mixed-gpu/repro-q81-cuda
GGML=/mnt/user/Strata/build-906-new/_deps/strata_llamacpp-src
test -d "$EV"; test ! -L "$EV"; test ! -e "$EV/build.log"
test ! -L "$HIP"; test ! -L "$CUDA"; test -d "$GGML"
export LD_LIBRARY_PATH=/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib
export PATH=/usr/local/cuda/bin:/opt/rocm-6.1.0/bin:$PATH
common=(-DCMAKE_BUILD_TYPE=Release -DSTRATA_ENABLE_CUDA=ON -DSTRATA_NATIVE_EXPERTS=ON -DSTRATA_BUILD_TESTS=ON
  -DSTRATA_GGML_DIR="$GGML" -DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF -DSTRATA_MMQ_KQUANTS=ON
  -DSTRATA_MMQ_TQ_OVERLAY=ON -DSTRATA_Q6K_EXPERTS=ON
  -DFETCHCONTENT_FULLY_DISCONNECTED=ON -DFETCHCONTENT_UPDATES_DISCONNECTED=ON)
{
  date -Is
  sha256sum tools/mixed_gpu/p2_measure/p2_measure{.cpp,.hpp,_main.cpp,_test.cpp} \
    tools/mixed_gpu/build_quantizer_diagnostic.sh > "$EV/sources-before.sha256"
  sha256sum -c build-mixed-gpu/nmse-tooling1/parent-build1-protected-before.sha256
  printf '\n=== HIP DIAGNOSTIC BUILD (NO ENGINE TARGET) ===\n'
  cmake -S . -B "$HIP" "${common[@]}" -DSTRATA_HIP_GFX906=ON \
    -DSTRATA_P2_DIAGNOSTICS=ON -DSTRATA_BUILD_EXPERT_HELPER=OFF \
    -DCMAKE_HIP_COMPILER=/opt/rocm-6.1.0/llvm/bin/clang++ -DCMAKE_HIP_ARCHITECTURES=gfx906 \
    '-DCMAKE_PREFIX_PATH=/home/user/rocm-gfx906-xnack;/opt/rocm-6.1.0'
  cmake --build "$HIP" --target p2_measure_test p2_measure_hip_reference -j 11
  ctest --test-dir "$HIP" -R '^p2_measure_test$' --no-tests=error --output-on-failure
  printf '\n=== CUDA DIAGNOSTIC BUILD (NO ENGINE/HELPER TARGET) ===\n'
  cmake -S . -B "$CUDA" "${common[@]}" -DSTRATA_HIP_GFX906=OFF \
    -DSTRATA_P2_DIAGNOSTICS=OFF -DSTRATA_BUILD_EXPERT_HELPER=ON \
    -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc -DCMAKE_CUDA_ARCHITECTURES=86
  cmake --build "$CUDA" --target p2_measure_test p2_measure_harness -j 11
  ctest --test-dir "$CUDA" -R '^p2_measure_test$' --no-tests=error --output-on-failure
  STRATA_P2_MEASURE_TEST="$ROOT/$HIP/p2_measure_test" python3 -m pytest -q --tb=short \
    tests/terminal/test_p2_diagnostics.py tests/terminal/test_p2_report_json.py \
    --basetemp "$ROOT/$EV/pytest"
  sha256sum -c "$EV/sources-before.sha256"
  sha256sum -c build-mixed-gpu/nmse-tooling1/parent-build1-protected-before.sha256
  sha256sum "$HIP/p2_measure_hip_reference" "$CUDA/p2_measure_harness" > "$EV/binaries.sha256"
  git diff --check
  printf '\nQUANTIZER_DIAGNOSTIC_BUILD_PASSED\n'
  date -Is
} 2>&1 | tee -- "$EV/build.log"
