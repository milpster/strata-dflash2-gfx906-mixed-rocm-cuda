// include/strata/core/pool_expert_source_adapter.hpp - P2.S3/P2.S5: the
// PoolExpertStore-backed ExpertSource adapter (CPU-testable, default OFF).
//
// Adapts PoolExpertStore leases into the ExpertSource contract so legacy
// consumers (generate.cpp's three adapted seams) can read pool payloads while
// async DMA keeps lease.data() live until the queued copy completes.
//
// NOTHING includes this from engine code yet (default OFF). Task 2 wires it at
// the generate.cpp srcp selection site.
#pragma once

#include <cstdint>
#include <functional>
#include <mutex>
#include <system_error>
#include <unordered_map>
#include <utility>
#include <vector>

#include "strata/core/expert_source.hpp"
#include "strata/core/pool_expert_store.hpp"

namespace strata::core {

/// Completion hook for async-note recording. `stream` is the caller's stream
/// (a cudaStream_t in the engine build; opaque here). The hook records the
/// completion event; an empty hook means "never complete via injection" -
/// borrowers are then retained until release/teardown (correctness never
/// depends on injection).
using AsyncNoteHook = std::function<void(int64_t layer, int64_t expert, void* stream)>;

/// One (layer, expert) borrower: refcount + the pool lease that pins the blob.
struct PoolBorrower {
    int64_t count = 0;
    PoolLease lease;
    bool pending_event = false;   // note_async_read issued, not yet swept
    bool completed = false;       // async event finished (test/engine hook side)
};

/// Adapter: serves pool payloads as an ExpertSource.  Every blob() / blob_stable()
/// call borrows (acquires + refcounts) so the raw pointer stays valid until the
/// borrower is dropped at release() or on note_async_read sweep completion.
class PoolExpertSourceAdapter : public ExpertSource {
public:
    /// `store` must outlive this adapter and all async DMA reading lease.data().
    /// `hook` records completion events (engine supplies CUDA events; tests a fake).
    explicit PoolExpertSourceAdapter(PoolExpertStore& store,
                                     AsyncNoteHook hook = {}) noexcept
        : store_(store), hook_(std::move(hook)) {}

    PoolExpertSourceAdapter(const PoolExpertSourceAdapter&) = delete;
    PoolExpertSourceAdapter& operator=(const PoolExpertSourceAdapter&) = delete;

    /// Simulate the async event completing for this borrower (CPU test hook;
    /// the engine's CUDA build drives completion through the async-note hook
    /// instead).  Marks the borrower so the next sweep drops it.
    void mark_completed(int64_t layer, int64_t expert);

    /// Inspection hook (tests/diagnostics): true while a borrower entry for
    /// (layer, expert) is live in the table.  Not used by engine code.
    bool borrowed(int64_t layer, int64_t expert) const;

    ~PoolExpertSourceAdapter() override {
        std::lock_guard<std::mutex> lk(mu_);
        drop_all_locked();
    }

    /// Borrow (acquire + refcount) and return lease.data() (stable pointer).
    const uint8_t* blob_stable(int64_t layer, int64_t expert) override;

    /// True when the store can produce the pair now, asked without a borrow: the lease is dropped the moment the
    /// answer is known, so the store's own cache reference does the caching and nothing is pinned.  Never waits -
    /// a planner probe must not spend the decode path's retry budget - so a refusal answers false at once.
    bool available(int64_t layer, int64_t expert) override;

    /// Alias of blob_stable (pointer stable for async copy).
    const uint8_t* blob(int64_t layer, int64_t expert) override { return blob_stable(layer, expert); }

    /// ONE part: the whole lease as a single dense buffer at=0.
    int blob_parts(int64_t layer, int64_t expert, BlobPart* out) override;

