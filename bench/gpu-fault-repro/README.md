# GPU fault repro — minimal HIP control for the recurring permission faults

Fault track, measurement contract Phase 4, first discriminator
(`/home/srcds/.opencode/plan/measurement-contract-speed-attribution-faults.md`).
Code and docs only: nothing here starts an engine, touches `:8009` or the
running prod pair.

## What it tests

The recurring fault is a kernel-side page permission fault on the engine's host
pages, logged on the XDMA client (vmid 8) on card 1 (GPU 09:00.0), which kills
the generation after 5–15 minutes under serving load. Every explanation so far
is a reading of code beside the kernel log; nothing has run the same access
pattern without Strata.

This program is that control. Two modes:

- **`--mode file` (default).** It creates a 64 MiB file in the run directory,
  `mmap(MAP_SHARED)`s it, registers that mapping with `hipHostRegister` as
  mapped, and the device scans the registered file mapping. The churn thread
  rewrites the same file in place, `fsync`s, `posix_fadvise(DONTNEED)`s it and
  renames the park slot over. The device's pages and the churned pages are the
  same pages, so a kernel that invalidates the file's page-cache mapping under
  DONTNEED would take the device's PTEs with it. That is the fault signature.
- **`--mode anon`.** The earlier control: a 64 MiB `hipHostMalloc` mapped
  buffer, with the park churn on a separate `park.tmp`/`park.bin` pair. Pinned
  pages are not reclaimable, so this mode tests the mapping kernel path, not the
  churn coupling.

Both modes let the host rewrite memory while the device mapping is live:
`--host-write-sec N` (default 30) replaces one word per page, per tick, in the
same mapping the device reads, and updates the expected checksums. That is the
CPU-write-into-a-live-device-mapping trigger the earlier version lacked;
`--host-write-sec 0` turns it off.

Checksums are per page: the integer sum of its words plus the word-index
weighted sum `sum i*w[i]`. The weighted sum catches permutations and
compensating bit changes that the plain sum misses.

Every 10 s a heartbeat logs the buffer's host and device VA ranges, one page's
checksums, `/proc/self/statm`, `Cached` and `MemAvailable`, and the churn
tick count. A checksum mismatch logs the page's host and device VA and dumps
`/proc/self/maps`. A HIP fault logs the raw HIP code, the buffer ranges and the
maps. SIGABRT/SIGSEGV/SIGBUS/SIGILL (the usual way a permission fault kills a
process) records the last completed scan and dumps the maps from a signal
handler on an alternate stack.

## The differential

| outcome | meaning |
|---|---|
| faults with no Strata code running | the cause is in the platform space: kernel/amdgpu-KFD SVM path, ROCm user space, or the card. The engine's buffer handling is exonerated as the *origin*; only a config or hardware change can help. |
| clean for hours with the same churn | the bare access pattern does not fault; suspicion moves to the engine's host-buffer lifetime (SVM range registration, pool growth, park/restore churn) and the attribution shim becomes the next step. |

A clean run is evidence about this one access pattern, not proof that every
engine path is innocent — see *Limits*.

## Build

Needs the ROCm 6.1.0 gfx906 userspace and, on this box, GCC 13 headers for the
ROCm clang (clang 17 cannot parse the default GCC 15.2 libstdc++). The
CMakeLists forces `CMAKE_HIP_ARCHITECTURES=gfx906` before `project()` (the
`amdgpu-arch` probe here is poisoned by `HSA_OVERRIDE_GFX_VERSION`), and sets
`--gcc-install-dir`, `-isystem /opt/rocm-6.1.0/include` and
`-L/opt/rocm-6.1.0/lib` itself. Never build or link against `/opt/rocm` 7.x.

```sh
cd bench/gpu-fault-repro
PATH=/opt/rocm-6.1.0/bin:$PATH cmake -S . -B build -G Ninja \
  -DCMAKE_HIP_COMPILER=/opt/rocm-6.1.0/llvm/bin/clang++ \
  -DCMAKE_PREFIX_PATH=/opt/rocm-6.1.0
PATH=/opt/rocm-6.1.0/bin:$PATH cmake --build build -j
```

Misbuild check, with and without the runtime path. Without
`LD_LIBRARY_PATH` this box resolves `libhsa-runtime64` from `/opt/rocm` 7.x, so
the env-free read is a build check only:

```sh
readelf -d build/hip-fault-repro | grep NEEDED          # only libamdhip64.so.6
ldd build/hip-fault-repro | grep -E 'amdhip|hsa|comgr'  # no env: hsa may be 7.x
LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib \
  ldd build/hip-fault-repro | grep -E 'amdhip|hsa|comgr'
```

Expected with the env: `libamdhip64.so.6`, `libhsa-runtime64.so.1`,
`libamd_comgr.so.2` from `/opt/rocm-6.1.0/lib`; any `libamdhip64.so.7` is a
misbuild. The start line of the log records `hipRuntimeGetVersion`,
`hipDriverGetVersion` and the `libhsa-runtime64` path actually mapped, so every
run states which runtime it loaded.

## Run

Before the window, with the engine down and the run dir empty:

1. Pre-boot gate: `MemAvailable >= 15 GiB` (`grep MemAvailable /proc/meminfo`).
   The contract's pre-boot gate; during-leg values are recorded, not gated.
2. Exclusivity: `fuser -v /dev/kfd` and `rocm-smi --showpids` must show no
   other process on the card; `ps -eLo psr,pcpu` must show nothing above ~50%
   of a core on the stage cores.
