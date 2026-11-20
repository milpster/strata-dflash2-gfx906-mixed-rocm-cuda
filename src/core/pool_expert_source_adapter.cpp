// src/core/pool_expert_source_adapter.cpp - PoolExpertStore-backed ExpertSource
// adapter (CPU-testable; the CUDA event implementation is supplied by Task 2,
// so this TU compiles without CUDA).

#include "strata/core/pool_expert_source_adapter.hpp"

#include <cstdint>
#include <utility>

namespace strata::core {

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
