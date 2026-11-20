// src/core/pool_expert_cache.cpp - CPU expert-cache foundation over ram_pool.
//
// Single std::list<Entry> LRU, linear lookup.  Cache owns one PoolLease per
// entry; borrowers own copies and may outlive the cache.  Release never
// allocates.  Reader is invoked under the mutex and is non-reentrant.
//
// Allocation/pressure contract: retry+evict happens ONLY on
// std::errc::no_buffer_space (no fitting range).  Metadata errors
// (not_enough_memory) and invalid requests never purge the cache.  A pending
// list node is prepared before any pool allocation, pressure eviction, or read,
// so nothing throws after shared state starts mutating.

#include "strata/core/pool_expert_cache.hpp"

#include <cstdint>
#include <stdexcept>
#include <system_error>
#include <utility>

#if defined(__linux__)
#include <unistd.h>
#endif

namespace strata::core {

namespace {

// System page size for alignment.  On Linux this is the real page size; on
// non-Linux it is arithmetic-only and pool creation is unsupported there.
uint64_t cache_page_size() noexcept {
#if defined(__linux__)
    const long page = ::sysconf(_SC_PAGESIZE);
    return page > 0 ? static_cast<uint64_t>(page) : 4096ull;
#else
    return 4096ull;
#endif
}

#if defined(STRATA_RAM_POOL_TEST)
// Test-only seam: invoked once before the pending Entry node is emplaced, so a
// hook can throw std::bad_alloc to force deterministic pending-node failure
// without any global new interception.  Not called on release/clear.
void (*g_pre_emplace_fault)() = nullptr;
#endif

}  // namespace

// Forward-declared below: rounds bytes up to page alignment.
bool detail_round_up(uint64_t v, uint64_t align, uint64_t& out);

PoolExpertCache::~PoolExpertCache() {
    clear();
}

std::error_code PoolExpertCache::acquire(int64_t layer, int64_t expert,
                                         uint64_t bytes,
                                         const ExpertReader& reader,
                                         PoolLease& out) {
    if (pool_ == nullptr || !reader || layer < 0 || expert < 0 ||
        bytes == 0) {
        return std::make_error_code(std::errc::invalid_argument);
    }

    std::unique_lock<std::mutex> g(mu_);

    // ---- hit path: validate bytes, promote, return lease copy (no read) ----
    for (auto it = lru_.begin(); it != lru_.end(); ++it) {
        if (it->layer == layer && it->expert == expert) {
            if (it->requested_bytes != bytes) {
                return std::make_error_code(std::errc::invalid_argument);
            }
            lru_.splice(lru_.begin(), lru_, it);
            ++hits_;
            out = it->lease;  // borrower copy while still locked
            return {};
        }
    }
    ++misses_;

    // Validate impossible size against pool capacity BEFORE any eviction so a
    // refused request never purges cache entries or touches `out`.
    const uint64_t page = cache_page_size();
    uint64_t reserved = 0;
    if (!detail_round_up(bytes, page, reserved) ||
        reserved > pool_->capacity()) {
        return std::make_error_code(std::errc::invalid_argument);
    }

    // ---- miss path ----
    // Prepare the pending Entry node in a temporary list BEFORE pool allocation
    // / pressure / read, so no allocation throws after shared state mutates.
    std::list<Entry> pending_list;
    Entry pending;
    pending.layer = layer;
    pending.expert = expert;
    pending.requested_bytes = bytes;

    PoolLease candidate;
    std::error_code ec = pool_->pool_alloc(bytes, page, candidate);
    // Retry/evict ONLY on no_buffer_space (no fitting range).  Metadata errors
    // and invalid requests leave the cache untouched.
    while (ec == std::errc::no_buffer_space && !lru_.empty()) {
        lru_.back().lease = PoolLease{};  // drop cold cache ref only
        lru_.pop_back();
        ++evictions_;
        ec = pool_->pool_alloc(bytes, page, candidate);
    }
    if (ec) return ec;  // refuse; pool exhausted by live borrowers

    pending.lease = std::move(candidate);

    // Test-only fault injection before the pending node is emplaced.  A throw
    // here simulates pending-node allocation failure; nothing has mutated the
    // cache or `out`, and the reader has not been called.
    try {
#if defined(STRATA_RAM_POOL_TEST)
        if (g_pre_emplace_fault) g_pre_emplace_fault();
#endif
        pending_list.push_front(std::move(pending));
    } catch (const std::bad_alloc&) {
        return std::make_error_code(std::errc::not_enough_memory);
    } catch (const std::length_error&) {
        return std::make_error_code(std::errc::not_enough_memory);
    }

    // Read payload under lock.  Reader bad_alloc/length caught here.  Failed
    // read publishes no entry, preserves populated out, releases candidate.
    uint8_t* dest = static_cast<uint8_t*>(pending_list.begin()->lease.data());
    std::error_code read_ec{};
    try {
        read_ec = reader(layer, expert, dest, bytes);
    } catch (const std::bad_alloc&) {
        read_ec = std::make_error_code(std::errc::not_enough_memory);
    } catch (const std::length_error&) {
        read_ec = std::make_error_code(std::errc::not_enough_memory);
    } catch (...) {
        read_ec = std::make_error_code(std::errc::io_error);
    }
    if (read_ec) {
        pending_list.begin()->lease = PoolLease{};  // release candidate
        return read_ec;
    }

    // Commit: splice pending node into LRU front, unlock before replacing out.
    lru_.splice(lru_.begin(), pending_list, pending_list.begin());
    PoolLease copy = lru_.begin()->lease;
    g.unlock();
    out = std::move(copy);
    return {};
}

// Round-up helper mirroring detail::round_up (not exposed in the header).
bool detail_round_up(uint64_t v, uint64_t align, uint64_t& out) {
    if (align == 0 || (align & (align - 1)) != 0) return false;
    if (v > UINT64_MAX - (align - 1)) return false;
    out = (v + align - 1) & ~(align - 1);
    return true;
}

#if defined(STRATA_RAM_POOL_TEST)
void cache_set_pre_emplace_fault(void (*hook)()) {
    g_pre_emplace_fault = hook;
}
#endif

void PoolExpertCache::release(int64_t layer, int64_t expert) noexcept {
    std::lock_guard<std::mutex> g(mu_);
    for (auto it = lru_.begin(); it != lru_.end(); ++it) {
        if (it->layer == layer && it->expert == expert) {
            it->lease = PoolLease{};  // drop cache ref only
            lru_.erase(it);
            return;
        }
    }
}

void PoolExpertCache::clear() noexcept {
    std::lock_guard<std::mutex> g(mu_);
    while (!lru_.empty()) {
        lru_.begin()->lease = PoolLease{};  // drop cache ref only
        lru_.erase(lru_.begin());
    }
}

PoolExpertCache::Stats PoolExpertCache::stats() const noexcept {
    std::lock_guard<std::mutex> g(mu_);
    Stats s;
    s.entries = static_cast<uint64_t>(lru_.size());
    uint64_t bytes = 0;
    for (const auto& e : lru_) bytes += e.lease.size();
    s.cached_bytes = bytes;
    s.hits = hits_;
    s.misses = misses_;
    s.evictions = evictions_;
    return s;
}

}  // namespace strata::core
