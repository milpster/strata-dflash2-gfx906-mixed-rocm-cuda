# Managed RAM Pool Design — Strata serving CPU budget (revised, attempt 2)

**Gate output.** Architecture/risk gate only. No code edits, no builds, no production boots. Written to the only in-scope path:
`/mnt/srcds/Strata/bench/ab-v0210/notes/managed-ram-pool-design-20261120.md`.

Source tree inspected: worktree `/mnt/srcds/Strata-wt-helper-ipc`, branch `helper-ipc-diet`, HEAD `bde983cf41`.
Config/launcher read from main tree `/mnt/srcds/Strata` (branch `tg-stall-cause`, dirty harness artifacts — not altered). Production is STOPPED
by operator order; this report does not depend on `:8009`, does not launch agents, does not touch launcher/config/state/pins/model files.

---

## Correction / decision (supersedes attempt 1)

**Attempt 1 is rejected.** It proposed locking the entire 46.84 GiB file arena *plus* a separate anonymous pool on a 52 GiB whole-stack budget —
that locks close to two budgets and does not make expert storage elastic. It also cited `release_pair` as ready rebalance for the mapped store,
but `release_pair` explicitly refuses `map_ != nullptr || shared_backing_` (`expert_source.cpp:7081`) and returns pages to the kernel via
`MADV_DONTNEED`, not to a persistent locked reusable pool. Finally, it assumed KV lives in VRAM, but prod config explicitly sets
`--kv-resident 262144` + `--kv-resident-mib 4096`, whose allocation path (`layer.cpp:585-614`, `:960-1004`) pins **host memory**.

**Corrected architecture (managed mode only):** The read-only file arena (`experts.bin`) becomes an **unlocked backing source** — its pages are
faulted in on demand and are reclaimable; it is never fully mlocked. A single bounded mlocked **writable anonymous pool** is the **primary resident
expert cache** AND carries reusable serving-buffer chunks (KV host staging, conversation host state, prompt staging). Experts are streamed into
the pool one blob/chunk at a time from the immutable backing source via existing `copy_blobs`/`read_direct` (O_DIRECT) paths — no full-arena load,
no full-arena lock, no double residency. The source file is never rewritten; no new GPU numerical route.

This revises sections 1–6 below; section 7 lists exact validation evidence.

---

## 1. Source-backed allocation / consumer inventory

All sizes from `prod-config-arena54.json` / `launch-prod-arena54.sh`; no invented slack. KV ownership is resolved from source, not assumed.

