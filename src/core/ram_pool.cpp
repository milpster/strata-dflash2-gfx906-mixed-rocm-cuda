// src/core/ram_pool.cpp - managed pre-locked RAM pool core (foundation lane).
//
// One anonymous, writable, page-aligned mmap, fully mlocked before any allocation is
// handed out.  Page-granular sub-allocator with refcounted leases: a range returns to
// the free list only when its LAST owning lease is released, and the mapping stays
// alive until every lease is gone (even if the Pool wrapper that created it is
// destroyed first).  Locked once at creation, unlocked only at final teardown.
//
// No GPU headers, no HIP/CUDA registration, no default behavior change, no env knob
// read here.  Not wired into the engine yet.

#include "strata/core/ram_pool.hpp"

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <map>
#include <memory>
#include <mutex>
#include <sys/mman.h>
#include <sys/resource.h>
#include <unistd.h>
#include <utility>
#include <vector>

namespace strata::core {
namespace detail {

constexpr uint64_t kPage = 4096;
constexpr uint64_t kLockStep = 256ull << 20;  // bounded mlock progress chunks

inline uint64_t round_up_page(uint64_t v) { return (v + kPage - 1) & ~(kPage - 1); }
inline uint64_t round_up(uint64_t v, uint64_t align) {
    return (v + align - 1) & ~(align - 1);
}
inline bool is_pow2(uint64_t v) { return v != 0 && (v & (v - 1)) == 0; }

struct LeaseState;  // defined below; PoolState::live keys on its pointer

struct PoolState {
    std::mutex mu;
    void* base = nullptr;
    uint64_t size = 0;
    bool locked = false;
    // free ranges, sorted by start, coalesced, page-aligned half-open [start, end)
    struct Range {
        uint64_t start;
        uint64_t end;
    };
    std::vector<Range> free;
    // live allocations keyed by lease state pointer
    struct Live {
        uint64_t start;
        uint64_t size;      // reserved (aligned) bytes
        uint64_t requested; // raw bytes requested
    };
    std::map<LeaseState*, Live> live;
    PoolStats stats;

    void free_range_unlocked(uint64_t start, uint64_t size);
};

struct LeaseState {
    std::shared_ptr<PoolState> pool;
    uint64_t start = 0;
    uint64_t size = 0;      // reserved bytes
    uint64_t requested = 0; // raw bytes requested

