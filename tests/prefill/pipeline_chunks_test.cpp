// STRATA_PIPELINE_CHUNKS: the explicit chunk count's math (header-only, no GPU).
#include <cstdio>

#include "strata/prefill/pipeline_chunks.hpp"

using strata::prefill::pipeline_chunks::chunk;
using strata::prefill::pipeline_chunks::kFloor;

static int failed = 0;
#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #x); ++failed; } } while (0)

// the chunks a (n, chunk) read really is: count, and the last chunk's size
static int chunk_count(int64_t n, int64_t c) { return (int) ((n + c - 1) / c); }
static int64_t last_chunk(int64_t n, int64_t c) { return n - (chunk_count(n, c) - 1) * c; }

int main() {
    const int64_t cap = 16384;   // the rig's buffers (the 80k legs read 14,637 tokens in one chunk)

    // the two legs of the 2026-11-10 window: 2 chunks each, equal on the 256 grid, remainder last
    const int64_t c80 = chunk(14637, cap, 2);
    CHECK(c80 == 7424);
    CHECK(chunk_count(14637, c80) == 2);
    CHECK(last_chunk(14637, c80) == 7213);
    CHECK(last_chunk(14637, c80) >= kFloor);
    const int64_t c8 = chunk(1643, cap, 2);
    CHECK(c8 == 1024);
    CHECK(chunk_count(1643, c8) == 2);
    CHECK(last_chunk(1643, c8) == 619);
    CHECK(last_chunk(1643, c8) >= kFloor);

    // 1 chunk is today's read: exactly the buffers' chunk (the plan then equals today's C, and the read
    // is single-chunk with no pipe flag - byte-for-byte the unplaned path)
    CHECK(chunk(14637, cap, 1) == cap);
    CHECK(chunk(1, cap, 1) == cap);
    CHECK(chunk(14637, 8192, 1) == 8192);

    // more chunks, still legal: equal, on the 256 grid, the last one at or above the floor
    CHECK(chunk(14637, cap, 3) == 5120);
    CHECK(chunk_count(14637, 5120) == 3);
    CHECK(last_chunk(14637, 5120) == 4397);
    CHECK(chunk(14637, cap, 4) == 3840);
    CHECK(chunk_count(14637, 3840) == 4);
    CHECK(last_chunk(14637, 3840) == 3117);
    CHECK(chunk(14637, cap, 5) == 3072);
    CHECK(last_chunk(14637, 3072) == 2349);
    CHECK(chunk(14637, cap, 6) == 2560);
    CHECK(last_chunk(14637, 2560) == 14637 - 5 * 2560);   // 1837
    CHECK(chunk(14637, cap, 7) == 2304);
    CHECK(last_chunk(14637, 2304) == 813);
    CHECK(chunk(14637, cap, 8) == -1);      // ceil256(1830) = 2048 leaves 14637 - 7*2048 = 301 < 512
    CHECK(chunk(14637, cap, 29) == -1);     // 29*512 > 14637 (the 12:20 sweep's 29 chunks are refused here)

    // floor rejection: the LAST chunk must reach the floor, not just the equal share
    CHECK(chunk(1643, cap, 3) == -1);       // 1643 - 2*768 = 107 < 512
    CHECK(chunk(1643, cap, 4) == -1);       // 4*512 > 1643
    CHECK(chunk(1023, cap, 2) == -1);       // 1023 - 512 = 511
    CHECK(chunk(1024, cap, 2) == 512);      // the smallest legal 2-chunk prompt
    CHECK(chunk(1024, cap, 3) == -1);       // 3*512 > 1024

    // cap: the chunk never exceeds the buffers (so the plan cannot ask for a bigger read than init gave)
    CHECK(chunk(20000, 8192, 2) == 8192);
    CHECK(chunk_count(20000, 8192) == 3);   // the prompt is longer than 2 chunks' worth: the plan caps
    CHECK(chunk(6000, 1024, 2) == 1024);
    CHECK(last_chunk(6000, 1024) == 6000 - 5 * 1024);   // the buffers' cap wins: 1024, so 6 chunks (800 last)

    // out of range: refused, never silently accepted
    CHECK(chunk(14637, cap, 0) == -1);
    CHECK(chunk(14637, cap, -3) == -1);
    CHECK(chunk(14637, cap, 65) == -1);     // kMaxChunks is the pipe path's own bound
    CHECK(chunk(14637, cap, 64) == -1);     // 64*512 = 32768 > 14637: the last chunk cannot reach the floor
    CHECK(chunk(32768, 32768, 64) == 512);  // 64 * 512 = 32768 exactly: legal, every chunk is 512
    CHECK(chunk(0, cap, 2) == -1);
    CHECK(chunk(14637, 0, 2) == -1);

    if (failed) {
        std::fprintf(stderr, "%d check(s) failed\n", failed);
        return 1;
    }
    std::fprintf(stderr, "pipeline_chunks_test: all checks passed\n");
    return 0;
}
