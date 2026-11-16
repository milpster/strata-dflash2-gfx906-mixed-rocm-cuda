#!/usr/bin/env bash
# quant_quality exe wrapper: the pin binary under the launcher's ROCm library resolution
# (launcher line 121; bare subprocess loads a mismatched system ROCm -> free(): invalid pointer)
exec env LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib \
  HSA_OVERRIDE_GFX_VERSION=9.0.6 \
  /mnt/srcds/Strata/v0236p0/rpcw-hip/strata "$@"
