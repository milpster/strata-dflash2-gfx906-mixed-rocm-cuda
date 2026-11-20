// tests/core/bounded_direct_reader_test.cpp - deterministic CPU-only tests for
// BoundedDirectReader (O_DIRECT) feeding PoolExpertCache with caller scratch
// from the same Pool.
//
// Real mmap/munmap/mlock/munlock via link-time --wrap; mlock/munlock faked.
// O_DIRECT pread is wrapped (__wrap_pread) to inject EINTR/short/EIO and to
// record fd O_DIRECT, buf==scratch, len<=cap, and syscall count. Fixture under
// the build dir (unique name, never a production model); total live mapping
// <= 1 MiB; source fixture never mutated; bytes+length compared, not checksums.
//
// Usage: bounded_direct_reader_test <build_dir>

#define STRATA_RAM_POOL_TEST 1
#include "strata/platform/bounded_direct_reader.hpp"
#include "strata/core/pool_expert_cache.hpp"
#include "strata/core/ram_pool.hpp"
#undef STRATA_RAM_POOL_TEST

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace {

using strata::core::Pool;
using strata::core::PoolLease;
using strata::core::PoolExpertCache;
using strata::platform::BoundedDirectReader;

int checks = 0, failures = 0;

static void check(bool ok, const char* label) {
    ++checks;
    std::fprintf(stderr, "%s: %s\n", ok ? "pass" : "FAIL", label);
    if (!ok) ++failures;
}

static uint8_t byte_at(uint64_t p) {
    return static_cast<uint8_t>((p * 31u) ^ (p >> 8) ^ (p >> 16) ^ 7u);
}

std::string g_fixture_path;
std::string g_full_data;

static bool write_file(const std::string& path, const std::string& data) {
    std::FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) return false;
    if (std::fwrite(data.data(), 1, data.size(), f) != data.size()) {
        std::fclose(f); return false;
    }
    return std::fclose(f) == 0;
}

static bool build_fixture(const std::string& dir) {
    const uint64_t size = 9000;   // not page-aligned: non-page-aligned EOF
    g_full_data.resize(size);
    for (uint64_t p = 0; p < size; ++p) g_full_data[p] = byte_at(p);
    g_fixture_path = dir + "/reader_fixture_20261120.bin";
    return write_file(g_fixture_path, g_full_data);
}

static bool file_contents_equal(const std::string& path,
                                const std::string& expected) {
    std::FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) return false;
    std::string got;
    char buf[4096];
    size_t n;
    while ((n = std::fread(buf, 1, sizeof buf, f)) > 0) got.append(buf, n);
    std::fclose(f);
    return got == expected;
}

// ---- link-time interposition ------------------------------------------------
namespace wrap {
int mmap_calls = 0, munmap_calls = 0, mlock_calls = 0, munlock_calls = 0;
static std::vector<std::pair<void*, uint64_t>> live_maps;
static uint64_t total_live_bytes = 0, peak_live_bytes = 0;
constexpr uint64_t kMappingLimit = 1ull << 20;

int pread_calls = 0, last_fd = -1;
void* last_buf = nullptr;
size_t last_len = 0;
int odirect_seen = 0;
enum Mode { kPass, kEintrOnce, kShort, kZero, kEio };
Mode cur_mode = kPass;
int eintr_remaining = 1;
ssize_t short_return = 0;
}  // namespace wrap

