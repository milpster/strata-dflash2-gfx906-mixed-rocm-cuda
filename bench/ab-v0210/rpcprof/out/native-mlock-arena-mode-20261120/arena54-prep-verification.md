# arena-54 trial preparation (oracle ruling 2026-11-20) - fixer lane

Date: 2026-11-20. Worktree `/mnt/srcds/Strata-wt-helper-ipc` at `bde983cf41` (builds only,
no new worktree commits). Main-tree edits limited to the three authorized files, committed
as `43c6b20655` on `tg-stall-cause`. No boot, no GPU execution, no serving/port/signal
operation; prod on :8009 untouched throughout.

## TASK A - CUDA expert helper

`strata-expert-helper-patient` is what `tools/mixed_gpu/build_l3.sh:83` produces: a copy of
the built `strata-expert-helper`. The reply-patience override it is named for is in-tree at
this HEAD (`tools/mixed_gpu/sidecar/helper_main.cpp:89,94`), so the copy is not a lie.

The standing CUDA trees in `build-mixed-gpu/` had lost their generated build files
(`build-mixed-gpu/rpcw-cuda/` holds only `CMakeCache.txt`), so a fresh derive tree was
configured from the frozen golden conf the standing script uses:

```
cmake -S . -B build-mixed-gpu/rpcw-cuda-arena54 -C tools/mixed_gpu/golden/rpcw-cuda.cmake \
  -DCMAKE_PREFIX_PATH=/home/srcds/rocm-gfx906-xnack;/opt/rocm-6.1.0 \
  -DROCM_PATH=/opt/rocm-6.1.0 -DHIP_PATH=/opt/rocm-6.1.0 -DHIP_ROOT_DIR=/opt/rocm-6.1.0
  -> configure exit 0   (generator: Unix Makefiles, per the frozen conf)
nice -n 19 taskset -c 8-15 cmake --build build-mixed-gpu/rpcw-cuda-arena54 \
  --target strata-expert-helper -j 2
  -> build exit 0
/usr/local/cuda/bin/cuobjdump --list-elf strata-expert-helper | grep -o 'sm_[0-9]*'  -> sm_86
cp strata-expert-helper strata-expert-helper-patient
```

Full command log: `helper-build.txt` in this directory.

| binary | sha256 |
| --- | --- |
| new `strata-expert-helper` and `-patient` (identical, 52,645,392 B) | `81e3e8fd5e5964fcc42c705ece4c78d26112249285497a34899b4c53a92aa40f` |
| frozen reference it replaces (`v0238p0/rpcw-cuda/strata-expert-helper-patient`) | `553f3bf48219c99298e4620547507eac7eefa29d660ee32e806bb9780a9ab361` |

`ldd` (recorded, never executed) and `readelf -d` of the new binary match the frozen
reference exactly: NEEDED = libcudart.so.13, libstdc++, libm, libgcc_s, libc;
RUNPATH = `/usr/local/cuda-13.1/targets/x86_64-linux/lib`. That matters because the
launcher starts the helper under `env -i` - only the RUNPATH can find libcudart.

## TASK B - pin v0239 (main tree, untracked like v0238p0)

`build-906/strata` sha256 verified equal to `c4c4a5ed2d7632466737adecdb1b0352d38446fcc373afc88ad7d8bd149b69a5`
BEFORE copying. `/mnt/srcds/Strata/v0239/SHA256SUMS.txt`:

```
c4c4a5ed2d7632466737adecdb1b0352d38446fcc373afc88ad7d8bd149b69a5  rpcw-hip/strata
81e3e8fd5e5964fcc42c705ece4c78d26112249285497a34899b4c53a92aa40f  rpcw-cuda/strata-expert-helper-patient
```

`sha256sum -c SHA256SUMS.txt` -> both OK. Only the two artifacts the trial launcher uses are
pinned; the non-`-patient` helper copy was left in the build tree.

## TASK C - trial-config-arena54.json (4 insertions, 3 deletions)

