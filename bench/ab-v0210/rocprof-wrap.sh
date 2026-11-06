#!/usr/bin/env bash
# Window5: run the v0.2.10 engine under classic rocprof (kernel stats) so the
# bottleneck ranking gets kernel-level ground truth. Output: out/rocprof-r*/
# Same engine, same args - serve.py invokes this instead of the raw binary.
# serve passes a stripped env and rocprof's post-processor (tblextr.py) runs `rocminfo`:
# give it the 6.1.0 tree's bin, matching the LD_LIBRARY_PATH the harness exports.
export PATH="/opt/rocm-6.1.0/bin:$PATH"
mkdir -p ${STRATA_DEV_TREE:-/home/user/dev/Strata}/bench/ab-v0210/out/rocprof-r1
exec /opt/_rocm-6.4.4/bin/rocprof --stats \
  -d ${STRATA_DEV_TREE:-/home/user/dev/Strata}/bench/ab-v0210/out/rocprof-r1 \
  ${STRATA_DEV_TREE:-/home/user/dev/Strata}/build-v0210/rpcw-hip/strata "$@"