3. Confirm the start line's `pci=` is `0000:09:00.0` (card 1). `--dev 1` is
   not enough on this box: an iGPU enumerates too, and `HSA_OVERRIDE_GFX_VERSION`
   makes arch strings agree across cards. Pass `--pci 0000:09:00.0` to pin it;
   without `--pci`, the program refuses any device whose `gcnArchName` is not
   `gfx906`.

```sh
cd bench/gpu-fault-repro
mkdir -p run
LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib \
HSA_OVERRIDE_GFX_VERSION=9.0.6 \
./build/hip-fault-repro --dev 1 --pci 0000:09:00.0 --duration-sec 1800 \
  --mode file --host-write-sec 30 \
  --dummy-dir ./run --log "./run/repro-$(date +%Y%m%d-%H%M).log" &
tail -f ./run/repro-*.log          # live progress: HB lines, churn ticks, faults
```

| flag | default | meaning |
|---|---|---|
| `--dev N` | 1 | device that reads the buffer |
| `--pci DDDD:BB:DD.F` | none | require this PCI address; overrides the arch check |
| `--duration-sec N` | 1800 | wall time before a clean exit |
| `--churn-sec N` | 2 | park-churn period (8 MiB rewritten per tick) |
| `--host-write-sec N` | 30 | host writes one word per page this often; 0 = off |
| `--mode file|anon` | file | coupled file mapping, or the old anon buffer |
| `--dummy-dir DIR` | `./run` | run directory (scan.bin / park files, created) |
| `--log FILE` | `hip-fault-repro.log` | append-only log (parent dirs created) |

Stop procedure: send `SIGTERM` (or `SIGINT`), wait for the `DONE` line, and
check the exit code — a stopped run must end at 0 or 5, never silently. Then
capture the log and the kernel slice below. If the process was killed by a
signal, the log ends with `FATAL signal` and the maps; capture it before
restarting anything.

## What to capture

1. The log: the `start:` line (device, `pci=`, arch), `runtime:` lines,
   `HB` heartbeats, `churn`, `host write round` lines, and on a fault the
   `CHECKSUM FAULT` / `HIP FAULT` / `FATAL signal` block with its maps dump.
2. The kernel side of the same minutes —
   `journalctl -k --since '-2h' | grep -Ei 'XDMA|permission fault|amdgpu|KFD|svm'` —
   since the faulting address lives there, not in HIP.
3. `rocm-smi --showmeminfo vram --showtemp` at start and end, and
   `/proc/meminfo` `Cached`/`MemAvailable`/`SwapCached` around the window
   (the heartbeat already samples the first two).
4. `uname -r`, the ROCm version (`cat /opt/rocm-6.1.0/.info/version`) and the
   boot state.

Clean exit prints
`DONE scans=… elapsed=…s, no HIP fault, no checksum fault`. A run whose churn
under-delivered prints `DONE scans=… elapsed=…s, CHURN DEFECT ticks=… expected~…`.
Exit codes: 0 clean, 2 usage/setup, 3 HIP fault, 4 checksum fault, 5 churn
defect, 128+signal when a signal ended it.

## Limits (read before interpreting a clean run)

- **Pinned pages are not reclaimable — the decisive premise.** `hipHostRegister`
  pins the range, so the kernel cannot swap or reclaim the pages the device
  reads. Any fault mechanism that needs page reclaim of *those* pages is out of
  reach here by construction; what the file mode can exercise is the mapping's
  invalidation (DONTNEED/rename under a live device mapping), not reclaim of
  pinned memory. A clean run therefore cannot exonerate engine paths that
  depend on the kernel reclaiming or re-registering host ranges.
- **Whether fadvise actually drops the mapped pages is kernel-dependent.**
  `posix_fadvise(DONTNEED)` skips pages that are mapped, and the file mapping
  stays mapped in this process for the whole run. If the kernel leaves them,
  the file mode's churn is pressure on the file without PTE effects; the `Cached`
  fall in the heartbeat and the kernel journal are the evidence of what
  happened. Treat a clean file-mode run as "the kernel did not invalidate this
  mapping", not as "the kernel never invalidates GPU PTEs".
- **Scans are maximally hot.** A full 64 MiB read takes roughly 11 ms over PCIe,
  so every page is touched every ~11 ms; the engine touches host pages in
  bursts — a layer's worth now, the next one seconds later. Hot, uniform,
  read-only-after-write access is the easiest case for a mapping to survive.
- **The churn rate is far below the working set.** 8 MiB every 2 s is ~4 MiB/s
  against the engine's ~40 GiB of parked/working data; the pressure is shaped
  like the park, not sized like it.
- **One copy queue.** The device reads through one kernel launch loop with a
  synchronize each scan; the engine runs many queues and its helper's DMA
  alongside. Queue concurrency and ordering are not reproduced.
- **The checksums are weak evidence.** Integer sum plus weighted sum catches
  replaced or zero-filled pages, permutations and large bit changes; it does
  not catch equal-sum corruptions, and it says nothing about pages the device
  never read before a fault. Its real job is to keep the process alive and
  reporting when memory is silently wrong, because a permission fault usually
  surfaces as `hipErrorIllegalAddress` / `hipErrorLaunchFailure` or kills the
  process outright.
- **HIP does not give the faulting address.** The log records the buffer's VA
  ranges and the page being scanned; the exact GPU VA comes from `dmesg`, so
  both must be captured together.
- **Untested on hardware.** This commit is code plus docs. The source compiles
  clean with ROCm 6.1.0 clang for gfx906 (`-Wall -Wextra`, host and device
  pass) and that is the only validation it has. The first run is a
  parent-window step.
