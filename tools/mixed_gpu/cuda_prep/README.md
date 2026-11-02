# cuda_prep — compile-only CUDA/HIP expert-helper preparation (Phase 2)

Standalone assets that build the unmodified root expert kernels (`src/kernels/cuda/iq_kernels.cu`,
`src/kernels/cuda/native_moe.cu`) into CPU, CUDA (sm_86) and HIP (gfx906) libraries behind one small,
versioned C ABI (`strata_prep_v1_*`), plus CPU-only fixture, guard and parity-driver tools.
Nothing here runs on a GPU, loads a backend, touches the engine, a driver, NVML/rocminfo, or a model blob.

## What this is not

- Not engine integration. The engine never links these libraries.
- No GPU execution happened, so: no numerical parity, no coexistence result, no speed claim.
  Compile-only success proves buildability and ABI separation only.
- Synthetic fixtures are ggml-quantized random weights. They are not real model blobs.
- IQ*, tq*, Hadamard v5 layouts and NVFP4 (type 40) are refused with `E_TYPE`, never faked.

## The ABI (v1)

`abi.hpp` is the whole contract: plain C POD structs, `extern "C"`, `abi_version` + `struct_size` +
reserved-zero fields in every request, named status codes (`0` ok, `E_*` refusals), a mutex that
serializes one request, decode-only (`nt == 1`, one token), expert outputs unweighted, weights applied
exactly once by the reused `native_moe_combine` kernel (k ≤ 15, shared added once, unweighted).
Exports: `strata_prep_v1_abi_version`, `_get_info`, `_strerror`, `_supported`, `_expert`, `_combine`
only (hidden visibility + `strata_prep_v1.exports`).

Bounded formats: q4_0, q4_1, q4_K, q5_0, q5_1, q5_K, q8_0 (and q6_K for gate/up only when built with
`STRATA_Q6K_EXPERTS=1`). ggml ids are `include/ggml.h` values: Q4_0=2, Q4_1=3, Q5_0=6, Q5_1=7, Q8_0=8,
Q4_K=12, Q5_K=13, Q6_K=14. Geometry follows the engine (`src/kernels/cpu/native_expert.cpp:95-96`,
`ggml_row_size`, blob = `2*gu_row*n_ff + d_row*n_embd`). The support gate `native_expert_supported`
lives in the GPU translation unit, so a CPU-only library returns `E_BACKEND` truthfully.

The v1 gate in `abi_validate.cpp` is a conjunction, and R1 made the host-side guard state it the way the
kernel states it (no new permission, one more faithful copy): a role table (which types the engine's
`native_quant_act`/`native_gu_rows`/`native_down_rows` accept for gate/up and for down), a geometry table
(the row size each accepted type needs), and the engine's own size condition (`n_embd % 256 == 0` and
`(n_ff * n_embd) % 256 == 0`, mirroring `native_expert_supported`).  A geometry entry is not a role
permission: IQ4_NL and Q2_0 are refused as gate/up types where the engine has no decoder for them, and
IQ2_XXS/IQ2_XS/IQ3_XXS/IQ3_S/IQ2_S are refused as down types, while the ten pairs the real packs use
(IQ2_XXS, IQ2_XS, IQ3_XXS, IQ3_S, IQ2_S with IQ4_NL/Q2_0) are accepted.  Both checks live inside
`derive_layout`, so the manifest parser inherits them; q4/q5/q8/q6 rules and the `E_TYPE` truths are the
existing ones.


## Build (compile-only, explicit backend, max 2 jobs)

```sh
GG=/mnt/user/Strata/build-906-new/_deps/strata_llamacpp-src   # pinned, read-only, HEAD == third_party/ggml/VERSION.txt
cmake -S tools/mixed_gpu/cuda_prep -B build-mixed-gpu/cuda-prep/cpu  -DCMAKE_BUILD_TYPE=Release -DSTRATA_PREP_BACKEND=CPU  -DSTRATA_GGML_DIR=$GG && cmake --build build-mixed-gpu/cuda-prep/cpu  -j2
cmake -S tools/mixed_gpu/cuda_prep -B build-mixed-gpu/cuda-prep/cuda -DCMAKE_BUILD_TYPE=Release -DSTRATA_PREP_BACKEND=CUDA -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc -DCMAKE_CUDA_ARCHITECTURES=86 && cmake --build build-mixed-gpu/cuda-prep/cuda -j2
cmake -S tools/mixed_gpu/cuda_prep -B build-mixed-gpu/cuda-prep/hip  -DCMAKE_BUILD_TYPE=Release -DSTRATA_PREP_BACKEND=HIP  -DCMAKE_HIP_COMPILER=/opt/rocm-6.1.0/llvm/bin/clang++ -DCMAKE_HIP_ARCHITECTURES=gfx906 && cmake --build build-mixed-gpu/cuda-prep/hip -j2
```

