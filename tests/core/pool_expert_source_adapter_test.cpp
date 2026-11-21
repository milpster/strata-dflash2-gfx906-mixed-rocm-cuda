// tests/core/pool_expert_source_adapter_test.cpp - deterministic CPU-only tests
// for the PoolExpertStore-backed ExpertSource adapter (default OFF, nothing in
// the engine includes it yet).
//
// Same fixture style as pool_expert_store_test.cpp: real temp experts.bin +
// real ExpertLayout, real Pool (mmap+mlock via link-time --wrap), fake ONLY
// lock calls.  The async-note hook is injected (CPU fake); no CUDA.
//
// Usage: pool_expert_source_adapter_test <build_dir>

#define STRATA_RAM_POOL_TEST 1
#include "strata/core/pool_expert_source_adapter.hpp"
#include "strata/core/pool_expert_store.hpp"
#include "strata/core/ram_pool.hpp"
#include "strata/kernels/cpu/expert_layout.hpp"
#undef STRATA_RAM_POOL_TEST

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <sys/mman.h>
#include <sys/stat.h>
#include <thread>
#include <vector>

namespace {

using strata::core::Pool;
using strata::core::PoolLease;
using strata::core::PoolExpertStore;
using strata::core::PoolExpertSourceAdapter;
using strata::core::pool_expert_source_adapter_set_admit_retry_for_test;
using strata::kernels::cpu::ExpertLayout;

int checks = 0;
int failures = 0;

void check(bool ok, const char* label) {
    ++checks;
    if (!ok) { ++failures; std::fprintf(stderr, "FAIL: %s\n", label); }
    else { std::fprintf(stderr, "pass: %s\n", label); }
}

constexpr uint64_t kPage = 4096;

static uint8_t byte_at(uint64_t p) {
    return static_cast<uint8_t>((p * 31u) ^ (p >> 8) ^ (p >> 16) ^ (p >> 24) ^ 7u);
}

std::string g_full_data;
std::string g_fixture_path;

static bool write_file(const std::string& path, const std::string& data) {
    std::FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) return false;
    if (std::fwrite(data.data(), 1, data.size(), f) != data.size()) {
        std::fclose(f);
        return false;
    }
    return std::fclose(f) == 0;
}

// Native layout: 3 layers, nonuniform per-expert bytes (64, 128, 5000), gapped
// offsets (0, 4096, 8192), 4 experts per layer. total = 28192.
static bool build_layout(ExpertLayout& layout) {
    layout = ExpertLayout{};
    layout.native = true;
    layout.n_layers = 3;
    layout.n_expert = 4;
    layout.offset = {0, 4096, 8192};
    layout.bytes = {64, 128, 5000};
    layout.max_blob = 5000;
    layout.total = 28192;
    return true;
}

static bool build_fixture(const std::string& dir) {
    ExpertLayout layout;
    if (!build_layout(layout)) return false;
    g_full_data.resize(layout.total);
    for (uint64_t p = 0; p < layout.total; ++p) g_full_data[p] = byte_at(p);
    g_fixture_path = dir + "/adapter_fixture_20261120.bin";
    return write_file(g_fixture_path, g_full_data);
}

static bool region_matches(const void* data, uint64_t file_offset, uint64_t size) {
    if (size == 0) return true;
    if (file_offset + size > g_full_data.size()) return false;
    return std::memcmp(data, g_full_data.data() + file_offset, size) == 0;
}

static uint64_t expert_file_offset(int64_t layer, int64_t expert) {
    static const uint64_t off[3] = {0, 4096, 8192};
    static const uint64_t b[3] = {64, 128, 5000};
    return off[(size_t)layer] + (uint64_t)expert * b[(size_t)layer];
}
static uint64_t expert_bytes(int64_t layer) {
    static const uint64_t b[3] = {64, 128, 5000};
    return b[(size_t)layer];
}

// ---- injected async-note hook fakes ----------------------------------------

struct HookLog {
    int noted = 0;      // how many times note_async_read called the hook
    bool completes = false;  // if true, the test marks completion after each note
};

static HookLog g_hook;

static void fake_hook(int64_t layer, int64_t expert, void* stream) {
    (void)layer; (void)expert; (void)stream;
    ++g_hook.noted;
}

}  // namespace

// ---- link-time interposition: mmap/munmap real+counted, mlock/munlock fake,--
// ---- pread wrapped to count calls issued by the store's reader --------------

