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

Three modes, and only the first one carries the churn coupling:

- **`--mode cycle` (default).** A 64 MiB file is mapped and registered, then
  repeatedly taken through: device sync → `hipHostUnregister` → `munmap` →
  `posix_fadvise(DONTNEED)` → rewrite the pattern with `pwrite` → `mmap` →
  `hipHostRegister` → re-derive the device pointer → scan. Unmapping first is
  what makes DONTNEED effective — measured on this box, a mapped-and-pinned
  file keeps `Cached` unchanged under DONTNEED, while munmap-then-fadvise
  frees 64,868 kB. Each cycle therefore exercises eviction, refault and
  re-registration under a live device mapping, the shape of the engine's
  park/restore.
- **`--mode file`.** One registered file mapping, read continuously, with the
  park churn on the same file. Because the folios stay mapped and pinned, this
  mode tests branch 1 only: a registered mapping read at maximum rate is
  stable. A clean result here must **not** be recorded as "churn exonerated".
- **`--mode anon`.** The earlier control: a `hipHostMalloc` buffer with a
  separate `park.tmp`/`park.bin` churn. Same branch-1-only limit.

`--host-write-sec N` (default 30) rewrites one word per page per tick in the
mapping the device reads. That is a cache-coherence probe — it shows whether
CPU writes and device reads agree — not a fault trigger. `--host-write-sec 0`
turns it off.

Checksums are per page: the integer sum of its words plus the word-index
weighted sum `sum i*w[i]`. The weighted sum catches permutations and
compensating bit changes that the plain sum misses.

Every 10 s a heartbeat logs the buffer's host and device VA ranges, one page's
checksums, `/proc/self/statm`, `Cached`, `MemAvailable`, churn ticks and
completed cycles. A checksum mismatch logs the page's host and device VA and
dumps `/proc/self/maps`. A HIP fault logs the raw HIP code, the buffer ranges
and the maps. SIGABRT/SIGSEGV/SIGBUS/SIGILL records the last completed scan and
dumps the maps from a signal handler on an alternate stack.

### Positive control

Nothing in this program had ever produced a `HIP FAULT` line, so the detector's
fault path is unproven until it is forced:

```sh
./build/hip-fault-repro --mode cycle --positive-control --dev 1 --pci 0000:09:00.0 ...
```

It queues a scan, then `hipHostUnregister` + `munmap` + `fadvise` **while
that scan is in flight** — the engine's park/restore race, deliberately. The
control passes on a **memory-fault class** result: exit 3 with
`positive control: memory fault detected as expected`, or a signal kill
(128+signal) whose log ends in the `FATAL signal` block — a fault that killed
the process has proved the detector just as well. Exit 7 means the unmap was
rejected without a memory fault (`positive control INCONCLUSIVE`), e.g.
`hipErrorInvalidValue` from a busy range: that does **not** certify the
detector. Exit 6 means no error at all was returned; the fault path is unproven
and a clean long leg cannot be interpreted. Run it once per window, before the
long leg, and record the exit code and the `positive control:` log line.

## The differential

| result | what it supports |
|---|---|
| **cycle** faults (exit 3/4, or SIGABRT with a `FATAL signal` block) | the platform space: a 64 MiB file mapping taken through eviction, refault and re-registration under live device reads cannot survive it without any Strata code. The engine's buffer handling is exonerated as the *origin*; only a config or hardware change helps. |
| **cycle** clean for hours | this eviction/refault/re-registration path is stable on this box. It does **not** exonerate the engine's remaining deltas: pool growth, SVM range churn, concurrent queues, the 40 GiB working set. |
| **file/anon** clean | only that a registered mapping read at maximum rate is stable. No churn was applied to the scanned pages (mapped, pinned folios are skipped by `invalidate_mapping_pages`). This row must never be written as "churn exonerated". |
| **positive control** exit 3 or 128+signal with `FATAL signal` | the detector's fault path works: a memory fault was returned, or the fault killed the process. |
| **positive control** exit 6 or 7 | the race did not produce a memory fault (no error / non-fault error); a clean long leg is uninterpretable until the control passes. |

