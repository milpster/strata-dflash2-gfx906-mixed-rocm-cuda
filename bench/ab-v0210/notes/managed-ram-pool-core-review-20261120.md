# Managed RAM Pool core review — L1 gate verdict (2026-11-20)

**Scope:** `include/strata/core/ram_pool.hpp`, `src/core/ram_pool.cpp`, `tests/core/ram_pool_test.cpp`, `tests/core/ram_pool_nonlinux_probe.cpp`, CMakeLists opt-in target in worktree `helper-ipc-diet` @ `aa5431a5d1`. No engine source, CMake, tests, journals, runtime, or git changes made by this review. Only this report is written.

## A. Verdict: BLOCK — do not release this foundation lane

One **proven build failure** plus several **static risks** that violate the stated contract. The existing 13/13 green ctest log predates the dirty partial repairs and is not acceptance evidence. Parent re-read `parent-repair-check/build.log` and `build.status`: both exist in the engine-worktree evidence directory and record build exit 1.

### Proven build failure (blocks the gate)

Linker undefined reference to `strata::core::ram_pool_set_lock_step_bytes(unsigned long)` — exactly the parent's reported `exit 1`.

Root cause (confirmed in source):
- `ram_pool.hpp:130-132` declares the test seam behind `#if defined(STRATA_RAM_POOL_TEST)`.
- `ram_pool_test.cpp:20-22` `#define`s then `#undef`s `STRATA_RAM_POOL_TEST` around the include, so declarations are visible in the test TU.
- `ram_pool.cpp:350-352` defines `ram_pool_lock_step_bytes`/`ram_pool_set_lock_step_bytes` behind the same `#if`, but `ram_pool.cpp` never defines `STRATA_RAM_POOL_TEST`, so the definitions are compiled out.
- Result: declarations referenced, never defined → undefined reference. This is reproducible from the source; no rerun needed.

### Static risks (exact symbols/paths/lines)

| # | Issue | File:lines | Kind | Contract violated |
|---|-------|------------|------|-------------------|
| 1 | Alignment applied to pool offset, not absolute `base+offset`. `data()` returns `base + start`; `base` is only page-aligned, so a 64 KiB request is not honored on a misaligned base. | `ram_pool.cpp:280-291` (and `hpp:109` promise) | Bug | Returned-address alignment promise |
| 2 | `end = (uintptr_t)base + pool_bytes` and `lo + lock_step()` unchecked pointer arithmetic. | `ram_pool.cpp:218-224` | Static risk | Checked pointer arithmetic requirement |
| 3 | `std::make_shared<LeaseState>()` and `st.live.emplace(...)` can throw `bad_alloc`, escaping the `std::error_code` API. | `ram_pool.cpp:305-316` | Static risk | error_code vs exception contract |
| 4 | `free.reserve(pool_bytes / kPage)` — one `Range` per page reserves ~220 MiB at 52 GiB to avoid allocation during free. | `ram_pool.cpp:196-202` | Design smell | "reserve hundreds of MiB to avoid an ownership problem" prohibition |
| 5 | Unix headers (`<sys/mman.h>`, `<sys/resource.h>`, `<unistd.h>`) and the `PoolStateDeleter` calling `munlock`/`munmap` are not behind `__linux__`, so the non-Linux refusal claim is false for the implementation TU. | `ram_pool.cpp:22-24`, `:153-161` | Static risk | Non-Linux guard claim |
| 6 | Test: single static 16 MiB `fake_base` aliasing every `mmap`, never freed → multiple pools alias. | `ram_pool_test.cpp:102-109` | Test bug | Test isolation |
| 7 | Test: uses `pool` after it was destroyed at :291. | `ram_pool_test.cpp:365-373` | Test bug | Use-after-destroy seam |
| 8 | Probe `ram_pool_nonlinux_probe.cpp` is not registered in CMakeLists; no skip code/test timeout. | probe + CMakeLists | Test bug | Test registration |

## B. Recommended simple repair architecture (retain useful API)

Keep the public API (`Pool::create`, `pool_alloc`, `pool_free`, `PoolLease`, `PoolStats`, `pool_capacity_bytes`, `PoolLeaseGuard`) and the `shared_ptr<PoolState>` + `shared_ptr<LeaseState>` lifetime design — it correctly keeps the mapping alive until the last lease and makes teardown allocation-free via the deleter. The repairs are bounded to the implementation and the test seam.

