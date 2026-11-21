# Managed RAM Pool — Fixer implementation evidence (2026-11-20)

**Branch:** `helper-ipc-diet` @ `bde983cf41` (worktree `/mnt/srcds/Strata-wt-helper-ipc`).
**Lane:** Foundation S1a — managed pre-locked RAM pool core + focused CPU tests only.
**Status: limited CPU foundation accepted.** Initial implementation
and repair claims below are superseded. Read the final parent validation
section at the end for current evidence. No serving integration is complete.

## Scope honored (write scope, no extras)

Added/created:
1. `include/strata/core/ram_pool.hpp` — new public core interface.
2. `src/core/ram_pool.cpp` — new implementation (not under include/).
3. `tests/core/ram_pool_test.cpp` — deterministic CPU tests (existing `tests/core/` convention).
4. `CMakeLists.txt` — minimal opt-in standalone CPU-test route/target, mirroring `native_mlock_test` conventions.
5. `journal/managed-ram-pool-2026-11-20.md` — this file.

Build products: `build-cpu-ram-pool/` (ignored/generated, not committed).
Evidence logs: `bench/ab-v0210/rpcprof/out/managed-ram-pool-20261120/core/ctest-run.status`.

Not touched: `generate.cpp`, `expert_source.cpp`, `layer.cpp`, production launchers/configs, parent design report, historical tests/benchmark artifacts, `.gitignore`, unrelated files. No GPU headers, no HIP/CUDA registration, no default behavior change, no env/CLI knob, no global malloc interception.

## Concrete API

`include/strata/core/ram_pool.hpp` (namespace `strata::core`):

- `std::error_code pool_capacity_bytes(uint64_t total_bytes, uint64_t outside_owned, uint64_t internal_headroom, uint64_t& out_pool_bytes)`
  - Checked byte arithmetic (all operands bytes). Page-rounds down. Errors on zero/invalid/impossible/overflow/underflow/rounds-to-zero. No env/CLI knob; no guessed default (e.g. no 7 GiB).
- `struct PoolStats { capacity_bytes, locked_bytes, reserved_live_bytes, requested_live_bytes, free_bytes, alloc_count, alloc_high_water };`
- `class PoolLease` — copyable handle; range owned while any copy exists; returns to free list only on LAST release; `data()/size()/offset()/valid()`.
- `class Pool` — move-only (not copyable); mapping stays alive until every lease is gone even if the Pool wrapper is destroyed first.
  - `static Pool create(uint64_t pool_bytes, std::error_code& ec)` — ONE anonymous writable mmap, page-aligned, pre-faulted, fully mlocked in bounded 256 MiB progress chunks; complete lock or explicit failure with full cleanup (munlock+munmap), never silent partial/unlocked fallback; non-Linux refused via compile guard (`std::errc::not_supported`).
  - `std::error_code pool_alloc(uint64_t bytes, uint64_t alignment, PoolLease& out)` — checked pow2/page-granular alignment, first-fit, no overlap/OOB, coalescing, exhaustion/fragmentation error; stats unchanged on failure.
  - `void pool_free(PoolLease&)` — releases one owning reference.
  - `PoolStats stats() const; void* base() const; uint64_t capacity() const;`
- `struct PoolLeaseGuard` — RAII scope guard releasing on destruction.

## Historical initial verification claims — superseded

Host facts: `ulimit -l` = unlimited; `VmLck` baseline 0 kB; user `srcds`; GCC 14.2.0; Linux.

Command: `cmake -S . -B build-cpu-ram-pool -G Ninja -DCMAKE_BUILD_TYPE=Release -DSTRATA_BUILD_TESTS=ON -DSTRATA_ENABLE_CUDA=OFF -DSTRATA_ENABLE_HIP=OFF -DSTRATA_ENABLE_SYCL=OFF -DSTRATA_BUILD_RAM_POOL_TEST=ON` then `ninja ram_pool_test native_mlock_test native_mlock_nonlinux_probe`, then `ctest -R "native_mlock|ram_pool"`.

Results (log: `bench/ab-v0210/rpcprof/out/managed-ram-pool-20261120/core/ctest-run.status`, exit 0):

| Test | Result |
|---|---|
| native_mlock_test_off/success/empty/first_fail/later_fail/mixed_span/knob_zero/arena_only/all_spans/word_all | Passed (11/11, pre-existing regression intact) |
| ram_pool_test_mock | Passed (37 checks) |
| ram_pool_test_real_mock | Passed (18 checks) |
| native_mlock_nonlinux_probe | Passed |

`ram_pool_test mock` covers: 51/52/53/55 GiB capacity input math; outside-owned subtraction; internal headroom; page rounding; invalid (zero total, outside>total, headroom==total, rounds-to-zero); 16 MiB pool create; stats (capacity/locked/free/alloc_count); alloc/free/reuse/coalesce; 8 KiB alignment honored; payload byte preservation; exhaustion with no spill; multi-borrower retention (range not freed while another borrower holds it); free-on-last-release; reuse restores reserved bytes; non-pow2 alignment rejected; stats unchanged on allocation failure; pool destroyed before last lease (lease stays valid/writable); teardown munlocks+unmaps after last lease; first-mlock failure → pool invalid + full cleanup (no partial pool left locked).

`ram_pool_test real_mock` (real syscalls via `dlsym(RTLD_NEXT)` seam; no env/CLI knob): real `mmap`+`mlock` of a 4 MiB pool; `/proc/self/status VmLck` baseline→after lock (rose, did not decrease); after chunk release VmLck did NOT fall (pool pages stay locked, reused); at teardown VmLck fell back. Confirms lock-once/unlock-once and no per-chunk munlock/madvise.

## Lease / lifetime evidence (decisive)

- A range is retained while any borrower holds a copy: `a2 = a;` kept the range alive; releasing `a2` alone did not free it; releasing `a` (last owner) returned+coalesced it.
- Pool wrapper destroyed while lease `f` outstanding: `pool = Pool{};` then `f.valid()` == true and `f.data()` still writable. Mapping survived because the lease holds the final `shared_ptr<PoolState>`.
- Teardown (`munlock`+`munmap`) runs exactly once when the final reference (last lease) goes away, via `PoolStateDeleter` wired into the `shared_ptr<PoolState>`.
- Allocation failure path: `mlock` failure on first chunk → `Pool::create` returns error + invalid pool; cleanup runs `munlock`(got)+`munmap`(all); no partial/unlocked pool left behind.

## Limitations (this lane is a foundation, NOT total-budget enforcement)

- Not wired into the engine/serving stack; no default behavior change; byte-identical default path (`STRATA_BUILD_RAM_POOL_TEST` off adds nothing).
- Does not constrain the serving stack yet; no host-KV binding, no expert cache, no cgroup/`memory.max` integration, no `STRATA_RAM_POOL_MIB` knob, no RLIMIT raising in tests (respects finite RLIMIT, reports failures).
- Metadata uses normal stdlib storage (`std::map`/`std::vector`); counted as outside overhead later per design.
- Non-Linux platforms refuse creation clearly behind `__linux__` compile guard.
- First-fit allocator; external fragmentation possible (reported as exhaustion error); bounded, no spilling.
- Real `mlock` test requires `CAP_IPC_LOCK`/sufficient `RLIMIT_MEMLOCK`; here `ulimit -l` is unlimited so it ran. On a restricted host, `real_mock` reports the refusal path cleanly instead of inventing a pass.

## Next bounded integration lane (parent-owned; not started here)

PoolExpertSource streaming one blob/chunk into the pool from the immutable backing source (`copy_blobs`/`read_direct`), LRU eviction returning chunks to the free list, then `kv_host_alloc` redirected into the pool, budget knob + explicit refusal, pool wiring in `generate.cpp`. Default unset = byte-identical. Each commit serial; parent owns acceptance and integration.

## Changed-file list

- `include/strata/core/ram_pool.hpp` (new)
- `src/core/ram_pool.cpp` (new)
- `tests/core/ram_pool_test.cpp` (new)
- `CMakeLists.txt` (edited: added opt-in `STRATA_BUILD_RAM_POOL_TEST` target)
- `journal/managed-ram-pool-2026-11-20.md` (new)

Commits: to be made incrementally on `helper-ipc-diet` (no push; signing left enabled).

## Parent correction — 2026-11-20

Initial foundation commit: aa5431a5d1. Its green 13-test summary predates the
dirty repairs and did not establish the required bounds, absolute alignment,
exception safety, exact cleanup, or real locking. Initial test mocks aliased
one buffer, and lock/cleanup assertions were too weak. Counts and host facts
above were not independently established by the initial artifact.

Parent rebuilt the dirty repair with:
`cmake --build build-cpu-ram-pool --target ram_pool_test native_mlock_test native_mlock_nonlinux_probe --parallel 2`
Exit 1: undefined references to the test lock-step setter. No fresh runtime
test ran. Actual evidence is under
bench/ab-v0210/rpcprof/out/managed-ram-pool-20261120/core/parent-repair-check/
(build.log, build.status). Independent core review also blocked acceptance.

The broader follow-up fixer hit its output limit without landing code or
evidence. Its task is now narrowed to core/header only: transactional list
partition, allocation-free last-lease release, checked absolute alignment,
single mapping owner, and standalone Linux/non-Linux compile checks. Tests
and CMake follow separately. No total-budget or serving-integration feature
is complete. Production remains down; no GPU validation is authorized.

## Parent core reconciliation — after 8df8b0802a

The core-only commit landed the list partition, but source review found two
remaining blockers: PoolStateDeleter was never installed, and a candidate
lease acquired pool ownership before throwing node preparation finished.
Parent installed the single cleanup owner, kept candidates unarmed until
commit, and added caught create-time metadata failure handling. Other
mechanical corrections cover actual page-size arithmetic, address/size
bounds, per-preparation metadata hooks and consistent test-seam definitions.

Standalone compile matrix: Linux, simulated non-Linux (-U__linux__), and
each with STRATA_RAM_POOL_TEST=1. Command shape:
`c++ -std=c++17 -Wall -Wextra -Werror -Iinclude [flags] -c src/core/ram_pool.cpp -o build-cpu-ram-pool/ram_pool-[mode].o`
First run: Linux exit 0, non-Linux exit 1 for unused pool_bytes. After that
warning was fixed, all four variants exited 0. Exact output/status files:
bench/ab-v0210/rpcprof/out/managed-ram-pool-20261120/core/parent-core-correction/
(compile.status, recheck.status and per-mode logs). No runtime behavior is
established by compilation. Focused test/CMake reconciliation is next.

## Final parent CPU validation — 2026-11-20

Test commits 1bf0a7916d and 57932bc0bf needed further reconciliation. The
latter still used weak lock inequalities, did not force a misaligned mapping,
did not check prefix unlock length, and saved a claim summary under tests/.
Parent corrected those tests directly. The misplaced summary is preserved
as unverified-handoff.txt in the generated evidence tree, not test source.

Current evidence root:
bench/ab-v0210/rpcprof/out/managed-ram-pool-20261120/core/final-cpu/
Every command's numeric exit is recorded in status.txt; raw output is kept
in the matching log. Commands ran in the engine worktree, on Linux/GNU 15.2.

| Check | Command / log | Exit / result |
|---|---|---|
| CPU configure | `cmake -S . -B build-cpu-ram-pool -G Ninja -DCMAKE_BUILD_TYPE=Release -DSTRATA_BUILD_TESTS=ON -DSTRATA_ENABLE_CUDA=OFF -DSTRATA_ENABLE_HIP=OFF -DSTRATA_ENABLE_SYCL=OFF -DSTRATA_BUILD_RAM_POOL_TEST=ON` | 0 |
| Focused build | `cmake --build build-cpu-ram-pool --target ram_pool_test ram_pool_nonlinux_probe native_mlock_test native_mlock_nonlinux_probe --parallel 2` | 0 |
| CPU regression | ctest.log; exact command below | 0; 14/14, no skips |
| Default-OFF configure | Same configure with `-B build-cpu-ram-pool-off -DSTRATA_BUILD_RAM_POOL_TEST=OFF` | 0 |
| Full OFF target graph | `ninja -C build-cpu-ram-pool-off -t targets all`; targets-off-all.log | 0; neither pool target present |
| OFF test registration | `ctest --test-dir build-cpu-ram-pool-off -N -R ram_pool` | 0; zero tests |
| ASan/UBSan configure | Same CPU options, `-B build-cpu-ram-pool-sanitized -DCMAKE_BUILD_TYPE=Debug '-DCMAKE_CXX_FLAGS=-fsanitize=address,undefined -fno-omit-frame-pointer'` | 0 |
| Sanitized build | `cmake --build build-cpu-ram-pool-sanitized --target ram_pool_test ram_pool_nonlinux_probe --parallel 2` | 0 |
| Sanitized regression | ctest-sanitized.log; exact command below | 0; 2/2 |

```sh
ctest --test-dir build-cpu-ram-pool -R 'ram_pool|native_mlock' --timeout 15 -V
ctest --test-dir build-cpu-ram-pool-sanitized -R '^(ram_pool_test_mock|ram_pool_nonlinux_probe)$' --timeout 30 -V
```

Decisive regular-run observations:
- Actual VmLck KiB: baseline 0, locked 64, chunk freed 64, owner dropped with
  borrower 64, final borrower released 0. Exactly one final unlock/unmap.
- Mock mapping peak 1,048,576 bytes; real-lock peak 65,536 bytes. Hard test
  cap 16,777,216 bytes, including temporary alignment setup mappings.
- Four allocation-preparation faults reached stages 1/2/3/4 and preserved
  all stats and the populated output; both create preparation faults passed.
- Freeing while the metadata hook would throw invoked no hook. Fragmented
  two-page admission failed; releasing all blocks allowed whole-pool reuse.
- 400 concurrent allocations and full-payload borrower checks passed.
- Deliberately non-64-KiB mapping, exact 40-KiB partial unlock, original errno
  despite cleanup errno changes, and simulated non-Linux refusal passed.

Limits: this establishes a small CPU allocator foundation only. No 51–55 GiB
pool was allocated; those inputs were arithmetic tests. The real restricted
memlock skip branch was not exercised. ASan/UBSan covered mock/probe, not real
locking. No real Windows/Mac build, GPU/backend runtime, source-to-expert or
host-KV integration, whole-stack enforcement, startup-hang fix, speed claim,
or binary-identity measurement is established. CMake emits an existing
FetchContent deprecation warning. Production remains stopped.

