// src/core/pool_expert_cache.cpp - CPU expert-cache foundation over ram_pool.
//
// Single std::list<Entry> LRU, linear lookup.  Cache owns one PoolLease per
// entry; borrowers own copies and may outlive the cache.  Release never
// allocates.  Reader is invoked under the mutex and is non-reentrant.

#include "strata/core/pool_expert_cache.hpp"

#include <cstdint>
#include <new>
#include <stdexcept>
#include <system_error>
#include <utility>

namespace strata::core {

namespace {

// Page alignment for every cached payload.  The pool's page size is 4096 on
// Linux; using it keeps cache-owned blocks page-aligned like pool_alloc.
constexpr uint64_t kCacheAlign = 4096ull;

}  // namespace

PoolExpertCache::~PoolExpertCache() {
    clear();
}

std::error_code PoolExpertCache::acquire(int64_t layer, int64_t expert,
                                         uint64_t bytes, ExpertReader reader,
                                         PoolLease& out) {
    if (pool_ == nullptr || !reader || layer == 0 || expert == 0 ||
        bytes == 0) {
        return std::make_error_code(std::errc::invalid_argument);
    }

    // All cache mutation and the payload read are serialized under mu_.  The
    // Reader is non-reentrant (it must not call back into the cache).
    std::unique_lock<std::mutex> g(mu_);

    // ---- hit path: validate bytes, promote, return lease copy (no read) ----
    for (auto it = lru_.begin(); it != lru_.end(); ++it) {
        if (it->layer == layer && it->expert == expert) {
            if (it->requested_bytes != bytes) {
                return std::make_error_code(std::errc::invalid_argument);
            }
            lru_.splice(lru_.begin(), lru_, it);
            ++hits_;
            out = it->lease;  // borrower copy
            return {};
        }
    }
    ++misses_;

    // ---- miss path ----
    // Prepare the candidate allocation before pressure eviction or read, so
    // nothing publishes partial state.  On capacity pressure drop cold cache
    // refs (LRU tail) one at a time and retry; live borrowers keep their
    // allocations, so the pool may still refuse.
    PoolLease candidate;
    std::error_code ec = pool_->pool_alloc(bytes, kCacheAlign, candidate);
    while (ec && !lru_.empty()) {
        auto& cold = lru_.back();
        cold.lease = PoolLease{};  // drop cache ref only
        lru_.pop_back();
        ++evictions_;
        ec = pool_->pool_alloc(bytes, kCacheAlign, candidate);
    }
    if (ec) return ec;  // refuse; pool exhausted by live borrowers

    Entry pending;
    pending.layer = layer;
    pending.expert = expert;
    pending.requested_bytes = bytes;
    pending.lease = std::move(candidate);

    // Read payload; reader bad_alloc/length caught here.  Failed read publishes
    // no entry, preserves populated out, releases candidate (no throw).
    uint8_t* dest = static_cast<uint8_t*>(pending.lease.data());
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
        pending.lease = PoolLease{};  // release candidate, no throw
        return read_ec;
    }

    // Commit: splice pending node into LRU front.  Unlock before replacing out.
    lru_.push_front(std::move(pending));
    PoolLease copy = lru_.begin()->lease;
    g.unlock();
    out = std::move(copy);
    return {};
}

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
