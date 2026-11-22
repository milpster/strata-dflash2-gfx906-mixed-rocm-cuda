// include/strata/core/finite_rows.hpp - the finite-row scan of the helper's reply contract (helper-ipc-diet).
//
// "Finite" is std::isfinite's EXACTLY: not NaN and not +-inf; subnormals and +-0 are finite.  The bit
// test (bits & 0x7fffffff) >= 0x7f800000 is the IEEE-754 exponent rule - equal to !isfinite for every
// fp32 value - so every path answers the SAME rows: the engine's kErrNonFinite and the helper's
// non-finite row refusal trigger on the same floats the pre-fix scalar loop triggered on.
//
// The AVX2 loop compiles only where the TU was built with -mavx2 (the per-file COMPILE_OPTIONS on the
// two hot call sites, mirroring the kernels' flags at the top of CMakeLists) on x86 GNU-family
// compilers, and runs only where __builtin_cpu_supports says the CPU has AVX2.  Everything else takes
// the portable scalar loop - non-x86 builds, MSVC, AVX2-less CPUs.  Header-only by construction: no
// library symbols, so the host-IPC tests keep linking no library.
#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

#if (defined(__x86_64__) || defined(__i386__)) && defined(__AVX2__) && defined(__GNUC__)
#include <immintrin.h>
#define STRATA_FINITE_AVX2 1
#endif

namespace strata::core::expert_helper {

inline bool is_finite(float v) {
    uint32_t b;
    std::memcpy(&b, &v, 4);
    return (b & 0x7fffffffu) < 0x7f800000u;   // exponent all-ones: NaN or inf
}

/// True when every float in [p, p+n) is finite.  Bit-for-bit the scalar loop's answer: the 8-lane
/// test flags |x| >= 0x7f800000 by (bits & 0x7fffffff) + 0x800000 setting the sign bit (no 32-bit
/// overflow: max |bits| + 0x800000 = 0x807fffff), i.e. a signed cmpgt against zero per lane; the
/// row tail past the last full 8-lane group runs the scalar rule.
inline bool all_finite(const float* p, size_t n) {
#ifdef STRATA_FINITE_AVX2
    if (__builtin_cpu_supports("avx2")) {
        const __m256i sign = _mm256_set1_epi32(0x7fffffff);
        const __m256i bias = _mm256_set1_epi32(0x00800000);
        const __m256i zero = _mm256_set1_epi32(0);
        size_t i = 0;
        for (; i + 8 <= n; i += 8) {
            const __m256i w = _mm256_add_epi32(_mm256_and_si256(_mm256_loadu_si256((const __m256i*) (p + i)), sign), bias);
            if (_mm256_movemask_epi8(_mm256_cmpgt_epi32(zero, w)) != 0) return false;
        }
        for (; i < n; ++i)
            if (!is_finite(p[i])) return false;
        return true;
    }
#endif
    for (size_t i = 0; i < n; ++i)
        if (!is_finite(p[i])) return false;
    return true;
}

}  // namespace strata::core::expert_helper
