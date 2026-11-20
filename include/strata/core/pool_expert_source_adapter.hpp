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

    ~PoolExpertSourceAdapter() override { drop_all_locked(); }

    /// Borrow (acquire + refcount) and return lease.data() (stable pointer).
    const uint8_t* blob_stable(int64_t layer, int64_t expert) override;

    /// Alias of blob_stable (pointer stable for async copy).
    const uint8_t* blob(int64_t layer, int64_t expert) override { return blob_stable(layer, expert); }

    /// ONE part: the whole lease as a single dense buffer at=0.
    int blob_parts(int64_t layer, int64_t expert, BlobPart* out) override;

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