| # | Owner | Size / calc | Lifetime | Poolable? | CPU/GPU/pinning | Source boundary |
|---|---|---|---|---|---|---|
| 1 | Expert arena backing (`ArenaExpertSource`, `experts.bin`) | 46.84 GiB = 50,294,988,800 B (`lay.total`, `expert_source.cpp:5964`) | Process | **No** (backing store, not pool member) | CPU file-backed mmap, **unlocked in managed mode**; pages faulted on demand, reclaimable | `expert_source.cpp:5952-6044` |
| 2 | Expert **resident cache** (pool sub-partition) | ≤ pool_bytes share, bounded | Dynamic | **Yes** (primary pool member) | CPU anonymous, mlocked, writable | NEW path: stream blobs into pool via `copy_blobs`/`read_direct` (`expert_source.cpp:7328-7345`, `copy_blob` at :7308-7318) |
| 3 | KV resident host copy | `--kv-resident 262144` cells/layer, capped by `--kv-resident-mib 4096`; `per_cell = qsa_kv_bytes_per_cell(g)` (`layer.cpp:802-807`), sized at `generate.cpp:5489-5503`; allocated via `kv_host_alloc` (`layer.cpp:585-614`) → `cudaHostAllocMapped\|Portable` (:588) or `cudaHostRegister` on `mmap(MAP_ANONYMOUS)` (:600-613); tracked in `g_kv_host_bytes` (:798, :977) | Process/session | **Yes** (host copy is pinned host RAM) | CPU pinned host memory, device-mapped alias (`cudaHostGetDevicePointer`, :969); authoritative host copy ("the authoritative K/V of every cell, pinned and device-mapped", :961) | `layer.cpp:960-1004` |
| 4 | KV device pool (VRAM slots) | `kv_pool_bytes(s, p.slots, ...)` (`layer.cpp:769-777`) | Session | **No** (VRAM wall, separate & unchanged) | GPU VRAM | `layer.cpp:769-787`, `generate.cpp:6893-6899` |
| 5 | Conversation cache (parked slots) | `--conversation-cache-mib 1024` → `ConversationCache(1024*1024*1024, slots, min_tokens)` | Process | **Yes** | CPU RAM | `generate.cpp:13309-13311` |
| 6 | Conversation RAM ceiling | `--conversation-cache-ram-mib 256` → parked snapshots + retained K/V ceiling (print only at :13314-13319; enforcement path separate) | Process | **Yes** | CPU RAM | `generate.cpp:13314-13319` |
| 7 | PLE table lock | `ple_table.table_bytes() = n_rows × row_bytes` (`ngram.cpp:554`); mlocked via `ngram.cpp:429-462`; `give_back_ram` unlocks it (`generate.cpp:8533-8540`) | Process | **No** (separate locked allocation, can be given back) | CPU RAM, mlocked, RLIMIT_MEMLOCK | `ngram.cpp:429-483`, `generate.cpp:6010-6145` |
| 8 | Prompt / serving staging | Prompt buffers, chunk rows, API bodies (256 MiB max `v0215/serve/server.py:7679`) | Per-request | **Yes** | CPU RAM | `prefill/prefill.cpp`, `generate.cpp:12282-12313` |
| 9 | Expert-helper pinned activation buffer | `kMaxSelTok × n_embd × float`, `mmap + mlock` (`expert_helper_socket.cpp:602-612`) | Helper process | **No** (separate cgroup, unless merged) | Helper process pinned host | `expert_helper_socket.cpp:592-617` |
| 10 | server.py (Python) | Python interpreter + API state (unmeasured here) | Process | **No** | CPU RAM | `v0215/serve/server.py` |
| 11 | Vision process | `strata-vision` (CPU, `vision.gpu=false` in prod config) | Process | **No** | CPU RAM | `v0215/serve/server.py:3448` |
| 12 | ROCm pinned reserves | `pin_reserve_take` soft+hard (`pin_budget.hpp:338-368`) | Around arena pin | **No** (reserve, released after) | CPU pinned (cudaHostAlloc) | `pin_budget.hpp:338-368` |
| 13 | Page cache / file cache | Reclaimable `inactive_file` credit (`conversation_memory.cpp:80-108`) | Dynamic | **No** (reclaimable) | CPU RAM reclaimable | `conversation_memory.cpp:83-108` |

### KV resolved (source)
- `--kv-resident 262144` (prod config line 38) sets `g_kv_resident` cells per QSA layer (`generate.cpp:4843`, `layer.cpp:781`).
- `--kv-resident-mib 4096` (prod config line 69) caps total resident KV bytes (`generate.cpp:5489-5503`), floored at `qsa_kv_resident_min()` (4096 cells, `layer.cpp:786-797`).
- `kv_resident = 0` default = all VRAM (`generate.cpp:687`, `layer.cpp:755`); non-zero = streaming mode with a **pinned host copy** (`layer.cpp:961-1004`).
- Therefore KV residency IS a CPU pinned-host consumer (~4096 MiB budget), and belongs in the pool's accounting as a sub-partition candidate.

### Conversation cache distinction (source)
- `conversation_cache_mib` (1024 MiB prod) constructs `ConversationCache` — parked conversation slot bytes (`generate.cpp:13309-13311`).
- `conversation_cache_ram_mib` (256 MiB prod) is a **separate RAM ceiling** for parked snapshots + retained K/V (`generate.cpp:13314-13319`).
- They are NOT synonymous and are NOT preallocated pool bytes; both are configuration ceilings the pool must respect.

### Mapped-store refusal hazard (source)
- `release_pair` (`expert_source.cpp:7076-7081`) returns `false` when `map_ != nullptr || shared_backing_` — it cannot return chunks of the current
  mapped/shared-mmap arena to a persistent locked pool. Its following code does `MADV_DONTNEED` + re-register remainders with fresh GPU aliases
  (:7090-7119), not pool return. A real pool-backed expert-source path must be designed (section 3).

---

## 2. Single total-budget accounting equation

One knob: `STRATA_RAM_POOL_MIB` (MiB) = the **whole Strata CPU serving world**. Default unset → byte-identical today.

