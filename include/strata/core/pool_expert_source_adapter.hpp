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
    /// eviction, which is the honest "no room" case.
    bool copy_blob(int64_t layer, int64_t expert, uint8_t* dst) override;

    /// `n` blobs at once, byte-identical to `n` calls of copy_blob.  Uses the window above; a pair the
    /// pool refuses while the window is held is retried alone (the least the pool can be asked for)
    /// before this reports false.
    bool copy_blobs(const int32_t* layers, const int32_t* experts, uint8_t* const* dst, size_t n) override;

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

    void borrow_locked(int64_t layer, int64_t expert);
    void drop_locked(int64_t key);
    void drop_all_locked();
    void sweep_completed_locked();

    PoolExpertStore& store_;
    AsyncNoteHook hook_;
    mutable std::mutex mu_;
    std::unordered_map<int64_t, PoolBorrower> borrowers_;   // key -> borrower
};

}  // namespace strata::core
