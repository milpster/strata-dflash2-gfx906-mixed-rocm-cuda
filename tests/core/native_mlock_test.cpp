// tests/core/native_mlock_test.cpp - CPU-only mock/injection tests for STRATA_NATIVE_MLOCK.
// No actual large-RAM locks, no real RLIMIT changes.  mlock is intercepted by defining
// our own mlock in this TU, which the linker prefers over libc's, so every outcome
// (OFF, success, first-chunk failure, later-chunk failure with already-locked bytes
// retained) is deterministically exercised.
//
// Usage: native_mlock_test <scenario>
//   off          - STRATA_NATIVE_MLOCK unset: no syscall, all-zero Result
//   success      - STRATA_NATIVE_MLOCK=1: small buffer mlocks fully
//   empty        - STRATA_NATIVE_MLOCK=1: n==0 returns all-zero complete
//   first_fail   - STRATA_NATIVE_MLOCK=1: fail on first chunk, locked=0
//   later_fail   - STRATA_NATIVE_MLOCK=1: fail on 2nd chunk, first chunk locked retained
//   knob_zero    - STRATA_NATIVE_MLOCK=0: same as OFF
//
// Each scenario must run in its own process because native_mlock_detail::on()
// caches its value on first call (static local init).
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <sys/mman.h>
#include <errno.h>

namespace {

int checks = 0;
void check(bool ok, const char* label) {
    ++checks;
    if (!ok) { std::fprintf(stderr, "FAIL: %s\n", label); std::exit(1); }
    std::fprintf(stderr, "pass: %s\n", label);
}

constexpr uint64_t kPage = 4096;

// ---- mock mlock (overrides libc's at link time) ------------------------------
namespace test_mock {
int mlock_errno = 0;
int fail_after_chunks = -1;   // 1-based chunk index to start failing at (<0: never)
int chunks_seen = 0;
uintptr_t last_addr = 0;
size_t last_len = 0;
}  // namespace test_mock

extern "C" int mlock(const void* addr, size_t len) {
    using namespace test_mock;
    last_addr = reinterpret_cast<uintptr_t>(addr);
    last_len = len;
    ++chunks_seen;
    if (fail_after_chunks >= 0 && chunks_seen > fail_after_chunks) {
        errno = mlock_errno ? mlock_errno : ENOMEM;
        return -1;
    }
    return 0;
}

}  // namespace

#include "strata/core/pin_budget.hpp"

using namespace strata::core;

// ---- helpers ----------------------------------------------------------------

// Page-aligned allocation so the mlock span covers exactly the requested bytes.
static uint8_t* alloc_page_aligned(uint64_t bytes) {
    void* p = nullptr;
    if (posix_memalign(&p, kPage, bytes) != 0) return nullptr;
    return static_cast<uint8_t*>(p);
}

static void free_page_aligned(uint8_t* p) {
    if (p) free(p);
}

void reset_mock() {
    using namespace test_mock;
    mlock_errno = 0;
    fail_after_chunks = -1;
    chunks_seen = 0;
    last_addr = 0;
    last_len = 0;
}

