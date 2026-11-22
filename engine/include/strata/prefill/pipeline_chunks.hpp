// include/strata/prefill/pipeline_chunks.hpp - STRATA_PIPELINE_CHUNKS: read a prompt in EXACTLY N chunks
// (no GPU needed to check the math).
//
// A layer split's stages overlap only across chunks: stage k+1 reads chunk i after stage k handed it
// over, so a one-chunk prompt is strictly serial (wall = stage0 + stage1).  --prefill-pipe sizes the
// chunks from a cost model (Prefill::pipeline_chunk) whose "ratio" is in whole tokens, so a value like
// 1.1 reads as b = 1 token per chunk and pushes the chunk count to the model's cap (~512-token chunks,
// 29 of them at 14,637 tokens - measured 3.3x slower, 2026-11-10).  This is the explicit knob instead:
// the prompt reads in exactly N equal chunks, the remainder in the last.
#pragma once

#include <algorithm>
#include <cstdint>

namespace strata::prefill::pipeline_chunks {

// The bounds the pipe path already carries: chunks of at least 512 tokens, at most 64 chunks
// (`std::max<int64_t>(512, ...)`, `std::min<int64_t>(64, (n + 511) / 512)` - `Prefill::pipeline_chunk`).
constexpr int64_t kFloor = 512;
constexpr int64_t kMaxChunks = 64;
constexpr int64_t kGrid = 256;

/// The chunk an `n`-token prompt reads in when it reads in exactly `chunks` chunks: equal, on the
/// 256-token grid, the remainder in the last, at most `cap` (the buffers' size).  1 chunk is today's
/// read (`cap`).  -1 when the request is unusable: `chunks` out of [1, kMaxChunks], or equal chunks
/// that would leave the LAST chunk below the 512-token floor.
inline int64_t chunk(int64_t n, int64_t cap, int64_t chunks) {
    if (n <= 0 || cap <= 0 || chunks < 1 || chunks > kMaxChunks) return -1;
    if (chunks == 1) return cap;
    if (chunks * kFloor > n) return -1;   // the last chunk could not reach the floor
    const int64_t per = std::min(cap, std::max(kFloor,
        (((n + chunks - 1) / chunks) + kGrid - 1) / kGrid * kGrid));
    return n - (chunks - 1) * per >= kFloor ? per : -1;
}

}  // namespace strata::prefill::pipeline_chunks
