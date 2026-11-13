// include/strata/core/read_once.hpp - round-17 design A: the request-scoped read-once window.
//
// The killer prefill (142,283 tokens) reads 368-380 GiB from the 78 GiB GGUF for ~40 GiB of unique
// experts (~9x re-read): the per-16k-chunk swap-in sweeps re-read ~19.7 GiB of the previous chunk's
// routed set each time, and the lending pool's admission re-reads the same dropped pairs again
// (the 99.5k-swap line, ~197 GiB) - all of it through FileExpertSource::copy_blobs -> read_direct,
// serialized at ~1.0 GiB/s.  Adjacent chunks share most of their routed set, so a window holding
// even ONE chunk's experts makes the next chunk's sweep mostly hits: with ~10 GiB of host RAM the
// expected read volume drops to ~150-200 GiB (PP ~600-700 from 482-534 at STRATA_SWAPIN_DEPTH=64).
//
// The window is an LRU of blob IMAGES keyed by (layer, expert), kept per REQUEST (armed at the
// prefill's start, dropped wholesale at its end - never parked, never pinned; plain pageable bytes,
// so the OS can take them back under pressure).  copy_blobs consults it BEFORE read_direct, at the
// same hook as the resident/complement copy, and admits what it just read under a byte budget:
//     STRATA_READONCE_MIB      the budget in MiB (0 / absent / unparsable = off, the default);
//                              STRATA_PREFILL_REUSE_MIB (the journal's spelling) is an alias
//     STRATA_PIN_RESERVE_GIB   the RAM that must stay free at every admission step (the pin-guard's
//                              reserve, default 2 GiB) - the effective budget is min(knob, free -
//                              reserve) and each admit re-checks it, the #1250 paced pattern
// Admission is stepwise (one blob at a time, only while the RAM left covers it above the reserve)
// and FADV discipline is untouched.  The knob OFF is bit-for-bit today: every hook is behind
// armed().  STRATA_SWAPIN_DEPTH composes with the window rather than replacing it: the depth sets
// the read queue's shape for the misses that remain, the window sets how many blobs are misses.
#pragma once

#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <list>
#include <memory>
#include <mutex>
#include <unordered_map>

namespace strata::core {

/// The window's byte budget from the env, in MiB.  STRATA_READONCE_MIB wins; the journal's
/// STRATA_PREFILL_REUSE_MIB is honored when the primary is absent.  Absent, empty, unparsable,
/// zero or negative = 0 (the window stays off).  Clamped at 128 GiB - the box's RAM is the real
/// bound (the effective budget is read against available RAM at arm time); this only stops absurd
/// values from promising on paper what the guard would refuse anyway.
inline int64_t read_once_budget_mib(const char* primary, const char* alias) {
    for (const char* v : {primary, alias}) {
        if (v == nullptr) continue;
        const long mib = std::atol(v);
        if (mib > 0) return (int64_t) std::min<long>(mib, 131072);
    }
    return 0;
}

/// The RAM that must stay free at every admission step: STRATA_PIN_RESERVE_GIB as the pin-guard
/// reads it (the same knob, so a box that sized its complement's pace sizes this the same way),
/// default 2 GiB.
inline uint64_t read_once_reserve_bytes(const char* pin_reserve_env) {
    return pin_reserve_env != nullptr && std::atof(pin_reserve_env) > 0
               ? (uint64_t) (std::atof(pin_reserve_env) * 1073741824.0)
               : 2ull << 30;
}

/// The budget the window arms with: 0 (off) when the knob is off or the RAM left minus the
/// reserve cannot hold anything, else the smaller of the knob and that room.
inline uint64_t read_once_effective_budget(uint64_t knob_bytes, uint64_t available, uint64_t reserve) {
    if (knob_bytes == 0) return 0;
    const uint64_t room = available > reserve ? available - reserve : 0;
    return std::min(knob_bytes, room);
}

/// The request-scoped LRU of blob images.  Thread-safe: the prefill's stager threads and the
/// arena's file batch consult and admit concurrently.  An image handed out by `find` stays alive
/// (a shared holder) across a concurrent eviction of its key, so the copy runs without the lock.
/// Counters are cumulative over the process (the prefill reports per-request deltas, as it does
/// the swap-in counters); the held bytes reset at every arm/disarm.
class ReadOnceWindow {
public:
    struct Counters {
        uint64_t hits = 0, hit_bytes = 0;            ///< blobs served from the window
        uint64_t reads = 0, read_bytes = 0;          ///< blobs the file tier read while armed (the misses)
        uint64_t admitted = 0, admitted_bytes = 0;   ///< of those, images kept
        uint64_t evicted = 0, evicted_bytes = 0;     ///< images dropped to stay in budget
        uint64_t refused = 0;                        ///< admissions the budget or the RAM guard refused
    };