    ~LeaseState() {
        if (pool == nullptr || size == 0) return;
        std::lock_guard<std::mutex> g(pool->mu);
        pool->live.erase(this);
        pool->stats.reserved_live_bytes -= size;
        pool->stats.requested_live_bytes -= requested;
        if (pool->stats.alloc_count > 0) pool->stats.alloc_count--;
        pool->stats.free_bytes =
            pool->stats.capacity_bytes - pool->stats.reserved_live_bytes;
        pool->free_range_unlocked(start, size);
    }
};

}  // namespace detail

std::error_code pool_capacity_bytes(uint64_t total_bytes,
                                    uint64_t outside_owned,
                                    uint64_t internal_headroom,
                                    uint64_t& out_pool_bytes) {
    out_pool_bytes = 0;
    if (total_bytes == 0) return std::make_error_code(std::errc::invalid_argument);
    if (outside_owned > total_bytes) return std::make_error_code(std::errc::invalid_argument);
    const uint64_t after_owners = total_bytes - outside_owned;
    if (internal_headroom > after_owners) return std::make_error_code(std::errc::invalid_argument);
    uint64_t raw = after_owners - internal_headroom;
    if (raw == 0) return std::make_error_code(std::errc::invalid_argument);
    uint64_t page = raw & ~(detail::kPage - 1);
    if (page == 0) return std::make_error_code(std::errc::invalid_argument);
    out_pool_bytes = page;
    return {};
}

namespace {

// Merge overlapping/adjacent free ranges; keeps the list sorted and non-overlapping.
void coalesce_free(std::vector<detail::PoolState::Range>& free) {
    if (free.empty()) return;
    std::sort(free.begin(), free.end(),
              [](const detail::PoolState::Range& a, const detail::PoolState::Range& b) {
                  return a.start < b.start;
              });
    std::vector<detail::PoolState::Range> out;
    out.push_back(free[0]);
    for (size_t i = 1; i < free.size(); ++i) {
        auto& last = out.back();
        if (free[i].start <= last.end) {
            if (free[i].end > last.end) last.end = free[i].end;
        } else {
            out.push_back(free[i]);
        }
    }
    free = std::move(out);
}

}  // namespace

void detail::PoolState::free_range_unlocked(uint64_t start, uint64_t size) {
    free.push_back({start, start + size});
    coalesce_free(free);
}

// ---- PoolStats ----

PoolStats Pool::stats() const noexcept {
    if (impl_ == nullptr) return {};
    std::lock_guard<std::mutex> g(impl_->mu);
    return impl_->stats;
}

// ---- PoolLease ----

PoolLease::~PoolLease() = default;

void* PoolLease::data() const noexcept {
    if (impl_ == nullptr || impl_->pool == nullptr) return nullptr;
    return static_cast<char*>(impl_->pool->base) + impl_->start;
}
uint64_t PoolLease::size() const noexcept { return impl_ ? impl_->size : 0; }
uint64_t PoolLease::offset() const noexcept { return impl_ ? impl_->start : 0; }

// ---- Pool ----

Pool::~Pool() {
    // Releasing our shared_ptr may be the last reference (or a lease may still hold
    // the mapping alive).  Either way the PoolState destructor performs munlock +
    // munmap exactly once when the final reference goes away.
    impl_.reset();
}

void* Pool::base() const noexcept {
    return impl_ ? impl_->base : nullptr;
}
uint64_t Pool::capacity() const noexcept {
    return impl_ ? impl_->stats.capacity_bytes : 0;
}

namespace {
struct PoolStateDeleter {
    void operator()(detail::PoolState* s) const noexcept {
        if (s == nullptr) return;
        if (s->base != nullptr && s->size != 0) {
            if (s->locked) ::munlock(s->base, s->size);
            ::munmap(s->base, s->size);
        }
        delete s;
    }
};
}  // namespace

Pool Pool::create(uint64_t pool_bytes, std::error_code& ec) {
    ec = {};
    Pool p;
    if (pool_bytes == 0 || pool_bytes % detail::kPage != 0) {
        ec = std::make_error_code(std::errc::invalid_argument);
        return p;
    }

#if defined(__linux__)
    auto st = std::shared_ptr<detail::PoolState>(new detail::PoolState, PoolStateDeleter{});
    st->stats.capacity_bytes = pool_bytes;
    st->stats.free_bytes = pool_bytes;

    // One anonymous writable mmap, page-aligned.
    void* base = ::mmap(nullptr, pool_bytes, PROT_READ | PROT_WRITE,
                        MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (base == MAP_FAILED) {
        ec = std::error_code(errno, std::generic_category());
        return p;
    }
    st->base = base;
    st->size = pool_bytes;
    st->free.push_back({0, pool_bytes});

    // Fully mlock in bounded progress chunks.  Complete lock or explicit failure
    // with full cleanup -- never a silent partial/unlocked fallback.
    uint64_t got = 0;
    bool ok = true;
    for (uintptr_t lo = (uintptr_t)base; lo < (uintptr_t)base + pool_bytes;
         lo += detail::kLockStep) {
        const uintptr_t piece =
            (lo + detail::kLockStep > (uintptr_t)base + pool_bytes)
                ? (uintptr_t)base + pool_bytes - lo
                : detail::kLockStep;
        if (::mlock((const void*)lo, (size_t)piece) != 0) {
            ok = false;
            break;
        }
        got += (uint64_t)piece;
    }
    if (ok) {
        st->locked = true;
        st->stats.locked_bytes = pool_bytes;
        p.impl_ = st;
        return p;
    }

    // Failure: unlock what we got, unmap everything, report error, no fallback.
    if (got != 0) ::munlock(base, (size_t)got);
    ::munmap(base, pool_bytes);
    ec = std::make_error_code(std::errc::not_enough_memory);
    return p;
#else
    ec = std::make_error_code(std::errc::not_supported);
    return p;
#endif
}

void Pool::pool_free(PoolLease& lease) {
    // Releasing our reference; when it was the last owning lease the LeaseState
    // destructor returns the range to the free list.
    lease.impl_.reset();
}

std::error_code Pool::pool_alloc(uint64_t bytes, uint64_t alignment,
                                 PoolLease& out_lease) {
    std::error_code ec;
    if (impl_ == nullptr) { ec = std::make_error_code(std::errc::invalid_argument); return ec; }
    if (bytes == 0) { ec = std::make_error_code(std::errc::invalid_argument); return ec; }
    if (!detail::is_pow2(alignment) || alignment < detail::kPage) {
        ec = std::make_error_code(std::errc::invalid_argument);
        return ec;
    }
    const uint64_t reserved = detail::round_up_page(bytes);
    if (reserved < bytes) { ec = std::make_error_code(std::errc::result_out_of_range); return ec; }

    auto& st = *impl_;
    std::lock_guard<std::mutex> g(st.mu);

    // First-fit over coalesced free ranges.
    size_t slot = 0;
    uint64_t chosen_start = 0;
    bool found = false;
    for (size_t i = 0; i < st.free.size(); ++i) {
        const uint64_t s0 = st.free[i].start;
        const uint64_t e0 = st.free[i].end;
        const uint64_t start = detail::round_up(s0, alignment);
        if (reserved > e0 - start) continue;  // overflow-safe fit test
        chosen_start = start;
        slot = i;
        found = true;
        break;
    }
    if (!found) { ec = std::make_error_code(std::errc::not_enough_memory); return ec; }

    const uint64_t s0 = st.free[slot].start;
    const uint64_t e0 = st.free[slot].end;
    const uint64_t lead = chosen_start - s0;
    const uint64_t trail = e0 - (chosen_start + reserved);

    auto ls = std::make_shared<detail::LeaseState>();
    ls->pool = impl_;
    ls->start = chosen_start;
    ls->size = reserved;
    ls->requested = bytes;

    detail::LeaseState* raw = ls.get();
    st.live.emplace(raw, detail::PoolState::Live{chosen_start, reserved, bytes});
    st.stats.reserved_live_bytes += reserved;
    st.stats.requested_live_bytes += bytes;
    st.stats.alloc_count++;
    if (st.stats.alloc_count > st.stats.alloc_high_water)
        st.stats.alloc_high_water = st.stats.alloc_count;
    st.stats.free_bytes =
        st.stats.capacity_bytes - st.stats.reserved_live_bytes;

    // Rebuild the free slot without the carved piece, keeping it sorted/coalesced.
    if (lead != 0 && trail != 0) {
        st.free[slot] = {s0, chosen_start};
        st.free.insert(st.free.begin() + static_cast<long>(slot) + 1,
                       detail::PoolState::Range{chosen_start + reserved, e0});
    } else if (lead != 0) {
        st.free[slot] = {s0, chosen_start};
    } else if (trail != 0) {
        st.free[slot] = {chosen_start + reserved, e0};
    } else {
        st.free.erase(st.free.begin() + static_cast<long>(slot));
    }
    coalesce_free(st.free);

    out_lease.impl_ = ls;
    return ec;
}

}  // namespace strata::core
