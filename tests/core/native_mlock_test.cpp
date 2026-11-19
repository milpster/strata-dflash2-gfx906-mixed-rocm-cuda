// tests/core/native_mlock_test.cpp - CPU-only mock/injection tests for STRATA_NATIVE_MLOCK.
// No actual large-RAM locks, no real RLIMIT changes.  mlock, getrlimit and setrlimit
// are intercepted by defining them in this TU, which the linker prefers over libc's,
// so every outcome (OFF, success, first-chunk failure, later-chunk failure with
// already-locked bytes retained) is deterministically exercised without touching real
// process resource limits.
//
// Usage: native_mlock_test <scenario>
//   off          - STRATA_NATIVE_MLOCK unset: no syscalls, no log, all-zero Result, silent
//   success      - STRATA_NATIVE_MLOCK=1: small buffer mlocks fully, progress on last chunk
//   empty        - STRATA_NATIVE_MLOCK=1: n==0 returns all-zero complete, no syscalls
//   first_fail   - STRATA_NATIVE_MLOCK=1: fail on first chunk, locked=0, partial
//   later_fail   - STRATA_NATIVE_MLOCK=1: fail on 2nd chunk, first chunk locked retained
//   mixed_span   - STRATA_NATIVE_MLOCK=1: span1 succeeds, span2 fails; aggregate retains span1 bytes
//   knob_zero    - STRATA_NATIVE_MLOCK=0: same as OFF
//
// Each scenario must run in its own process because native_mlock_detail::on()
// caches its value on first call (static local init).
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <sys/mman.h>
#include <sys/resource.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <mutex>

namespace {

int checks = 0;
int failures = 0;

void check(bool ok, const char* label) {
    ++checks;
    if (!ok) {
        ++failures;
        std::fprintf(stderr, "FAIL: %s\n", label);
    } else {
        std::fprintf(stderr, "pass: %s\n", label);
    }
}

constexpr uint64_t kPage = 4096;

// ---- mocks: mlock, getrlimit, setrlimit -------------------------------
// These override libc's at link time.  No real RLIMIT is ever read or changed;
// fake limits are injected through the mock variables below.
namespace test_mock {
    int mlock_errno = 0;
    int fail_after_chunks = -1;   // 1-based chunk index to start failing at (<0: never)
    int chunks_seen = 0;

    int getrlimit_calls = 0;
    int setrlimit_calls = 0;
    int getrlimit_retval = 0;     // 0 = success, nonzero = errno
    int setrlimit_retval = 0;     // 0 = success, nonzero = errno
    rlim_t rlimit_cur = RLIM_INFINITY;
    rlim_t rlimit_max = RLIM_INFINITY;
}  // namespace test_mock

extern "C" int mlock(const void* addr, size_t len) {
    using namespace test_mock;
    ++chunks_seen;
    if (fail_after_chunks >= 0 && chunks_seen > fail_after_chunks) {
        errno = mlock_errno ? mlock_errno : ENOMEM;
        return -1;
    }
    return 0;
}

extern "C" int getrlimit(int resource, struct rlimit* rl) {
    using namespace test_mock;
    ++getrlimit_calls;
    if (getrlimit_retval != 0) { errno = getrlimit_retval; return -1; }
    rl->rlim_cur = rlimit_cur;
    rl->rlim_max = rlimit_max;
    return 0;
}

extern "C" int setrlimit(int resource, const struct rlimit* rl) {
    using namespace test_mock;
    ++setrlimit_calls;
    if (setrlimit_retval != 0) { errno = setrlimit_retval; return -1; }
    return 0;
}

void reset_mocks() {
    using namespace test_mock;
    mlock_errno = 0;
    fail_after_chunks = -1;
    chunks_seen = 0;
    getrlimit_calls = 0;
    setrlimit_calls = 0;
    getrlimit_retval = 0;
    setrlimit_retval = 0;
    rlimit_cur = RLIM_INFINITY;
    rlimit_max = RLIM_INFINITY;
}

}  // namespace

