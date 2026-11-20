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