### Data structure / lifetime decision
- Keep `std::shared_ptr<PoolState>` owned by `Pool` and shared by every `LeaseState`. The mapping is freed exactly once in `PoolStateDeleter` (munlock+munmap) when the last reference drops — no allocation, no throw, valid during final lease release.
- Keep the coalesced free-range `std::vector<Range>` plus `std::map<LeaseState*, Live>` for live tracking.
- **Remove the per-page `free.reserve(pool_bytes / kPage)`.** It reserves hundreds of MiB purely to make `free_range_unlocked` non-throwing. Instead: pre-reserve a small fixed number of slots (e.g. 8–16) at create, and make `free_range_unlocked` handle allocation failure gracefully by still coalescing into existing capacity or leaving the range on a small emergency list. This trades a vanishingly small fragmentation risk for not reserving ~220 MiB. Simpler valid structure; metadata outside pool accounted later per design.

### Allocation-failure transaction (plain error_code contract)
Reorder `pool_alloc` so no published state mutates before a committing point:
1. Validate args + find first-fit slot (read-only on stats/free).
2. Build candidate `LeaseState` via `make_shared` inside a try/catch → on `bad_alloc`, return `errc::not_enough_memory`; state untouched.
3. Insert into `st.live` keyed by raw pointer; if it fails (should not), return error; state untouched.
4. Only then: update stats, carve the free range, publish `out_lease.impl_ = ls` **after** dropping the mutex.
5. `emplace` into `live` must not throw — `std::map::emplace` allocates the node; wrap in try/catch too. If it fails, erase nothing and return error.

This makes `pool_alloc` a transaction: error leaves stats/free/output unchanged; no exception escapes the `std::error_code` API.

### Alignment fix
Compute absolute returned address: `base + round_up(base_offset_to_alignment, alignment)`. Since `base` is `mmap`'d (page-aligned only), the alignment rounding must account for `reinterpret_cast<uintptr_t>(base) % alignment`. Simplest correct form: walk free ranges by absolute `addr = base + start`, round `addr` up to `alignment`, derive `start = addr - base`, check bounds. This directly satisfies "returned-address alignment."

### Non-Linux guard
Wrap `ram_pool.cpp` syscall bodies AND the `PoolStateDeleter` in `#if defined(__linux__)`; on other platforms `Pool::create` returns `errc::not_supported` and the deleter is a no-op (or the whole TU is conditionally compiled). Keep `<sys/...>` includes inside the guard or provide stubs. The probe (compiled with `-U__linux__`) validates the header-only path; the cpp must also compile-refuse cleanly.

### Remove
- The test-only `ram_pool_set_lock_step_bytes` seam can stay (it is the cleanest injectable lock-failure hook), but it must be defined in the implementation TU unconditionally or consistently gated so the link succeeds. Prefer: define it always (small symbol) and guard only its *use* in tests, or keep the macro but apply it to both declaration and definition TUs.

## C. Minimal test strategy (tiny, deterministic, time-bounded)

Reuse the standalone CPU CMake route (`STRATA_BUILD_RAM_POOL_TEST`, `ram_pool_test`, `native_mlock_nonlinux_probe`). Real scenario uses `<= 16 MiB` anonymous mmaps; no large locks, no RLIMIT changes in mock mode.

1. **Injectable lock callbacks instead of a broken global fake buffer.** Interpose `mmap`/`mlock`/`munlock`/`munmap` per test with strict call counters. `mmap` returns a distinct fresh page-aligned buffer per call (no aliasing). Track first/later lock failure `<= 16 MiB` via `fail_after_mlock`.
2. **Strict mapping/lock counters:** exactly one `mmap` per `create`; on lock failure, exactly one `munmap`, `munlock` count == locked chunks, no partial pool left.
3. **Absolute alignment on deliberately misaligned base:** interpose `mmap` to return a base not aligned to the requested alignment (e.g. base = page-aligned but not 64 KiB-aligned), request 64 KiB, assert returned `data() % 65536 == 0`.
4. **Populated-output safety:** allocate into an `out_lease` that already owns a block (replacement) and into a different pool's lease; assert no deadlock, old block freed, stats reflect only the new block.
5. **Concurrent leases:** 4 threads, small cap, alloc/free; assert `reserved_live_bytes + free_bytes == capacity_bytes` invariant and no crash.
6. **Metadata failure:** inject `make_shared`/`emplace` failure (or exhaust via tiny pool) and assert `std::error_code` returned, no exception, stats unchanged.
7. **Real VmLck:** `real_mock` 4 MiB pool via `RTLD_NEXT`; assert VmLck rise after lock, hold after chunk release, fall at teardown. If `mlock` is not permitted (RLIMIT/CAP), skip with CTest `SKIP_RETURN_CODE 77`, return 77, do not report green.
8. **Lifetime:** pool destroyed before last lease → lease still valid/writable; final release triggers exactly one munlock+munmap.
9. **Non-Linux probe:** registered in CMake, compiled with `-U__linux__`; assert `not_supported`, zero capacity/base, pure arithmetic works.

