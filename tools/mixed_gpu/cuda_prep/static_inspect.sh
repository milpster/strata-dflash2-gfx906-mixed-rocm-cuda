#!/usr/bin/env bash
# tools/mixed_gpu/cuda_prep/static_inspect.sh - CPU-only GPU-freedom proof, readelf/nm only.
#
# Prints, for each CPU tool of the cuda_prep project, the evidence that it carries no GPU runtime: zero
# GPU-family DT_NEEDED, zero undefined cuda*/hip*/hsa*/nvml* symbols, zero .nv_fatbin/.hip_fatbin sections.
# Static inspection only: it never executes an inspected binary.  Run it BEFORE running any CPU tool, so the
# execution of a CPU-only tool is authorized by evidence rather than by assumption.
#
# Usage: static_inspect.sh <build-dir>           (e.g. build-mixed-gpu/cuda-prep)
set -u
B="${1:?usage: static_inspect.sh <build-dir>}"
CPU="$B/cpu"
TOOLS="sha256_selftest fixture_export fixture_export_real abi_guard_test cuda_prep_check"

echo "# GPU-free proof (readelf/nm; no inspected binary is executed)"
date -u '+utc %Y-%m-%d %H:%M:%S'
echo "host: $(uname -sr)"
echo "build dir: $B"
echo

run() { printf '%s\n' "\$ $*"; "$@"; echo "rc=$?"; echo; }

for t in $TOOLS; do
  f="$CPU/$t"
  echo "== $f"
  if [ ! -f "$f" ]; then echo "MISSING (not built)"; echo; continue; fi
  ls -l "$f"
  run readelf -d "$f"
  echo "-- GPU-family DT_NEEDED (expect 0):"
  run sh -c "readelf -d '$f' | grep -ciE 'libcuda|libcudart|libhip|amdhip|librocm|hsa-runtime|nvml|opencl|sycl'"
  echo "-- undefined GPU API symbols (expect 0):"
  run sh -c "nm -D -u '$f' | grep -ciE ' (cuda|hip|hsa|nvml|roc[a-z]*|comgr)[A-Za-z0-9_]*$'"
  run sh -c "nm -D -u '$f' | grep -cE ' cu[A-Z]'"
  echo "-- GPU binary sections (expect 0):"
  run sh -c "readelf -S '$f' | grep -ciE 'nv_fatbin|hip_fatbin|__nv_|.cuinfo'"
  echo "-- dynamic links:"; ldd "$f" | sed 's/^/  /'; echo
done

echo "== cuda/libstrata_prep_cuda.so / hip/libstrata_prep_hip.so (GPU libraries: inspected, NEVER executed)"
for l in "$B/cuda/libstrata_prep_cuda.so" "$B/hip/libstrata_prep_hip.so"; do
  if [ -f "$l" ]; then ls -l "$l"; else echo "MISSING (not built): $l"; fi
done
echo
echo "== libggml-cpu.a GPU symbols (expect 0)"
run sh -c "nm -A '$B/ggml/libggml-cpu.a' 2>/dev/null | grep -ciE ' (cuda|hip|hsa|nvml)[A-Za-z0-9_]*$'"
echo "== libstrata_prep_sha256.a GPU symbols (expect 0)"
run sh -c "nm -A '$B/libstrata_prep_sha256.a' 2>/dev/null | grep -ciE ' (cuda|hip|hsa|nvml)[A-Za-z0-9_]*$'"
echo "== the CPU driver's dynamic-loader imports (allowed: dlopen is behind the runtime gate)"
run sh -c "nm -D -u '$CPU/cuda_prep_check' | grep -E 'dlopen|dlsym'"
