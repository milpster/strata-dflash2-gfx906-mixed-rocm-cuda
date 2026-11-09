#!/usr/bin/env bash
# Freeze a PROVEN build tree's pinned configuration into a cmake initial-cache
# file (stdout), for tools/mixed_gpu/golden/<name>.cmake. The golden files are
# committed; build_l3.sh configures from them instead of grep-replicating a
# template cache, so every iteration uses the same build conf and drift shows
# up as a reviewable diff (lesson of 2026-11-09: the replication grep missed
# CMAKE_CUDA_ARCHITECTURES:UNINITIALIZED=86, the fresh configure defaulted to
# sm_120, and the helper died on the 3080's first kernel).
#
# Usage: freeze_build_conf.sh <build-tree>   (e.g. build-mixed-gpu/rpcw-cuda-diet)
#        freeze_build_conf.sh <build-tree> > tools/mixed_gpu/golden/rpcw-cuda.cmake
# Only freeze a tree whose binaries have been PROVEN on the real GPUs.
set -euo pipefail
tree=${1:?usage: freeze_build_conf.sh <build-tree>}
cache="$tree/CMakeCache.txt"
[[ -f $cache ]] || { echo "no CMakeCache.txt in $tree" >&2; exit 1; }

# Everything the fresh-configure allowlist must carry: project flags plus the
# compiler/arch/toolchain facts that decide what the binaries can run on.
# STRATA_GGML_DIR/STRATA_ORACLE_* are machine-local paths - kept, the golden
# file is per-repo-checkout anyway. UNINITIALIZED is normalized to STRING.
grep -E '^(STRATA_[A-Z_]+|GGML_[A-Z0-9_]+|CMAKE_BUILD_TYPE|CMAKE_HIP_[A-Z_]+|CMAKE_CUDA_ARCHITECTURES|CMAKE_CUDA_COMPILER|CMAKE_CXX_COMPILER|CMAKE_C_COMPILER):' "$cache" \
  | grep -v ADVANCED | grep -v 'NOTFOUND' \
  | sed -e 's/:UNINITIALIZED=/:STRING=/' -e 's/:INTERNAL=.*//' \
  | awk -F: '{ key=$1; rest=substr($0, length($1)+2); split(rest, kv, "="); type=kv[1]; sub(/^[^=]*=/, "", rest); val=rest;
              gsub(/[\\"$]/, "\\\\&", val);
              printf "set(%s \"%s\" CACHE %s \"frozen by freeze_build_conf.sh from %s\")\n", key, val, (type=="" ? "STRING" : type), tree }'
echo "# frozen $(date -Is) from $tree ($(git -C . rev-parse --short HEAD 2>/dev/null || echo unknown))" >&2
