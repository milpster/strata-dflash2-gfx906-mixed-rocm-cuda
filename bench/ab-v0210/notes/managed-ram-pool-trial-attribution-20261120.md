# Managed RAM pool — trial attribution (first live run, 2026-11-20)

Source read at `helper-ipc-diet` HEAD `ac000b98b0` (worktree `/mnt/srcds/Strata-wt-helper-ipc`).
Facts below are either **SRC** (file:line at that HEAD), **MEAS** (the trial-window logs), or **HYP**
(arithmetic/reasoning, not measured). Report-only: nothing was booted, built or changed.

Geometry (**SRC** `/mnt/srcds/ai/iq3xxs-ud/native_experts.txt`, GGUF header of shard 1):
`block_count 48`, `expert_count 512`, `expert_used_count 10` → **480 routed lookups/token**;
24 576 (layer, expert) pairs, 50 292 326 400 B, **mean blob 2 046 393 B (1.951 MiB)**; 3 blob sizes.

## 1. Miss economics — disk bytes per token

- A miss is one `read_exact` of the **whole blob**: `PoolExpertStore::acquire` builds one callback of
  `bytes = layer blob` (pool_expert_store.cpp:191-198); the adapter advertises **one** part (pool_expert_source_adapter.cpp:26).
- **The reader is O_DIRECT and chunks the blob through a 4 KiB scratch.** `open(O_RDONLY|O_DIRECT)` (bounded_direct_reader.cpp:51);
  the store admits exactly `new_reader.alignment()`**=4096 B** of scratch (pool_expert_store.cpp:69-71), and
  `read_exact` caps every `pread` at `io_cap = min(scratch_bytes, …)` (bounded_direct_reader.cpp:135, 143-151) →
  **~500 synchronous 4 KiB O_DIRECT preads + 500 memcpys per 1.95 MiB miss**. No readahead, no `fadvise`, no page cache.
