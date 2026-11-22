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
# The DFlash2 options: upstream v0.2.18's drafter (STRATA_HAVE_DFLASH2) is ON and the serving lane's own
# runtime (STRATA_ENABLE_DFLASH2) is OFF, which is the only pair this tree compiles today - the lane's
# runtime needs the fork's hunks in src/program/generate.cpp and src/prefill/prefill.cpp, which are not
# carried yet (PORT-NOTES.md), and upstream's generate.cpp needs its own drafter. The fork's production
# pair is the opposite (ENABLE=ON, HAVE=OFF); it becomes reachable with those two files.
#
# Usage: sh tools-port/configure-906.sh [BUILD_DIR] [-- extra cmake options]
set -eu
PORT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="build-906"
EXTRA=""
while [ $# -gt 0 ]; do
	case "$1" in
	--) shift; EXTRA="$*"; break ;;
	*)  BUILD="$1"; shift ;;
	esac
done
ROCM=/opt/rocm-6.1.0
GCCDIR=/usr/lib/gcc/x86_64-linux-gnu/15

[ -n "$EXTRA" ] || EXTRA="-DSTRATA_ENABLE_DFLASH2=OFF -DSTRATA_HAVE_DFLASH2=ON"

echo "configuring $PORT/engine -> $BUILD"
PATH="$ROCM/bin:$PATH" exec cmake -S "$PORT/engine" -B "$BUILD" -G Ninja \
  -DSTRATA_HIP_GFX906=ON \
  -DCMAKE_HIP_ARCHITECTURES=gfx906 \
  -DCMAKE_HIP_COMPILER="$ROCM/llvm/bin/clang++" \
  -DCMAKE_PREFIX_PATH="$ROCM" \
  -DCMAKE_HIP_FLAGS="--gcc-install-dir=$GCCDIR -isystem $ROCM/include" \
  -DCMAKE_CXX_FLAGS="-isystem $ROCM/include" \
  $EXTRA