int main(int argc, char** argv) {
    const char* scenario = (argc > 1) ? argv[1] : "off";

#if defined(__linux__)
    if (std::strcmp(scenario, "off") == 0) {
        // ---- OFF: knob unset, no syscall, no log, all-zero Result ---------------
        ::unsetenv("STRATA_NATIVE_MLOCK");
        reset_mock();
        auto buf = alloc_page_aligned(kPage);
        native_mlock_detail::Result r = native_mlock_span(buf, kPage, "off-test");
        check(r.requested == 0, "OFF: requested == 0");
        check(r.locked == 0, "OFF: locked == 0");
        check(r.failed == 0, "OFF: failed == 0");
        check(r.complete == true, "OFF: complete == true");
        check(r.seconds == 0.0, "OFF: seconds == 0");
        check(test_mock::chunks_seen == 0, "OFF: no mlock call");
        native_mlock_report("off-test");   // must not print, must not crash
        free_page_aligned(buf);
    }
    else if (std::strcmp(scenario, "success") == 0) {
        // ---- ON, success: small buffer, real mlock succeeds ---------------------
        ::setenv("STRATA_NATIVE_MLOCK", "1", 1);
        reset_mock();
        auto buf = alloc_page_aligned(kPage);
        native_mlock_detail::Result r = native_mlock_span(buf, kPage, "success-test");
        check(r.requested == kPage, "success: requested == kPage");
        check(r.locked == kPage, "success: locked == kPage");
        check(r.failed == 0, "success: failed == 0");
        check(r.complete == true, "success: complete == true");
        check(r.seconds >= 0.0, "success: seconds >= 0");
        check(test_mock::chunks_seen == 1, "success: one mlock call");
        native_mlock_report("success-test");
        free_page_aligned(buf);
    }
    else if (std::strcmp(scenario, "empty") == 0) {
        // ---- n == 0: all-zero, complete -----------------------------------------
        ::setenv("STRATA_NATIVE_MLOCK", "1", 1);
        reset_mock();
        native_mlock_detail::Result r = native_mlock_span(nullptr, 0, "empty-test");
        check(r.requested == 0, "empty: requested == 0");
        check(r.locked == 0, "empty: locked == 0");
        check(r.complete == true, "empty: complete == true");
        check(test_mock::chunks_seen == 0, "empty: no mlock call");
    }
    else if (std::strcmp(scenario, "first_fail") == 0) {
        // ---- first-chunk failure: locked=0, failed=total, complete=false --------
        ::setenv("STRATA_NATIVE_MLOCK", "1", 1);
        reset_mock();
        test_mock::fail_after_chunks = 0;   // fail on the very first chunk
        auto buf = alloc_page_aligned(kPage * 3);     // 3 pages, 1 chunk of 256 MiB covers it all
        native_mlock_detail::Result r = native_mlock_span(buf, kPage * 3, "first-fail-test");
        check(r.requested == kPage * 3, "first-fail: requested == 3 pages");
        check(r.locked == 0, "first-fail: locked == 0");
        check(r.failed == kPage * 3, "first-fail: failed == all requested");
        check(r.complete == false, "first-fail: complete == false");
        check(test_mock::chunks_seen == 1, "first-fail: one mlock attempt");
        native_mlock_report("first-fail-test");   // should show [PARTIAL]
        free_page_aligned(buf);
    }
    else if (std::strcmp(scenario, "later_fail") == 0) {
        // ---- later-chunk failure: already-locked bytes counted ------------------
        // Buffer of 256 MiB + 1 page = 2 chunks (first full, second partial).
        ::setenv("STRATA_NATIVE_MLOCK", "1", 1);
        reset_mock();
        const uint64_t two_chunks = (256ull << 20) + kPage;
        auto buf = alloc_page_aligned(two_chunks);
        test_mock::fail_after_chunks = 1;   // fail on the second chunk
        native_mlock_detail::Result r = native_mlock_span(buf, two_chunks, "later-fail-test");
        check(r.requested == two_chunks, "later-fail: requested == 256 MiB + 1 page");
        check(r.locked == (256ull << 20), "later-fail: locked == first chunk (256 MiB)");
        check(r.failed == kPage, "later-fail: failed == remaining page");
        check(r.complete == false, "later-fail: complete == false");
        check(test_mock::chunks_seen == 2, "later-fail: two mlock attempts");
        // Verify State counted the already-locked bytes
        auto& s = native_mlock_detail::st();
        {
            std::lock_guard<std::mutex> g(s.mu);
            check(s.bytes >= (256ull << 20), "later-fail: State.bytes counts locked chunk");
            check(s.requested >= two_chunks, "later-fail: State.requested counts full request");
        }
        native_mlock_report("later-fail-test");   // should show [PARTIAL]
        free_page_aligned(buf);
    }
    else if (std::strcmp(scenario, "knob_zero") == 0) {
        // ---- ON with "0": same as OFF -------------------------------------------
        ::setenv("STRATA_NATIVE_MLOCK", "0", 1);
        reset_mock();
        auto buf = alloc_page_aligned(kPage);
        native_mlock_detail::Result r = native_mlock_span(buf, kPage, "zero-knob-test");
        check(r.requested == 0, "knob=0: requested == 0");
        check(r.locked == 0, "knob=0: locked == 0");
        check(r.complete == true, "knob=0: complete == true");
        check(test_mock::chunks_seen == 0, "knob=0: no mlock call");
        free_page_aligned(buf);
    }
    else {
        std::fprintf(stderr, "unknown scenario: %s\n", scenario);
        return 2;
    }
#else
    // Non-Linux: on() returns false, no-op contract, no syscalls.
    (void)scenario;
    reset_mock();
    auto buf = alloc_page_aligned(kPage);
    native_mlock_detail::Result r = native_mlock_span(buf, kPage, "nonlinux-test");
    free_page_aligned(buf);
    check(r.requested == 0, "non-Linux: requested == 0");
    check(r.locked == 0, "non-Linux: locked == 0");
    check(r.complete == true, "non-Linux: complete == true");
    check(native_mlock_detail::on() == false, "non-Linux: on() == false");
    check(test_mock::chunks_seen == 0, "non-Linux: no mlock call");
    native_mlock_report("nonlinux-test");   // must not print, must not crash
#endif

    std::fprintf(stderr, "native_mlock_test (%s): %d checks passed\n", scenario, checks);
    return 0;
}