`"exe"` -> `/mnt/srcds/Strata/v0239/rpcw-hip/strata`; env gains `"STRATA_ARENA_MMAP": "1"`
next to the existing `"STRATA_NATIVE_MLOCK": "arena"`; `"log"` ->
`/mnt/srcds/Strata/logs/engine-trial-arena54-20261120.log` (was prod's
`engine-prod-ud-iq3xxs-v0233.log`). Nothing else changed - the vision sub-config's own
`"exe"` (`build-vision/bin/strata-vision`) was left alone.

Validation: `.venv/bin/python -c "json.load(...)"` -> exit 0, and it echoed
`env NATIVE_MLOCK = 'arena'  ARENA_MMAP = '1'`, `exe = .../v0239/rpcw-hip/strata`,
`log = .../engine-trial-arena54-20261120.log`, `port = 8019`.

Note for whoever reads `-u STRATA_ARENA_MMAP` in the launcher and worries: `serve/server.py`
`child_env()` (line 4531) re-applies the config's `env` map to the engine, so the
engine does get both variables. Documented in the launcher header.

## TASK D - launch-trial-arena54.sh (45 changed lines)

- `BIN=/mnt/srcds/Strata/v0239`.
- cgroup props computed: defaults `MemoryMax=56G`, `MemoryMin=51G`, `MemorySwapMax=4G`;
  `STRATA_TRIAL_MEMORY_MAX` / `STRATA_TRIAL_MEMORY_MIN` / `STRATA_TRIAL_SWAP_MAX` override,
  and an empty value or `none` omits that `-p` prop. Expansion is
  `${cgroup_props[@]+"${cgroup_props[@]}"}` so it stays safe under `set -u`.
- `STRATA_MEMORY_LIMIT_MIB` 53760 -> 55296.
- header comment rewritten (it still described relief 24000/28000, 56320 MiB and 48G):
  now names the pin, the mmap + arena-only-lock design and which variables carry it, the
  56G/51G/4G wall with the 46.84 GiB reason, the 54 GiB self-limit, and the first-run
  pre-generation requirement.

`bash -n` -> exit 0. Props logic probed from the file itself:

```
defaults                -> -p MemoryMax=56G -p MemoryMin=51G -p MemorySwapMax=4G
STRATA_TRIAL_SWAP_MAX=8G -> -p MemoryMax=56G -p MemoryMin=51G -p MemorySwapMax=8G
STRATA_TRIAL_MEMORY_MAX= (empty) -> -p MemoryMin=51G -p MemorySwapMax=4G
all three = none        -> (no -p props at all)
```

The env survives the supervisor hop (`pair_supervisor.py:282` passes `dict(os.environ)` to
the `--pair-child` launcher), so the driver can lift the caps for the pre-gen boot.

## TASK E - bench/ab-v0210/rpcprof/gpu-rounds/arena54-window-20261120.sh (140 changed lines)

Structure kept: stop-all -> boot -> verify -> T0 -> pressure -> T1 -> restore -> prod
restore + completion check. Added:

- **E1 pre-gen phase**, before the measured boot: skips the boot when `experts.bin` is
  already the exact expected size (a mapping boot never prints the wrote line, so polling
  for one would burn the 20 minutes for nothing); otherwise boots the trial launcher with
  all three caps `none`, polls the engine log for
  `STRATA_ARENA_MMAP: wrote` / `the next start maps it` for up to 20 min, and aborts early
  on `could NOT write` / `NOT writing`. Then stop-all, sleep 8, and a hard assert that
  `stat -c %s /mnt/srcds/ai/iq3xxs-ud/experts.bin` is exactly 50294988800 - on any other
  size (or a failed write) it prints the `iq_pack.py --experts-bin` warning (it writes the
  unpadded size and silently breaks the mmap size match), restores prod and exits 1. The
  phase comment says in words that this boot is not the measured one.