#include "strata/core/pin_budget.hpp"

using namespace strata::core;

// ---- helpers ----------------------------------------------------------

// Page-aligned allocation so the mlock span covers exactly the requested bytes.
static uint8_t* alloc_page_aligned(uint64_t bytes) {
    void* p = nullptr;
    if (posix_memalign(&p, kPage, bytes) != 0) return nullptr;
    return static_cast<uint8_t*>(p);
}
static void free_page_aligned(uint8_t* p) { if (p) free(p); }

// ---- stderr capture ---------------------------------------------------
// Redirects stderr to a file so the helper's own output can be asserted.
// The test's own check() messages go to the terminal, not the capture.
// Returns 0 on success, -1 if redirection could not be set up.
static int g_stderr_saved = -1;

static int capture_stderr(const char* path) {
    g_stderr_saved = dup(fileno(stderr));
    if (g_stderr_saved < 0) return -1;
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) { close(g_stderr_saved); g_stderr_saved = -1; return -1; }
    if (dup2(fd, fileno(stderr)) < 0) { close(fd); close(g_stderr_saved); g_stderr_saved = -1; return -1; }
    close(fd);
    return 0;
}
static void restore_stderr() {
    if (g_stderr_saved >= 0) {
        fflush(stderr);
        dup2(g_stderr_saved, fileno(stderr));
        close(g_stderr_saved);
        g_stderr_saved = -1;
    }
}
// Returns the captured content.  If the file cannot be opened, returns a
// sentinel so a missing file is never mistaken for silence.
static std::string read_capture(const char* path) {
    std::FILE* f = std::fopen(path, "r");
    if (!f) return "___CAPTURE_FAILED___";
    std::string out;
    char buf[512];
    while (std::fgets(buf, sizeof buf, f)) out += buf;
    std::fclose(f);
    return out;
}

// ---- main -------------------------------------------------------------