## Build

Needs the ROCm 6.1.0 gfx906 userspace and, on this box, GCC 13 headers for the
ROCm clang (clang 17 cannot parse the default GCC 15.2 libstdc++). The
CMakeLists forces `CMAKE_HIP_ARCHITECTURES=gfx906` before `project()` unless
it is defined (the `amdgpu-arch` probe here is poisoned by
`HSA_OVERRIDE_GFX_VERSION`), and sets `--gcc-install-dir`,
`-isystem /opt/rocm-6.1.0/include` and `-L/opt/rocm-6.1.0/lib` itself. Never
build or link against `/opt/rocm` 7.x.

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
misbuild. The start line records `hipRuntimeGetVersion`,
`hipDriverGetVersion` and the `libhsa-runtime64` path actually mapped, so every
run states which runtime it loaded.

## Run

Before the window, with the engine down and the run dir empty:

1. Pre-boot gate: `MemAvailable >= 15 GiB` (`grep MemAvailable /proc/meminfo`).
2. Exclusivity: `fuser -v /dev/kfd` and `rocm-smi --showpids` must show no
   other process on the card; `ps -eLo psr,pcpu` must show nothing above ~50%
   of a core on the stage cores.
3. Confirm the start line's `pci=` is `0000:09:00.0` (card 1). `--dev 1` is
   not enough on this box: an iGPU enumerates too, and `HSA_OVERRIDE_GFX_VERSION`
   makes arch strings agree across cards. Pass `--pci 0000:09:00.0` to pin it;
   without `--pci`, the program refuses any device whose `gcnArchName` does not
   start with `gfx906`.

Positive control first (expect exit 3), then the long leg:

```sh
cd bench/gpu-fault-repro
export LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib
export HSA_OVERRIDE_GFX_VERSION=9.0.6
mkdir -p run
./build/hip-fault-repro --dev 1 --pci 0000:09:00.0 --mode cycle --positive-control \
  --duration-sec 120 --dummy-dir ./run --log ./run/control.log; echo "control exit=$?"
./build/hip-fault-repro --dev 1 --pci 0000:09:00.0 --mode cycle --duration-sec 1800 \
  --cycle-sec 30 --host-write-sec 30 --dummy-dir ./run \
  --log "./run/repro-$(date +%Y%m%d-%H%M).log" &
tail -f ./run/repro-*.log          # live progress: HB lines, cycles, churn, faults
```

| flag | default | meaning |
|---|---|---|
| `--dev N` | 1 | device that reads the buffer |
| `--pci DDDD:BB:DD.F` | none | require this PCI address; overrides the arch check |
| `--duration-sec N` | 1800 | wall time before a clean exit |
| `--churn-sec N` | 2 | park-churn period (8 MiB rewritten per tick) |
| `--cycle-sec N` | 30 | cycle period in `--mode cycle`; 0 = cycle before every scan |
| `--host-write-sec N` | 30 | host coherence probe, one word per page; 0 = off |
| `--mode cycle\|file\|anon` | cycle | see *What it tests* |
| `--positive-control` | off | in-flight unmap race; expects exit 3 |
| `--dummy-dir DIR` | `./run` | run directory (scan.bin / park files, created) |
| `--log FILE` | `hip-fault-repro.log` | append-only log (parent dirs created) |

Stop procedure: send `SIGTERM` (or `SIGINT`), wait for the `DONE` line, and
check the exit code — a stopped run must end at 0 or 5, never silently. The
churn gate measures ticks against the time actually elapsed, so a documented
early stop does not read as a churn defect. Then capture the log and the kernel
slice below. If the process was killed by a signal, the log ends with
`FATAL signal` and the maps; capture it before restarting anything.

## What to capture

1. The log: the `start:` line (device, `pci=`, arch, mode), `runtime:` lines,
   `positive control:` (first run), `HB` heartbeats, `cycle` lines with
   `Cached before=… after-fadvise=… after-rewrite=…` (the **middle** sample is
   the eviction evidence; the third shows the rewrite refilled the cache),
   `churn`, `host write round`,
   and on a fault the `CHECKSUM FAULT` / `HIP FAULT` / `FATAL signal` block
   with its maps dump.
