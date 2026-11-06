<!-- Preserved from the llama.cpp-era Vega 20 setup on this box (rocBLAS gfx906 fallback-crash investigation).
     Paths are relative to that era's home layout; the workaround patch targets llama.cpp's ggml-cuda.cu, not this fork. -->
# rocBLAS 6.1.0 Segfault on gfx906 (Radeon VII / MI50 / MI60) — Missing Fallback Kernels

## Summary

`librocblas.so.4.1.60100` shipped with ROCm 6.1.0 (and identical file in 6.4.4) crashes with a NULL pointer dereference inside `Tensile::PlaceholderLibrary::loadPlaceholderLibrary()` for any GEMM shape on gfx906 that lacks a **tuned** kernel **AND** lacks a **fallback** kernel. The crash affects all FP32 (SS), FP16 (HH), and other non-MMQ GEMM paths.

The root cause is that the lazy-loading kernel metadata (`.dat` files) for the fallback paths is **not shipped** with the rocBLAS library — only the compiled `.hsaco` code files exist. rocBLAS detects the missing metadata, prints `"No fallback kerenel is defined. Will return a null pointer"` (sic — typo in rocBLAS source), returns NULL, and the caller dereferences it → SIGSEGV.

## Environment

- **GPU**: gfx906 (Radeon VII / MI50 / MI60 — Vega 20)
- **ROCm**: 6.1.0 (`/opt/rocm-6.1.0/.info/version` reports `6.1.0-82`)
- **rocBLAS**: `librocblas.so.4.1.60100` (identical file present in `/opt/rocm-6.4.4/lib/`)
- **OS**: Ubuntu (kernel 7.0.14-x64v3-xanmod1)
- **Driver**: amdgpu kernel module (DKMS, xanmod)
- **Application**: llama.cpp (HIP backend, any recent version)

## Bug Details

### Crash signature

```
Thread 1 "llama-server" received signal SIGSEGV, Segmentation fault.
0x00007fffed605f43 in Tensile::PlaceholderLibrary<Tensile::ContractionProblem, Tensile::ContractionSolution>::loadPlaceholderLibrary() const
   from /opt/rocm-6.1.0/lib/librocblas.so.4
```

Kernel log:
```
llama-server[1870278]: segfault at 8 ip 00007f38a2005f43 sp 00007fff6a608fa0 error 4
  in librocblas.so.4.1.60100[34204f43,7f38a1068000+fd3000] likely on CPU 13 (core 6, socket 0)
Code: 8b 7c 24 38 48 8d 44 24 48 48 39 c7 74 05 e8 a4 fd 02 00 48 8b 7c 24 10 4c 39 ff
      74 05 e8 95 fd 02 00 4c 8b bc 24 98 00 00 00 <49> 8b 47 08 49 89 46 08 49 8b 6f 10
```

Faulting instruction decodes to:
```
mov r15, [rsp+0x98]   ; load pointer field (NULL because metadata missing)
mov rax, [r15+8]      ; ← CRASH: dereference r15+8 with r15==NULL
```

### Triggering GEMM shape

Crashes reproducibly on this `rocblas_sgemm` / `rocblas_gemm_ex` call:

```
./rocblas-bench -f gemm -r f32_r --transposeA T --transposeB N \
  -m 48 -n 384 -k 5120 --alpha 1 --lda 5120 --ldb 5120 --beta 0 --ldc 48
```

(Any `n` reproduces — `n=256`, `n=384`, `n=512` all crash. The bug is gated on `m=48` and `k=5120`, not `n`.)

Same crash occurs with FP16 inputs:
```
./rocblas-bench -f gemm_ex --transposeA T --transposeB N \
  -m 48 -n 384 -k 5120 --alpha 1 --a_type f16_r --lda 5120 --b_type f16_r --ldb 5120 \
  --beta 0 --c_type f16_r --ldc 48 --d_type f16_r --ldd 48 --compute_type f16_r
```

### Stack trace (gdb `bt`)

