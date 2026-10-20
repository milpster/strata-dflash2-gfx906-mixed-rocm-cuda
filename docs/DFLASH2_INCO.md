# DFlash2 for the official Inco checkpoint (Qwen3.8-Flash-Next-DFlash2)

This file records the artifact work we have measured. Runtime (engine load, generation, server)
is **not verified on this machine yet** - DFlash2 compiles only for NVIDIA CUDA today
(CMakeLists refuses `STRATA_ENABLE_HIP`/`STRATA_HIP_GFX906` with `STRATA_ENABLE_DFLASH2`), so the
gfx906 port is open work. Nothing in the "runtime" section below is a claim that a run worked.

## The checkpoint

Source: https://huggingface.co/incoai/Qwen3.8-Flash-Next-DFlash2, revision
`a22a1744e85c17364dcc54a6a420af7135d4c7f6` (sole revision, confirmed through the HF API 2026-10-20).

| file | size | sha256 |
|---|---|---|
| model.safetensors | 1,206,140,008 B | `d6723f2a359eaf33ec0c95f22ae8edda896774020c552296a85e1913b4d3186f` |

81 BF16 tensors, 603,065,600 parameters, no embedding and no LM head (the target supplies both).
Geometry: hidden 2560, 5 layers, intermediate 7680, 32 query / 8 KV heads at head_dim 128,
vocabulary 248320, block 8, conv kernel 2 / group 16, selector rank 256 / top-k 16,
mask token 248077, taps [5,15,25,35,45] over a 48-layer target, RoPE theta 10000, sliding window
2048 on all layers, `max_position_embeddings` 262144.

The capture contract (from `patches/qwen38-dflash2-capture.patch` in the repo): taps are the
**completed** layer outputs, averaged over the four hyper-connection streams
(`hidden_states.unflatten(-1,(4,2560)).mean(dim=-2)`), TP1 only. The GGUF key
`dflash.target_hc_mean=true` records that reading; the converter sets it for the official geometry.

## The artifact

Converted 2026-10-20 on Linux (python 3.11.9, numpy 2.3.5):

```
python3 tools/dflash_gguf.py /mnt/user/dflash2/checkpoint/model.safetensors \
    -o /mnt/user/dflash2/artifacts/inco-dflash2-bf16.gguf \
    --config /mnt/user/dflash2/checkpoint/config.json
```

Result: 1,206,137,248 B, sha256
`f9f8f5c0fecb49fa6c5a24815a8b76f4c14f868c1e57b198784da96c1b029084`. The converter's own
read-back self-check passed: all 81 tensors round-trip bit-exact through BF16, and
`dflash.source.sha256` carries `d6723f2a…3186f` inside the file (provenance travels with the
artifact; the sidecar manifest is `/mnt/user/dflash2/artifacts/manifest.json`).

## Element layout proof (no transposes)

GGUF dims are the torch dims reversed (dim 0 varies fastest) with the row-major bytes untouched -
the standard llama.cpp convention. `tools/test_dflash2_inco.py` validates this against the
kernels, not against the converter:

- conv base: `src/kernels/cuda/dflash2.cu` `conv_kernel` reads
  `base[(side*kernel + tap)*hidden + channel]`. For GGUF dims `[hidden, kernel, 2]` the flat index
  of `(channel, tap, side)` is `channel + tap*hidden + side*hidden*kernel` - the same arithmetic.
  The checkpoint's `attention_conv.base_kernel` `[2, 2, 2560]` is `[side, tap, channel]` in torch
  row-major; it lands as `[2560, 2, 2]` with identical bytes, which is also the exact shape
  `src/core/dflash.cpp` demands. Test: 5 corner indices per side tensor, all 10 tensors, byte-equal.
- conv dynamic: `((row*2 + side)*kernel + tap)*groups + channel/group` matches `[2*kernel*(H/group), H]`.
- selector: `prev[token*rank + j]` matches the `[vocab, rank]` codebooks; GGUF shape `[rank, vocab]`.
- weights: torch `[out, in]` -> GGUF `[in, out]` (e.g. `q_proj` `[4096,2560]` -> `[2560,4096]`).