```
TOTAL_BUDGET (e.g. 52 GiB = 54525952 KiB)
  = POOL_BYTES + OUTSIDE_OWNERS + CGROUP_SLOP

POOL_BYTES = TOTAL_BUDGET − OUTSIDE_OWNERS − CGROUP_SLOP
```

**OUTSIDE_OWNERS** (charged in the same cgroup, measured at boot, not magic flags):
- server.py Python interpreter + API state
- vision process (`strata-vision`, CPU)
- expert helper (if merged into scope; see section 4 topology)
- PLE table lock (`ngram.cpp:554`, unlockable via `give_back_ram` `generate.cpp:8533-8540`)
- ROCm pinned reserves (`pin_budget.hpp:338-368`, released after arena `:8558`)
- Third-party/driver allocations the engine cannot relocate into the pool (CUDA context, driver mappings, ROCml runtime)

**CGROUP_SLOP**: kernel page-cache pressure headroom the engine does not control; bounded by `MemorySwapMax` and cgroup reclaim behavior. Not a user flag.

**POOL_BYTES** is sub-allocated (mlocked writable anonymous pool) to:
1. Expert resident cache (primary, evictable cold copies)
2. KV host staging (pinned host copy redirected into pool)
3. Conversation host state (parked snapshots + retained K/V, within `conversation_cache_ram_mib` ceiling)
4. Serving/prompt staging (transient, per-request)

**Startup sequence** (engine enters scope first, then allocates):
1. Process enters the cgroup scope whose `memory.max` = TOTAL_BUDGET (launcher side).
2. Engine measures OUTSIDE_OWNERS (existing `sample_host_memory()` + cgroup scan, `conversation_memory.cpp:261-281`) and computes POOL_BYTES.
3. **Explicit refusal** if `POOL_BYTES < min_viable_pool` (expert cache min + KV host staging min + conversation min) — no silent fallback, no unbounded spill.
4. Preallocate + pre-fault + `mlock` one anonymous pool of POOL_BYTES (bounded 256 MiB steps, `native_mlock_span` pattern `pin_budget.hpp:240-310`; RLIMIT_MEMLOCK raised via `raise_limit_once()` `:192-215`).
5. Sub-allocate pool partitions; read-only arena stays **unlocked** backing (pages faulted on demand, reclaimable).
6. Experts streamed into pool on demand via `copy_blobs`/`read_direct` (O_DIRECT) into pool buffers — no full-arena load/lock (`expert_source.cpp:7328-7345`, `copy_blob` `:7308-7318`, `read_direct` `:1798+`).