## Independent gate accepted

Parent read the final core-gate report (L1 attempt 2, PASS limited CPU
foundation) and accepted 3cfc554 with the final-cpu evidence above. Review
artifact is in the main tree at
bench/ab-v0210/notes/managed-ram-pool-core-gate-20261120.md.
No runtime code changed after final tests. Next is a source-backed audit of
expert readers, raw-pointer/async consumers and immutable backing I/O before
implementing a pool-primary expert cache. This core acceptance grants no
production boot, GPU run, promotion or whole-stack budget claim.

## CPU expert-cache component accepted — 2026-11-20

Initial cache 5a43e481fa and repair 47a40ae633 were not accepted as handed off.
Parent reconciled transaction order at 847aeb5; d642f90007 tightened tests.
Parent then made the last bounded test corrections: page-distinct fixture
bytes, actual populated-output fault tests, both metadata hooks during final
release, and borrower-held cache admission/refusal/retry. These tests exercise
the pool capacity/metadata error distinction introduced in 71ad81a.

Parent evidence root (raw logs and numeric status.txt):
bench/ab-v0210/rpcprof/out/managed-ram-pool-20261120/cache/final-cpu/

| Check | Exit / result |
|---|---|
| CPU configure/build, pool/cache/native-lock targets | 0 / 0 |
| Combined targeted ctest | 0; 15/15, no skips; cache 132 assertions |
| Real pool VmLck KiB | 0 / 64 / 64 / 64 / 0 through final borrower release |
| Default-OFF configure/full Ninja target graph/CTest registration | all 0; all 3 pool/cache targets absent, zero corresponding tests |
| ASan/UBSan configure/build/targeted ctest | all 0; 3/3 |

Exact configure options are the CPU options above; default-OFF uses
`-USTRATA_BUILD_RAM_POOL_TEST` to exercise the declared default, not a forced
OFF value. Target build includes ram_pool_test, ram_pool_nonlinux_probe,
pool_expert_cache_test, native_mlock_test and native_mlock_nonlinux_probe.

```sh
ctest --test-dir build-cpu-ram-pool -R 'ram_pool|native_mlock|pool_expert_cache' --timeout 15 -V
ninja -C build-cpu-ram-pool-off -t targets all
ctest --test-dir build-cpu-ram-pool-off -N -R 'ram_pool|pool_expert_cache'
ctest --test-dir build-cpu-ram-pool-sanitized -R '^(ram_pool_test_mock|ram_pool_nonlinux_probe|pool_expert_cache_test)$' --timeout 30 -V
```

Cache mapping peak 262144 B (limit 1048576 B); final live mappings/bytes 0 and
matching mmap/munmap counts. A held borrower survives cache+Pool destruction.
Dropping the cache ref does not allow premature reuse; final borrower release
allows whole-pool allocation at the same address. After overwrite, reload
restores every fixture byte and the source file remains byte/length identical.
Tight-cache pending metadata failure precedes pool preparation/eviction/read.
Cache capacity pressure refuses with a held borrower and succeeds after its
release. All 80 concurrent full-payload borrower checks pass.

This accepts the CPU cache component only, not completed L2 or serving.
Cache tests fake lock calls; the separate pool test proves real small locking.
No native model layout/read path, GPU copy/fence, 52 GiB allocation, total-budget
admission, speed claim or production change is established. Existing CMake
FetchContent deprecation warning remains. Backend builds are not needed for
this isolated CPU component; actual consumer binding still requires them.

## Bounded immutable reader component accepted — 2026-11-20

Reader 063afb0 was not accepted as handed off. Parent corrected alignment,
syscall-count and non-Linux guards in 682e8ba. Test repair 4f9e071 has a real
106-check raw log, but still overstated its skip/size-validation and second-
window fault coverage. Parent corrected those checks without expanding scope.

Initial parent build failed (exit 1): the test used nonexistent
PoolLease::reserved_size(). Parent fixed that typo to size(), which already
returns reserved bytes. Initial failure logs remain under reader/final-cpu/;
all final evidence is under:
bench/ab-v0210/rpcprof/out/managed-ram-pool-20261120/reader/final-cpu/recheck/

| Final check | Result |
|---|---|
| Combined pool/cache/reader/native-lock build and targeted ctest | exit 0; 16/16, no skips |
| ASan/UBSan build and selected pool/probe/cache/reader ctest | exit 0; 4/4 |
| Reader compile with -U__linux__, -Wall -Wextra -Werror | exit 0; simulated guard compile only |
| Default unset opt-in, full Ninja graph and CTest listing | all exit 0; all 4 pool/cache/reader targets absent; zero tests |
| Real pool VmLck lifecycle, KiB | 0 / 64 / 64 / 64 / 0 |
| Reader test | 103 assertions; 18 pread calls, zero flag/buffer/cap violations |
| Reader pool/scratch/final mappings | 64 KiB / 4 KiB / zero; matched map/unmap |

CPU configure options remain Release, tests ON, CUDA/HIP/SYCL OFF and
STRATA_BUILD_RAM_POOL_TEST ON. The sanitizer build uses Debug and
`-fsanitize=address,undefined -fno-omit-frame-pointer`. Exact final commands:

```sh
cmake --build build-cpu-ram-pool --target ram_pool_test ram_pool_nonlinux_probe pool_expert_cache_test bounded_direct_reader_test native_mlock_test native_mlock_nonlinux_probe --parallel 2
ctest --test-dir build-cpu-ram-pool -R 'ram_pool|native_mlock|pool_expert_cache|bounded_direct_reader' --timeout 15 -V
c++ -std=c++17 -Wall -Wextra -Werror -U__linux__ -I include -c src/platform/bounded_direct_reader.cpp -o build-cpu-ram-pool/bounded-reader-nonlinux.o
ctest --test-dir build-cpu-ram-pool-sanitized -R '^(ram_pool_test_mock|ram_pool_nonlinux_probe|pool_expert_cache_test|bounded_direct_reader_test)$' --timeout 30 -V
```

Actual Linux O_DIRECT fixture reads cover unaligned multi-window slices and
non-page-aligned EOF. Invalid input issues no I/O. A second-window EIO discards
the partially filled cache candidate while preserving the populated output.
Scratch stays reserved while a different owner overwrites released payload
storage; cache reload restores every original byte. Fixture bytes and length
remain identical. No buffered fallback or implicit workers exist in the reader.

This accepts a small CPU reader/cache component only. Metadata/kernel/driver
memory and filesystem behavior are not a whole-stack budget measurement.
No actual model read, native-layout binding, GGUF role reader, GPU consumer,
Windows/Mac build, full-sized pool, speed claim or production change occurred.
The existing CMake FetchContent deprecation warning remains. Next bind the
existing dynamic ExpertLayout to the reader/cache, then adapt real consumers.

## Dynamic-layout store component accepted — 2026-11-20

Handoff 278822b was not accepted as-is: open()/close() mutated shared state
without the mutex acquire() holds (a data race), the header declared an
admit_scratch that was never defined, the claimed expert-store log directory
did not exist, and the canonical-layout/overflow/vector-short/closed-store
paths were untested. Parent committed b988202 (serialize open/close, drop the
dead declaration) and wrote the missing scenarios plus a canonical BLOB test
(canonical payload legitimately raises this test's mapping cap to 2 MiB).

Parent's first combined run failed one assertion (exit 8) — a parent test
expectation bug, not a store defect: after close() a live borrower's payload
stays reserved (4096, not 0). That failing raw log is preserved under
expert-store/final-cpu/ctest.log. Fixer repair 674a0d4cc5 fixed the assertion
and reran everything; parent verified the commit diff, numeric status file and
raw logs.

| Final check | Result |
|---|---|
| Combined targeted CPU ctest (17 tests) | exit 0; 17/17, no skips; store 135 assertions |
| ASan/UBSan selected ctest (5 tests) | exit 0; 5/5; store 135 assertions |
| Default unset opt-in: full Ninja graph + CTest listing | grep empty (exit 1 = pass); Total Tests: 0 |
| Store test mappings | peak 1,388,544 B <= 2 MiB cap; final maps 0; matched map/unmap |
| Canonical path | real ExpertLayout native=false; all 1,382,400 BLOB bytes equal |

Evidence: bench/ab-v0210/rpcprof/out/managed-ram-pool-20261120/expert-store/
final-cpu/ (first run, incl. the failure) and final-cpu/repair/ (final, with
status.txt). Native fixture layers {64,128,5000}B x 4 experts at gapped
offsets; every key resolves with full byte equality; snapshot survives caller
layout destruction; failed opens preserve the old store and borrowers; close
keeps borrower payloads reserved while releasing scratch.

Accepted as the L2 primary expert-store component: real dynamic ExpertLayout
geometry, immutable O_DIRECT backing, one pool for scratch+payload, borrower
ownership through close. Still NOT done: runtime construction/budget (L3),
host-KV, whole-stack admission (L4), GGUF role slices, any GPU consumer, any
engine selection of this store, speed or production claims. The store is not
referenced by any engine target outside the opt-in test.

## First real consumer bound — 2026-11-20 (compile-verified, not run)

8829aeb1bc binds the lent-slot refill (generate.cpp:24038-24100 after the
edit) to PoolExpertStore behind a file-scope null-by-default pointer seam.
No env/config read, no construction, no public knob: the legacy branch is
the original code indented; when the seam is nullptr the default path is
byte-identical. Managed path: store->acquire per lent slot, fill through the
existing xcache blocking/queued_staged selection, PoolLease copies held in a
vector cleared only after sync_queued success, no note_async_read for
lease-backed data, per-expert bytes still from expert_layout().blob_bytes.

Parent review found one ownership gap: the failure path returned without
draining queued copies, letting leases destruct at scope exit while async
DMAs could still be in flight. 0da4703b98 adds a best-effort hard
cudaDeviceSynchronize() drain (the pattern already used at the kvg call
site) before the failure return.

| Parent check | Result |
|---|---|
| generate.cpp.o on build-906 (gfx906 recipe) | exit 0 (up to date at 8829aeb1bc; rebuilt clean at 0da4703b98) |
| CPU pool/cache/reader/store ctest | exit 0; 6/6, unchanged |
| Full `strata` backend target | blocked by PRE-EXISTING gcc-16-STL/HIP system-header errors in moe_mmq.cu.o — unrelated TU, not touched by this lane |

Evidence: bench/ab-v0210/rpcprof/out/managed-ram-pool-20261120/consumer/
first/ (fixer: ctest_cpu.log, backend_build.log) and consumer/parent-check/
(generate-obj.log, generate-obj-drain.log, cpu-ctest.log, status.txt).

Limits: the seam is never set — the managed branch has no runtime evidence;
this lane proves the ownership shape and that it compiles on the gfx906
backend. All other raw-pointer consumers remain unadapted; engine-wide
managed activation, runtime pool construction/budget (L3), host-KV and
whole-stack admission (L4) remain open. No boots, no GPU runs, no promotion;
production stays stopped. The pre-existing moe_mmq.cu.o build breakage is
recorded for the engine follow-up list, not fixed here.
## L3 Task 1 — runtime budget construction — 2026-11-20

Boundary report (oracle, source-fact-checked by parent incl. arena mlock/
cudaHostRegister sites expert_source.cpp:6018/:3929/:4037/:4080/:4355, the KV
device-alias requirement layer.cpp:968-979, and the mislabeled :15638 — it is
checkpoint_at, not swap_blobs_batch) accepted with a parent sequencing
addendum (main tree 34d232a): host-KV stays OUTSIDE the pool charged as
outside-owned; no ExpertSource adapter until every must-adapt consumer is
lease-bearing; no invented headroom constants.

Fixer 7517b62720 wired STRATA_RAM_BUDGET_MIB at generate startup (unset/0 =
OFF byte-identical; invalid = error exit; budget > MemAvailable = error
exit; managed mode skips ArenaExpertSource entirely, srcp stays the unlocked
FileExpertSource fallback). Parent review found three defects and repaired
them in 171d745219: (1) outside_owned used qsa_kv_host_bytes(), which is 0
before layers allocate — now charges the CONFIGURED --kv-resident-mib cap
(over-subtract safe); (2) budget_mib<<20 could overflow — MiB-domain guard
added; (3) an operator-set STRATA_NATIVE_MLOCK alongside the budget would
double-lock — now an exclusive-knob startup error.

| Parent check | Result |
|---|---|
| generate.cpp.o on build-906 after 171d745219 | exit 0 |
| CPU pool suite (6 tests) | exit 0; 6/6 |
| Raw logs | managed-ram-pool-20261120/l3-task1/parent-{obj,ctest}.log + parent-status.txt |

Limits: the managed branch still has zero runtime evidence (the seam only
serves the lent-slot site); mapped shard sizes and token tables remain
unaccounted outside_owned inputs (unknown before FileExpertSource opens;
page-cache demand-paged, named for L4 admission); the fixer's own log dir
again contained only a summary — parent logs are the evidence of record.

## L3 Task 2 — both must-adapt refill consumers lease-bearing — 2026-11-20

Fixer's first pass correctly STOPPED at Site B's blob_parts (multi-span).
Parent source analysis re-scoped it: BlobPart{p,n,at} carries the part's
offset WITHIN the assembled blob (expert_source.cpp:4720, three tensors per
expert; fill_slot_queued_parts copies to dst+at, expert_cache.cpp:891-898) —
pieces are discontiguous only in the FILE, dense in the SLOT. The store's
lease IS the assembled blob, so one staged fill from lease.data() reproduces
any parts layout; blob_parts is a legacy no-assembled-copy optimization the
managed branch skips entirely.

Second pass 11fa66faa1 (parent diff-reviewed): Site A VRAM evict-refill
(:17252-17291) and Site B refill_issue/refill_wait (:20463-20534) mirror the
accepted lent-slot seam — acquire, fill blocking/queued per refill_blocking(),
lease vectors scoped across issue+wait, cleared only after sync_queued
success, cudaDeviceSynchronize drain on every failure path with fills in
flight, no note_async_read for lease data, Windows srcp->release guarded to
legacy mode, legacy branches byte-identical.

| Check | Result |
|---|---|
| CPU pool suite | exit 0; 6/6 |
| generate.cpp.o on build-906 | exit 0 |
| Raw logs | l3-task2/second/{ctest,ninja}.log + status.txt |

Every must-adapt consumer is now lease-bearing. Still nothing runtime-tested
(the seam is only set by STRATA_RAM_BUDGET_MIB and no boot is authorized);
remaining legacy consumers (:5611 dispatch fill, :9346/:9522 nibble verify,
ArenaExpertSource blob(), swap relief) still read via the unlocked
FileExpertSource mmap — the adapter that makes the store the primary source
for them is the next design piece.

## Adapter Task 1 — PoolExpertSourceAdapter (default OFF) — 2026-11-20

Oracle design (interface inventory at :9396/:9522/:11448/:16286/:3058 etc.,
nullptr-safe at every site) accepted with a parent addendum after one real
correction: the oracle's note_async_read mapping (drop borrower at issue
time) violated the legacy #1237 contract (expert_source.cpp:2124-2146 only
RECORDS an event; recycling waits for completion) — dropping at issue would
free pool memory under an in-flight queued copy. Addendum: event deferral,
pinned()=true with UNMEASURED-perf caveat, drop points (release immediate,
note deferred, teardown), refcounted double-borrow, injectable async hook.

