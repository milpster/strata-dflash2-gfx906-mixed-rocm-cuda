#!/usr/bin/env bash
# QA round for the merged tree (v0.2.10 upmerge + gfx906 fork) — parent-driven, prod-down.
# Logs everything to run/qa-round-<ts>.log. Phases: HIP build -> CUDA build -> ldd guards -> ctest.
set -uo pipefail
cd /mnt/srcds/Strata
source /home/srcds/.bashrc >/dev/null 2>&1
export PATH=$(echo "$PATH" | tr ':' '\n' | grep -v cuda | paste -sd:)
TS=$(date +%Y%m%d-%H%M%S)
LOG="run/qa-round-$TS.log"
exec >>"$LOG" 2>&1
echo "== QA round $TS =="
free -g | awk 'NR==2{print "RAM avail:",$7,"GiB"}'

echo "== Phase 1a: HIP engine configure (build-qa/rpcw-hip) =="
export PATH=/opt/rocm-6.1.0/bin:$PATH
hash -r
command -v nvcc >/dev/null && { echo "FATAL: nvcc leaked into HIP configure PATH"; exit 1; }
T0=$SECONDS
cmake -S . -B build-qa/rpcw-hip -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DSTRATA_HIP_GFX906=ON -DCMAKE_HIP_ARCHITECTURES=gfx906 \
  -DCMAKE_HIP_COMPILER=/opt/rocm-6.1.0/llvm/bin/clang++ \
  -Dhip_DIR=/opt/rocm-6.1.0/lib/cmake/hip -Dhipblas_DIR=/opt/rocm-6.1.0/lib/cmake/hipblas \
  -DROCM_PATH=/opt/rocm-6.1.0 -DHIP_PATH=/opt/rocm-6.1.0 -DHIP_ROOT_DIR=/opt/rocm-6.1.0 \
  "-DCMAKE_PREFIX_PATH=$HOME/rocm-gfx906-xnack;/opt/rocm-6.1.0" \
  -DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF -DSTRATA_BUILD_TESTS=ON \
  || { echo "FATAL: HIP configure failed"; exit 1; }

echo "== Phase 1b: HIP engine build (ninja -j11) =="
ninja -j11 -C build-qa/rpcw-hip strata || { echo "FATAL: HIP build failed"; exit 1; }
echo "HIP build wall: $((SECONDS-T0)) s"

echo "== Phase 2a: CUDA helper configure (build-qa/rpcw-cuda) =="
T1=$SECONDS
cmake -S . -B build-qa/rpcw-cuda -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DSTRATA_ENABLE_CUDA=ON -DSTRATA_BUILD_EXPERT_HELPER=ON \
  -DCMAKE_CUDA_ARCHITECTURES=86 -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
  -DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF -DSTRATA_BUILD_TESTS=ON \
  || { echo "FATAL: CUDA configure failed"; exit 1; }

echo "== Phase 2b: CUDA build (ninja -j11) =="
ninja -j11 -C build-qa/rpcw-cuda strata strata-expert-helper || { echo "FATAL: CUDA build failed"; exit 1; }
echo "CUDA build wall: $((SECONDS-T1)) s"

echo "== Phase 3: SONAME guards (ldd) =="
H=$(ldd build-qa/rpcw-hip/strata | grep -E "libhipblas|libamdhip64" || true)
echo "$H"
echo "$H" | grep -qE "libhipblas.so.3|libamdhip64.so.7" && { echo "FATAL: bad SONAME"; exit 1; }
echo "ldd guard: PASS"

echo "== Phase 4: ctest (helper suite, CUDA tree) =="
ctest --test-dir build-qa/rpcw-cuda -R helper --output-on-failure 2>&1 | tail -15
echo "== QA build phase done: $((SECONDS)) s total =="