namespace wrap {
int mmap_calls = 0;
int munmap_calls = 0;
int mlock_calls = 0;
int munlock_calls = 0;
int pread_calls = 0;
static std::map<void*, uint64_t> live_maps;
static uint64_t total_live_bytes = 0;
static uint64_t peak_live_bytes = 0;
constexpr uint64_t kMappingLimit = 2ull << 20;
}  // namespace wrap

static void track_mmap(void* p, uint64_t len) {
    if (p == nullptr || p == MAP_FAILED) return;
    if (wrap::live_maps.count(p)) { std::fprintf(stderr, "FAIL: duplicate mmap\n"); std::abort(); }
    wrap::live_maps[p] = len; wrap::total_live_bytes += len;
    wrap::peak_live_bytes = std::max(wrap::peak_live_bytes, wrap::total_live_bytes);
    if (wrap::total_live_bytes > wrap::kMappingLimit) std::abort();
}
static void track_munmap(void* p, uint64_t len) {
    auto it = wrap::live_maps.find(p);
    if (it == wrap::live_maps.end() || it->second != len) {
        std::fprintf(stderr, "FAIL: unmatched munmap\n"); std::abort();
    }
    wrap::total_live_bytes -= it->second; wrap::live_maps.erase(it);
}

extern "C" {
void* __real_mmap(void*, size_t, int, int, int, off_t);
int __real_munmap(void*, size_t);
int __real_mlock(const void*, size_t);
int __real_munlock(const void*, size_t);
ssize_t __real_pread(int, void*, size_t, off_t);

void* __wrap_mmap(void* a, size_t l, int p, int f, int d, off_t o) {
    ++wrap::mmap_calls;
    void* p2 = __real_mmap(a, l, p, f, d, o);
    if (p2 == MAP_FAILED) return p2;
    track_mmap(p2, l);
    return p2;
}
int __wrap_munmap(void* a, size_t l) {
    ++wrap::munmap_calls;
    track_munmap(a, l);
    return __real_munmap(a, l);
}
int __wrap_mlock(const void*, size_t) { ++wrap::mlock_calls; return 0; }
int __wrap_munlock(const void*, size_t) { ++wrap::munlock_calls; return 0; }
ssize_t __wrap_pread(int fd, void* buf, size_t count, off_t offset) {
    ++wrap::pread_calls;
    return __real_pread(fd, buf, count, offset);
}
}  // extern "C"

// ExpertSource::copy_blob / copy_blobs are virtual, not inline, and defined in
// src/core/expert_source.cpp.  The adapter never calls them, but the vtable
// references them; provide minimal stubs so the CPU-only TU links.
namespace strata {
namespace core {
bool ExpertSource::copy_blob(int64_t, int64_t, uint8_t*) { return false; }
bool ExpertSource::copy_blobs(const int32_t*, const int32_t*,
                              uint8_t* const*, size_t) { return false; }
}  // namespace core
}  // namespace strata

static std::error_code make_pool(Pool& out, uint64_t bytes) {
    std::error_code ec;
    auto p = Pool::create(bytes, ec);
    out = std::move(p);
    return ec;
}

