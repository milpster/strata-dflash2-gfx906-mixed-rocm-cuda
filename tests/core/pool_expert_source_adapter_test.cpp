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
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <sys/mman.h>
#include <sys/stat.h>
#include <vector>

namespace {

using strata::core::Pool;
using strata::core::PoolLease;
using strata::core::PoolExpertStore;
using strata::core::PoolExpertSourceAdapter;
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

    std::fprintf(stderr, "pool_expert_source_adapter_test: %d checks, %d FAILURES\n",
                 checks, failures);
    return failures == 0 ? 0 : 1;
}
