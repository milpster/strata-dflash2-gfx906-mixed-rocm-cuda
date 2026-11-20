# L3 boundary — managed RAM pool (single 52 GB-style serving budget)

Source facts (read from /mnt/srcds/Strata-wt-helper-ipc, helper-ipc-diet HEAD 0da4703b98):

- `include/strata/core/ram_pool.hpp`: one anonymous, fully-mlocked mmap sub-allocated from an ordered `list<Range>`. `pool_capacity_bytes(total_bytes, outside_owned, internal_headroom, out_pool_bytes)` = total − outside − headroom, floored to a page. `Pool::create(pool_bytes, ec)`; `pool_alloc(bytes, alignment, out_lease)` returns `no_buffer_space` on no fitting range, `not_enough_memory` on metadata failure ("consumers must not evict on a metadata error"). `PoolLease::data()` stable while a lease copy exists. Default path untouched, not wired in (ram_pool.hpp:11-12). No GPU headers.
- `include/strata/core/pool_expert_store.hpp`: `PoolExpertStore(Pool&)`; `open(experts_bin, layout)` transactional; `acquire(layer, expert, PoolLease& out)` returns a lease; `release/clear/close`; non-owning `Pool&` must outlive store. One pool for scratch+payload; BoundedDirectReader over experts.bin + PoolExpertCache. Default path untouched, opt-in test only (pool_expert_store.hpp:20).
- `src/program/generate.cpp:3712`: `static PoolExpertStore* g_pool_expert_store = nullptr;` — null-by-default seam, never constructed (managed branch never executes).
- `src/program/generate.cpp:24038-24100` (ACCEPTED, do not re-review): managed lent-slot refill uses `g_pool_expert_store->acquire(layer, expert, lease)` → `lease.data()` into `fill_slot_blocking`/`fill_slot_queued_staged`; leases held until `sync_queued` quiesces then cleared; on failure `cudaDeviceSynchronize()` before lease destruction. Legacy `else` branch (:24079-24099) is byte-identical `srcp->blob_stable()`.
- `src/program/generate.cpp:8307,8444,8631`: `ExpertSource* srcp` = `&src` (FileExpertSource, mmap-experts) or `&arena_src` (ArenaExpertSource) or nullptr. No PoolExpertStore wiring yet.
- `src/core/expert_source.cpp`: ArenaExpertSource full-file lock sites — `native_mlock_span(...,"ArenaExpertSource shared expert arena")` (:6018); `cudaHostAlloc(...cudaHostAllocMapped|cudaHostAllocPortable)` (:3929-3930); whole-arena `cudaHostRegister` (:4037,:4080,:4355/PinnedArena). FileExpertSource mapped-pack/GGUF mlock spans :1569, :1773 (STRATA_NATIVE_MLOCK).
- `src/core/layer.cpp:585-622` `kv_host_alloc`: `cudaHostAllocMapped|cudaHostAllocPortable`, or mmap+register_anon (WSL2). `:968-979`: `h=kv_host_alloc(bytes)`, then `cudaHostGetDevicePointer(&d,h,0)`, then `Cursor hc{d}` — the HOST WRITES through `d` and the GPU READS the device alias directly over PCIe. KV REQUIRES a device alias; pool pages are anonymous mlocked mmap, not device-mappable.
- No `STRATA_RAM_BUDGET_MIB` knob exists anywhere. Launch flags live in `setup.py` (`--resident-budget-gib` at :10235); engine knobs are `STRATA_*` env vars read via `getenv` (e.g. STRATA_NATIVE_MLOCK pin_budget.hpp, STRATA_KV_HOST_REGISTER layer.cpp:596).

Proposals (L3 boundary, ONE architecture, no menu):

1. BUDGET SOURCE + DEFAULT. One opt-in knob: env `STRATA_RAM_BUDGET_MIB` (engine env, read at generate.cpp startup, matches existing STRATA_* pattern; no setup.py/plumbing change). Default = unset = OFF = today's byte-identical path. Managed mode REPLACES the full-file expert arena lock, not a second residency. Skip when managed: ArenaExpertSource::open full-file `native_mlock_span` (expert_source.cpp:6018) and whole-arena `cudaHostAlloc`/`cudaHostRegister` (expert_source.cpp:3929-3930, 4037, 4080, 4355). FileExpertSource mmap spans (:1569,:1773) stay only for the mmap-experts fallback. `srcp` becomes a `PoolExpertStore`-backed `ExpertSource` adapter whose `blob()`/`blob_stable()` return the leased pointer (leases held by adapter/consumer); raw store pointer only at the refill seam (:24041, already accepted). Immutable mapped GGUF shards + token tables stay outside the pool (read-only mmap, never mlocked into it) and are counted as `outside_owned` in `pool_capacity_bytes(total=budget, outside_owned=..., headroom)`.