static void track_mmap(void* p, uint64_t len) {
    if (p == nullptr || p == MAP_FAILED) return;
    for (auto& e : wrap::live_maps)
        if (e.first == p) { std::fprintf(stderr, "FAIL: duplicate mmap\n"); std::abort(); }
    wrap::live_maps.push_back({p, len});
    wrap::total_live_bytes += len;
    wrap::peak_live_bytes = std::max(wrap::peak_live_bytes, wrap::total_live_bytes);
    if (wrap::total_live_bytes > wrap::kMappingLimit) std::abort();
}
static void track_munmap(void* p, uint64_t len) {
    for (auto it = wrap::live_maps.begin(); it != wrap::live_maps.end(); ++it) {
        if (it->first == p && it->second == len) {
            wrap::total_live_bytes -= len; wrap::live_maps.erase(it); return;
        }
    }
    std::fprintf(stderr, "FAIL: unmatched munmap\n"); std::abort();
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
    wrap::last_fd = fd; wrap::last_buf = buf; wrap::last_len = count;
    wrap::odirect_seen = (::fcntl(fd, F_GETFL) & O_DIRECT) ? 1 : 0;
    if (wrap::cur_mode == wrap::kEintrOnce && wrap::eintr_remaining-- > 0) {
        errno = EINTR; return -1;
    }
    if (wrap::cur_mode == wrap::kEio) { errno = EIO; return -1; }
    if (wrap::cur_mode == wrap::kShort) { wrap::cur_mode = wrap::kPass; return wrap::short_return; }
    if (wrap::cur_mode == wrap::kZero) { wrap::cur_mode = wrap::kPass; return 0; }
    return __real_pread(fd, buf, count, offset);
}
}  // extern "C"

// ---- reader unit helpers ----------------------------------------------------
static std::error_code make_pool(Pool& out, uint64_t bytes) {
    std::error_code ec;
    auto p = Pool::create(bytes, ec);
    out = std::move(p);
    return ec;
}

static bool dst_untouched(uint8_t* dst, uint64_t bytes) {
    for (uint64_t i = 0; i < bytes; ++i) if (dst[i] != 0xAA) return false;
    return true;
}

static void expect_fail_no_io(BoundedDirectReader& r, PoolLease& scratch,
                              uint64_t off, uint64_t bytes, const char* label) {
    uint8_t dst[64];
    std::memset(dst, 0xAA, sizeof dst);
    std::memset(scratch.data(), 0xBB, scratch.size());
    const auto before = wrap::pread_calls;
    std::error_code ec = r.read_exact(off, bytes, dst,
                                      (uint8_t*)scratch.data(), scratch.size());
    check(static_cast<bool>(ec), label);
    check(wrap::pread_calls == before, label);
    check(dst_untouched(dst, sizeof dst), label);
}

}  // namespace

