# gpu-fault-repro: why the positive control never fired — 2026-11-24

Fixer lane, branch `recovery-startup-watchdog`. Task: explain and fix the repro's
positive control (5 runs at 00:04–00:05, all exit 6, `positive control FAILED: no
fault`). Box GUAM, card 0000:09:00.0, engine down, GPU idle. Every run used
`env -u CUDA_VISIBLE_DEVICES HSA_OVERRIDE_GFX_VERSION=9.0.6` with
`LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib`.
Files and logs beside `bench/gpu-fault-repro/`; logs in `bench/gpu-fault-repro/run/`.

## Three defects, in the order they were found

1. **The kernel never ran.** The build produced a plain `gfx906` code object; the
   device is `gfx906:sramecc+:xnack-`. `hipLaunchKernelGGL` set
   `hipErrorInvalidDeviceFunction` (98) and `hipDeviceSynchronize` returned
   `hipErrorSuccess`, so the scan was a silent no-op, `isum` stayed 0 and a file
   leg exited 4 with `CHECKSUM FAULT page 0 … isum=0`. Instrumented: 1 µs per
   64 MiB "scan". Fixed by building `gfx906:sramecc+:xnack-` (both sramecc
   variants; clang refuses mixing plain and feature-qualified) and by checking
   `hipGetLastError` after every launch, which is now a setup error (exit 2).
2. **`hipHostUnregister` is blocking.** Timed: warm scan 125–170 ms;
   `unregister` 127–216 ms in the original order, and 3.09–3.46 s for a
   16-repeat scan held in flight (~3.2 s). `munmap` therefore always landed
   after the device was idle. Hypothesis 1 confirmed; hypothesis 2 moot (the
   scan is 125–149 ms, not microseconds, and 11 ms was never measured here).
3. **The engine's shape is munmap-while-registered.** 16-repeat scan, then
   `munmap` with the registration still live: the kernel logs
   `amdgpu 0000:09:00.0: [gfxhub0] no-retry page fault (src_id:0 ring:24 vmid:8)`
   with `PERMISSION_FAULTS: 0x3`, `Faulty UTCL2 client ID: TCP (0x8)`, and the
   HSA runtime prints `Memory access fault by GPU node-2 … Page not present or
   supervisor privilege` then aborts. Exit 134, log ends `FATAL signal 6`.
   Identical result for a read of an already-unregistered VA.

## Result

Redesigned control, steps 0–3, step 3 last because it is fatal:
10 runs, **all exit 134** with a `FATAL signal` block (control-A1–A3, B1, C1–C5, D1).
No GPU reset followed: 70 no-retry page faults across the runs, `ring gfx` never
reset, and a 12 s `--mode cycle` leg straight after a control run was clean
(exit 0, 74 scans). A 20 s `--mode file` leg is clean too (exit 0, 122 scans).

## What the pass does and does not certify

The engine's live fault is `[mmhub0]` / `IH client 0x12 (VMC)` /
`Faulty UTCL2 client ID: XDMA (0x0)`; the control's is `[gfxhub0]` /
`IH client 0x1b (UTCL2)` / `TCP (0x8)`. Same vmid 8, same card, same
`PERMISSION_FAULTS: 0x3`, same process-killing outcome — but the DMA client is
not exercised. The pass certifies that a GPU permission fault on revoked host
pages reaches this process and the detector path fires; it says nothing about
the XDMA path. Kept in the README under *Limits*.

## Not run

The long `--mode cycle` leg and any engine boot/window: parent's step. Nothing
touched `:8009`, the supervisor or any prod process.