2. HOST-KV. Source fact: GPU reads KV via `cudaHostGetDevicePointer` device alias (layer.cpp:969) written by host through the same pointer — not merely a cudaMemcpy source. Honest L3 binding: KV stays OUTSIDE the pool, charged as `outside_owned`. Pool leases as KV backing with slower pageable copies is NOT viable (no device alias; whole-pool HIP/CUDA registration not approved). Pageable-vs-pinned KV copy cost is UNMEASURED and must not gate implementation.

3. CONSUMER ORDER. Must adapt before store is PRIMARY: `:17117` (VRAM evict-refill, `fill_slot_queued_staged`+`note_async_read`) and `:20300` (lent-slot staged fill, `blob_parts`/`arena_span`, `fill_slot_blocking`/`fill_slot_queued_staged`) — these write expert bytes into VRAM. Can keep legacy `srcp` alongside: `:5611` (native_embed load, not expert blob), `:9346`/`:9522` (nibble read-only verify), ArenaExpertSource `blob()` arena pointers (legacy `srcp==&arena_src` only), `swap_blobs_batch` relief pack-file path (~:2635; task's :15638 is checkpoint_at, not swap_blobs_batch). Smallest first task: make `g_pool_expert_store` non-null at startup (construct store, open experts.bin) so accepted seam :24041 executes; legacy `else` untouched.

4. ACCOUNTING + ADMISSION PREVIEW. `outside_owned` at startup = GGUF shard mappings + token tables + host-KV (outside per §2) + any arenas that are skipped (managed replaces them, so not double-counted). `pool_capacity_bytes(budget, outside_owned, headroom)` → `pool_bytes`; `Pool::create(pool_bytes)` mlocks whole mapping upfront. L4 (multi-process scope/cgroups, helper/server not in engine) named only as input, not designed here. Admission on `pool_alloc` returning `no_buffer_space` during serving: FAIL THE REQUEST — do not evict live borrowers (cache holds live refs; pool_expert_cache.hpp:51-54), do not fall back to legacy (would break single-residency / add second residency). Unmeasured: no eviction policy needed since leases are held only by active refills.

5. TWO SERIAL FIXER TASKS.

Task 1 (budget + pool + store wiring): read `STRATA_RAM_BUDGET_MIB` at generate.cpp startup; if set, compute `outside_owned` (GGUF shards + token tables + host-KV), `pool_capacity_bytes`, `Pool::create`, construct `PoolExpertStore` (`open(experts_bin, layout)`), assign `g_pool_expert_store`; skip ArenaExpertSource full-file mlock + whole-arena `cudaHostAlloc`/`cudaHostRegister` when managed. Files: `src/program/generate.cpp` (startup/srcp selection); `include/strata/core/ram_pool.hpp`, `pool_expert_store.{hpp,cpp}` already exist. Verification: CPU unit test (no GPU) — `Pool::create` small, `pool_alloc` until `no_buffer_space`; `PoolExpertStore` open/acquire/release on synthetic experts.bin; confirm unset env = default OFF compiles and runs no managed code.

Task 2 (adapt primary refill consumers to leases): `:17117` and `:20300` take `PoolLease` from `g_pool_expert_store->acquire` instead of `srcp->blob_stable`; hold leases until `sync_queued`; on failure `cudaDeviceSynchronize()` before lease destruction (mirror accepted :24041). Keep `:5611`, `:9346`/`:9522`, arena-blob (legacy), `swap_blobs_batch` on legacy `srcp`. Files: `src/program/generate.cpp`. Verification: CPU test simulating a refill with a lease; compile-only; default OFF unchanged. No benchmark claims; no giant refactors.

L4 out of scope except naming inputs: cgroup/multi-process scope, helper/server not in engine process.

## Parent addendum (authoritative, amends Task 1 sequencing)

The adapter proposal is deferred: :17117/:20300 still read through `srcp` until
Task 2, so `srcp` must NOT become a store adapter in Task 1. Task 1 sequencing:

- Managed mode (STRATA_RAM_BUDGET_MIB set): ArenaExpertSource is NOT
  constructed at all; `srcp` = FileExpertSource (unlocked mmap fallback for
  every legacy consumer, page-cache-backed). Single LOCKED residency = pool.
  FileExpertSource's own STRATA_NATIVE_MLOCK spans (:1569/:1773) must stay
  disabled in managed mode (do not enable native mlock when the budget knob
  is set).
- The store exists only as `g_pool_expert_store` serving the accepted
  :24041 seam in Task 1; Task 2 moves :17117/:20300 onto it.
- The ExpertSource adapter over the store comes LAST, only after every
  must-adapt consumer is lease-bearing.
- outside_owned inputs must be measured/derived at runtime where cheap
  (mapped shard sizes, token tables, g_kv_host_bytes) — no hardcoded
  constants; headroom stays 0 unless a measured value exists (recorded
  decision: no invented fixed reserve).