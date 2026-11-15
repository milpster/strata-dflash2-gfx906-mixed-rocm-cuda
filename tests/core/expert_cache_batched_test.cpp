// F6 slice 1: ExpertCache::fill_slots_batched.  Every slot goes through the batched pinned-staged pipeline and
// `verify_slot` says its bytes are right - the engine's own byte check, applied to EVERY slot (the exhaustive
// host-side proof the boot line's one-slot spot check is not).  The batched bytes are then proven identical to
// the `fill_slot_blocking` bytes: read the slot back after the batched fill, poison it through the blocking
// path, re-fill the same blob through the blocking path, read again, and compare.  Covers the batch shapes the
// half-ring's event path needs (> 2 x kBatchStage), blobs smaller than a slot, the refusals (bad slot, null
// blob) leaving the cache usable, `fills_` accounting, and the ring's reallocation after `close()`.
// Needs a CUDA device (exits 77 without).
#include "strata/core/expert_cache.hpp"

#include <cuda_runtime.h>

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace {

int g_fail = 0;

void check(bool ok, const char* what) {
    if (!ok) {
        std::fprintf(stderr, "FAIL: %s\n", what);
        ++g_fail;
    }
}

// deterministic pseudo-random bytes - no RNG dependency on the runner
uint64_t g_rng = 0x9E3779B97F4A7C15ull;
uint8_t rbyte() {
    g_rng ^= g_rng << 13;
    g_rng ^= g_rng >> 7;
    g_rng ^= g_rng << 17;
    return (uint8_t) (g_rng >> 33);
}
std::vector<uint8_t> rbytes(size_t n) {
    std::vector<uint8_t> v(n);
    for (auto& b : v) b = rbyte();
    return v;
}

constexpr int32_t kSlots = 24;       // the rounds below end at every offset within the 8-blob halves
constexpr int64_t kBlob = 16384;

}  // namespace