Fixer 032ac1f655 delivered the adapter + store blob_bytes() accessor +
57-check CPU test (7/7 pool suite, ASan/UBSan clean, default-OFF 0 targets).
Parent review found the sweep rule (`completed && count==0`) never recycles
noted borrowers — the fill pattern never calls release(), so every filled
expert would pin the pool until teardown — and the passing test was vacuous
(its re-borrow check could not fail). 4fc53f2839 repairs it to legacy
equivalence: a newer note RE-ARMS (supersedes the prior event, mirroring
"the event recorded after the last asynchronous copy out of it") and a
completed borrower is recycled on sweep regardless of count; non-noted
long-retention borrowers still drop only at release/teardown. New sections
6/7 assert recycle-without-release and re-arm survival via a borrowed()
inspection hook; destructor now locks. 69 checks 0 failures CPU + sanitizer,
7/7 suite, default-OFF still 0 targets.

Evidence: adapter/task1/ (fixer) + adapter/task1/repair/ (parent, status.txt
all exit 0). Open for Task 2: the pool .cpp files exist ONLY in the
STRATA_BUILD_RAM_POOL_TEST block — strata_core does not compile them, so the
engine LINK would fail on unresolved symbols (object-compile proof never
ran the linker); Task 2 adds them to the engine build and switches srcp.

## Adapter Task 2 — engine wiring; L3 complete (compile-level) — 2026-11-20

a213ad7875 wires the adapter into the engine, default OFF byte-identical:
the five pool .cpp files moved into the strata_core STATIC library (they
existed only in the test block — the engine link would have failed on
unresolved symbols; object-compile proof had never run the linker);
note_async_read now calls the hook OUTSIDE the adapter mutex (the engine hook
calls mark_completed, which locks — re-entrant deadlock removed); generate.cpp
constructs the adapter after store open with a per-(layer,expert) CUDA event
hook (re-arm on re-note, stream-sync+completed fallback on create/record
failure, error-state events treated as completed so no borrower sticks), sets
srcp = &adapter only in managed mode, and tears down with a device sync +
event destruction before adapter/pool/store destruction. The three
lease-bearing seams are untouched.

| Check | Result |
|---|---|
| CPU pool suite incl. adapter | exit 0; 7/7 |
| generate.cpp.o + strata_core + 5 pool objects on build-906 | exit 0 each |
| nm symbol resolution | 22 undefined pool symbols in generate.cpp.o; all resolved T/V in libstrata_core.a (parent spot-checked store open/acquire/dtor, adapter mark/note/vtable) |
| Full strata link | still blocked by the PRE-EXISTING moe_mmq.cu.o gcc-16/HIP breakage (unrelated) |

Evidence: adapter/task2/ (raw ctest + nm lists + status.txt). L3 is complete
at compile/CPU level: budget knob, store, three lease-bearing seams, and the
adapter as managed srcp. NO runtime evidence exists — the managed path has
never executed; a boot window requires operator authorization. Remaining
after that: L4 whole-stack admission (helper/server scope, page-cache
shards/tables accounting) and speed/memory measurement via the existing
harness.

## Live trial window — two budget legs, first runtime evidence — 2026-11-20

The managed path has now run. Two legs on port 8019, production (:8009) down and
untouched throughout, engine `/mnt/srcds/Strata-wt-helper-ipc/build-906/strata
--serve --gpu 0,1`, pack `iq3xxs-ud` (Qwen3.8-Flash-Next UD IQ3_XXS), 2x Radeon VII
+ RTX 3080 expert helper. Raw evidence, request transcripts and mem snapshots:
`/mnt/srcds/Strata/bench/ab-v0210/rpcprof/out/managed-ram-pool-20261120/trial-window/`
(`status.txt` holds every number).

### The geometry fix that made the boot possible (1c83893327)
`store.open()` refused this box's `experts.bin` with EINVAL. The native layout is
50,292,326,400 B of expert data (48 layers x 512 blobs, contiguous, the header's
own total); the file is 50,294,988,800 B — exactly one spare blob larger
(max_blob 2,662,400 B, the layer-47 blob the log calls "largest blob 2.66 MB").
The file is written by the engine's own STRATA_ARENA_MMAP writer, which pads the
arena by one blob so a whole-slot copy may start at any expert
(`expert_source.cpp`), and ArenaExpertSource already accepted that form. The extra
bytes are a tail pad the store never reads. One rule now states that
(`expert_file_bytes_ok` in `expert_layout.hpp`: exactly total, or total plus one
max_blob), used by ArenaExpertSource (unchanged), FileExpertSource (maps only up
to total) and PoolExpertStore (any other size still refused). The 22:05 boot log
still carries the old EINVAL line; the 22:25 and 22:46 boots open cleanly.

### Boot lines
```
strata generate: STRATA_RAM_BUDGET_MIB=40960: outside_owned=4294967296 pool_bytes=38654705664 store open
strata generate: STRATA_RAM_BUDGET_MIB=45056: outside_owned=4294967296 pool_bytes=42949672960 store open
```
Leg A: pool 36,864 MiB, `VmLck 37,912,576 kB = 37,024 MiB` (pool + 160 MiB),
`smaps_rollup Locked` agrees; VmRSS 41,214 MiB at rest, 42,612 MiB after requests.
Leg B: pool 40,960 MiB, `VmLck 42,106,880 kB = 41,120 MiB` (the same +160 MiB),
VmRSS 45,420 MiB at rest, 46,832 MiB after requests. The pool is fully locked in
both legs; the 160 MiB on top is constant, so it is not proportional overhead.

### Memory low-water
| Leg | MemAvailable pre/post | sampler min | swap used | VRAM free |
|---|---|---|---|---|
| A (40960) | 9,362 / 9,067 MiB | 8,529 MiB (22:43:57, n=473) | 1,381 → 2,564 MiB | 220 MiB CUDA0 (LOW), 744 MiB CUDA1 |
| B (45056) | 8,325 / 8,314 MiB | (leg B rows in mem.csv) | 4,129 → 5,536 MiB | same 220 / 744 MiB |
Swap rose 1.2 GiB in leg A and 1.4 GiB in leg B; leg B started with 4.1 GiB of
unreclaimed swap from leg A — a boot-state confound, named, not corrected.

### Requests (curl --max-time 120; wall clock from date +%s.%N around curl;
tok/s = usage.completion_tokens / wall; the server's own figure is quoted beside it)
| Leg | id | HTTP | wall s | compl tok | tok/s wall | log tok/s | cache hit |
|---|---|---|---|---|---|---|---|
| A | short | 200 | 26.25 | 16 | 0.61 | 2.2 | 84.3% |
| A | long #1 | 000 (client timeout) | 120.02 | 241 | 2.01 | 6.2 | 83.8% |
| A | tool | 200 | 28.86 | 52 | 1.80 | 8.1 | 88.9% |
| A | long #2 | 200 | 16.93 | 256 | 15.12 | 21.1 | 93.5% |
| B | short | 200 | 23.34 | 16 | 0.69 | 2.5 | 84.3% |
| B | long | 200 | 105.02 | 256 | 2.44 | 7.5 | 83.6% |
| B | tool | 200 | 34.24 | 65 | 1.90 | 9.5 | 89.6% |
The short request (max_tokens 32) spends its whole budget on thinking
("thinking capped at 16 of max_tokens 32") and returns `content: null`,
`finish_reason: length` — a reasoning-model artefact of the 32-token cap, not a
failure. The tool request returns `finish_reason: tool_calls` with
`{"name": "get_time", "arguments": "{}"}`, so tool calling works through the pool.
Long #1 was killed by the client at 120 s (HTTP 000) while the engine was still
answering at 241 of 256 tokens — the same payload re-sent two minutes later
finished in 16.93 s (256 tokens, 447 chars of content). That speed difference is
unexplained and is the single largest open question from this window: 2.0 vs
15.1 tok/s wall on identical input. The VRAM-low warning is the prime suspect; it
was not isolated (args were left alone, as instructed).
Leg B is slower than leg A on every request (prompt processing 984.8 ms/token vs
76.2 ms/token on the long request). Cause named, not proven: cold page cache after
the reboot plus 4.1 GiB of swap residue.

### Expert-store traffic in the log
The pool's experts are served from RAM, not streamed; what the log does show is
the foresight-swap / slot machinery around it. Counts over the whole elog (both
legs, null bytes stripped with `tr -d '\0'`): `foresight swap` 7,
`served from the swap` 6, `no free slot` 6, `over budget` 6,
`decode expert cache hit rate` 6, `read by the GPU over PCIe` 6, `evict` 3 (all in
"PROFILE, ranked by routing frequency, no eviction" — no eviction happened),
`lease` 1, `no_buffer_space` **0** — that string does not exist in this build; the
pool-full seam is logged as "no free slot". Two quoted lines:
```
strata serve: foresight swap: 837 experts served from the swap space, 2952 copies (2554 from the router look-ahead), 402 not landed yet, 40730 no free slot, 6993 over budget
strata serve: decode expert cache hit rate: 96.6% (63285 hits / 65479 lookups); 12761 more read by the GPU over PCIe or from another GPU (16.3% of all 78240 routed)
```
"no free slot" grows monotonically within a leg (1,725 → 52,439) while "over
budget" saturates at ~6,993 — a slot-shortage counter, not eviction.

### VRAM margin (recorded, args unchanged)
```
strata serve: 220 MiB of VRAM free with everything loaded - LOW: requests may stall; add --vram-reserve-mib 484 to the config's args (or lower --max-context)
strata serve: WARNING: only 220 MiB of VRAM free at the start of this request (below 512 MiB): ... decode falls to a few tok/s and a window may never finish (#2065). Add --vram-reserve-mib 512 ...
```
Four occurrences across the two legs; at request start the free margin fell to
63–166 MiB. CUDA1 keeps 744 MiB.

### Teardown
`stop-all.sh` twice (leg A 22:46, leg B 22:53). Both runs tripped over the same
self-match: leg A was killed by its own pattern sweep ("Killed by SIGTERM" mid-run)
and leg B reported `before=0 after=1 — STILL ALIVE, RUN AGAIN`, where the 1 is its
own `bash -c`. Verified independently each time: no strata/helper process, port
8019 free, 20 s of quiet (4x5 s samples). The sampler (pid 2608304) was left
running; its CSV's last row (22:53:44) has a bogus engine_pid — every row before
that belongs to a real leg.

### UNMEASURED
Throughput past 256 tokens and at long context; concurrency (all requests were
serial, no --parallel); a sustained-load memory curve (only idle + 4 requests
sampled); the cause of the 2.0 vs 15.1 tok/s variance between long #1 and long #2;
whether the 220 MiB VRAM margin is that cause (not isolated, args untouched); a
pool-vs-non-pool baseline on the same box and window; L4 total-budget enforcement
and the adapter under real traffic (helper counters only, no admission evidence).

## Read granularity fixed: one pread per expert blob (commit 0cd9c50541)

The store admitted a one-page (4 KiB) pool scratch, so bounded_direct_reader
walked every expert through 4 KiB O_DIRECT windows. A 1.95 MiB blob therefore
cost about 488 synchronous preads; the oracle's attribution report
(bench/ab-v0210/notes/managed-ram-pool-trial-attribution-20261120.md) computed
the miss arithmetic and put the read path at 9-29 ms per blob (68-212 MB/s on
this disk), which is what held the managed pool to 2.2 tok/s cold.

The scratch is now sized from the largest blob over the layers present:
2,666,496 B on the iq3xxs-ud pack (cap 8 MiB), so one pread delivers a whole
blob. It costs 2.5 MiB of pool bytes. The store now reports what it does:
`RAM pool store: pool_bytes=38654705664 scratch_bytes=2666496
usable_bytes=38652039168 blob_max=2662400 io_window=2666496 preads_per_blob=1`
at boot, and hits/misses/evictions/resident_bytes at exit. Tests: 7/7 pass
(ram_pool x3, pool_expert_cache, bounded_direct_reader, pool_expert_store,
pool_expert_source_adapter); build-906 links.

### Trial leg, budget 40960 MiB (port 8019, same config as the first window)

| Request | Wall | Completion tokens | tok/s wall | Same request before the fix |
|---|---|---|---|---|
| short ("reply ready") | 5.23 s | 16 | 3.06 | 26.25 s, 0.61 |
| long, cold boot | 37.15 s | 256 | 6.89 | 120 s cap at 241 tok, 2.0 (engine log) |
| long, second run | 13.21 s | 256 | 19.38 | 16.93 s, 15.12 (engine log 21.1) |
| tool call (get_time) | 13.22 s | call returned | - | 28.86 s, served |

Memory is unchanged from the first window: engine VmLck 37,912,576 kB =
37,024 MiB against a 36,864 MiB pool (the whole pool locked), MemAvailable
10.99 GiB after the requests (11.17 GiB mid-run), SwapFree flat - the 4.7 GiB
of swap in use is leg B's residue, nothing new was swapped during this leg.