    /// Arm with `budget_bytes` (0: stays off).  Clears anything a previous request left.
    void arm(uint64_t budget_bytes) {
        std::lock_guard<std::mutex> lk(mu_);
        budget_ = budget_bytes;
        map_.clear();
        order_.clear();
        held_ = 0;
        armed_.store(budget_bytes > 0, std::memory_order_release);
    }
    /// Drop every image wholesale (the request's end).  A copy in flight keeps its holder.
    void disarm() {
        std::lock_guard<std::mutex> lk(mu_);
        map_.clear();
        order_.clear();
        held_ = 0;
        budget_ = 0;
        armed_.store(false, std::memory_order_release);
    }
    bool armed() const { return armed_.load(std::memory_order_acquire); }

    /// The image for `key` (nullptr: miss, or off); `bytes` says how many bytes it holds (the
    /// layer's blob size).  A hit becomes the most-recent entry.
    const uint8_t* find(int64_t key, uint64_t* bytes = nullptr) {
        if (!armed()) return nullptr;
        std::lock_guard<std::mutex> lk(mu_);
        const auto it = map_.find(key);
        if (it == map_.end()) return nullptr;
        order_.splice(order_.begin(), order_, it->second.pos);
        c_.hits += 1;
        c_.hit_bytes += it->second.size;
        if (bytes != nullptr) *bytes = it->second.size;
        return it->second.image.get();
    }

    /// Count `n` file-read blobs of `bytes` (what the tier read past the window).
    void note_reads(size_t n, uint64_t bytes) {
        if (!armed()) return;
        std::lock_guard<std::mutex> lk(mu_);
        c_.reads += n;
        c_.read_bytes += bytes;
    }

    /// Keep a copy of the `bytes` at `key` the tier just read into `src`.  `room(need)` is the
    /// pin-guard: true while the RAM available covers `need` above the reserve; nullptr admits
    /// without the guard (the host test's shape).  LRU entries go until it fits; false when the
    /// blob alone exceeds the budget, the allocation fails, or the guard refuses (counted; the
    /// blob keeps today's per-chunk read).
    bool admit(int64_t key, const uint8_t* src, uint64_t bytes,
               const std::function<bool(uint64_t)>& room) {
        if (!armed() || bytes == 0) return false;
        std::lock_guard<std::mutex> lk(mu_);
        if (map_.find(key) != map_.end()) return true;   // another thread kept this pair already
        if (bytes > budget_) {                           // no eviction could make it fit
            c_.refused += 1;
            return false;
        }
        while (held_ + bytes > budget_) evict_lru();
        if (room != nullptr && !room(bytes)) {
            c_.refused += 1;
            return false;
        }
        uint8_t* raw = new (std::nothrow) uint8_t[bytes];
        if (raw == nullptr) {                            // no room in RAM: today's path for this pair
            c_.refused += 1;
            return false;
        }
        std::memcpy(raw, src, (size_t) bytes);
        held_ += bytes;
        order_.push_front(key);
        Entry& e = map_[key];
        e.image = std::shared_ptr<const uint8_t[]>(raw);
        e.size = bytes;
        e.pos = order_.begin();
        c_.admitted += 1;
        c_.admitted_bytes += bytes;
        return true;
    }

    uint64_t held_bytes() const {
        std::lock_guard<std::mutex> lk(mu_);
        return held_;
    }
    Counters counters() const {
        std::lock_guard<std::mutex> lk(mu_);
        return c_;
    }

private:
    struct Entry {
        std::shared_ptr<const uint8_t[]> image;   // freed when the last holder (map or copy) goes
        uint64_t size = 0;
        std::list<int64_t>::iterator pos;
    };

    void evict_lru() {   // mu_ held; the least-recent entry goes (a reader mid-copy keeps its holder)
        const int64_t key = order_.back();
        order_.pop_back();
        const auto it = map_.find(key);
        held_ -= it->second.size;
        c_.evicted += 1;
        c_.evicted_bytes += it->second.size;
        map_.erase(it);
    }

    mutable std::mutex mu_;
    std::unordered_map<int64_t, Entry> map_;
    std::list<int64_t> order_;   // front: most recent
    uint64_t held_ = 0, budget_ = 0;
    std::atomic<bool> armed_{false};
    Counters c_;
};

}  // namespace strata::core