int main() {
    int n = 0;
    if (cudaGetDeviceCount(&n) != cudaSuccess || n == 0) {
        std::puts("no CUDA device: skipped");
        return 77;
    }
    using strata::core::ExpertCache;
    using Batch = ExpertCache::BatchFill;
    std::string err;

    ExpertCache c;
    if (!c.open(kSlots, 3, 8, kBlob, err)) { std::fprintf(stderr, "open: %s\n", err.c_str()); return 1; }
    int64_t fills = 0;

    // 1. every batch shape around the half ring (kBatchStage = 8 halves, 2 of them): the > 16 rounds are the
    //    ones that must wait on a half's event before overwriting it.  bytes = 0: the whole slot.
    for (int m : {1, 8, 9, 16, 17, 19, (int) kSlots}) {
        std::vector<std::vector<uint8_t>> blobs((size_t) m);
        for (auto& b : blobs) b = rbytes((size_t) kBlob);
        std::vector<Batch> items((size_t) m);
        for (int32_t s = 0; s < m; ++s) items[(size_t) s] = {s, blobs[(size_t) s].data(), 0};
        check(c.fill_slots_batched(items.data(), m, err), "batched fill lands");
        fills += m;
        for (int32_t s = 0; s < m; ++s)
            check(c.verify_slot(s, blobs[(size_t) s].data(), err), "batched fill: every slot holds its blob, byte for byte");
        check(c.fills() == fills, "fills() counts one per blob");
    }

    // 2. the byte proof against fill_slot_blocking: the device bytes the batched path wrote, a poison fill
    //    through the blocking path (so the second read cannot be stale), and the blocking path's own write of
    //    the same blob must be one and the same byte image.  One batch of kSlots: several half swaps.
    std::vector<std::vector<uint8_t>> proof((size_t) kSlots);
    for (auto& b : proof) b = rbytes((size_t) kBlob);
    std::vector<Batch> items((size_t) kSlots);
    for (int32_t s = 0; s < kSlots; ++s) items[(size_t) s] = {s, proof[(size_t) s].data(), kBlob};
    check(c.fill_slots_batched(items.data(), kSlots, err), "batched fill of the byte-proof blobs");
    fills += kSlots;
    std::vector<uint8_t> got1((size_t) kBlob), got2((size_t) kBlob);
    for (int32_t s = 0; s < kSlots; ++s) {
        check(c.verify_slot(s, proof[(size_t) s].data(), err), "verify after the batched byte-proof fill");
        check(cudaMemcpy(got1.data(), c.device_slot(s), (size_t) kBlob, cudaMemcpyDeviceToHost) == cudaSuccess,
              "read the slot back after the batched fill");
    }
    for (int32_t s = 0; s < kSlots; ++s) {
        const std::vector<uint8_t> poison((size_t) kBlob, (uint8_t) (s * 7 + 1));
        check(c.fill_slot_blocking(s, poison.data(), err, kBlob), "poison the slot through fill_slot_blocking");
        ++fills;
    }
    for (int32_t s = 0; s < kSlots; ++s) {
        check(c.fill_slot_blocking(s, proof[(size_t) s].data(), err, kBlob), "re-fill the byte-proof blob through fill_slot_blocking");
        ++fills;
        check(c.verify_slot(s, proof[(size_t) s].data(), err), "verify after the blocking fill");
        check(cudaMemcpy(got2.data(), c.device_slot(s), (size_t) kBlob, cudaMemcpyDeviceToHost) == cudaSuccess,
              "read the slot back after the blocking fill");
        check(std::memcmp(got1.data(), got2.data(), (size_t) kBlob) == 0, "the batched bytes are the fill_slot_blocking bytes");
    }

    // 3. bytes smaller than the slot: each blob is distinct and only its prefix travels, so a staging stride
    //    that did not advance by the slot's blob size would fail a verify here.  The items are (re)built AFTER
    //    the buffers they point into are refreshed, so their pointers are always the live ones.
    for (int32_t s = 0; s < kSlots; ++s) {
        proof[(size_t) s] = rbytes((size_t) kBlob);
        items[(size_t) s] = {s, proof[(size_t) s].data(), 6000};
    }
    check(c.fill_slots_batched(items.data(), kSlots, err), "batched fill of prefixes (bytes < the slot)");
    fills += kSlots;
    for (int32_t s = 0; s < kSlots; ++s)
        check(c.verify_slot(s, proof[(size_t) s].data(), err, 6000), "batched fill: the prefix landed in the right slot");

    // 4. refusals say so with nothing enqueued, and the cache fills on afterwards
    const Batch bad_slot = {kSlots, proof[0].data(), kBlob};
    err.clear();
    check(!c.fill_slots_batched(&bad_slot, 1, err) && !err.empty(), "a slot outside the arena is refused");
    const Batch null_blob = {0, nullptr, kBlob};
    check(!c.fill_slots_batched(&null_blob, 1, err) && !err.empty(), "a null blob is refused");
    check(c.fill_slots_batched(nullptr, 0, err), "an empty batch is a no-op");
    check(c.fill_slots_batched(items.data(), kSlots, err), "the cache fills again after the refusals");
    fills += kSlots;
    for (int32_t s = 0; s < kSlots; ++s)
        check(c.verify_slot(s, proof[(size_t) s].data(), err, 6000), "the batch after a refusal is intact");
    check(c.fills() == fills, "fills() is unchanged per blob across refusals");

    // 5. close() frees the ring, the stream and the events; the next fill allocates them again
    c.close();
    if (!c.open(kSlots, 3, 8, kBlob, err)) { std::fprintf(stderr, "reopen: %s\n", err.c_str()); return 1; }
    check(c.fill_slots_batched(items.data(), kSlots, err), "the batched fill works again after close and reopen");
    for (int32_t s = 0; s < kSlots; ++s)
        check(c.verify_slot(s, proof[(size_t) s].data(), err, 6000), "verify after the reopen");
    c.close();

    if (g_fail != 0) {
        std::fprintf(stderr, "expert cache batched fill: %d checks failed\n", g_fail);
        return 1;
    }
    std::puts("expert cache batched fill: the half-ring pipeline matches fill_slot_blocking byte for byte, every slot");
    return 0;
}
