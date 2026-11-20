// include/strata/core/pool_expert_cache.hpp - CPU expert-cache foundation.
//
// A thin LRU cache over the managed pre-locked RAM pool
// (include/strata/core/ram_pool.hpp).  It holds a non-owning Pool&; the Pool
// must outlive the cache.  Cache operations and the payload read are serialized
// under one mutex; the Reader callback is invoked while the lock is held and is
// documented non-reentrant (it must not call back into the cache).
//
// Default path untouched: not wired into the engine yet.  No GPU headers, no
// HIP/CUDA registration, no env knob read here.

#pragma once

#include <cstdint>
#include <functional>
#include <list>
#include <mutex>
#include <system_error>

#include "strata/core/ram_pool.hpp"

namespace strata::core {

/// Synchronous payload reader.  Given a (layer, expert) key, fills `dest` with
/// exactly `bytes` bytes starting at that expert's source offset; returns an
/// error_code on failure (short read, bad key, IO error).  Invoked under the
/// cache mutex and must not re-enter the cache.
using ExpertReader =
    std::function<std::error_code(int64_t layer, int64_t expert,
                                  uint8_t* dest, uint64_t bytes)>;

class PoolExpertCache {
public:
    PoolExpertCache() = delete;
    explicit PoolExpertCache(Pool& pool) noexcept : pool_(&pool) {}
    PoolExpertCache(const PoolExpertCache&) = delete;
    PoolExpertCache& operator=(const PoolExpertCache&) = delete;
    PoolExpertCache(PoolExpertCache&&) = delete;
    PoolExpertCache& operator=(PoolExpertCache&&) = delete;
    ~PoolExpertCache();

    /// Acquire the cached payload for (layer, expert) into `out`.
    ///   Hit: validates requested `bytes` match the cached entry, promotes the
    ///     entry to front (no read), returns a lease copy.  No read/copy/alloc
    ///     on hits beyond the lease copy.
    ///   Miss: prepares a pending list node BEFORE any pool allocation or
    ///     pressure eviction, then pool_alloc(bytes, page alignment, candidate).
    ///     Retry/eviction happens ONLY on std::errc::no_buffer_space: cold cache
    ///     refs are dropped one at a time and the alloc retried; live borrowers
    ///     keep allocations so the pool may still refuse.  Invalid key, negative
    ///     key, zero/impossible bytes, or a metadata error leave `out` and the
    ///     cache unchanged and never purge entries.
    ///   Reader is invoked under the lock; on failure nothing is published,
    ///     populated `out` is preserved, and the candidate is released (no throw).
    ///   On commit the pending node is spliced into the LRU front, the mutex is
    ///     unlocked, then `out` is replaced.
    std::error_code acquire(int64_t layer, int64_t expert, uint64_t bytes,
                            const ExpertReader& reader, PoolLease& out);

    /// Drop the cache's own reference to (layer, expert) if present.  Must not
    /// allocate.  Borrowers holding copies are unaffected.
    void release(int64_t layer, int64_t expert) noexcept;

    /// Drop every cache-owned reference.  Borrowers may outlive the cache.  Never
    /// allocates.
    void clear() noexcept;

    struct Stats {
        uint64_t entries = 0;          // cache-owned entries
        uint64_t cached_bytes = 0;     // bytes reserved by cache-owned leases
        uint64_t hits = 0;
        uint64_t misses = 0;
        uint64_t evictions = 0;
    };
    Stats stats() const noexcept;

private:
    struct Entry {
        int64_t layer = 0;
        int64_t expert = 0;
        uint64_t requested_bytes = 0;
        PoolLease lease;
    };

    Pool* pool_ = nullptr;  // non-owning; must outlive the cache
    mutable std::mutex mu_;
    std::list<Entry> lru_;  // front = most recent
    mutable uint64_t hits_ = 0;
    mutable uint64_t misses_ = 0;
    mutable uint64_t evictions_ = 0;

#if defined(STRATA_RAM_POOL_TEST)
    friend void cache_set_pre_emplace_fault(void (*hook)());
#endif
};

#if defined(STRATA_RAM_POOL_TEST)
// Test-only seam: invoked once before the pending Entry node is emplaced.
void cache_set_pre_emplace_fault(void (*hook)());
#endif

}  // namespace strata::core
