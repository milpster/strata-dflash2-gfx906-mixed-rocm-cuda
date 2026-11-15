// src/core/vram_guard.cpp - see include/strata/core/vram_guard.hpp.
#include "strata/core/vram_guard.hpp"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>

namespace strata::core::vram_guard {
namespace {

constexpr int kMaxDevices = 16;   // the split's planner stays far below this; a device past it is ignored

struct Config {
    std::atomic<bool> on{false};
    std::atomic<int64_t> step_mib{64};
    std::atomic<int64_t> max_growth_mib{4096};
    std::atomic<int64_t> min_step_ms{1000};
};

int64_t env_mib(const char* name, int64_t fallback) {
    const char* v = std::getenv(name);
    const long long n = v != nullptr ? std::atoll(v) : 0;
    return n > 0 ? (int64_t) n : fallback;
}

/// Read once (magic static, leaked on purpose like the conversation copy-handle pool);
/// `configure` may replace the values afterwards.
Config& config() {
    static Config* c = [] {
        Config* k = new Config;
        const char* on = std::getenv("STRATA_VRAM_GUARD");
        k->on.store(on != nullptr && std::strcmp(on, "1") == 0);
        k->step_mib.store(env_mib("STRATA_VRAM_GUARD_STEP_MIB", 64));
        k->max_growth_mib.store(env_mib("STRATA_VRAM_GUARD_MAX_MIB", 4096));
        return k;
    }();
    return *c;
}

/// One card's guard state.  Everything a check reads without stepping is atomic (the check
/// sites are hot); the step itself serializes on g_mu below.
struct Dev {
    std::atomic<int64_t> base_mib{0};
    std::atomic<int64_t> floor_mib{0};
    std::atomic<int64_t> steps{0};
    std::atomic<int64_t> last_step_ms{0};   // 0: never stepped (a real steady-clock ms is never 0)
    std::atomic<bool> cap_said{false};
};

Dev devs[kMaxDevices];

// N1 (round 18p): g_mu covers only the step bookkeeping - the donor NEVER runs under it, so a slow
// donor on one card cannot stall another card's checks and cannot deadlock against a re-entering
// probe.  g_donor_mu serializes the donor calls themselves: the engine's recall machinery is one
// pool that is not threaded for two cards at once.
std::mutex g_mu;
std::mutex g_donor_mu;
Reclaim g_reclaim = nullptr;

/// True on a thread while its donor call runs.  The donor's own probes must not step or recurse, so
/// check() answers nothing-to-do on that thread until the call returns (the no-reentry contract).
thread_local bool t_in_donor = false;

// The wiring this file's round-18o TODO named, landed in round 18p (src/program/generate.cpp):
//   1. after the expert-cache sizing: set_base_mib per card - the --vram-reserve-mib the sizing
//      actually applied there (the auto path may lower it, and a split stage keeps
//      --vram-reserve-later-mib when one was given);
//   2. once at startup, before serve: set_reclaim with the engine's own VRAM-pressure recall - the
//      parked conversations' K/V slots (conversation_park_vram_release_all), each freed on the card
//      it lives on.  On HIP ExpertCache::shrink refuses (no VMM segments), so that recall is the
//      engine's real runtime donor; a step usually recalls more than it asked (whole 64 MiB-rounded
//      parked images) and the guard logs the real figure.

int64_t now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch()).count();
}

}  // namespace

bool enabled() {
    return config().on.load(std::memory_order_relaxed);
}

void configure(bool on, int64_t step_mib, int64_t max_growth_mib, int64_t min_step_ms) {
    Config& c = config();
    c.on.store(on, std::memory_order_relaxed);
    c.step_mib.store(step_mib > 0 ? step_mib : 64, std::memory_order_relaxed);
    c.max_growth_mib.store(max_growth_mib >= 0 ? max_growth_mib : 4096, std::memory_order_relaxed);
    c.min_step_ms.store(min_step_ms >= 0 ? min_step_ms : 1000, std::memory_order_relaxed);
}

void set_reclaim(Reclaim fn) {
    std::lock_guard<std::mutex> lk(g_mu);
    g_reclaim = fn;
}

void set_base_mib(int device, int64_t base_mib) {
    if (device < 0 || device >= kMaxDevices) return;
    Dev& d = devs[device];
    const int64_t base = base_mib > 0 ? base_mib : 0;
    d.base_mib.store(base, std::memory_order_relaxed);
    if (d.floor_mib.load(std::memory_order_relaxed) < base)   // a rising boot floor lifts the floor
        d.floor_mib.store(base, std::memory_order_relaxed);   // nothing ever lowers it
}

