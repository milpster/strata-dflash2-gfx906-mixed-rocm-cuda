# DFlash2 for the official Inco checkpoint (Qwen3.8-Flash-Next-DFlash2)

Status (2026-10-21): the gfx906 HIP port is COMPILED (prod-matched environment, logs 31/32) and
the CPU-safe suites have PASSED under parent execution at `69a55085` (all 8 CPU ctests incl.
`dflash_tap_plan_test`, `dflash_ring_test`, loader + Python ring/stage parity fixtures;
`/mnt/user/dflash2/logs/parent-l8-cpu-tests.log`). The slot-test binding-order fix `9c0223cd`
is compiled (try8/try9) and awaits the parent rerun. **All GPU execution — including "synthetic"
tests — requires the prod-down window with explicit user approval; nothing GPU has run in these
lanes.** No runtime (engine load, generation, server, acceptance, memory) is claimed anywhere
below. Production (`/mnt/user/Strata-2966` MTP serve) is untouched by every lane.

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
follows `--max-context`; unset follows `--max-context` for this geometry, so the 32768 default
applies only to legacy drafters and does not cap official 262K use; explicit caps are honored). Append steps
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

## L8 cross-GPU tap collection (2026-10-21, compiled only — parent executes everything)

Under `--serve --layer-split 32 --gpu 0,1` the five target taps are captured by the stage whose
layer range contains each tap's boundary (split32: GPU0 captures boundaries 6/16/26, the drafter's
GPU1 captures 36/46) and assembled on the drafter's card by the bounded collector
(`include/strata/core/dflash_tap_plan.hpp`, `dflash_tap_collect.hpp`): compact rows-only D2H into
two pinned slots per kind keyed by chunk/window pos0, drafter-stream H2D/D2D, event-drained reuse,
mutex-serialized bookkeeping (the prefill chain genuinely overlaps stage N+1's producer with
stage N's assembly — prefill.cpp:4948-4963), fail-closed refusals throughout.
**Shared head, no extra BF16 copy:** the drafter binds the target's FINAL-STAGE head
(`last_st->wt`/`last_st->head`, the MTP pattern) — it already lives on the drafter's card. The
cross-card fallback (head on a foreign card) copies the encoded matrix verbatim through pinned-host
staging; it stays dormant in the split layout (IQ-form pinned gather → no embedding copy either).
Ring storage is the DESIGN bound `43,253,760 B` (5 pools × 2112 cells × 8 heads × 128 × 2 × 2 B) —
a sizing bound fixed before expert-cache auto, NOT a measured VRAM figure. Serial-only:
`--batch` stays refused; `--pipeline-windows` auto-refuses dflash+split. Sampled requests fall back
to target-only, explicitly logged. Commits (unpushed): `6d7d3600`→`88dc44a6`→`3602444a`→
`2f31bf7b`→`65bb8c93`→`69a55085`→`0b5ceadc`→`0096a5b4`→`b1493619`→`9c0223cd`. Build evidence:
log 31 `SCRIPT_EXIT7=0`, log 32 `SCRIPT_EXIT8=0` (zero errors; prod-matched wrapper, nice19 -j2).
Full report: `/mnt/user/dflash2/L8-MULTIGPU-REPORT.md`; parent commands: `PARENT-TEST-COMMANDS.md`.

## L9 harnesses (2026-10-21, CPU plan/report only — parent runs all tests)

- `tools/inco_safetensors.py` — safetensors is the AUTHORITY: lazy per-tensor reader
  (safetensors pkg + stdlib header/memmap fallback, no Torch/ROCm), canonical-name mapping onto
  the ONE existing stage oracle (`dflash2_stage_parity.py::check_weights`, now source-agnostic —
  GGUF and safetensors feed the same formulas), config→metadata derivation, shape refusals,
  recorded sha256 authority.
 - `tools/test_inco_dflash2_parity.py` (fixed at 9379c3d5 after parent FAIL at e8edb643) -
   `--selfcheck` CPU fixtures: RoPE compared at the ENGINE's explicitly-rounded FP32 phases
   (theta_scale=powf(f32,-2f/hd); inv=powf(scale,pair); ang=f32(pos)*inv; cosf/sinf, per
   native_rope.cu:222-251) at positions 0/2047/2048/2049/65536/200000/260000, D=128, tolerance
   UNCHANGED 1e-5; FP64-ideal drift printed as diagnostic only; checkpoint/kernel convention
   cross-check UNRESOLVED pending a parent GPU reference; HC-mean SYNTHETIC 4-stream fixture at
   tap layers 5/15/45 (labeled, not target truth); GQA mini 8/2 = the real 32/8 ratio; per-K
   symmetric non-causal window 2048 with explicit per-row absolute visibility sets and block
   cutoff; conv per-(row,channel) scalar tap sums on BOTH sides with distinct coefficients
   (row 0 = current-tap coeff x0, not zero); selector walk with nonzero codebooks, anchor row
   excluded, predecessor dependence proven, ties resolve to the smaller TOKEN ID, exact-path
   compare, vocab bounds; exact rmsnorm/fusion fixtures vs FP64-scalar references; refusals on
   real public helpers only; real checkpoint tensors exercised;
  `--integrity [--hash]`; `--dump-dir DIR` compares REAL engine intermediates (STRATA_DF_PARITY)
  per-row/per-layer abs/rel/cosine at the existing gates (cos≥0.9999, relL2≤0.02 — unchanged,
  not relaxed). Truth: NO head tensors exist in the safetensors (tie_word_embeddings false) →
  head parity NOTRUN → `dflash_head_parity.py` against the target GGUF head; target-side HC-mean
  truth needs an F32 target reference run → NOTRUN. CPU output proves formulas/artifact integrity
  ONLY — never HIP numerics, never an independent SGLang/target claim.
 - `tools/test_inco_dflash2_integration.py` (fixed at 9379c3d5) - clones the REAL production
   serve config `/mnt/user/Strata/prod-config-swift15.json` READ-ONLY: all structured keys
   preserved (layer_split=32 -> --layer-split via server.py:2604), exe swapped, port moved to
   8019; writes valid server-schema configs `proto-server-swift15-{target_only,dflash_w3}.json`
   + `inco-dflash2-plan-manifest.json` under `/mnt/user/dflash2/config/` (prod config never
   touched); structural flag merge (exactly one --spec=4, one --spec-min-p=0.5, --dflash +
   --dflash-block 3). prod_reference labeled SUFFIX LOOKUP (suffix_draft DEFAULT 3, generate.cpp:778;
   runtime logs 'suffix drafts: N windows, X of Y accepted' - MTP is OFF, target-only is NOT;
new_mtp READY-RT (config valid; runtime NOTRUN) - loader input
   /home/user/dev/Strata-data/mtp-bf16/rt/ (dense.txt 29 index lines, dense.bin ~116 MB,
   experts.bin ~675 MiB, draft_vocab.bin ~415 KiB; tools/mtp_rt.py contract) is consumed directly by
   MtpDrafter (--mtp <rt> --spec 6 --spec-min-p 0.7 --suffix-draft 0, no copies); cheap metadata
   preflight only; ACTUAL GPU load + Swift acceptance UNVALIDATED - never 'pack validated'; the
   lama-format GGUFs under ai/ai are NOT Strata-loadable
   refuse; port 8009 refused outright; --confirm-execution required; NO override flag exists;
   --execute performs real HTTP checks (health, props identity, greedy repeat/reset,
   sampled-fallback) ONLY against the isolated prototype on 8019 started by the parent; nothing
   stops/restarts/probes production. Source-cited flag schema kept: drafter exclusive 5440;
   width=min(--dflash-block,--spec-1) 5500; serve --batch refused 2836; pipeline+split+dflash
   refused 4214; sampled->target-only 2830/12457.
 - `tools/bench_inco_dflash2.py` (fixed at 9379c3d5) - plan/report + REAL grouped measurement:
   modes prod_reference (SUFFIX LOOKUP)/new_suffix_lookup/new_target_only (pure AR)/
   old_target_only (old binary pure AR)/new_dflash w1..7 (suffix off)/new_mtp
   (READY-RT - config valid via real loader dir, runtime NOTRUN - never 'pack validated', no fake baseline); BF16 primary - no quantized-drafter claims
   until conversion+parity validated; contexts 2K/16K/64K/200K(+260K); engine-reported token
   counts (never char==token); per-run JSONs per
   (mode,context,width,prompt,seed,repeat); --report medians/spread GROUPED per configuration,
   NEVER one aggregate number; unexposed metrics null WITH REASON; non-finite/bool refused;
   failed runs recorded and excluded. Greedy byte-compare gate pins caller-supplied EQUAL
   explicit `--expert-cache N` slots (int slots, generate.cpp:2536; auto is not a pin) +
   `STRATA_IQ_MT_MIN=1`, old-vs-new target-only FIRST; benchmark mode keeps the production
   `--expert-cache auto` deployment unchanged. Full hashing gated behind `--offline-guarded`
   (prod-up 76 GB reads thrash the page cache, #1691); default `--preflight` is a size/mtime
   fingerprint vs the durable L3/L4 shas. No GPU in plan mode; execution is parent-owned behind
   the fail-closed stopped-prod guard.

### Prototype activation config (NOT deployed)

`/mnt/user/dflash2/config/proto-server-swift15-dflash_w3.json` (written by the integration harness):
the production serve invocation UNCHANGED (split32, IQ3_XXS native quant, pack, KV int8, 262144
context, expert-cache auto, ROCm env prefix) plus `--dflash /mnt/user/dflash2/artifacts/inco-dflash2-bf16.gguf --spec 4 --dflash-block 3 --spec-min-p 0.5`, run from the prototype binary
`/mnt/user/dflash2/Strata-wt/build-gfx906-dflash2/strata`. Exact revert = start production
exactly as `launch-prod.sh` / the live cmdline (no `--dflash`): production flags carry no
DFlash2 state, so reversion is dropping the prototype binary/flags — no migration to undo.
No automatic stop/restart is performed by anything in this program; every switch is an explicit
parent/user action.

### Ready-for-downtime checklist (parent, in order; requires explicit user approval to start)

1. Greedy A/B baseline: old-binary vs new-binary target-only, byte-compare, `STRATA_IQ_MT_MIN=1`,
   identical `--expert-cache auto` (gate before any DFlash diagnosis).
2. CPU rerun (prod-up, no GPU): `ctest -R '^(dflash_tap_plan_test|dflash_tap_slot_test|dflash_ring_test|dflash_load_test)$'`
   + `python3 tools/test_dflash2_stage_parity.py` + `python3 tools/test_inco_dflash2_parity.py --selfcheck --integrity`  # (--hash only in an offline window)
   + `--selfcheck` of the integration/bench harnesses.
3. GPU primitives (prod env prefix): `dflash_tap_plan/slot` already CPU; `dflash_tap_collect_test`,
   `dflash2_parity` dump generation → `test_inco_dflash2_parity.py --dump-dir`, `dflash_taps_test`,
   `dflash_rope_parity`, `dflash_head_bf16_test`, `dflash_owned_kv_test` ring cases.
4. Engine split run: prototype serve `--dflash --layer-split 32`, width 3; verify the startup line
   `dflash: cross-GPU tap collection serial on CUDA1: boundary 6<-CUDA0 ... (HC stream mean capture)`.
5. Server cases: repeat-conversation reset, cancel/parked-restore, deep rewind >200K, EOS/stop,
   sampled→target-only explicitness (`tools/dflash_sampling.py`, `tools/dflash_cache.py`).
6. Long-context + bench order per `bench_inco_dflash2.py --plan`; record per-run JSONs; report via
   `--report DIR`. No perf numbers exist until measured.

## Risk ledger (all 28, status at L9 close — no invented closures)

| # | Risk | Status |
|---|------|--------|
| 1 | Element layout (no transposes) | PASS (L4 byte-exact suite 21/21) |
| 2 | Converter determinism / provenance sha | PASS (re-convert byte-identical, sha in GGUF) |
| 3 | gfx906 HIP compile of dflash2.cu | PASS (logs 13–16, 21, 31, 32, 33) |
| 4 | Wave32 half-wave mapping of the shfl butterfly | NOTRUN (measured semantics need GPU window) |
| 5 | Engine load of the artifact | NOTRUN (loader CPU test 1/1 PASS; engine open NOTRUN) |
| 6 | Ring math model (2112 cells, wrap, retained span) | PASS CPU (`dflash_ring_test`, fixtures) |
| 7 | Ring on-device wrap/pool reads | NOTRUN (owned-KV/parity ring cases, prod-down) |
| 8 | Ring storage bound 43,253,760 B | DESIGN bound (not measured VRAM) |
| 9 | Snapshot park/restore of ring image | NOTRUN (engine cases, prod-down) |
| 10 | Tap ownership model (split boundaries) | PASS CPU (`dflash_tap_plan_test` @69a55085) |
| 11 | Collector slot lifecycle (bind/evict/reset) | PASS CPU @69a55085; fix `9c0223cd` rerun PENDING |
| 12 | Prefill producer/consumer race (real, evidence prefill.cpp:4948-4963) | serialization COMPILED; GPU overlap test NOTRUN |
| 13 | Cross-device transport (D2H/H2D, events, pinned) | NOTRUN (2-GPU collect test, prod-down) |
| 14 | Shared final-stage head (no 1.27 GB duplication) | COMPILED; NOTRUN (engine startup line) |
| 15 | Cross-card fallback (host-staged, rebind-safe) | COMPILED; dormant in split; NOTRUN |
| 16 | Embedding gather on drafter card (IQ pinned path) | COMPILED; NOTRUN |
| 17 | Debug export composition (no foreign add_context) | COMPILED; NOTRUN (parity export run) |
| 18 | Memory sizing (reachable rows, foreign-only pins) | COMPILED; startup print NOTRUN |
| 19 | Gates: batch refused, pipeline+split+dflash refused, sampled→target-only | PASS by source/schema; engine lines NOTRUN |
| 20 | Serve/CLI consumer split (per-stage vs full-plane) | COMPILED (`65bb8c93`); NOTRUN |
| 21 | Stage-parity fixtures (union span 197896/2112) | PASS (Python fixtures @69a55085) |
| 22 | Prototype-vs-prod base divergence (`497e4be4` vs `27b65cc8`+stack) | OPEN (greedy A/B gate, prod-down) |
| 23 | Numerical parity checkpoint→engine (formulas) | CPU fixtures compiled; --dump-dir NOTRUN |
| 24 | Head truth (no head tensors in checkpoint) | NOTRUN by design (target-GGUF head parity tool ready) |
| 25 | Target-side HC-mean truth | NOTRUN (needs F32 target reference run) |
| 26 | Quantized drafter variants | NOT VALIDATED (BF16 primary; conversion+parity required first) |
| 27 | Host RAM for pinned staging (251.7 MB @8192 → 1.007 GB @32768, 3 foreign taps) | priced in diagnostics; NOTRUN |
| 28 | Bench baselines (prod_reference SUFFIX LOOKUP (observed source+logs) / new_suffix_lookup / new_target_only + old_target_only (pure AR) / new_mtp READY-RT runtime NOTRUN / new_dflash w1..7 suffix-off) | PLAN ONLY; zero measured numbers |

All GPU items require the prod-down window with explicit user approval (2026-10-21 safety rule);
no script in this program stops, restarts, or probes production.