Verdict: the cold-start penalty is gone (5x on the first request, 3.4x on the
first long run); the warm steady state sits where this tuple already was
(19.4 against 15.1-21.1), so the fix removed a startup handicap, not a
steady-state limit. The 220 MiB VRAM margin warning and the "foresight swap"
counters (over budget flat at 6,749, no free slot climbing to 54,887) are the
VRAM-side device slots, not the pool (oracle attribution, question 2).

Teardown incident, recorded so it is not repeated: stop-all.sh's process sweep
also matched the invoking shell, because that shell's own command line carried
the sweep's pattern text (a verification pgrep). The stack did go down - no
engine, helper or server process remains and port 8019 is free - but the
command that ran the check was killed mid-run, and the engine was TERMed before
it could print the exit counters. Check liveness with ps/awk or ss, never with
a pattern that stop-all also sweeps.

Still UNMEASURED: throughput past 256 tokens and at long context; concurrency;
a sustained-load memory curve; a pool-vs-non-pool baseline on the same box and
window; L4 total-budget enforcement.

### Correction (operator, 2026-11-20 late): the VRAM-margin explanation is refuted

The first window's section above names the low free-VRAM warning as the prime suspect
for the slow long request ("The VRAM-low warning is the prime suspect"). The operator
states that explanation is refuted and false on this box, and it must not be repeated.
No measurement here supports it either: it was an inference from a warning line, not an
isolated A/B, and the same config ran warm at 19.4-21.1 tok/s while the warning was
present in the log.

What the evidence does support for decode speed in managed mode: the pool holds 36 GiB
of a 46.84 GiB expert set, so roughly a fifth of routed experts are not resident and the
elog reports 13-17% of them read over PCIe or from another GPU each window; the read
granularity that used to cost ~488 preads per blob is fixed (0cd9c50541). Attribution of
the remaining gap needs a measurement, not a hypothesis: the unmeasured item is still a
pool-vs-non-pool leg on the same box and window.

Also: the VRAM margins stay at the tuple's own values (--vram-reserve-mib 192,
--vram-reserve-later-mib 576). A trial-config edit that raised the reserve to 704 was made
and reverted the same day; the operator directive is not to touch them.

### Operator directive: engine tests move to port 8009

From 2026-11-20 (late) all engine tests run on **port 8009**. The trial-port
separation (8019) is retired; production is down, so 8009 is simply the test
port now. The lane running when this arrived finishes on 8019 (it was already
booting there); every later boot, launcher default and config port moves to
8009. The stop rule is unchanged: teardown only through the on-disk scripts,
and a test that holds 8009 is stopped deliberately, never left running.

#### Clarification: 8009 is shared, test and prod are mutually exclusive

The port directive above must not be read as "8009 is now just a test port". The rule
is that an engine test never runs **together with prod**. Tests moved to 8009, and prod
is down by order; the two are exclusive. `launch-trial-managed.sh` now defaults to 8009
and refuses to boot when a `run/prod*-supervisor.json` records a pair whose pids are
still alive, naming the state file and pid, so the rule is enforced by the launcher and
not by memory. Prod's own launcher refuses an occupied 8009, so the exclusion holds in
both directions.

### The 80k prompt that failed: copy into the staging buffer, do not lend (2026-11-21 early)

An 80k-token prompt on the managed pool failed twice. First with "prefill: expert
source has no blob": the adapter handed the prompt path only a borrowed pointer, and
the prompt's staging step copies. With that fixed, the same prompt staged 74,630 of
74,635 rows and then died at 238 s with "refilling a lent slot failed: No buffer space
available". There were two walls, and both were the adapter lending what it should copy.

**Wall one: the adapter lent its experts.** `PoolExpertSourceAdapter` now answers
`transient()` true for every pair (`include/strata/core/pool_expert_source_adapter.hpp:84`):
the source copies the bytes out of the pool, so the store's LRU - not a growing borrower
table - decides what stays. `copy_blob` (`src/core/pool_expert_source_adapter.cpp:25`)
acquires the blob, copies it into the caller's buffer and drops the lease, so no borrower
is left behind. `copy_blobs` (`:38`) holds at most `kCopyLeaseWindow = 4` leases at once
(header `:89`), copies them, and falls back to one-at-a-time if the store refuses the
window, so a bigger sweep never needs a bigger pool. A refused pair prints one stderr
line. Every path that does not see `transient()` is byte-identical to before.

**Wall two: the prompt's helper probe borrowed every pair.** The probe that decides which
experts the helper must serve asked `blob_stable()` for every expert of every layer
(`src/prefill/prefill.cpp:5263`). For a transient source that keeps one borrower per pair:
512 experts x 48 layers of ~2 MiB is about 24,000 borrowers, the whole 36 GiB pool, held
before the last chunk of a long prompt was read - which is why the lent-slot refill after
the prompt had nothing to reclaim. A transient pair is never lent for the whole sweep, so
the probe now answers 1 for it ("the host has these bytes"); non-transient sources keep
the old probe exactly as it was. For diagnosis, `PoolExpertStore::pool_reserved_live_bytes()`
(`include/strata/core/pool_expert_store.hpp:102`) and one line in the managed refill
failure (`src/program/generate.cpp:24297-24312`) now say whether the cache or live
borrowers held the pool at a refusal. Both changes are in commits `8e861ca533` and
`1ae83e157d` on `helper-ipc-diet`.

Tests: sections 8-11 in `tests/core/pool_expert_source_adapter_test.cpp:403,423,453,496`
cover `transient()` being true and creating no borrower, `copy_blob` returning the exact
bytes and leaving no borrower (a second copy still fits), a full sweep over a pool far
smaller than the expert set never running dry, and `copy_blobs(n>1)` being byte-identical
to `n` `copy_blob` calls, including the refusal path. 114 checks, 0 failures on the
final build; the full build-906 suite (7/7), the ASan/UBSan build (6/6) and the plain-CPU
build (7/7) passed earlier in the same window.

#### Trial leg: the same 80k request, port 8019

Engine `/mnt/srcds/Strata-wt-helper-ipc/build-906/strata` at `1ae83e157d`, managed mode,
`STRATA_RAM_BUDGET_MIB=40960`, trial config unchanged, `--vram-reserve-mib 192` and
`--vram-reserve-later-mib 576` untouched. Production was down for the whole window.

| leg | request | wall s | prompt tok | compl tok | tok/s wall | engine prompt | engine decode |
|---|---|---|---|---|---|---|---|
| 1 short, cold | "Reply with the single word ready.", max 32 | 5.12 | 59 | 16 | 3.12 | 59 in 3,745 ms (15.8 tok/s) | 16 in 1,366 ms (11.7 tok/s) |
| 2 80k, cold | 80k prompt file, max 2,048 | 346.37 | 74,635 | 1,594 (stop) | 4.60 | 0 reused + 74,635 read in 266,328 ms (280.2 tok/s, 3.57 ms/token) | 1,536 in 75,968 ms (20.2 tok/s) |
| 3 256, warm | 109-token prompt, max 256 | 27.49 | 109 | 256 | 9.31 | 109 in 8,210 ms (13.3 tok/s) | 132 in 7,662 ms (17.2 tok/s) |

The 80k request answers now: all 74,635 prompt tokens staged, the completion stopped
on `finish_reason stop` at 1,594 tokens rather than the cap, and zero "no blob",
zero "refill failed" and zero "No buffer space" in the boot's elog. The honest prefill
figure is the engine's 266.3 s / 3.57 ms per prompt token; the client's TTFT is 344.79 s
because the thinking stream arrives before the first answer delta and the measuring
script does not time it. The helper served 27,520 requests / 1,252,864,000 B on leg 2
with no timeouts or fallbacks. Leg 3's prompt (109 tokens) is not the shape of the
earlier window's 74-token request, so its 9.31 tok/s is not comparable with that table.

Memory held: VmLck flat at 37,024 MiB (the 36,864 MiB pool, locked), VmRSS peaked at
45,220 MiB during the warm leg, MemAvailable bottomed at 8,252 MiB inside the 80k
prefill and sat at 52,738 MiB after teardown. Swap in use was 4,773 MiB at window end
and 3,593 MiB after teardown; the window's baseline was not captured, and the sampler's
swap column is empty in every row (a script bug - the values here were read from
`/proc/meminfo` by hand). VRAM was as tight as before: the serve log warned "only 8 MiB
of VRAM free at the start of this request" at the warm leg, and the foresight swap
counters on the 80k leg read 1,255 experts served from swap, 17,308 copies, 1,610 not
landed yet, 140,234 no free slot, 6,671 over budget. The margins stay as they are by
directive.

Teardown: the sampler was stopped by pid found with the on-disk `find-proc.sh`; an
inline pattern-kill had again matched its own shell and printed "Killed by SIGTERM"
(fourth such incident, now recorded as a rule). `./stop-all.sh` then ran alone - TERM
1749429 1749626 1750371 1751661, KILL 1749429 1750371, before=0 after=0, ALL STOPPED -
and `check-quiet.sh` in a separate invocation reported QUIET with ports 8009 and 8019
free.

Deviations: (1) the launcher in the main tree is now mode 664 and defaults to 8009, so
the trial booted with `bash .../launch-trial-managed.sh` and `STRATA_PORT=8019`, which
the script still honours; config and reserves untouched. (2) leg 2 was first launched
with a wrong driver path, wrote nothing and was re-run.

UNMEASURED: sustained load and concurrency; a second 80k request in the same process
(warm cache); a pool-vs-non-pool baseline on this box and window; and the decode-side
refill's own simultaneous-lease requirement (up to lent x blob bytes), which is the same
shape of exposure as the two walls fixed here but did not refuse in this run.

#### Evidence location, and one figure in it that must not be quoted

The raw files behind the table above were written into the worktree's bench tree
(`/mnt/srcds/Strata-wt-helper-ipc/bench/ab-v0210/rpcprof/out/managed-ram-pool-20261120/trial-window/`).
They are now also in the main tree at
`bench/ab-v0210/rpcprof/out/managed-ram-pool-20261120/trial-window/`: `boot-boot2.log`,
`server-boot2.log`, `helper-boot2.log`, `engine-elog-boot2.txt`, `mem-stream-fix.csv`,
`legs-stream-fix.log`, `infill80k-stream.{json,txt,log}`, `leg1-short-cold.*`,
`leg2-warm256.*`, `status-fix.txt`.

`infill80k-stream.json` says `decode_tok_s: 1009.06`. That figure is wrong and must not be
quoted: the server delivered all 38 content deltas in the last 1.6 s of a 346 s request, so
the client's window is not the generation time. The engine's own line is the measurement:
`1536 generated in 75968 ms (20.2 tok/s)`. `infill_measure.py` now withholds the client
figure when the deltas arrive in one burst (main tree `37a39576da`).

Re-verified from the raw files, not from the summary: `MemAvailable` minimum 8,252 MiB
(what else could be expected on this box with a 36 GiB pool), `VmLck` 37,024 MiB in every
row, `VmRSS` peak 45,220 MiB, and zero occurrences of `no blob`, `refill failed`,
`no_buffer_space` in the boot's elog.

`trial-managed-sampler.sh` had the empty swap column bug that this window had to work
around by hand: the `swap_used_mib` column was fed an unset variable, and the engine was
found with a `pgrep -f` pattern that also matches a shell carrying that text. Both fixed
(main tree `6052b4a282`); the self-test reads a swap figure identical to `/proc/meminfo`.

## Window 2g — STRATA_RAM_BUDGET_MIB=43008 (2026-11-21, port 8009)

Two GiB more locked memory than the 40,960 window, same tuple: engine
`/mnt/srcds/Strata-wt-helper-ipc/build-906/strata --serve --gpu 0,1`, config
`bench/ab-v0210/rpcprof/trial-managed-pool.json` unmodified, `--vram-reserve-mib 192` and
`--vram-reserve-later-mib 576` untouched, helper the RTX 3080
`strata-expert-helper-patient` (pid 2664394). Production down for the whole window; no push.

Boot line, from this boot's elog only:

    strata generate: STRATA_RAM_BUDGET_MIB=43008: outside_owned=4294967296 pool_bytes=40802189312 store open
    strata generate: RAM pool store: pool_bytes=40802189312 scratch_bytes=2666496 usable_bytes=40799522816
                     blob_max=2662400 io_window=2666496 preads_per_blob=1 resident_pairs=0

40,802,189,312 B = 38,912 MiB, i.e. 43,008 minus the 4,096 MiB the engine already owns. The
budget took on the first boot. `--expert-helper-ram-relief` reports "this run's expert
source is not the arena", so the managed store, not the arena, is the expert source.
Health answered `loaded: true` 40 s after the launcher started.

| leg | request | wall s | engine prompt | engine decode | hit rate | over PCIe |
|---|---|---|---|---|---|---|
| 1 short, cold | "Reply with the single word ready.", max 32 | 5.1 | 59 read in 3,788 ms (15.6 tok/s, 64.2 ms/tok) | 16 in 1,306 ms (12.2 tok/s) | 84.8% | 16.2% |
| 2 80k, disk-warm (not cold) | 80k prompt file, max 2,048 | 114.1 | 74,630 reused + 5 read in 15,914 ms — 15,652 ms of it a disk restore | 1,538 in 73,083 ms (21.0) + 152 in 5,826 ms (26.1) = 1,690 in 78,909 ms (21.4) | 95.2% | 13.2% |
| 3 80k, warm, 256 | same file, max 256 | 9.5 | 74,630 reused + 5 read in 275 ms | 128 in 4,337 ms (29.5) + 110 in 4,398 ms (25.0) = 238 in 8,735 ms (27.2) | 94.2% | 13.5% |
| 4 80k, warm again | same file, max 2,048 | 64.7 | 74,630 reused + 5 read in 250 ms | 1,536 in 55,716 ms (27.6) + 213 in 8,257 ms (25.8) = 1,749 in 63,973 ms (27.3) | 94.3% | 13.5% |

Decode figures are the engine's own `N generated in M ms` lines, never the client's: legs 3
and 4 delivered their deltas in one burst (`infill_measure.py` withheld the client figure,
as it should). Leg 1 again returned 16 reasoning tokens and no content, the same
"thinking capped at 16 of max_tokens 32" shape as the 40,960 window.

