Managed RAM pool -> ExpertSource adapter design (REPORT-ONLY, no code written).

Source facts (worktree /mnt/srcds/Strata-wt-helper-ipc, helper-ipc-diet HEAD 4119919):

1. INTERFACE INVENTORY — ExpertSource virtuals consumers actually invoke outside the three adapted seams (src/program/generate.cpp):
- blob_stable() — :2625 (null-check only), :9396/:9422/:9432 (profile fill, copied synchronously into slot via fill_slot_blocking), :9567/:9584/:9594 (layer-split profile fill, synchronous), :11448 -> cudaMemcpyAsync :11451 (retained until sync_queued/sync), :16213 -> pin_blobs spans :16214-16215, :16286 -> DMA batch cp_src :16292-16295 (retained until copies land), :17276/:20491/:24235 (legacy queued fill branches, store==nullptr), :24776/:24826. Async DMA keeps the raw pointer live until the copy completes (cudaDeviceSynchronize / sync_queued / event wait).
- blob() — :3058 (measure_pool_affinity: pointers RETAINED in blobs[] across layers until pool.run_split_multi_native :3089 — long retention!), :20506 (fallback when blob_stable null).
- pinned() — :8698, :10923, :16546, :20498 (bool reads only).
- release() — :10829, :16047, :16727, :20532 (Windows), :21990, :24251 (Windows), :24713 (drops RAM copy once in VRAM).
- blob_parts() — :20485 (legacy only; 0 = not available -> take blob_stable).
- note_async_read() — :17281, :20516, :24242 (#1237: source must not recycle that memory until the queued copy finishes).
- spans() — not an ExpertSource virtual (not present in the interface).
Nullptr handling at call sites: :9398 returns 1 on nullptr; :11449 continues; :20508 returns false with err; :24235 returns 1. None dereference without a null check.

2. OWNERSHIP MAPPING (adapter = PoolExpertStore-serving ExpertSource for legacy consumers):
- blob_stable(layer,expert) / blob(layer,expert): g_pool_expert_store->acquire(layer, expert, lease); insert lease into a per-(layer,expert) borrower table (unordered_map key -> PoolLease); return lease.data(). Borrower holds the PoolLease (shared ownership of the pool mapping), so the raw pointer stays valid until the borrower is dropped. Async-DMA callers (:11448, :16286, :17276, :20491, :24235) get the pointer and the borrower table keeps the lease alive past the call.
- release(layer,expert) / note_async_read(ptr): drop the matching borrower entry -> lease released -> pool range freed (unless another borrower holds it).
- blob_parts(): return ONE part = the whole lease as a single dense buffer at=0 (cite fill_slot_queued_staged accepts one buffer at :17266/:20475/:24211). Justification: the pool needs dense in-slot memory; the GGUF multi-piece path is legacy-only (store==nullptr branch) and choosing one part forces the simple staged path. Alternative (return 0 to force assembled path) also valid but adds a copy the pool doesn't need.
- pinned(): true (pool memory is pre-mlocked per ram_pool.hpp:3-9).
- Borrower table bounds growth: capped by pool capacity — each borrower = one expert lease charged to the Pool; count unbounded but total bytes bounded by pool_capacity_bytes. When full, acquire returns no_buffer_space.
- Pool pressure (acquire fails): adapter returns nullptr; existing staged-read machinery runs. Callers already treat nullptr as read failure (:9398, :20508, :24235). This matches file-source behavior when a staged read fails.

3. FALLBACK DECISION: (a) return nullptr and let existing staged-read machinery run. Justification: PoolExpertCache::acquire eviction drops only cache refs; live borrowers keep allocations, so the pool may still refuse (no_buffer_space) (pool_expert_cache.hpp:50-57). (b) block-retry risks deadlock against async DMA holding leases. (c) never-fail violates the fixed pool budget. Eviction latency is UNMEASURED; safe default is (a).

4. PLACEMENT + TASK SPLIT:
- Adapter class: include/strata/core/pool_expert_source_adapter.hpp + src/core/pool_expert_source_adapter.cpp (new files). CPU-testable against existing store test fixtures under STRATA_RAM_POOL_TEST.
- Lives at generate.cpp srcp selection site: in managed mode srcp = &adapter instead of srcp = &src (Task-1 site; default OFF -> byte-identical). The three adapted seams (:17253-17291, :20464-20527, :24194-24231) use g_pool_expert_store directly and must NOT change.
- Adapter lifetime: must outlive all async DMA that read lease.data() (borrower table holds leases until release/note_async_read drops them).
- Task 1 (serial): write adapter class + headers; compile-only verification + CPU test borrowing/release round-trip against store fixtures.
- Task 2 (serial): generate.cpp srcp selection in managed mode; default OFF; no behavior change when off.

5. RISKS (top 3) + guards:
1. Reader-callback reentrancy: PoolExpertCache reader runs under cache mutex (pool_expert_cache.hpp:8-9); if a fill calls store.acquire -> deadlock. Guard: adapter never calls store.acquire from inside a cache reader callback; borrow paths run outside the cache mutex.
2. Borrower leak pinning the whole pool: entries never dropped -> pool exhausts -> no_buffer_space everywhere. Guard: release()/note_async_read drop the borrower; scope-exit cleanup on failure paths (see :24223-24228 pattern).
3. nullptr mishandled at dispatch fill (~:5611): caller uses pointer without null check -> segfault/corruption. Guard: adapter returns nullptr only on acquire failure; every existing call site already checks null (:9398, :20508, :24235).

Proposals above (items 2-5) are design, not verified source fact.

## Parent addendum (authoritative corrections; implementation must follow these)

1. note_async_read MUST NOT drop a borrower at issue time. Verified legacy contract
   (expert_source.cpp:2124-2146, #1237): the source records a cudaEventDisableTiming
   event on the caller's stream and recycles only after the event completes. The
   adapter mirrors exactly that: record an event on (stream), mark the borrower
   pending-event, sweep completed events on every subsequent note_async_read/release
   call and drop only completed ones; if event create/record fails, fall back to
   cudaStreamSynchronize(stream) then drop (legacy lines 2135/2142). Rationale:
   dropping at issue time frees pool memory an in-flight queued copy still reads —
   the exact bug class every adapted seam guards against — and it stays correct if
   pool ranges are ever device-registered (true-async DMA).
2. pinned(): pool memory is mlocked but NOT device-registered. Returning true is
   CORRECT for the callers' purpose (they select the queued-copy path;
   cudaMemcpyAsync from unregistered mlocked memory is legal — the driver stages
   synchronously before returning — but NOT zero-copy). Keep pinned()=true and
   record the perf caveat: UNMEASURED; no speed claim may cite this path.
3. Borrower drop points (complete list): release(layer,expert) — immediate (caller
   confirms VRAM copy); note_async_read — deferred via events per (1); engine
   teardown — drop all. Nothing else drops.
4. Double-borrow of the same (layer,expert) before release is legal: leases are
   refcounted shared state; the table keeps a per-key count and drops the entry
   when the count reaches zero (data() stays identical across leases of one blob).
5. CPU testability: the CUDA event logic must sit behind a small injectable hook
   (adapter constructor takes an async-note callback; the engine build supplies the
   CUDA event implementation, the CPU test a fake). The CPU test covers: borrow/
   return round-trip byte equality, drop-on-release frees capacity (next acquire
   of a different expert succeeds), pressure -> nullptr (no throw, no drop of live
   borrowers), double-borrow refcount, note-deferral with a fake that never
   completes (borrower retained) and one that completes (dropped on sweep).
