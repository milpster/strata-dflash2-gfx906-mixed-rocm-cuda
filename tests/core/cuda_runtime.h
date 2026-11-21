// tests/core/cuda_runtime_shim.h - minimal CUDA runtime declarations for CPU-only tests.
//
// pin_budget.hpp includes <cuda_runtime.h> unconditionally.  This shim provides
// just enough of the CUDA API surface that the header parses under a plain host
// compiler with no GPU SDK/runtime present.  It is used ONLY by the
// native_mlock_test and native_mlock_nonlinux_probe targets; production builds
// use the real ROCm/CUDA headers via include/strata/platform/hip_compat.
#pragma once

#include <cstddef>
#include <cstdint>

typedef int cudaError_t;

#define cudaSuccess 0
#define cudaErrorMemoryAllocation 2
#define cudaHostAllocPortable 1

static inline cudaError_t cudaHostAlloc(void** ptr, size_t size, unsigned int flags) {
    (void)ptr; (void)size; (void)flags;
    return cudaSuccess;
}
static inline cudaError_t cudaFreeHost(void* ptr) {
    (void)ptr;
    return cudaSuccess;
}
static inline cudaError_t cudaHostRegister(void* ptr, size_t size, unsigned int flags) {
    (void)ptr; (void)size; (void)flags;
    return cudaSuccess;
}
static inline cudaError_t cudaHostUnregister(void* ptr) {
    (void)ptr;
    return cudaSuccess;
}
static inline cudaError_t cudaGetLastError() {
    return cudaSuccess;
}
