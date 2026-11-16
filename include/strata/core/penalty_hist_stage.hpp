// include/strata/core/penalty_hist_stage.hpp - F8: the host side of the mapped penalty-history staging.
//
// Today (STRATA_PENALTY_MAPPED absent) serve builds a window's penalty-history rows with
// `strata::kernels::penalty_rows` into a pageable vector and moves them to the device with a BLOCKING
// cudaMemcpy between the window's draft and `ver.run` - on the round's critical path.  With the gate on,
// the SAME bytes land in a mapped image (host-pinned, device-visible) that the window graph copies out at
// its head, so the launch replaces the memcpy.  The rows are still built at the same point (they need this
// window's drafts) and still land at the same device destination; only the copy's mechanism changes.
//
// CPU-only header arithmetic: the byte image and the gate are testable with no device at all
// (tests/core/penalty_hist_stage_test.cpp links no library), the expert_helper_reserve_test shape.
#pragma once

#include <atomic>
#include <cstdint>
#include <cstdlib>
#include <cstring>

namespace strata::core {

/// The `penalty_last_n` ceiling this engine runs, and the constant the mapped image is sized from: serve
/// clamps every request's penalty window to it (`kPenaltyWindowCap` in generate.cpp, mirrored by the batch
/// path's `kSlotHistMax` in verify.cpp) and allocates the device row buffer with the same clamp, so
/// `T * penalty_last_n` is never more than (a window's rows) x kHistMax.  The image covers that product,
/// which is what lets the graph's copy be bounded by a length captured with the graph while the window's
/// real count is only known at replay.
inline constexpr int kHistMax = 4096;

/// Words of control that follow the rows' image: ctl[0] is the count, the rest is slack so the word does
/// not share a cache line with the rows the copy streams.
inline constexpr int kHistCtlWords = 16;

/// STRATA_PENALTY_MAPPED, default OFF: absent or "0" keeps today's pageable vector + blocking cudaMemcpy
/// byte for byte; "1" stages the same rows into the mapped image the window graph copies out at its head.
/// Read ONCE per translation unit, and every decision that has to agree - allocating the image in prepare(),
/// capturing the copy in record_window, and filling it in stage_hist() - lives in verify.cpp, so a captured
/// graph and a staged image cannot disagree about whether this engine stages through mapped memory.
inline bool penalty_hist_mapped() {
    static const bool on = [] {
        const char* v = std::getenv("STRATA_PENALTY_MAPPED");
        return v != nullptr && std::atoi(v) != 0;
    }();
    return on;
}

/// Write one window's rows into the image and publish its length word.  `rows` is the [T][hist_n] image
/// `penalty_rows` built (most recent token LAST, -1 padded in front) - exactly the bytes today's vector +
/// blocking cudaMemcpy staged - and ctl[0] becomes the WORD COUNT the graph's head copy moves at replay.
/// The count cannot be captured with the graph: the graph is cached per window size and shared by requests
/// whose penalty_last_n differ, so a captured length would stage another request's row width.
///
/// ctl[0] == 0 means nothing is staged: the copy moves nothing and the device buffer keeps the bytes it
/// already has - which is what the blocking-copy fallback needs, since the same graph replays either way.
/// Returns false (having written 0, and having left the rows untouched) when the rows are empty or do not
/// fit `cap_words`, leaving the caller to stage with today's blocking copy.  The fence sits between the rows
/// and the word, so a replay that sees the new count sees the rows it counts.
inline bool hist_stage_write(int32_t* rows_dst, int32_t* ctl, int64_t cap_words, const int32_t* rows, int T,
                             int hist_n) {
    const int64_t words = (int64_t) T * (int64_t) hist_n;
    if (rows == nullptr || T <= 0 || hist_n <= 0 || hist_n > kHistMax || words > cap_words) {
        ctl[0] = 0;
        return false;
    }
    std::memcpy(rows_dst, rows, (size_t) words * sizeof(int32_t));
    std::atomic_thread_fence(std::memory_order_seq_cst);
    ctl[0] = (int32_t) words;
    return true;
}

}  // namespace strata::core