    /// CS-T: the pool is a fixed budget and the prompt path's sweep touches EVERY expert of EVERY layer
    /// (48 x 512 = 24,576 pairs, 46.84 GiB on the iq3xxs-ud pack).  Lending each blob as a borrower that
    /// lives until the request ends ran the 36 GiB pool dry around layer 36; `blob_stable` then returned
    /// null and prefill stopped with "the engine reported an error: prefill: expert source has no blob".
    /// A managed-pool blob is therefore declared transient: the prompt path copies it into its own staging
    /// buffer through `copy_blob`/`copy_blobs`, and the store's LRU - not a growing borrower table - decides
    /// what stays resident.  The decode path is untouched: blob()/blob_stable() still borrow for as long as
    /// the async copy needs the pointer.
    bool transient(int64_t layer, int64_t expert) const override { (void)layer; (void)expert; return true; }

    /// The copy path's lease window: `copy_blobs` holds at most this many leases at once before it copies
    /// and drops them, so the copy path's pool footprint is bounded by (stager threads x window) blobs -
    /// never by the size of the sweep.  `copy_blob` holds one.
    static constexpr size_t kCopyLeaseWindow = 4;

    /// The blob's bytes into `dst` (`store_.blob_bytes(layer)` of them).  A pool HIT is a memcpy from the
    /// cached payload (no file read); a MISS reads through the store's bounded reader, exactly as
    /// blob_stable() does.  The lease is dropped before this returns, so no borrower is created and no
    /// caller can make the pool run dry.  Thread-safe: the store serializes its own admission.
    /// False (with one stderr line) only when the store itself refuses - it has already run its own
    /// eviction, which is the honest "no room" case.  With STRATA_RAM_POOL_ADMIT_RETRY set, a refusal
    /// that another thread's release can clear is retried first, inside a fixed budget.
    bool copy_blob(int64_t layer, int64_t expert, uint8_t* dst) override;

    /// `n` blobs at once, byte-identical to `n` calls of copy_blob.  Uses the window above; a pair the
    /// pool refuses while the window is held is retried alone (the least the pool can be asked for)
    /// before this reports false.
    bool copy_blobs(const int32_t* layers, const int32_t* experts, uint8_t* const* dst, size_t n) override;

    // ---- admission retry (OPT-IN, default OFF) ------------------------------
    // Environment gate, read once per process: `STRATA_RAM_POOL_ADMIT_RETRY`.
    // Unset, empty, `0`, `off`, `false` or `no` (any case, surrounding blanks ignored) = OFF, and
    // then every admission is exactly one store_.acquire() with the refusal returned at once - the
    // behaviour before this retry existed: no sleep, no sweep, no store stats, no extra lock.  Any
    // other value = ON.
    //
    // Why it exists: a lease holder makes the store's own LRU unable to free that pair, and the pool
    // refuses with no_buffer_space.  Measured on 2026-11-21: two long-context decodes (53,717 and
    // 64,507 prompt tokens) died there with "the expert source could not produce a blob".
    //
    // The holders that matter are the caller's OWN earlier borrowers: single-threaded decode leaves
    // pairs completed (their DMA event landed) but unswept, because only that thread runs the sweep.
    // A thread blocked in acquire_admit() runs no note_async_read()/release(), so the retry drives
    // its own sweep between attempts instead of waiting for a release that will never come.
    //
    // Bounded three times:
    //   * per admission: kAdmitRetryAttempts acquire calls in total and a hard
    //     kAdmitRetryDeadlineUs wall clock;
    //   * per process: at most kRetrySleepBudgetUsPerSecond of retry sleep in any one second of wall
    //     clock (a fixed-window token bucket over all threads and all adapters), so the total added
    //     stall cannot exceed 10% of wall clock however many pairs refuse - the prompt sweep's
    //     24,576 pairs can no longer add 24,576 x 20 ms.  Once the bucket is empty an admission is
    //     one acquire() and fails at once.
    //   * every sleep is clamped to the deadline AND to what the bucket grants; no sleep happens
    //     under mu_ (see acquire_admit()).
    static constexpr unsigned kAdmitRetryAttempts = 8;         // first try + 7 retries
    static constexpr int64_t kAdmitRetryDeadlineUs = 20000;    // 20 ms per admission, hard cap
    static constexpr int64_t kAdmitRetryFirstBackoffUs = 500;  // doubling, capped below
    static constexpr int64_t kAdmitRetryMaxBackoffUs = 8000;
    static constexpr int64_t kRetrySleepBudgetUsPerSecond = 100000;  // 100 ms of sleep per second

