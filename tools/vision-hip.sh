#!/usr/bin/env bash
# vision-hip.sh - run the gfx906 HIP build of strata-vision with the env it needs.
# Used as the vision "exe" in prod configs that put the encoder on a Radeon VII.
# HIP_VISIBLE_DEVICES=0,1 masks the Cezanne iGPU: unmasked enumeration on this box
# yields "no ROCm-capable device is detected" (the engine's launcher does the same).
set -euo pipefail
export HIP_VISIBLE_DEVICES="${HIP_VISIBLE_DEVICES:-0,1}"
export HSA_OVERRIDE_GFX_VERSION="${HSA_OVERRIDE_GFX_VERSION:-9.0.6}"
export LD_LIBRARY_PATH="/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec /mnt/user/Strata/build-vision-hip/bin/strata-vision "$@"
