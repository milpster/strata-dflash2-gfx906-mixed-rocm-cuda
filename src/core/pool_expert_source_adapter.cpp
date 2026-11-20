// src/core/pool_expert_source_adapter.cpp - PoolExpertStore-backed ExpertSource
// adapter (CPU-testable; the CUDA event implementation is supplied by Task 2,
// so this TU compiles without CUDA).

#include "strata/core/pool_expert_source_adapter.hpp"

#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <utility>

namespace strata::core {

namespace {
void report_copy_refusal(int64_t layer, int64_t expert, const std::error_code& ec) {
    std::fprintf(stderr,
                 "strata: managed pool: no room to copy expert %lld of layer %lld (%s)\n",
                 (long long) expert, (long long) layer,
                 ec ? ec.message().c_str() : "lease not valid");
}
}  // namespace

bool PoolExpertSourceAdapter::copy_blob(int64_t layer, int64_t expert, uint8_t* dst) {
    const uint64_t bytes = store_.blob_bytes(layer);
    if (dst == nullptr || bytes == 0) return false;
    PoolLease lease;
    // The store's cache is the file cache: a hit copies bytes it already holds
    // (no read), a miss reads through the bounded reader into the same pool.
    const std::error_code ec = store_.acquire(layer, expert, lease);
    if (ec || !lease.valid()) { report_copy_refusal(layer, expert, ec); return false; }
    std::memcpy(dst, lease.data(), (size_t) bytes);
    lease = PoolLease{};   // drop the pool reference at once: no borrower is created
    return true;
}

bool PoolExpertSourceAdapter::copy_blobs(const int32_t* layers, const int32_t* experts,
                                         uint8_t* const* dst, size_t n) {
    if (n == 0) return true;
    if (layers == nullptr || experts == nullptr || dst == nullptr) return false;
    if (n == 1) return copy_blob(layers[0], experts[0], dst[0]);

    PoolLease held[kCopyLeaseWindow];
    size_t base = 0;
    while (base < n) {
        const size_t m = std::min(kCopyLeaseWindow, n - base);
        uint64_t bytes[kCopyLeaseWindow] = {};
        std::error_code ec;
        size_t got = 0;
        for (; got < m; ++got) {
            const uint64_t b = store_.blob_bytes(layers[base + got]);
            if (dst[base + got] == nullptr || b == 0) break;
            bytes[got] = b;
            ec = store_.acquire(layers[base + got], experts[base + got], held[got]);
            if (ec || !held[got].valid()) break;
        }
        // The window the header documents: a debug build fails if it ever grows.
        assert(got <= kCopyLeaseWindow);
        for (size_t t = 0; t < got; ++t)
            std::memcpy(dst[base + t], held[t].data(), (size_t) bytes[t]);
        for (size_t t = 0; t < got; ++t) held[t] = PoolLease{};   // window dropped before anything waits
        if (got == m) { base += m; continue; }
        // The pool refused one pair while the window was held: give the window
        // back and ask for the rest one pair at a time (the least a pool can be
        // asked for).  The pairs already copied keep their bytes.
        for (size_t i = base + got; i < n; ++i)
            if (!copy_blob(layers[i], experts[i], dst[i])) return false;
        return true;
    }
    return true;
}

const uint8_t* PoolExpertSourceAdapter::blob_stable(int64_t layer, int64_t expert) {
    std::lock_guard<std::mutex> lk(mu_);
    borrow_locked(layer, expert);
    auto it = borrowers_.find(key_of(layer, expert));
    if (it == borrowers_.end() || !it->second.lease.valid()) return nullptr;
    return static_cast<const uint8_t*>(it->second.lease.data());
}

int PoolExpertSourceAdapter::blob_parts(int64_t layer, int64_t expert, BlobPart* out) {
    std::lock_guard<std::mutex> lk(mu_);
    borrow_locked(layer, expert);
    auto it = borrowers_.find(key_of(layer, expert));
    if (it == borrowers_.end() || !it->second.lease.valid() || out == nullptr) return 0;
    const uint64_t bytes = store_.blob_bytes(layer);
    out[0] = BlobPart{static_cast<const uint8_t*>(it->second.lease.data()), bytes, 0};
    return 1;
}

uint64_t PoolExpertSourceAdapter::release(int64_t layer, int64_t expert) {
    std::lock_guard<std::mutex> lk(mu_);
    drop_locked(key_of(layer, expert));
    sweep_completed_locked();
    return 0;
}

void PoolExpertSourceAdapter::note_async_read(const uint8_t* blob, void* stream) {
    int64_t found_key = -1;
    {
        std::lock_guard<std::mutex> lk(mu_);
        for (auto& kv : borrowers_) {
            if (kv.second.lease.valid() &&
                kv.second.lease.data() == static_cast<const void*>(blob)) {
                found_key = kv.first;
                break;
            }
        }
        if (found_key < 0) return;
        auto& b = borrowers_[found_key];
        // A newer note supersedes any prior event: the legacy stage buffer recycles
        // on the event recorded after the LAST async copy out of it
        // (expert_source.cpp:1255).  Re-arm before recording.
        b.pending_event = true;
        b.completed = false;
    }   // mu_ released BEFORE hook_: the engine hook may call mark_completed(),
    // which locks mu_; calling hook_ while holding mu_ would deadlock re-entrantly.
    // The hook may call mark_completed()/borrowed() safely.  Never drop at issue time.
    if (found_key >= 0 && hook_) hook_(found_key >> 32, found_key & 0xFFFFFFFFll, stream);
    {
        std::lock_guard<std::mutex> lk(mu_);
        sweep_completed_locked();
    }
}

// Simulate the async event completing for this borrower (CPU test hook; the
// engine's CUDA build drives completion through the async-note hook instead).
void PoolExpertSourceAdapter::mark_completed(int64_t layer, int64_t expert) {
    std::lock_guard<std::mutex> lk(mu_);
    auto it = borrowers_.find(key_of(layer, expert));
    if (it != borrowers_.end()) it->second.completed = true;
}

void PoolExpertSourceAdapter::borrow_locked(int64_t layer, int64_t expert) {
    const int64_t key = key_of(layer, expert);
    auto it = borrowers_.find(key);
    if (it != borrowers_.end()) {
        ++it->second.count;
        return;
    }
    PoolLease lease;
    const std::error_code ec = store_.acquire(layer, expert, lease);
    if (ec || !lease.valid()) return;  // no_buffer_space -> nullptr, no retry
    PoolBorrower b;
    b.count = 1;
    b.lease = std::move(lease);
    borrowers_.emplace(key, std::move(b));
}

void PoolExpertSourceAdapter::drop_locked(int64_t key) {
    auto it = borrowers_.find(key);
    if (it == borrowers_.end()) return;
    if (--it->second.count > 0) return;
    it->second.lease = PoolLease{};
    borrowers_.erase(it);
}

void PoolExpertSourceAdapter::drop_all_locked() {
    for (auto& kv : borrowers_) kv.second.lease = PoolLease{};
    borrowers_.clear();
}

void PoolExpertSourceAdapter::sweep_completed_locked() {
    // A completed borrower's last issued copy has landed: recycle it NOW, even
    // at count > 0.  The legacy contract recycles the stage buffer once the
    // event recorded after the LAST async copy completes, and callers re-borrow
    // for each further use (expert_source.cpp:1255).  Requiring count==0 would
    // pin every filled expert until teardown — the fill pattern never calls
    // release().  Non-noted borrowers (long synchronous retention) are never
    // swept: they drop only at release()/teardown.
    for (auto it = borrowers_.begin(); it != borrowers_.end();) {
        if (it->second.completed) {
            it->second.lease = PoolLease{};
            it = borrowers_.erase(it);
        } else {
            ++it;
        }
    }
}

bool PoolExpertSourceAdapter::borrowed(int64_t layer, int64_t expert) const {
    std::lock_guard<std::mutex> lk(mu_);
    const auto it = borrowers_.find(key_of(layer, expert));
    return it != borrowers_.end() && it->second.count > 0;
}

}  // namespace strata::core