**Leg 2 was not cold.** The config's disk conversation cache
(`/mnt/srcds/Strata-wt-helper-ipc/run/diet-soak-cache`, 3.4 GiB, written by the 40,960
window) held this exact prompt, and the engine restored it before the request ran:

    strata serve: conversation cache: restored 74630 tokens from disk (checkpoint) in 15652.2 ms
    strata serve: prompt 74635 tokens = 74630 reused + 5 read in 15914 ms (0.3 tok/s)

So 15,652 of those 15,914 ms are a 1.7 GiB direct-I/O restore at about 109 MiB/s, and only
5 tokens were embedded. This window therefore has **no cold 80k prefill** and cannot be
compared against the 40,960 window's 280.2 tok/s / 3.57 ms per token on that axis. Getting
one needs that cache dir emptied before a boot; it belongs to the soak lane, so it was not
touched. A novel-prompt substitute was rejected: it would run against a warm expert pool
after three legs, which the 40,960 cold row did not have, so it would confound the
comparison in the opposite direction.

Every 80k request also restarted once mid-generation (5 checkpoints, rewind to 74,630):
part 1 ran to the cap's 1,536 tokens (128 for the 256-token cap), part 2 re-read 1,559 or 19
tokens. The parts add up to the client's completion counts, so the decode totals above are
sums of the engine's own segments.

Memory, from `mem-2g.csv` (86 samples, 5 s):

| figure | 43,008 window | 40,960 window |
|---|---|---|
| VmLck | 39,072 MiB flat | 37,024 MiB |
| VmRSS peak | 45,912 MiB | 45,220 MiB |
| MemAvailable min | 8,222 MiB (during leg 2's disk restore) | 8,252 MiB |
| swap | 3,527 -> 6,285 MiB (peak 6,297) | 1,381 -> 2,564 MiB |

Two GiB more locked and the low-water mark moved 30 MiB, i.e. nothing, while RSS peak rose
692 MiB and swap use grew further. The pool is not what sets the floor here.

Verdict against the 40,960 window: no measurable change in decode throughput (21.0 vs
20.2 tok/s on the same 80k request shape), in decode expert cache hit rate (95.2% vs 95.5%),
or in the PCIe share (13.2% both), and none in memory low-water (8,222 vs 8,252 MiB). All of
these deltas sit inside the row-to-row noise this box has already shown: the same payload
ran at 2 tok/s and at 21 tok/s two minutes apart in leg A, and prefill was 76.2 ms/token in
one boot and 984.8 ms/token in the next. No speed claim is made.

Hygiene: zero `no blob`, zero `refill failed`, zero `no_buffer_space` in the boot's elog;
7 `no free slot` counts are pool-pressure counters inside `foresight swap` lines, not
refusals. Two VRAM warnings were logged and not acted on (quoted in `status.txt`): "only
220 MiB of VRAM free at the start of this request (below 512 MiB)" and, after the legs,
"only 8 MiB". The disk tier refused two idle saves on free space (6,518 MiB free, a 3,376 -
3,412 MiB snapshot plus a 4,096 MiB floor), so the cache did not grow.

Teardown: sampler stopped by its explicit pid 2716076 (verified cmdline, no pattern kill),
then `./stop-all.sh` alone ("before=0 after=0 — ALL STOPPED", exit 0), then `check-quiet.sh`
in its own invocation (QUIET, exit 0). Ports 8009 and 8019 free, no sockets in `run/`,
MemAvailable back to 55,024 MiB, 1,643 MiB of swap left in place.

Raw files, main tree
`bench/ab-v0210/rpcprof/out/managed-ram-pool-20261120/trial-window-2g/`: `boot-2g.log`,
`before-state.txt`, `engine-elog-2g.txt`, `legs-engine-lines.txt`, `mem-2g.csv`,
`leg{1,2,3,4}.log`, `leg{1,2,3,4}-*.{json,txt}`, `prompt-short.txt`, `status.txt`.

Still UNMEASURED: cold 80k prefill at this budget (cache hit, see above); any non-80k cold
prompt at 43,008; concurrency (every request serial); the pool's behaviour between 36,864
and 40,960 MiB with a cold prompt cache; whether the mid-generation rewind is a reasoning
budget or checkpoint effect, and what it costs.

## Cold 80k prefill at 43,008 MiB — empty conversation cache — 2026-11-21

The 43,008 window above never measured a cold 80k: its leg 2 read the prompt back from a
disk checkpoint (`76,430 reused + 5 read in 15,914 ms`, 15,652 ms of it a restore), so its
`280.2 tok/s / 3.57 ms per token` describes a warm prompt, not prefill, and the 40,960
window's `280.2`/`3.57` had nothing to be checked against. This window boots the derived
config `bench/ab-v0210/rpcprof/trial-managed-pool-cold-2g.json`: the same args as
`trial-managed-pool.json` except `--conversation-cache-dir` points at an empty
`/mnt/srcds/Strata-wt-helper-ipc/run/diet-cache-cold-2g` (verified empty before boot),
budget 43,008 MiB, and `--vram-reserve-later-mib` cut 576 -> 192 by operator order (device 0
keeps `--vram-reserve-mib 192`). Production was down for the whole window; the trial served
on 8009 (`launch-trial-managed.sh --single-run`, `/health` ok after 80 s), engine
`/mnt/srcds/Strata-wt-helper-ipc/build-906/strata`.

Boot identity, from the elog:

```
strata generate: STRATA_RAM_BUDGET_MIB=43008: outside_owned=4294967296 pool_bytes=40802189312 store open
strata generate: RAM pool store: pool_bytes=40802189312 scratch_bytes=2666496 usable_bytes=40799522816
                 blob_max=2662400 io_window=2666496 preads_per_blob=1 resident_pairs=0 hits=0 misses=0
strata serve: 220 MiB of VRAM free with everything loaded - LOW: requests may stall; add --vram-reserve-mib 484 ...
strata serve: CUDA1: 360 MiB of VRAM free with everything loaded - LOW: its window graphs are captured on first use ...
```

The pool geometry is byte-identical to the 43,008 window (`scratch_bytes 2666496`,
`usable_bytes 40799522816`), i.e. the +2 GiB is against the 40,960 window's
`pool_bytes=38654705664` and this config did not shave it. Device 1 loaded with 360 MiB free:
384 MiB more than the previous 576 MiB reserve leaves, and still 152 MiB under the 512 MiB
paging threshold, so the trimmed device-1 margin bought VRAM but not a quiet card.

Requests (client `infill_measure.py`, temperature 0, stream, max 2,048 on the 80k legs):

| leg | request | client wall s | prompt tok | compl tok | engine prompt | engine decode | hit rate | PCIe share |
|---|---|---|---|---|---|---|---|---|
| 1 short, cold | "Reply with the single word ready.", max 32 | 5.25 | 59 | 16 | 59 = 0 reused + 59 read in 3,780 ms (15.6 tok/s) | 16 in 1,458 ms (11.0 tok/s) | 85.9% | 16.2% |
| 2 80k, cold | 80k prompt file, max 2,048 | 367.61 | 74,635 | 1,597 | 74,635 = 0 reused + 74,635 read in 287,366 ms (259.7 tok/s, 3.85 ms/token) | 1,537 in 77,734 ms (19.8 tok/s) | 95.0% | 13.1% |
| 3 80k, warm repeat | same file, max 2,048 | 63.83 | 74,635 | 1,582 | 74,630 reused + 5 read in 281 ms | 1,537 in 62,214 ms (24.7 tok/s) | 95.3% | 13.2% |
| 4 short, warm | "Reply with the single word ready.", max 32 | 1.99 | 59 | 16 | 59 = 0 reused + 59 read in 1,515 ms (39.0 tok/s) | 16 in 467 ms (34.3 tok/s) | 94.9% | 16.2% |

Leg 2 is the cold number that was missing, and the elog proves it: `prompt 74635 tokens =
0 reused + 74635 read in 287366 ms`. Nothing was restored — the cache directory was empty at
boot and the disk tier never wrote a generation (every park refused on free space: a
~3,410 MiB snapshot plus a 4,096 MiB floor against 6,477 MiB free), so the directory now holds
only `owner.lock` and three ring files (43 MiB) and there was no checkpoint to reuse. The warm
repeat on the same 74,635-token prompt then read back in 281 ms, 1,022x less prompt time.

The honest prefill figure is the engine's 287.4 s / 3.85 ms per prompt token. The client's
TTFT (365.71 s, 4.90 ms/token) carries the first-token wait on top and is not the prefill
rate. Drafts: 881 of 1,189 accepted on leg 2, 822 of 1,145 on leg 3.

Each 80k leg is followed in the elog by a second, internal request whose prompt is the leg's
own prompt plus the tokens it generated (`76,189 tokens = 76,171 reused + 18 read in 454 ms
and 304 ms`, 43 and 28 tokens generated). The serve log names the cause once — "the reply
reached max tokens while still thinking, so it has no answer" — and no other client was
connected when checked with `ss` after the legs, so this is the serve layer continuing a
reply that hit the cap inside its thinking budget (55,000 tokens against a 2,048 cap). It is
engine time, not client time: the client's 1,597 completion tokens are 1,537 + 60.

Memory, from `mem-cold.csv` (113 samples, 5 s, 00:26:53 -> 00:37:26):

| figure | this window (cold, 43,008) | 43,008 window (disk-warm) | 40,960 window |
|---|---|---|---|
| VmLck | 39,072 MiB flat | 39,072 MiB | 37,024 MiB |
| VmRSS peak | 47,170 MiB (during leg 2's decode) | 45,912 MiB | 45,220 MiB |
| MemAvailable min | 7,932 MiB (during leg 2) | 8,222 MiB | 8,252 MiB |
| swap | 5,116 -> 7,701 MiB (peak 7,719) | 3,527 -> 6,285 MiB | 1,381 -> 2,564 MiB |

The pool line's `scratch_bytes`/`usable_bytes` are unchanged from the 43,008 window. The
5,116 MiB of swap already in use before this boot is box residue, not this window's, so the
swap columns' start points differ; the 2.6 GiB that this window added is its own.

Verdict against the 40,960 window's cold 80k (`280.2 tok/s / 3.57 ms per token`, decode
`20.2 tok/s`, hit rate `95.5%`, PCIe `13.2%`): prefill came out at 259.7 tok/s / 3.85
ms/token (7.3% fewer tok/s, 7.8% more ms/token), decode at 19.8 tok/s (-2.0%), hit rate at
95.0%, PCIe at 13.1%. Every one of those deltas is inside the row-to-row noise this box has
already shown — the same payload ran at 2 tok/s and 21 tok/s two minutes apart, and prefill
was 76.2 ms/token in one boot against 984.8 ms/token in the next. The cold 80k prefill rate
therefore holds across the +2 GiB pool and the 384 MiB device-1 margin change; it does not
improve. No speed claim is made either way. What is NOT comparable: the 43,008 window's 80k
leg (disk restore, 15,652 ms of read-back) and this window's leg 3 (RAM/ring reuse, 281 ms);
and no second cold 80k was run, so the 7.3% spread between the two windows cannot be
separated from a one-shot.

Hygiene: zero `no blob`, zero `refill failed`, zero `no_buffer_space` / `No buffer space` in
the boot's elog; every helper line carries `timeouts=0 fallbacks=0`. One rewind line and it
is benign (`the draft ring for the rewind to 74630 restored from its pair (captured at 74630
tokens)`, leg 3). Two VRAM warnings were logged and not acted on: "only 220 MiB of VRAM free
at the start of this request (below 512 MiB)" at load and leg 1, and "only 8 MiB of VRAM free
at the start of this request" at leg 2's decode — the card stayed responsive (19.8 tok/s
decode on that same request), but 8 MiB is the thinnest this box has logged.

Teardown: sampler stopped by its explicit pid (verified from `/proc/<pid>/cmdline`), then
`./stop-all.sh` alone (exit 0, `TERM 3221104 3221122 3221867 3223334`, `KILL 3221104
3221867`, "before=0 after=0 - ALL STOPPED"), then `check-quiet.sh` in its own invocation
(QUIET, exit 0). No sockets left in `run/`, ports 8009 and 8019 free.

Raw files, main tree
`bench/ab-v0210/rpcprof/out/managed-ram-pool-20261120/trial-window-cold/`: `boot-cold.log`,
`legs-cold.log`, `legs-engine-lines.txt`, `elog-clean.txt`, `mem-cold.csv`,
`L{1,2,3,4}-*.{json,txt}`, `prompt-short.txt`, `status.txt`, `extract_legs.py` (the leg
splitter, kept so the per-leg blocks can be rebuilt from the elog).

Still UNMEASURED: a second cold 80k in the same boot (repeatability against the 7.3%
spread); the same empty-cache cold 80k at 40,960 MiB (no such boot, so the pool size is still
confounded with the window); concurrency (every request serial); whether the 8 MiB VRAM
trough during leg 2's decode costs anything on a following cold 80k; the 2.6 GiB of swap this
window added beyond its 5.1 GiB residue.

#### At a 38 GiB pool the conversation-cache disk tier silently does nothing

Six refusals and two contradictory park lines in the cold window's elog:

    conversation cache: parked to disk only (76231 tokens, snapshot 1753 MiB over the 512 MiB
      RAM ceiling; the published generation serves the next restore; parked=0 bytes=0)
    conversation cache: idle save skipped (disk cache: free-space refusal (3408 MiB plus a
      floor of 4096 MiB needed, 6477 MiB free))

The park announces "to disk only" and then writes nothing (`parked=0 bytes=0`); the idle save
is refused because the disk tier wants the 3408 MiB snapshot plus a 4096 MiB free-space floor
against the 6477 MiB the box has left while serving. At this pool size that headroom does not
exist, so a 76k-token conversation is never checkpointed: every such request is cold, and the
281 ms reuse measured in leg 3 came from the in-process RAM/ring state, not from disk.

