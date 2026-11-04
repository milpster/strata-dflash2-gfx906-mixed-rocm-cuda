#!/usr/bin/env bash
# Window5: run the v0.2.10 engine under classic rocprof (kernel stats) so the
# bottleneck ranking gets kernel-level ground truth. Output: out/rocprof-r*/
# Same engine, same args - serve.py invokes this instead of the raw binary.
mkdir -p /home/user/dev/Strata/bench/ab-v0210/out/rocprof-r1
exec /opt/_rocm-6.4.4/bin/rocprof --stats \
  -d /home/user/dev/Strata/bench/ab-v0210/out/rocprof-r1 \
  /home/user/dev/Strata/build-v0210/rpcw-hip/strata "$@"