- **E2 boot gate**, after readiness and before any probe, on the measured boot's engine-log
  bytes only. PASS needs all of: `strata native mlock: locked`;
  `file pages locked, not pinned`; and
  `ArenaExpertSource shared expert arena progress chunk N: X of M MiB locked` with
  `M` equal to the expected MiB. FAIL (abort + restore + exit 1) on a missing pass line or
  on `STRATA_ARENA_MMAP: not locked`, `partial, not pinned`, `NOT writing`,
  `could NOT write`, `MAP_HUGETLB unavailable`, or `expert arena read` (the last is the
  string the anonymous-arena path prints, so it is what an actual fallback looks like).
  A comment records that `nothing dropped: this run maps` is expected, not a failure.
  VmLck is recorded at ready and again by every snap.
- **E3 snap() additions**: `VmLck`/`VmRSS` from `/proc/<engine>/status`; the experts.bin
  VMA block from its smaps (`KernelPageSize`, `AnonHugePages`, `FilePmdMapped`,
  `THPeligible`, `Rss`, `Dirty`, `Locked`); and `memory.events` next to the existing
  `memory.max/min/current` readout.

Supporting, in the same file: a `newlog <file> <offset>` helper (the engine log is opened
`"a"` by `serve/server.py:1271`, so each phase reads only its own boot's bytes - without
this, a stale `wrote` line from the pre-gen boot would pass the pre-gen check on the next
run), a shared `restore_prod`, and a `sha256sum -c` of the pin at window start.

`bash -n` -> exit 0. Helper self-tests (synthetic log and synthetic smaps, nothing booted):

- `newlog` excludes the pre-offset content: `strata native mlock: locked` count over the
  new bytes = 1 with a stale line sitting before the offset. An earlier version of the
  helper used the wrong `tail -c` offset and chopped the match mid-line; the self-test
  caught it and the helper was fixed.
- the progress-line regex yields `47965` and the equality check passes.
- the smaps awk picks exactly the experts.bin VMA and ignores the neighbouring ones.
- none of the six fail strings fires on the mmap-mode `nothing dropped: this run maps` note.

## One correction to the ruling

Its expected progress total **47944 MiB** is the *unpadded* arena. The span the engine
locks is the whole mapped file, arena plus one padding blob, and
`50294988800 >> 20 = 47965 MiB`; `pin_budget.hpp:287` prints that number. A gate on 47944
would have failed a correct run, so the script derives the expected total from the asserted
file size (`EXPECTED_MIB=$(( EXPERTS_BIN_BYTES / 1048576 ))` = 47965) instead of hardcoding.

## Facts the lane should know before it boots

- `/mnt/srcds/ai/iq3xxs-ud/experts.bin` does **not exist yet**, so the pre-gen boot is real.
- Free space on that filesystem is 56.87 GiB against the 48.84 GiB the engine's room test
  wants (`want + blob + 2 GiB`, `expert_source.cpp:6354`), so the write is allowed - with
  ~8 GiB left over. Any other writer on that volume during the pre-gen boot can flip
  `NOT writing` and the phase aborts (by design).
- The supervisor around the trial boot carries `--startup-timeout 1200` (20 min). The
  pre-gen boot is a 46.84 GiB anon load *plus* a 46.84 GiB write; if it exceeds 20 min the
  supervisor may rebuild the pair before the wrote line lands. The driver's own poll is
  also 20 min. If the lane sees that, raising `--startup-timeout` on the trial launcher is
  the fix (not changed here: out of the authorized scope).
- The window's prod restore uses `launch-v0238.sh`, which is the current :8009 launcher
  (state file `run/prod-swift15-f16kv-mixed-supervisor.json`, log tag
  `prod-swift15-f16kv-dflash-w4-q4_0-mixed-*`) - verified by reading it, not by booting it.

## NOT RUN

- No boot of anything (no pre-gen boot, no measured boot, no prod restart). No probe, no
  request, no port or signal operation - prod :8009 was not touched at all, not even read
  with `curl` (the window script does that when the lane runs it).
- No GPU execution. `cuobjdump`/`readelf`/`ldd` were run on binaries; nothing was executed.
- No mlock measurement: no RSS, swap, VmLck, `RLIMIT_MEMLOCK`, THP or tok/s figure exists
  for this configuration yet. `MemorySwapMax=4G` and the 56G wall are untested.
- The engine binary was not re-run through `ldd` here; its linkage was recorded with the
  `bde983cf41` build in `verification.md` in this directory.