int main(int argc, char** argv) {
    const std::string build_dir = (argc > 1) ? argv[1] : ".";
    if (!build_fixture(build_dir)) {
        std::fprintf(stderr, "FAIL: cannot write fixture\n"); return 2;
    }

    // ---- preflight: bare OS O_DIRECT using the same aligned scratch ---------
    int probe_fd = ::open(g_fixture_path.c_str(), O_RDONLY | O_DIRECT);
    if (probe_fd < 0) {
        std::fprintf(stderr, "SKIP 77: O_DIRECT open unsupported on this fs: %s\n",
                     strerror(errno));
        return 77;
    }
    uint8_t* probe_buf = nullptr;
    if (::posix_memalign((void**)&probe_buf, 4096, 4096) != 0) {
        ::close(probe_fd); return 2;
    }
    const ssize_t probe_got = ::pread(probe_fd, probe_buf, 4096, 0);
    const int probe_err = errno;
    ::close(probe_fd);
    std::free(probe_buf);
    if (probe_got < 0 && probe_err == EINVAL) {
        std::fprintf(stderr, "SKIP 77: O_DIRECT pread unsupported on this fs\n");
        return 77;
    }

    // ---- pool + aligned 4 KiB scratch (reservation visible in Pool.stats) ----
    Pool pool;
    std::error_code ec = make_pool(pool, 64ull << 10);
    check(!ec, "create 64KiB pool");
    PoolLease scratch;
    ec = pool.pool_alloc(4096, 4096, scratch);
    check(!ec && scratch.valid() && scratch.size() == 4096, "alloc 4KiB scratch");
    check(pool.stats().reserved_live_bytes == 4096, "scratch reservation visible");

    BoundedDirectReader reader;
    ec = reader.open(g_fixture_path.c_str());
    check(!ec, "open fixture");
    check(reader.is_open(), "reader is_open");
    check(reader.size() == 9000, "reader size 9000");
    check(reader.alignment() == 4096, "alignment 4096");

    // ---- S2: unaligned offset+len within one page ---------------------------
    {
        uint8_t dst[500];
        std::memset(dst, 0xAA, sizeof dst);
        std::memset(scratch.data(), 0xBB, scratch.size());
        wrap::cur_mode = wrap::kPass;
        ec = reader.read_exact(1000, 500, dst,
                               (uint8_t*)scratch.data(), scratch.size());
        check(!ec, "unaligned read 1000/500");
        check(std::memcmp(dst, g_full_data.data() + 1000, 500) == 0,
              "unaligned bytes correct");
        check(wrap::odirect_seen == 1, "pread fd has O_DIRECT");
        check(wrap::last_buf == scratch.data() && wrap::last_len <= scratch.size(),
              "pread buf==scratch len<=cap");
    }

    // ---- S3: multi-window blob larger than scratch --------------------------
    {
        std::vector<uint8_t> dst(8000);
        std::memset(dst.data(), 0xAA, dst.size());
        wrap::cur_mode = wrap::kPass;
        ec = reader.read_exact(0, 8000, dst.data(),
                               (uint8_t*)scratch.data(), scratch.size());
        check(!ec, "multi-window read 0/8000 (>scratch)");
        check(std::memcmp(dst.data(), g_full_data.data(), 8000) == 0,
              "multi-window bytes correct");
    }

    // ---- S4: non-page-aligned EOF exact success -----------------------------
    {
        uint8_t dst[808];
        std::memset(dst, 0xAA, sizeof dst);
        wrap::cur_mode = wrap::kPass;
        ec = reader.read_exact(8192, 808, dst,
                               (uint8_t*)scratch.data(), scratch.size());
        check(!ec, "non-page-aligned EOF read 8192/808");
        check(std::memcmp(dst, g_full_data.data() + 8192, 808) == 0,
              "EOF bytes correct");
    }

    // ---- S5: validation failures BEFORE I/O, output unchanged ---------------
    expect_fail_no_io(reader, scratch, 8500, 600, "past end of file");
    expect_fail_no_io(reader, scratch, UINT64_MAX, 1, "offset overflow");
    expect_fail_no_io(reader, scratch, 0, 0, "zero bytes");
    {
        uint8_t dst[64]; std::memset(dst, 0xAA, 64);
        const auto before = wrap::pread_calls;
        ec = reader.read_exact(0, 64, dst, (uint8_t*)scratch.data(), 100);
        check(static_cast<bool>(ec), "scratch too small");
        check(wrap::pread_calls == before, "scratch too small no pread");
        check(dst_untouched(dst, 64), "scratch too small dst untouched");
    }
    {
        uint8_t dst[64]; std::memset(dst, 0xAA, 64);
        ec = reader.read_exact(0, 64, dst, (uint8_t*)scratch.data(), 3000);
        check(static_cast<bool>(ec), "scratch not multiple of alignment");
        check(dst_untouched(dst, 64), "scratch not multiple dst untouched");
    }
    {
        uint8_t* s = (uint8_t*)scratch.data();
        std::memset(s, 0xAA, 64);
        const auto before = wrap::pread_calls;
        ec = reader.read_exact(0, 64, s, s, scratch.size());
        check(static_cast<bool>(ec), "dst/scratch overlap");
        check(wrap::pread_calls == before, "overlap no pread");
    }

    // ---- S6: missing path / directory preserve old reader -------------------
    {
        const bool was_open = reader.is_open();
        const uint64_t was_size = reader.size();
        ec = reader.open("/nonexistent/path/reader_fixture_20261120.bin");
        check(static_cast<bool>(ec), "missing path open fails");
        check(reader.is_open() == was_open && reader.size() == was_size,
              "missing path preserves old reader");
        uint8_t dst[16];
        ec = reader.read_exact(0, 16, dst,
                               (uint8_t*)scratch.data(), scratch.size());
        check(!ec, "old reader still reads after failed open");
    }
    {
        ec = reader.open(build_dir.c_str());
        check(static_cast<bool>(ec), "open directory fails");
        check(reader.is_open(), "directory open preserves old reader");
    }

    // ---- S7: close/reopen ---------------------------------------------------
    reader.close();
    check(!reader.is_open(), "closed reader not open");
    ec = reader.open(g_fixture_path.c_str());
    check(!ec && reader.is_open() && reader.size() == 9000, "reopen works");

    // ---- S8: EINTR injected, reader recovers --------------------------------
    {
        wrap::cur_mode = wrap::kEintrOnce; wrap::eintr_remaining = 1;
        const int before = wrap::pread_calls;
        uint8_t dst[100];
        ec = reader.read_exact(2000, 100, dst,
                               (uint8_t*)scratch.data(), scratch.size());
        check(!ec, "EINTR read recovers");
        check(wrap::pread_calls > before, "EINTR retried (>=2 preads)");
        check(std::memcmp(dst, g_full_data.data() + 2000, 100) == 0,
              "EINTR bytes correct");
        wrap::cur_mode = wrap::kPass;
    }

    // ---- S9: EIO and insufficient short refuse safely -----------------------
    {
        wrap::cur_mode = wrap::kEio;
        uint8_t dst[100]; std::memset(dst, 0xAA, 100);
        const auto before = wrap::pread_calls;
        ec = reader.read_exact(2000, 100, dst,
                               (uint8_t*)scratch.data(), scratch.size());
        check(static_cast<bool>(ec), "EIO refused");
        check(wrap::pread_calls == before + 1, "EIO one pread then stop");
        check(dst_untouched(dst, 100), "EIO dst untouched");
        wrap::cur_mode = wrap::kPass;

        wrap::cur_mode = wrap::kShort; wrap::short_return = 100;
        std::memset(dst, 0xAA, 100);
        ec = reader.read_exact(2000, 500, dst,
                               (uint8_t*)scratch.data(), scratch.size());
        check(static_cast<bool>(ec), "insufficient short refused");
        check(dst_untouched(dst, 100), "short dst untouched");
        wrap::cur_mode = wrap::kPass;
    }

    // ---- PoolExpertCache integration: synthetic NONuniform keys -------------
    struct Entry { int64_t layer, expert; uint64_t offset, bytes; };
    const Entry entries[] = {
        {0, 0, 1000, 500}, {0, 1, 4096, 128}, {1, 0, 8192, 808},
    };
    auto cache_cb = [&](int64_t l, int64_t e, uint8_t* d, uint64_t b)
                          -> std::error_code {
        for (const auto& en : entries)
            if (en.layer == l && en.expert == e)
                return reader.read_exact(en.offset, en.bytes, d,
                                         (uint8_t*)scratch.data(),
                                         scratch.size());
        return std::make_error_code(std::errc::invalid_argument);
    };

    // ---- T10: cache hit does not read ---------------------------------------
    {
        PoolExpertCache cache(pool);
        int reads = 0;
        auto r = [&](int64_t l, int64_t e, uint8_t* d, uint64_t b)
                         -> std::error_code { ++reads; return cache_cb(l, e, d, b); };
        PoolLease out;
        ec = cache.acquire(0, 0, 500, r, out);
        check(!ec && out.valid(), "cache miss acquires (0,0)");
        check(reads == 1, "miss reads once");
        check(std::memcmp(out.data(), g_full_data.data() + 1000, 500) == 0,
              "cache payload nonuniform bytes correct");
        const void* hit_ptr = out.data();
        ec = cache.acquire(0, 0, 500, r, out);
        check(!ec && reads == 1, "cache hit does not read again");
        check(out.data() == hit_ptr, "hit returns same lease");
        check(cache.stats().hits == 1, "hit counted");
        out = PoolLease{};
    }

    // ---- T11: eviction+reload restores ALL bytes after owner overwrite ------
    // Fresh pool (no scratch competing) so full-capacity alloc can succeed.
    {
        Pool tpool;
        std::error_code tec = make_pool(tpool, 64ull << 10);
        check(!tec, "T11 create 64KiB pool");
        PoolExpertCache cache(tpool);
        PoolLease borrower;
        ec = cache.acquire(0, 1, 128, cache_cb, borrower);
        check(!ec && borrower.valid(), "borrower (0,1)");
        const void* borrower_ptr = borrower.data();
        PoolLease full;
        ec = tpool.pool_alloc(tpool.capacity(), 4096, full);
        check(ec && !full.valid(), "live borrower blocks full-pool alloc");

        cache.release(0, 1);
        ec = tpool.pool_alloc(tpool.capacity(), 4096, full);
        check(ec && !full.valid(), "full-pool refused with borrower alive");
        borrower = PoolLease{};
        ec = tpool.pool_alloc(tpool.capacity(), 4096, full);
        check(!ec && full.valid(), "full-pool succeeds after release");
        check(full.data() == borrower_ptr, "reuses same address");
        std::memset(full.data(), 0xAA, full.size());   // OWNER overwrites pool memory
        full = PoolLease{};

        PoolLease fresh;
        ec = cache.acquire(0, 1, 128, cache_cb, fresh);
        check(!ec && fresh.valid(), "reload (0,1) after owner overwrite");
        check(std::memcmp(fresh.data(), g_full_data.data() + 4096, 128) == 0,
              "reload restores ALL original bytes from fixture");
        fresh = PoolLease{};
    }

    // ---- T12: short/error cache miss preserves output, publishes nothing -----
    {
        PoolExpertCache cache(pool);
        PoolLease out;
        ec = cache.acquire(0, 0, 500, cache_cb, out);
        check(!ec && out.valid(), "warm (0,0) populated");
        const void* ptr = out.data();
        const uint64_t sz = out.size();
        const auto entries_before = cache.stats().entries;
        const auto reserved_before = pool.stats().reserved_live_bytes;

        // Fresh out for the failing acquire: on failure nothing is published,
        // and the previously populated `out` is preserved.
        PoolLease short_out;
        wrap::cur_mode = wrap::kShort; wrap::short_return = 10;
        ec = cache.acquire(1, 0, 808, cache_cb, short_out);
        wrap::cur_mode = wrap::kPass;
        check(static_cast<bool>(ec), "short cache miss -> error");
        check(!short_out.valid(), "short miss: nothing published");
        check(cache.stats().entries == entries_before, "short miss: no new entry");
        check(pool.stats().reserved_live_bytes == reserved_before,
              "short miss: candidate pool bytes released");
        check(out.data() == ptr && out.size() == sz, "short miss: output preserved");
        out = PoolLease{};
    }

    // ---- fixture immutable (bytes + length) + mapping limits ----------------
    // Release pool + scratch so the final mapping checks see a teardown state.
    scratch = PoolLease{};
    pool = Pool{};
    check(file_contents_equal(g_fixture_path, g_full_data),
          "fixture file unchanged at end (bytes + length)");
    std::fprintf(stderr, "peak_mapping_bytes=%llu limit=%llu\n",
                 (unsigned long long)wrap::peak_live_bytes,
                 (unsigned long long)wrap::kMappingLimit);
    check(wrap::peak_live_bytes <= wrap::kMappingLimit, "peak <= 1 MiB");
    check(wrap::live_maps.empty(), "final: no live mappings");
    check(wrap::mmap_calls == wrap::munmap_calls, "final: mmap==munmap");

    std::fprintf(stderr, "bounded_direct_reader_test: %d checks, %d FAILURES\n",
                 checks, failures);
    return failures == 0 ? 0 : 1;
}
