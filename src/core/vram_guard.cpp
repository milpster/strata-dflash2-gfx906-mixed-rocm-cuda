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

/// The low line the request-start warning uses (kVramSafeMib, src/program/generate.cpp, #2065):
/// under it the driver can page GPU memory to system RAM and a window may never finish.
/// Mirrored here because that constant is file-local to the program TU this lane must not edit.
constexpr int64_t kLowMib =
#ifdef _WIN32
    1024;   // WDDM pages instead of failing, so the line sits higher (kVramSafeMib's rule)
#else
    512;
#endif

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

std::mutex g_mu;   // the step path and the donor slot (the donor runs under it: steps are rare)
Reclaim g_reclaim = nullptr;

// TODO(src/program/generate.cpp - the parallel lane owns that file, round 18o): the engine is the
// only side that can donate, and the two call sites it needs there are:
//   1. after the expert-cache auto sizing (the "expert cache auto:" boot print, ~generate.cpp:8820):
//      `strata::core::vram_guard::set_base_mib(dev, o.vram_reserve_mib);` per card, so each floor
//      starts at the static boot reserve the sizing already subtracted;
//   2. once at startup: `strata::core::vram_guard::set_reclaim(+[](int dev, int64_t want_mib) { ... })`,
//      whose body trims about want_mib from THAT card's pool and returns the MiB freed.  On HIP
//      ExpertCache::shrink refuses (no VMM segments), so the donor is the engine's own per-card
//      loan/swap recall - machinery only generate.cpp reaches.
// Until that lands the guard still steps and logs per card, with "(no reclaim donor registered)".

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
    if (!enabled() || device < 0 || device >= kMaxDevices) return false;
    Config& c = config();
    if (free_mib >= kLowMib) return false;   // not low: the floor keeps its hysteresis
    Dev& d = devs[device];
    const int64_t now = now_ms();
    const int64_t window = c.min_step_ms.load(std::memory_order_relaxed);
    const int64_t last = d.last_step_ms.load(std::memory_order_relaxed);
    if (last != 0 && now - last < window) return true;
    std::lock_guard<std::mutex> lk(g_mu);   // another thread may have stepped while this one waited
    const int64_t stepped = d.last_step_ms.load(std::memory_order_relaxed);
    if (stepped != 0 && now - stepped < window) return true;
    const int64_t base = d.base_mib.load(std::memory_order_relaxed);
    const int64_t floor = d.floor_mib.load(std::memory_order_relaxed);
    const int64_t step = c.step_mib.load(std::memory_order_relaxed);
    const int64_t max_growth = c.max_growth_mib.load(std::memory_order_relaxed);
    if (floor - base >= max_growth) {
        if (!d.cap_said.exchange(true, std::memory_order_relaxed))
            std::fprintf(stderr, "strata vram guard: CUDA%d: still %lld MiB free but the reserve floor is "
                                 "capped at %lld MiB (%lld MiB over the %lld MiB boot floor): not raising it "
                                 "further - lower --max-context / --batch or raise --vram-reserve-mib\n",
                         device, (long long) free_mib, (long long) (base + max_growth),
                         (long long) max_growth, (long long) base);
        return true;
    }
    const int64_t next = floor + step;
    d.floor_mib.store(next, std::memory_order_relaxed);
    const int64_t n_steps = d.steps.fetch_add(1, std::memory_order_relaxed) + 1;
    d.last_step_ms.store(now, std::memory_order_relaxed);
    d.cap_said.store(false, std::memory_order_relaxed);
    Reclaim donor = g_reclaim;
    const int64_t got = donor != nullptr ? donor(device, step) : 0;
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