`GGML_NATIVE` is forced OFF with explicit `GGML_AVX/AVX2/FMA/F16C/BMI2` ON (the root portable recipe);
no `-march=native`, no CPU autodetection. HIP uses the root compat-header pattern; CUDA native_moe
keeps the root's `--use_fast_math`. ROCm must be 6.1.0 — the machine default `/opt/rocm` has no gfx906.

## Run (CPU-only tools, and only after this proof)

`static_inspect.sh <build-dir>` prints the proof, from `readelf`/`nm` only, and never runs what it inspects:
zero GPU `DT_NEEDED`, zero undefined `cuda*`/`hip*`/`hsa*`/`nvml*`, zero `.nv_fatbin`/`.hip_fatbin` sections
for every CPU tool, and zero GPU symbols in `libggml-cpu.a` and `libstrata_prep_sha256.a`. A CPU binary runs
only after that log shows zeros for it. The GPU libraries are never executed (not even `--help`).

```sh
bash tools/mixed_gpu/cuda_prep/static_inspect.sh build-mixed-gpu/cuda-prep   # readelf/nm only, nothing executed
build-mixed-gpu/cuda-prep/cpu/sha256_selftest                                # FIPS 180-4 vectors, CPU-only
build-mixed-gpu/cuda-prep/cpu/fixture_export  build-mixed-gpu/cuda-prep/fixtures
build-mixed-gpu/cuda-prep/cpu/abi_guard_test  tests/data/native_experts \
    build-mixed-gpu/cuda-prep/fixtures build-mixed-gpu/cuda-prep/fixtures-real-r1
```

`sha256_selftest` runs the published SHA-256 vectors through the same `sha256.cpp` the exporters use. Every
exporter, the guard test and the parity driver call `sha256_selftest()` at startup and refuse to hash anything
if it fails (exit 3).

## Provenance hashes: the defect R1 found, and the fix

`sha256.cpp` is a self-contained FIPS 180-4 SHA-256 so a fixture manifest needs no openssl link in a GPU-free
tool.  The first version of that file was wrong: its round-constant table had `K[17] = 0xefbe4787` where FIPS
has `0xefbe4786` (off by one), and its tail `K[47..63]` was shifted — it dropped the real round constants
`0x106aa070`, `0x391c0cb3`, `0x5b9cca4f` and used `0x1e376c08`, `0x5b96a391`, `0x69596b03` (cube-root
constants of primes that are not in that position) in their place.  The padding, the message schedule, the
round function, the initial values and the output order were all correct, which is why the digest looked
plausible and only failed against `sha256sum`: SHA-256("abc") came out `b9e8abd8…` instead of
`ba7816bf…`, SHA-256("") `73e56d08…` instead of `e3b0c442…`.

Found by: the byte-for-byte real-fixture check (`verify_real_bytes.sh`) reported 48 FAIL, all of them
`sha256(blob.bin) != manifest`, with `cmp` passing every byte — so the bytes were right and the hash was not.
The table was then checked against the published constant list and against
`floor(2^32 * frac(cbrt(p)))` for the first 64 primes computed in exact integer arithmetic
(`p=61 → 0xefbe4786`, `p=227 → 0x19a4c116`, `p=317 → 0xd186b8c7`).

