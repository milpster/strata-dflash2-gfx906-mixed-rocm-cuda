# Managed RAM pool — L2 implementation boundary (2026-11-20)

One bounded architecture (no menu). L1 Pool/PoolLease (ram_pool.hpp) accepted; not re-reviewed.

## 1. A is viable with existing PoolLease

API (new files include/strata/core/pool_expert_cache.hpp, src/core/pool_expert_cache.cpp):

namespace strata::core {
class PoolExpertCache {
public:
  explicit PoolExpertCache(Pool& pool) noexcept;   // non-owning; pool outlives cache
  ~PoolExpertCache();
  struct View { const uint8_t* data; uint64_t bytes; };
  // Miss: serialize (one read at a time), pool_alloc exact blob bytes, copy_from_files into it.
  // Hit: promote in LRU, return a lease copy. Failure: invalid lease + err, nothing published.
  std::pair<PoolLease, std::string> acquire(int64_t layer, int64_t expert);
  void release(int64_t layer, int64_t expert);     // drop cache ref only
  void clear();                                     // drop all cache refs
  PoolStats stats() const noexcept;
private:
  Pool& pool_;
  std::mutex mu_;
  // LRU: key(layer,expert) -> PoolLease; bounded by capacity/max_blob
};
}

Contract:
- Entries own a PoolLease; acquisition returns an owning lease/view, never a bare pointer with cache lifetime.
- LRU eviction drops only the cache's ref; live borrower leases keep storage (shared LeaseState keeps the mmap alive).
- pool rejects new allocs only when capacity exhausted, never because a borrower holds a lease.
- Destruction with live borrowers: cache drops its refs; borrower leases keep memory until the last copy dies.
- pool_alloc returns error_code (no exceptions); copy_from_files returns bool; on any failure nothing is published (no partial entry).
- Explicit release/clear enables later non-expert pool allocation; cross-owner reuse proven by test (freed lease re-served to another owner), not madvise of a separate mapping.
- Bounded: max entries = capacity / max_blob; one blob per entry; no thread-local scratch.

## 2. Smallest bounded immutable reader seam

Seam: reuse FileExpertSource::copy_from_files(layer, expert, dst) (src/core/expert_source.cpp:1828), which copies exactly layer_blob_bytes(layer) bytes from layer_offsets_[layer] + expert*per (ExpertLayout::blob_offset, expert_layout.hpp:39-41) into caller memory. File scope: src/core/expert_source.cpp (copy_from_files + layer_offsets_/layer_blob_bytes_ + open) + include/strata/kernels/cpu/expert_layout.hpp. No new format parsing, no fixed geometry, no fake GPU validation.

L2 read = pool_alloc(bytes=layer_blob_bytes(layer), align=page) then copy_from_files into lease.data(). The lease IS the scratch; no second arena, no unlimited thread-local 32MiB scratch.

Honesty (blocked gate): default copy_blobs path is mapped memcpy; copy_from_files falls back to mapped page cache when not unbuffered, so transient page-cache pages exist and are not bounded by the pool. Do NOT claim no-double-store/direct-I/O for L2. O_DIRECT alignment/partial-page/EOF and retained scratch/page cache are out of scope; keep the managed no-double-store gate BLOCKED until unbuffered mode (direct_/pdirect_) is accounted (L3+).

## 3. C — lease-bearing ExpertSource acquisition + one consumer

Add default lease-bearing acquisition to ExpertSource (expert_source.hpp/cpp):
  virtual std::pair<PoolLease, std::string> acquire_blob(int64_t layer, int64_t expert)
    { err="source has no lease-bearing acquisition"; return {}; }   // preserves legacy behavior
PoolExpertSource (new: pool_expert_source.hpp/.cpp) derives ExpertSource, holds a PoolExpertCache, overrides acquire_blob to allocate+copy_from_files, and never makes an unsafe blob_stable lifetime promise.

Bind ONE consumer: the lent-slot refill at generate.cpp:24032-24049 (blob_stable -> fill_slot_blocking/queued_staged -> note_async_read -> sync_queued). Replace with acquire_blob -> retain PoolLease -> fill from lease.data() -> sync_queued -> release lease. Retain every acquired lease until sync_queued quiesces the in-flight copies; on error/early return, sync first then release (no use-after-free of copied bytes).

Managed source is NOT activated globally. Unadapted consumers needing safe lifetime/copy before managed selection (follow-up checklist, actual symbols): generate.cpp:5611 (CPU dispatch fill), :9346/:9522 (verify/nibble blob_stable), :17117 (VRAM refill), :20300; ArenaExpertSource::blob() arena pointers (expert_source.cpp:6727) + note_async_read (cpp:7538); layer.cpp:585 kv_host_alloc (host-KV, L3).

## 4. D — out of L2
Runtime budget creation + host-KV mapped GPU allocation (L3); total scope/admission (L4). No new public pool-size knob; no duplicate full arena; no GPU registration of whole pool; default path unchanged when not opted-in.

## 5. Two serial bounded fixer tasks

Task 1 — pure CPU cache/read/fixture (~5 files, owner: CPU test suite)
Files: include/strata/core/pool_expert_cache.hpp, src/core/pool_expert_cache.cpp, src/kernels/cpu/pool_expert_cache_test.cpp (+ CMakeLists registration).
Verification: byte-equivalence cache-lease vs mapped blob of experts.bin (memcmp); eviction keeps live borrower storage valid; alloc failure returns invalid lease + err with nothing published; clear()/dtor with live borrowers keeps memory until the last lease dies; pool re-serves freed bytes to another owner (cross-owner reuse).

Task 2 — API + selected-consumer binding (~5 files, owner: generate/serve lane)
Files: include/strata/core/expert_source.hpp (default acquire_blob), src/core/expert_source.cpp (default impl), include/strata/core/pool_expert_source.hpp, src/core/pool_expert_source.cpp, src/program/generate.cpp (bind lent-slot refill).
Verification: lease held across queued fill + sync_queued; failure path syncs before release; generate source-compatible when not opted-in.

## Parent implementation clarification (authoritative)

Accept the lease/cache direction, not the illustrative API as written. Task 1
injects a synchronous reader callback receiving key, exact byte count and pool
destination; it cannot call FileExpertSource's private, GPU-linked method from
a CPU-only object. Tests use a small immutable fixture, not the real model.
Production reader binding remains unverified until Task 2. The cache returns
an error_code plus owning lease; a cached key with a different size is an error.
One LRU list is enough initially; no map or metadata bound of capacity/max_blob
(blob sizes vary). Pressure eviction drops cache refs; held borrowers can still
prevent allocation. Failed admission/read publishes no new entry and preserves
the caller's output, but may have evicted cold cache refs. State this explicitly.
Prepare entry metadata before eviction, catch allocation failure, and do not
expose a source whose legacy raw-pointer promises cannot be met. The later
default acquisition adapter must preserve legacy behavior, not return an
unsupported error to the existing default refill path. Failed GPU synchronization
is not proof of quiescence: retain owners through a proven drain or teardown.
Buffered backing and cache-only tests do not complete L2's primary-storage gate;
bounded production reads and remaining real consumers are still required.
