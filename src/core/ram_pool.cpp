// src/core/ram_pool.cpp - managed pre-locked RAM pool core (foundation lane).
//
// ONE anonymous writable mmap, fully mlocked before any allocation is handed out.
// Sub-allocator over a single ordered std::list<Range> partition of [0, capacity),
// each node free or allocated.  LeaseState holds shared PoolState ownership plus a
// stable iterator to its allocated node.  Mapping stays alive until the last lease
// is gone even if the Pool wrapper is destroyed first.  Locked once at creation,
// unlocked exactly once at final teardown (incl. a partly locked prefix).
//
// No GPU headers, no HIP/CUDA registration, no default behavior change, no env knob.
// Not wired into the engine yet.

#include "strata/core/ram_pool.hpp"

#include <cerrno>
#include <cstdint>
#include <functional>
#include <list>
#include <memory>
#include <mutex>
#include <new>
#include <utility>

#if defined(__linux__)
#include <sys/mman.h>
#include <sys/resource.h>
#include <unistd.h>
#endif

namespace strata::core {
namespace detail {

constexpr uint64_t kPage = 4096;

inline uint64_t round_up_page(uint64_t v) { return (v + kPage - 1) & ~(kPage - 1); }

inline bool round_up(uint64_t v, uint64_t align, uint64_t& out) {
    if (align == 0 || (align & (align - 1)) != 0) return false;
    if (v > UINT64_MAX - (align - 1)) return false;
    out = (v + align - 1) & ~(align - 1);
    return true;
}

inline bool is_pow2(uint64_t v) { return v != 0 && (v & (v - 1)) == 0; }

// Test-only mlock progress-chunk seam (default production step).  Defined in the
// implementation TU so declarations and definitions always match.
inline uint64_t& lock_step() {
    static uint64_t step = 256ull << 20;
    return step;
}
inline void set_lock_step(uint64_t s) { lock_step() = s; }

// Test-only metadata-injection seam: if non-null, invoked once before each metadata
// allocation during pool_alloc preparation (make_shared<LeaseState>).  May throw
// std::bad_alloc, caught transactionally, to force deterministic failure at each
// preparation point.  Not called on release.
using MetadataFault = std::function<void()>;
MetadataFault g_metadata_fault;

struct Range {
    uint64_t start = 0;
    uint64_t end = 0;   // exclusive
    bool free = true;
};

struct PoolState {
    std::mutex mu;
    void* base = nullptr;
    uint64_t size = 0;
    bool locked = false;
    std::list<Range> partition;   // ordered [start,end) partition of [0, size)
    PoolStats stats;
};

struct LeaseState {
    std::shared_ptr<PoolState> pool;
    std::list<Range>::iterator node;   // stable iterator into pool->partition
    uint64_t size = 0;      // reserved (aligned) bytes
    uint64_t requested = 0; // raw bytes requested

