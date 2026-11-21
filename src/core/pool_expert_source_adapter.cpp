// src/core/pool_expert_source_adapter.cpp - PoolExpertStore-backed ExpertSource
// adapter (CPU-testable; the CUDA event implementation is supplied by Task 2,
// so this TU compiles without CUDA).

#include "strata/core/pool_expert_source_adapter.hpp"

#include <algorithm>
#include <cassert>
#include <cctype>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <utility>

namespace strata::core {

namespace {

// ---- admission-retry gate (STRATA_RAM_POOL_ADMIT_RETRY, default OFF) --------

#if defined(STRATA_RAM_POOL_TEST)
// Tests flip the gate without touching the process environment; -1 = follow it.
int g_admit_retry_override = -1;
#endif

bool admit_retry_from_env() {
    const char* v = std::getenv("STRATA_RAM_POOL_ADMIT_RETRY");
    if (v == nullptr) return false;
    std::string s(v);
    const size_t b = s.find_first_not_of(" \t");
    const size_t e = s.find_last_not_of(" \t");
    if (b == std::string::npos) return false;
    s = s.substr(b, e - b + 1);
    for (char& c : s) c = (char) std::tolower((unsigned char) c);
    return !(s == "0" || s == "off" || s == "false" || s == "no");
}

bool admit_retry_enabled() {
#if defined(STRATA_RAM_POOL_TEST)
    if (g_admit_retry_override >= 0) return g_admit_retry_override != 0;
#endif
    static const bool on = admit_retry_from_env();   // read once per process
    return on;
}

// Is this refusal something another thread's release can plausibly clear?
//
// Only std::errc::no_buffer_space can be, and only for this reason: read how the two layers build
// their errors.
//   PoolExpertStore::acquire() - not_connected (store not open), invalid_argument (layer/expert out
//     of range), result_out_of_range (offset overflow): all permanent, no release can help.
//   PoolExpertCache::acquire() - invalid_argument (bad geometry, requested bytes != cached bytes,
//     payload+page > whole pool capacity), not_enough_memory (metadata alloc): permanent;
//     reader errors (invalid_argument / result_out_of_range / not_supported /
//     operation_not_supported / not_connected / io_error, see bounded_direct_reader.cpp): the file
//     or the scratch refuses - permanent for this request.
//   What is left is the pool's own pool_alloc(): invalid_argument, result_out_of_range,
//     not_enough_memory, and no_buffer_space.  The cache has already dropped EVERY reference it
//     owns (its LRU loop) before it returns pool_alloc's ec, so a no_buffer_space that reaches the
//     adapter means "the free space is pinned by leases the cache does not own" - i.e. borrowers
//     and/or another copy window.  Those are exactly the holders that release() and the DMA
//     completion sweep free.  Hence: transient.
//
// Known limit of the classification: a blob that can NEVER fit (its page-rounded payload is larger
// than the usable space) also reports no_buffer_space.  acquire_admit() rejects that case up front
// from the store's own geometry, so it does not burn the window.
bool admit_refusal_is_transient(const std::error_code& ec) {
    return ec == std::errc::no_buffer_space;
}

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
    // acquire_admit() is one acquire() call with the gate OFF; with it ON it is
    // the bounded retry (see the header).  No adapter mutex is held here, so a
    // wait can never block the release that would clear the refusal.
    const std::error_code ec = acquire_admit(layer, expert, lease);
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
    if (admit_retry_enabled()) return blob_stable_admit(layer, expert);
    std::lock_guard<std::mutex> lk(mu_);
    borrow_locked(layer, expert);
    auto it = borrowers_.find(key_of(layer, expert));
    if (it == borrowers_.end() || !it->second.lease.valid()) return nullptr;
    return static_cast<const uint8_t*>(it->second.lease.data());
}

int PoolExpertSourceAdapter::blob_parts(int64_t layer, int64_t expert, BlobPart* out) {
    if (admit_retry_enabled()) return blob_parts_admit(layer, expert, out);
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

std::error_code PoolExpertSourceAdapter::acquire_admit(int64_t layer, int64_t expert,
                                                       PoolLease& out) const {
    std::error_code ec = store_.acquire(layer, expert, out);
    // Gate OFF: exactly the one acquire() above, refusal returned at once.  This
    // is the whole behaviour before the retry existed.
    if (!ec || !admit_retry_enabled()) return ec;
    if (!admit_refusal_is_transient(ec)) return ec;   // permanent cause: no retry

    // A payload the pool can never hold is not transient even though the store
    // reports exactly the same code for it (the pool refuses after the cache has
    // dropped every reference it owns).  Ask the store's own geometry once,
    // before spending the window: a blob larger than the usable pool is a
    // permanent refusal.
    const PoolExpertStore::Stats s = store_.stats();
    const uint64_t bytes = store_.blob_bytes(layer);
    if (bytes == 0 || (s.usable_bytes > 0 && bytes > s.usable_bytes)) return ec;

    // Bounded twice: kAdmitRetryAttempts acquire calls in total, and a hard
    // kAdmitRetryDeadlineUs wall clock for the whole call.  The backoff doubles
    // (0.5, 1, 2, 4, 8, 8 ... ms) and is clamped to the remaining budget, so the
    // call never oversleeps its deadline and never sleeps without a bound.
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::microseconds(kAdmitRetryDeadlineUs);
    int64_t backoff_us = 500;
    for (unsigned attempt = 1; attempt < kAdmitRetryAttempts; ++attempt) {
        const auto now = std::chrono::steady_clock::now();
        if (now >= deadline) break;
        const auto left = std::chrono::duration_cast<std::chrono::microseconds>(deadline - now);
        const auto wait = std::min(left, std::chrono::microseconds(backoff_us));
        std::this_thread::sleep_for(wait);   // no adapter mutex is held here
        ec = store_.acquire(layer, expert, out);
        if (!ec) return ec;
        if (!admit_refusal_is_transient(ec)) return ec;
        backoff_us = std::min<int64_t>(backoff_us * 2, 8000);
    }
    return ec;
}

bool PoolExpertSourceAdapter::borrow_admit(int64_t layer, int64_t expert) {
    const int64_t key = key_of(layer, expert);
    {
        // Fast path under the lock: a live borrower needs no pool admission and
        // therefore no wait.
        std::lock_guard<std::mutex> lk(mu_);
        auto it = borrowers_.find(key);
        if (it != borrowers_.end()) {
            ++it->second.count;
            return it->second.lease.valid();
        }
    }
    // Miss: admit with NO adapter mutex held, so a wait cannot block release(),
    // sweep_completed_locked() or note_async_read()'s hook - the three things
    // that free the lease this refusal is waiting for.
    PoolLease lease;
    const std::error_code ec = acquire_admit(layer, expert, lease);
    if (ec || !lease.valid()) return false;
    std::lock_guard<std::mutex> lk(mu_);
    auto it = borrowers_.find(key);
    if (it != borrowers_.end()) {
        // Another thread admitted the same pair while this one waited: keep its
        // lease and let ours go.
        ++it->second.count;
        return it->second.lease.valid();
    }
    PoolBorrower b;
    b.count = 1;
    b.lease = std::move(lease);
    borrowers_.emplace(key, std::move(b));
    return true;
}

const uint8_t* PoolExpertSourceAdapter::blob_stable_admit(int64_t layer, int64_t expert) {
    if (!borrow_admit(layer, expert)) return nullptr;
    std::lock_guard<std::mutex> lk(mu_);
    auto it = borrowers_.find(key_of(layer, expert));
    if (it == borrowers_.end() || !it->second.lease.valid()) return nullptr;
    return static_cast<const uint8_t*>(it->second.lease.data());
}

int PoolExpertSourceAdapter::blob_parts_admit(int64_t layer, int64_t expert, BlobPart* out) {
    if (out == nullptr) return 0;
    if (!borrow_admit(layer, expert)) return 0;
    std::lock_guard<std::mutex> lk(mu_);
    auto it = borrowers_.find(key_of(layer, expert));
    if (it == borrowers_.end() || !it->second.lease.valid()) return 0;
    out[0] = BlobPart{static_cast<const uint8_t*>(it->second.lease.data()),
                      store_.blob_bytes(layer), 0};
    return 1;
}

#if defined(STRATA_RAM_POOL_TEST)
void pool_expert_source_adapter_set_admit_retry_for_test(int enabled) {
    g_admit_retry_override = (enabled < 0) ? -1 : (enabled != 0 ? 1 : 0);
}
#endif

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
