#pragma once
// Foresight swap space (opt-in: STRATA_FS_SLOTS > 0; unset = nothing here exists and the engine is unchanged).
//
// A few VRAM slots per layer on each card, apart from the expert cache, refilled on their own copy stream with experts
// that just missed.  The verify pool asks, for every expert it would otherwise give the CPU, whether that expert sits
// in a slot whose copy has landed; if so the expert becomes a plain GPU group (kind 0) pointing at the slot, exactly
// like a cache hit - the grouped kernel reads whatever address the plan carries, so no kernel changes.  The host pool
// decides and the GPU follows the plan, so both always agree on who computes an expert.
//
// Safety: a slot is refilled only when every window that may have read it has finished: each plan reference stamps
// the slot with the count of finished verify windows, and a slot is reusable once `depth` more windows finished
// (STRATA_FS_DEPTH, default 4; --pipeline-windows 2 keeps at most two windows in flight).  A copy that has not landed
// is simply not used (the CPU computes the expert as before).  Not with STRATA_VERIFY_DEVICE_PLAN (the device would
// plan resident layers without the host).
//
// With STRATA_FS_AHEAD=1 (default 1 when the swap space is on) the router look-ahead (RouterLookahead, one layer ahead on the
// previous layer's MoE input) also feeds the slots, not only the page warming of the file tier.
//
// Switches: STRATA_FS_SLOTS (per layer and card), STRATA_FS_BUDGET (copies per card per finished window, default 32),
// STRATA_FS_ADMIT (misses within the last 4 windows before an expert is copied, default 1), STRATA_FS_DEPTH,
// STRATA_FS_HELPER_SHADOW=1 (slots also serve pairs a helper tier holds; default 0), STRATA_FS_VERIFY=1 (test mode:
// every slot is read back and compared with its source the first time it is used).
//
// STRATA_STAGE_OFFTHREAD=1 (needs STRATA_FS_HELPER_SHADOW with its staging ring; default 0 = the begin seam still
// stages inline, exactly as before): note_miss only queues the (layer, expert) demand and returns at once; a stager
// thread reads the bytes into the ring (stage_blob) off the request thread, so the begin seam never blocks on a pack
// read or a helper D2H.  The stager waits out a full ring (about 1 s in 2 ms steps) instead of rejecting, so "ring
// full" counts sustained exhaustion only, and a demand queue deeper than 16384 drops the note like the inline path's
// ring-full refusal would.  Outputs never change: a slot either lands in time and serves the same bytes, or the row
// keeps today's helper/CPU fallback.
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace strata::core {

class ExpertSource;

struct ForesightSwap {
    struct Slot {
        int32_t expert = -1;
        bool ready = false;                  ///< the copy has landed (dispatch thread only)
        bool loading = false;                ///< a copy is assigned and not yet seen landed (dispatch thread only)
        std::atomic<bool> issued{false};     ///< the filler has queued the copy and recorded `ev`
        uint64_t last_ref = 0;               ///< finished-window count when a plan last pointed here
        unsigned long long dptr = 0;         ///< the slot's device address
        const uint8_t* src = nullptr;        ///< the host blob being copied in
        int ring_idx = -1;                   ///< `src` is the ring entry with this index (helper shadow), -1 = the arena's own blob
        uint64_t bytes = 0;
        void* ev = nullptr;                  ///< cudaEvent_t on the layer's card
    };

    int slots = 0, budget = 32, admit = 1, depth = 4;
    /// STRATA_FS_HELPER_SHADOW=1 (needs STRATA_FS_SLOTS; default 0 = the engine unchanged): the dispatch
    /// serves a landed slot BEFORE a helper tier's holds(), so the engine's own card computes the pair
    /// instead of sending the row over the helper RPC.  Also the gate for the staging ring that lets the
    /// filler copy pairs the RAM arena dropped (helper-held, MADV_DONTNEED) - see `stage_blob`.
    bool helper_shadow = false;
    /// STRATA_FS_RING (helper shadow only): entries in the pinned staging ring.  0 = none allocated.
    int ring_want = 0;
    /// STRATA_STAGE_OFFTHREAD=1 (needs the helper shadow's ring; default 0): the begin seam's note_miss only
    /// queues the demand; the stager thread runs the ring staging (stage_miss) off the request thread.
    bool stage_offthread = false;
    /// The helper shadow's blob source, set by the driver before the first window: stage one pair's
    /// blob bytes into `dst` and return true - a helper tier's engine-side cache
    /// (RemoteExperts::copy_cached_blobs) first, else the pack files through the source's batched
    /// swap-in read.  The helper-ipc rig's socket proxy has no engine-side bytes, so there the pack
    /// files are the source (the same bytes the helper filled from).  Unset = a pair without a
    /// pinned arena blob is never copied into a slot, exactly as before.
    std::function<bool(int64_t, int32_t, uint8_t*)> stage_blob;
    int64_t n_layers = 0, n_expert = 0;
    std::unique_ptr<Slot[]> slot;            ///< n_layers x slots
    std::vector<int16_t> where;              ///< (layer, expert) -> slot in its layer, -1
    std::vector<uint8_t> miss_cnt;           ///< STRATA_FS_ADMIT > 1: recent misses per (layer, expert)
    std::vector<uint64_t> miss_at;
    std::vector<int> layer_card, layer_dev;
    std::vector<int> card_slots;             ///< allocatable slots per card (STRATA_FS_SLOTS "3,2"); `slots` is the stride
    std::vector<void*> card_stream;          ///< cudaStream_t per card
    std::vector<void*> card_mem;             ///< the slots' allocation per card
    std::vector<int> card_dev;
    std::vector<uint64_t> card_round;
    std::vector<int> card_used;
    std::atomic<uint64_t> completed{0};      ///< finished verify windows (the serve loops count them)
    std::atomic<bool> failed{false};
    // counters (dispatch thread)
    uint64_t hits = 0, copies = 0, busy = 0, over_budget = 0, pending = 0, predicted = 0;   // (under smu_)
    uint64_t staged = 0, ring_busy = 0, no_src = 0;   ///< helper shadow: ring staging outcomes (under smu_)
    std::atomic<uint64_t> stage_drops{0};   ///< off-thread stager: notes dropped because the queue was full
    bool verify = false;                     ///< STRATA_FS_VERIFY=1: read every landed slot back and compare (slow, tests)
    uint64_t verify_ok = 0, verify_bad = 0;
    uint64_t dropped = 0;                    ///< slots dropped because an exchange moved their expert's bytes (under smu_)
    static constexpr int16_t kHeld = -2;     ///< `where`: an exchange is moving this expert's bytes, no copy now (#2134)
    static constexpr int32_t kDropped = -2;  ///< Slot::expert: dropped by `hold`, reusable under the same depth rule

    /// Allocates the slots on each card and starts the filler.  `card_of_layer[l]` indexes `devices` (CUDA ids).
    /// `slots_per_card` (empty = `slots_per_layer` on every card) caps each card's own count, so a split
    /// rig can fund card0's layers while card1 stays on its thin margin; `slots` stays the array stride.
    bool init(int slots_per_layer, int64_t layers, int64_t experts, const std::vector<int>& card_of_layer,
              const std::vector<int>& devices, std::string& err, const std::vector<int>& slots_per_card = {});
    ~ForesightSwap();

    /// The pool, for a routed expert that is not in the cache: true and its device address when a landed slot holds it.
    bool take(int64_t layer, int32_t expert, unsigned long long& ptr);
    /// The pool, for an expert the CPU computes now: maybe copy it into a slot of its layer for the next windows.
    /// With the helper shadow on, also the demand feed for helper-claimed rows (the begin seam calls it per
    /// distinct claimed expert): a pair whose blob the source does not pin - helper-held, dropped from the
    /// RAM arena - is staged through `stage_blob` into the pinned ring first, then queued like any other.
    void note_miss(ExpertSource* src, int64_t layer, int32_t expert);
    /// The router look-ahead (any thread), for an expert the next layer's router predicts and no cache holds: maybe copy
    /// it into a slot of that layer now, so that the copy has landed when the layer's plan is made.  One prefetch design
    /// with note_miss (the same slots, budget and reuse rule); a prediction only ever moves bytes, the plan is still made
    /// by the host pool and the model's own router stays authoritative.
    void predict(ExpertSource* src, int64_t layer, int32_t expert);
    /// #2134: the adaptive tier moves this expert's bytes in the RAM copy (an exchange, its two halves or a rotation),
    /// so a copy issued from its old place may land holding another expert's bytes.  Its slot is dropped (never served;
    /// refilled only once `depth` windows finished, like any slot a window may still read; a copy in flight finishes
    /// first) and no copy of it is issued until `unhold`, when its bytes sit at their new place.  Any thread.
    void hold(int64_t layer, int32_t expert);
    void unhold(int64_t layer, int32_t expert);
    std::string report() const;
    /// The staging ring's entry count (boot line of foresight_swap_from_env; 0 = no ring).
    int ring_entries() const { return ring_n; }

private:
    void request_locked(ExpertSource* src, int64_t layer, int32_t expert, bool predicted);
    /// The first half of request_locked, without the blob: true when this pair is not on its way,
    /// the admit count passes (`count_miss` = bump it - the shadow's re-check after staging skips
    /// the bump) and the card's copy budget has room.
    bool gates_pass_locked(int64_t layer, int32_t expert, bool predicted_copy, bool count_miss);
    /// The second half: pick a reusable slot of the layer and queue its copy from `b` (an entry of
    /// the staging ring when `ring_idx` >= 0, else a blob the source pinned).  False when every slot
    /// of the layer is still within `depth` of a window that may read it.
    bool assign_locked(const uint8_t* b, int ring_idx, int64_t layer, int32_t expert, bool predicted_copy);
    /// The staging ring: `ring_acquire` hands out a free entry, `ring_release` returns one.  An
    /// entry stays checked out for the lifetime of the slot it filled (the filler's H2D reads it),
    /// so a slot's `ring_idx` is released only when the slot itself is reassigned.
    int ring_acquire();
    void ring_release(int idx);
    /// STRATA_STAGE_OFFTHREAD: one noted (layer, expert) waiting for the stager thread.
    struct StageReq {
        int64_t layer;
        int32_t expert;
    };
    /// note_miss's helper-shadow body: the gates, the ring entry, the bytes read outside every lock, the
    /// re-checked assign.  `wait_ring` (the stager thread) waits out a full ring instead of refusing it.
    void stage_miss(int64_t layer, int32_t expert, bool wait_ring);
    void stage_loop();
    std::mutex stage_mu_;                     ///< stage_queue_/stage_stop_
    std::condition_variable stage_cv_;
    std::deque<StageReq> stage_queue_;
    bool stage_stop_ = false;
    std::thread stager_;
    std::mutex smu_;                         ///< slot, where, card_* and the counters: the pool thread and the look-ahead thread
    std::mutex mu_;
    std::mutex ring_mu_;                     ///< ring_used
    std::condition_variable cv_;
    std::deque<int64_t> queue_;              ///< slot indices whose copy the filler issues
    bool stop_ = false;
    std::thread filler_;
    void fill_loop();
    int ring_n = 0;                          ///< 0 = no staging ring (shadow off or STRATA_FS_RING=0)
    uint64_t ring_bytes = 0;                 ///< one entry's bytes: the largest layer blob
    uint8_t* ring_mem = nullptr;             ///< cudaHostAlloc(ring_n * ring_bytes), freed with the filler joined
    std::vector<uint8_t> ring_used;
};

ForesightSwap* foresight_swap_from_env(int64_t layers, int64_t experts, const std::vector<int>& card_of_layer,
                                       const std::vector<int>& devices);

}  // namespace strata::core
