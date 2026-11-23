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

This program is that control. It does only what the fault needs and nothing the
engine does:

- one 64 MiB host buffer, `hipHostMalloc` mapped (fallback: malloc +
  `hipHostRegister` mapped), filled with a known pattern;
- device 1 (the second Radeon VII, the card in the fault history) reads all
  16384 pages continuously through the device-side mapping;
- a host thread reproduces the conversation-cache park churn that precedes the
  faults: write 8 MiB, `fsync`, `posix_fadvise(POSIX_FADV_DONTNEED)`, rename
  it live, repeat every 2 s;
- every 10 s a heartbeat with the buffer's host and device VA ranges, a
  device-computed page checksum, `/proc/self/statm`, `Cached` and
  `MemAvailable`;
- on a checksum mismatch: the page's host and device VA, both checksums, and a
  full `/proc/self/maps` dump in the log (the pair of VAs is what a post-mortem
  matches against the kernel's `amdgpu`/XDMA line);
- on a HIP fault: the raw HIP error code, the buffer's host and device VA
  ranges, and the same maps dump. A SIGABRT/SIGSEGV/SIGBUS (the usual way this
  fault kills a process) writes the last completed scan number and the maps
  from a signal handler.

## The differential

| outcome | meaning |
|---|---|
| faults with no Strata code running | the cause is in the platform space: kernel/amdgpu-KFD SVM path, ROCm user space, or the card. The engine's buffer handling is exonerated as the *origin*; only a config or hardware change can help. |
| clean for hours with the same churn | the bare access pattern does not fault; suspicion moves to the engine's host-buffer lifetime (SVM range registration, pool growth, park/restore churn) and the attribution shim becomes the next step. |

A clean run is evidence about this one access pattern, not proof that every
engine path is innocent — see *Limits*.

## Build

Needs the ROCm 6.1.0 gfx906 userspace and, on this box, GCC 13 headers for the
ROCm clang (clang 17 cannot parse GCC 16 libstdc++). The CMakeLists sets
`--gcc-install-dir`, `-isystem /opt/rocm-6.1.0/include` and
`-L/opt/rocm-6.1.0/lib` itself. Never build or link against `/opt/rocm` 7.x.

```sh
cd bench/gpu-fault-repro
PATH=/opt/rocm-6.1.0/bin:$PATH cmake -S . -B build -G Ninja \
  -DCMAKE_HIP_COMPILER=/opt/rocm-6.1.0/llvm/bin/clang++ \
  -DCMAKE_HIP_ARCHITECTURES=gfx906 \
  -DCMAKE_PREFIX_PATH=/opt/rocm-6.1.0
PATH=/opt/rocm-6.1.0/bin:$PATH cmake --build build -j
```

Misbuild check (expected, from `AGENTS.md`):

```sh
LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib \
  ldd build/hip-fault-repro | grep -E 'amdhip|hsa|comgr'
```

`libamdhip64.so.6`, `libhsa-runtime64.so.1`, `libamd_comgr.so.2` →
`/opt/rocm-6.1.0/lib`. Any `libamdhip64.so.7` is a misbuild.

## Run

Run inside a window the operator has opened. When prod occupies card 1, the
operator must agree that the repro is the only workload on that card.

```sh
cd bench/gpu-fault-repro
LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib \
HSA_OVERRIDE_GFX_VERSION=9.0.6 \
./build/hip-fault-repro --dev 1 --duration-sec 1800 \
  --dummy-dir ./run --log "./run/repro-$(date +%Y%m%d-%H%M).log"
```

| flag | default | meaning |
|---|---|---|
| `--dev N` | 1 | device that reads the buffer (1 = second Radeon VII) |
| `--duration-sec N` | 1800 | wall time before a clean exit |
| `--churn-sec N` | 2 | park-churn period (8 MiB per tick) |
| `--dummy-dir DIR` | `./run` | where `park.tmp` / `park.bin` live |
| `--log FILE` | `hip-fault-repro.log` | append-only log |

## What to capture

1. The log: `HB` heartbeats, `churn tick` lines, and on a fault the
   `CHECKSUM FAULT` / `HIP FAULT` / `FATAL signal` block with its maps dump.
2. The kernel side of the same minutes —
   `journalctl -k --since '-2h' | grep -Ei 'XDMA|permission fault|amdgpu|KFD|svm'` —
   since the faulting address lives there, not in HIP.
3. `rocm-smi --showmeminfo vram --showtemp` at start and end, and
   `/proc/meminfo` `Cached`/`MemAvailable`/`SwapCached` around the window
   (the heartbeat already samples the first two).
4. `uname -r`, the ROCm version (`cat /opt/rocm-6.1.0/.info/version`) and the
   boot state.

Clean exit prints `DONE scans=… elapsed=… no HIP fault, no checksum fault`.
Exit codes: 0 clean, 2 usage/environment, 3 HIP fault, 4 checksum fault,
128+signal when a signal ended it.

## Limits (read before interpreting a clean run)

- **The checksum is weak evidence.** Each page gets a 64-bit integer sum plus a
  double sum over the same words re-read as floats. It catches a page whose
  contents change (reclaimed or replaced backing, zero fill, bit rot large
  enough to move the sum). It does not catch small equal-sum corruptions, and it
  says nothing about pages the device never read before a fault. Its real job is
  to keep the process alive and reporting when memory is silently wrong, because
  a permission fault usually surfaces as `hipErrorIllegalAddress` /
  `hipErrorLaunchFailure` or kills the process outright.
- **HIP does not give the faulting address.** The log records the buffer's VA
  ranges and the page being scanned, which is what a post-mortem needs to match
  against the `amdgpu`/XDMA line in the kernel journal. The exact GPU VA comes
  from `dmesg`, so both must be captured together.
- **One mapping, not the engine's lifetime.** A clean 30-minute run says a
  single mapped 64 MiB allocation under page-cache churn is stable on this
  machine. It does not reproduce SVM range registration churn, pool growth,
  park-of-a-live-range or the 40 GiB expert working set; that is the
  attribution shim's job on dedicated boots.
- **The churn is a stand-in.** `posix_fadvise(DONTNEED)` on a rewritten file
  applies the same page-cache pressure the conversation-cache park does, but it
  does not write the engine's cache format, does not touch the engine's
  directories and does not deliberately exhaust RAM. If the real fault needs a
  free-memory collapse, record `MemAvailable` beside the result and add a
  memory-pressure step in the harness around this binary, not inside it.
- **The scan is per page, the heartbeat shows one page.** Every scan checksums
  all 16384 pages, so a page that goes bad is found wherever it is; the `HB`
  line prints the page for that scan only to keep the line short.
- **Untested on hardware.** This commit is code plus docs. The source compiles
  clean with ROCm 6.1.0 clang for gfx906 (`-Wall -Wextra`, host and device
  pass) and that is the only validation it has. The first run is a
  parent-window step.