int64_t base_mib(int device) {
    return device >= 0 && device < kMaxDevices ? devs[device].base_mib.load(std::memory_order_relaxed) : 0;
}

int64_t floor_mib(int device) {
    return device >= 0 && device < kMaxDevices ? devs[device].floor_mib.load(std::memory_order_relaxed) : 0;
}

int64_t steps(int device) {
    return device >= 0 && device < kMaxDevices ? devs[device].steps.load(std::memory_order_relaxed) : 0;
}

bool check(int device, int64_t free_mib) {
    if (t_in_donor) return false;   // the donor's own probe: no step, no recursion (N1)
    if (!enabled() || device < 0 || device >= kMaxDevices) return false;
    Config& c = config();
    if (free_mib >= kLowMib) return false;   // not low: the floor keeps its hysteresis
    Dev& d = devs[device];
    const int64_t now = now_ms();
    const int64_t window = c.min_step_ms.load(std::memory_order_relaxed);
    const int64_t last = d.last_step_ms.load(std::memory_order_relaxed);
    if (last != 0 && now - last < window) return true;
    // Everything the step needs is decided under the lock; the donor runs after it is released, so a
    // slow donor on this card never holds up another card's checks (N1).
    Reclaim donor = nullptr;
    int64_t floor = 0, next = 0, step = 0, n_steps = 0;
    {
        std::lock_guard<std::mutex> lk(g_mu);   // another thread may have stepped while this one waited
        const int64_t stepped = d.last_step_ms.load(std::memory_order_relaxed);
        if (stepped != 0 && now - stepped < window) return true;
        const int64_t base = d.base_mib.load(std::memory_order_relaxed);
        floor = d.floor_mib.load(std::memory_order_relaxed);
        const int64_t max_growth = c.max_growth_mib.load(std::memory_order_relaxed);
        const int64_t room = base + max_growth - floor;   // N2: the last step clamps to the cap
        if (room <= 0) {
            if (!d.cap_said.exchange(true, std::memory_order_relaxed))
                std::fprintf(stderr, "strata vram guard: CUDA%d: still %lld MiB free but the reserve floor is "
                                     "capped at %lld MiB (%lld MiB over the %lld MiB boot floor): not raising it "
                                     "further - lower --max-context / --batch or raise --vram-reserve-mib\n",
                             device, (long long) free_mib, (long long) (base + max_growth),
                             (long long) max_growth, (long long) base);
            return true;
        }
        step = c.step_mib.load(std::memory_order_relaxed);
        if (step > room) step = room;   // a non-divisor STRATA_VRAM_GUARD_MAX_MIB no longer overshoots
        next = floor + step;
        d.floor_mib.store(next, std::memory_order_relaxed);
        n_steps = d.steps.fetch_add(1, std::memory_order_relaxed) + 1;
        d.last_step_ms.store(now, std::memory_order_relaxed);
        d.cap_said.store(false, std::memory_order_relaxed);
        donor = g_reclaim;
    }
    // Outside every guard lock, and a no-op for this thread's own probes while it runs (N1).  The
    // engine's recall is one pool, so two cards stepping at once donate one after the other.
    int64_t got = 0;
    if (donor != nullptr) {
        const std::lock_guard<std::mutex> dlk(g_donor_mu);
        t_in_donor = true;
        got = donor(device, step);
        t_in_donor = false;
    }
    std::fprintf(stderr, "strata vram guard: CUDA%d: %lld MiB free is under the %lld MiB line: reserve "
                         "floor %lld -> %lld MiB (+%lld MiB, step %lld), reclaimed %lld MiB%s\n",
                 device, (long long) free_mib, (long long) kLowMib, (long long) floor, (long long) next,
                 (long long) step, (long long) n_steps, (long long) got,
                 donor != nullptr ? "" : " (no reclaim donor registered)");
    return true;
}

void reset(int device) {
    if (device < 0 || device >= kMaxDevices) return;
    Dev& d = devs[device];
    d.base_mib.store(0, std::memory_order_relaxed);
    d.floor_mib.store(0, std::memory_order_relaxed);
    d.steps.store(0, std::memory_order_relaxed);
    d.last_step_ms.store(0, std::memory_order_relaxed);
    d.cap_said.store(false, std::memory_order_relaxed);
}

}  // namespace strata::core::vram_guard