```
#0  Tensile::PlaceholderLibrary<...>::loadPlaceholderLibrary() const
#1  Tensile::PlaceholderLibrary<...>::findBestSolution(...)
#2  Tensile::ExactLogicLibrary<...>::findBestSolution(...)        (×4 frames)
#6  Tensile::ProblemMapLibrary<...>::findBestSolution(...)
#7  Tensile::ExactLogicLibrary<...>::findBestSolution(...)        [HardwarePredicate]
#8  Tensile::CachingLibrary<...>::findBestSolution(...)
#9  Tensile::MasterSolutionLibrary<...>::findBestSolution_runner(...)
#10 Tensile::MasterSolutionLibrary<...>::findBestSolution(...)
#11 runContractionProblem<float,...>
#12 rocblas_internal_gemm_template<false, float, ...>
#13 rocblas_sgemm
#14 hipblasSgemm
#15 ggml_cuda_mul_mat_cublas          (llama.cpp — caller)
```

The crash site is in the **fallback-kernel lookup path** of Tensile. After failing to find a tuned kernel for the requested shape, rocBLAS attempts to load the fallback kernel — but the fallback metadata file is missing on disk → NULL → crash.

## Root Cause

### Missing `.dat` files

rocBLAS expects each fallback kernel to ship with **both**:
- `TensileLibrary_Type_<TT>_Contraction_l_<layout>_fallback_gfx906-xnack-.hsaco` — compiled GPU code
- `TensileLibrary_Type_<TT>_Contraction_l_<layout>_fallback_gfx906.dat` — Tensile metadata

For gfx906 in ROCm 6.1.0, only the `.hsaco` is shipped. The `.dat` is missing for **all** fallback variants:

```
$ ls /opt/rocm-6.1.0/lib/rocblas/library/ | grep -E "fallback.*gfx906.*\.dat"
(no output)

$ ls /opt/rocm-6.1.0/lib/rocblas/library/ | grep -E "fallback.*gfx906.*\.hsaco"
TensileLibrary_Type_SS_Contraction_l_Ailk_Bjlk_Cijk_Dijk_fallback_gfx906-xnack-.hsaco
TensileLibrary_Type_SS_Contraction_l_Ailk_Bljk_Cijk_Dijk_fallback_gfx906-xnack-.hsaco
TensileLibrary_Type_SS_Contraction_l_Alik_Bjlk_Cijk_Dijk_fallback_gfx906-xnack-.hsaco
TensileLibrary_Type_SS_Contraction_l_Alik_Bljk_Cijk_Dijk_fallback_gfx906-xnack-.hsaco
TensileLibrary_Type_HH_Contraction_l_Ailk_Bjlk_Cijk_Dijk_fallback_gfx906-xnack-.hsaco
... (and DD, CC, ZZ, BS, BB variants — all .hsaco only, no .dat)
```

### Confirmation in rocBLAS binary

The string `"No fallback kerenel is defined. Will return a null pointer"` (sic) is present in `librocblas.so.4.1.60100`, confirming this is the failure path:

```bash
$ strings /opt/rocm-6.1.0/lib/librocblas.so.4.1.60100 | grep -i fallback
No fallback kerenel is defined. Will return a null pointer
Returning the fallback kernel:
Failed to find a valid kernel after searching the full ensamble. will return the fallback kernel.
```

### ROCm 7.15.0 ships the missing files

The same `.dat` files **do exist** in ROCm 7.15.0:

```
$ ls /opt/rocm/lib/rocblas/library/gfx906/ | grep -E "fallback.*\.dat"
TensileLibrary_Type_SS_Contraction_l_Ailk_Bjlk_Cijk_Dijk_fallback_gfx906.dat
TensileLibrary_Type_SS_Contraction_l_Ailk_Bljk_Cijk_Dijk_fallback_gfx906.dat
TensileLibrary_Type_SS_Contraction_l_Alik_Bjlk_Cijk_Dijk_fallback_gfx906.dat
TensileLibrary_Type_SS_Contraction_l_Alik_Bljk_Cijk_Dijk_fallback_gfx906.dat
... (and HH, DD, CC, ZZ variants)
```

This indicates the missing files are a **packaging regression** in the 6.1.0 release — the kernels were generated but the metadata was not included.

### Why grafting 7.15's files into 6.1 doesn't work

The lazy index file format changed between 6.1 and 7.15:

| Version | Lazy index entry example |
|---|---|
| 6.1 | `ATensileLibrary_Type_SS_Contraction_l_Alik_Bljk_Cijk_Dijk_fallback` |
| 7.15 | `HTensileLibrary_Type_SS_Contraction_l_Alik_Bljk_Cijk_Dijk_fallback_gfx906` |

Different prefix codes (`A` vs `H`) and different path conventions (with/without `_gfx906` suffix). Just copying 7.15's `.dat` files into 6.1's library directory does **not** fix the crash — the 6.1 lazy loader does not reference the new files.

