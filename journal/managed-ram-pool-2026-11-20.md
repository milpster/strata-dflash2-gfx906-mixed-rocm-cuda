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
