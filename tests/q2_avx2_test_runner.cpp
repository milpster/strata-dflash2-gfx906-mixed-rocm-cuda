// Test-only baseline-ISA launcher. Never link the AVX2 production TU into this
// executable: its static initialization must run only after the feature check.
#include <cstdio>
#include <cstring>
#if (defined(__i386__) || defined(__x86_64__)) && (defined(__GNUC__) || defined(__clang__))
#include <cpuid.h>
#endif
#if defined(_WIN32)
#include <process.h>
#else
#include <unistd.h>
#endif

static bool supported() {
#if (defined(__i386__) || defined(__x86_64__)) && (defined(__GNUC__) || defined(__clang__))
    __builtin_cpu_init();
    // Older supported Clang versions do not recognize the "f16c" builtin name.
    // AVX2 admission below also checks the OS support for AVX register state.
    unsigned int eax, ebx, ecx, edx;
    if (!__get_cpuid(1, &eax, &ebx, &ecx, &edx)) return false;
    return __builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma") &&
           ((ecx & (1u << 29)) != 0);
#else
    return false;
#endif
}

int main(int argc, char** argv) {
    if (argc != 3 || (std::strcmp(argv[2], "on") && std::strcmp(argv[2], "off"))) {
        std::fputs("usage: q2_avx2_test_runner TEST_BINARY on|off\n", stderr);
        return 2;
    }
    if (!supported()) {
        std::fputs("SKIP q2_avx2_spread: requires AVX2, FMA and F16C\n", stdout);
        return 77;
    }
#if defined(_WIN32)
    // Wait explicitly: Windows _execv does not preserve POSIX exec semantics.
    // Keep argv[0] space-free because the CRT flattens the argument array.
    const char* arguments[] = {"q2_avx2_spread_test", argv[2], nullptr};
    const auto status = _spawnv(_P_WAIT, argv[1], arguments);
    if (status != -1) return static_cast<int>(status);
#else
    execv(argv[1], argv + 1);
#endif
    std::perror("q2_avx2_test_runner: cannot execute test");
    return 1;
}
