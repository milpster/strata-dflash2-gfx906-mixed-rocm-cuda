# pool-admit-bin — what prod runs now

| | |
|---|---|
| Engine | `rpcw-hip/strata`, sha256 `5a699be1...` — merged `main` + `helper-ipc-diet` (`159e3097ef`, built in `/mnt/srcds/Strata-wt-merge-ipc/build-906-merge`) |
| Helper | `rpcw-cuda/strata-expert-helper-patient`, sha256 `9d16742d...` — fill-budget fix (`3d1e031f37`) + reserve floor 0 (`7d3230c316`) + the measured headroom prose (`a2303f4021`), built in `/mnt/srcds/Strata-wt-helper-ipc/build-mixed-gpu/helper-ipc-budget`, sm_86 |
| Setting | `--vram-reserve-mib 256` (CUDA0), `--vram-reserve-later-mib 192` (CUDA1), `STRATA_RAM_BUDGET_MIB=36864` → pool 32 GiB |
| Rollback engine | `run/pool-admit-a506/` (byte-identical copy of `a506e620...`, the pre-merge serving engine) |
| Rollback helper | `run/helper-553f3bf4/` (byte-identical copy of the previous helper) |

## What the helper change does

The fill budget was `free − reserve − scratch − buffers − 16 MiB`, where `free` came from
`cudaMemGetInfo` at fill time. Every one of those buffers is `cudaMalloc`'d in `main` **before** any
fill is served, so `free` already nets them out — and part of `buffers_bytes` is pinned host memory
that never occupied device VRAM at all. The budget now takes the live free figure, the reserve and
the 16 MiB context headroom only (`fill_budget_bytes` in
`include/strata/core/expert_helper_reserve.hpp`), which recovers ~172 MiB.

The launcher also asks `--reserve-mib 0` and `--fill-mib 0`. The 64 MiB reserve floor that used to
raise that silent 0 is gone (`7d3230c316`), and with no fill cap the budget governs, so on this
dedicated card the fill spends down to the 16 MiB context headroom. What that headroom has to hold
late was measured, not assumed: the helper's device code is 14 sm_86 cubins totalling 1.08 MiB
(largest 0.21 MiB), and the prod fill's post-arena work — warmup launch included — cost 2 MiB.

The engine binary and the config are unchanged by this swap.

## Rollback

```sh
rsync -a run/pool-admit-a506/ run/pool-admit-bin/     # engine
rsync -a run/helper-553f3bf4/ run/pool-admit-bin/     # helper (rpcw-cuda only)
```

Promotion and rollback are the operator's call (`bench/ab-v0210/rpcprof/OPERATOR-DIRECTIVES.md`).