## Validation

```
python3 tools/test_dflash2_gguf.py     # legacy suite: 7/7 PASS
python3 tools/test_dflash2_inco.py     # Inco suite: 21/21 PASS
python3 tools/gguf_writer.py           # writer self-check: PASS
```

The Inco suite parses the safetensors header and the GGUF header with its own stdlib readers
(not `tools/gguf_reader.py`), derives names/shapes from the runtime contract, and checks:
all 81 tensor identities byte-for-byte (streaming mmap); conv index semantics above; selector
geometry; 23 metadata keys (geometry, RoPE base, block size, mask token, taps, sliding window,
`target_layer_inputs=false`, `target_hc_mean=true`, `sample_from_anchor=false`, markov/confidence
off, source sha); deterministic projection parity - 960 sampled elements from fc/Q/K/V/selector
rows widened BF16->FP32 from both files, 15/15 rows exact, max abs diff 0.0, fixed-vector dot
products 15/15 exact; and refusals for missing/unexpected tensors, non-BF16 dtype, a swapped 3D
conv shape, bad selector rank/top-k, non-divisible conv group, an `architectures` list naming
anything other than `DFlash2DraftModel`/`DFlashDraftModel` (a plain config with no architectures
key keeps the legacy path, tested through a real converter invocation), nonzero
`attention_dropout`, taps outside `num_target_layers`; the validator itself refuses truncated,
overlapping, unaligned, non-BF16, and bad-magic GGUF files.

`tools/dflash_gguf.py` now also refuses nonzero `attention_dropout`, taps that fall outside a
declared `num_target_layers`, and an `architectures` list naming anything outside
`DFlash2DraftModel`/`DFlashDraftModel` (config-driven; absent keys keep the old behavior).
Re-converting the checkpoint with the updated converter produced a byte-identical artifact
(same sha256).

Projection parity is **sampled artifact/weight parity**: fixed FP32 inputs against sampled
rows/columns of the checkpoint's raw bytes and the artifact's bytes, widened independently on
both sides. It is not an independent runtime numerical forward.

## Limits and open work

### gfx906 HIP build (compiled + CPU-tested 2026-10-20; no GPU execution)

On a gfx906 box, `STRATA_ENABLE_DFLASH2=ON` with `STRATA_HIP_GFX906=ON` now configures and
compiles: the gate (CMakeLists) accepts NVIDIA CUDA and the gfx906 wave64 build; wave32 HIP and
SYCL stay refused. `src/kernels/cuda/dflash2.cu` is compiled as HIP (`--offload-arch=gfx906`)
through `include/strata/platform/hip_compat/` - its only warp construct, the 5-step
`__shfl_xor_sync(0xffffffff, dot, 16..1)` butterfly, becomes the compat layer's width-32
half-wave shuffle (the documented contract); the kernels use no cuBLAS, no WMMA, no packed-byte
SIMD. The wave32 lane arithmetic is therefore the documented half-wave mapping, not a measured
wave64 rewrite. Build facts (2026-10-20):

- configure: `-DSTRATA_HIP_GFX906=ON -DSTRATA_ENABLE_DFLASH2=ON -DCMAKE_HIP_ARCHITECTURES=gfx906`
  with `-DSTRATA_GGML_DIR=/mnt/user/dflash2/deps/llama.cpp` (a copy at pin `3cf03257`), HIP
  compiler `/opt/rocm-6.1.0/llvm/bin/clang++`, host gcc/g++ 14.2; ldd resolves
  libamdhip64/libhipblas/librocblas/librocsolver/librocsparse from `/opt/rocm-6.4.4` and
  libhsa-runtime64 from `/opt/rocm`.