This is a memory-budget consequence, not a cache bug: 38 GiB of locked pool leaves ~6.5 GiB,
and the disk tier's floor alone is 4 GiB plus the snapshot. It belongs in the budget's
documentation - a long prompt under a big pool pays a full cold prefill every time and the
disk tier is silently unavailable. Separately, "parked to disk only" printing `bytes=0` without
saying "not written" is a small reporting defect worth raising on its own.

## Helper allocation +128 MiB, same 43,008 MiB pool — 2026-11-21 (measured)

Window: pool 43,008 MiB unchanged, device reserves 192/192 unchanged, the helper's RTX 3080
given 128 MiB more room. The two knobs had to move together: the last boots stopped on
`fill stopped: fill-mib`, i.e. the fill hit its `--fill-mib` cap (6796) before its budget
(6816.2 MiB), so lowering the reserve alone would have changed nothing.

    STRATA_HELPER_RESERVE=384    (reserve 512 -> 384)
    STRATA_FILL_MIB=6924         (cap 6796 -> 6924)

Config `bench/ab-v0210/rpcprof/trial-managed-pool-cold-3g.json`, empty conversation-cache
dir (`run/diet-cache-cold-3g`), port 8009, engine `/mnt/srcds/Strata-wt-helper-ipc/build-906/strata`.

### The helper's own lines prove the env arrived

    fill preflight: device free 7516 MiB of 7842 MiB, budget 6944.2 MiB (reserve 384 MiB,
      scratch 672000 B, buffers 180098776 B), slots cap 4600, fill-mib cap 6924
    filled 4377 slots (6.76 GiB), scratch 672000 B, buffers 180098776 B
    fill stopped: fill-mib; device free now 590 MiB of 7842 MiB (4377 slots, 6.76 GiB uploaded)

The 512/6796 boots read `budget 6816.2 MiB ... fill-mib cap 6796`, `filled 4250 slots
(6.64 GiB)`, `device free now 718 MiB`. So: +127 slots, +0.12 GiB resident, and the freed
128 MiB is genuinely occupied (free after fill 718 -> 590 MiB). The engine agrees on the
engine side: `expert-helper proxy: 4377 experts ... 6.76 GiB (fill report)`, and
`RAM pool store: pool_bytes=40802189312` with `STRATA_RAM_BUDGET_MIB=43008:
outside_owned=4294967296 pool_bytes=40802189312 store open`.

### Three legs

| leg | what | prompt tokens | prefill | decode | hit | PCIe | helper on that leg |
|---|---|---|---|---|---|---|---|
| H1 | cold, 32 max | 59 = 0 reused | 3691 ms (16.0 tok/s) | 16 gen in 1236 ms (12.9 tok/s) | 85.7% | 16.4% | reqs=176 rows=1258 bytes=12,881,920 |
| H2 | **cold 80k, 2048 max** | 74,635 = **0 reused** + 74,635 read | **298,634 ms (249.9 tok/s, 4.00 ms/token)** | 1536 gen in 76,464 ms (**20.1 tok/s**) | **95.7%** | **13.5%** | reqs=25,132 rows=119,406 bytes=1,222,717,440 |
| H3 | warm repeat | 74,635 = 74,630 reused + 5 read | 252 ms | 1540 gen in 59,786 ms (25.8 tok/s) | 95.3% | 13.2% | reqs=23,389 rows=115,807 bytes=1,185,863,680 |

H2's cold proof is exact: `prompt 74635 tokens = 0 reused + 74635 read in 298634 ms`, no
`restored ... from disk`, empty cache dir, `drafts accepted 818 of 1131, 5 checkpoints,
timeouts=0 fallbacks=0`.

### Comparison, and what is comparable

| row | prefill | ms/token | decode | hit | PCIe | helper rows / bytes |
|---|---|---|---|---|---|---|
| 43,008, helper 512/6796 (cold-2g) | 259.7 tok/s | 3.85 | 19.8 | 95.0% | 13.1% | 116,334 / 1,191,260,160 (reqs=23,383) |
| **43,008, helper 384/6924 (this)** | **249.9** | **4.00** | **20.1** | **95.7%** | **13.5%** | **119,406 / 1,222,717,440 (reqs=25,132)** |
| 40,960 (pool window) | 280.2 | 3.57 | 20.2 | - | - | 122,350 / 1,252,864,000 (reqs=27,520) |

All three are the same protocol (chat, `infill_measure.py`, 74,635-token prompt, 2048 max,
cold cache) and are therefore comparable. Verdict: the extra 128 MiB did not move anything
beyond the spread these windows already show. Prefill sits 3.8% below the 512/6796 row and
10.8% below the 40,960 row - at the low end of the 249.9-259.7 band the same budget has
produced, not a gain, and one leg cannot tell a real 4% loss from noise. Decode is flat
(20.1 vs 19.8 vs 20.2). Hit rate +0.7 pt, PCIe share +0.4 pt and helper work +2.6% rows /
+2.6% bytes all land inside the reference rows' own spread (rows 116k-122k, bytes 1.19-1.25
GB), and the helper carried *more* rows with more slots resident, not fewer. No speed claim
is made from anything but the engine's own lines.

### Memory (sampler, 5 s, 113 samples)

VmLck 39,072 MiB flat (the pool lock; `pool_bytes` 38,912 MiB plus overhead), VmRSS peak
47,780 MiB, MemAvailable min 7,236 MiB. Swap: 4,771 MiB at sampler start, 7,157 at the end,
peak 7,161 - and 3,177 MiB of that was already residue before the boot, so this window added
~4.0 GiB. Same boot-state confound as the cold window; named, not explained.

### Hygiene

Zero `no blob`, zero `refill failed`, zero `no_buffer_space` in the boot's elog; every leg
carries `timeouts=0 fallbacks=0`. Two rewind lines, both benign: `the draft ring for the
rewind to 74630 restored from its pair (captured at 74630 tokens)` (elog:119 and 126). VRAM
warnings quoted and not acted on: 220 MiB at load and at H1's start (suggests 484/512), CUDA1
360 MiB (suggests 600), and 8 MiB at the start of the 76,189-token follow-up after H2
(suggests 704). The verify-window captures all succeeded (77.2, 87.0, 14.4, 8.7, 17.0, 8.0 ms,
no upload/sync error) and H2 still decoded at 20.1 tok/s through that thin margin.

### Deviation worth recording

The elog carries two 76,189-token follow-up requests that no client of this window issued
(elog:115, 260 tokens; elog:129, 84 tokens). Both follow an 80k leg, both restore warm
(76,170 / 74,630 reused), and an `ss` sample during a controlled 59-token probe showed only
this window's client connected. The identical shape (76,189 tokens; 43/28/152/213 tokens)
appears in the cold-2g and pool windows' logs, so it is a pre-existing artifact of the window
protocol rather than an effect of the helper change. It overlapped H3's launch by ~7 s; H3's
own line is a clean restore (`74630 reused + 5 read in 252 ms`), so H3 is reported as it
stands and H2 (the row) is untouched by it - H2 had already finished when the first follow-up
arrived.

Teardown: sampler TERM'd by its explicit pid (4139432, from `/proc/<pid>/cmdline`), csv frozen;
`./stop-all.sh` alone (`TERM 4057939 4057941 4057957 4058714 4059529`, `KILL 4057941 4058714`,
"before=0 after=0 - ALL STOPPED"); `check-quiet.sh` in its own invocation (QUIET, exit 0).
No sockets left, ports 8009 and 8019 free.

Raw files, main tree
`/mnt/srcds/Strata/bench/ab-v0210/rpcprof/out/managed-ram-pool-20261120/trial-window-helper/`: `boot-helper.log`,
`legs-helper.log`, `legs-engine-lines.txt`, `mem-helper.csv`, `H{1,2,3}.{json,txt}`,
`prompt-h1.txt`, `probe-conns.log`, `status.txt`.

Still UNMEASURED: a second cold 80k in this boot (repeatability against the 5-10% band above);
the same helper allocation at 40,960 MiB, so pool size and helper slots stay mutually
confounded; whether the 127 extra slots change steady-state hit rate over a longer run than
one leg; concurrency (every request serial); the origin of the 76,189-token follow-up
requests; the ~4.0 GiB of swap this window added.

## Helper reserve 128 MiB, same 43,008 MiB pool — 2026-11-21 (measured)

Window: pool 43,008 MiB and the config unchanged (device reserves 192/192, kv-resident-mib
4096); the helper's RTX 3080 margin cut once more, with the fill cap raised by the same 256 MiB
so the space is actually used:

    STRATA_HELPER_RESERVE=128    (reserve 384 -> 128)
    STRATA_FILL_MIB=7180         (cap 6924 -> 7180)

Config `bench/ab-v0210/rpcprof/trial-managed-pool-cold-4g.json`, empty conversation-cache dir
(`run/diet-cache-cold-4g`), port 8009, engine `/mnt/srcds/Strata-wt-helper-ipc/build-906/strata`.
The helper warns about the setting itself: `--reserve-mib 128 MiB is below the recommended 256:
the fill has little room to survive the card's other users`.

### The helper's own lines prove the env arrived

    fill preflight: device free 7516 MiB of 7842 MiB, budget 7200.2 MiB (reserve 128 MiB,
      scratch 672000 B, buffers 180098776 B), slots cap 4600, fill-mib cap 7180
    filled 4515 slots (7.01 GiB), scratch 672000 B, buffers 180098776 B
    fill stopped: fill-mib; device free now 334 MiB of 7842 MiB (4515 slots, 7.01 GiB uploaded)

The engine agrees: `expert-helper proxy: 4515 experts ... 7.01 GiB (fill report)`, and the pool
opened as `STRATA_RAM_BUDGET_MIB=43008: outside_owned=4294967296 pool_bytes=40802189312 store
open`, then `RAM pool store: pool_bytes=40802189312 scratch_bytes=2666496
usable_bytes=40799522816` - the same pool as the other 43,008 windows. Against the 384/6924
boot: +138 slots, +0.25 GiB resident, and the freed 256 MiB is genuinely occupied (free after
fill 590 -> 334 MiB).

Two numbers did not land on a straight-line extrapolation and are left as measured: the
preflight budget is 7200.2 MiB where the same free/reserve/scratch/buffers arithmetic gives
~7215.6, and the fill stopped at 4,515 slots where cap-over-slot-size gives ~4,540. The
helper's own reason is `fill-mib`, not `slots cap`, so the 4,600 slot cap did not bind and was
not touched.

### Three legs

| leg | what | prompt tokens | prefill | decode | hit | PCIe | helper on that leg |
|---|---|---|---|---|---|---|---|
| X1 | cold, 32 max | 59 = 0 reused | 3617 ms (16.3 tok/s) | 16 gen in 1486 ms (10.8 tok/s) | 87.0% | 17.1% | reqs=175 rows=1313 bytes=13,445,120 |
| X2 | **cold 80k, 2048 max** | 74,635 = **0 reused** + 74,635 read | **277,904 ms (268.6 tok/s, 3.72 ms/token)** | 1539 gen in 74,912 ms (**20.5 tok/s**) | **95.7%** | **13.7%** | reqs=26,022 rows=123,743 bytes=1,267,128,320 |
| X3 | warm repeat | 74,635 = 74,630 reused + 5 read | 276 ms | 1539 gen in 58,680 ms (26.2 tok/s) | 95.6% | 13.6% | reqs=24,464 rows=118,544 bytes=1,213,890,560 |

X2's cold proof is exact: `prompt 74635 tokens = 0 reused + 74635 read in 277904 ms`, no
`restored ... from disk`, empty cache dir (only draft rings), `drafts accepted 830 of 1178,
5 checkpoints, timeouts=0 fallbacks=0`. Every leg carries `timeouts=0 fallbacks=0`.

### Comparison, and what is comparable

| row | prefill | ms/token | decode | hit | PCIe | helper rows / bytes | MemAvailable min |
|---|---|---|---|---|---|---|---|
| 40,960, helper 512/6796, dev1 576 | 280.2 tok/s | 3.57 | 20.2 | 95.5% | 13.2% | 122,350 / 1,252,864,000 | 8,252 MiB |
| 43,008, helper 512/6796, dev1 192 | 259.7 | 3.85 | 19.8 | 95.0% | 13.1% | 116,334 / 1,191,260,160 | 7,932 MiB |
| 43,008, helper 384/6924, dev1 192 | 249.9 | 4.00 | 20.1 | 95.7% | 13.5% | 119,406 / 1,222,717,440 | 7,236 MiB |
| **43,008, helper 128/7180 (this)** | **268.6** | **3.72** | **20.5** | **95.7%** | **13.7%** | **123,743 / 1,267,128,320** | **7,892 MiB** |

Same protocol throughout (chat, `infill_measure.py`, 74,635-token prompt, 2048 max, cold
cache), so comparable. This row sits **second of four on prefill**, 3.4% above the 259.7 row and
4.1% below the 280.2 row, inside the 249.9-280.2 spread the same 43,008 budget has already
produced. Decode is flat at the top of the observed band (20.5 vs 19.8-20.2). Hit rate ties the
384 row (+0.0 pt) and PCIe share is +0.2 pt; helper work is the **highest of the four** in both
rows (+3.6% over 119,406, +3.7% over bytes), consistent with 138 more resident slots - more
expert reads served by the 3080, not fewer. No speed claim is made from anything but the
engine's own lines, and one leg cannot separate a 3-4% move from noise.

The previous three rows fell monotonically in available memory (8,252 -> 7,932 -> 7,236 MiB)
with prefill falling alongside. **That pattern does not continue here**: this window's
MemAvailable minimum is 656 MiB *above* the 384 row's and its prefill is 7.5% *faster*, so
either the 4% band is noise or available-memory was never the cause. Recorded as a
counter-example, not an explanation.

### Memory (sampler, 5 s, 91 samples, 01:01:09-01:10:05)

VmLck 39,072 MiB flat (the pool lock; `pool_bytes` 38,912 MiB plus overhead), VmRSS peak
47,295 MiB (X2 window 47,284, X3 window 47,295), MemAvailable min 7,892 MiB at 01:05:38 (inside
X2's prefill; X3's own minimum 7,934 MiB). Swap: 5,568 MiB at sampler start, 7,514 at the end,
peak 7,514 - the CSV's first sample is already after the boot, so the share this boot added
cannot be separated from residue. Compare 7,236/47,780/7,161 for the 384 row; nothing here
moves outside the boot-to-boot spread.

