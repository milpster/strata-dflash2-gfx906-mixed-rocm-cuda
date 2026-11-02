#!/usr/bin/env bash
# tools/mixed_gpu/cuda_prep/abi_reprove.sh - static ABI/hidden-visibility proof for the built sidecar libraries.
#
# READ-ONLY and STATIC: readelf/nm/grep over the built .so files and the build caches.  It never runs a built
# library, never dlopens, never touches a driver or a GPU.  The facts it prints are the ones the phase gate
# asks for: which symbols the sidecar exports, that a CUDA build references no AMD runtime and a HIP build
# references no NVIDIA runtime, that each library carries its own family's fat binary, and that the pinned
# ggml used by the CPU build has no native-CPU autodetection.
#
# Usage: abi_reprove.sh <build-dir>      (e.g. build-mixed-gpu/cuda-prep)
set -u
B="${1:?usage: abi_reprove.sh <build-dir>}"
CUDA_LIB="$B/cuda/libstrata_prep_cuda.so"
HIP_LIB="$B/hip/libstrata_prep_hip.so"
EXPORTS_FILE="$(dirname "$0")/strata_prep_v1.exports"

say() { printf '%s\n' "$*"; }
run() { printf '%s\n' "\$ $*"; "$@"; echo "rc=$?"; }

say "# Sidecar ABI / hidden-visibility proof (readelf/nm/grep only; no built library is executed)"
date -u '+utc %Y-%m-%d %H:%M:%S'
say "host: $(uname -sr)"
say "build dir: $B"
say ""
say "== 1. exported dynamic symbols: exactly the strata_prep_v1_* set, nothing else"
for L in "$CUDA_LIB" "$HIP_LIB"; do
  say "-- $L"
  if [ ! -f "$L" ]; then say "MISSING"; say ""; continue; fi
  ls -l "$L"
  say "   exported (defined GLOBAL) symbol count:"
  run sh -c "readelf -W --dyn-syms '$L' | awk '\$5==\"GLOBAL\" && \$7!=\"UND\"' | grep -c ."
  say "   exported strata_prep_v1_* names:"
  readelf -W --dyn-syms "$L" | awk '$5=="GLOBAL" && $7!="UND"' | grep -o 'strata_prep_v1_[A-Za-z0-9_]*' \
    | sort -u | sed 's/^/     /'
  say "   all exported names (any non-strata_prep_v1 export is a leak):"
  leaks="$(readelf -W --dyn-syms "$L" | awk '$5=="GLOBAL" && $7!="UND"' | awk '{print $8}' \
          | grep -v '^strata_prep_v1_' | sort -u)"
  if [ -z "$leaks" ]; then say "     none: PASS"; else printf '%s\n' "$leaks" | sed 's/^/     LEAK /'; fi
  say "   undefined (imported) symbol count: $(readelf -W --dyn-syms "$L" | awk '$7=="UND"' | grep -c .)"
  say "   the export list the version script declares:"
  grep -v '^\s*#' "$EXPORTS_FILE" | sed 's/^/     /'
  say ""
done

say "== 2. cross-family references (a CUDA library must know no AMD runtime, a HIP library no NVIDIA runtime)"
for L in "$CUDA_LIB" "$HIP_LIB"; do
  say "-- $L"
  if [ ! -f "$L" ]; then say "MISSING"; say ""; continue; fi
  say "   DT_NEEDED:"; readelf -W -d "$L" | grep -i NEEDED | sed 's/^/     /'
  fam_amd="$(readelf -W -d "$L" | grep -icE 'libamdhip|libhip|hsa-runtime|librocfft|rocblas|librocm|amdsmi')"
  fam_nv="$(readelf -W -d "$L" | grep -icE 'libcudart|libcuda\.|libnvrtc|libcurand|libcublas')"
  sym_amd="$(nm -D -u "$L" 2>/dev/null | grep -ciE ' (hip[A-Z]|hsa[A-Z]|__hip|amdhip|roc[a-z]*|comgr)')"
  sym_nv="$(nm -D -u "$L" 2>/dev/null | grep -ciE ' (cuda[A-Z]|cu[A-Z][a-z]|nvrtc|__cuda)')"
  say "   amd DT_NEEDED=$fam_amd  nvidia DT_NEEDED=$fam_nv  amd undefined syms=$sym_amd  nvidia undefined syms=$sym_nv"
  say ""
done

say "== 3. each library carries its own family's fat binary section"
for L in "$CUDA_LIB" "$HIP_LIB"; do
  say "-- $L"
  if [ ! -f "$L" ]; then say "MISSING"; say ""; continue; fi
  readelf -W -S "$L" | grep -iE 'fatbin|\.cuinfo|\.hip_fatbin|nv_fatbin' | sed 's/^/     /' || say "     (none)"
  say "   count: $(readelf -W -S "$L" | grep -icE 'fatbin')"
  say ""
done

say "== 4. the CPU build's ggml switches (native autodetection OFF, every GPU backend OFF, no plugin loader)"
run sh -c "grep -E 'GGML_NATIVE|GGML_CUDA|GGML_HIP|GGML_SYCL|GGML_MUSA|GGML_METAL|GGML_VULKAN|GGML_RPC|GGML_BACKEND_DL|GGML_CPU_ALL_VARIANTS' '$B/cpu/CMakeCache.txt'"
say ""
say "== 5. the CUDA/HIP build caches name the explicit architectures (native/all are refused at configure)"
run sh -c "grep -E 'CMAKE_CUDA_ARCHITECTURES:|CMAKE_CUDA_COMPILER:|STRATA_PREP_BACKEND:' '$B/cuda/CMakeCache.txt'"
run sh -c "grep -E 'CMAKE_HIP_ARCHITECTURES:|CMAKE_HIP_COMPILER:|STRATA_PREP_BACKEND:|ROCM_ROOT:' '$B/hip/CMakeCache.txt'"
say ""
say "== 6. source identity of the read-only kernel inputs (git, read-only)"
run sh -c "git -C '$(dirname "$(dirname "$(dirname "$(dirname "$0")")")")' status --porcelain=v1 -- src include CMakeLists.txt"
run sh -c "git -C '$(dirname "$(dirname "$(dirname "$(dirname "$0")")")")' diff --stat HEAD -- src/kernels include CMakeLists.txt | tail -3"
say "(an empty output above means src/, include/ and the root CMakeLists.txt are byte-identical to HEAD;"
say " the sidecar compiles the kernels exactly as the engine has them)"