**mmAP backing is NOT automatically resident.** The file is mapped `PROT_READ, MAP_SHARED` (`expert_source.cpp:6010`) but pages enter RAM only on fault
(`expert_source.cpp:5991-5994` comment: "the page cache keeps what the CPU pool and the prompt path actually read ... and gives back the rest under
memory pressure; a page dropped is re-read from the file"). Temporary residency = working-set of touched pages only, bounded by pool eviction — not
all-at-once 46.84 GiB.

---

## 3. Preferred design + simpler valid alternative

### 3A. Preferred: single mlocked writable pool (managed mode)

**Allocator invariant:** `pool_alloc(tag, size) → handle/offset`; `pool_free(handle)`; `pool_rebalance(tag)` evicts coldest by LRU/heat. Pool bytes
≤ `TOTAL_BUDGET − OUTSIDE_OWNERS − CGROUP_SLOP` at all times. Mlocked range never shrinks below live allocations.

**Expert-source path (real, pool-backed):** A new `PoolExpertSource` (or mode on `ArenaExpertSource`) that:
- Does NOT `mlock` the full 46.84 GiB arena; the file mapping stays unlocked backing.
- On a cache miss, streams one blob/chunk directly into a pool buffer from the immutable backing source via `FileExpertSource::copy_blob(dst)` /
  `copy_blobs(layers, experts, dsts, n)` (`expert_source.cpp:7308-7345`) which uses `read_direct`/O_DIRECT preads into caller buffers
  (`expert_source.cpp:1798-1865`).
- Evicts cold expert cache entries (LRU/heat from `--expert-profile`) via `release_pair`-equivalent that **returns pool chunks to the free list**
  (not `MADV_DONTNEED` of the mapped arena). Stable ownership: pool returns offset handles, callers never hold raw pointers across rebalance.
- GPU lifetime: pool buffers used for DMA staging are `cudaHostRegister`/device-mapped through the existing pin budget (`pin_budget.hpp:85-117`);
  async fences tracked per transfer; a chunk is only returned to the free list after its transfer fence completes.

**Consumer:** KV host pools (`kv_host_alloc`, `layer.cpp:585-614`) redirected to allocate from the pool instead of standalone `cudaHostAlloc`/`mmap+register`.
This is a real host serving consumer already in the critical path (streamed KV for `--kv-resident`).

**Eviction/pressure:** On memory pressure (cgroup `memory.max` approached), the controller evicts cold expert cache entries and conversation snapshots
(park-to-disk via existing disk tier `generate.cpp:13461-13468`), returns their pool chunks. Pool stays mlocked; page cache is reclaimed instead.

### 3B. Simpler valid alternative (honestly does NOT meet reusable-pool preference)

**Independently budgeted partial file lock with runtime lock adjustment.** At startup, `mlock` only a *subset* of the arena (hottest experts by
`--expert-profile`) sized from a sub-budget; the rest of the arena stays unlocked file-backed. Runtime lock adjustment grows/shrinks the locked span
within its sub-budget as heat changes.

- **Does NOT meet the goal:** no single writable pool, no shared storage with KV/conversation/staging, no reusable serving-buffer chunks, no
  sub-allocator, no cross-consumer rebalance. It is a partial-lock budgeting scheme.
- **Honest about complexity:** simpler than 3A (no pool allocator, no pointer-stability/fence rules, no cross-partition eviction) but does not satisfy
  "everything fits in one pool with dynamic allocation inside it." Offered only as a fallback if 3A cannot be bounded in stage 1.

---

## 4. Whole-stack kernel scope / process topology

The user defined WHOLE Strata memory. Helper scope membership cannot be "unless operator merges it." Topology plan (no launcher changes now):

| Scope member | Process | Today (launch-prod-arena54.sh) | Managed-mode requirement |
|---|---|---|---|
| server | `server.py` | `systemd-run --user --scope -p MemoryMin=53G -p MemoryMax=56G -p MemorySwapMax=4G` (:119-127) | In scope; `memory.max` = TOTAL_BUDGET |
| engine | `strata` `--serve` | Child of server (:1302 `popen`) | In scope; allocates pool AFTER entering scope |
| vision | `strata-vision` (CPU) | Child of server (:3448 `popen`) | In scope |
| expert helper | `strata-expert-helper-patient` | Separate `env -i`, **no systemd scope** (:91-100) | **Must be brought into the same scope** (launcher change, out of scope for this gate) or excluded explicitly with its own budget subtracted from TOTAL_BUDGET |

**Allocation/charge ordering:** The pool is allocated and charged only **after** engine+server+vision are in the cgroup scope whose `memory.max` =
TOTAL_BUDGET. `STRATA_MEMORY_LIMIT_MIB` (`generate.cpp:4517-4530`) and `sample_host_memory()` (`conversation_memory.cpp:261-281`) feed the same explicit
limit — one source of truth; no manual coordination of four env flags.

**Reserve consumption & refusal:** Third-party/driver allocations (CUDA context, driver mappings, ROCml runtime) and file-cache pressure consume
OUTSIDE_OWNERS/CGROUP_SLOP. If `TOTAL_BUDGET < OUTSIDE_OWNERS + min_viable_pool`, the engine **explicitly refuses** at startup (exit code, plain
error line) — no unbounded spill, no silent fallback. Bounded guarantees (mlocked pool size, refusal floor) are distinguished from things no allocator
controls (kernel page-cache reclaim, driver residency, other processes outside the cgroup).

---

## 5. Staged implementation plan (helper-ipc-diet, disjoint sequential file scopes)

Default path byte-identical unless opted in. Implementation lands serially in the fixer lane; each commit builds on the last. No GPU/boot/benchmark in
this gate.

| Stage | Scope (files) | Work | Exit criterion |
|---|---|---|---|
| **S1 (first pass — honest usable slice)** | `include/strata/core/ram_pool.hpp/.cpp` (new), `src/core/expert_source.cpp` (new `PoolExpertSource` mode or pool-backed `refill`/`release`), `src/core/layer.cpp` (`kv_host_alloc` → pool-backed), `src/program/generate.cpp` (budget knob, refusal, pool wiring) | Opt-in `STRATA_RAM_POOL_MIB`; compute `POOL_BYTES = TOTAL − OUTSIDE − SLOP`; explicit refusal below floor; prealloc/pre-fault/mlock one anonymous pool; **PoolExpertSource** streams one blob/chunk into pool from immutable backing via `copy_blobs`/`read_direct` (no full-arena load/lock); **KV host pools redirected into pool** (real consumer); pool free-list + LRU eviction of cold expert entries; `munlock`/`munmap` teardown. | Opt-in run starts, serves, evicts cold expert entries and reuses pool space under pressure; default unset byte-identical (future validation goal) |
| S2 | `src/core/conversation_memory.cpp` + `generate.cpp` | Conversation host state + parked snapshots allocated from pool within `conversation_cache_ram_mib` ceiling; publish `pool_used/pool_free/pool_locked` accounting (reuse cgroup scan + `rss_probe` `generate.cpp:522-534`) | Accounting line at start; park-to-disk stays within pool |
| S3 | `src/program/generate.cpp` + launcher (parent-owned) | Wire `STRATA_MEMORY_LIMIT_MIB` → cgroup `memory.max` = TOTAL_BUDGET; helper brought into scope (launcher change, parent-owned) | `memory.max` matches budget; helper charged in same scope |
| S4 (follow-up) | `src/core/expert_source.cpp`, `generate.cpp` | Full single-pool: experts as primary store in writable pool, read-only mmap as read-only backing/fallback; bounded startup copy-on-warmup | Declared out of S1; double-residency bounded |

**S1 interfaces:**
- `ram_pool_bytes(total_mib, outside_owners_mib) → pool_bytes`; `pool_refusal(total_mib, needed_mib) → error`
- `pool_alloc(tag, size) → handle`; `pool_free(handle)`; `pool_rebalance(tag)`
- `PoolExpertSource::fill_blob(layer, expert, pool_handle)` / `release_to_pool(handle)` — stable offset handles, fence-aware return
- `kv_host_alloc_pool(bytes)` — redirects `layer.cpp:585` into pool

**S1 source-confirmed candidate consumer:** KV host pools (`kv_host_alloc`, `layer.cpp:585-614`, consumed by `prefill.cpp:4585/8353`, `verify.cpp:2033/2054`)
— already pinned host memory in the serving critical path.

**What remains outside the pool (S1):** VRAM KV device pool (`layer.cpp:769-777`), expert-helper pinned buffer (separate process/cgroup), server.py Python,
vision, PLE table lock (separate, unlockable), ROCm pinned reserves, page cache, file-cache pressure, driver overhead.

**Claims S1 does NOT establish:** full single-pool expert store as primary (double-residency unbounded — follow-up S4), helper cgroup merge (launcher,
parent-owned), pressure-depth speed claims, any fix for the recorded ROCm hang (`logs/prod-startup-20261120.bt`).

---

## 6. Verification path (before fixer implementation; no GPU boots now)

**Validation is planning + CPU-only evidence; no production boot.**

### Claims/invariants
- C1: Pool bytes = TOTAL − OUTSIDE − SLOP; explicit refusal below floor (no silent spill).
- C2: Pool is mlocked; pages charged to engine cgroup; count against `memory.max`; mlocked pages do not swap.
- C3: One blob/chunk streamed into pool from immutable backing via `copy_blobs`/`read_direct` — no full-arena load, no full-arena lock, source never rewritten.
- C4: Cold expert entry evicted → pool chunk returned to free list → reused by a later fill; no mapped-arena page written as KV.
- C5: KV host allocation redirected into pool; streamed KV still correct (numerical route unchanged — same `KvHostPools` layout, only allocator backing changes).
- C6: Teardown `munlock`+`munmap` releases all pool pages; restart re-allocates cleanly.

### Deterministic CPU tests + fault injection (no GPU, safe limits)
- `native_mlock_test`-style `ram_pool` scenario: allocate/lock a pool of N MiB under `RLIMIT_MEMLOCK` = N; assert complete; then lower RLIMIT →
  refusal path exercised; `munlock` → pages released. Build target already exists: `build-cpu-native-mlock` (`CMakeLists.txt:519-533`).
- `RLIMIT_MEMLOCK` exhaustion: `setrlimit` below pool size → `pool_alloc` returns explicit error, no partial pool left locked.
- `STRATA_TEST_PIN_BUDGET_MIB` (`pin_budget.hpp:60-62`) simulates pin-ceiling exhaustion; assert `alloc_locked`/`pin_register` return
  `cudaErrorMemoryAllocation` and `host_alloc_guarded` (`pin_budget.hpp:411-428`) retries once.

### Correct paths (fixing attempt 1 errors)
- `ram_raw.py` is at `bench/ab-v0210/rpcprof/gpu-rounds/ram_raw.py` (NOT `bench/ab-v0210/rpcprof/ram_raw.py`); CPU test:
  `bench/ab-v0210/rpcprof/tests/test_ram_raw_cpu.py`.
- Existing `native_mlock_test` 11 scenarios / 136 checks passed (`bench/ab-v0210/rpcprof/out/native-mlock-arena-mode-20261120/ctest-run.status`) —
  these are **pre-feature evidence only**, not proof of default byte identity for the new feature (future validation goal).

### Default-path regression
- `ctest --test-dir build-cpu-native-mlock -R native_mlock` must remain 100% (11/11) with knob unset.
- Engine start with `STRATA_RAM_POOL_MIB` unset → no new syscalls, no pool allocated, byte-identical behavior (to be verified by S1 implementation).

### Later backend build requirements
- Any touched HIP/CUDA path builds against **ROCm 6.1.0 userspace, never `/opt/rocm` 7.x** (box gfx906 fact: `libhipblas.so.2`, `libamdhip64.so.6`
  → `/opt/rocm-6.1.0/lib`; `librocblas.so.4` → `/home/srcds/rocm-gfx906-xnack/lib`; `HSA_OVERRIDE_GFX_VERSION=9.0.6`). CUDA/HIP/SYCL builds checked
  per touched file.
- Separate operator-authorized GPU validation plan: memory metrics (`max_vram_mib`, `max_rss_mib`, cgroup `memory.current/peak`) beside speed; tps
  comparable only on same-day, same-driver-protocol legs; never report speed apart from memory cost. NO validation boots now.

---

## 7. Exact commands run and statuses (this session; read-only diagnostics)

| Command | Purpose | Status |
|---|---|---|
| `git status; git branch -a` (main tree) | Confirm main tree = `tg-stall-cause`, dirty harness artifacts | Done (tool success) |
| `git status; git log --oneline -3; git branch --show-current` (worktree) | Confirm worktree = `helper-ipc-diet` @ `bde983cf41` | Done (tool success) |
| read `prod-config-arena54.json` | Confirm `--kv-resident 262144`, `--kv-resident-mib 4096`, `MemoryMax=56G` | Done (tool success) |
| read `launch-prod-arena54.sh` | Confirm helper outside systemd scope (:91-100), server in scope (:119-127) | Done (tool success) |
| grep `mlock\|native_mlock\|ARENA_MMAP` across `src/` | Locate locking/allocation boundaries | Done (tool success) |
| read `pin_budget.hpp` (full), `platform/memory.cpp` | `native_mlock_span` (:240-310), `raise_limit_once` (:192-215), `pin_reserve_take` (:338-368), `mlock` impl (:164-172) | Done (tool success) |
| read `expert_source.cpp` :5952-6044, :6942-7119, :7308-7345 | Arena open/mlock; `release_pair` refuses mapped store (:7081); `refill_pair`/`copy_blobs`/`copy_blob`/`read_direct` streaming paths | Done (tool success) |
| read `layer.cpp` :585-614, :755-807, :960-1004 | `kv_host_alloc` = `cudaHostAllocMapped\|Portable` or `cudaHostRegister` on mmap anon; KV host bytes tracked (`g_kv_host_bytes`); `qsa_kv_bytes_per_cell`; `--kv-resident-mib` sizing | Done (tool success) |
| read `generate.cpp` :4517-4530, :522-534, :13309-13319, :8533-8540 | Memory-limit knob; `rss_probe`; conversation cache vs RAM ceiling distinction; PLE `give_back_ram` | Done (tool success) |
| read `pair_supervisor.py` :107-139 | Subreaper + descendant tracking for engine+helper topology | Done (tool success) |
| `stat /mnt/srcds/ai/iq3xxs-ud/experts.bin` | Confirm arena file size 50,294,988,800 B | Done (tool success) |

### Changed-file list
Only the report file: `bench/ab-v0210/notes/managed-ram-pool-design-20261120.md` (this document). No code edits, no builds, no git mutations,
no `/tmp` artifacts, no new benchmark harnesses. Pre-existing dirty main-tree files (`ram_raw.py`, `supervisor.json`, etc.) were not touched.

### Unverified claims / limits
- OUTSIDE_OWNERS component sizes (server.py Python, vision, helper, PLE table, ROCm reserves) not measured here — must be sampled to size `POOL_BYTES`.
- `kMaxSelTok`, `n_embd` for helper buffer size; not needed for S1 (helper out of pool scope).
- Whether helper should be in the same cgroup (launcher decision, parent-owned; gate specifies it must be accounted).
- The exit -6 (SIGABRT) on big cold requests is undiagnosed (journal 2026-11-20) — not claimed fixed by this design.
- No production boot/benchmark performed; all speed/pressure claims from journal are operator measurements, not re-derived here.
- Default byte-identity is a **future validation goal**, not evidence from old tests.

### Next bounded fixer task
Implement **Stage S1** as a single usable slice: `include/strata/core/ram_pool.hpp/.cpp` (budget arithmetic, refusal, mlocked pool allocator),
`PoolExpertSource` in `src/core/expert_source.cpp` (stream one blob/chunk into pool from immutable backing, LRU eviction returning chunks to free list),
`kv_host_alloc` redirected into pool in `src/core/layer.cpp`, budget knob + refusal + pool wiring in `src/program/generate.cpp`. Default unset =
byte-identical. After S1 review, proceed to S2 (conversation + accounting) serially.

---

**Author:** Oracle (technical advisor). Report only — no implementation performed in this gate.

## Parent integration decision — authoritative implementation boundary

The preferred direction is accepted, not every integration claim above.
Parent source checks confirmed `kv_host_alloc` and its device-mapped consumer
in `layer.cpp:585-620,961-995`. These additional conditions supersede stale
stage descriptions in sections 5 and 7:

1. The expert pool is the primary resident store in the first completed
   integration pass. There is no later S4 that repeats that same migration.
   The full file lock must be absent in managed mode; immutable file backing
   is not another resident arena. No whole-stack guarantee is claimed yet.
2. Helper membership in the total-budget scope is mandatory before that
   guarantee. Source-accounted runtime reserve and admission/failure behavior
   must be implemented, not inferred from one free-memory sample.
3. `expert_source.cpp:7308-7345` defines ArenaExpertSource forwarding, not
   FileExpertSource's direct reader. FileExpertSource's actual batch/copy
   paths at :1798-1878 include buffered/mapped fallbacks. A bounded managed
   reader must explicitly handle or reject those fallbacks and account for
   their transient pages; pool eviction alone does not bound page cache.
4. Returning an allocation to a still-locked pool frees internal capacity,
   not physical RAM or cgroup charge. Internal rebalancing must not be sold
   as a cure for outside-pool growth. Controller admission/headroom policy
   remains an integration gate; unsupported demand must not silently spill.
5. Existing raw-pointer expert APIs need retained ownership through actual
   consumer quiescence/fences. Changing allocator backing is not sufficient
   to make those pointers safe. Whole-pool HIP registration is not required
   and is not approved; mlock and device mapping remain separate contracts.

The serial implementation lanes for the first pass are:
- L1: CPU-only locked anonymous pool, checked arithmetic, coalescing allocator,
  shared allocation leases and lock/lifetime/failure tests. This is a
  foundation, not a serving feature; no env/CLI or GPU dependency yet.
- L2: source-compatible pool-primary expert cache with immutable bounded
  reads, explicit ownership and real cache-to-serving storage reuse tests.
- L3: bind host-KV and engine setup/accounting to the pool through one total
  budget source, preserving mapped-device aliases and inactive default paths.
- L4: whole-stack scope/admission integration and final CPU/backend checks;
  only then seek a separately authorized hardware validation window using
  the existing harness. Production remains stopped throughout preparation.

Each lane is reviewed before the next. Runtime integration interfaces and
remaining outside-pool consumers must be resolved before their lane starts.
