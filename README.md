# Strata — gfx906 Mixed-GPU Fork

An engine fork of [Niko1221/Strata](https://github.com/Niko1221/Strata) (all releases v0.1.45 → **v0.2.11** kept) that runs **Qwen3.8-Flash-Next ("Swift 1.5")** on **2× Radeon VII (gfx906) + an NVIDIA helper card** — hardware stock only compile-checks — and beats stock on it.

> Built and operated agent-first: pinned toolchains, custom libs, harness scripts, and a mandatory measure-before-promote discipline. Start at `JOURNAL.md` (session record + verdicts) and `bench/ab-v0210/out/RESULTS*.md`. Every claim below is measured on the rig in this README.

**Contents:** [Latest](#latest--2026-11-08) · [At a glance](#at-a-glance) · [Who it's for](#who-its-for) · [The rig](#the-rig) · [How the fork works](#how-the-fork-works) · [Measured vs stock](#measured-vs-stock) · [Quality gates](#quality-gates) · [Trade-offs](#trade-offs-and-known-defects) · [Build guide](#build-guide) · [Repo map](#repo-map)

## Latest — 2026-11-08 (evening)

**Publish-only idle saves + disk-tier park-gain** — the disk cache no longer churns. With `--conversation-cache-mib 0`, every completed request used to park the ACTIVE conversation's whole image to disk AND invalidate the mounted session, so the same conversation's next step restored its own file from disk: on prod, 91% of requests restored (p50 28 s, 62–67 s at 150k ctx, scaling with context) and rewrote the file every turn even at +45 tokens.

- Fix 1 (`e155f3391`): the request-start lookup lets the disk tier win only when deeper than everything the session already holds (live/resume, batch slot, RAM match) — an idle-published generation of the SAME conversation never wins, so sequential steps continue live. Idle-save is publication only.
- Fix 2 (`5d6e3c4e4`): `--conversation-cache-park-gain` gates the disk tier (prefix-rule `parked_prefix_tokens` + `DiskCache::publish_worth`) — within the gain the published file stays; a restart re-prefills only the unpublished tail.
- Gated (2× gfx906 + helper, split-32, fp16 KV, one conversation, sequential turns): **0 restores vs 11-of-12 turns pre-fix; sequential turns 16–19 s vs 80–96 s at 130–160k ctx**; publish cadence exactly `advance/4096`; s3 alternation, s5 restart, s4 rewind all reference-identical (2 restores / 24350 reused / 0 refusals, pair hits intact); RSS HWM unchanged (48.8 GiB).
- Promoted to prod (v0212) and live-verified: same-conversation follow-up continues live (no restore), a real A→B→A switch restores once (`complete target+draft hit`), and the idle boundary logs `park-gain: advanced 23 of 4096 tokens … the published file stays`. Zero new RAM/VRAM.

## 2026-11-08 — Streaming disk conversation cache

**Streaming disk conversation cache** (branch `disk-cache-streaming`, gated on-hardware before promotion): conversations park to and restore from disk with bounded host RAM — no full-RAM snapshot on save or restore, direct I/O so the page cache isn't charged either.

- Split-aware session format v2 + paired DFlash draft-ring resume + automatic tier with opt-in idle-save; disk-only serving via `--conversation-cache-mib 0`.
- Measured (2× gfx906 + CUDA helper, split-32, fp16 KV, ~12k-token conversations): **park 1.1–1.3 s, restore 6.1–6.7 s vs 42–45 s cold reread** (~7×), `12175 reused + 21 read` on resume. One 12.2k-token generation is **~0.61–0.65 GB on disk** (650 MiB/gen measured). The restore streams the file twice (validate, then transfer) at ~250 MB/s aggregate: at 32k tokens it restores in 10.0–11.2 s and at 96k in 28.1–30.4 s — about **1.5× faster than the first promoted reader**, which streamed the same bytes a third time in a sinkless precheck (removed: the precheck now reads the manifest only). Park writes on the same disk measured 678–831 MB/s.
- RAM: the whole conversation RAM tier turns off — the gate's disk-only arm ran its full series ~0.5 GiB below the RAM-tier arms (RSS HWM 48.8 vs 49.4 GiB) with conversations on NVMe instead.
- Quality gate (A = base, B1 = branch, B2 = branch disk-only, A2 = base repeat; same boot): greedy thinking-on probe and both restore-path continuations **hash-identical across all four arms**; PP 605.1/603.1/602.8/603.6 tok/s (fresh 16k), TG 41.2/41.2/41.6/40.8 tok/s (cached band), drafts accepted 18/20 everywhere — no regression, token-exact restores.
- Hardened by the harness that found four real defects pre-promotion (dangling stream captures, a missing finish sink, a `from_live` flow bug, and unaligned O_DIRECT preads disabling the tier) plus a fifth caught by the on-hardware window before the read-ahead promotion (the pipelined reader's second-buffer aliasing — see `fix(engine): ping-pong the read-ahead buffers across blocks`); scenarios cover A/B alternation, rewind past the ring horizon, restart + idle-save, edited-prefix misses, and corrupt-byte safe refusal.

## At a glance

- **Runs where stock barely reaches** — stock's gfx906 support is a compile-checked community port; v0.2.10/v0.2.11 as tagged don't even build there. This fork runs it as a daily server.
- **~1.5× decode** — pooled median **34.7 vs 23.3 tok/s** vs same-day stock (+49 %); 1.3× on 1024-token completions.
- **1.4–2.4× faster short-prompt reads** (214–620 tok), plus 14–20 % more from the prompt-stage memo.
- **−6.2 GiB host RAM** via expert-helper RAM relief.
- **Exact quality parity** — greedy 80k infill byte-identical; 4995-position teacher-forced yardstick: top-1 100 %, KL 0.00000, PPL equal.
- **Self-healing pair** — SIGKILL the helper: requests finish, the supervisor rebuilds the pair, health returns.

## Who it's for

- **gfx906 owners** (Radeon VII; MI50/MI60 share the target, untested here) wanting a production local-LLM server.
- **Mixed AMD + NVIDIA rigs** — stock has no cross-vendor path; here the NVIDIA card serves expert/prefill rows to the AMD HIP engine.
- **Big MoE, small cards** — ~47 GiB expert set serving from 2× 16 GiB VRAM + 62 GiB host RAM.
- **RAM-tight hosts** — 6.2 GiB handed back to the OS.
- **Interactive agent traffic** — OpenAI/Anthropic-compatible API (`serve/server.py`), 200-token reads in ~3 s instead of 6.5 s.
- **Anyone benching engine changes** — the A/B(/C stock) harness, sha parity gates and quality yardstick are reusable.

## The rig

Every number was measured on exactly this machine:

| Part | What | Role |
| --- | --- | --- |
| CPU | Ryzen 9 5900HX, 8C/16T | host staging, quality yardstick |
| RAM | 62 GiB | expert arena (~41 GiB anon after relief) |
| GPU 0+1 | 2× Radeon VII (gfx906, 16 GiB) | **the HIP engine**: all execution, layer-split, DFlash2 drafter |
| GPU 2 | RTX 3080 Laptop, 8 GiB (sm_86) | **the CUDA helper**: expert + prefill rows, ~6.5 GiB cache |
| Storage | 1 TB NVMe + 2 TB SATA | model packs, relief reads |
| OS | Ubuntu 25.04, XanMod `7.2.9-x64v3` | userland the custom rocBLAS is built against |
| rocBLAS | custom **4.1.0** for `gfx906:xnack-` (`~/rocm-gfx906-xnack`) | GEMM for both VIIs; stock 6.1/6.4 segfaults on gfx906 |
| Model | Swift-1.5 packs (IQ3_XXS / IQ3_S) | ~47 GiB expert set |

**ROCm first, CUDA second.** The engine maps no CUDA runtime; the helper card only serves rows on request. The pack's own Jinja chat template is rendered exactly like HF `apply_chat_template`.

## How the fork works

Six opt-in pieces; the default path stays byte-identical to stock.

**1. gfx906 backend** — full HIP wave64 port, kept compiling v0.1.45 → v0.2.11; compat shims (cublas→hipBLAS, `__ldg`, int4 align fix); tuned env knobs (`STRATA_GFX906_TOPK_*`, `STRATA_HIP_PROMPT_F16`); Blackwell-only paths gated off.

**2. CUDA expert helper** — separate process owning the NVIDIA card: fills ~3.6–4.6k slots (~6.5 GiB; caps from bench sweeps, +13.6 % decode vs old cap); two tiers (decode/verify + prompt via `STRATA_PREFILL_HELPER=1`); RAM relief drops helper-held pairs from the host arena. Decode tier alone ~neutral on this pack (96–97 % cache hit) — its value is the prompt tier, expert capacity, and relief.

**3. Host RAM relief** — `--expert-helper-ram-relief`: 4116/4116 pairs dropped, **6.24 GiB back**; deferred `cudaHostRegister`; W1a/W1b residency fixes (relief TG 16.0 → 37.65, disk reads 40 GB → 2.31 GB per drive); opt-in `STRATA_ARENA_COLLAPSE`.

**4. DFlash2 speculative decoding** — HIP-side drafter (stock is CUDA-only), q4_0 draft measured optimal, `--spec 5` optimum; beyond stock: exact-match verification for sampled requests, bounded SWA draft ring with deep rewinds, draft-image parking, batched KV append, confidence gating. Attribution arm E: with the drafter off, decode = stock speed — the whole win is DFlash2, byte-identical output.

**5. Short-prompt latency** — `STRATA_PREFILL_CPU_SHARE=auto` (214 tok 2.4×, 620 tok 1.8×, ≥18k unchanged); `STRATA_PROMPT_STAGE_MEMO` (+14–20 % short legs); `--prefill 16384` peak for long-context serving.

**6. Bench + quality discipline** — `bench/ab-v0210/` arm harness (`run-arm.sh`, `drive.py`, fail-fast gates); `tools/quant_quality.py` yardstick; standing rule: speed lane AND quality lane before any promotion.

## Measured vs stock

Serving build = the v0.2.11 upmerge (arm-B lane: helper + `--spec 5`), stock = clean v0.2.10 binary, same night, exclusive box, 2 rounds pooled:

| Metric | Fork (v0.2.11) | Stock | Delta |
| --- | ---: | ---: | ---: |
| TG 494-tok, pooled median tok/s | 34.7 | 23.3 | **+49 %** |
| TG 512/513-tok, tok/s | 29.3–31.9 | 23.6–23.9 | +26 % |
| 1024-token completion wall | 30.5–36.4 s | 43.0–47.8 s | **1.3×** |
| Short prompt 214 tok (prod lane) | 2.6–2.9 s | 6.5 s baseline | **2.4×** |
| Short prompt 620 tok (prod lane) | 6.2 s | 11.5 s baseline | **1.8×** |
| Fresh 16k prefill read, tok/s | 299 | 338 | −12 % |
| 88k infill read, tok/s | 472 | 549 | −14 % |
| Host anon RAM (prod lane) | 41.2 GiB | 47.4 GiB | **−6.2 GiB** |
| Greedy 80k output | `1c7d4a6d…` | identical | ✓ |

Historical multi-round windows (relief/CPU-share/memo arms, prod configs) match or beat these: TG 37.7–38.5 vs 27.6 (+36–39 %). Full per-arm verdicts: `bench/ab-v0210/out/RESULTS*.md`, `JOURNAL.md`.

## Quality gates

| Gate | What it checks | Result |
| --- | --- | --- |
| Greedy 80k SHA | deep-infill byte-identical fork / flags-off / stock | held 18/18 kept rounds; reproduced on the v0.2.10 and v0.2.11 builds |
| Teacher-forced yardstick | 4995 positions, full-vocab top-1 / KL(p99) / PPL vs stock | **top-1 100.00 % · KL 0.00000 · p99 0.0000 · PPL 4.052 = 4.052** (iq3xxs-15 pack; historical iq3s: 4.115 = 4.115) |
| Prompt-mode first logits | 20 prompts, fork vs stock | top-1 100 % · KL 0.00000 (both upmerges) |
| Probe determinism | fixed prompt → exact sha per config | reproduces exactly |
| ctest | full suite, fresh builds | 291 tests: all pass (guarded skips only) |

Re-run them: `bench/ab-v0210/run-arm.sh` (speed lane), `tools/quant_quality.py run|compare` (yardstick), `ctest` from a built tree.

## Trade-offs and known defects

- Fresh ~16k prefill reads pay **−12 %** for DFlash tap collection (long-context reads gain instead).
- `#267` gfx906 lost-completion wedge: transient verify-timeout hangs (~1 in 20 long drives, also on old stock); the bench retries the leg once.
- Small fresh-read stalls (30–140 s, ~200-token cold reads) — **host co-tenancy**, not the engine; watchdog 900 s, helper reply deadlines tolerant.
- iq3_s at 256/256 MiB reserves → non-finite logits on long prefill; fixed policy 640/640 + fill 7000.
- Not promoted, default off: tap-overlap F1/F2, `STRATA_REFILL_OVERLAP`, `STRATA_WINDOW_POPULATE`, `--short-read` routing.
- New upstream tests sized for big cards may skip here (e.g. `gemm_w16_parity` needs ~8.2 GiB free VRAM → exit 77 below that).

---

## Build guide

### Prerequisites

- **ROCm 6.1.0 pinned** at `/opt/rocm-6.1.0` (`libhipblas.so.2`, `libamdhip64.so.6`); newer trees on the box must not leak into the link (unloadable SONAMEs, exit 127).
- **Custom rocBLAS 4.1.0** at `~/rocm-gfx906-xnack` — single-target `gfx906:xnack-`, merged Tensile library (stock 6.1.0/6.4.4 segfaults on gfx906: fallback `.dat` metadata missing — analysis + repro: `docs/work/rocblas-gfx906-bug-report.md`). Sits FIRST on `CMAKE_PREFIX_PATH` and runtime `LD_LIBRARY_PATH`.
- **CUDA toolkit** for the helper only (`nvcc`, arch 86). Ninja + CMake (Release).

### Rebuilding the custom rocBLAS

The recipe below is the shape, **not a re-verified run** (the original invocation is lost; only the installed prefix survived):

```sh
git clone -b rocm-6.1.0 https://github.com/ROCm/rocBLAS.git
cmake -S rocBLAS -B rocBLAS/build \
  -DCMAKE_INSTALL_PREFIX="$HOME/rocm-gfx906-xnack" \
  -DAMDGPU_TARGETS="gfx906:xnack-" \
  -DCMAKE_PREFIX_PATH=/opt/rocm-6.1.0
cmake --build rocBLAS/build --parallel 11
cmake --install rocBLAS/build
```

Heavy: Tensile codegen, hours, tens of GB. When next rebuilt, this section gets the real invocation and verification numbers.

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

Runtime env the launchers set:

```sh
LD_LIBRARY_PATH=$HOME/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib
HSA_OVERRIDE_GFX_VERSION=9.0.6  HIP_FORCE_P2P=1  HSA_ENABLE_SDMA=1
```

### Lessons baked in

- Configure derived trees fresh (stale caches keep dead `find_library` results); pin everything.
- Hide `nvcc` from the HIP configure (mixed-header build, 231 errors).
- Never pipe `ninja` into `head` (SIGPIPE, exit 141); never compile while serving or benching.
- Memory-gentle fallback: `nice ninja -j2`, `-j1` below 3 GiB available.
- Verify `ldd`: no `libhipblas.so.3`, no `libamdhip64.so.7`.
- Run GPU tests with `env -u CUDA_VISIBLE_DEVICES` if your shell hides the NVIDIA card.

## Repo map

| Path | What |
| --- | --- |
| `JOURNAL.md` | session record, verdicts, post-mortems — read first |
| `bench/ab-v0210/` | arm configs, drivers, `out/RESULTS*.md`, quality artifacts |
| `docs/MIXED_GPU_HELPER.md`, `docs/DFLASH*.md` | fork feature docs |
| `docs/work/` | lever calibrations, rocBLAS bug report, design docs |
| `tools/mixed_gpu/` | pair supervisor, window harness, helper tooling |
| `tools/quant_quality.py` | quality yardstick (teacher-forced, prompt-mode) |
| `serve/server.py` | OpenAI/Anthropic-compatible API over the supervised pair |

Branches: `main` (canonical, published, always builds + gated), `stock-v0210-gfx906-buildfix`, `build-mixed-v0210` (prod line), `pair-self-heal` (kill-test validation), `v0210-dev-line` (pre-merge archive), `main-pre-publish`, `build-latest`, `tap-overlap-f1f2` (parked).

Upstream hygiene: releases v0.1.45 → v0.2.11 kept; upstream machinery wins where it supersedes, fork specifics re-integrated and depth-verified each merge; fork features stay opt-in with the default path byte-identical to stock; both backends build before any promotion.