2. The kernel side of the same minutes —
   `journalctl -k --since '-2h' | grep -Ei 'XDMA|permission fault|amdgpu|KFD|svm'` —
   since the faulting address lives there, not in HIP.
3. `rocm-smi --showmeminfo vram --showtemp` at start and end, and
   `/proc/meminfo` `Cached`/`MemAvailable`/`SwapCached` around the window
   (the heartbeat samples the first two, the cycle lines the first).
4. `uname -r`, the ROCm version (`cat /opt/rocm-6.1.0/.info/version`) and the
   boot state.

Clean exit prints
`DONE scans=… elapsed=…s, no HIP fault, no checksum fault`. A run whose churn
under-delivered prints `DONE scans=… elapsed=…s, CHURN DEFECT ticks=… expected~…`.
Exit codes: 0 clean, 2 usage/setup, 3 HIP fault (also the positive control's
pass), 4 checksum fault, 5 churn defect, 6 positive control failed (no error at
all), 7 positive control inconclusive (rejected without a memory fault),
128+signal when a signal ended it (a control killed by a signal passes).

## Limits (read before interpreting a clean run)

- **`file`/`anon` mode proves nothing about the churn.** Mapped and pinned
  folios are skipped by `posix_fadvise(DONTNEED)` — measured here: mapped +
  fadvise leaves `Cached` unchanged, munmap + fadvise frees 64,868 kB. A clean
  `file`/`anon` run says only that a registered mapping read at maximum rate is
  stable; never record it as "churn exonerated".
- **The cycle is quiescent.** The device is synced before unregister and
  re-registered before the next scan; the engine's park/restore can overlap DMA
  work. The cycle reproduces the eviction/refault/re-registration sequence, not
  its overlap with other queues.
- **A stale mapping that still reads the same pattern is invisible to the
  checksums.** After `munmap`, `fadvise` and `pwrite` the file holds the same
  bytes as before, so a stale device PTE that resolves to the same page content
  produces no mismatch. What fires instead is the HIP fault path when the stale
  translation is invalid, or the checksum when it returns zeros or other data.
  That is why the positive control exists: it proves the fault path, and the
  `after-fadvise` sample in the cycle line plus the kernel journal are the
  eviction evidence.
- **HIP does not give the faulting address.** The log records the buffer's VA
  ranges and the page being scanned, which is what a post-mortem needs to match
  against the `amdgpu`/XDMA line in the kernel journal. The exact GPU VA comes
  from `dmesg`, so both must be captured together.
- **The host write is a probe, not a trigger.** One word per page per 30 s
  checks CPU/device coherence in a live mapping; it is not a churn or reclaim
  source. In cycle mode the pattern rewrite restores the initial contents, so
  the probe restarts from a known state each cycle.
- **The checksums are weak evidence.** Integer sum plus weighted sum catches
  replaced or zero-filled pages, permutations and large bit changes; it does
  not catch equal-sum corruptions, and it says nothing about pages the device
  never read before a fault.
- **Scans are maximally hot.** A full 64 MiB read takes roughly 11 ms over PCIe,
  so every page is touched every ~11 ms; the engine touches host pages in
  bursts. Hot, uniform, read-only-between-writes access is the easiest case for
  a mapping to survive.
- **The churn rate is far below the working set.** 8 MiB every 2 s is ~4 MiB/s
  against the engine's ~40 GiB of parked/working data.
- **One copy queue.** The device reads through one kernel launch loop with a
  synchronize each scan; the engine runs many queues and its helper's DMA
  alongside.
- **`hipHostRegister` on a file-backed mapping can be refused.** If setup
  exits 2 with `SETUP ERROR hipHostRegister(file mapping)`, record the error
  and run `--mode anon`; the refusal is itself a platform fact about this
  kernel/driver.
- **Untested on hardware.** This commit is code plus docs. The source compiles
  clean with ROCm 6.1.0 clang for gfx906 (`-Wall -Wextra`, host and device
  pass) and that is the only validation it has. The first run is a
  parent-window step.
