#!/usr/bin/env bash
# QA round 2026-11-07 for the v0.2.11-upmerged tree — parent-driven, prod-DOWN window.
# Phases: stop prod -> drain -> HIP build -> CUDA build (full, incl. test binaries)
#         -> ldd guards -> full ctest (env -u CUDA_VISIBLE_DEVICES; /tmp/agent ensured).
# Logs to run/qa2-round-<ts>.log. Fresh dirs build-qa2/ (derived trees configure fresh).
set -uo pipefail
cd /mnt/srcds/Strata
source /home/srcds/.bashrc >/dev/null 2>&1
export PATH=$(echo "$PATH" | tr ':' '\n' | grep -v cuda | paste -sd:)
TS=$(date +%Y%m%d-%H%M%S)
LOG="run/qa2-round-$TS.log"
exec >>"$LOG" 2>&1
echo "== QA2 round $TS (v0.2.11 merge) =="

echo "== Phase 0: stop prod (supervisor first) and drain =="
export STRATA_PROD_TREE=/mnt/srcds/Strata
bash stop-prod.sh
pkill -9 -f '[r]pcw-(hip|cuda)/' 2>/dev/null; pkill -9 -f '/serve/[s]erver[.]py' 2>/dev/null
for i in $(seq 1 60); do
  vram_b=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card[01]/{u+=$3} END{print u+0}')
  (( vram_b < 2000000000 )) && break; echo "$(date +%T) waiting VII drain: $vram_b bytes"; sleep 5
done
mkdir -p /tmp/agent   # scrubbed engine/test tmp base; reboots clear /tmp

echo "== Phase 1a: HIP engine configure (build-qa2/rpcw-hip) =="
export PATH=/opt/rocm-6.1.0/bin:$PATH
hash -r
command -v nvcc >/dev/null && { echo "FATAL: nvcc leaked into HIP configure PATH"; exit 1; }
T0=$SECONDS
cmake -S . -B build-qa2/rpcw-hip -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DSTRATA_HIP_GFX906=ON -DCMAKE_HIP_ARCHITECTURES=gfx906 \
  -DCMAKE_HIP_COMPILER=/opt/rocm-6.1.0/llvm/bin/clang++ \
  -Dhip_DIR=/opt/rocm-6.1.0/lib/cmake/hip -Dhipblas_DIR=/opt/rocm-6.1.0/lib/cmake/hipblas \
  -DROCM_PATH=/opt/rocm-6.1.0 -DHIP_PATH=/opt/rocm-6.1.0 -DHIP_ROOT_DIR=/opt/rocm-6.1.0 \
  "-DCMAKE_PREFIX_PATH=$HOME/rocm-gfx906-xnack;/opt/rocm-6.1.0" \
  -DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF -DSTRATA_BUILD_TESTS=ON \
  || { echo "FATAL: HIP configure failed"; exit 1; }

echo "== Phase 1b: HIP engine build (ninja -j11) =="
ninja -j11 -C build-qa2/rpcw-hip strata || { echo "FATAL: HIP build failed"; exit 1; }
echo "HIP build wall: $((SECONDS-T0)) s"

echo "== Phase 2a: CUDA configure (build-qa2/rpcw-cuda) =="
T1=$SECONDS
cmake -S . -B build-qa2/rpcw-cuda -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DSTRATA_ENABLE_CUDA=ON -DSTRATA_BUILD_EXPERT_HELPER=ON \
  -DCMAKE_CUDA_ARCHITECTURES=86 -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
  -DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF -DSTRATA_BUILD_TESTS=ON \
  || { echo "FATAL: CUDA configure failed"; exit 1; }

echo "== Phase 2b: CUDA FULL build incl. test binaries (ninja -j11) =="
ninja -j11 -C build-qa2/rpcw-cuda || { echo "FATAL: CUDA build failed"; exit 1; }
echo "CUDA full build wall: $((SECONDS-T1)) s"

echo "== Phase 3: SONAME guards (ldd) =="
H=$(ldd build-qa2/rpcw-hip/strata | grep -E "libhipblas|libamdhip64" || true)
echo "$H"
echo "$H" | grep -qE "libhipblas.so.3|libamdhip64.so.7" && { echo "FATAL: bad SONAME"; exit 1; }
echo "ldd guard: PASS"

echo "== Phase 4: full ctest (CUDA tree, GPU visible) =="
env -u CUDA_VISIBLE_DEVICES ctest --test-dir build-qa2/rpcw-cuda 2>&1 | tail -12
echo "== QA2 build phase done: $((SECONDS)) s total =="
