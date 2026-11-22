# Fork defect — internal note, not for upstream. Fix or accept is the fork maintainer's call.

## The shim shadows the platform header

Fork commit `7baf1501c3` (2026-11-19) added `tests/core/cuda_runtime.h`, a 37-line shim. For any target that
compiles with `-I tests/core` (a plain `-I` beats `-isystem`), it shadows
`include/strata/platform/hip_compat/cuda_runtime.h`.

**Measured on the fork's own tree, with the fork's own include order: 37 errors with the directory on the
include path, 0 without.**

**Age check.** The fork's last gfx906 object of the affected file (`rpcw-hip-fix16b`, dated 2026-11-13) lists
`hip_compat/cuda_runtime.h` in its dep file — six days *before* the shim landed, so the shipped binaries never
compiled against the shim.

**Where this bites the port.** In `/mnt/srcds/Strata-wt-0218` (branch `port-0218`):
- `dflash_tap_collect_test` drops `tests/core` from its include path (provably unused by that TU) and builds.
- `dflash_head_bf16_test` is wired `EXCLUDE_FROM_ALL`, exactly as the fork wires it, and does not compile.

**Not verified.** Whether the shim is load-bearing for some other test target on a CUDA build (no CUDA compile
is possible on this box at the moment — nvcc 13.1 vs glibc 2.43 `rsqrt` clash, separate note).