## Reproduction

### Minimal repro

```bash
LD_LIBRARY_PATH=/opt/rocm-6.1.0/lib /opt/rocm-6.1.0/bin/rocblas-bench \
  -f gemm -r f32_r --transposeA T --transposeB N \
  -m 48 -n 384 -k 5120 --alpha 1 --lda 5120 --ldb 5120 --beta 0 --ldc 48
# Segfault
```

### Via llama.cpp

Build llama.cpp HIP backend against ROCm 6.1.0 with `GGML_CUDA_FORCE_MMQ=ON`. Run `llama-server` with any Qwen3-style model that has an F32 or F16 weight tensor with output dim 48 (or other "weird" m value lacking a tuned kernel).

The crash is deterministic and immediate on the first inference step.

## Workaround (applied locally)

Patch `ggml/src/ggml-cuda/ggml-cuda.cu` in llama.cpp to route small-m GEMMs (`m ≤ 64`) through a naive HIP kernel that bypasses rocBLAS entirely:

1. Add a `naive_sgemm_kernel` (~30 lines of HIP code).
2. In `ggml_cuda_mul_mat_cublas` dispatcher: force `compute_type = GGML_TYPE_F32` when `src0->ne[1] <= 64`.
3. In `ggml_cuda_mul_mat_cublas_impl<GGML_TYPE_F32>`: when `ne01 <= 64`, call `naive_sgemm` instead of `cublasSgemm`.

Full patch: [`rocblas-gfx906-workaround.patch`](./rocblas-gfx906-workaround.patch) (96 lines).

Cost: ~50 µs per inference for the m=48 GEMM. Negligible.

## Impact

- **Performance**: Unpatched, users with gfx906 hardware must choose between
  - ROCm 6.1.0 (fast, ~370 tps in llama.cpp, but crashes)
  - ROCm 7.15.0 (stable, but ~277 tps — 25% slower due to tuning rot)
- **Hardware affected**: Radeon VII, MI50, MI60 (all gfx906). Possibly other "legacy" arches (gfx900, gfx908, gfx90a) — not tested.
- **Workload affected**: Any application using rocBLAS GEMM with shapes that don't match a tuned kernel. Most prominent: LLM inference (llama.cpp, vLLM, etc.) with modern transformer architectures that produce unusual projection dims.

## Recommendations

### For AMD / rocBLAS team

1. **Ship the missing `.dat` files** for fallback kernels on all supported architectures in the 6.1.x and 6.4.x release branches. The metadata exists internally (it's shipped in 7.15) — this is a packaging gap.
2. **Add a NULL check** in `loadPlaceholderLibrary()` (or its caller) to return `rocblas_status_not_initialized` instead of dereferencing NULL. The string `"No fallback kerenel is defined. Will return a null pointer"` shows the code path **knows** it's about to return NULL — it should return an error code, not crash.
3. **Add a smoke test** that exercises GEMM with off-tuned shapes (non-power-of-2 `m`, `n`, `k`) on every supported arch in CI.

### For llama.cpp

The patch above is a reasonable defense-in-depth measure even after AMD fixes rocBLAS, because:
- Other ROCm versions may have similar gaps.
- Other "legacy" arches may be affected.
- The naive kernel adds negligible cost and provides a deterministic fallback.

Suggested upstream patch:
- Gate the workaround on a runtime ROCm version check (`hipRuntimeGetVersionInfo`) so it only activates on known-broken versions.
- Or gate on `GGML_CUDA_NO_ROCBLAS_FALLBACK_CRASH` env var.
- Or always use naive for `m < threshold` (cheaper than rocBLAS dispatch overhead for tiny GEMMs anyway).

## Timeline / Versions Tested

| ROCm version | `librocblas.so.4.1.60100` present? | Crash reproducible? | Notes |
|---|---|---|---|
| 6.1.0 | yes | **yes** | Bug present |
| 6.4.4 | yes (identical file) | **yes** | Same file, same bug |
| 7.2.4 | only `library/` data files, no `.so` | n/a | Incomplete install on test machine |
| 7.15.0 | `librocblas.so.5.x` (different soname) | no | Different ABI; has the missing `.dat` files |

## Contact / Files

- Workaround patch: `~/rocblas-gfx906-workaround.patch`
- This writeup: `~/rocblas-gfx906-bug-report.md`
- Mixed library experiment (didn't work, kept for reference): `~/rocblas-mixed/`
