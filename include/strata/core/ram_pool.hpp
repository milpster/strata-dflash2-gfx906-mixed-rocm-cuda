// include/strata/core/ram_pool.hpp - managed pre-locked RAM pool core (foundation lane).
//
// A single anonymous, writable, page-aligned mmap that is fully mlocked before any
// allocation is handed out.  Sub-allocates page-granular slices out of that fixed
// mapping; nothing grows, nothing spills to the kernel.  Locked once at creation,
// unlocked only at final teardown.
//
// Default path untouched: this header/library is not wired into the engine yet.
// No GPU headers, no HIP/CUDA registration, no default behavior change, no env knob
// read here (budget inputs are passed explicitly as bytes).

#pragma once

#include <cstdint>
#include <memory>
#include <system_error>

namespace strata::core {

namespace detail {
struct PoolState;   // defined in src/core/ram_pool.cpp
struct LeaseState;  // defined in src/core/ram_pool.cpp
}  // namespace detail

/// Deterministic, checked byte arithmetic for pool capacity.
///
/// total_bytes     - whole RAM budget the caller commits to this process (bytes).
/// outside_owned   - bytes already owned outside the pool (server, vision, PLE lock,
///                   driver reserves, cgroup slop headroom ...).
/// internal_headroom - bytes the pool keeps reserved for its own metadata/slack
///                     (passed explicitly; this lane does not guess a default).
///
/// Returns the pool's usable capacity, page-rounded down, or an error if any operand
/// is invalid, zero, impossible (overflow/underflow), or rounds to zero.  All
/// operands are bytes.  No env/CLI knob here.
std::error_code pool_capacity_bytes(uint64_t total_bytes,
                                    uint64_t outside_owned,
                                    uint64_t internal_headroom,
                                    uint64_t& out_pool_bytes);

/// Stats sufficient for deterministic accounting.  All values are bytes unless noted.
struct PoolStats {
    uint64_t capacity_bytes = 0;     // usable pool capacity (page-rounded down)
    uint64_t locked_bytes = 0;       // bytes of the mapping currently mlocked
    uint64_t reserved_live_bytes = 0;// aligned bytes handed out to live leases
    uint64_t requested_live_bytes = 0;// raw bytes requested by live leases
    uint64_t free_bytes = 0;         // capacity - reserved_live_bytes
    uint64_t alloc_count = 0;        // live allocation count
    uint64_t alloc_high_water = 0;   // peak live allocation count
};

/// Opaque handle to a sub-allocated range.  Copyable; range is owned while any
/// copy of the handle exists.  Returns to the free list only when the LAST owning
/// lease is released (via pool_free or destructor).  Holding a lease for async use
/// prevents eviction/reuse.
class PoolLease {
public:
    PoolLease() = default;
    ~PoolLease();                          // defined in .cpp (needs complete LeaseState)
    PoolLease(const PoolLease&) = default;
    PoolLease& operator=(const PoolLease&) = default;
    PoolLease(PoolLease&&) noexcept = default;
    PoolLease& operator=(PoolLease&&) noexcept = default;

    bool valid() const noexcept { return impl_ != nullptr; }
    void* data() const noexcept;
    uint64_t size() const noexcept;
    uint64_t offset() const noexcept;

private:
    friend class Pool;
    explicit PoolLease(std::shared_ptr<detail::LeaseState> impl) noexcept : impl_(std::move(impl)) {}
    std::shared_ptr<detail::LeaseState> impl_;
};

/// The pool.  Move-only; owns nothing copyable.  The underlying mmap stays alive
/// until every outstanding lease is gone, even if the Pool object that created it
/// is destroyed first.
class Pool {
public:
    Pool() = default;
    ~Pool();                               // defined in .cpp (needs complete PoolState)
    Pool(const Pool&) = delete;
    Pool& operator=(const Pool&) = delete;
    Pool(Pool&&) noexcept = default;
    Pool& operator=(Pool&&) noexcept = default;

    /// Create + fully lock ONE anonymous writable mmap of `pool_bytes` (must be
    /// page-aligned and > 0).  Complete lock or explicit failure with full cleanup;
    /// never a silent partial/unlocked fallback.  On platforms where mlock of a whole
    /// anonymous mapping is unsupported, creation fails with a clear error behind a
    /// compile guard.
    static Pool create(uint64_t pool_bytes, std::error_code& ec);

    /// Allocate `bytes` with `alignment` (checked power of two, page-granular).
    /// Returns an error on invalid args, overflow, or exhaustion/fragmentation.
    /// Stats unchanged on failure.
    std::error_code pool_alloc(uint64_t bytes,
                               uint64_t alignment,
                               PoolLease& out_lease);

    /// Release a lease.  The range returns to the free list only when this was the
    /// last owning lease (coalesced with neighbors).  No-op on an invalid lease.
    void pool_free(PoolLease& lease);

    PoolStats stats() const noexcept;

    /// The locked mapping's base address (for consumers that stage into it directly).
    void* base() const noexcept;

    uint64_t capacity() const noexcept;

private:
    std::shared_ptr<detail::PoolState> impl_;
};

/// RAII scope guard: releases the lease on destruction.
struct PoolLeaseGuard {
    Pool& pool;
    PoolLease lease;
    ~PoolLeaseGuard() { if (lease.valid()) pool.pool_free(lease); }
    PoolLeaseGuard(const PoolLeaseGuard&) = delete;
    PoolLeaseGuard& operator=(const PoolLeaseGuard&) = delete;
    PoolLeaseGuard(PoolLeaseGuard&&) noexcept = default;
    PoolLeaseGuard& operator=(PoolLeaseGuard&&) noexcept = delete;
};

}  // namespace strata::core