- built: `strata` (39.3 MB), `dflash2_parity`, `dflash_taps_test`, `dflash_head_bf16_test`,
  `dflash_rope_parity`, `dflash_owned_kv_test`, `dflash_load_test`; dflash2.cu compiles with zero
  warnings; full log in `/mnt/user/dflash2/logs/13..16,21`.
- executed: `dflash_load_test` only (its main has no CUDA calls): **1/1 Passed**. The five GPU
  parity tests were NOT run - they allocate device memory and run only with prod-down approval.
- option OFF leaves the target inventory unchanged (825 vs 829; ON-only = the dflash2 objects,
  parity target, its object-order phony).
- host-side fix needed by the parity tool: `hip_compat/strata_hip.h` adds plain-C++ `__half_as_ushort`/
  `__ushort_as_half` bit casts (`!__HIPCC__` only; hipcc keeps hip_fp16.h's own).
- cherry-picked provenance: `10715d85` (upstream gfx906 fixes) → `5ef3000d`; local `70e0d531`
  (int4 shared-array align syntax) → `a5c6272f`.

## Limits and open work

- No engine load test. `dflash_load_test.cpp` fixtures exercise the loader; this exact file has
  not been opened by the engine on any backend yet.
- No generation, server, acceptance-rate, or memory claims. Those measurements belong to the
  CUDA validation lane and the gfx906 HIP port (net-new: the CUDA-only gate must be relaxed and
  `dflash2.cu` compiled through the wave64 compat layer).
- Projection parity samples rows/columns deterministically; it does not claim a full draft forward.
- Parity of GGUF bytes is proven against the checkpoint bytes; quantized (Q8_0 etc.) variants are
  covered by the legacy quantizer-extension test, not by new numbers here.

## L7 bounded SWA ring (2026-10-20, compiled only — tests NOT executed)

Official DFlash2 geometry (window 2048, head_dim 128) now runs the five draft pools as bounded
rings: physical `round64(2048 + 8 - 1) = 2112` cells per pool — exactly 43,253,760 bytes of FP16
(5 pools x 2112 cells x 8 KV heads x 128 dim x 2 (K,V) x 2 B) — fixed before expert-cache sizing,
elastic/VMM disabled for them. `--dflash-window` keeps its docs/DFLASH.md capacity meaning (0
follows `--max-context`), so the 32768 default does not cap official 262K use. Append steps
(`dflash_build_steps`) and the attention's pool reads map `cell % ring`
(`include/strata/core/dflash_ring.hpp`, one CPU model shared by runtime and tests); RoPE
positions, the readable window, and visibility stay absolute. Host horizon guards refuse gaps
and deep rewinds past the retained span; conversation snapshots park the physical ring image
(clamped extent) and restore sets the captured-end horizon.

Compiled evidence (gfx906 HIP, `nice -n19 -j2`, logs 23–26): `strata`, `dflash_ring_test` (new
CPU target), `dflash_load_test` (now accepts `--artifact PATH`), `dflash2_parity` (ring-wrap
cases), `dflash_owned_kv_test` (ring section, gated on the DFlash2 flag) all link; the OFF
config rebuilds clean. NOTHING executed except the pre-existing L5 `dflash_load_test` result:
the parent owns every run. Commits (branch `inco-dflash2-gfx906`, unpushed, on top of
`10180441`): `ca2e0a16` ring helper + CPU test, `f9187f9a` kernel ring mapping,
`f5f82df9` runtime pools/horizon/snapshot clamps, `5b83c26d` + `7ab565c7` test coverage.

## Limits and open work (L7)

- Ring wrap behavior on device is compile-verified only: the parity and owned-KV ring cases
  need the parent's prod-down GPU window.
- The 2112-cell bound assumes the widest block (8 rows = 7 proposals + anchor); geometry with
  a larger trained block needs `dflash_ring_capacity` with that width (the helper takes it).
- Snapshot horizon rejects stale rewinds at propose time; the engine's fresh-prefill path for
  the rejection is exercised by the parent's parked-conversation runs, not by this lane.
