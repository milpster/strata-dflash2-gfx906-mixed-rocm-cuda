# Optional gfx906 hyper-connection kernels

These two experiments are disabled at build time and at run time. They do not select
new model formats. They apply to the existing BF16 HC weights and activations, with
FP32 outputs, on an actual gfx906 wave64 device. A target-name override is not a
substitute for checking the physical card and loaded ROCm libraries.

Build with the existing gfx906 compatibility backend and exactly
`CMAKE_HIP_ARCHITECTURES=gfx906`. Add either or both:

- `-DSTRATA_GFX906_HC_BF16_N4=ON`
- `-DSTRATA_GFX906_HC_F16_EXACT=ON`

Both require `-DSTRATA_HIP_GFX906=ON`. The standard CUDA, wave32 HIP, RDNA2 SGEMM,
gfx1151 `STRATA_HCD_EXACT`, and current-main FP16 activation-image paths are retained.
With the new build flags off, the existing prefill source paths and Gemm layout are
unchanged. Enabling a build option alone does not enable a runtime route.

## HC injection, N=4

`STRATA_HC_BF16_N4=1` enables the narrow injection kernel only for T=4095/4096,
N=4, K=10240, packed BF16 inputs, and the original FP32 output layout. It reproduces
one inspected rocBLAS split-reduction schedule, including multiplication order. A
new ROCm library or compiler requires a new full-word comparison; tolerance-based
comparison is not acceptable for this route.

The optional workspace is 196,608 bytes, charged conservatively as 2 MiB, with a
256 MiB free-memory floor. It belongs to its prefill stage and is never part of the
cache, carve, relayout, or loan buffers. Resource refusals take the existing GEMM.
`STRATA_HC_BF16_N4_SHADOW=1` compares every candidate output word with a fresh BF16
reference and leaves the reference in the output. Shadow requires the main switch.

## HC down/up, scaled FP16 with exact input guards

`STRATA_HC_F16_EXACT=1` enables only T=4095/4096 and (N,K)=(320,10240) or
(10240,320), beta=0, packed BF16 inputs, and a valid FP32 output stride. Neither
`f16_io_` nor `x_f16` activation representations can enter this route. The N4
callsite also excludes the FP16 activation image introduced in current main.

Weights are multiplied by 4096 and activations by 512 before conversion to FP16;
the GEMM uses alpha=2^-21. Every input must be finite, remain finite and within FP16
range after scaling, and round-trip bit for bit. Positive and negative BF16 zero
preserve their sign. Any guard rejection runs the existing BF16 GEMM before the
candidate GEMM has written an output. This input guard is necessary, but does not
prove that a different library's FP16 and BF16 GEMM reduction orders match.

The independent workspace is 90,440,448 bytes, charged as 96 MiB. It requires
256 MiB of headroom after allocation. It is prepared after the complete N4 traversal
and mandatory serving allocations; neither experiment changes cache sizing. Each
stage owns its workspace, binds only a nonowning Gemm view, drains the pipeline on
normal return and exceptional unwind, clears the view, and frees on the owner.

`STRATA_HC_F16_EXACT_REFERENCE_ONLY=1` runs conversion/guards but retains BF16
output. The historical `STRATA_HC_F16_EXACT_SHADOW=1` computes a candidate and then
overwrites it with a BF16 reference. **That HC-F16 shadow mode does not itself
compare outputs.** It must not be presented as a parity check. Reference-only and
shadow require the main switch and cannot be combined. All switches accept only
exact `0` or `1`; absent switches mean OFF.

## Checks and qualification

The host tests exercise policy and ownership with a private fake runtime. They do
not establish GPU arithmetic parity. `hc_bf16_n4_parity` compares every FP32 output
word against this build's BF16 reference for 20 synthetic cases; it also checks
finite results and buffer sentinels. `hc_f16_exact_parity` compares bound candidate
and unbound reference calls in 56 cases over both HC shapes and both T values. It
requires 12 actual route takes, 24 guard rejections, 16 OFF/FP16-representation
bypasses and four reference-only cases, comparing every output word and checking
padding, sentinels and unchanged inputs. The `x_f16` probe verifies exclusion from
the new route; it does not qualify an FP16 activation-image path on gfx906. Its
separate host helper test checks the comparison and counter-checking logic, not GPU
math.

Run the component tests on each physical gfx906 independently, with all unrelated
experimental runtime selectors disabled. Do not turn a skip, a mismatch, a resource
refusal, or an unexecuted test into PASS. After component checks, qualify real
activations with full-word comparisons, then a candidate-output model run, and
repeat the established 64K and 200K profiles on this exact current-main build.
Historical results from the earlier source tree are supporting evidence only.
No benchmark or model-quality claim is made for this port until those runs finish.
