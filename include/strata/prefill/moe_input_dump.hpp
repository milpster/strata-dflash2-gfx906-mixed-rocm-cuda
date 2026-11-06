#pragma once

// Measurement-only input format shared with p2_measure::load_moe_dump.
// No device work here. Existing files are never overwritten; partial failures stay
// on disk as evidence and callers must not treat them as a successful capture.
#include <cstdint>
#include <cstdio>
#include <limits>

namespace strata::prefill {
inline bool write_moe_input_dump(const char* path, int64_t t, int64_t n, int64_t k,
                                 const float* x, const int32_t* ids) {
    const auto limit = std::numeric_limits<size_t>::max();
    if (!path || !*path || !x || !ids || t <= 0 || n <= 0 || k <= 0 ||
        uint64_t(t) > limit / sizeof(float) / uint64_t(n) ||
        uint64_t(t) > limit / sizeof(int32_t) / uint64_t(k)) return false;
    std::FILE* f = std::fopen(path, "wbx");
    if (!f) return false;
    const int64_t header[3] = {t, n, k};
    const size_t nx = size_t(t) * size_t(n), ni = size_t(t) * size_t(k);
    const bool ok = std::fwrite(header, sizeof(header), 1, f) == 1 &&
                    std::fwrite(x, sizeof(float), nx, f) == nx &&
                    std::fwrite(ids, sizeof(int32_t), ni, f) == ni;
    return std::fclose(f) == 0 && ok;
}
}  // namespace strata::prefill