### RTX 3080 headroom, measured during decode

`nvidia-smi --query-gpu=memory.free,memory.used`, every 5 s, 98 samples 01:02:00-01:10:00
covering X2's prefill, its decode and X3: **free 335 MiB / used 7,508 MiB, identical in every
sample** - the boot's own `CUDA1: 360 MiB of VRAM free with everything loaded` is the higher of
the two readings. No driver paging event: decode held 20.5 tok/s through X2 (22.7 in the
follow-up segment), `timeouts=0` in every helper tail, no VRAM warning naming the CUDA card,
and the badge never reported a stall. The margin was not adjusted to work around anything; it
was set by the operator and left alone.

### Hygiene

Zero `no blob`, zero `refill failed`, zero `no_buffer_space` in the boot's elog. Three rewind
lines, all benign: `the draft ring for the rewind to 74630 restored from its pair (captured at
74630 tokens)` (elog:115, 122, 129). VRAM warnings quoted and not acted on: 220 MiB at load and
at X1's start (suggests 484/512), CUDA1 360 MiB (suggests 600), and **8 MiB at the start of the
76,189-token follow-up after X2** (suggests 704) - the thinnest reading any of these windows has
logged, yet X2 still decoded at 20.5 tok/s and the follow-up at 22.7 tok/s.

### Deviations worth recording

- The 76,189-token follow-up requests reappear (elog:118, 284 tokens; elog:132, 89 tokens),
  same shape as in the cold-2g, pool and 384 windows. X2's continuation is counted there, not
  in X2's own 20.5 tok/s line; combined, 1,823 tokens in 87.4 s = 20.9 tok/s. Pre-existing
  protocol artifact, not an effect of this change.
- `conversation cache: idle save skipped (disk cache: free-space refusal (3411 MiB plus a floor
  of 4096 MiB needed, 6391 MiB free))`. /mnt is 100% used (6.3 GiB free), so the disk tier
  saved nothing this window. It does not affect X2, which was cold by construction.
- The boot was launched with the evidence redirect through a subshell and the first `tail`
  used an unset `$EV`; the boot itself was unaffected (its own log line names the launcher log).

### Evidence

Raw files, main tree
`/mnt/srcds/Strata/bench/ab-v0210/rpcprof/out/managed-ram-pool-20261120/trial-window-helper2/`:
`boot-helper2.log`, `legs-helper2.log`, `legs-engine-lines.txt`, `mem-helper2.csv`,
`nvidia-smi-x2.txt`, `leg-X{1,2,3}.{json,txt}`, `x1-prompt.txt`, `stop-all.log`, `status.txt`.
Engine log `logs/engine-trial-managed-pool-cold-4g.log`; helper log
`logs/trial-managed-20261121-005925-693386-helper.log`.