    ~LeaseState() noexcept {
        if (pool == nullptr || size == 0) return;
        std::lock_guard<std::mutex> g(pool->mu);
        node->free = true;
        pool->stats.reserved_live_bytes -= size;
        pool->stats.requested_live_bytes -= requested;
        if (pool->stats.alloc_count > 0) pool->stats.alloc_count--;
        pool->stats.free_bytes = pool->stats.capacity_bytes - pool->stats.reserved_live_bytes;
        // Merge adjacent free nodes by erase only (no allocation).
        auto cur = node;
        auto n = std::next(cur);
        if (n != pool->partition.end() && cur->free && n->free && cur->end == n->start) {
            cur->end = n->end;
            pool->partition.erase(n);
        }
        if (cur != pool->partition.begin()) {
            auto p = std::prev(cur);
            if (p->free && p->end == cur->start) {
                p->end = cur->end;
                pool->partition.erase(cur);
            }
        }
    }
};

}  // namespace detail

// ---- pool_capacity_bytes ----

std::error_code pool_capacity_bytes(uint64_t total_bytes,
                                    uint64_t outside_owned,
                                    uint64_t internal_headroom,
                                    uint64_t& out_pool_bytes) {
    out_pool_bytes = 0;
    if (total_bytes == 0) return std::make_error_code(std::errc::invalid_argument);
    if (outside_owned > total_bytes) return std::make_error_code(std::errc::invalid_argument);
    const uint64_t after_owners = total_bytes - outside_owned; // no underflow (guarded)
    if (internal_headroom > after_owners) return std::make_error_code(std::errc::invalid_argument);
    const uint64_t raw = after_owners - internal_headroom; // no underflow (guarded)
    if (raw == 0) return std::make_error_code(std::errc::invalid_argument);
    if (raw > UINT64_MAX - (detail::kPage - 1)) return std::make_error_code(std::errc::invalid_argument);
    const uint64_t page = raw & ~(detail::kPage - 1);
    if (page == 0) return std::make_error_code(std::errc::invalid_argument);
    out_pool_bytes = page;
    return {};
}

// ---- PoolStats / PoolLease ----

PoolStats Pool::stats() const noexcept {
    if (impl_ == nullptr) return {};
    std::lock_guard<std::mutex> g(impl_->mu);
    return impl_->stats;
}

PoolLease::~PoolLease() = default;

void* PoolLease::data() const noexcept {
    if (impl_ == nullptr || impl_->pool == nullptr) return nullptr;
    return static_cast<char*>(impl_->pool->base) + impl_->node->start;
}
uint64_t PoolLease::size() const noexcept { return impl_ ? impl_->size : 0; }
uint64_t PoolLease::offset() const noexcept { return impl_ ? impl_->node->start : 0; }

// ---- Pool lifecycle ----

Pool::~Pool() { impl_.reset(); }

void* Pool::base() const noexcept { return impl_ ? impl_->base : nullptr; }
uint64_t Pool::capacity() const noexcept { return impl_ ? impl_->stats.capacity_bytes : 0; }

namespace {

struct PoolStateDeleter {
    void operator()(detail::PoolState* s) const noexcept {
        if (s == nullptr) return;
#if defined(__linux__)
        if (s->base != nullptr && s->size != 0) {
            int saved = 0;
            if (s->locked) {
                if (::munlock(s->base, s->size) != 0 && saved == 0) saved = errno;
            }
            if (::munmap(s->base, s->size) != 0 && saved == 0) saved = errno;
        }
#endif
        delete s;
    }
};

}  // namespace

// ---- Pool::create ----

Pool Pool::create(uint64_t pool_bytes, std::error_code& ec) {
    ec = {};
    Pool p;
    if (pool_bytes == 0 || pool_bytes % detail::kPage != 0) {
        ec = std::make_error_code(std::errc::invalid_argument);
        return p;
    }
    if (pool_bytes > static_cast<uint64_t>(SIZE_MAX)) {
        ec = std::make_error_code(std::errc::not_enough_memory);
        return p;
    }

#if defined(__linux__)
    long pagesize = ::sysconf(_SC_PAGESIZE);
    if (pagesize <= 0) {
        ec = std::error_code(EINVAL, std::generic_category());
        return p;
    }
    const uint64_t page = static_cast<uint64_t>(pagesize);
    if (pool_bytes % page != 0) {
        ec = std::make_error_code(std::errc::invalid_argument);
        return p;
    }

    auto st = std::make_shared<detail::PoolState>();
    st->stats.capacity_bytes = pool_bytes;
    st->stats.free_bytes = pool_bytes;
    st->partition.push_back({0, pool_bytes, true});

    void* base = ::mmap(nullptr, pool_bytes, PROT_READ | PROT_WRITE,
                        MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (base == MAP_FAILED) {
        ec = std::error_code(errno, std::generic_category());
        return p;
    }
    st->base = base;
    st->size = pool_bytes;

    // Lock in bounded offset-sized chunks; pre-fault each page.  Preserve first
    // failing syscall errno.  Complete lock or explicit failure with cleanup.
    uint64_t got = 0;
    int saved_errno = 0;
    bool ok = true;
    const uint64_t step = detail::lock_step();
    for (uint64_t off = 0; off < pool_bytes && ok; ) {
        const uint64_t piece = (step < pool_bytes - off) ? step : (pool_bytes - off);
        volatile char* fp = static_cast<volatile char*>(base) + off;
        for (uint64_t i = 0; i < piece; i += page) { *fp = 0; fp += page; }
        if (::mlock(static_cast<const void*>(static_cast<char*>(base) + off),
                    static_cast<size_t>(piece)) != 0) {
            ok = false;
            saved_errno = errno;
            break;
        }
        got += piece;
        off += piece;
    }

    if (ok) {
        st->locked = true;
        st->stats.locked_bytes = pool_bytes;
        p.impl_ = st;
        return p;
    }

    // Failure: unlock the locked prefix, unmap the whole mapping, drop base/size so
    // the shared ownership runs its deleter exactly once at teardown.  Preserve errno.
    if (got != 0) {
        if (::munlock(static_cast<const void*>(base), static_cast<size_t>(got)) != 0 && saved_errno == 0)
            saved_errno = errno;
    }
    if (::munmap(base, pool_bytes) != 0 && saved_errno == 0)
        saved_errno = errno;
    st->base = nullptr;
    st->size = 0;
    ec = std::error_code(saved_errno ? saved_errno : ENOMEM, std::generic_category());
    return p;
#else
    ec = std::make_error_code(std::errc::not_supported);
    return p;
#endif
}

// ---- Pool::pool_free ----

void Pool::pool_free(PoolLease& lease) {
    lease.impl_.reset();  // last owner's LeaseState dtor frees+coalesces the node
}

// ---- Pool::pool_alloc ----

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
    std::unique_lock<std::mutex> g(st.mu);

    // First-fit over the ordered free partition.  Absolute-address alignment:
    // returned address = base + chosen_start; base may be only page-aligned, so
    // round the absolute address up to alignment and derive start.
    const uintptr_t base_addr = reinterpret_cast<uintptr_t>(st.base);
    uint64_t chosen_start = 0;
    auto chosen = st.partition.end();
    for (auto it = st.partition.begin(); it != st.partition.end(); ++it) {
        if (!it->free) continue;
        const uint64_t s0 = it->start;
        const uint64_t e0 = it->end;
        uintptr_t abs = base_addr + s0; // checked: s0 <= capacity <= SIZE_MAX
        uintptr_t abs_rounded = abs;
        if (!detail::round_up(abs, alignment, abs_rounded)) continue;
        if (abs_rounded < abs) continue;                 // overflow (unreachable)
        const uint64_t start = static_cast<uint64_t>(abs_rounded - base_addr);
        if (start > e0) continue;
        if (reserved > e0 - start) continue;
        if (reserved > st.stats.capacity_bytes - st.stats.reserved_live_bytes) continue;
        chosen_start = start;
        chosen = it;
        break;
    }
    if (chosen == st.partition.end()) { ec = std::make_error_code(std::errc::not_enough_memory); return ec; }

    const uint64_t s0 = chosen->start;
    const uint64_t e0 = chosen->end;
    const uint64_t lead = chosen_start - s0;
    const uint64_t trail = e0 - (chosen_start + reserved);

    // Prepare UNARMED candidate + replacement nodes before mutating shared state.
    std::shared_ptr<detail::LeaseState> ls;
    std::list<detail::Range> repl;
    try {
        if (detail::g_metadata_fault) detail::g_metadata_fault();
        ls = std::make_shared<detail::LeaseState>();
        ls->pool = impl_;
        ls->size = reserved;
        ls->requested = bytes;
        if (lead != 0) repl.push_back({s0, chosen_start, true});
        repl.push_back({chosen_start, chosen_start + reserved, false});
        if (trail != 0) repl.push_back({chosen_start + reserved, e0, true});
    } catch (const std::bad_alloc&) {
        ec = std::make_error_code(std::errc::not_enough_memory);
        return ec;
    } catch (const std::length_error&) {
        ec = std::make_error_code(std::errc::not_enough_memory);
        return ec;
    }

    // Commit with non-allocating splice/erase.  Splice moves repl nodes before
    // chosen, then erase the old node.  Iterators into repl remain valid and now
    // refer to partition nodes.
    auto live_it = std::next(repl.begin(), (lead != 0) ? 1 : 0);
    ls->node = live_it;
    st.partition.splice(chosen, repl, repl.begin(), repl.end());
    st.partition.erase(chosen);

    // Arm stats, unlock BEFORE replacing the output (safe last-lease swap).
    st.stats.reserved_live_bytes += reserved;
    st.stats.requested_live_bytes += bytes;
    st.stats.alloc_count++;
    if (st.stats.alloc_count > st.stats.alloc_high_water)
        st.stats.alloc_high_water = st.stats.alloc_count;
    st.stats.free_bytes = st.stats.capacity_bytes - st.stats.reserved_live_bytes;
    g.unlock();
    out_lease.impl_ = ls;
    return {};
}

}  // namespace strata::core
