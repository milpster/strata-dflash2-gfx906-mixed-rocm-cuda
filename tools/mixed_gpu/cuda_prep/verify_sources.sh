#!/usr/bin/env bash
# tools/mixed_gpu/cuda_prep/verify_sources.sh - the reusable source/toolchain identity check for the
# compile-only Phase-2 lane.  Version/info commands only: NO GPU probe, NO driver/NVML/rocminfo call, NO
# configure/build/test, NO install/download, NO backend-linked binary is run.  Every command's exit status is
# recorded.  Usage: verify_sources.sh <evidence-file> [ggml-checkout-root]
set -u
export PYTHONDONTWRITEBYTECODE=1

LOG="${1:?usage: verify_sources.sh <evidence-file> [ggml-checkout-root]}"
GG="${2:-${STRATA_PROD_TREE:-/mnt/user/Strata}/build-906-new/_deps/strata_llamacpp-src}"
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
mkdir -p "$(dirname "$LOG")"
: > "$LOG"

run() {  # echo the exact command, run it, record the exit status
  printf '$ %s\n' "$*" >> "$LOG"
  "$@" >> "$LOG" 2>&1
  printf 'rc=%d\n\n' "$?" >> "$LOG"
}

printf '# Source/toolchain identity evidence (%s). Read-only checks.\n' "$(date -Is)" >> "$LOG"
printf '# No GPU probe, no configure/build, no install, no backend-linked binary was run.\n\n' >> "$LOG"

run env PYTHONDONTWRITEBYTECODE=1 git -C "$ROOT" rev-parse HEAD
run env PYTHONDONTWRITEBYTECODE=1 git -C "$ROOT" status --porcelain
run env PYTHONDONTWRITEBYTECODE=1 git -C "$GG" rev-parse HEAD
run env PYTHONDONTWRITEBYTECODE=1 git -C "$GG" status --porcelain
run cat "$ROOT/third_party/ggml/VERSION.txt"
run env PYTHONDONTWRITEBYTECODE=1 ls "$GG/ggml/src/ggml.c" "$GG/ggml/src/ggml-cpu/ggml-cpu.c"
GG_HEAD="$(git -C "$GG" rev-parse HEAD)"
PIN_HEAD="$(cut -d' ' -f1 "$ROOT/third_party/ggml/VERSION.txt")"
run sh -c "test '$GG_HEAD' = '$PIN_HEAD' && echo 'GGML HEAD == candidate VERSION.txt: MATCH' || echo 'GGML HEAD != candidate VERSION.txt: MISMATCH'"

run sh -c "command -v nvcc"
run nvcc --version
run sh -c "nvcc --list-gpu-code | tr ' ' '\n' | grep -x sm_86"   # compiler info, not a GPU probe
run sh -c "test -x /opt/rocm-6.1.0/bin/hipcc && echo /opt/rocm-6.1.0/bin/hipcc"
run /opt/rocm-6.1.0/bin/hipcc --version
run sh -c "test -x /opt/rocm-6.1.0/llvm/bin/clang++ && echo /opt/rocm-6.1.0/llvm/bin/clang++"
run sh -c "command -v cmake && cmake --version | head -1"
run sh -c "command -v g++ && g++ --version | head -1"
run sh -c "command -v readelf && command -v nm"

# The new-scope file manifest (hashes the fixer reports): every file under the owned scopes.
run sh -c "cd "$ROOT" && find tools/mixed_gpu/cuda_prep -type f | sort | xargs -r sha256sum"

printf '# identity check complete: %s\n' "$(date -Is)" >> "$LOG"
tail -n 2 "$LOG"
