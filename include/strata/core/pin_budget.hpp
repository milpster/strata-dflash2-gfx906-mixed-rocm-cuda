// include/strata/core/pin_budget.hpp - pinned (page-locked) host memory is a budget, and the expert arena must not spend all of it.
//
// Windows (WDDM) lets a process page-lock about half the RAM for the GPU (the shared-memory segment).  The expert arena
// is registered slice by slice until the driver refuses one, which leaves NOTHING for the pinned/mapped buffers the
// engine allocates after it: the verify window's staging, the session's pool staging, the KV copy's tables.  Those were
// only a few MiB, and their failure ended the load ("verify: mapped staging allocation failed", exit 1) on a PC with
// 64 GB of free RAM.
//
// So the engine takes two reserves BEFORE the arena pins anything and gives them back after it:
//   * the SOFT reserve is released right after the arena is placed: the prompt path's staging and everything else
//     that already degrades to pageable memory then finds room, as it always did;
//   * the HARD reserve (the buffers whose failure ended the load) stays held until the first such allocation fails or
//     the engine is up; the allocation then releases it and retries once (host_alloc_guarded).
// A reserve that cannot be had is shrunk, then skipped: this never fails a start.
//
// Defaults: Windows CUDA 640 + 160 MiB (not the HIP build); Linux off (the NVIDIA driver there pins past RLIMIT_MEMLOCK and has no such
// ceiling; checked with a 4 GiB cudaHostRegister under `ulimit -l 1 GiB`).  STRATA_PIN_RESERVE_MIB=N sets the soft
// reserve (0 = off) on any OS; the hard one is a quarter of it, at least 64 MiB.
//
// STRATA_TEST_PIN_BUDGET_MIB=N is a TEST knob: it makes the engine's own pinned allocations and registrations that go
// through these wrappers (the arena's, the verify window's, the session's, the KV copy's) fail once N MiB are
// outstanding, which is what the WDDM ceiling does on Windows.  It lets Linux reproduce the exhaustion.
#pragma once

#include <cuda_runtime.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <mutex>
#include <string>

#if defined(__linux__)
#include <cerrno>
#include <chrono>
#include <cstring>
#include <sys/mman.h>
#include <sys/resource.h>
#endif

