# Armed, not active: the merged engine (`main` + `helper-ipc-diet`)

This directory holds the engine that results from merging the finished pool branch into `main`,
ready to be swapped in with one command. **Prod does not run it.** Prod runs
`run/pool-admit-bin/rpcw-hip/strata` at sha256 `a506e620...`, and that is unchanged.

| | |
|---|---|
| Engine | `rpcw-hip/strata`, sha256 `5a699be1...` |
| Helper | `rpcw-cuda/strata-expert-helper-patient`, sha256 `553f3bf4...` (identical to prod's) |
| Commit | `159e3097ef` on branch `merge-helper-ipc-diet` (parents `bd4172ed00` + `9358921670`), pushed to `fork` |
| Built in | `/mnt/srcds/Strata-wt-merge-ipc/build-906-merge` (fresh gfx906 dir, DFlash2 on, tests on) |
| Validated | build 200/200, `pool_expert_source_adapter_test` **237 checks / 0 failures**, `ctest -R "ram_pool|pool_expert"` 6/6, extra merged paths 13/13, `ldd` routes `libhipblas.so.2`+`libamdhip64.so.6` to `/opt/rocm-6.1.0/lib` and `librocblas.so.4` to `/home/srcds/rocm-gfx906-xnack/lib` |
| **Not** validated | no boot, no serving leg. The merge brings `main`'s other features (tail-skip, `STRATA_NATIVE_MLOCK`, the helper IPC changes) together with the pool work for the first time, so its runtime behaviour is unproven until a leg runs on it |

## What activating it means

It is a config-level swap of the engine binary, nothing else: the launcher and config keep
`--vram-reserve-mib 640`, `STRATA_RAM_BUDGET_MIB=43008`, helper shard `00002`, fill 7400, slots
4600.

```sh
# stop first, on its own (never in the same command as a boot)
./stop-all.sh
# then, in a separate command, swap and boot
rsync -a run/merge-bin/ run/pool-admit-bin/ && ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh
```

Revert is the same shape, from the pinned copy of what serves today:

```sh
./stop-all.sh
rsync -a run/pool-admit-a506/ run/pool-admit-bin/ && ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh
```

`run/pool-admit-a506/` is a byte-identical copy of the running engine (`a506e620`) with its own
`SHA256SUMS`, so a rollback needs nothing from a build directory. The older rollback pin
`run/winner-pin-38g/` (`d534e72a`) is untouched and still the last pre-pool-fix binary.

## Why it waits

Promotion of an engine is the operator's decision (`bench/ab-v0210/rpcprof/OPERATOR-DIRECTIVES.md`).
A merge that builds and passes the pool tests is not the same claim as an engine that has carried
an agent leg, so the honest status of this directory is *armed*.
