#!/usr/bin/env bash
# build_pin.sh — clean golden-config build of the pin candidate from HEAD (build_l3.sh replication
# minus its ctest tail, which must not touch GPUs while prod serves). Usage: build_pin.sh <dst-dir-name>
set -euo pipefail
DST=${1:?dst dir name}
ROOT=/mnt/srcds/Strata-wt-integ
cd "$ROOT"
golden="$ROOT/tools/mixed_gpu/golden/rpcw-hip.cmake"
[ -f "$golden" ] || { echo "no golden"; exit 9; }
git -C "$ROOT" rev-parse --short HEAD | sed 's/^/building from HEAD /'
rm -rf "build-mixed-gpu/$DST"
args=(-C "$golden"
  "-DCMAKE_PREFIX_PATH=/home/srcds/rocm-gfx906-xnack;/opt/rocm-6.1.0"
  -DROCM_PATH=/opt/rocm-6.1.0
  -DHIP_PATH=/opt/rocm-6.1.0
  -DHIP_ROOT_DIR=/opt/rocm-6.1.0)
cmake -S . -B "build-mixed-gpu/$DST" "${args[@]}"
echo "=== $(date -Is) build $DST (-j11) ==="
cmake --build "build-mixed-gpu/$DST" -j 11
# backend guards (same as build_l3.sh)
grep -q '^STRATA_HIP_GFX906:BOOL=ON' "build-mixed-gpu/$DST/CMakeCache.txt" || { echo "GUARD FAIL: STRATA_HIP_GFX906 not ON"; exit 9; }
if grep -q '^CMAKE_CUDA_COMPILER:' "build-mixed-gpu/$DST/CMakeCache.txt"; then echo "GUARD FAIL: nvcc in HIP cache"; exit 9; fi
ldd "build-mixed-gpu/$DST/strata" 2>/dev/null | grep -q libamdhip64 || { echo "GUARD FAIL: no libamdhip64"; exit 9; }
objdump -h "build-mixed-gpu/$DST/strata" | grep -q '\.hip_fatbin' || { echo "GUARD FAIL: no .hip_fatbin"; exit 9; }
[ "$(strings -a "build-mixed-gpu/$DST/strata" | grep -c gfx906)" -gt 0 ] || { echo "GUARD FAIL: no gfx906 strings"; exit 9; }
strings -a "build-mixed-gpu/$DST/strata" | grep -q 'prefill mmq:' || { echo "GUARD FAIL: MMQ state line missing"; exit 9; }
echo "=== GUARD PASS: golden-config gfx906 HIP build with MMQ state line ==="
sha256sum "build-mixed-gpu/$DST/strata" | tee "build-mixed-gpu/$DST/strata.sha256"
echo "=== BUILD PIN COMPLETE ($(date +%H:%M:%S)) ==="
