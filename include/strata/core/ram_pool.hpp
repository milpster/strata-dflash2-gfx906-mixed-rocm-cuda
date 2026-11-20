// include/strata/core/ram_pool.hpp - managed pre-locked RAM pool core (foundation lane).
//
// A single anonymous, writable, page-aligned mmap that is fully mlocked before any
// allocation is handed out.  Sub-allocates out of ONE ordered std::list<Range>
// partition of the whole mapping; each node is [start, end), free or allocated.
// A LeaseState holds shared ownership of PoolState plus a stable iterator to its
// allocated node, so the mapping stays alive until the last lease is gone even if
// the Pool wrapper is destroyed first.  Locked once at creation, unlocked only at
// final teardown.
//
// Default path untouched: not wired into the engine yet.  No GPU headers, no HIP/CUDA,
// no default behavior change, no env knob read here (budget inputs passed explicitly).

#pragma once

#include <cstdint>
#include <memory>
#include <system_error>
#include <utility>

namespace strata::core {

namespace detail {
struct PoolState;   // defined in src/core/ram_pool.cpp
struct LeaseState;  // defined in src/core/ram_pool.cpp
}  // namespace detail

/// Total minus outside-owned bytes and headroom, rounded down to a system page.
/// Errors reset out_pool_bytes to zero. This does not enforce a serving budget.
std::error_code pool_capacity_bytes(uint64_t total_bytes,
                                    uint64_t outside_owned,
                                    uint64_t internal_headroom,
                                    uint64_t& out_pool_bytes);

struct PoolStats {
    uint64_t capacity_bytes = 0;
    uint64_t locked_bytes = 0;
    uint64_t reserved_live_bytes = 0;
    uint64_t requested_live_bytes = 0;
    uint64_t free_bytes = 0;
    uint64_t alloc_count = 0;
    uint64_t alloc_high_water = 0;
};

class PoolLease {
public:
    PoolLease() = default;
    ~PoolLease();
    PoolLease(const PoolLease&) = default;
    PoolLease& operator=(const PoolLease&) = default;
    PoolLease(PoolLease&&) noexcept = default;
    PoolLease& operator=(PoolLease&&) noexcept = default;

    bool valid() const noexcept { return impl_ != nullptr; }
    /// Pointer stays valid while a lease copy exists. size() is page-reserved bytes.
    void* data() const noexcept;
    uint64_t size() const noexcept;
    uint64_t offset() const noexcept;

private:
    friend class Pool;
    explicit PoolLease(std::shared_ptr<detail::LeaseState> impl) noexcept : impl_(std::move(impl)) {}
    std::shared_ptr<detail::LeaseState> impl_;
};

class Pool {
public:
    Pool() = default;
    ~Pool();
    Pool(const Pool&) = delete;
    Pool& operator=(const Pool&) = delete;
    Pool(Pool&&) noexcept = default;
    Pool& operator=(Pool&&) noexcept = default;

    static Pool create(uint64_t pool_bytes, std::error_code& ec);
    /// Alignment applies to the returned address. Failure preserves out_lease.
    std::error_code pool_alloc(uint64_t bytes, uint64_t alignment, PoolLease& out_lease);
    void pool_free(PoolLease& lease);
    PoolStats stats() const noexcept;
    void* base() const noexcept;
    uint64_t capacity() const noexcept;

private:
    std::shared_ptr<detail::PoolState> impl_;
};

/// RAII scope guard: releases the lease on destruction.
struct PoolLeaseGuard {
    Pool& pool;
    PoolLease lease;
    PoolLeaseGuard(Pool& owner, PoolLease held) noexcept
        : pool(owner), lease(std::move(held)) {}
    ~PoolLeaseGuard() = default;
    PoolLeaseGuard(const PoolLeaseGuard&) = delete;
    PoolLeaseGuard& operator=(const PoolLeaseGuard&) = delete;
    PoolLeaseGuard(PoolLeaseGuard&&) noexcept = default;
    PoolLeaseGuard& operator=(PoolLeaseGuard&&) noexcept = delete;
};

#if defined(STRATA_RAM_POOL_TEST)
// Process-global test controls: change only when no pool operation is running.
uint64_t ram_pool_lock_step_bytes();
void ram_pool_set_lock_step_bytes(uint64_t);
void ram_pool_set_metadata_fault(void (*hook)());
#endif

}  // namespace strata::core