Fixed 2026-10-27: the table is FIPS 180-4 / RFC 6234, `sha256_selftest()` checks 15 published vectors
(whole-buffer and 7-byte streamed updates) and every tool runs it before hashing.  Measured after the fix on
this machine: `sha256_selftest` rc=0 (15/15 PASS); all 152 hashes in the 19 synthetic manifests match
`sha256sum`; `verify_real_bytes.sh` reports 48 fixtures match the model's own bytes, 0 failures (was 0 PASS /
48 FAIL).  The manifests written before the fix are archived in
`build-mixed-gpu/cuda-prep/evidence/r1-pre-fix-manifest-hashes.log`, not silently overwritten.

## Real-blob fixtures (`fixture_export_real`, the S2 exporter)

`fixture_export_real` turns a real model's expert bytes into fixtures for the parity driver.  It reads two
files and nothing else: the pack's own `native_experts.txt` (through the engine's existing parser) and the
model's GGUF.  The weights are the model's own bytes, sliced straight out of the file — gate rows, up rows,
down rows, back to back — and written unchanged.  A real fixture says `synthetic=0` in its manifest, its label
starts with `real_`, and the manifest names the source file, the layer, the expert and the byte offsets.  The
activation input `x` is a seeded pseudo-random test input, and the manifest says so.  Nothing is generated,
re-quantized or guessed.  The tool checks the byte counts three ways (the GGUF tensor's own dimensions x
ggml's row sizes x the sidecar's declared blob size) and refuses when they disagree.  Each fixture carries the
same eight files and the same CPU oracle references (the ggml-cpu decode) as the synthetic fixtures, plus a
`v1-verdict.txt`: the checker's own bounded-set gate and manifest parser, run on the real pair, printed as
they answered.

Measured on 2026-10-27 (this machine): every real pack here quantizes its experts in IQ* and Q2_0 types
(all 96 expert triples of the Swift-1.5 IQ3_XXS pack: 0 inside the bounded v1 set).  A real fixture of those
types is therefore honest evidence, not something v1 `cuda_prep_check` can run — the exporter writes it with
`v1_verdict=REFUSED (…)` and the manifest parser refuses it, exactly as the checker will.  A real pack whose
experts are q4_K/q5_K/q8_0 (an Unsloth UD-Q4_K_XL form, docs/UNSLOTH_Q4.md) is not on this machine; a
consumable real fixture needs such a pack, and that limit is recorded, not hidden.

```sh
build-mixed-gpu/cuda-prep/cpu/fixture_export_real /mnt/user/ai/iq3xxs-15 \
    /mnt/user/ai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf \
    build-mixed-gpu/cuda-prep/fixtures-real L0:0 L1:0 L11:0
```

Evidence: `build-mixed-gpu/cuda-prep/evidence/s2-real-fixture-generate.log` (commands, manifests, verdicts,
the byte-for-byte source spot-check, the coverage scan, the static CPU-only proof, the guard re-run).
GPU runtime stays parent-owned: this tool loads no GPU anything.

## R1 coverage: all 48 layers, all ten real pair classes (`fixtures-real-r1`)

