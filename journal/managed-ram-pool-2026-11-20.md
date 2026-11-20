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