int main(int argc, char** argv) {
    const std::string build_dir = (argc > 1) ? argv[1] : ".";
    // The admission-retry gate is read ONCE per process, and this binary must
    // exercise the default (OFF): make sure the caller's environment cannot arm
    // it by accident.  Cases 12-15 set it through the test hook instead.
    ::unsetenv("STRATA_RAM_POOL_ADMIT_RETRY");
    if (!build_fixture(build_dir)) {
        std::fprintf(stderr, "FAIL: cannot write fixture\n");
        return 2;
    }

    // ============ 1. borrow/return round-trip byte equality ============
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "1: create 256KiB pool");

        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "1: open store");

        PoolExpertSourceAdapter adapter(store);
        for (int64_t l = 0; l < 3; ++l) {
            for (int64_t e = 0; e < 4; ++e) {
                const uint8_t* p = adapter.blob_stable(l, e);
                check(p != nullptr, "1: blob_stable non-null");
                check(region_matches(p, expert_file_offset(l, e), expert_bytes(l)),
                      "1: blob_stable byte equality");
                adapter.release(l, e);
            }
        }
        check(store.valid(), "1: store still valid after round trip");
    }

    // ============ 2. drop-on-release frees capacity ============
    {
        // Pool = the store's scratch (a whole max blob + one page = 12288) +
        // one 4096-page entry = exactly full.
        Pool pool;
        auto ec = make_pool(pool, 4 * kPage);
        check(!ec, "2: create tight 16KiB pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "2: open on tight pool");

        PoolExpertSourceAdapter adapter(store);

        const uint8_t* a = adapter.blob_stable(0, 0);  // 64B expert, reserves page
        check(a != nullptr, "2: borrow (0,0)");
        check(region_matches(a, 0, 64), "2: (0,0) bytes correct");

        // Live borrower blocks a second admission (pool full).
        const uint8_t* b = adapter.blob_stable(0, 1);
        check(b == nullptr, "2: live borrower blocks second admission");

        // Release (0,0) -> admission of (0,1) succeeds.
        adapter.release(0, 0);
        const uint8_t* b2 = adapter.blob_stable(0, 1);
        check(b2 != nullptr, "2: admission after release");
        check(region_matches(b2, 64, 64), "2: (0,1) bytes correct");
        adapter.release(0, 1);
    }

    // ============ 3. pressure -> nullptr (no throw, no drop of live borrowers) ============
    {
        Pool pool;
        auto ec = make_pool(pool, 4 * kPage);
        check(!ec, "3: create tight pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "3: open store");

        PoolExpertSourceAdapter adapter(store);

        const uint8_t* live = adapter.blob_stable(0, 0);  // fills pool
        check(live != nullptr, "3: borrow (0,0) fills pool");

        // Pressure: next acquire returns nullptr, throws nothing, live borrower kept.
        bool threw = false;
        const uint8_t* under = nullptr;
        try { under = adapter.blob_stable(0, 1); }
        catch (...) { threw = true; }
        check(!threw, "3: pressure: no throw");
        check(under == nullptr, "3: pressure: nullptr on refuse");
        check(adapter.blob_stable(0, 0) == live, "3: live borrower still valid");
        adapter.release(0, 0);
    }

    // ============ 4. double-borrow refcount ============
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "4: create pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "4: open store");

        PoolExpertSourceAdapter adapter(store);

        const uint8_t* p1 = adapter.blob_stable(0, 0);
        const uint8_t* p2 = adapter.blob_stable(0, 0);  // double-borrow same key
        check(p1 != nullptr && p2 != nullptr && p1 == p2, "4: double-borrow same pointer");

        // One release leaves the borrower alive (refcount 2 -> 1).
        adapter.release(0, 0);
        check(adapter.blob_stable(0, 0) == p1, "4: borrower alive after one release");
        adapter.release(0, 0);
        // Second release drops it; a fresh borrow may be a different mapping.
        adapter.release(0, 0);  // extra release is a no-op (count already 0)
        check(adapter.blob_stable(0, 0) != nullptr, "4: re-borrow after full release");
    }

    // ============ 5. note-deferral: fake never completes (borrower retained) ============
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "5: create pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "5: open store");

        g_hook = HookLog{0, false};
        PoolExpertSourceAdapter adapter(store, fake_hook);

        const uint8_t* p = adapter.blob_stable(0, 0);
        check(p != nullptr, "5: borrow (0,0)");

        // note_async_read with a hook that never completes: borrower retained.
        adapter.note_async_read(p, nullptr);
        check(g_hook.noted == 1, "5: hook called once");
        check(adapter.blob_stable(0, 0) == p, "5: borrower retained (never completes)");
        check(store.valid(), "5: store still valid");
        adapter.release(0, 0);
    }

    // ============ 6. note-deferral: event completes (recycled on sweep, no release) ============
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "6: create pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "6: open store");

        g_hook = HookLog{0, false};
        PoolExpertSourceAdapter adapter(store, fake_hook);

        const uint8_t* p = adapter.blob_stable(0, 0);   // count 1, never released
        check(p != nullptr, "6: borrow (0,0)");
        check(adapter.borrowed(0, 0), "6: borrower table has (0,0)");

        adapter.note_async_read(p, nullptr);            // pending, hook called, retained
        check(g_hook.noted == 1, "6: hook called once");
        check(adapter.blob_stable(0, 0) == p, "6: borrower still there right after note");

        // The event lands.  A sweep triggered by an UNRELATED note (no release
        // of (0,0) anywhere - the fill pattern never calls release) must
        // recycle the completed borrower despite count > 0: legacy recycles
        // the stage buffer once the event after the LAST async copy completes.
        adapter.mark_completed(0, 0);
        const uint8_t* q = adapter.blob_stable(0, 1);
        check(q != nullptr, "6: borrow (0,1)");
        adapter.note_async_read(q, nullptr);            // sweep runs inside
        check(!adapter.borrowed(0, 0), "6: completed borrower recycled without release");
        check(adapter.borrowed(0, 1), "6: pending (0,1) survives the sweep");
    }

    // ============ 7. re-arm: a later note supersedes a completed-but-unswept event ============
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "7: create pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "7: open store");

        g_hook = HookLog{0, false};
        PoolExpertSourceAdapter adapter(store, fake_hook);

        const uint8_t* r = adapter.blob_stable(1, 0);
        check(r != nullptr, "7: borrow (1,0)");
        adapter.note_async_read(r, nullptr);
        adapter.mark_completed(1, 0);                   // completed but NOT yet swept
        adapter.note_async_read(r, nullptr);            // re-arm: a newer copy is in flight
        check(adapter.borrowed(1, 0), "7: re-armed borrower alive right after re-note");

        const uint8_t* s = adapter.blob_stable(1, 1);   // trigger a sweep
        check(s != nullptr, "7: borrow (1,1)");
        adapter.note_async_read(s, nullptr);
        check(adapter.borrowed(1, 0), "7: re-armed borrower survives sweep (new event pending)");
        check(adapter.blob_stable(1, 0) == r, "7: same pointer while re-armed");

        adapter.mark_completed(1, 0);                   // the newest event lands
        const uint8_t* t = adapter.blob_stable(1, 2);
        check(t != nullptr, "7: borrow (1,2)");
        adapter.note_async_read(t, nullptr);            // sweep
        check(!adapter.borrowed(1, 0), "7: drops after the final completion");
    }

    // ============ 8. transient: true for every pair, creates no borrower ============
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "8: create pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "8: open store");
        PoolExpertSourceAdapter adapter(store);

        bool all_transient = true;
        for (int64_t l = 0; l < 3; ++l)
            for (int64_t e = 0; e < 4; ++e) all_transient = all_transient && adapter.transient(l, e);
        check(all_transient, "8: transient() true for all 12 pairs");
        check(!adapter.borrowed(0, 0) && !adapter.borrowed(2, 3), "8: transient() created no borrower");
        check(store.stats().misses == 0, "8: transient() read nothing");
    }

    // ============ 9. copy_blob: the exact bytes, no borrower left behind ============
    {
        Pool pool;
        auto ec = make_pool(pool, 256ull << 10);
        check(!ec, "9: create pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "9: open store");
        PoolExpertSourceAdapter adapter(store);

        const int64_t pairs[5][2] = {{0, 0}, {0, 3}, {1, 2}, {2, 0}, {2, 3}};
        for (const auto& p : pairs) {
            std::vector<uint8_t> buf((size_t) expert_bytes(p[0]), 0xAB);
            check(adapter.copy_blob(p[0], p[1], buf.data()), "9: copy_blob true");
            check(region_matches(buf.data(), expert_file_offset(p[0], p[1]), expert_bytes(p[0])),
                  "9: copy_blob byte equality");
            check(!adapter.borrowed(p[0], p[1]), "9: copy_blob left no borrower");
        }
        // A pool hit must not re-read the file: (2,3) was just copied, copy it again.
        const uint64_t miss_before = store.stats().misses;
        std::vector<uint8_t> again((size_t) expert_bytes(2), 0);
        check(adapter.copy_blob(2, 3, again.data()), "9: second copy_blob true");
        check(store.stats().misses == miss_before, "9: second copy is a pool hit, no file read");
        check(region_matches(again.data(), expert_file_offset(2, 3), expert_bytes(2)),
              "9: second copy bytes equal");
        check(adapter.copy_blob(0, 0, nullptr) == false, "9: null dst refused");
    }

    // ============ 10. a full sweep on a pool far smaller than the set never runs dry ====
    {
        // Usable payload space = one 8192-byte page: the 12 pairs' payloads
        // (20,768 B) and even the store scratch (12,288 B) do not fit together
        // with more than one admitted blob.  The manager path used to hold a
        // borrower per pair for the whole sweep; the copy path must not.
        Pool pool;
        auto ec = make_pool(pool, 20 * 1024);
        check(!ec, "10: create 20KiB pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "10: open store on the small pool");
        const uint64_t scratch = store.stats().scratch_bytes;
        check(scratch > 0 && scratch < (20 * 1024), "10: store scratch leaves one page usable");

        PoolExpertSourceAdapter adapter(store);
        bool all_ok = true, all_bytes = true, no_borrowers = true;
        for (int64_t l = 0; l < 3; ++l) {
            for (int64_t e = 0; e < 4; ++e) {
                std::vector<uint8_t> buf((size_t) expert_bytes(l), 0);
                all_ok = all_ok && adapter.copy_blob(l, e, buf.data());
                all_bytes = all_bytes && region_matches(buf.data(), expert_file_offset(l, e),
                                                        expert_bytes(l));
                no_borrowers = no_borrowers && !adapter.borrowed(l, e);
            }
        }
        check(all_ok, "10: all 12 pairs copied on the small pool (no exhaustion)");
        check(all_bytes, "10: every copied pair has the exact bytes");
        check(no_borrowers, "10: no borrower left behind");
        check(store.stats().evictions > 0, "10: the cache evicted instead of running dry");
        check(store.stats().resident_bytes <= 8192,
              "10: resident payloads fit one 8192-byte page, not the 12-pair set");
        check(pool.stats().reserved_live_bytes ==
                  scratch + store.cache_stats().cached_bytes,
              "10: pool hold == scratch + cache's own hold");

        // The honest refusal: a 5000-byte blob cannot be admitted at all here.
        // (Its 8192-byte page is exactly the usable space, so keep this pair for
        // a pool with page-size payload only.)
    }

    // ============ 11. copy_blobs(n>1) is byte-identical to n copy_blob calls =====
    {
        Pool pool;
        auto ec = make_pool(pool, 20 * 1024);
        check(!ec, "11: create 20KiB pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "11: open store");
        PoolExpertSourceAdapter adapter(store);

        // All 12 pairs, in a mixed order (the window spans layers).
        int32_t ls[12], es[12];
        size_t k = 0;
        for (int64_t e = 0; e < 4; ++e)
            for (int64_t l = 2; l >= 0; --l) { ls[k] = (int32_t) l; es[k] = (int32_t) e; ++k; }
        std::vector<std::vector<uint8_t>> one(12), many(12);
        for (size_t i = 0; i < 12; ++i) {
            one[i].assign((size_t) expert_bytes(ls[i]), 0);
            many[i].assign((size_t) expert_bytes(ls[i]), 0);
        }
        std::vector<uint8_t*> dst;
        for (size_t i = 0; i < 12; ++i) dst.push_back(many[i].data());

        bool single = true;
        for (size_t i = 0; i < 12; ++i)
            single = single && adapter.copy_blob(ls[i], es[i], one[i].data());
        check(single, "11: 12 copy_blob calls all true");
        check(adapter.copy_blobs(ls, es, dst.data(), 12), "11: copy_blobs(12) true");
        bool identical = true;
        for (size_t i = 0; i < 12; ++i)
            identical = identical && one[i] == many[i] &&
                        region_matches(many[i].data(), expert_file_offset(ls[i], es[i]),
                                       expert_bytes(ls[i]));
        check(identical, "11: copy_blobs == copy_blob, pair by pair");
        bool clean = true;
        for (int64_t l = 0; l < 3; ++l)
            for (int64_t e = 0; e < 4; ++e) clean = clean && !adapter.borrowed(l, e);
        check(clean, "11: copy_blobs left no borrower");
        check(pool.stats().reserved_live_bytes == store.stats().scratch_bytes +
                                                     store.cache_stats().cached_bytes,
              "11: pool hold back to scratch + cache hold");
        // A pair the store refuses (invalid expert) reports false, not a crash.
        std::vector<uint8_t> bad(64, 0);
        check(adapter.copy_blobs(nullptr, nullptr, nullptr, 0),
              "11: n==0 with null arrays is a no-op true");
        int32_t bl = 0, be = 99; uint8_t* bd = bad.data();
        check(!adapter.copy_blobs(&bl, &be, &bd, 1), "11: invalid expert refused");
        check(!adapter.borrowed(0, 99), "11: refusal left no borrower");
    }

    // ============ 12. admission retry: DEFAULT OFF is today's behaviour ============
    {
        // No test override: this is the real default path with
        // STRATA_RAM_POOL_ADMIT_RETRY unset (case 0 of main unsets it).
        Pool pool;
        auto ec = make_pool(pool, 4 * kPage);
        check(!ec, "12: create tight 16KiB pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "12: open store");
        PoolExpertSourceAdapter adapter(store);

        const uint8_t* live = adapter.blob_stable(0, 0);   // fills the pool
        check(live != nullptr, "12: borrow (0,0) fills pool");

        std::vector<uint8_t> buf(64, 0);
        const uint64_t misses_before = store.stats().misses;
        const auto t0 = std::chrono::steady_clock::now();
        const bool ok = adapter.copy_blob(0, 1, buf.data());
        const double ms = std::chrono::duration<double, std::milli>(
                              std::chrono::steady_clock::now() - t0).count();
        check(!ok, "12: default off: refusal is immediate false");
        check(ms < 5.0, "12: default off: no wait");
        check(store.stats().misses == misses_before + 1, "12: default off: exactly one acquire");
        adapter.release(0, 0);
    }

    // ============ 13. retry ON: a competing lease released in the window ============
    {
        Pool pool;
        auto ec = make_pool(pool, 4 * kPage);
        check(!ec, "13: create tight 16KiB pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "13: open store");
        PoolExpertSourceAdapter adapter(store);
        pool_expert_source_adapter_set_admit_retry_for_test(1);

        const uint8_t* live = adapter.blob_stable(0, 0);   // the competing lease
        check(live != nullptr, "13: borrow (0,0) fills pool");

        // The "DMA completion": released ~3 ms in, i.e. inside the retry window
        // (backoff 0.5, 1, 2, 4 ... ms) and well inside the 20 ms deadline.
        std::thread releaser([&adapter] {
            std::this_thread::sleep_for(std::chrono::milliseconds(3));
            adapter.release(0, 0);
        });

        std::vector<uint8_t> buf(64, 0xAB);
        const uint64_t misses_before = store.stats().misses;
        const auto t0 = std::chrono::steady_clock::now();
        const bool ok = adapter.copy_blob(0, 1, buf.data());
        const double ms = std::chrono::duration<double, std::milli>(
                              std::chrono::steady_clock::now() - t0).count();
        releaser.join();

        check(ok, "13: retry succeeds once the lease is released");
        check(region_matches(buf.data(), expert_file_offset(0, 1), 64),
              "13: retried copy has the exact bytes");
        check(ms >= 2.0, "13: it really waited for the release");
        check(ms < 1000.0, "13: and the wait stayed bounded");
        check(store.stats().misses >= misses_before + 2, "13: more than one acquire attempt");
        check(!adapter.borrowed(0, 0) && !adapter.borrowed(0, 1), "13: no borrower left behind");
        pool_expert_source_adapter_set_admit_retry_for_test(-1);
    }

    // ============ 14. retry ON: nothing released -> bounded failure ============
    {
        Pool pool;
        auto ec = make_pool(pool, 4 * kPage);
        check(!ec, "14: create tight 16KiB pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "14: open store");
        PoolExpertSourceAdapter adapter(store);
        pool_expert_source_adapter_set_admit_retry_for_test(1);

        const uint8_t* live = adapter.blob_stable(0, 0);
        check(live != nullptr, "14: borrow (0,0) fills pool, nothing will release it");

        std::vector<uint8_t> buf(64, 0xCD);
        const uint64_t misses_before = store.stats().misses;
        const auto t0 = std::chrono::steady_clock::now();
        const bool ok = adapter.copy_blob(0, 1, buf.data());
        const double ms = std::chrono::duration<double, std::milli>(
                              std::chrono::steady_clock::now() - t0).count();
        const uint64_t attempts = store.stats().misses - misses_before;

        check(!ok, "14: no release: refusal is still false");
        check(ms >= 15.0, "14: it spent the window, did not fail early");
        check(ms <= 500.0, "14: it failed by the deadline, not forever");
        check(buf[0] == 0xCD, "14: nothing was copied into the caller's buffer");
        check(attempts >= 2 && attempts <= PoolExpertSourceAdapter::kAdmitRetryAttempts,
              "14: attempt count stayed within the fixed budget");
        std::fprintf(stderr, "note: 14: %.2f ms, %llu acquire attempts\n",
                     ms, (unsigned long long) attempts);
        adapter.release(0, 0);
        pool_expert_source_adapter_set_admit_retry_for_test(-1);
    }

    // ============ 15. retry ON: permanent causes are refused unretried ============
    {
        Pool pool;
        auto ec = make_pool(pool, 4 * kPage);
        check(!ec, "15: create tight 16KiB pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "15: open store");
        PoolExpertSourceAdapter adapter(store);
        store.stats();   // warm nothing; just make sure stats() is callable
        const uint64_t usable = store.stats().usable_bytes;
        check(usable > 0 && usable < 5000, "15: usable pool space is smaller than a layer-2 blob");
        pool_expert_source_adapter_set_admit_retry_for_test(1);

        // (a) bad geometry: unknown expert.  The store refuses with
        // invalid_argument before any pool admission.
        std::vector<uint8_t> bad(64, 0xEE);
        uint64_t misses_before = store.stats().misses;
        auto t0 = std::chrono::steady_clock::now();
        check(!adapter.copy_blob(0, 99, bad.data()), "15: unknown expert refused");
        double ms = std::chrono::duration<double, std::milli>(
                        std::chrono::steady_clock::now() - t0).count();
        check(ms < 5.0, "15: unknown expert is not retried");
        check(store.stats().misses == misses_before, "15: unknown expert never reached the cache");

        // (b) a blob that can never fit this pool (5000 B > 4096 B usable):
        // the pool reports no_buffer_space, but no release can ever make room.
        std::vector<uint8_t> big(5000, 0xEE);
        misses_before = store.stats().misses;
        t0 = std::chrono::steady_clock::now();
        check(!adapter.copy_blob(2, 0, big.data()), "15: blob larger than the usable pool refused");
        ms = std::chrono::duration<double, std::milli>(
                 std::chrono::steady_clock::now() - t0).count();
        check(ms < 5.0, "15: oversized blob is not retried (geometry pre-check)");
        check(store.stats().misses == misses_before + 1, "15: oversized blob tried exactly once");
        pool_expert_source_adapter_set_admit_retry_for_test(-1);
    }

    // ============ 16. retry ON through blob_stable (the mu_-free wait) ============
    {
        Pool pool;
        auto ec = make_pool(pool, 4 * kPage);
        check(!ec, "16: create tight 16KiB pool");
        ExpertLayout layout;
        build_layout(layout);
        PoolExpertStore store(pool);
        ec = store.open(g_fixture_path.c_str(), layout);
        check(!ec, "16: open store");
        PoolExpertSourceAdapter adapter(store);
        pool_expert_source_adapter_set_admit_retry_for_test(1);

        const uint8_t* live = adapter.blob_stable(0, 0);
        check(live != nullptr, "16: borrow (0,0) fills pool");

        std::thread releaser([&adapter] {
            std::this_thread::sleep_for(std::chrono::milliseconds(3));
            adapter.release(0, 0);   // needs mu_: must not be blocked by the waiter
        });
        const auto t0 = std::chrono::steady_clock::now();
        const uint8_t* p = adapter.blob_stable(0, 1);
        const double ms = std::chrono::duration<double, std::milli>(
                              std::chrono::steady_clock::now() - t0).count();
        releaser.join();

        check(p != nullptr, "16: blob_stable succeeds once the lease is released");
        check(region_matches(p, expert_file_offset(0, 1), 64), "16: borrowed bytes are correct");
        check(ms >= 2.0 && ms < 1000.0, "16: it waited, bounded");
        check(adapter.borrowed(0, 1), "16: the borrower was registered");
        adapter.release(0, 1);

        // Nothing will release this one: bounded nullptr, not a hang.
        const uint8_t* held = adapter.blob_stable(0, 2);
        check(held != nullptr, "16: borrow (0,2) fills pool again");
        const auto t1 = std::chrono::steady_clock::now();
        const uint8_t* q = adapter.blob_stable(1, 1);
        const double ms2 = std::chrono::duration<double, std::milli>(
                               std::chrono::steady_clock::now() - t1).count();
        check(q == nullptr, "16: held pool -> nullptr after the window");
        check(ms2 >= 15.0 && ms2 <= 500.0, "16: bounded, not forever");
        check(!adapter.borrowed(1, 1), "16: a refused pair registers no borrower");
        adapter.release(0, 2);
        pool_expert_source_adapter_set_admit_retry_for_test(-1);
    }

    std::fprintf(stderr, "pool_expert_source_adapter_test: %d checks, %d FAILURES\n",
                 checks, failures);
    return failures == 0 ? 0 : 1;
}
