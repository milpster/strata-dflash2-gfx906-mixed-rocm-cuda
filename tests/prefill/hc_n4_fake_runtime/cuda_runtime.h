#pragma once
// This directory is used only by hc_bf16_n4_owned_test, never by an engine target.
#include <cstddef>
using cudaStream_t = void*;
using cudaError_t = int;
constexpr int cudaSuccess = 0, cudaErrorMemoryAllocation = 2, cudaMemcpyDeviceToHost = 3;
cudaError_t cudaGetDevice(int*);
cudaError_t cudaSetDevice(int);
cudaError_t cudaMemGetInfo(size_t*, size_t*);
cudaError_t cudaMalloc(void**, size_t);
cudaError_t cudaFree(void*);
cudaError_t cudaGetLastError();
cudaError_t cudaMemcpyAsync(void*, const void*, size_t, int, cudaStream_t);
cudaError_t cudaStreamSynchronize(cudaStream_t);
constexpr int cudaMemcpyHostToDevice = 4;
const char* cudaGetErrorString(cudaError_t);
cudaError_t cudaMemcpy(void*, const void*, size_t, int);
cudaError_t cudaStreamCreate(cudaStream_t*);
cudaError_t cudaStreamDestroy(cudaStream_t);
