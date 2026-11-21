// src/core/pool_expert_source_adapter.cpp - PoolExpertStore-backed ExpertSource
// adapter (CPU-testable; the CUDA event implementation is supplied by Task 2,
// so this TU compiles without CUDA).

#include "strata/core/pool_expert_source_adapter.hpp"

#include <algorithm>
#include <atomic>
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

// ---- process-wide retry-sleep budget ---------------------------------------
//
// A fixed one-second window with at most kRetrySleepBudgetUsPerSecond of sleep spent in it, shared by
// every thread and every adapter.  This is what turns "bounded per call" into "bounded by
// construction": the prompt sweep can ask 24,576 times and still add at most 100 ms of sleep per
// second of wall clock.  Two relaxed atomics, no lock: the refusal path is rare, and even a refusal
// storm only does one CAS pair per admission - it never becomes a decode-hot-path contention point
// (the gate-off path does not touch it at all).
int64_t now_us() {
    return std::chrono::duration_cast<std::chrono::microseconds>(
               std::chrono::steady_clock::now().time_since_epoch()).count();
}

// One line per second per category: a single relaxed compare-exchange, no lock, no allocation.  Two separate
// gates so the refusal line and the many-borrowers guard cannot starve each other.
struct SecondGate {
    std::atomic<int64_t> last_us{0};
    bool take() {
        const int64_t now = now_us();
        int64_t prev = last_us.load(std::memory_order_relaxed);
        if (now - prev < 1000000) return false;
        return last_us.compare_exchange_strong(prev, now, std::memory_order_relaxed);
    }
};
SecondGate g_refusal_gate;       // the refusal evidence line
SecondGate g_borrow_table_gate;  // the many-borrowers guard

// The guard's line: more leases than a request's working set with NO async cycle behind any of them.  The
// count is of the leak shape (no note issued, not completed), not of the table - a startup fill legitimately
// holds thousands of borrowers, and the adapt round marks its entries pending.
constexpr size_t kManyStaleBorrowers = 4000;

std::atomic<int64_t> g_retry_window_start_us{0};
std::atomic<int64_t> g_retry_used_us{0};

