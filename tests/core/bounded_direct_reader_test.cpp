// tests/core/bounded_direct_reader_test.cpp - deterministic CPU-only tests for
// BoundedDirectReader (O_DIRECT) feeding PoolExpertCache with caller scratch
// from the same Pool.
//
// Real mmap/munmap/mlock/munlock via link-time --wrap; mlock/munlock faked.
// O_DIRECT pread is wrapped (__wrap_pread) to inject EINTR/short/EIO and to
// record fd O_DIRECT, buf==scratch, count<=cap&aligned on EVERY call.
// Fixture under build dir (unique name, never a production model); total live
// mapping <= 1 MiB; source fixture never mutated; bytes+length compared,
// not checksums. Skip 77 ONLY when the real filesystem lacks O_DIRECT.
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

// Exact bytes + length; also fails on fread error / fclose failure.
static bool file_contents_equal(const std::string& path,
                                const std::string& expected) {
    std::FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) return false;
    std::string got;
    char buf[4096];
    size_t n;
    while ((n = std::fread(buf, 1, sizeof buf, f)) > 0) got.append(buf, n);
    if (std::ferror(f)) { std::fclose(f); return false; }
    if (std::fclose(f) != 0) return false;
    return got == expected;
}

// ---- link-time interposition ------------------------------------------------
namespace wrap {
int mmap_calls = 0, munmap_calls = 0, mlock_calls = 0, munlock_calls = 0;
static std::vector<std::pair<void*, uint64_t>> live_maps;
static uint64_t total_live_bytes = 0, peak_live_bytes = 0;
constexpr uint64_t kMappingLimit = 1ull << 20;

int pread_calls = 0;
int violations = 0;   // fcntl<0, !O_DIRECT, buf!=scratch, count>cap, count!aligned
void* scratch_buf = nullptr;
void* last_buf = nullptr;
size_t last_len = 0;
int last_fd = -1;
uint64_t scratch_cap = 0;
enum Mode { kPass, kEintrOnce, kShort, kZero, kEio, kEio2nd };
Mode cur_mode = kPass;
int eintr_remaining = 1;
ssize_t short_return = 0;
int eio_window = 0;   // kEio2nd: fail on the 2nd covering window of a read
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
    const int flags = ::fcntl(fd, F_GETFL);
    if (flags < 0 || (flags & O_DIRECT) == 0) ++wrap::violations;
    if (buf != wrap::scratch_buf) ++wrap::violations;
    if (count > wrap::scratch_cap || (count % 4096) != 0) ++wrap::violations;
    wrap::last_fd = fd; wrap::last_buf = buf; wrap::last_len = count;

    // kEio2nd: fail the 2nd covering window (simulate first chunk copied,
    // second window failing mid-read).
    if (wrap::cur_mode == wrap::kEio2nd) {
        if (wrap::eio_window++ >= 1) { errno = EIO; return -1; }
        wrap::cur_mode = wrap::kPass;  // first window succeeds via real pread
    }
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
static bool scratch_untouched(PoolLease& scratch) {
    uint8_t* s = (uint8_t*)scratch.data();
    for (uint64_t i = 0; i < scratch.size(); ++i) if (s[i] != 0xBB) return false;
    return true;
}

static void expect_fail_no_io(BoundedDirectReader& r, PoolLease& scratch,
                              uint8_t* dst, uint64_t dst_bytes,
                              uint64_t off, uint64_t bytes,
                              uint64_t scratch_bytes, const char* label) {
    std::memset(dst, 0xAA, dst_bytes);
    std::memset(scratch.data(), 0xBB, scratch.size());
    const auto before = wrap::pread_calls;
    const auto before_viol = wrap::violations;
    std::error_code ec = r.read_exact(off, bytes, dst,
                                      (uint8_t*)scratch.data(), scratch_bytes);
    check(static_cast<bool>(ec), label);
    check(wrap::pread_calls == before, (std::string(label) + " no pread issued").c_str());
    check(wrap::violations == before_viol, (std::string(label) + " no pread violations").c_str());
    check(dst_untouched(dst, dst_bytes), (std::string(label) + " dst untouched").c_str());
    check(scratch_untouched(scratch), (std::string(label) + " scratch untouched").c_str());
}

}  // namespace

