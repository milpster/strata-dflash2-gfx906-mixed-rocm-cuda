/// Sweep arithmetic for the arena cap + decode floor (IQ3_S shape): 24,576 pairs, 1.9517 MiB/pair
/// (47,964 MiB arena), hot budget 15,000 MiB stride = 7,500 pairs, helper's 4,615 mirror pairs,
/// 7,858 cache-resident pairs (the 358 past the hot budget drop with the stage).
#include <cstdio>
#include <vector>
#include <utility>
#include "strata/core/relief_arena_cap.hpp"

int main() {
    const int64_t L = 48, E = 512;
    const uint64_t pair_b = 2046122;   // 47,964 MiB / 24,576
    std::vector<std::pair<int32_t, int32_t>> profile;
    for (int64_t i = 0; i < L * E; ++i) profile.emplace_back((int32_t)(i / E), (int32_t)(i % E));
    std::vector<uint8_t> hot((size_t)(L * E), 0), dropped((size_t)(L * E), 0);
    for (int64_t i = 0; i < 7500; ++i) hot[i] = 1;              // the hot budget's pairs
    for (int64_t i = 7500; i < 7858; ++i) dropped[i] = 1;       // cache-resident past the hot budget
    for (int64_t i = 7858; i < 7858 + 4615; ++i) dropped[i] = 1;// the helper's mirror
    const uint64_t total = (uint64_t)(L * E) * pair_b;
    std::printf("total %llu MiB\n", (unsigned long long)(total >> 20));
    const long caps[] = {20000, 24000, 28000, 33600};
    const long ds[] = {0, 6000, 9600, 12000};
    for (long cap : caps) for (long d : ds) {
        const auto p = strata::core::relief_arena_cap_plan(L, E, total, (uint64_t)cap << 20,
            profile, hot, dropped, [&](int64_t) { return pair_b; }, (uint64_t)d << 20);
        std::printf("cap %5ld decode %5ld: drop %lld pairs (%lld MiB) kept %lld resident %llu MiB "
                    "prot %lld (%llu MiB) %s\n",
                    cap, d, (long long)p.drop.size(), (long long)(p.dropped_bytes >> 20),
                    (long long)p.kept_pairs, (unsigned long long)(p.resident_after >> 20),
                    (long long)p.decode_pairs, (unsigned long long)(p.decode_bytes >> 20),
                    p.short_of_cap ? "SHORT" : "ok");
    }
    return 0;
}
