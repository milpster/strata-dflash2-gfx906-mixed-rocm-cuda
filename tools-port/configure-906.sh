#!/bin/sh
# Configure the gfx906 build of this port tree, per AGENTS.md (ROCm 6.1.0 userspace, never /opt/rocm).
#
# Two pins are NOT in AGENTS.md's recipe and are environment adaptations, not port changes. Both were
# needed on GUAM on 2026-11-22 to get past CMake's own compiler check; without them the build dies in
# ROCm's and libstdc++'s own headers and never reaches Strata's code:
#
#   --gcc-install-dir=/usr/lib/gcc/x86_64-linux-gnu/15
#       clang 17 (ROCm 6.1.0) compiles against the highest libstdc++ it finds, and this box has a
#       stray /usr/include/c++/16 tree with no matching g++-16. libstdc++ 16 does not build under
#       clang 17 ("member access into incomplete type '__normal_iterator'"). Pin 15, which is what
#       /usr/bin/c++ (g++ 15.2.0) really is.
#
#   -isystem /opt/rocm-6.1.0/include
#       the distro package libamdhip64-dev dropped a NEWER ROCm's headers into /usr/include/hip, and
#       <hip/hip_runtime.h> resolves there by default; that header wants
#       __builtin_amdgcn_wavefrontsize, which clang 17 does not have. Put 6.1.0's own headers first -
#       that is the userspace this deployment links and runs against.
#
# 0.2.18 moved the engine build under engine/, so the source dir is engine/, not the tree root.
#
# The DFlash2 options follow docs/PLAN-UPSTREAM-0218.md: the fork's runtime answers to
# STRATA_ENABLE_DFLASH2, upstream's own drafter to STRATA_HAVE_DFLASH2 (which 0.2.18 does not offer
# as an option - it sets the definition directly, so passing it is a no-op; see PORT-NOTES.md).
#
# Usage: sh tools-port/configure-906.sh [BUILD_DIR] [-- extra cmake options]
set -eu
PORT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="build-906"
EXTRA=""
while [ $# -gt 0 ]; do
	case "$1" in
	--) EXTRA="${EXTRA# }$2"; shift 2 ;;
	*)  BUILD="$1"; shift ;;
	esac
done
ROCM=/opt/rocm-6.1.0
GCCDIR=/usr/lib/gcc/x86_64-linux-gnu/15

[ -n "$EXTRA" ] || EXTRA="-DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF"

echo "configuring $PORT/engine -> $BUILD"
PATH="$ROCM/bin:$PATH" exec cmake -S "$PORT/engine" -B "$BUILD" -G Ninja \
  -DSTRATA_HIP_GFX906=ON \
  -DCMAKE_HIP_ARCHITECTURES=gfx906 \
  -DCMAKE_HIP_COMPILER="$ROCM/llvm/bin/clang++" \
  -DCMAKE_PREFIX_PATH="$ROCM" \
  -DCMAKE_HIP_FLAGS="--gcc-install-dir=$GCCDIR -isystem $ROCM/include" \
  -DCMAKE_CXX_FLAGS="-isystem $ROCM/include" \
  $EXTRA