Tests must return non-zero on any failure, 77 on skipped real test, and run in <1 s each.

## D. Bounded next-fixer task (exact file scope)

**Task:** Fix the link failure and the four contract bugs in the RAM pool foundation; make tests pass.

**File scope (only these):**
- `include/strata/core/ram_pool.hpp` — keep API, possibly adjust seam gating.
- `src/core/ram_pool.cpp` — fix alignment to absolute base, wrap syscalls/deleter in `__linux__`, make `pool_alloc` transactional (error_code, no throw), remove per-page `free.reserve`, define lock-step seam consistently.
- `tests/core/ram_pool_test.cpp` — distinct fake mmap buffers, remove use-after-destroy, strict counters, injected lock failure, alignment-on-misaligned-base, populated-output replacement, concurrent leases, metadata failure, real VmLck with SKIP 77, return 77 on skip.
- `tests/core/ram_pool_nonlinux_probe.cpp` — keep; register in CMakeLists.
- `CMakeLists.txt` — register the probe target; keep `ram_pool_test` opt-in target.

**Acceptance commands (CPU-only, no GPU, no network):**
```
cmake -S . -B build-cpu-ram-pool -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DSTRATA_BUILD_TESTS=ON -DSTRATA_ENABLE_CUDA=OFF -DSTRATA_ENABLE_HIP=OFF \
  -DSTRATA_ENABLE_SYCL=OFF -DSTRATA_BUILD_RAM_POOL_TEST=ON
cmake --build build-cpu-ram-pool --target ram_pool_test native_mlock_nonlinux_probe --parallel 2
# expect exit 0, no undefined references
ctest --test-dir build-cpu-ram-pool -R "ram_pool|native_mlock_nonlinux"  # expect all pass; real_mock skips 77 if mlock not permitted
./build-cpu-ram-pool/ram_pool_test mock   # exit 0
./build-cpu-ram-pool/ram_pool_test real_mock  # exit 0 or 77 (skip)
./build-cpu-ram-pool/native_mlock_nonlinux_probe  # exit 0
```

**Out of scope for this task (remain blocked until core gate passes):** expert/KV integration, `kv_host_alloc` redirection, `PoolExpertSource`, budget knob, cgroup/`memory.max`, any default-path or runtime-flag change, whole-stack serving feature claim. Prod stays down; the parent owns promotion and integration.

## Parent acceptance amendment — minimal repair contract

The BLOCK verdict stands, but B's emergency-list proposal is rejected. A
small fixed vector plus a best-effort free path can lose capacity under the
very pressure the pool must handle. Exhausting a tiny payload pool is also
not a metadata-allocation failure test. No probabilistic fallback is accepted.

Use one ordered `std::list<Range>` partition of the entire pool, with each
node marked free or allocated. A live LeaseState retains its node iterator
and shared PoolState ownership. There is no separate live map or per-page
metadata reservation. Under the mutex, prepare a candidate lease and up to
three replacement nodes in a temporary list before changing the partition.
On allocation failure discard the unarmed candidate and return an error with
state/output unchanged. Commit with non-allocating list splice/erase, arm
the lease, update stats, unlock, then replace the output. Last lease release
marks its existing node free and merges adjacent free nodes using erase;
it needs no new node or emergency storage. Other live iterators stay valid.

The mapping owns its cleanup once, including a partly locked prefix. Use
actual Linux page size and checked absolute-address alignment. Test-only
hooks may inject lock/metadata failures, but use distinct valid small
mappings; no process-global fake mmap buffer. A strict real VmLck test must
show baseline, fully locked, chunk-freed, owner-dropped and final-release
values, or return genuine SKIP 77 for a demonstrated lock restriction.
This is still a CPU foundation, not whole-serving budget enforcement.