int main(int argc, char** argv) {
    const char* scenario = (argc > 1) ? argv[1] : "off";

#if defined(__linux__)
    if (std::strcmp(scenario, "off") == 0) {
        // ---- OFF: knob unset, no syscalls, no log, all-zero Result, silent ----
        ::unsetenv("STRATA_NATIVE_MLOCK");
        reset_mocks();
        auto buf = alloc_page_aligned(kPage);

        if (capture_stderr("stderr_off.txt") != 0) {
            std::fprintf(stderr, "FAIL: OFF: stderr capture setup failed\n");
            return 1;
        }
        native_mlock_detail::Result r = native_mlock_span(buf, kPage, "off-test");
        std::fflush(stderr);
        restore_stderr();

        check(r.requested == 0, "OFF: requested == 0");
        check(r.locked == 0, "OFF: locked == 0");
        check(r.failed == 0, "OFF: failed == 0");
        check(r.complete == true, "OFF: complete == true");
        check(r.seconds == 0.0, "OFF: seconds == 0");
        check(test_mock::chunks_seen == 0, "OFF: no mlock call");
        check(test_mock::getrlimit_calls == 0, "OFF: no getrlimit call");
        check(test_mock::setrlimit_calls == 0, "OFF: no setrlimit call");

        std::string out = read_capture("stderr_off.txt");
        check(out.find("___CAPTURE_FAILED___") == std::string::npos, "OFF: capture file readable");
        check(out.empty(), "OFF: no stderr output from span");

        // native_mlock_report must also be silent when knob is off
        if (capture_stderr("stderr_off_report.txt") != 0) {
            std::fprintf(stderr, "FAIL: OFF: report capture setup failed\n");
            return 1;
        }
        native_mlock_report("off-report");
        std::fflush(stderr);
        restore_stderr();
        out = read_capture("stderr_off_report.txt");
        check(out.find("___CAPTURE_FAILED___") == std::string::npos, "OFF: report capture file readable");
        check(out.empty(), "OFF: report silent when knob off");

        free_page_aligned(buf);
    }
    else if (std::strcmp(scenario, "knob_zero") == 0) {
        // ---- knob "0": same as OFF -------------------------------------------
        ::setenv("STRATA_NATIVE_MLOCK", "0", 1);
        reset_mocks();
        auto buf = alloc_page_aligned(kPage);

        if (capture_stderr("stderr_knob_zero.txt") != 0) {
            std::fprintf(stderr, "FAIL: knob=0: stderr capture setup failed\n");
            return 1;
        }
        native_mlock_detail::Result r = native_mlock_span(buf, kPage, "zero-knob-test");
        std::fflush(stderr);
        restore_stderr();

        check(r.requested == 0, "knob=0: requested == 0");
        check(r.locked == 0, "knob=0: locked == 0");
        check(r.complete == true, "knob=0: complete == true");
        check(test_mock::chunks_seen == 0, "knob=0: no mlock call");
        check(test_mock::getrlimit_calls == 0, "knob=0: no getrlimit call");
        check(test_mock::setrlimit_calls == 0, "knob=0: no setrlimit call");

        std::string out = read_capture("stderr_knob_zero.txt");
        check(out.find("___CAPTURE_FAILED___") == std::string::npos, "knob=0: capture file readable");
        check(out.empty(), "knob=0: no stderr output from span");

        // native_mlock_report must also be silent when knob is "0"
        if (capture_stderr("stderr_knob_zero_report.txt") != 0) {
            std::fprintf(stderr, "FAIL: knob=0: report capture setup failed\n");
            return 1;
        }
        native_mlock_report("zero-knob-report");
        std::fflush(stderr);
        restore_stderr();
        out = read_capture("stderr_knob_zero_report.txt");
        check(out.find("___CAPTURE_FAILED___") == std::string::npos, "knob=0: report capture file readable");
        check(out.empty(), "knob=0: report silent when knob=0");

        free_page_aligned(buf);
    }
    else if (std::strcmp(scenario, "empty") == 0) {
        // ---- n == 0: all-zero, complete, no syscalls ---------------------------
        ::setenv("STRATA_NATIVE_MLOCK", "1", 1);
        reset_mocks();
        native_mlock_detail::Result r = native_mlock_span(nullptr, 0, "empty-test");

        check(r.requested == 0, "empty: requested == 0");
        check(r.locked == 0, "empty: locked == 0");
        check(r.complete == true, "empty: complete == true");
        check(test_mock::chunks_seen == 0, "empty: no mlock call");
        check(test_mock::getrlimit_calls == 0, "empty: no getrlimit call");
        check(test_mock::setrlimit_calls == 0, "empty: no setrlimit call");
    }
    else if (std::strcmp(scenario, "success") == 0) {
        // ---- ON, success: small buffer mlocks fully ---------------------------
        ::setenv("STRATA_NATIVE_MLOCK", "1", 1);
        reset_mocks();
        auto buf = alloc_page_aligned(kPage);

        if (capture_stderr("stderr_success.txt") != 0) {
            std::fprintf(stderr, "FAIL: success: stderr capture setup failed\n");
            return 1;
        }
        native_mlock_detail::Result r = native_mlock_span(buf, kPage, "success-test");
        std::fflush(stderr);
        restore_stderr();

        check(r.requested == kPage, "success: requested == kPage");
        check(r.locked == kPage, "success: locked == kPage");
        check(r.failed == 0, "success: failed == 0");
        check(r.complete == true, "success: complete == true");
        check(r.seconds >= 0.0, "success: seconds >= 0");
        check(test_mock::chunks_seen == 1, "success: one mlock call");
        check(test_mock::getrlimit_calls >= 1, "success: getrlimit called at least once");
        check(test_mock::setrlimit_calls == 0, "success: setrlimit not called (unlimited soft limit)");

        std::string out = read_capture("stderr_success.txt");
        check(out.find("___CAPTURE_FAILED___") == std::string::npos, "success: capture file readable");
        // One chunk = last chunk, so progress IS emitted (chunk % 4 == 0 || last chunk)
        check(out.find("progress") != std::string::npos, "success: progress line emitted on last chunk");
        check(out.find("then failed") == std::string::npos, "success: no mlock failure line");

        // Report: locked == requested, no [PARTIAL]
        if (capture_stderr("stderr_report_success.txt") != 0) {
            std::fprintf(stderr, "FAIL: success: report capture setup failed\n");
            return 1;
        }
        native_mlock_report("success-report");
        std::fflush(stderr);
        restore_stderr();
        out = read_capture("stderr_report_success.txt");
        check(out.find("___CAPTURE_FAILED___") == std::string::npos, "success: report capture file readable");
        check(out.find("[PARTIAL]") == std::string::npos, "success-report: no [PARTIAL] marker");
        check(out.find("locked") != std::string::npos, "success-report: report emitted");

        // Reset: after report, State should be zero
        auto& s = native_mlock_detail::st();
        {
            std::lock_guard<std::mutex> g(s.mu);
            check(s.bytes == 0, "success: State.bytes reset after report");
            check(s.requested == 0, "success: State.requested reset after report");
            check(s.spans == 0, "success: State.spans reset after report");
        }

        free_page_aligned(buf);
    }
    else if (std::strcmp(scenario, "first_fail") == 0) {
        // ---- first-chunk failure: locked=0, failed=total, complete=false ------
        ::setenv("STRATA_NATIVE_MLOCK", "1", 1);
        reset_mocks();
        test_mock::fail_after_chunks = 0;   // fail on the very first chunk
        auto buf = alloc_page_aligned(kPage * 3);

        if (capture_stderr("stderr_first_fail.txt") != 0) {
            std::fprintf(stderr, "FAIL: first-fail: stderr capture setup failed\n");
            return 1;
        }
        native_mlock_detail::Result r = native_mlock_span(buf, kPage * 3, "first-fail-test");
        std::fflush(stderr);
        restore_stderr();

        check(r.requested == kPage * 3, "first-fail: requested == 3 pages");
        check(r.locked == 0, "first-fail: locked == 0");
        check(r.failed == kPage * 3, "first-fail: failed == all requested");
        check(r.complete == false, "first-fail: complete == false");
        check(test_mock::chunks_seen == 1, "first-fail: one mlock attempt");
        check(test_mock::getrlimit_calls >= 1, "first-fail: getrlimit called");

        std::string out = read_capture("stderr_first_fail.txt");
        check(out.find("___CAPTURE_FAILED___") == std::string::npos, "first-fail: capture file readable");
        check(out.find("then failed") != std::string::npos, "first-fail: failure line emitted");
        check(out.find("chunk 1") != std::string::npos, "first-fail: failure at chunk 1");

        // Report should show [PARTIAL]
        if (capture_stderr("stderr_report_first_fail.txt") != 0) {
            std::fprintf(stderr, "FAIL: first-fail: report capture setup failed\n");
            return 1;
        }
        native_mlock_report("first-fail-report");
        std::fflush(stderr);
        restore_stderr();
        out = read_capture("stderr_report_first_fail.txt");
        check(out.find("___CAPTURE_FAILED___") == std::string::npos, "first-fail: report capture file readable");
        check(out.find("[PARTIAL]") != std::string::npos, "first-fail-report: [PARTIAL] marker present");

        free_page_aligned(buf);
    }
    else if (std::strcmp(scenario, "later_fail") == 0) {
        // ---- later-chunk failure: already-locked bytes counted and retained ----
        // Buffer just over one 256 MiB step = 2 chunks (first full, second partial).
        // Progress is emitted only every 4 chunks or on the last chunk, so with
        // failure on chunk 2 there is NO prior progress line.  Do not assert one.
        ::setenv("STRATA_NATIVE_MLOCK", "1", 1);
        reset_mocks();
        const uint64_t two_chunks = (256ull << 20) + kPage;
        auto buf = alloc_page_aligned(two_chunks);
        test_mock::fail_after_chunks = 1;   // fail on the second chunk

        if (capture_stderr("stderr_later_fail.txt") != 0) {
            std::fprintf(stderr, "FAIL: later-fail: stderr capture setup failed\n");
            return 1;
        }
        native_mlock_detail::Result r = native_mlock_span(buf, two_chunks, "later-fail-test");
        std::fflush(stderr);
        restore_stderr();

        check(r.requested == two_chunks, "later-fail: requested == 256 MiB + 1 page");
        check(r.locked == (256ull << 20), "later-fail: locked == first chunk (256 MiB)");
        check(r.failed == kPage, "later-fail: failed == remaining page");
        check(r.complete == false, "later-fail: complete == false");
        check(test_mock::chunks_seen == 2, "later-fail: two mlock attempts");
        check(test_mock::getrlimit_calls >= 1, "later-fail: getrlimit called");

        // Verify State counted the already-locked bytes (exact, not >=)
        auto& s = native_mlock_detail::st();
        {
            std::lock_guard<std::mutex> g(s.mu);
            check(s.bytes == (256ull << 20), "later-fail: State.bytes exactly counts locked chunk");
            check(s.requested == two_chunks, "later-fail: State.requested exactly counts full request");
            check(s.spans == 1, "later-fail: State.spans counts one attempted span");
            check(s.seconds == r.seconds && r.seconds >= 0.0, "later-fail: elapsed time retained exactly");
        }

        std::string out = read_capture("stderr_later_fail.txt");
        check(out.find("___CAPTURE_FAILED___") == std::string::npos, "later-fail: capture file readable");
        check(out.find("then failed") != std::string::npos, "later-fail: failure line emitted");
        // No progress expected: chunk 1 is not chunk 4 and not the last chunk
        check(out.find("progress") == std::string::npos, "later-fail: no progress before failure on chunk 2");

        // Report should show [PARTIAL] with exact numbers
        if (capture_stderr("stderr_report_later_fail.txt") != 0) {
            std::fprintf(stderr, "FAIL: later-fail: report capture setup failed\n");
            return 1;
        }
        native_mlock_report("later-fail-report");
        std::fflush(stderr);
        restore_stderr();
        out = read_capture("stderr_report_later_fail.txt");
        check(out.find("___CAPTURE_FAILED___") == std::string::npos, "later-fail: report capture file readable");
        check(out.find("[PARTIAL]") != std::string::npos, "later-fail-report: [PARTIAL] marker present");

        // Reset after report
        {
            std::lock_guard<std::mutex> g(s.mu);
            check(s.bytes == 0, "later-fail: State.bytes reset after report");
            check(s.requested == 0, "later-fail: State.requested reset after report");
            check(s.spans == 0, "later-fail: State.spans reset after report");
        }

        free_page_aligned(buf);
    }
    else if (std::strcmp(scenario, "mixed_span") == 0) {
        // ---- mixed spans: span1 succeeds fully, span2 fails; aggregate retains ----
        // This proves a failed span does not discard earlier successful bytes.
        ::setenv("STRATA_NATIVE_MLOCK", "1", 1);
        reset_mocks();

        // Span 1: small, succeeds
        auto buf1 = alloc_page_aligned(kPage);
        test_mock::fail_after_chunks = -1;   // never fail
        if (capture_stderr("stderr_mixed_span1.txt") != 0) {
            std::fprintf(stderr, "FAIL: mixed: span1 capture setup failed\n");
            return 1;
        }
        native_mlock_detail::Result r1 = native_mlock_span(buf1, kPage, "mixed-span1");
        std::fflush(stderr);
        restore_stderr();
        check(r1.complete == true, "mixed: span1 complete");
        check(r1.locked == kPage, "mixed: span1 locked == kPage");

        // Span 2: larger, fails on first chunk
        auto buf2 = alloc_page_aligned(kPage * 3);
        test_mock::fail_after_chunks = 0;   // fail immediately
        if (capture_stderr("stderr_mixed_span2.txt") != 0) {
            std::fprintf(stderr, "FAIL: mixed: span2 capture setup failed\n");
            return 1;
        }
        native_mlock_detail::Result r2 = native_mlock_span(buf2, kPage * 3, "mixed-span2");
        std::fflush(stderr);
        restore_stderr();
        check(r2.complete == false, "mixed: span2 not complete");
        check(r2.locked == 0, "mixed: span2 locked == 0");
        check(r2.failed == kPage * 3, "mixed: span2 failed == all");

        // Aggregate State must retain span1's locked bytes even though span2 failed
        auto& s = native_mlock_detail::st();
        {
            std::lock_guard<std::mutex> g(s.mu);
            check(s.bytes == kPage, "mixed: State.bytes retains span1 locked bytes after span2 failure");
            check(s.requested == kPage + (kPage * 3), "mixed: State.requested counts both spans");
            check(s.spans == 2, "mixed: State.spans counts both attempted spans");
            check(s.seconds == r1.seconds + r2.seconds, "mixed: elapsed time counts both spans exactly");
        }

        // Report should show [PARTIAL] (span2 failed) with exact aggregate
        if (capture_stderr("stderr_report_mixed.txt") != 0) {
            std::fprintf(stderr, "FAIL: mixed: report capture setup failed\n");
            return 1;
        }
        native_mlock_report("mixed-report");
        std::fflush(stderr);
        restore_stderr();
        std::string out = read_capture("stderr_report_mixed.txt");
        check(out.find("___CAPTURE_FAILED___") == std::string::npos, "mixed: report capture file readable");
        check(out.find("[PARTIAL]") != std::string::npos, "mixed-report: [PARTIAL] marker present");
        check(out.find("locked") != std::string::npos, "mixed-report: aggregate report emitted");

        // Reset after report
        {
            std::lock_guard<std::mutex> g(s.mu);
            check(s.bytes == 0, "mixed: State.bytes reset after report");
            check(s.requested == 0, "mixed: State.requested reset after report");
            check(s.spans == 0, "mixed: State.spans reset after report");
            check(s.seconds == 0.0, "mixed: State.seconds reset after report");
        }

        free_page_aligned(buf1);
        free_page_aligned(buf2);
    }
    else {
        std::fprintf(stderr, "unknown scenario: %s\n", scenario);
        return 2;
    }
#else
    // Non-Linux build: on() returns false, no-op contract, no syscalls.
    // This path is only exercised if the test is somehow compiled for a
    // non-Linux target; on this Linux host the __linux__ branch above runs.
    (void)scenario;
    reset_mocks();
    auto buf = alloc_page_aligned(kPage);
    native_mlock_detail::Result r = native_mlock_span(buf, kPage, "nonlinux-test");
    check(r.requested == 0, "non-Linux: requested == 0");
    check(r.locked == 0, "non-Linux: locked == 0");
    check(r.complete == true, "non-Linux: complete == true");
    check(native_mlock_detail::on() == false, "non-Linux: on() == false");
    check(test_mock::chunks_seen == 0, "non-Linux: no mlock call");
    check(test_mock::getrlimit_calls == 0, "non-Linux: no getrlimit call");
    check(test_mock::setrlimit_calls == 0, "non-Linux: no setrlimit call");
    native_mlock_report("nonlinux-test");   // must not print, must not crash
    free_page_aligned(buf);
#endif

    if (failures != 0) {
        std::fprintf(stderr, "native_mlock_test (%s): %d checks, %d FAILURES\n", scenario, checks, failures);
        return 1;
    }
    std::fprintf(stderr, "native_mlock_test (%s): %d checks passed\n", scenario, checks);
    return 0;
}