    /// Pool memory is mlocked; returns true (UNMEASURED perf: no speed claim may cite this path).
    bool pinned(int64_t layer, int64_t expert) const override { (void)layer; (void)expert; return true; }

    /// Drop the borrower immediately (caller confirms the copy finished).
    uint64_t release(int64_t layer, int64_t expert) override;

    /// Record async read on `stream`, mark pending-event, sweep completed,
    /// drop only completed borrowers. Never drops at issue time.
    void note_async_read(const uint8_t* blob, void* stream) override;

private:
    int64_t key_of(int64_t layer, int64_t expert) const noexcept {
        return (layer << 32) | (expert & 0xFFFFFFFFll);
    }

    /// Admission behind the retry gate.  MUST be called with mu_ NOT held: it takes mu_ for the
    /// sweep between attempts, and no wait ever happens while holding it.  With the gate OFF this is
    /// one store_.acquire() and nothing else.
    std::error_code acquire_admit(int64_t layer, int64_t expert, PoolLease& out);

    /// Retry-gated twin of borrow_locked(): admits outside mu_, then registers the borrower and
    /// returns its data pointer under the same lock (a concurrent sweep must not turn a successful
    /// admission into a null pointer).  nullptr when the admission failed.  mu_ must NOT be held.
    const uint8_t* borrow_admit(int64_t layer, int64_t expert);

    /// Retry-gated twin of the blob_parts() body (gate ON only).
    int blob_parts_admit(int64_t layer, int64_t expert, BlobPart* out);

    void borrow_locked(int64_t layer, int64_t expert);
    void drop_locked(int64_t key);
    void drop_all_locked();
    void sweep_completed_locked();

    /// The refusal evidence line: at most one per second for the whole process, one fprintf, no allocation.
    /// The snapshot (borrower counts, completion and pending flags) is taken under mu_ and printed after it
    /// is released, so the line says what the pool held at the moment it refused.  Called on every refusal
    /// of copy_blob()/borrow_admit(), gate ON or OFF: this is the measurement that tells a later death
    /// whether the pool was still pinned by unswept borrowers.
    void note_refusal(const char* path, int64_t layer, int64_t expert) const;

    /// The leak guard: once per second at most, and only when the borrower table has grown past 4,000 entries
    /// and NOT ONE entry is completed or pending, print what it holds and the pair whose borrow
    /// crossed the line.  That shape - a table far larger than a request's working set with no async state on
    /// any entry - is a caller borrowing without ever noting or releasing, which is what emptied the pool.
    /// Cheap: one relaxed compare-exchange per borrow, and the scan runs only when that gate opens.
    void note_borrow_table(int64_t layer, int64_t expert) const;

    PoolExpertStore& store_;
    AsyncNoteHook hook_;
    mutable std::mutex mu_;
    std::unordered_map<int64_t, PoolBorrower> borrowers_;   // key -> borrower
};

#if defined(STRATA_RAM_POOL_TEST)
/// Test hook, same convention as PoolExpertCache's pre-emplace fault hook: force
/// the admission-retry gate without touching the process environment, which is
/// otherwise read once per process.  -1 restores "follow the environment".
void pool_expert_source_adapter_set_admit_retry_for_test(int enabled);

/// Test hooks for the process-wide retry-sleep budget: start a fresh one-second
/// window (spend 0), and read how much of the current window's budget the process
/// has used.  Neither touches production code paths.
void pool_expert_source_adapter_reset_retry_budget_for_test();
int64_t pool_expert_source_adapter_retry_used_us_for_test();
#endif

}  // namespace strata::core
