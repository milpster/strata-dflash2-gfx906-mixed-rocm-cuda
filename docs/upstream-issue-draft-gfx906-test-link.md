# Draft upstream issue — NOT submitted. Operator decision required before filing.

Repo: Niko1221/Strata. Searched first: no issue mentions `native_moe_gated_test`; `prefill_decode_golden`
appears only in passing inside #6515 (gfx12 `nw_barrier`, unrelated).

## Title

[Engine] gfx906: no `.cu` under `tests/cuda/` gets a compile rule — `native_moe_gated_test` and
`prefill_decode_golden` fail to link

## Body

**Problem.** On a clean checkout of tag `v0.2.18`, configured for gfx906, the test targets
`native_moe_gated_test` and `prefill_decode_golden` do not link: `undefined symbol: main`. No `.cu` file under
`tests/cuda/` receives a compile rule, so each target links its libraries with no object behind them.

**Result.** A default `ninja` build reaches 566 targets and stops there on the tag; the two test targets never
build on this card.

**How it was checked.** The tag was checked out into a scratch worktree on this machine and configured with the
gfx906 recipe (`-DSTRATA_HIP_GFX906=ON`, `CMAKE_HIP_ARCHITECTURES=gfx906`, compiler
`/opt/rocm-6.1.0/llvm/bin/clang++`, prefix `/opt/rocm-6.1.0`). The link lines and the zero compile rules are
identical on the tag and on a branch that carries unrelated local changes, so the fault is the tag's. The
scratch tree was removed after the check.

**Hardware.** 2× Radeon VII (gfx906), ROCm 6.1.0 userspace, custom xnack rocBLAS at
`/home/srcds/rocm-gfx906-xnack/lib`.

**Not tested.** No other backend (no CUDA, SYCL or Metal run); no device execution of these two tests — the
fault is at build time only. This is a build-level finding; nothing about the tests' runtime behavior is claimed.