int main(int argc, char** argv) {
    const std::string build_dir = (argc > 1) ? argv[1] : ".";
    if (!build_fixture(build_dir)) {
        std::fprintf(stderr, "FAIL: cannot write fixture\n"); return 2;
    }

    // ---- pool + aligned 4 KiB scratch FIRST (setup must succeed or stop) ----
    Pool pool;
    std::error_code ec = make_pool(pool, 64ull << 10);
    check(!ec, "create 64KiB pool");
    if (ec) return 2;
    PoolLease scratch;
    ec = pool.pool_alloc(4096, 4096, scratch);
    check(!ec && scratch.valid() && scratch.size() == 4096, "alloc 4KiB scratch");
    check(pool.stats().reserved_live_bytes == 4096, "scratch reservation visible");
    if (!ec && scratch.valid()) {
        wrap::scratch_buf = scratch.data();
        wrap::scratch_cap = scratch.size();
    } else {
        return 2;
    }

    // ---- preflight: bare OS O_DIRECT using THAT pool scratch -----------------
    int probe_fd = ::open(g_fixture_path.c_str(), O_RDONLY | O_DIRECT);
    if (probe_fd < 0) {
        const int openerr = errno;
        std::fprintf(stderr, "SKIP 77: O_DIRECT open unsupported on this fs (%s)\n",
                     strerror(openerr));
        return 77;
    }
    const ssize_t probe_got = ::pread(probe_fd, scratch.data(), 4096, 0);
    const int probe_err = errno;
    ::close(probe_fd);
    // EINVAL/EOPNOTSUPP/ENOSYS => real unsupported -> skip 77. Anything else
    // (EIO/permission/missing) is a test failure, not a silent continue.
    if (probe_got < 0 &&
        (probe_err == EINVAL || probe_err == EOPNOTSUPP || probe_err == ENOSYS)) {
        std::fprintf(stderr, "SKIP 77: O_DIRECT pread unsupported on this fs\n");
        return 77;
    }
    check(probe_got == 4096, "preflight pread returns full 4096 bytes");

    BoundedDirectReader reader;
    ec = reader.open(g_fixture_path.c_str());
    check(!ec, "open fixture");
    check(reader.is_open(), "reader is_open");
    check(reader.size() == 9000, "reader size 9000");
    check(reader.alignment() == 4096, "alignment 4096");

    // ---- S1: unaligned offset+len within one page ---------------------------
    {
        uint8_t dst[500];
        std::memset(dst, 0xAA, sizeof dst);
        std::memset(scratch.data(), 0xBB, scratch.size());
        wrap::cur_mode = wrap::kPass;
        const auto before = wrap::pread_calls;
        ec = reader.read_exact(1000, 500, dst,
                               (uint8_t*)scratch.data(), scratch.size());
        check(!ec, "unaligned read 1000/500");
        check(std::memcmp(dst, g_full_data.data() + 1000, 500) == 0,
              "unaligned bytes correct");
        check(wrap::pread_calls == before + 1, "exactly one pread");
        check(wrap::last_buf == scratch.data() && wrap::last_len <= scratch.size(),
              "pread buf==scratch len<=cap");
        check(wrap::violations == 0, "pread fd has O_DIRECT + aligned count");
    }

    // ---- S2: true unaligned multi-window offset257/len8000 > scratch -------
    {
        std::vector<uint8_t> dst(8000);
        std::memset(dst.data(), 0xAA, dst.size());
        wrap::cur_mode = wrap::kPass;
        const auto before = wrap::pread_calls;
        ec = reader.read_exact(257, 8000, dst.data(),
                               (uint8_t*)scratch.data(), scratch.size());
        check(!ec, "unaligned multi-window read 257/8000 (>scratch)");
        check(std::memcmp(dst.data(), g_full_data.data() + 257, 8000) == 0,
              "multi-window bytes correct");
        check(wrap::pread_calls > before, "multi-window issues >=2 preads");
        check(wrap::violations == 0, "zero pread violations across multi-window");
    }

    // ---- S3: non-page-aligned EOF exact success -----------------------------
    {
        uint8_t dst[808];
        std::memset(dst, 0xAA, sizeof dst);
        wrap::cur_mode = wrap::kPass;
        const auto before = wrap::pread_calls;
        ec = reader.read_exact(8192, 808, dst,
                               (uint8_t*)scratch.data(), scratch.size());
        check(!ec, "non-page-aligned EOF read 8192/808");
        check(std::memcmp(dst, g_full_data.data() + 8192, 808) == 0,
              "EOF bytes correct");
        check(wrap::pread_calls == before + 1, "EOF read exactly one pread");
    }

    // ---- S4: validation failures BEFORE I/O; dst AND scratch unchanged ------
    uint8_t dst64[64];
    expect_fail_no_io(reader, scratch, dst64, 64, 8500, 600, scratch.size(),
                      "past end of file");
    expect_fail_no_io(reader, scratch, dst64, 64, UINT64_MAX, 1, scratch.size(),
                      "offset overflow");
    expect_fail_no_io(reader, scratch, dst64, 64, 0, 0, scratch.size(),
                      "zero bytes");
    expect_fail_no_io(reader, scratch, dst64, 64, 0, 64, 100,
                      "scratch cap 100 too small");
    {
        // scratch 3000 >= alignment but NOT a multiple of alignment
        uint8_t dst64b[64];
        std::memset(dst64b, 0xAA, 64);
        std::memset(scratch.data(), 0xBB, scratch.size());
        const auto before = wrap::pread_calls;
        ec = reader.read_exact(0, 64, dst64b, (uint8_t*)scratch.data(), 3000);
        check(static_cast<bool>(ec), "scratch 3000 not multiple of alignment");
        check(wrap::pread_calls == before, "scratch 3000 no pread");
        check(dst_untouched(dst64b, 64), "scratch 3000 dst untouched");
        check(scratch_untouched(scratch), "scratch 3000 scratch untouched");
    }
    {
        uint8_t* s = (uint8_t*)scratch.data();
        std::memset(s, 0xAA, 64);
        const auto before = wrap::pread_calls;
        ec = reader.read_exact(0, 64, s, s, scratch.size());
        check(static_cast<bool>(ec), "dst/scratch overlap");
        check(wrap::pread_calls == before, "overlap no pread");
    }
    // null dst / null scratch
    {
        const auto before = wrap::pread_calls;
        ec = reader.read_exact(0, 64, nullptr,
                               (uint8_t*)scratch.data(), scratch.size());
        check(static_cast<bool>(ec), "null dst fails");
        check(wrap::pread_calls == before, "null dst no pread");
        ec = reader.read_exact(0, 64, dst64, nullptr, scratch.size());
        check(static_cast<bool>(ec), "null scratch fails");
        check(wrap::pread_calls == before, "null scratch no pread");
    }
    // closed reader
    {
        reader.close();
        const auto before = wrap::pread_calls;
        ec = reader.read_exact(0, 64, dst64,
                               (uint8_t*)scratch.data(), scratch.size());
        check(static_cast<bool>(ec), "closed reader fails");
        check(wrap::pread_calls == before, "closed reader no pread");
        ec = reader.open(g_fixture_path.c_str());
        check(!ec, "reopen after close");
    }

    // ---- S5: missing path / directory preserve old reader -------------------
    {
        const bool was_open = reader.is_open();
        const uint64_t was_size = reader.size();
        ec = reader.open("/nonexistent/path/reader_fixture_20261120.bin");
        check(static_cast<bool>(ec), "missing path open fails");
        check(reader.is_open() == was_open && reader.size() == was_size,
              "missing path preserves old reader");
        ec = reader.read_exact(0, 16, dst64,
                               (uint8_t*)scratch.data(), scratch.size());
        check(!ec, "old reader still reads after failed missing open");
    }
    {
        ec = reader.open(build_dir.c_str());
        check(static_cast<bool>(ec), "open directory fails");
        check(reader.is_open(), "directory open preserves old reader");
    }

    // ---- S6: EINTR must produce exactly 2 calls -----------------------------
    {
        wrap::cur_mode = wrap::kEintrOnce; wrap::eintr_remaining = 1;
        const int before = wrap::pread_calls;
        uint8_t dst100[100];
        ec = reader.read_exact(2000, 100, dst100,
                               (uint8_t*)scratch.data(), scratch.size());
        check(!ec, "EINTR read recovers");
        check(wrap::pread_calls == before + 2, "EINTR exactly 2 preads (1 fail + 1 ok)");
        check(std::memcmp(dst100, g_full_data.data() + 2000, 100) == 0,
              "EINTR bytes correct");
        wrap::cur_mode = wrap::kPass;
    }

    // ---- S7: EIO refuses safely; short uses valid 500 buffer ----------------
    {
        wrap::cur_mode = wrap::kEio;
        uint8_t dst500[500];
        std::memset(dst500, 0xAA, 500);
        const auto before = wrap::pread_calls;
        ec = reader.read_exact(2000, 100, dst500,
                               (uint8_t*)scratch.data(), scratch.size());
        check(static_cast<bool>(ec), "EIO refused");
        check(wrap::pread_calls == before + 1, "EIO one pread then stop");
        check(dst_untouched(dst500, 500), "EIO whole 500 buffer untouched");
        wrap::cur_mode = wrap::kPass;

        wrap::cur_mode = wrap::kShort; wrap::short_return = 100;
        std::memset(dst500, 0xAA, 500);
        ec = reader.read_exact(2000, 500, dst500,
                               (uint8_t*)scratch.data(), scratch.size());
        check(static_cast<bool>(ec), "insufficient short refused");
        check(dst_untouched(dst500, 500), "short whole 500 buffer untouched");
        wrap::cur_mode = wrap::kPass;
    }

    // ---- PoolExpertCache integration: synthetic NONuniform keys -------------
    struct Entry { int64_t layer, expert; uint64_t offset, bytes; };
    const Entry entries[] = {
        {0, 0, 1000, 500}, {0, 1, 4096, 128}, {1, 0, 8192, 808}, {0, 2, 257, 8000},
    };
    auto cache_cb = [&](int64_t l, int64_t e, uint8_t* d, uint64_t b)
                          -> std::error_code {
        for (const auto& en : entries) {
            if (en.layer == l && en.expert == e) {
                // callback MUST validate requested bytes == entry.bytes
                check(b == en.bytes, "cache_cb validates entry bytes");
                return reader.read_exact(en.offset, en.bytes, d,
                                         (uint8_t*)scratch.data(),
                                         scratch.size());
            }
        }
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

    // ---- T11: same-pool reuse; borrower blocks usable-capacity alloc ------
    // usable capacity = pool.capacity() - scratch.reserved_size()
    {
        const uint64_t usable = pool.capacity() - scratch.size();
        PoolExpertCache cache(pool);
        PoolLease borrower;
        ec = cache.acquire(0, 1, 128, cache_cb, borrower);
        check(!ec && borrower.valid(), "borrower (0,1)");
        const void* borrower_ptr = borrower.data();
        const uint64_t borrower_sz = borrower.size();

        // live borrower: usable-capacity owner alloc refused
        PoolLease full;
        ec = pool.pool_alloc(usable, 4096, full);
        check(ec && !full.valid(), "live borrower blocks usable-capacity alloc");

        cache.release(0, 1);
        ec = pool.pool_alloc(usable, 4096, full);
        check(ec && !full.valid(), "usable-capacity refused with borrower alive");
        borrower = PoolLease{};  // final borrower release
        ec = pool.pool_alloc(usable, 4096, full);
        check(!ec && full.valid(), "usable-capacity succeeds after release");
        check(full.data() == borrower_ptr, "reuses former borrower address");
        std::memset(full.data(), 0xAA, full.size());   // OWNER overwrites pool memory
        full = PoolLease{};
        check(pool.stats().reserved_live_bytes == 4096,
              "scratch reservation STILL 4096 after owner cycle");

        PoolLease fresh;
        ec = cache.acquire(0, 1, 128, cache_cb, fresh);
        check(!ec && fresh.valid(), "reload (0,1) after owner overwrite");
        check(std::memcmp(fresh.data(), g_full_data.data() + 4096, 128) == 0,
              "reload restores ALL original bytes from fixture");
        check(pool.stats().reserved_live_bytes == 4096 + 4096,
              "scratch + payload both reserved (128 rounds to page)");
        fresh = PoolLease{};
        check(scratch.size() == 4096,
              "scratch reservation STILL 4096 after payload release (cache retains payload)");
    }

    // ---- T12: failing acquire into POPULATED out; short + EIO --------------
    {
        PoolExpertCache cache(pool);
        PoolLease out;
        ec = cache.acquire(0, 0, 500, cache_cb, out);
        check(!ec && out.valid(), "warm (0,0) populated");
        const void* ptr = out.data();
        const uint64_t sz = out.size();
        // snapshot ALL original bytes
        std::vector<uint8_t> orig(sz);
        std::memcpy(orig.data(), out.data(), sz);
        const auto entries_before = cache.stats().entries;
        const auto reserved_before = pool.stats().reserved_live_bytes;

        // SHORT into the SAME populated out
        wrap::cur_mode = wrap::kShort; wrap::short_return = 10;
        ec = cache.acquire(1, 0, 808, cache_cb, out);
        wrap::cur_mode = wrap::kPass;
        check(static_cast<bool>(ec), "short cache miss -> error");
        // populated out is preserved (still valid, same pointer+size+bytes)
        check(out.data() == ptr && out.size() == sz,
              "short miss: out pointer+size preserved");
        check(std::memcmp(out.data(), orig.data(), sz) == 0,
              "short miss: out ALL bytes preserved");
        check(cache.stats().entries == entries_before, "short miss: no new entry");
        check(pool.stats().reserved_live_bytes == reserved_before,
              "short miss: candidate pool bytes released");

        // EIO into the SAME populated out (fresh cache so candidate exists)
        cache.clear();
        PoolLease out2;
        ec = cache.acquire(0, 0, 500, cache_cb, out2);
        check(!ec && out2.valid(), "warm (0,0) populated again");
        const void* ptr2 = out2.data();
        const uint64_t sz2 = out2.size();
        std::vector<uint8_t> orig2(sz2);
        std::memcpy(orig2.data(), out2.data(), sz2);
        wrap::cur_mode = wrap::kEio;
        ec = cache.acquire(1, 0, 808, cache_cb, out2);
        wrap::cur_mode = wrap::kPass;
        check(static_cast<bool>(ec), "EIO cache miss -> error");
        // populated out is preserved (still valid, same pointer+size+bytes)
        check(out2.valid(), "EIO miss: out still valid (preserved)");
        check(out2.data() == ptr2 && out2.size() == sz2,
              "EIO miss: out pointer+size preserved");
        check(std::memcmp(out2.data(), orig2.data(), sz2) == 0,
              "EIO miss: out ALL bytes preserved");
        check(pool.stats().reserved_live_bytes == reserved_before + 4096,
              "EIO miss: candidate freed, back to scratch+first payload");
        out2 = PoolLease{};
    }

    // ---- fixture immutable (bytes + length) + mapping limits ----------------
    // Release pool + scratch so final mapping checks see teardown state.
    scratch = PoolLease{};
    pool = Pool{};
    check(file_contents_equal(g_fixture_path, g_full_data),
          "fixture file unchanged at end (bytes + length)");
    std::fprintf(stderr, "pread_calls=%d violations=%d peak_mapping_bytes=%llu limit=%llu\n",
                 wrap::pread_calls, wrap::violations,
                 (unsigned long long)wrap::peak_live_bytes,
                 (unsigned long long)wrap::kMappingLimit);
    check(wrap::violations == 0, "zero pread violations");
    check(wrap::peak_live_bytes <= wrap::kMappingLimit, "peak <= 1 MiB");
    check(wrap::live_maps.empty(), "final: no live mappings");
    check(wrap::mmap_calls == wrap::munmap_calls, "final: mmap==munmap");

    std::fprintf(stderr, "bounded_direct_reader_test: %d checks, %d FAILURES\n",
                 checks, failures);
    return failures == 0 ? 0 : 1;
}

