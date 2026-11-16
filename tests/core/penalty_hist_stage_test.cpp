// tests/core/penalty_hist_stage_test.cpp - F8 (the mapped penalty-history staging), host-only: the byte image
// Verifier::stage_hist writes into the mapped buffer IS the byte image serve's pageable vector + blocking
// cudaMemcpy stages today (golden compare), the control word carries THIS window's row count (the count is read
// from mapped at graph replay, because one captured graph of a window size serves requests whose penalty_last_n
// differ), nothing past the count is written (the graph's copy moves the count and no more), and every request
// the image cannot hold is refused with the count at 0 - the inert copy that lets the caller stage with today's
// blocking memcpy.
//
// CPU-only BY CONSTRUCTION: header arithmetic and RAM; the test links NO library and makes no device call, like
// expert_helper_reserve_test.  Exit 0 = every check passed.
#include "strata/core/penalty_hist_stage.hpp"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

using namespace strata::core;

namespace {

int failures = 0;
void check(bool ok, const char* what) {
    std::printf("  %-64s %s\n", what, ok ? "ok" : "FAIL");
    if (!ok) ++failures;
}

// The sentinel the image is filled with before every stage: a word no window writes, so "untouched" is visible.
constexpr int32_t kSentinel = 0x5A5A5A5A;

// A window's rows the way `penalty_rows` lays them out: T rows of hist_n slots, the front slots -1 (the history
// the state has not consumed yet), the most recent token LAST.  Row t counts the tokens row t-1's pick follows.
std::vector<int32_t> make_rows(int T, int hist_n, int base) {
    std::vector<int32_t> rows((size_t) T * hist_n, -1);
    for (int t = 0; t < T; ++t) {
        const int filled = hist_n - t;   // row t has one fewer draft behind it than row t-1
        for (int i = 0; i < filled; ++i) rows[(size_t) t * hist_n + i] = base + t * 1000 + i;
    }
    return rows;
}

int32_t* image(std::vector<int32_t>& buf, int64_t cap_words) {
    buf.assign((size_t) cap_words + kHistCtlWords, kSentinel);
    return buf.data();
}

// ---- the golden case: a small window whose expected image is a literal, so the compare is against bytes
// written down here, not against the same loop that produced the input.
void golden_case() {
    std::vector<int32_t> buf;
    int32_t* dst = image(buf, 16);
    int32_t* ctl = dst + 16;
    // T = 3 rows of hist_n = 5: row 0 holds five consumed tokens, row 1 four (one draft behind), row 2 three.
    const int32_t rows[] = {7, 8, 9, 10, 11,
                            12, 13, 14, 15, -1,
                            16, 17, 18, -1, -1};
    const int32_t want[] = {7, 8, 9, 10, 11,
                            12, 13, 14, 15, -1,
                            16, 17, 18, -1, -1};   // the bytes today's cudaMemcpy(dst, rows, 15 * 4) moved
    const bool ok = hist_stage_write(dst, ctl, 16, rows, 3, 5);
    check(ok, "stage: a window that fits is staged");
    check(std::memcmp(dst, want, sizeof want) == 0, "stage: the image is the vector's bytes, row-major, no stride padding");
    check(ctl[0] == 15, "stage: the control word carries this window's word count");
    check(dst[15] == kSentinel, "stage: nothing past the window's rows is written (the copy moves the count)");
}

// ---- a window of the shape the plan sizes for: 8 rows x 64 slots = 2 KiB, plus the untouched tail.
void wide_case() {
    constexpr int kT = 8, kHistN = 64;
    const std::vector<int32_t> rows = make_rows(kT, kHistN, 1000);
    std::vector<int32_t> buf;
    int32_t* dst = image(buf, (int64_t) kT * kHistN * 2);   // room past the window, to see the tail untouched
    int32_t* ctl = dst + (int64_t) kT * kHistN * 2;
    check(hist_stage_write(dst, ctl, (int64_t) kT * kHistN * 2, rows.data(), kT, kHistN),
          "stage: an 8x64 window (2 KiB) is staged");
    check(ctl[0] == kT * kHistN, "stage: ctl[0] = T * hist_n");
    check(std::memcmp(dst, rows.data(), (size_t) kT * kHistN * sizeof(int32_t)) == 0,
          "stage: 2 KiB image == the bytes penalty_rows put in the vector");
    bool tail = true;
    for (int64_t i = (int64_t) kT * kHistN; i < (int64_t) kT * kHistN * 2; ++i)
        if (dst[i] != kSentinel) tail = false;
    check(tail, "stage: the image past the count still holds the sentinel");
}

// ---- hist_n moves between windows of the SAME graph (penalty_last_n, consumed growth): the narrow window's
// stage republishes the count only over its own words; what lies past it keeps the previous bytes, which is
// safe because the sampler reads only hist_len_ slots per row of THIS window.
void changing_hist_n() {
    constexpr int kT = 8, kHistN = 64;
    const std::vector<int32_t> wide = make_rows(kT, kHistN, 2000);
    const std::vector<int32_t> one = make_rows(1, 8, 3000);
    std::vector<int32_t> buf;
    int32_t* dst = image(buf, (int64_t) kT * kHistN);
    int32_t* ctl = dst + (int64_t) kT * kHistN;
    check(hist_stage_write(dst, ctl, (int64_t) kT * kHistN, wide.data(), kT, kHistN), "hist_n: the wide window stages");
    check(hist_stage_write(dst, ctl, (int64_t) kT * kHistN, one.data(), 1, 8), "hist_n: the narrow window stages after it");
    check(ctl[0] == 8, "hist_n: ctl[0] is this window's count, not the graph's");
    check(std::memcmp(dst, one.data(), 8 * sizeof(int32_t)) == 0, "hist_n: the first words are the new window's rows");
    check(dst[8] == wide[8], "hist_n: words past the count keep the previous bytes (hist_len_ gates the reads)");
}

// ---- the refusals: each leaves the count at 0 (the captured copy moves nothing, so whatever the caller's
// blocking cudaMemcpy staged survives) and writes no rows.
void refusals() {
    constexpr int kCap = 8 * 4096;
    const std::vector<int32_t> rows = make_rows(8, 64, 4000);
    std::vector<int32_t> buf;
    int32_t* dst = image(buf, kCap);
    int32_t* ctl = dst + kCap;
    check(!hist_stage_write(dst, ctl, kCap, rows.data(), 8, 0), "refuse: hist_n 0 (a request without penalties)");
    check(ctl[0] == 0, "refuse: ctl[0] = 0 after a refusal (the in-graph copy stays inert)");
    check(!hist_stage_write(dst, ctl, kCap, rows.data(), 0, 64), "refuse: T 0");
    check(!hist_stage_write(dst, ctl, kCap, nullptr, 8, 64), "refuse: no rows (the neutral path)");
    check(!hist_stage_write(dst, ctl, kCap, rows.data(), 8, kHistMax + 1), "refuse: hist_n past the penalty_last_n ceiling");
    check(!hist_stage_write(dst, ctl, kCap, rows.data(), 16, 4096), "refuse: rows past the image's capacity");
    check(ctl[0] == 0, "refuse: ctl[0] = 0 after every refusal");
    bool untouched = true;
    for (int64_t i = 0; i < kCap; ++i)
        if (dst[i] != kSentinel) untouched = false;
    check(untouched, "refuse: a refusal writes no rows (the caller's blocking copy owns the buffer now)");
}

// ---- the sizing decision: the image is sized (the widest window this build captures) x kHistMax, and the
// ceiling product itself stages - the review's cap check (kHistMax is the engine's clamp, not a guess).
void ceiling_case() {
    check(kHistMax == 4096, "kHistMax == 4096: serve's penalty_last_n clamp (kPenaltyWindowCap / kSlotHistMax)");
    constexpr int64_t kCap = (int64_t) 16 * kHistMax;   // kVerifyLongT (16 on both the HIP and the CUDA build) x the ceiling
    std::vector<int32_t> rows((size_t) kCap, 7);
    std::vector<int32_t> buf;
    int32_t* dst = image(buf, kCap);
    int32_t* ctl = dst + kCap;
    check(hist_stage_write(dst, ctl, kCap, rows.data(), 16, kHistMax),
          "ceiling: a window at the capture-time bound (16 rows x 4096) stages");
    check(ctl[0] == (int32_t) kCap, "ceiling: its word count is the image's capacity");
    check(!hist_stage_write(dst, ctl, kCap, rows.data(), 17, kHistMax), "ceiling: one row past it is refused");
}

// ---- the gate: default OFF, and read once, which is what keeps prepare(), the capture and stage_hist() agreed.
void gate_case() {
    const char* v = std::getenv("STRATA_PENALTY_MAPPED");
    const bool want = v != nullptr && std::atoi(v) != 0;
    check(penalty_hist_mapped() == want, "gate: STRATA_PENALTY_MAPPED decides it, once per process");
    check(v == nullptr || want, "gate: absent = OFF (today's vector + blocking cudaMemcpy)");
}

}  // namespace

int main() {
    std::printf("penalty_hist_stage_test: the mapped penalty-history staging (F8)\n");
    golden_case();
    wide_case();
    changing_hist_n();
    refusals();
    ceiling_case();
    gate_case();
    std::printf("%s (%d failures)\n", failures == 0 ? "penalty_hist_stage_test PASS" : "FAIL", failures);
    return failures == 0 ? 0 : 1;
}