- One read = one range. Pool holds 36 GiB → **18 889 of 24 576 pairs (76.9 %)**; leg B 40 GiB → 20 987 (85.4 %).
  Leg A VRAM hit 84.3 % (short) / 93.5 % (long#2) (**MEAS**) → **75 / 31 host refills per token**; at 23.1 % host-miss
  that is **17.4 / 7.2 blobs per token = 34 / 14 MiB per token** of disk (HYP, uniform-miss assumption).
- **MEAS cross-check:** 15.12 tok/s = 66 ms/token with 14 MiB/token ⇒ 212 MB/s effective, i.e. ~9 ms/blob
  (500 preads ⇒ ~18 µs/pread); the 2 tok/s tokens (34 MiB/token, 500 ms) ⇒ 68 MB/s, ~29 ms/blob (~58 µs/pread).
  Both are consistent with 4 KiB QD1 preads on **this** pack disk: `/mnt/srcds/ai` is `nvme0n1p2`,
  **99 % full, 9.8 GiB free, WDC SN530 (DRAM-less)** — a single coalesced 2 MiB `pread` would be ~1-2 ms (**HYP**).
  Verdict: **2-15 tok/s is explained by the miss path at 4 KiB granularity, not by the miss rate.**
- Legacy (arena) leg for comparison: the whole 46.84 GiB would have to be resident (impossible here) or it streams
  through `FileExpertSource`, which batches pairs of 64 (`prefetch_pairs`, generate.cpp:9415) and only uses O_DIRECT
  when `src.unbuffered()` — and **none of that machinery is reachable in managed mode**: every seam is gated on
  `srcp == &src` and `srcp` is the adapter (generate.cpp:8593, 9418, 10246).

## 2. Where the residency is wasted

- There is **no admission control and no slot table**. A miss calls `pool_alloc(bytes, page, …)` for a fresh range
  (pool_expert_cache.cpp:108); eviction happens **only** on `errc::no_buffer_space`, one LRU tail entry per retry
  (109-113, `ram_pool.cpp:306`). So the pool fills with *whatever order misses arrive in* — there is no "keep the
  payroll: 100 % hot" policy, and the hot set is not protected: a token's misses evict the coldest entries, LRU only.
- Two silent capacity reductions: (a) `PoolExpertCache::release` drops only the cache's reference, so a still-borrowed
  range stays allocated but un-reusable (`pool_expert_cache.cpp:156-165`); (b) the **boot profile fill borrows and never
  releases** — `blob_stable` at generate.cpp:9448 leaves a borrower with `completed=false`, which
  `sweep_completed_locked` never drops (pool_expert_source_adapter.cpp:102-118) → the profiled pairs stay pinned in the
  pool for the whole run (benign, but untracked and uncounted).
- **"no free slot" and "over budget" are not the pool.** They are `ForesightSwap`, the *device* swap space:
  `if (best < 0) { ++busy; return; }` (expert_source.cpp:7600 → the 40 730 "no free slot") and
  `if (card_used[c] >= budget) { ++over_budget; return; }` (7579 → 6 993), with `slots = min(STRATA_FS_SLOTS, 64)`
  per layer and `depth` 4 (7642-7649). Busy means: every slot of that layer is loading or read within `depth` windows,
  i.e. **the device swap space duplicates the VRAM expert cache and mostly cannot place anything** (837 hits in a leg).
- The managed pool emits **no counters at all**: `PoolExpertCache::stats()` (pool_expert_cache.cpp:175) has **zero
  callers** in the engine, so host-pool hits/misses/evictions and resident bytes were never observable (**SRC**+grep).

## 3. Heat/profile use

- `--expert-profile` (24 576 ranked pairs) reaches the pool **only by side effect**: it orders the *VRAM* slot fill
  (`xcache.admit`, generate.cpp:9443) and those reads happen through the adapter, so the top `xcache.slots()` pairs are
  incidentally read into the pool first. `--expert-cache-cpu-order` sets `drive.d.hit_cpu_order` (generate.cpp:10230),
  consumed at expert_source.cpp:5567 — a VRAM-cache ordering hint, **not** pool admission.
- No ranking is passed to `PoolExpertStore`/`PoolExpertCache` (no priority argument anywhere in their signatures).
- Cheapest correct fix: **pre-load the top-N ranked pairs into the pool at boot** in rank order, N = pool_bytes/mean_blob
  minus slack, reusing the same read; then LRU retains them because demand fits under them. Cost = one boot pass over
  `(N − vram_slots) × 1.951 MiB` extra reads (pool bytes are already charged); resident-pin sets are unnecessary and
  admission priority is a bigger change for the same effect.

## 4. Page-cache competition

- The managed reader opens its **own** O_DIRECT fd (bounded_direct_reader.cpp:51) — the legacy fd's
  `POSIX_FADV_RANDOM` (expert_source.cpp:1184) and the arena's `fadvise(WILLNEED)` never apply; `io_prefetch()` is
  unreachable in managed mode (generate.cpp:10264). So **(a) every miss is a real disk read; the ~8 GiB of free RAM is
  invisible to it.**
- (b) buffered refill would only win the ~8 GiB/46.84 GiB ≈ **17 %** of misses that happen to be cached, at the cost of
  a second copy and of evicting the rest of the 8 GiB — and that page cache is *not* mlocked, so it is exactly what the
  observed swap growth eats. **Verdict: keep O_DIRECT; fix the read size (§6.1) instead of the cache. A buffered tier is
  only worth revisiting if the pool is later shrunk to ~24-28 GiB to leave 12 GiB of page cache.**

## 5. Swap / pressure

- Pinned: pool (`mlock`, ram_pool.cpp:240, after a write-to-every-page pre-fault, ram_pool.cpp:236-247); host KV
  (`cudaHostAlloc`/`HostRegister`, layer.cpp:585, 968-977); dflash pinned staging (dflash_runtime.cpp:655).
- Pageable: conversation cache and checkpoints (host heap, generate.cpp:2533), allocator/JIT/thread stacks, and the
  page cache. The 36-40 GiB pre-fault is the pressure that pushes them out — **the swap growth is a boot-time
  side effect of the pool, not a steady-state leak** (**HYP**; nothing sampled the split). It cannot be the per-token
  cause: 4-5.5 GiB of swap-in is a one-time few seconds, and the measured decode cost scales
  with routed lookups, not with a paging event. It *does* explain the **prefill** collapse (leg B 984.8 ms/token vs
  76.2 in leg A long#2, **MEAS**) and part of leg B being slower on every request.

## 6. Improvement shortlist (effect per risk)

| # | Change | Files | Confirming measurement |
|---|---|---|---|
| 1 | **One `pread` per blob**: scratch = max layer blob (+pad), keep O_DIRECT | pool_expert_store.cpp:67-72,164 (use `expert_layout().max_blob_bytes()`-sized scratch, ~2.6 MiB of pool); reader already accepts it (bounded_direct_reader.cpp:134-151) | boot line `pre-filled N of M slots … MB/s` (same 24 576 reads) before/after; then one window leg: tok/s + `MemAvailable` |
| 2 | **Stop reading disk under `ForesightSwap::smu_`**: place only pairs the pool already holds (`src->blob` at expert_source.cpp:7601 runs with `smu_` held, called from the decode miss path at 5119 and the lookahead at 3397) | pool_expert_source_adapter.hpp:77 (`pinned()` lies — says true unconditionally), add `PoolExpertCache::contains()/resident()`, use it at expert_source.cpp:7601 | `foresight swap: … no free slot / over budget` deltas + decode tok/s, same leg |
| 3 | **Observability + lookup cost**: hash index instead of the linear LRU scan (18 889 entries per acquire, pool_expert_cache.cpp:72-84) and report `PoolExpertCache::stats()` + resident bytes at request end | pool_expert_cache.cpp/.hpp, generate.cpp request-end logging (next to 23832) | the new line itself; tok/s unchanged expected (small CPU win) |

**Budget:** full residency needs pool ≥ 46.84 GiB, i.e. `STRATA_RAM_BUDGET_MIB` ≥ ~52 060 MiB out of 62 GiB total
(pool = budget − outside_owned 4 GiB, ram_pool.cpp:122-141; the boot lines match exactly) — **not viable**: leg A at
36 GiB pool already left 8.5 GiB MemAvailable min 8 157-8 529 MiB and 1.4→5.5 GiB swap. The evidence supports
**≤ 45 056 MiB (leg B) and no more**; 40 960 MiB (leg A) is the sane default. Marginal value is ~0.17 pp of the pair
space per GiB, i.e. real but far below the 5-15× on the miss path from #1 — **fix #1 before touching the budget.**

**UNMEASURED:** existing profile-fill MB/s (needs one boot); host-pool hit/miss/eviction counts (no counter exists);
per-blob disk cost before/after #1; whether long#1's 120 s stall was #1 cost or VRAM-low; any concurrency leg; the
swap pageable/pinned split; the buffered-vs-O_DIRECT comparison on this drive.