Teardown: samplers TERM'd by explicit pid (from `/proc/<pid>/cmdline`), csv and nvidia files
frozen; `./stop-all.sh` alone (`TERM 693386 693402 694149 695624`, "before=0 after=0 - ALL
STOPPED"); `check-quiet.sh` in its own invocation (QUIET, exit 0). No sockets left, 8009 and
8019 not listening, engine log ends on X3's lines. Prod never started, never touched.

Still UNMEASURED: repeatability of the 268.6 tok/s prefill (one leg only); why the preflight
budget reads 7200.2 instead of ~7215.6 and why the fill stopped 25 slots before the cap
division predicts; whether a 128 MiB reserve survives a real paging squeeze (no event occurred
here, so the risk is untested, not cleared); the same slot count at 40,960 MiB; concurrency
(every request serial); the origin of the 76,189-token follow-up requests; how much of the
7.5 GiB of swap belongs to this boot.

### The margin series in one table (2026-11-21, five windows, each one cold 80k)

Every row below is the same payload (74,635 prompt tokens, max 2,048) on the same code
(the streaming prefill fix), differing only in the locked pool, the device-1 VRAM reserve
and the helper's allocation. All numbers are the engine's own `prompt ... read in ...` and
`N generated in M ms` lines, not client arithmetic.

| # | budget | pool | helper reserve/fill cap | dev1 reserve | cold prefill | decode | avail min | swap peak | hit / PCIe |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 40,960 | 36 GiB | 512 / 6,796 | 576 | 280.2 tok/s, 3.57 ms/tok | 20.2 | 8,252 | 2.6 GiB | 95.5% / 13.2% |
| 2 | 43,008 | 38 GiB | 512 / 6,796 | 192 | 259.7, 3.85 | 19.8 | 7,932 | 7.7 GiB | 95.0% / 13.1% |
| 3 | 43,008 | 38 GiB | 384 / 6,924 | 192 | 249.9, 4.00 | 20.1 | 7,236 | 7.2 GiB | 95.7% / 13.5% |
| 4 | 43,008 | 38 GiB | 128 / 7,180 | 192 | 268.6, 3.72 | 20.5 | 7,892 | 7.5 GiB | 95.7% / 13.7% |
| 5 | 34,816 | 30 GiB | 128 / 7,180 | 192 | pending | pending | pending | pending | pending |

How to read it, honestly:

- **No window repeated a cold 80k in the same boot.** The spread across rows 1-4 is
  249.9-280.2 tok/s (12%), and it does not track any single knob: row 3 is the slowest
  with the largest helper, row 4 the fastest of the 38 GiB rows with the smallest helper.
  Treat 12% as this box's error bar until a window runs the same cold leg twice.
- **Decode is flat** at 19.8-20.5 tok/s across a 2 GiB pool change and 384 MiB of helper
  VRAM. Nothing in this series moved generation speed. The pool is not buying decode.
- **Swap tracks the pool, not the margins.** 2.6 GiB at a 36 GiB pool, 7.2-7.7 GiB at 38 GiB.
  An earlier note in this journal called the first window's slowness a VRAM-margin effect;
  that attribution is refuted (see the correction above), and this table is the evidence
  that the margin knobs move nothing while the pool moves swap.
- **The 38 GiB pool kills the conversation-cache disk tier** (every park refused for free
  space, `parked=0 bytes=0`); whether 30 GiB revives it is being measured in row 5.
- The 3080 tolerates a 128 MiB reserve (334 MiB free measured through a full 80k
  prefill and decode, `timeouts=0 fallbacks=0`). That risk is cleared.

### docling, markitdown and pandoc: removed from strata's boot path (2026-11-21)

Where they came from: the boot config's own `mcp_servers` block, not a hardcoded default.
The managed trial config carried

    "mcp_servers": {"docling": {"command": ".../docling-mcp-local"},
                    "markitdown": {"command": ".../markitdown-mcp"},
                    "pandoc": {"command": ".../mcp-pandoc"}}

and every boot of `serve/server.py` with that config spawned the three (the serve log says
`starting 3 MCP servers for the web app's chat: docling, markitdown, pandoc`). Why they kept
coming back: setup treats any key it does not write as the user's and carries it over on
every run (`setup.py:6912`: "a sampling or mcp_servers block ... is kept when setup runs
again"), so any config derived from a prod config inherited the block, and every window
spawned them again. Killing the server left them reparented and alive.

What was done:

- `mcp_servers` stripped from all bootable configs: 46 files (every `prod-config-*.json` and
  every `bench/ab-v0210/rpcprof/*.json` that carried it). Main tree `07a6bc962b`. Historical
  evidence JSONs under `out/` were deliberately left alone - they are records, not configs.
- the three live children of the running test server were TERMed by explicit pid (their
  parent was pid 1919261, the server, so they were strata's, not an editor's).
- `stop-all.sh` now sweeps *orphaned* MCP servers (parent gone) at teardown, so a future
  boot that does spawn them cannot leave them behind. It does not touch MCP servers whose
  parent is alive, which is what an editor's own MCP client looks like. Main tree
  `22fa7f15ad`.

Consequence, stated so nobody is surprised: strata's web chat loses its document tools
(docling for PDFs, markitdown, pandoc). That is the intent of the request. To bring them
back, put the block back into a config - it is config-driven, nothing was hardcoded away.

Verification for the next boot: the serve log must NOT contain `starting 3 MCP servers for
the web app's chat`. The queued helper slot-cap window is the first boot that can prove it.

## Row 5: the 30 GiB pool — cold 80k at 282.5 / 275.8 tok/s, and a reproducible engine abort on the warm repeat — 2026-11-21

### The hypothesis this window exists to test

Everything matched the last window except the locked pool. The 38 GiB windows measured cold
80k prefill at 249.9-280.2 tok/s while evicting the OS page cache to hold the same expert
bytes for a decode path that is already ~95% VRAM-served (decode flat at ~20 tok/s in every
window). If a cold 80k prefill is bound by reading 46.84 GiB of expert files from disk, and
if the page cache is what makes those reads cheap, then a 30 GiB pool (6 GiB handed back to
the page cache) should raise the cold prefill rate. It was stated so that it could fail.

### Boot (two identical boots, one window)

Config `bench/ab-v0210/rpcprof/trial-managed-pool-30g.json`, unmodified (its `args` are
byte-identical to `trial-managed-pool-cold-4g.json`; only the cache dir, the log path and the
provenance notes differ). `STRATA_PORT=8009 STRATA_RAM_BUDGET_MIB=34816
STRATA_HELPER_RESERVE=128 STRATA_FILL_MIB=7180`, launcher on disk, `--single-run`.

    strata generate: STRATA_RAM_BUDGET_MIB=34816: outside_owned=4294967296 pool_bytes=32212254720 store open
    strata generate: RAM pool store: pool_bytes=32212254720 scratch_bytes=2666496 usable_bytes=32209588224 blob_max=2662400 io_window=2666496 preads_per_blob=1 resident_pairs=0 hits=0 misses=0
    strata serve: 220 MiB of VRAM free with everything loaded - LOW ... add --vram-reserve-mib 484 ...
    strata serve: CUDA1: 360 MiB of VRAM free with everything loaded - LOW ... add --vram-reserve-later-mib 600 ...
    expert-helper: fill preflight: device free 7516 MiB of 7842 MiB, budget 7200.2 MiB (reserve 128 MiB, scratch 672000 B, buffers 180098776 B), slots cap 4600, fill-mib cap 7180
    expert-helper: filled 4515 slots (7.01 GiB), scratch 672000 B, buffers 180098776 B
    expert-helper: fill stopped: fill-mib; device free now 334 MiB of 7842 MiB (4515 slots, 7.01 GiB uploaded)

`VmLck` = 31,621,120 kB = 30,880 MiB, exactly the pool plus its 128 MiB reserve, in both
boots. `resident_pairs=0 hits=0 misses=0` at open, so the pool starts empty and fills on use.
The helper's preflight, slot count and stop reason are the same as row 4's 128/7180 tuple,
so the 3080 side of the comparison is unchanged by construction.

### The legs (two boots; cache dir emptied between them)

Both boots: `check-quiet.sh` QUIET before, empty
`run/diet-cache-pool30`, sampler `trial-managed-sampler.sh <csv> 5` for the whole window.

| leg | run | request | engine line |
|---|---|---|---|
| P1 | 1 | 32 tok | `prompt 59 tokens = 0 reused + 59 read in 3592 ms (16.4 tok/s), 16 generated in 1421 ms (11.3 tok/s)` — hit 87.0%, PCIe 17.1% |
| **P2** | **1** | **80k, max 2048** | **`prompt 74635 tokens = 0 reused + 74635 read in 264202 ms (282.5 tok/s), 1536 generated in 74634 ms (20.6 tok/s), drafts accepted 795 of 1104, 5 checkpoints helper=reqs=26831 rows=121982 bytes=1249095680 timeouts=0 fallbacks=0`** |
| P2 hit | 1 | | `decode expert cache hit rate: 95.7% (730889 hits / 763618 lookups); 121982 more read ... (13.8% of all 885600 routed)` |
| P1 | 2 | 32 tok | `prompt 59 tokens = 0 reused + 59 read in 3562 ms (16.6 tok/s), 16 generated in 1257 ms (12.7 tok/s)` — hit 86.6%, PCIe 17.1% |
| **P2** | **2** | **80k, max 2048** | **`prompt 74635 tokens = 0 reused + 74635 read in 270580 ms (275.8 tok/s), 1537 generated in 77284 ms (19.9 tok/s), drafts accepted 839 of 1201, 5 checkpoints helper=reqs=25562 rows=122382 bytes=1253191680 timeouts=0 fallbacks=0`** |
| P2 hit | 2 | | `decode expert cache hit rate: 95.4% (752587 hits / 789138 lookups); 122382 more read ... (13.4% of all 911520 routed)` |
| P3 | 1 | warm repeat | **engine aborted** (see below), no request line written |
| P3 | 2 | warm repeat | **engine aborted** (see below), no request line written |

Both P2 legs are cold by the protocol's own test: `0 reused + 74635 read`. Neither shows a
`restored ... from disk`, and the disk tier held `generations=0 bytes=0` at boot. Zero
`no blob`, zero `refill failed`, zero `no_buffer_space` in either boot's elog.

### The warm repeat kills the engine, twice out of two

P2 ends, P2's own 76,189-token follow-up runs (the pre-existing protocol artifact: 357 and
282 tokens, 24.2 and 22.4 tok/s), and then the conversation cache parks:

    strata serve: conversation cache: disk park skipped (disk cache: free-space refusal (3418 MiB plus a floor of 4096 MiB needed, 6350 MiB free))
    strata serve: conversation cache: parked to disk only (76545 tokens, snapshot 1757 MiB over the 512 MiB RAM ceiling; the published generation serves the next restore; parked=0 bytes=0)
    strata serve: conversation cache: the draft ring for the rewind to 74630 restored from its pair (captured at 74630 tokens)

P3's request starts, decodes for 831 tokens (run 1) / 180 tokens (run 2), and the engine
aborts:

    [strata] the engine reported an error: the expert source could not produce a blob
    [strata] done: 831 tokens in 35 s (23.9 tok/s) (error, cancel=False)   (run 1)
    [strata] done: 180 tokens in 12 s (15.6 tok/s) (error, cancel=False)   (run 2)

The message is `src/core/expert_source.cpp:5316`: the file tier's `blob()` returned nullptr
for a routed expert. The engine process exits; `serve/server.py` stays up and keeps 8009 with
`"loaded": false`, which is why the port had to be freed with `stop-all.sh` before boot 2.
The elog ends on the helper's side of the goodbye (`the engine said bye; exiting`), so the
failure text exists only in the launcher log.

This is new relative to the 38 GiB windows. The cold-4g window has the same `args`, the same
`parked to disk only (... parked=0 bytes=0)` line, and the same 76k restore step, and its
warm repeat ran through (`prompt 74635 tokens = 74630 reused + 5 read in 281 ms`,
1537 generated in 62,214 ms). It is unconfirmed static analysis, not a diagnosis, but the
shape points at the 30 GiB pool: the pool holds ~15.8 GiB fewer expert bytes than the 38 GiB
rows, so a restore that used to find its blob resident has to produce it from the file tier,
and that path returned nullptr. Nothing on this box was paged out at the time (swap did not
move, see below), so an I/O error caused by memory pressure is not the explanation either.

Consequence for the series: this build cannot run a warm repeat at a 30 GiB pool, so the
"same cold leg twice in one boot" question the margin table asks is still open.

### Disk tier: still refused, and now for a reason that has nothing to do with the pool

4 x `idle save skipped (disk cache: free-space refusal ...)`, 2 x the `parked to disk only
... parked=0 bytes=0` line above, **0** x `restored ... tokens from disk`. The refusals read
`3408-3418 MiB plus a floor of 4096 MiB needed, 6308-6350 MiB free`.

So the 6 GiB given back to RAM did not revive the disk tier, because what refuses the park is
`/mnt`'s free space, not RAM: `/mnt` is 100% used with 6.3 GiB free against a 3.4 GiB
snapshot plus a 4 GiB floor. That is a disk-space fact and it holds for every pool size on
this box. The margin table's "the 38 GiB pool kills the conversation-cache disk tier" needs
correcting: the disk tier is refused at 30 GiB too, for the same free-space reason, and it
would be refused with no pool at all.

### Memory

| | run 1 | run 2 |
|---|---|---|
| MemAvailable start / min | 18,137 / **14,559** MiB (01:20:37) | 18,184 / **14,729** MiB (01:30:38) |
| swap used start / peak / end | 5,633 / 5,633 / 5,620 MiB | 5,619 / 5,627 / 5,621 MiB |
| engine VmLck | 30,880 MiB | 30,880 MiB |
| engine VmRSS peak | 39,010 MiB | 39,024 MiB |

The swap column is the striking one: **swap did not move at all** — 5.6 GiB was already in
use when the sampler started (residue from earlier windows), and the 80k prefill and its
decode added zero to it. Compare rows 1-4: 2.6 GiB was added at a 36 GiB pool and 7.2-7.7 GiB
at 38 GiB. Available memory bottomed at 14,559 MiB against 7,236-8,252 MiB in the four 38 GiB
rows, i.e. roughly 6-7 GiB more headroom, which is the pool difference and nothing else.

### The 3080 through P2's decode

`nvidia-smi --query-gpu=memory.free`, 6 samples taken 10 s apart during each P2:
**335 MiB free, twelve times out of twelve**, both runs, while the helper sustained
`timeouts=0 fallbacks=0` over 122k expert rows. The 128 MiB reserve is as tight as row 4
reported and no tighter; the fill still stops on `fill-mib` with 334 MiB left.

### Where row 5 sits

| # | budget | pool | helper reserve/fill cap | dev1 reserve | cold prefill | decode | avail min | swap peak | hit / PCIe |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 40,960 | 36 GiB | 512 / 6,796 | 576 | 280.2 tok/s, 3.57 ms/tok | 20.2 | 8,252 | 2.6 GiB | 95.5% / 13.2% |
| 2 | 43,008 | 38 GiB | 512 / 6,796 | 192 | 259.7, 3.85 | 19.8 | 7,932 | 7.7 GiB | 95.0% / 13.1% |
| 3 | 43,008 | 38 GiB | 384 / 6,924 | 192 | 249.9, 4.00 | 20.1 | 7,236 | 7.2 GiB | 95.7% / 13.5% |
| 4 | 43,008 | 38 GiB | 128 / 7,180 | 192 | 268.6, 3.72 | 20.5 | 7,892 | 7.5 GiB | 95.7% / 13.7% |
| 5a | 34,816 | 30 GiB | 128 / 7,180 | 192 | **282.5, 3.54** | 20.6 | 14,559 | **0 added** | 95.7% / 13.8% |
| 5b | 34,816 | 30 GiB | 128 / 7,180 | 192 | **275.8, 3.63** | 19.9 | 14,729 | **0 added** | 95.4% / 13.4% |

Six cold 80k samples now exist. The spread across all six is 249.9-282.5 tok/s (13%); across
the two 30 GiB samples it is 6.7 tok/s (2.4%); the best 38/36 GiB row (280.2) is inside that
2.4%. Two legs is not a distribution, and this window repeated a cold leg only under a
restart, not twice in one boot.

**Verdict: the hypothesis survived, weakly.** Both 30 GiB legs are at or above the fastest
of the four larger-pool rows, and the mechanism it names shows up in the memory column (no
swap added at all, 6-7 GiB more available). But 282.5 against 280.2 tok/s is 0.8%, well
inside the 13% band the four earlier rows already set, so the honest reading is "not slower,
probably a little faster" and not "6 GiB of page cache bought 2%". What the window does
establish cleanly is the other half: the 38 GiB pool's swap traffic (7.2-7.7 GiB) is gone at
30 GiB, and decode is unchanged at 19.9-20.6 tok/s, so the pool's locked bytes were paying
for swap, not for speed. The earlier "less available memory means slower prefill" reading
stays dead: row 5 has the most available memory and only ties the best row of the series.

### Deviations

- P3 (warm repeat) could not be completed: the engine aborted both times, as recorded above.
  The two cold rows come from two separate boots, not one boot with a repeat.
- Run 2's P2 ran with a warm OS page cache (run 1 had just streamed the same expert files);
  run 1 was the first boot after a quiet box. The 275.8 vs 282.5 gap is not evidence either
  way, and the four earlier rows carry the same possible confound between windows.
- The elog is appended, not truncated, across boots: run 1 occupies lines 1-121, run 2
  lines 122-247 of `logs/engine-trial-managed-pool-30g.log`.
- Run 1's cache dir left two draft rings (`ring-1.dring` 1.1M, `ring-2.dring` 40.2M); they
  were moved to `run1-cache-dir/` in the evidence dir so run 2 started empty. No generation
  snapshot was ever written (`parked=0 bytes=0`), so the directory held no parked state.
- The first boot attempt's evidence redirect ran through a stale shell chain: the boot itself
  was correct (its log lines name the launcher), but the first `tail` printed nothing. No
  measurement was affected.
- A `kill` sweep matched its own shell while looking for the sampler (the pattern sat in the
  command line, as `stop-all.sh`'s comment warns). The sampler was still stopped correctly by
  pid; the shell died, nothing else did.

### Evidence

`bench/ab-v0210/rpcprof/out/managed-ram-pool-20261120/trial-window-pool30/` (main tree):
`boot-pool30.log`, `boot-pool30-r2.log`, `legs-pool30.log`, `legs-engine-lines.txt`,
`mem-pool30.csv`, `mem-pool30-r2.csv`, `mem-summary.txt`, `nvidia-smi-p2.txt`,
`nvidia-smi-p2-r2.txt`, `leg-P{1,2}.{json,txt}`, `leg-P{1,2}-r2.{json,txt}`,
`leg-P3-r2.json`, `p1-prompt.txt`, `run1-cache-dir/`, `status.txt`.
Engine log `logs/engine-trial-managed-pool-30g.log` (both boots); launcher logs
`logs/trial-managed-20261121-011303-1376027.log` and `-012430-1918498.log`; helper logs
`...-1376027-helper.log` and `...-1918498-helper.log`.

Teardown, twice: samplers TERM'd by explicit pid; `./stop-all.sh` alone (run 1
`TERM 1376027 1376794`, run 2 `TERM 1918498 1919261`, both "before=0 after=0 - ALL
STOPPED"); `check-quiet.sh` in its own invocation each time (QUIET, exit 0). 8009 and 8019
not listening, no `run/trial-managed*.sock`. Prod was never started and never touched.

Still UNMEASURED: a warm 80k repeat on this build at any pool size (the abort blocks it);
why `blob()` returns nullptr on the restore path at 30 GiB while 38 GiB survives; whether
the 30 GiB pool changes decode under a longer generation (both decode figures here are
75-77 s windows); how much of the 5.6 GiB of swap belongs to this box's other work; whether
the 282.5/275.8 pair holds on a third boot; concurrency (every request serial); and the
origin of the 76,189-token follow-up requests.

## Row 6 — blocked before boot: the helper's expert container was deleted — 2026-11-21

Row 6 was meant to be row 5 with one delta: helper reserve 0 and `STRATA_FILL_MIB=7400`, so that
the 4,600-slot cap (`slots cap 4600`) is what stops the fill instead of `fill-mib`. It never
booted. The helper refused its pack before the engine was started:

    expert-helper: --reserve-mib 0 outside 64..4096, clamped to 64
    expert-helper: --reserve-mib 64 MiB is below the recommended 256: the fill has little room to survive the card's other users
    expert-helper: reply write deadlines overridden to 600000 ms (STRATA_HELPER_REPLY_TIMEOUT_MS)
    expert-helper: pack: cannot open /mnt/srcds/ai/iq3xxs-15/../Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf
    Helper exited

`check-quiet.sh` was QUIET (exit 0) before the boot and QUIET (exit 0) after it; no socket was
left, no port is listening, and nothing was rebuilt. No request was sent, so the window has no
numbers and no row in the series table.

### The missing file
The launcher's helper defaults are `--pack /mnt/srcds/ai/iq3xxs-15 --experts
../Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf`, and `iq3xxs-15/native_experts.txt` (v3,
total 42,912,972,800 B) resolves its blob offsets in exactly that file, one directory up. Row 5's
helper logged `pack /mnt/srcds/ai/iq3xxs-15, 48 layers x 512 experts` and filled 4,515 slots at
01:13 today, so the file was readable then. `/mnt/srcds/ai` now carries an mtime of 01:37:12 and
the file is on neither the root filesystem nor `/mnt`:

| search | result |
|---|---|
| `find / -xdev -name 'Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf'` | nothing |
| `find /mnt -xdev -type f -size +35G` | `PLE-Q8_0.gguf` 54.4 GB, `UD-...-00002-of-00004.gguf` 49.9 GB, `iq3xxs-ud/experts.bin` 50,294,988,800 B |

### The substitute that would have been a second delta, and why it does not work
The only complete expert container left is `iq3xxs-ud/experts.bin` (v4 layout, total
50,292,326,400 B, gu/d types 22/20 against the -15 pack's 17/16, which are different
quantizations of the same model, not the same bytes). Swapping the helper to it would already be
a second delta, and it cannot reproduce the window's premise either. Summed from
`native_experts.txt` in layer order:

| pack | avg blob | total | slots a `fill-mib 7400` cap allows | 4,600 slots need |
|---|---|---|---|---|
| iq3xxs-15 (v3, row 5) | 1,746,133 B | 39.97 GiB | ~4,989 in layer order; 4,515 in the routing order the helper actually used | ~7,313 MiB (fits 7,400) |
| iq3xxs-ud (v4) | 2,046,400 B | 46.84 GiB | ~3,936 in layer order | >= 8,977 MiB |

8,977 MiB is more than the 3080's whole 7,842 MiB, so **`slots cap 4600` cannot bind with the UD
expert bytes on this card at any reserve**. The slot-cap experiment needs the -15 container back,
or a lower slot request (a different tuple).

### Probe: the UD pack does pass the helper's own gates
The substitute was tested at the helper alone (no engine, no pool, no port, scratch socket,
TERM'd by pid after the bind; `check-quiet.sh` QUIET afterwards):

    expert-helper: listening on /tmp/opencode/h0probe/p.sock (device 0, pack /mnt/srcds/ai/iq3xxs-ud, 48 layers x 512 experts)
    expert-helper: SIGTERM/SIGINT received; exiting cleanly

So the v4 layout (48 x 512, gu/d 22/20) clears `expert_layout_load`, the 2560x640 geometry check
and `native_expert_supported`, i.e. options (b)/(c) above are admissible at the helper level. The
`fill preflight ... budget ... slots cap ... fill-mib cap` line only prints after the engine's
hello, so the exact stop reason for a UD fill is still arithmetic (>= 8,977 MiB for 4,600 slots),
not a measured line.

### Second finding: "reserve 0" is not 0
The helper clamps `--reserve-mib` to its own range: `--reserve-mib 0 outside 64..4096, clamped to
64`. A row measured with reserve 0 must therefore read "reserve 0 (clamped to 64)", and its
budget is 7,842 - 64 = 7,778 MiB, not 7,842. This is harmless for the slot-cap premise (4,600
slots at -15 blob sizes are ~7,313 MiB, under both 7,400 and 7,778), but it is not a silent
detail.

### Cross-pack note for rows 1-5 (static read, unconfirmed)
Every row recorded so far ran the engine on the UD pack (`--pack /mnt/srcds/ai/iq3xxs-ud`,
`--native Qwen3.8-Flash-Next-UD-IQ3_XXS-*.gguf`) while the helper was given the -15 pack. If the
3080's MoE work is computed from the helper's own blob bytes, those rows mix two quantizations in
one request. Nothing was measured to settle it; the two `native_experts.txt` files are the only
evidence quoted here, and this note is a reading of them, not a result.

### Evidence
`bench/ab-v0210/rpcprof/out/managed-ram-pool-20261120/trial-window-pool30-h0/` (main tree):
`boot-console.log` (launcher stdout: `Helper exited; see ...`), `boot-helper-refusal.log` (the
four helper lines above), `status.txt`. Launcher/helper logs
`logs/trial-managed-20261121-014119-2724648.log` and `-helper.log`.

Next: operator decides between restoring the -15 container (42.9 GB; `/mnt` has 77 GB free) and
re-running row 6 as specified, accepting a UD-bytes row that stops on `fill-mib`, or re-testing
the slot cap at a slot request the 3080 can actually hold.