// Grants up to `want_us` of sleep, or 0 when this window's budget is gone (the caller must then fail
// at once, having already made its one attempt).
int64_t retry_take_budget_us(int64_t want_us) {
    if (want_us <= 0) return 0;
    const int64_t now = now_us();
    int64_t start = g_retry_window_start_us.load(std::memory_order_relaxed);
    if (now - start >= 1000000) {   // roll the window (one thread wins; the rest see the new start)
        if (g_retry_window_start_us.compare_exchange_strong(start, now, std::memory_order_relaxed))
            g_retry_used_us.store(0, std::memory_order_relaxed);
    }
    for (int spin = 0; spin < 8; ++spin) {
        int64_t used = g_retry_used_us.load(std::memory_order_relaxed);
        if (used >= PoolExpertSourceAdapter::kRetrySleepBudgetUsPerSecond) return 0;
        const int64_t take = std::min(want_us,
            PoolExpertSourceAdapter::kRetrySleepBudgetUsPerSecond - used);
        if (g_retry_used_us.compare_exchange_weak(used, used + take,
                                                  std::memory_order_relaxed))
            return take;
    }
    return 0;   // lost every CAS race: treat as empty rather than spin on the decode path
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
    if (ec || !lease.valid()) {
        report_copy_refusal(layer, expert, ec);
        note_refusal("copy_blob", layer, expert);
        return false;
    }
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

bool PoolExpertSourceAdapter::available(int64_t layer, int64_t expert) {
    // The planner asks a question, not for a loan: acquire and let the lease go at once.  The store's own cache
    // reference keeps a hit cached and a miss it read stays cached too, so the answer is the same one a borrow
    // would give - with no borrower and nothing pinned.  Deliberately NOT the retry path: a probe must never
    // wait, and a refusal here is a plain "not now".
    PoolLease lease;
    const std::error_code ec = store_.acquire(layer, expert, lease);
    return !ec && lease.valid();
}

const uint8_t* PoolExpertSourceAdapter::blob_stable(int64_t layer, int64_t expert) {
    const uint8_t* p = nullptr;
    if (admit_retry_enabled()) {
        p = borrow_admit(layer, expert);
    } else {
        std::lock_guard<std::mutex> lk(mu_);
        borrow_locked(layer, expert);
        auto it = borrowers_.find(key_of(layer, expert));
        if (it != borrowers_.end() && it->second.lease.valid())
            p = static_cast<const uint8_t*>(it->second.lease.data());
    }
    note_borrow_table(layer, expert);
    return p;
}

int PoolExpertSourceAdapter::blob_parts(int64_t layer, int64_t expert, BlobPart* out) {
    int n = 0;
    if (admit_retry_enabled()) {
        n = blob_parts_admit(layer, expert, out);
    } else {
        std::lock_guard<std::mutex> lk(mu_);
        borrow_locked(layer, expert);
        auto it = borrowers_.find(key_of(layer, expert));
        if (it != borrowers_.end() && it->second.lease.valid() && out != nullptr) {
            out[0] = BlobPart{static_cast<const uint8_t*>(it->second.lease.data()),
                              store_.blob_bytes(layer), 0};
            n = 1;
        }
    }
    note_borrow_table(layer, expert);
    return n;
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
                                                       PoolLease& out) {
    std::error_code ec = store_.acquire(layer, expert, out);
    // Gate OFF: exactly the one acquire() above, refusal returned at once.  This
    // is the whole behaviour before the retry existed: no sleep, no sweep, no
    // store stats, no budget.
    if (!ec || !admit_retry_enabled()) return ec;
    if (!admit_refusal_is_transient(ec)) return ec;   // permanent cause: no retry

    // A payload the pool can never hold is not transient even though the store
    // reports exactly the same code for it (the pool refuses after the cache has
    // dropped every reference it owns).  Ask the store's own geometry once,
    // before spending the window: a blob larger than the usable pool is a
    // permanent refusal.
    const uint64_t bytes = store_.blob_bytes(layer);
    if (bytes == 0) return ec;
    {
        const PoolExpertStore::Stats s = store_.stats();
        if (s.usable_bytes > 0 && bytes > s.usable_bytes) return ec;
    }

    // Bounded per admission (attempts + deadline), per process (the sleep budget)
    // and per wait (every sleep is clamped to both).  The sweep between attempts
    // is what actually frees a self-pinned pool: during single-threaded decode the
    // pairs holding the pool are this caller's earlier borrowers, completed (their
    // DMA event landed) but unswept, and a thread blocked here runs no
    // note_async_read()/release() - nobody else would ever sweep them.  Recycling
    // them is the documented contract: sweep_completed_locked() drops only
    // completed borrowers, and callers re-borrow per use (see its comment and
    // generate.cpp's blob_stable -> note_async_read per pair).
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::microseconds(kAdmitRetryDeadlineUs);
    int64_t backoff_us = kAdmitRetryFirstBackoffUs;
    for (unsigned attempt = 1; attempt < kAdmitRetryAttempts; ++attempt) {
        {
            std::lock_guard<std::mutex> lk(mu_);
            sweep_completed_locked();   // free our own landed leases; never under a wait
        }
        ec = store_.acquire(layer, expert, out);
        if (!ec) return ec;
        if (!admit_refusal_is_transient(ec)) return ec;
        const auto now = std::chrono::steady_clock::now();
        if (now >= deadline) break;
        const auto left = std::chrono::duration_cast<std::chrono::microseconds>(deadline - now);
        const int64_t want = std::min<int64_t>(backoff_us, left.count());
        const int64_t granted = retry_take_budget_us(want);
        if (granted <= 0) break;   // this second's sleep budget is gone: fail at once
        std::this_thread::sleep_for(std::chrono::microseconds(granted));   // no mu_ held
        backoff_us = std::min<int64_t>(backoff_us * 2, kAdmitRetryMaxBackoffUs);
    }
    return ec;
}

const uint8_t* PoolExpertSourceAdapter::borrow_admit(int64_t layer, int64_t expert) {
    const int64_t key = key_of(layer, expert);
    {
        // Fast path under the lock: a live borrower needs no pool admission and
        // therefore no wait.
        std::lock_guard<std::mutex> lk(mu_);
        auto it = borrowers_.find(key);
        if (it != borrowers_.end()) {
            ++it->second.count;
            // Re-arm the async state for THIS lease: a borrow does not inherit an earlier one's completion,
            // and a sweep from another thread must not erase a borrower whose copy is now in flight
            // (note_async_read() re-arms the same way at issue time).
            it->second.completed = false;
            it->second.pending_event = false;
            return it->second.lease.valid()
                       ? static_cast<const uint8_t*>(it->second.lease.data()) : nullptr;
        }
    }
    // Miss: admit with NO adapter mutex held, so a wait cannot block release(),
    // sweep_completed_locked() or note_async_read()'s hook - the three things
    // that free the lease this refusal is waiting for.
    PoolLease lease;
    const std::error_code ec = acquire_admit(layer, expert, lease);
    if (ec || !lease.valid()) {
        note_refusal("borrow", layer, expert);
        return nullptr;
    }
    // Register and read the pointer under ONE lock: a sweep by another thread in
    // between must not turn a successful admission into a null pointer.
    std::lock_guard<std::mutex> lk(mu_);
    auto it = borrowers_.find(key);
    if (it != borrowers_.end()) {
        // Another thread admitted the same pair while this one waited: keep its
        // lease and let ours go.
        ++it->second.count;
        it->second.completed = false;   // as above: this lease starts its own cycle
        it->second.pending_event = false;
        return it->second.lease.valid()
                   ? static_cast<const uint8_t*>(it->second.lease.data()) : nullptr;
    }
    const uint8_t* p = static_cast<const uint8_t*>(lease.data());
    PoolBorrower b;
    b.count = 1;
    b.completed = false;
    b.pending_event = false;
    b.lease = std::move(lease);
    borrowers_.emplace(key, std::move(b));
    return p;
}

int PoolExpertSourceAdapter::blob_parts_admit(int64_t layer, int64_t expert, BlobPart* out) {
    if (out == nullptr) return 0;
    const uint8_t* p = borrow_admit(layer, expert);
    if (p == nullptr) return 0;
    out[0] = BlobPart{p, store_.blob_bytes(layer), 0};
    return 1;
}

#if defined(STRATA_RAM_POOL_TEST)
void pool_expert_source_adapter_set_admit_retry_for_test(int enabled) {
    g_admit_retry_override = (enabled < 0) ? -1 : (enabled != 0 ? 1 : 0);
}

void pool_expert_source_adapter_reset_retry_budget_for_test() {
    g_retry_window_start_us.store(now_us(), std::memory_order_relaxed);
    g_retry_used_us.store(0, std::memory_order_relaxed);
}

int64_t pool_expert_source_adapter_retry_used_us_for_test() {
    return g_retry_used_us.load(std::memory_order_relaxed);
}
#endif

void PoolExpertSourceAdapter::note_refusal(const char* path, int64_t layer, int64_t expert) const {
    // One line per second for the whole process: the rate gate is a single relaxed
    // compare-exchange, so even a refusal storm pays one atomic per refusal.
    if (!g_refusal_gate.take()) return;
    // Snapshot under mu_, print outside it (fprintf may take a libc lock).
    size_t nb = 0, nc = 0, np = 0;
    {
        std::lock_guard<std::mutex> lk(mu_);
        nb = borrowers_.size();
        for (const auto& kv : borrowers_) {
            if (kv.second.completed) ++nc;
            if (kv.second.pending_event) ++np;
        }
    }
    const PoolExpertStore::Stats st = store_.stats();
    std::fprintf(stderr,
                 "strata: managed pool: %s refused layer %lld expert %lld: borrowers=%zu completed=%zu "
                 "pending=%zu resident_pairs=%llu evictions=%llu\n",
                 path, (long long) layer, (long long) expert, nb, nc, np,
                 (unsigned long long) st.resident_pairs, (unsigned long long) st.evictions);
}

void PoolExpertSourceAdapter::note_borrow_table(int64_t layer, int64_t expert) const {
    // Guard against a silent return of the pin leak.  The shape that matters is NOT the table's size: a fill
    // legitimately holds many borrowers, and the adapt round marks its entries pending.  It is the count of
    // entries that are in NO async cycle at all - no note ever issued (pending_event false) and not completed -
    // because a caller that borrows for a copy notes or releases, and one that does neither leaves exactly those.
    // One atomic per borrow; the scan (and the lock) run only when the one-second gate opens.
    if (!g_borrow_table_gate.take()) return;
    size_t nb = 0, stale = 0;
    {
        std::lock_guard<std::mutex> lk(mu_);
        nb = borrowers_.size();
        for (const auto& kv : borrowers_)
            if (!kv.second.pending_event && !kv.second.completed) ++stale;
    }
    if (stale <= kManyStaleBorrowers) return;   // in or past an async cycle: not this shape
    const PoolExpertStore::Stats st = store_.stats();
    std::fprintf(stderr,
                 "strata: managed pool: %zu live borrowers, %zu of them with no copy in flight and none completed "
                 "(the borrow that crossed the line: layer %lld expert %lld) - a caller is borrowing without "
                 "noting or releasing; resident_pairs=%llu evictions=%llu\n",
                 nb, stale, (long long) layer, (long long) expert,
                 (unsigned long long) st.resident_pairs, (unsigned long long) st.evictions);
}

void PoolExpertSourceAdapter::borrow_locked(int64_t layer, int64_t expert) {
    // The sweep is only safe because of the invariant in the header, over borrow_admit(): a note's event is
    // recorded after its copy is queued on the same stream, and a re-borrow re-arms the entry - so no sweep can
    // free a lease that a queued, unnoted copy still reads.  There is no per-lease accounting to check that
    // beyond the flags below; a borrow dropped without a note or a release is simply never swept.
    const int64_t key = key_of(layer, expert);
    auto it = borrowers_.find(key);
    if (it != borrowers_.end()) {
        ++it->second.count;
        // Re-arm the async state for THIS lease (the twin of borrow_admit's): a borrow does not inherit an
        // earlier one's completion, so a sweep cannot erase a borrower whose copy is in flight.
        it->second.completed = false;
        it->second.pending_event = false;
        return;
    }
    PoolLease lease;
    const std::error_code ec = store_.acquire(layer, expert, lease);
    if (ec || !lease.valid()) return;  // no_buffer_space -> nullptr, no retry
    PoolBorrower b;
    b.count = 1;
    b.completed = false;
    b.pending_event = false;
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
