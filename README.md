# Strata — gfx906 Mixed-GPU Fork

An engine fork of [Niko1221/Strata](https://github.com/Niko1221/Strata) that runs **Qwen3.8-Flash-Next ("Swift 1.5")** on **2× Radeon VII (gfx906) + an NVIDIA helper card** — hardware stock only compile-checks — with a **262,144-token context** and a disk conversation cache that survives restarts.

Serving line: upstream **v0.2.11** merged into the fork's `main`. A port onto upstream **v0.2.18** (engine 0.1.64) is staged on branch `port-0218`: it builds and its suites pass device-free, but **it has not run** — see [port status](#port-status-0218-staged-not-run).

> Agent-first: pinned toolchains, custom libs, harness scripts, measure-before-promote. Records: `journal/pool-admission-fix-2026-11-21.md` (the 2026-11-21/22 window), `CHAMPION.md` (serving tuple), `JOURNAL.md`, `bench/ab-v0210/out/RESULTS*.md`. Every number names what it was measured on; numbers from reading code say *static analysis*.

**Contents:** [Latest](#latest--2026-11-22) · [At a glance](#at-a-glance) · [Who it's for](#who-its-for) · [The rig](#the-rig) · [How the fork works](#how-the-fork-works) · [Measured vs stock](#measured-vs-stock) · [Quality gates](#quality-gates) · [Trade-offs](#trade-offs-and-known-defects) · [Build guide](#build-guide) · [Repo map](#repo-map) · [Support Strata](#support-strata)

## Latest — 2026-11-22

The **240k no-output bug is closed**, the prod tuple was **re-measured at 144/144 reserves**, and the **0.2.18 port** reached a merged decode loop (built and tested, not booted).

### The 240k no-output bug, closed

| | |
| --- | --- |
| Symptom | 239,663-token prompt read cleanly, then `non-finite logits (#879)` at the first decode step: 0 tokens, 4 of 4 passes |
| Cause | `STRATA_FS_SLOTS="2,8"` is read as a **per-card** list; the old fork parse (`atoi`) gave 2 slots to every card. The thin CUDA card got 8 slots — ~293 MiB allocated after the expert-cache fill, outside every reserve — and loaded with **140 MiB free**, not 360. Under ~150 MiB free its first stage-2 GEMM emits scattered non-finite outputs |
| Fix 1, config | `STRATA_FS_SLOTS=2`, margins untouched → **239,663 tokens in 814.6 s (294.2 tok/s)**, 0 non-finite |
| Fix 2, code | `564e1dbbea` clamps `card_slots[c]` in `ForesightSwap::init` against `cudaMemGetInfo` so the `cudaMalloc` leaves `vram_guard::kLowMib` free; engine `4b8251e9` → **239,663 tokens in 834.8 s (287.1 tok/s)**, 0 non-finite |
| Source | journal §"Root cause found and confirmed by measurement", §"239,663 tokens on the clamped engine"; `CHAMPION.md` |

One cause, two symptoms: the same starvation stalled a 240k request at `--layer-split 31` (156 MiB free, full-card stall `#2065`). VRAM margin was ruled out first (284 → 730 MiB free, same failure), then the prompt path (`0 reused`, batched read, same failure).

### Current prod tuple

Serving on `127.0.0.1:8009`, launcher only, config `prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed-fson.json`.

| Part | Value | Source |
| --- | --- | --- |
| Engine | `run/fsclamp-bin/rpcw-hip/strata`, sha `4b8251e9` (clamp `564e1dbbea`) | journal |
| Helper | `run/fsclamp-bin/rpcw-cuda/strata-expert-helper-patient`, sha `9d16742d`, reserve 0, no fill cap — **3,913 slots / 7.32 GiB**, **14 MiB** free | journal |
| Reserves | `--vram-reserve-mib 144` / `--vram-reserve-later-mib 144` → card0 288, card1 **386 MiB** free loaded | journal |
| Foresight swap | `STRATA_FS_SLOTS=2,8`; clamp grants **0 slots → swap off** at these margins | journal |
| RAM pool | `STRATA_RAM_BUDGET_MIB=40960` (36 GiB pool + 4 GiB outside area; raised from 36864 on 2026-11-22 by operator order) | config |
| Context, KV, drafter | `--max-context 262144`, `--kv int8`, `--kv-resident-mib 4096`, `--prefill 16384`; `--dflash …/inco-dflash2-q4_0.gguf --dflash-block 7 --spec 5` | config |
| Speed | prefill **284.4–290.8 tok/s** (239,646–239,663-token prompt); decode **20.2–32.2 tok/s** (cold infill 20.2–22.8, restored turns 30.2–32.2) | journal, `fson-rsv600` / `fson-rsv344-240k` |

Margin ladder, each step measured before its boot: 256/192 → 600/600 (swap always on) → 344/344 → **144/144**.

`CHAMPION.md`'s "what is serving" block predates this promotion (last edited 2026-11-21); the journal is later, and this table follows it.

### Conversation cache: park and restore

| Leg | Measured | Source |
| --- | --- | --- |
| Cold read, same prompt | 239,646 tokens in 842,742 ms (**284.4 tok/s**) | `fsclamp-champion` |
| Same-boot reuse | 240,158 tokens reused + 18 read in **393 ms** | same |
| Cross-boot restore | **239,641 tokens from disk in 32,681 ms** (checkpoint mount) | `…-xboot` |
| Park write | 240,158 tokens → `conv-1.sess`, 3,854.8 MiB in 4,884 ms (~810 MB/s, direct I/O) | `fsclamp-champion` |
| Disk tier | 24,576 MiB budget; 11,603 MiB after 3 generations | `…-xboot` |
| Live prod | 2,279 tokens restored in 185.9 ms; a 192,840-token conversation restored from the previous boot's cache in 31.4 s | journal §"Park/restore verified after the restart" |

A 4,097 MiB snapshot is over the 512 MiB `--conversation-cache-ram-mib` ceiling, so a 240k conversation parks **disk-only** by design: ~810 MB/s out, ~118 MiB/s back.

### Port status: 0.2.18 staged, not run

Branch `port-0218` (worktree `/mnt/srcds/Strata-wt-0218`), upstream tag `v0.2.18`, engine 0.1.64.

| Item | State | Evidence |
| --- | --- | --- |
| Fork-only files carried | **140**: 57 DeepSeek V4.1, 27 other headers, 32 `src/core/`+`src/prefill/`, 4 kernels, 4 spec/platform, 10 server, 6 `sycl/` | `PORT-MANIFEST.txt` |
| Plus | 3 helper sidecar sources, 49 lane test programs, 5 test-support files, 6 DSV41 extras | `PORT-MANIFEST-*` |
| Decode loop | `generate.cpp` + `prefill.cpp` merged by real 3-way (`git merge-file --diff3`, base `54828c7efb`), 41 + 16 conflicts. The `patch(1)` attempt was **discarded**: it deleted upstream's `BSlot` members, 5 includes, the park-pool block, the SSD tier, `STRATA_FOLLOW` | `PORT-NOTES.md` |
| Lane suite, device-free | **47 cases, 100 % passed, 0 failed, 5 skipped** (exit 77); default pair 43/43 | port verification table |
| Builds, server | engine `ldd` = the expected trio; helper sha `58c3ae02`, 55,541,552 B, **38 cubins all sm_86**, `--help` exit 0; server tests 94 passed / 6 failed (the same 6 fail on clean upstream) | port tables |
| Runtime | **not validated** — no boot, no prompt, no GPU work. Merged loop, SSD+DISK tiers together, upstream `--batch`/GEN hold/checkpoint pool, the lane's `--dflash` path: unproven | `PORT-NOTES.md` |
| Next | boot-and-compare against the fork's build, quality gate = `tools/quant_quality.py`; tests on `:8009` one at a time, `:8019` retired | journal |

A `strata` hash here is **not** a content hash: v0.2.18 stamps `git rev-parse --short=9 HEAD` at configure time. Quote (commit, sha256).

## At a glance

| Claim | Number | Measured on |
| --- | --- | --- |
| Runs where stock only compile-checks | gfx906 HIP backend, daily server; stock v0.2.10 as tagged does not compile there (three compile-only fixes, branch `stock-v0210-gfx906-buildfix`) | `JOURNAL.md` |
| Decode vs stock, same night | **~35.1 vs 23.2 tok/s** (494-token shape, +51 %); the v0.2.10 build held 37.9/36.9/40.1 vs 23.3/23.4/23.0 (+60–74 %) | v0.2.11 vs stock v0.2.10, exclusive box |
| 240k prompts answer | 239,663 tokens at 287–294 tok/s, 0 non-finite | 2026-11-21 |
| Restarts are cheap | 239,641 tokens restored in **32.7 s**; 240,158 in **393 ms** same-boot | 2026-11-22 |
| Agent traffic | **138 requests on one boot**, largest prompt 100,291 tokens, `timeouts=0 fallbacks=0` on all 138, 0 refusals, 0 restarts | engine `a506e620` |
| RAM handed back | 4,116/4,116 pairs dropped (**6.24 GiB**); engine anon 42.1 vs 47.4 GiB | `RESULTS-relief-fix.md` |
| Quality parity | 4,995 positions: top-1 100.00 %, KL 0.00000, PPL 4.052 = 4.052 | v0.2.11 vs stock, iq3xxs-15 |

## Who it's for

| You have | What this fork gives you |
| --- | --- |
| **gfx906 cards** (Radeon VII; MI50/MI60 share the target, untested here) | a production local-LLM server, not a compile check |
| **Mixed AMD + NVIDIA** | stock has no cross-vendor path; here the NVIDIA card serves expert and prompt rows over two AF_UNIX channels |
| **Big MoE, small cards** | a ~40 GiB expert set from 2× 16 GiB VRAM + 62 GiB host RAM |
| **Long sessions** | 262,144-token context; 240k prompts answer; a parked conversation returns from disk after a restart |
| **RAM-tight hosts / agent traffic** | 6.24 GiB handed back; OpenAI- and Anthropic-compatible API with a 138-request boot and no refusal |

## The rig

| Part | What | Role |
| --- | --- | --- |
| CPU | Ryzen 9 5900HX, 8C/16T | host staging, yardstick |
| RAM | 62 GiB | 32 GiB managed pool + 4 GiB outside area |
| GPU 0+1 | 2× Radeon VII (gfx906, 16 GiB) | **the HIP engine**: all execution, split `0-29 / 30-47`, DFlash2 drafter |
| GPU 2 | RTX 3080 Laptop, 8 GiB (sm_86) | **the CUDA helper**: 3,913 slots, 7.32 GiB, 14 MiB left free |
| Storage | 1 TB NVMe + 2 TB SATA | pack, 24 GiB disk conversation tier |
| OS | Ubuntu 26.04.1 LTS, XanMod `7.3.4-x64v3` | userland the custom rocBLAS targets |
| rocBLAS | custom **4.1.0**, `gfx906:xnack-` (`~/rocm-gfx906-xnack`) | GEMM for both VIIs; stock 6.1/6.4 segfaults on gfx906 |
| Model | Swift-1.5 IQ3_XXS UD / IQ3_S | ~40–47 GiB expert set |

**ROCm first, CUDA second.** The engine maps no CUDA runtime; the helper card only serves rows on request. The pack's Jinja chat template is rendered exactly like HF `apply_chat_template`.

## How the fork works

All pieces are opt-in or env-gated; each row says what is actually measured.

| # | Piece | What it does, and what is measured |
| --- | --- | --- |
| 1 | **gfx906 HIP backend** | full HIP wave64 port, compat shims (cublas→hipBLAS, `__ldg`, int4 align), tuned knobs, Blackwell-only paths off. *The serving path.* |
| 2 | **Out-of-process CUDA expert helper** | owns the NVIDIA card; two AF_UNIX channels — `--socket` for the decode/verify **window** tier + drafter lane, `--socket2` for the **prompt** tier — so neither queue waits on the other. Same frames, same handshake, `kFill` on one channel; each channel fails alone. Prod wires both. *Fill and split measured; two-channel gain **not** measured.* |
| 3 | **Host RAM pool + relief** | `ram_pool`, `pool_expert_cache`, `pool_expert_store`, `pool_expert_source_adapter`: reference-counted pair leases, LRU eviction, per-card budgets; `--expert-helper-ram-relief` drops helper-held pairs. *4,116/4,116 pairs = 6.24 GiB; adapter 237 checks / 0 failures; pool ctest 6/6.* |
| 4 | **Pool-safe CPU share** | admits a blob when it is `pinned` and not `transient`; upstream accepts pinned **or** non-transient, which sends managed-pool blobs down the DMA-from-borrow path where the loan is never noted or released. *Load-bearing; the merge keeps the fork's rule.* |
| 5 | **Foresight expert swap + clamp** | `STRATA_FS_SLOTS` is a **per-card** list (`2,8`); `564e1dbbea` clamps each card so the `cudaMalloc` leaves 512 MiB free, and all-zero means swap off. *Clamp measured at 3k and 240k; the swap served **0 experts, 0 copies** in every arm (600/600: 290.8 vs 288.8 tok/s).* |
| 6 | **Fork DFlash2 drafter (`dflash_inco`)** | HIP-side drafter (stock's is CUDA-only), q4_0, `--spec 5`; sampled-request verification, bounded SWA draft ring, draft parking, batched KV append. *Arm E, drafter off = stock speed (23.8 vs 23.6 tok/s): the decode win is the drafter.* |
| 7 | **Conversation-cache DISK tier** | split-aware `conv-NNN.sess` + paired `ring-N.dring`, direct I/O, prompt checkpoints, park-gain gate, idle-save, disk-only park above the RAM ceiling. *Park ~810 MB/s, same-boot 393 ms, **cross-boot 32.7 s**.* |
| 8 | **`vram_guard`** | one definition of the `#2065` low line (512 MiB; 1,024 on WDDM); under it a card's reserve grows stepwise (64 MiB default) to a 4,096 MiB cap. *Opt-in `STRATA_VRAM_GUARD=1`, inert otherwise; static analysis.* |
| 9 | **Native i-quant kernels** | native-expert quantised GEMMs; the fork adds `native_expert_grouped_q8k` on upstream's `native_expert_i8_*`. *Static analysis.* |
| 10 | **DeepSeek V4.1** | 57 manifest files behind `STRATA_BUILD_DSV41` (OFF) and `NOT STRATA_HIP_GFX906`: it needs EXL3 decode kernels gfx906 does not get. *Opt-in, **never built on this rig**.* |
| 11 | **Bench + quality discipline** | `bench/ab-v0210/` harness, `knob-leg.sh` (one boot→leg→teardown), `tools/quant_quality.py`. *Used for every number here.* |

## Measured vs stock

Same-night, exclusive-box A/Bs from the v0.2.10/v0.2.11 line, **before** the 2026-11-21 pool work. **No same-day stock comparison exists for the current 2026-11-22 tuple**: these are the last measured margin over stock, not a claim about today's binary.

| Window | Metric | Fork | Stock | Delta |
| --- | --- | ---: | ---: | ---: |
| v0.2.11 (`JOURNAL.md` 2026-11-06) | TG 494-token, median tok/s | ~35.1 | 23.2 | **+51 %** |
| v0.2.11 | 1024-token completion wall | 30.5–36.4 s | 33.3 s | parity |
| v0.2.11 | Fresh 16k prefill, tok/s | 299 | 338 | −12 % |
| v0.2.11 | 88k read, tok/s | 472 | 549 | −14 % |
| v0.2.11 | Greedy 80k output sha | `1c7d4a6d9be118d8` | identical | ✓ |
| v0.2.10 (`JOURNAL.md`) | TG 494 / 513-token; 1024 wall | 37.9/36.9/40.1; 35.8/36.0; 32.5–33.4 s | 23.3/23.4/23.0; 23.9; 43.0–47.7 s | +60–74 %; +50 %; 1.34× |
| v0.2.10 sync (`RESULTS.md` 2026-11-03) | TG cached 1024, tok/s | 33.1 | 23.6 | +40 % |
| relief fix (`RESULTS-relief-fix.md`) | TG median, tok/s | 37.65 | 27.6 | +36 % |
| relief fix | 88k read, tok/s | 552.9 | 550.0 | parity |
| relief fix | engine anonymous RAM | 42.1 GiB | 46.2 GiB stock / 47.4 GiB control | −5.3 GiB |

The v0.2.11 upmerge cost ~7 % on the 494 shape against the v0.2.10 build of the same tree (`JOURNAL.md`).

Attribution (2026-11-03): the decode win is **entirely** the drafter (arm E, drafter off, prompt tier on: 23.8 = stock 23.6). The fresh-read giveback is tap collection (arm E recovers it: 337.6 vs 337.2); on the 80k read, tap ≈ 2/3 and the prompt tier ≈ 1/3 of the gap, which the relief fix later closed (552.9 vs 550.0).

## Quality gates

Promotion needs a speed lane **and** a quality lane.

| Gate | What it checks | Result |
| --- | --- | --- |
| Greedy 80k sha | deep infill byte-identical across arms | `1c7d4a6d9be118d8` across 7 runs / 5 arms incl. stock (2026-11-03); identical on v0.2.11, v0.2.10, stock |
| Teacher-forced yardstick | 4,995 positions: top-1 / KL(p99) / PPL vs stock | **100.00 % · 0.00000 · 0.0000 · PPL 4.052 = 4.052** (v0.2.11, iq3xxs-15; historical iq3s 4.115 = 4.115) |
| Yardstick, knob-gated | `STRATA_MMVF_ROWS=1` vs same-night baseline | KL 0.00000 · p99 0.0000 · top-1 100 % · PPL 3.924 = 3.924 (2026-11-11) |
| Prompt-mode first logits | 20 prompts, fork vs stock | top-1 100.00 %, KL mean/median/max 0.00000 (`run/qgate-20261107.sh`) |
| ctest, fork tree | 291 tests (v0.2.11) | all pass; `gemm_w16_parity` exits 77 below ~8.2 GiB free VRAM |
| ctest, 0.2.18 port | lane + default pairs, devices hidden; `terminal/serve` | 47/47 and 43/43 (5 and 2 device skips); server 94/100, the same 6 fail on clean upstream |
| Pool / RAM suites | adapter + pool ctest | 237 checks / 0 failures; 6/6 (needs `-DSTRATA_BUILD_RAM_POOL_TEST=ON`) |

Re-run: `bench/ab-v0210/run-arm.sh`, `tools/quant_quality.py run|compare`, `ctest`. **Not discharged for 0.2.18** — no boot yet.

## Trade-offs and known defects

| Item | Cost | Status |
| --- | --- | --- |
| Fresh reads | **−12 to −19 %** prefill vs stock on 16k–88k (serial tap collection) | measured; 88k at parity after the relief fix |
| Foresight swap | 293 MiB on card1; **never** served an expert in any measured arm | on at 600/600 for quality; 144/144 clamps it off (386 < 512 low line); `~600/600` re-enables the full `2,8` (548 MiB free), 344/344 leaves card1 at 2 slots |
| Two helper channels | one more socket and failure domain | gain **not measured** |
| 512 MiB conversation RAM ceiling | above it a conversation parks **disk-only**: ~1.7 GiB at 70k tokens, ~3.85 GiB at 240k | deliberate; documented only in `--help` (docs gap) |
| Pool failure amplification | a request-level pool failure ends the **engine process**, taking concurrent requests with it | backlog; trigger fixed, amplification not |
| `#267` gfx906 wedge | transient verify-timeout hang, also seen on old stock | bench retries a leg once |
| `tests/core/cuda_runtime.h` shim | the 37-line CPU shim (fork `7baf1501c3`) shadows `hip_compat/cuda_runtime.h` for targets with `-I tests/core`: `cudaMalloc`/`cudaMemcpy` undeclared, **37 errors with the directory, 0 without** (fork's own tree) | fork's call: move the shim, or keep `tests/core` off the two dflash blocks |
| Three upstream test targets | `native_moe_gated_test`, `prefill_decode_golden` link with no object on gfx906; `conversation_transfer_test` passes upstream's `NOT STRATA_ENABLE_HIP` gate (OFF here) and needs `cudaErrorInvalidValue` | **locally guarded** as upstream defects (`strata_gpu_engine.cmake:512`/`:1359`, `backends/cuda.cmake:135`); reproduced on the untouched tag |
| CUDA 13.1 + glibc 2.43 | `nvcc` cannot compile even a one-line `.cu`: `rsqrt`/`rsqrtf` collide with glibc's | two-line `noexcept (true)` patch on this box (lines 629/653, backup kept); **any CUDA build here needs it** |
| Upstream SSD spill tier | 0.2.18 enables `--conversation-spill-mib` by default; with both tiers carried a park is written twice | ported/test configs must set `--conversation-spill-mib 0` |
| Merge default changes | `STRATA_MTP_BATCH_F16` now FP16 (fork: MMQ); `STRATA_ROUTE_TAIL_SKIP` now off everywhere | upstream's defaults kept; compare decode against the fork's build knowing this |
| Default off | tap-overlap F1/F2, `STRATA_REFILL_OVERLAP`, `STRATA_WINDOW_POPULATE`, `--short-read`, the bounded pool retry | parked |

---

## Build guide

### Prerequisites

- **ROCm 6.1.0 pinned** at `/opt/rocm-6.1.0` (`libhipblas.so.2`, `libamdhip64.so.6`); nothing newer may leak into the link (unloadable SONAMEs, exit 127).
- **Custom rocBLAS 4.1.0** at `~/rocm-gfx906-xnack` — `gfx906:xnack-`, merged Tensile library (stock 6.1/6.4 segfaults on gfx906: fallback `.dat` metadata missing; `docs/work/rocblas-gfx906-bug-report.md`). FIRST on `CMAKE_PREFIX_PATH` and `LD_LIBRARY_PATH`.
- **CUDA toolkit** for the helper only (13.1 here, arch 86, patched for glibc 2.43).
- A fresh build directory needs `-DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF`; without it the engine refuses to start and the supervisor loops.

### Engine (HIP) and helper (CUDA)

```sh
# HIP engine — keep nvcc OUT of this configure
export PATH=/opt/rocm-6.1.0/bin:$(echo $PATH | tr ':' '\n' | grep -v cuda | paste -sd:)
cmake -S . -B build/rpcw-hip -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DSTRATA_HIP_GFX906=ON -DCMAKE_HIP_ARCHITECTURES=gfx906 \
  -DCMAKE_HIP_COMPILER=/opt/rocm-6.1.0/llvm/bin/clang++ \
  -Dhip_DIR=/opt/rocm-6.1.0/lib/cmake/hip -Dhipblas_DIR=/opt/rocm-6.1.0/lib/cmake/hipblas \
  -DROCM_PATH=/opt/rocm-6.1.0 -DHIP_PATH=/opt/rocm-6.1.0 -DHIP_ROOT_DIR=/opt/rocm-6.1.0 \
  "-DCMAKE_PREFIX_PATH=$HOME/rocm-gfx906-xnack;/opt/rocm-6.1.0" \
  -DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF -DSTRATA_BUILD_TESTS=ON
ninja -j11 -C build/rpcw-hip strata

# CUDA helper + full test tree
cmake -S . -B build/rpcw-cuda -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DSTRATA_ENABLE_CUDA=ON -DSTRATA_BUILD_EXPERT_HELPER=ON \
  -DCMAKE_CUDA_ARCHITECTURES=86 -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
  -DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF -DSTRATA_BUILD_TESTS=ON
ninja -j11 -C build/rpcw-cuda
env -u CUDA_VISIBLE_DEVICES ctest --test-dir build/rpcw-cuda
```

Add `-DSTRATA_BUILD_RAM_POOL_TEST=ON` for the pool/RAM targets; without it they are not built.

### 0.2.18 port tree

The port moved the build under `engine/` and needs two pins this box requires:

```sh
cd /mnt/srcds/Strata-wt-0218        # branch port-0218
sh tools-port/configure-906.sh build-906
ninja -C build-906 strata
sh tools-port/lane-tests.sh build-906            # PAIR=lane adds the 4 device programs
sh tools-port/configure-cuda-helper.sh build-cuda-helper
ninja -C build-cuda-helper strata-expert-helper
```

`configure-906.sh` adds `--gcc-install-dir=…/gcc/x86_64-linux-gnu/15` (a stray `/usr/include/c++/16` breaks clang 17) and `-isystem /opt/rocm-6.1.0/include` (the distro `/usr/include/hip` wants `__builtin_amdgcn_wavefrontsize`). The helper configure wants `STRATA_BUILD_TESTS=ON` — not for tests: the release shape configures no ggml and the sidecar includes `ggml.h`.

Runtime env the launchers set:

```sh
LD_LIBRARY_PATH=$HOME/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib
HSA_OVERRIDE_GFX_VERSION=9.0.6  HIP_FORCE_P2P=1  HSA_ENABLE_SDMA=1
```

### Lessons baked in

- Check `ldd`: `libhipblas.so.2`/`libamdhip64.so.6` from `/opt/rocm-6.1.0/lib`, `librocblas.so.4` from `~/rocm-gfx906-xnack/lib`. Anything else is a misbuild.
- Hide `nvcc` from the HIP configure (mixed-header build: 231 errors).
- Never pipe `ninja` into `head` (SIGPIPE); never compile while serving or benching.
- Hide both cards from lane tests (`HIP_VISIBLE_DEVICES=-1 CUDA_VISIBLE_DEVICES=-1`) so they cannot take VRAM from a serving engine.
- A TU compiling is not the engine linking: hash the `strata` binary you will boot.

### Rebuilding the custom rocBLAS

The shape, **not a re-verified run** (the original invocation is lost; only the prefix survived):

```sh
git clone -b rocm-6.1.0 https://github.com/ROCm/rocBLAS.git
cmake -S rocBLAS -B rocBLAS/build \
  -DCMAKE_INSTALL_PREFIX="$HOME/rocm-gfx906-xnack" \
  -DAMDGPU_TARGETS="gfx906:xnack-" \
  -DCMAKE_PREFIX_PATH=/opt/rocm-6.1.0
cmake --build rocBLAS/build --parallel 11
cmake --install rocBLAS/build
```

Heavy: Tensile codegen, hours, tens of GB.

## Repo map

| Path | What |
| --- | --- |
| `journal/pool-admission-fix-2026-11-21.md` | 240k bug, margin ladder, park/restore, the 0.2.18 port — read first |
| `CHAMPION.md`, `JOURNAL.md` | champion tuple + knob table; session record, verdicts, post-mortems |
| `bench/ab-v0210/` | arm configs, drivers, `out/RESULTS*.md`; `rpcprof/knob-leg.sh` = one stop → boot → leg → teardown |
| `docs/MIXED_GPU_HELPER.md`, `docs/DFLASH*.md` | fork feature docs |
| `tools/mixed_gpu/` | pair supervisor, window harness, helper tooling |
| `tools/quant_quality.py` | quality yardstick (teacher-forced, prompt-mode) |
| `serve/server.py` | OpenAI/Anthropic-compatible API over the supervised pair |
| `/mnt/srcds/Strata-wt-0218` | the 0.2.18 port: `PORT-NOTES.md`, `PORT-MANIFEST*.txt`, `tools-port/` |

Branches: `main` (canonical, published, builds + gated), `build-mixed-v0210` (prod line), `pair-self-heal`, `merge-helper-ipc-diet` (pool suite + the `564e1dbbea` clamp), `port-0218`, `stock-v0210-gfx906-buildfix`, `v0210-dev-line`, `tap-overlap-f1f2` (parked).

Upstream hygiene: releases v0.1.45 → v0.2.11 kept; upstream machinery wins where it supersedes, fork specifics re-integrated and depth-verified each merge; fork features stay opt-in with the default path byte-identical to stock; both backends build before any promotion.

## Support Strata

**Help keep local AI open to everyone.** A GitHub sponsorship or a coffee helps fund development and testing on more hardware.

<a href="https://github.com/sponsors/Niko1221"><img src="https://img.shields.io/badge/Sponsor_on_GitHub-EA4AAA?style=for-the-badge&logo=githubsponsors&logoColor=white" alt="GitHub Sponsors: sponsor Strata" height="48"></a>
<a href="https://buymeacoffee.com/strataengine"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me a Coffee: support Strata" height="48"></a>

### Loyal supporters

Thank you to everyone who supports Strata.

<!-- supporters:start (one line per public supporter, newest last; copy a line and change the GitHub name in all four places) -->
<a href="https://github.com/ryan4yin"><img src="https://github.com/ryan4yin.png?size=96" width="48" height="48" alt="ryan4yin" title="ryan4yin"></a>
<a href="https://github.com/themactep"><img src="https://github.com/themactep.png?size=96" width="48" height="48" alt="themactep" title="themactep"></a>
<a href="https://github.com/macintog"><img src="https://github.com/macintog.png?size=96" width="48" height="48" alt="macintog" title="macintog"></a>
<a href="https://github.com/bisonbet"><img src="https://github.com/bisonbet.png?size=96" width="48" height="48" alt="bisonbet" title="bisonbet"></a>
<a href="https://github.com/vizv"><img src="https://github.com/vizv.png?size=96" width="48" height="48" alt="vizv" title="vizv"></a>
<!-- supporters:end -->

[MIT license](LICENSE) · [Credits and model licenses](docs/HOW_IT_WORKS.md#credits) · [Privacy](docs/SAFETY.md)