`fixture_export_real` writes a `coverage.txt` beside the fixtures: the source GGUF and sidecar, the fixture
count, the pair-class count, the layer count, the real blob byte range, one line per fixture (class, layer,
expert, byte count, blob SHA-256, the checker's own verdict) and the per-class/per-layer tallies.  The R1 set
was measured on 2026-10-27 on this machine (Linux 7.3.0-x64v3-xanmod1, coreutils `sha256sum` 9.5):

```sh
build-mixed-gpu/cuda-prep/cpu/fixture_export_real /mnt/user/ai/iq3xxs-15 \
    /mnt/user/ai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf \
    build-mixed-gpu/cuda-prep/fixtures-real-r1 L0:0 L1:0 ... L47:0
bash tools/mixed_gpu/cuda_prep/verify_real_bytes.sh build-mixed-gpu/cuda-prep/fixtures-real-r1
```

48 fixtures, 10 pair classes, 48 layers, blob bytes 1305600..2329600, every fixture `v1_verdict=consumable`
by the checker's own `derive_layout` and `manifest_parse=accepted`; every blob byte compared against the same
byte ranges of the same GGUF with `cmp` (48 PASS / 0 FAIL, 152 synthetic manifest hashes matching `sha256sum`).
These fixtures are unexecuted: the CUDA/HIP libraries that would run them were built (compile-only) and
inspected, never executed.  No numerical parity, no speed claim, and no production activation is claimed by
any line of `coverage.txt` — `x` is a seeded pseudo-random test input, the weights are the model's own bytes.

`abi_guard_test <data-dir> <synthetic-fixtures> <real-fixtures>` adds the R1 coverage pass: all ten classes
present, one fixture per production layer, layer ids 0..47 complete, and the real blob byte range equal to the
pack's own 1305600..2329600 (measured, not estimated).

## Scripts in this directory

| script | what it does | runs a GPU-linked binary? |
|---|---|---|
| `verify_sources.sh` | kernel/dependency/toolchain identity before any rebuild (read-only git + compiler versions) | no |
| `static_inspect.sh` | the readelf/nm GPU-freedom proof for the CPU tools | no (inspects, never executes) |
| `abi_reprove.sh` | exports, cross-family references, fatbin sections, build-cache switches (readelf/nm/grep) | no |
| `checker_cli_tests.sh` | `cuda_prep_check` gate/usage/dlopen-failure exit codes, the `--all` zero-fixture refusals and a `LD_DEBUG=libs` no-dlopen capture | only paths that load nothing |
| `verify_real_bytes.sh` | every real fixture's bytes against the same ranges of the same GGUF (`cmp` + `sha256sum`) | no |

`cuda_prep_check` is the parity driver. It is compile-only here and refuses (exit 77) without
`STRATA_PREP_RUNTIME_GATE=1`; `STRATA_PREP_LIB=cuda|hip` picks the single-library mode so exactly one library
is dlopened, and dual mode prints the known CUDA+HIP coexistence-crash warning.  `--all` resolves the fixture
list before the mode is decided and before any library is opened: an absent, non-directory, empty,
manifest-less or unreadable fixtures directory refuses with exit 2 and loads nothing (a zero-fixture run is a
usage refusal, never a PASS).  Running it against the real fixtures is a parent-owned GPU window.

## Validation on 2026-10-26 (candidate 26631e4bbbeb, ggml 3cf03257f219, nvcc 13.1, HIP 6.1.40091)

PASSED: all three configure+build (rc=0); static ABI isolation (both libs export exactly the 6
v1 symbols, CUDA `.so` zero hip-family references, HIP `.so` zero cuda-family references, payloads
`.nv_fatbin` / `.hip_fatbin`); CPU-only proof; `fixture_export` 9 synthetic pairs rc=0;
`abi_guard_test` rc=0 (102 ok / 0 fail: 34 ABI guards, 9 layout cross-checks against ggml-cpu
`native_fmt`, manifest round-trip, 7 malformed-manifest refusals, 6 real-pack parser guards).

NOT RUN (by design): GPU numerics, real-blob parity, coexistence, performance, the runtime
`native_expert_supported` gate, the parity driver. See
`.slim/deepwork/mixed-cuda/cuda-prep-implementation.md` for commands, exit codes and hashes.

## Validation on 2026-10-27 (R1, same candidate 26631e4bbbeb, ggml 3cf03257f219, nvcc 13.1.115, ROCm 6.1.0)

PASSED (CPU evidence, logs in `build-mixed-gpu/cuda-prep/evidence/`):
`r1-source-identity2.log` (identity, toolchains), `r1-cpu-rebuild5.log` (rc=0),
`r1-static-inspect2.log` + `r1-static-inspect2-summary.txt` (0 GPU `DT_NEEDED` / 0 GPU undefined symbols /
0 GPU sections for all five CPU tools), `r1-sha-selftest.log` (15/15 published vectors, rc=0),
`r1-fixture-export.log` (19 synthetic fixtures, rc=0), `r1-real-fixture-export.log` (48 real fixtures,
10 classes, 48 layers, rc=0), `r1-real-byte-check.log` (48 PASS / 0 FAIL vs the model's own bytes),
`r1-guard-test.log` (320 lines, 0 failures, rc=0), `r1-cuda-rebuild.log` and `r1-hip-rebuild.log`
(compile-only, rc=0, `-j2`), `r1-abi-reprove.log` (6 exports each, no leak, 0 cross-family references,
`.nv_fatbin` / `.hip_fatbin`, `GGML_NATIVE=OFF`, explicit `86` / `gfx906`),
`r1-checker-cli.log` (10 gate/usage/dlopen-failure cases, exit codes as specified).
Real defects found and fixed in this pass: the SHA-256 round-constant table above;
`verify_real_bytes.sh` demanding an "optional" second argument.

FAILED: nothing in this pass.  Before the SHA-256 fix the byte-for-byte check reported 48 FAIL for the
manifest hashes (`r1-pre-fix-manifest-hashes.log` keeps that record).

NOT RUN (unchanged, and the reason is authorization, not capability): running the CUDA/HIP libraries (not
even `--help`), numerical parity of any fixture — real or synthetic — on a GPU, coexistence, performance,
the runtime `native_expert_supported` gate.  The full verdict, the exact parent command and the class hashes
are in `.slim/deepwork/mixed-cuda-finish/r1-coverage.md`.

## Validation on 2026-10-27 (R1 remediation + grouped-boundary asset, same candidate)

PASSED (CPU evidence, logs in `build-mixed-gpu/cuda-prep/evidence/`): the `--all` zero-fixture keeper fix
(`cuda_prep_check.cpp`: enumeration resolved before the mode is decided and before any `dlopen`, refusal
with exit 2) rebuilt `r1-cpu-rebuild6.log` (rc=0), GPU-freedom re-proven `r1-static-inspect3.log`,
`r1-checker-cli2.log` **17 cases / 0 fail / 0 skip** (gate 77/77, usage 2/2, `--classes` 0, dlopen-failure 1
paths, the four `--all` refusals on a nonexistent dir / a non-directory / an empty dir / manifest-less
subdirs with the real CUDA library named and no dlopen marker, an `LD_DEBUG=libs` capture where the CUDA
library is never named by the loader, and the good-directory controls), source identity
`r1-source-identity4.log` (ggml HEAD MATCH).  The grouped-boundary asset: `native_grouped_parity` built
compile-only from the unchanged `src/kernels/native_grouped_parity.cpp` and the root target
(`CMakeLists.txt:1149-1152`) in the isolated `build-mixed-gpu/engine-cuda` (CUDA 13.1.115, explicit `sm_86`,
pinned local ggml `3cf03257f219`, `-j2`, rc=0, no FetchContent: `STRATA_GGML_DIR` pinned):
`r1-grouped-parity-build.log` + `r1-grouped-parity-inspect.log` (DT_NEEDED `libcudart.so.13` only,
`.nv_fatbin` present, 0 AMD/HIP references).  **The binary needs a GPU and was never executed here, not
even `--help`.**  The parent ran the real CUDA 48-fixture / 10-class window on 2026-10-27 (rc=0, max raw
rel-L1 1.82e-2, combine 0.00e+00, `r1-parent-cuda-real.log`) and the gate accepted it
(`.slim/deepwork/mixed-cuda-finish/r1-gate.md`).

NOT RUN here (parent-owned): running `native_grouped_parity` (it calls `cudaMalloc` on the first line), any
GPU numerics, the real-blob window re-run.

## P0 runner status (2026-10-27, split-topology gate)

The one-shot generate-mode runner `tools/mixed_gpu/measure_headroom.py` first held is **RETIRED AND
INVALID** for production evidence: generate mode refuses `--layer-split` outside `--serve` (measured rc=2,
`build-mixed-gpu/p0/logs/incident-4k-2026-10-27T07:45*.log`), so it stripped the production split — a
single-process path is not production-representative. The file now holds the accepted **split-SERVE
runner**: plan default; `--execute --url http://127.0.0.1:8019 --cell … --runs 1..3` against the isolated
config `build-mixed-gpu/p0/config-split-timing.json` (production config + only port 8019, log path, and
`STRATA_DECODE_TIMING=1 STRATA_VERIFY_PROFILE=1 STRATA_LOOKAHEAD_STATS=1`), the incumbent binary with its
own serve counters, `tools.strata_bench.chat/digest` requests reconstructed from the cells, `/health` +
`/props` + identity + digest gates, port 8009 refused, lifecycle parent-only. `--selftest` (12/12 PASS,
`build-mixed-gpu/p0/p0-selftest.log`) is the CPU mock proof. Details:
`.slim/deepwork/mixed-cuda-finish/p0-split-ready.md`.