namespace strata::core {

namespace pin_detail {

struct State {
    std::mutex mu;
    uint64_t sim_budget = 0;      // bytes; 0 = no simulated ceiling
    uint64_t sim_used = 0;
    uint64_t sim_peak = 0;
    std::map<const void*, uint64_t> sim_ptrs;
    void* soft = nullptr;
    uint64_t soft_bytes = 0;
    void* hard = nullptr;
    uint64_t hard_bytes = 0;
    bool taken = false;
    uint64_t arena_pinned = 0;    // what the arena registered (for the hint)
    int hard_retries = 0;
    State() {
        if (const char* e = std::getenv("STRATA_TEST_PIN_BUDGET_MIB"); e != nullptr && *e != '\0')
            sim_budget = (uint64_t) std::strtoull(e, nullptr, 10) << 20;
    }
};
inline State& st() {
    static State s;
    return s;
}
// caller holds the lock
inline bool sim_charge(State& s, const void* p, uint64_t bytes) {
    if (s.sim_budget == 0) return true;
    if (s.sim_used + bytes > s.sim_budget) return false;
    s.sim_used += bytes;
    if (s.sim_used > s.sim_peak) s.sim_peak = s.sim_used;
    if (p != nullptr) s.sim_ptrs[p] = bytes;
    return true;
}
inline void sim_uncharge(State& s, const void* p) {
    if (s.sim_budget == 0) return;
    auto it = s.sim_ptrs.find(p);
    if (it == s.sim_ptrs.end()) return;
    s.sim_used -= it->second;
    s.sim_ptrs.erase(it);
}

inline cudaError_t alloc_locked(State& s, void** p, size_t bytes, unsigned flags) {
    if (s.sim_budget != 0 && s.sim_used + bytes > s.sim_budget) return cudaErrorMemoryAllocation;
    const cudaError_t e = cudaHostAlloc(p, bytes, flags);
    if (e == cudaSuccess) sim_charge(s, *p, bytes);
    return e;
}
inline void free_locked(State& s, void* p) {
    if (p == nullptr) return;
    (void) cudaFreeHost(p);
    sim_uncharge(s, p);
}

inline uint64_t soft_reserve_default_mib() {
    if (const char* e = std::getenv("STRATA_PIN_RESERVE_MIB"); e != nullptr && *e != '\0')
        return (uint64_t) std::strtoull(e, nullptr, 10);
#if defined(_WIN32) && !defined(STRATA_USE_HIP)   // CUDA on Windows (WDDM); the AMD build keeps its pinning as it was
    return 640;
#else
    return st().sim_budget != 0 ? 640 : 0;   // Linux: only when the test ceiling is on
#endif
}
}  // namespace pin_detail

/// cudaHostRegister through the (test) budget.
inline cudaError_t pin_register(void* p, size_t bytes, unsigned flags) {
    auto& s = pin_detail::st();
    if (s.sim_budget == 0) return cudaHostRegister(p, bytes, flags);
    std::lock_guard<std::mutex> g(s.mu);
    if (s.sim_used + bytes > s.sim_budget) return cudaErrorMemoryAllocation;
    const cudaError_t e = cudaHostRegister(p, bytes, flags);
    if (e == cudaSuccess) pin_detail::sim_charge(s, p, bytes);
    return e;
}
inline cudaError_t pin_unregister(void* p) {
    auto& s = pin_detail::st();
    if (s.sim_budget != 0) {
        std::lock_guard<std::mutex> g(s.mu);
        pin_detail::sim_uncharge(s, p);
    }
    return cudaHostUnregister(p);
}

// ---- arena mlock (STRATA_ARENA_MLOCK, opt-in, Linux) -------------------------
//
// WHY mlock() right after a successful cudaHostRegister: a registered host range is a KFD userptr mapping,
// and amdgpu invalidates the GPU queues' view of it whenever the CPU page tables under it change - reclaim,
// swap and compaction all do.  On gfx906 each invalidation evicts a queue with a dispatch still in a barrier
// (the eviction storm behind public-project PR #3913, route B): cudaHostRegister keeps the pages DMA-able but
// leaves them movable, mlock() makes the kernel keep them exactly where they are.  Default OFF; a span
// mlock() cannot take is logged and left at today's behavior, never fatal.
namespace arena_mlock_detail {
struct State {
    std::mutex mu;
    uint64_t bytes = 0;
    uint64_t spans = 0;
    double ms = 0.0;
};
inline State& st() {
    static State s;
    return s;
}
inline bool on() {
    static const bool v = std::getenv("STRATA_ARENA_MLOCK") != nullptr;   // presence turns it on, as STRATA_ARENA_COLLAPSE
    return v;
}
}  // namespace arena_mlock_detail

/// STRATA_ARENA_MLOCK=1: mlock() the page-aligned-outward span a cudaHostRegister just pinned.  No-op, not
/// even a line, when the knob is unset.  Linux only: elsewhere the knob is ignored.
inline void arena_mlock_span(void* p, uint64_t n) {
#if defined(__linux__)
    if (!arena_mlock_detail::on() || n == 0) return;
    static const bool limit_logged = [] {   // once, at first use
        struct rlimit rl {};
        if (getrlimit(RLIMIT_MEMLOCK, &rl) != 0) return true;
        if (rl.rlim_cur == RLIM_INFINITY)
            std::fprintf(stderr, "strata pin: arena mlock: RLIMIT_MEMLOCK soft limit: unlimited\n");
        else
            std::fprintf(stderr, "strata pin: arena mlock: WARNING: RLIMIT_MEMLOCK soft limit is %llu MiB, not unlimited "
                                 "- mlock may partially fail\n", (unsigned long long) (rl.rlim_cur >> 20));
        return true;
    }();
    (void) limit_logged;
    constexpr uint64_t kPage = 4096;   // the pin paths' page size; the registered span is aligned out to it
    const uintptr_t a = (uintptr_t) p & ~(uintptr_t) (kPage - 1);
    const uintptr_t b = ((uintptr_t) p + n + kPage - 1) & ~(uintptr_t) (kPage - 1);
    const auto t0 = std::chrono::steady_clock::now();
    if (mlock((void*) a, (size_t) (b - a)) != 0) {
        std::fprintf(stderr, "strata pin: arena mlock: mlock of [%#llx, +%llu MiB) failed (errno %d, %s); that span "
                             "keeps today's behavior\n",
                     (unsigned long long) a, (unsigned long long) ((b - a) >> 20), errno, std::strerror(errno));
        return;
    }
    auto& s = arena_mlock_detail::st();
    std::lock_guard<std::mutex> g(s.mu);
    s.bytes += (uint64_t) (b - a);
    ++s.spans;
    s.ms += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
#else
    (void) p;
    (void) n;
#endif
}

/// One banner line with everything arena_mlock_span() has locked so far (`complement_bytes` = the span set's
/// unregistered rest, named when there is one).  Nothing when the knob is unset.
inline void arena_mlock_report(uint64_t complement_bytes) {
#if defined(__linux__)
    if (!arena_mlock_detail::on()) return;
    const std::string tail = complement_bytes > 0
                                 ? " (complement covers " + std::to_string(complement_bytes >> 20) + " MiB)"
                                 : std::string();
    auto& s = arena_mlock_detail::st();
    std::lock_guard<std::mutex> g(s.mu);
    std::fprintf(stderr, "strata pin: arena mlock: locked %.2f GiB over %llu spans in %.0f ms%s\n",
                 (double) s.bytes / 1073741824.0, (unsigned long long) s.spans, s.ms, tail.c_str());
#else
    (void) complement_bytes;
#endif
}

// ---- native GGUF mlock (STRATA_NATIVE_MLOCK, opt-in, Linux) --------------------
//
// WHY mlock() a native/dense GGUF file mapping: the expert pack and the dense GGUF files are mapped
// PROT_READ|MAP_SHARED, so their clean pages sit in the page cache as reclaimable Shared_Clean memory - the
// ~8 GiB the kernel hands back to the first caller under memory pressure, and the engine then faults them
// back in through the NVMe on the next touch.  mlock() keeps those pages exactly where they are, so the
// serving loop keeps reading the file it already has.
//
// Scope: open_gguf locks each mapped GGUF shard in full (st.st_size), not merely an observed resident subset.
// This helper locks what is mapped, full shard by full shard; it makes no claim of complete model/expert
// coverage, uses no mlockall, and does not assert that any PLE pages are excluded or that any specific
// expert pages are covered.  Whether full-shard contents or aliasing demonstrably exclude PLE, and whether
// the locked set fits within the RLIMIT_MEMLOCK ceiling, is a separate coverage/fit audit (runtime blocked
// pending that audit).  A span mlock() cannot take is logged and left reclaimable, never fatal - a partial
// lock is a warning and the engine continues.  Locked in bounded steps so a long lock reports progress
// rather than looking like a stall, and one failure does not abandon the spans after it.
//
// Values of STRATA_NATIVE_MLOCK:
//   unset, empty, "0"  OFF. The default path is byte-identical: native_mlock_span() returns before a single
//                        syscall and prints nothing.
//   "1"                every mapped native span: the expert pack, each GGUF shard, the shared expert arena.
//   "arena"            the SHARED EXPERT ARENA's own mapping, and nothing else. The pack and the shards ask as
//                        ordinary spans and are refused before any syscall, so their pages stay reclaimable
//                        exactly as they are with the knob off, and their report stays silent too. The point is
//                        a run that reads its experts out of one mapped arena file: that file's pages are the
//                        ones worth locking, not a second locked copy of the same weights in pack and shards.
//   anything else      every mapped native span, as any non-empty value always did.
namespace native_mlock_detail {
// What the knob asked for.  Decided once, on the first call: an engine's spans never see the environment
// change under them.
enum class Mode { off, all, arena };
struct State {
    std::mutex mu;
    uint64_t bytes = 0;     // locked since the last report()
    uint64_t requested = 0; // requested since the last report()
    uint64_t spans = 0;
    double seconds = 0.0;
    bool limit_logged = false;
};
inline State& st() {
    static State s;
    return s;
}
// Which spans the knob covers.  Unset, empty or "0": off.  "arena": the shared expert arena only.  Any other
// non-empty word ("1" included): every mapped native span, which is what any non-empty value always meant.
// On non-Linux the knob can never be on: strcmp and the RLIMIT types below do not exist there.
inline Mode mode() {
#if defined(__linux__)
    static const Mode v = [] {
        const char* e = std::getenv("STRATA_NATIVE_MLOCK");
        if (e == nullptr || *e == '\0' || std::strcmp(e, "0") == 0) return Mode::off;
        if (std::strcmp(e, "arena") == 0) return Mode::arena;
        return Mode::all;
    }();
    return v;
#else
    return Mode::off;
#endif
}
// the knob is on in some mode; unset (the default) is a no-op
inline bool on() { return mode() != Mode::off; }
// #4671, once: raise the soft RLIMIT_MEMLOCK to what the hard limit allows (no privilege needed). This box
// runs unlimited, so it is a no-op line there; it is what lets a service started without LimitMEMLOCK lock.
// Linux only; no-op on every other platform (no rlimit, no syscall, no log).
#if defined(__linux__)
inline void raise_limit_once() {
    auto& s = st();
    std::lock_guard<std::mutex> g(s.mu);
    if (s.limit_logged) return;
    s.limit_logged = true;
    struct rlimit rl {};
    if (getrlimit(RLIMIT_MEMLOCK, &rl) != 0) return;
    if (rl.rlim_cur == RLIM_INFINITY) {
        std::fprintf(stderr, "strata native mlock: RLIMIT_MEMLOCK soft limit: unlimited\n");
        return;
    }
    if (rl.rlim_max > rl.rlim_cur) {
        struct rlimit up = rl;
        up.rlim_cur = rl.rlim_max;
        if (setrlimit(RLIMIT_MEMLOCK, &up) == 0) {
            std::fprintf(stderr, "strata native mlock: RLIMIT_MEMLOCK soft limit raised from %llu MiB to %llu MiB\n",
                         (unsigned long long) (rl.rlim_cur >> 20), (unsigned long long) (rl.rlim_max >> 20));
            return;
        }
    }
    std::fprintf(stderr, "strata native mlock: WARNING: RLIMIT_MEMLOCK soft limit is %llu MiB, not unlimited - "
                         "the native GGUF lock may be partial (set LimitMEMLOCK=infinity or `ulimit -l unlimited`)\n",
                 (unsigned long long) (rl.rlim_cur >> 20));
}
#else
inline void raise_limit_once() {}
#endif

/// Outcome of one native_mlock_span() call.  Populated on every platform; all-zero + complete=true when the
/// knob is off or the span is empty (nothing requested, nothing failed).
struct Result {
    uint64_t requested = 0;   // bytes asked to lock
    uint64_t locked = 0;      // bytes actually page-locked
    uint64_t failed = 0;      // requested - locked
    bool complete = false;    // true iff locked == requested
    double seconds = 0.0;     // elapsed wall time
};
}  // namespace native_mlock_detail

/// Which mapping a native_mlock_span() call speaks for.  The knob's "arena" value takes the arena's own
/// mapping and refuses every ordinary span, so a caller that serves the experts out of one mapped arena file
/// keeps the page-locked copy it reads from, without a second locked copy of the same weights in the pack and
/// the GGUF shards.
enum class NativeMlockScope { file, arena };

/// STRATA_NATIVE_MLOCK: mlock one read-only native GGUF file mapping span, in bounded steps. No-op, not even
/// a line, when the knob is unset - and no-op for an ordinary span when the knob says "arena".
/// Linux only; elsewhere the call is discarded.
inline native_mlock_detail::Result native_mlock_span(const void* p, uint64_t n, const char* what,
                                                     NativeMlockScope scope = NativeMlockScope::file) {
    native_mlock_detail::Result r;
#if defined(__linux__)
    const native_mlock_detail::Mode want_mode = native_mlock_detail::mode();
    // Off: nothing at all.  "arena" with an ordinary span: nothing at all, and NOT the limit line below either
    // - a refused span is silent, exactly as a span with the knob unset, and its native_mlock_report() stays
    // silent too because it books no span.
    if (want_mode == native_mlock_detail::Mode::off || n == 0 ||
        (want_mode == native_mlock_detail::Mode::arena && scope != NativeMlockScope::arena)) {
        r.complete = true;
        return r;
    }
    native_mlock_detail::raise_limit_once();
    constexpr uint64_t kPage = 4096;
    constexpr uint64_t kStep = 256ull << 20;
    const uintptr_t a = (uintptr_t) p & ~(uintptr_t) (kPage - 1);
    const uintptr_t b = ((uintptr_t) p + n + kPage - 1) & ~(uintptr_t) (kPage - 1);
    const uint64_t total = (uint64_t) (b - a);
    const auto t0 = std::chrono::steady_clock::now();
    r.requested = total;
    uint64_t got = 0;
    uint64_t chunk = 0;
    for (uintptr_t lo = a; lo < b; lo += kStep) {
        const uintptr_t piece = lo + kStep > b ? b - lo : kStep;
        ++chunk;
        if (mlock((const void*) lo, (size_t) piece) != 0) {
            const int why = errno;
            std::fprintf(stderr, "strata native mlock: mlock of %s [%#llx, +%llu MiB) chunk %llu: reached %llu of %llu MiB then failed (errno %d, %s); the rest stays reclaimable and the engine continues\n",
                          what, (unsigned long long) a, (unsigned long long) (total >> 20),
                          (unsigned long long) chunk, (unsigned long long) (got >> 20), (unsigned long long) (total >> 20),
                          why, std::strerror(why));
            r.locked = got;
            r.failed = total - got;
            r.complete = false;
            const double secs = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
            r.seconds = secs;
            auto& s = native_mlock_detail::st();
            std::lock_guard<std::mutex> g(s.mu);
            s.bytes += got;
            s.requested += total;
            ++s.spans;
            s.seconds += secs;
            return r;
        }
        got += (uint64_t) piece;
        if ((chunk % 4) == 0 || lo + kStep >= b) {
            std::fprintf(stderr, "strata native mlock: %s progress chunk %llu: %llu of %llu MiB locked\n",
                          what, (unsigned long long) chunk, (unsigned long long) (got >> 20),
                          (unsigned long long) (total >> 20));
        }
    }
    const double secs = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    r.locked = total;
    r.failed = 0;
    r.complete = true;
    r.seconds = secs;
    auto& s = native_mlock_detail::st();
    std::lock_guard<std::mutex> g(s.mu);
    s.bytes += got;
    s.requested += total;
    ++s.spans;
    s.seconds += secs;
#else
    (void) p;
    (void) n;
    (void) what;
    r.complete = true;
#endif
    return r;
}

/// One progress line with what native_mlock_span() has locked since the last call here, then reset.  Nothing
/// when the knob is unset, and nothing when no span was booked since the last call: that is how the pack and
/// the GGUF shard paths stay silent with STRATA_NATIVE_MLOCK=arena, having had every span they asked for refused.
/// Call it once a source has mapped all of its native GGUF spans.
inline void native_mlock_report(const char* when) {
#if defined(__linux__)
    if (!native_mlock_detail::on()) return;
    auto& s = native_mlock_detail::st();
    std::lock_guard<std::mutex> g(s.mu);
    if (s.spans == 0) return;
    const uint64_t failed = s.requested > s.bytes ? s.requested - s.bytes : 0;
    std::fprintf(stderr, "strata native mlock: locked %.2f GiB of %.2f GiB requested over %llu spans in %.1f s (%s)%s\n",
                 (double) s.bytes / 1073741824.0, (double) s.requested / 1073741824.0,
                 (unsigned long long) s.spans, s.seconds, when,
                 failed != 0 ? " [PARTIAL]" : "");
    s.bytes = 0;
    s.requested = 0;
    s.spans = 0;
    s.seconds = 0.0;
#else
    (void) when;
#endif
}


/// Take the reserves (call before the expert arena pins).  Never fails.
inline void pin_reserve_take() {
    auto& s = pin_detail::st();
    std::lock_guard<std::mutex> g(s.mu);
    if (s.taken) return;
    s.taken = true;
    const uint64_t soft_mib = pin_detail::soft_reserve_default_mib();
    if (soft_mib == 0) return;
    const uint64_t hard_mib = soft_mib / 4 > 64 ? soft_mib / 4 : 64;
    // the hard one first: it is the one whose absence ends a load.  Shrink by halves down to 16 MiB.
    auto take = [&](void*& p, uint64_t& got, uint64_t mib) {
        for (; mib >= 16; mib /= 2) {
            void* q = nullptr;
            if (pin_detail::alloc_locked(s, &q, (size_t) (mib << 20), cudaHostAllocPortable) == cudaSuccess) {
                p = q;
                got = mib << 20;
                return;
            }
            (void) cudaGetLastError();
        }
    };
    take(s.hard, s.hard_bytes, hard_mib);
    take(s.soft, s.soft_bytes, soft_mib);
    std::fprintf(stderr, "strata: pinned-memory headroom: holding %llu MiB for the buffers allocated after the expert arena "
                         "(released after it: %llu MiB; kept until needed: %llu MiB; STRATA_PIN_RESERVE_MIB changes it)\n",
                 (unsigned long long) ((s.soft_bytes + s.hard_bytes) >> 20), (unsigned long long) (s.soft_bytes >> 20),
                 (unsigned long long) (s.hard_bytes >> 20));
    if (s.soft_bytes + s.hard_bytes < ((soft_mib + hard_mib) << 20))
        std::fprintf(stderr, "strata: WARNING: pinned-memory budget is already nearly spent before the expert arena "
                             "(got %llu of %llu MiB of headroom)\n",
                     (unsigned long long) ((s.soft_bytes + s.hard_bytes) >> 20), (unsigned long long) (soft_mib + hard_mib));
}
/// The arena is placed: hand the soft reserve back.
inline void pin_reserve_release_soft() {
    auto& s = pin_detail::st();
    std::lock_guard<std::mutex> g(s.mu);
    if (s.soft == nullptr) return;
    pin_detail::free_locked(s, s.soft);
    s.soft = nullptr;
    std::fprintf(stderr, "strata: pinned-memory headroom: %llu MiB handed back after the expert arena\n",
                 (unsigned long long) (s.soft_bytes >> 20));
    s.soft_bytes = 0;
}
/// The engine is up (or the hard allocation needs it): hand the hard reserve back.
inline void pin_reserve_release_hard() {
    auto& s = pin_detail::st();
    std::lock_guard<std::mutex> g(s.mu);
    if (s.hard == nullptr) return;
    pin_detail::free_locked(s, s.hard);
    s.hard = nullptr;
    s.hard_bytes = 0;
}
inline void pin_note_arena(uint64_t pinned_bytes) {
    auto& s = pin_detail::st();
    std::lock_guard<std::mutex> g(s.mu);
    s.arena_pinned = pinned_bytes;
    // TEST knob: the driver took everything but N KiB (what WDDM did on the 64 GB PC: the arena pinned to the ceiling)
    if (s.sim_budget != 0)
        if (const char* e = std::getenv("STRATA_TEST_PIN_LEFT_KIB"); e != nullptr && *e != 0)
            s.sim_budget = s.sim_used + ((uint64_t) std::strtoull(e, nullptr, 10) << 10);
}
inline uint64_t pin_sim_peak() { return pin_detail::st().sim_peak; }
/// Test knob only: one line with the simulated ceiling, what is pinned now and the peak.
inline void pin_sim_report(const char* when) {
    auto& s = pin_detail::st();
    std::lock_guard<std::mutex> g(s.mu);
    if (s.sim_budget == 0) return;
    std::fprintf(stderr, "strata: test pinned budget %llu MiB: %llu MiB charged, peak %llu MiB (%s)\n",
                 (unsigned long long) (s.sim_budget >> 20), (unsigned long long) (s.sim_used >> 20),
                 (unsigned long long) (s.sim_peak >> 20), when);
}

/// cudaHostAlloc for a buffer the engine cannot run without.  On a refusal it hands the reserves back and tries once
/// more; the caller still reports a second refusal.
inline cudaError_t host_alloc_guarded(void** p, size_t bytes, unsigned flags) {
    auto& s = pin_detail::st();
    {
        std::lock_guard<std::mutex> g(s.mu);
        const cudaError_t e = pin_detail::alloc_locked(s, p, bytes, flags);
        if (e == cudaSuccess) return e;
        (void) cudaGetLastError();
        if (s.hard == nullptr && s.soft == nullptr) return e;
        if (s.soft != nullptr) { pin_detail::free_locked(s, s.soft); s.soft = nullptr; s.soft_bytes = 0; }
        if (s.hard != nullptr) { pin_detail::free_locked(s, s.hard); s.hard = nullptr; s.hard_bytes = 0; }
        ++s.hard_retries;
        std::fprintf(stderr, "strata: pinned-memory headroom: a %llu KiB pinned buffer was refused; the held headroom is handed back "
                             "and the allocation retried\n", (unsigned long long) (bytes >> 10));
        const cudaError_t e2 = pin_detail::alloc_locked(s, p, bytes, flags);
        if (e2 != cudaSuccess) (void) cudaGetLastError();
        return e2;
    }
}

/// The sentence every hard failure ends with, so the app can recognise it in the log.
inline std::string pin_exhausted_hint() {
    auto& s = pin_detail::st();
    const uint64_t gib = s.arena_pinned >> 30;
    std::string t = "[pinned-memory-exhausted] the OS gave no more page-locked memory";
    if (gib > 2) t += "; start again with STRATA_ARENA_PIN_GIB=" + std::to_string(gib - 2) + " (pin less of the expert arena) or with the low-RAM option";
    return t;
}

}  // namespace strata::core
