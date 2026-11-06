# Strata — gfx906 Mixed-GPU Fork

An engine fork of [Niko1221/Strata](https://github.com/Niko1221/Strata) (upstream **v0.2.10**) that runs **Qwen3.8-Flash-Next ("Swift 1.5")** on **2× Radeon VII (gfx906) + an NVIDIA helper card** — hardware stock Strata only compile-checks — and beats stock on it.

> **Made to be built and run by an agent, not by humans.** Compiling, installing, benching, promoting and operating this fork are agent workflows: pinned toolchains, custom libs, harness scripts and a mandatory speed+quality measurement discipline. A human gives orders and reads results — the agent executes and documents. Start at `JOURNAL.md` (session record), `bench/ab-v0210/out/RESULTS*.md` (verdicts) and the build guide below; `AGENTS.md` is the technical reference for AI tools.

**Contents:** [At a glance](#at-a-glance) · [Who it's for](#who-its-for) · [The rig](#the-rig) · [How it works](#how-the-fork-works) · [Measured vs stock](#measured-fork-vs-stock-v0210) · [Quality gates](#quality-gates) · [Trade-offs](#trade-offs-and-known-defects) · [Build guide](#build-guide--how-we-built-it) · [Repo map](#repo-map)

## At a glance

- **Runs where stock barely reaches** — stock's gfx906 support is a community source build the maintainers have never run, and v0.2.10 as tagged does not even compile for gfx906; this fork runs it as a daily server.
- **Faster decode** — **+36–39 %** vs same-day stock (37.7–38.5 vs 27.6 tok/s; +48 % in the widest same-day frame).
- **Faster chat** — **1.4–2.4×** faster short-prompt reads, plus **14–20 %** more from the prompt-stage memo.
- **Less RAM** — **−6.2 GiB** host memory via expert-helper RAM relief, at control speed.
- **Bit-exact quality** — greedy 80k-token infill byte-identical to stock; teacher-forced gates logprob-identical (KL 0.00000, top-1 100 %, PPL 4.115 = 4.115).
- **Measured or it didn't happen** — every claim comes from a same-day A/B/C bench window; artifacts in `bench/ab-v0210/out/`, the story in `JOURNAL.md`.

## Who it's for

The fork pays off when your hardware or your traffic matches what it was built for:

- **gfx906 owners (Radeon VII, MI50, MI60)** — stock ships only a compile-checked community source path; this fork runs these cards as a production local-LLM server. MI50/MI60 share the target but are untested here.
- **Mixed AMD + NVIDIA rigs** — stock has no cross-vendor serving path; here the NVIDIA card pulls expert and prefill duty for the AMD HIP engine.
- **Big MoE model, small cards** — a ~47 GiB expert set serving from 2× 16 GiB VRAM + 62 GiB host RAM.
- **RAM-tight hosts** — 6.2 GiB of anonymous memory handed back to the OS.
- **Interactive agent traffic** (OpenCode, Claude Code-style clients) — 200-token reads in ~3 s instead of 6.5 s, on the OpenAI/Anthropic-compatible API.
- **Quality-critical users** — byte-identical greedy output vs stock, plus KL / top-1 / PPL gates before every promotion.
- **Bench & tuning work** — the A/B/C harness, sha parity gates and quality yardstick are reusable for any further Strata engine work.

## The rig

Every number in this README was measured on exactly this machine; nothing is claimed for other hardware.

| Part | What | Role here |
| --- | --- | --- |
| CPU | AMD Ryzen 9 5900HX, 8C/16T, ~4.7 GHz | host staging, quality yardstick |
| RAM | 62 GiB | expert arena: ~41 GiB anon after relief (~34 GiB registered) |
| GPU 0+1 | 2× AMD Radeon VII (Vega 20, gfx906, 16 GiB HBM2) | **first — the ROCm/HIP engine**: all model execution, layer-split, DFlash2 drafter |
| GPU 2 | NVIDIA RTX 3080 Laptop, 8 GiB (sm_86) | **second — the CUDA helper**: expert rows + prefill tier, ~6.5 GiB cache |
| Storage | 1 TB WD SN530 NVMe + 2 TB Crucial BX500 SATA | model packs, relief reads |
| OS | Linux, XanMod `7.2.9-x64v3-xanmod1` | — |
| Model | Swift-1.5-Qwen3.8-Flash-Next, GSQ-RCO IQ3_XXS / IQ3_S packs | ~47 GiB expert set |
| Server | `serve/server.py` | OpenAI- and Anthropic-compatible API, supervised pair |

The point of the rig: a laptop-class CPU, two 2019-vintage Radeon VIIs and an 8 GiB laptop GPU together serve a ~47 GiB MoE model at coding-agent speed.

**ROCm first, CUDA second.** The AMD cards run everything; the NVIDIA card only serves expert rows and prefill rows on request. The engine process maps no CUDA runtime at all. Helper loss is **self-healing**: in-flight requests finish on the engine alone (it recomputes the claimed rows), and the pair supervisor restarts the whole engine+helper pair on any peer death — the SIGKILL-the-helper kill test passes (generation bump, health back to 200).

## How the fork works

Six pieces, each opt-in; the default path stays byte-identical to stock. The first three make the rig possible at all, the last three make it fast.

### 1. gfx906 backend (Radeon VII)

Stock treats gfx906 as a compile-checked community port; this line runs it daily and carries the v0.2.10 compile fixes (`stock-v0210-gfx906-buildfix`).

- Full HIP wave64 port of the engine, kept compiling through every upstream merge since v0.1.45 → v0.2.10.
- Compat shims: `cudaStreamCreateWithPriority`, cublas→hipBLAS, `__ldg` / fp16 bit-casts, `STRATA_SHARED_ALIGN16_I4` int4 fix.
- Tuned knobs in prod configs: `STRATA_GFX906_TOPK_FIT17`, `STRATA_GFX906_TOPK_REG`, `STRATA_HIP_PROMPT_F16`.
- Blackwell-only `mmq_w4a4` and known-broken paths cleanly gated off.

### 2. The CUDA expert helper

A second process hands the AMD engine the NVIDIA card's VRAM and compute over a local socket pair — the helper is NVIDIA-only and owns all of that card's expert memory.

- Fills ~3.6–4.6k cache slots (pack-dependent, ~6.5 GiB) and keeps them filled; caps come from bench sweeps (**+13.6 %** decode median vs the old 4000-slot cap).
- Two serving tiers: a **decode/verify tier** (expert rows for ≤ 8-token windows) and a **prompt tier** (`STRATA_PREFILL_HELPER=1`) that computes prefill rows for helper-held experts.
- Relief interplay: helper-held pairs are dropped from the host arena (`--expert-helper-ram-relief`); pack-file reads exist only as the emergency fallback.
- Resilience, two layers: **in-flight** — the engine recomputes claimed rows and finishes the request (measured kill test); **pair self-heal** — the supervisor restarts the whole pair on ANY peer death (verified prod cutover, default on). `--persist` re-listens for the next engine; v4 fill supports multi-shard packs.
- Honest attribution: the decode tier alone is ~neutral on this pack (96–97 % local cache hit, arms D/E) — the helper's measured value is the prompt tier, expert capacity, and RAM relief.

### 3. Host RAM relief

The experts the helper holds in VRAM don't also need to live in host RAM — relief drops them from the anon arena and gives the memory back.

- `--expert-helper-ram-relief`: 4116/4116 pairs dropped, 6.24 GiB back.
- Deferred-complement `cudaHostRegister`: arena built unregistered, only kept ranges registered, outward span rounding.
- W1a/W1b fixes: lock-free residency pre-checks + dropped-only prefetch forwarding — relief TG 16.0 → 37.65 tok/s (control 38.05), disk reads 40 GB → 2.31 GB per drive.
- `STRATA_ARENA_COLLAPSE`: opt-in post-fill `MADV_COLLAPSE` sweep (25.05 GiB of the arena collapsed in H1-r5), with a scoped fix for inherited `PR_SET_THP_DISABLE`.

### 4. DFlash2 speculative decoding

Every decoded token we don't pay for comes from the drafter — attribution arm E (drafter off) decodes at stock speed (23.9 vs 23.6 tok/s), so the entire decode gain is DFlash2, at byte-identical output.

- Runs HIP engine-side on the Radeon VIIs (stock DFlash2 is CUDA-only) — drafting never touches the helper card.
- Official Flash-Next draft format (inco draft, q4_0 GGUF); Q8/Q5/Q4 draft weights supported — q4_0 measured optimal, q8_0 flat: the ~60–65 % acceptance ceiling is the draft model, not its quantization.
- Tuned optimum, sweep fully closed: `--spec 5` / block 5 / q4_0 (spec 7 −3 % TG, block 3 flat, acceptance never bought back by sampling arms).
- Beyond stock drafter behavior: **target exact-match verification** for sampled requests (`STRATA_DFLASH_SAMPLED`); a **bounded SWA draft ring** with recoverable deep rewinds plus `STRATA_DF_RING_MULT` so segment resumes stop re-reading the context; draft image survives conversation parking; per-section timing (`STRATA_DF_EVENTS=1`); batched KV append, attention reuse across value lanes, batched BF16 projections, confidence gating.
- DFlash standalone (the prerequisite layer): GGUF drafter artifacts via `tools/dflash_gguf.py`, target taps [3,15,23,35,43], private draft K/V; MTP stays available, mutually exclusive.

### 5. Short-prompt latency

Coding agents send small prompts constantly — three promoted knobs make those cheap, and ≥18k prompts don't regress.

- `STRATA_PREFILL_CPU_SHARE=auto`: 214 tok **2.4×**, 620 tok **1.8×**, 4k **1.4×**, TG128 −5.1 s, ≥18k unchanged, zero regressions.
- `STRATA_PROMPT_STAGE_MEMO`: a dropped prompt expert is read once per request — medians **14–20 %** faster across short-prompt legs.
- `--prefill 16384`: measured peak for long-context serving (24k −10 %, 32k −37 %).

### 6. Bench + quality discipline

Nothing ships on a hunch — every knob above was an arm in a controlled window before it was promoted.

- `bench/ab-v0210/`: A/B/C arm harness (`run-arm.sh`), traffic driver (`drive.py`), verdict docs, RAM/IO samplers, fail-fast guards, port / VRAM / MemFree boot gates.
- Quality yardstick (`tools/quant_quality.py`): 4995 teacher-forced positions, full-vocab KL / top-1 / PPL vs the stock binary.
- Parity gates: greedy 80k-infill SHA byte-identical across fork, fork-flags-off, and stock in every kept round (18/18 in the tap-overlap window alone).
- Standing rule: a new engine version passes BOTH the speed lane and the quality lane before promotion.

## Measured: fork vs stock v0.2.10

Same day, same box, same pack. Attribution (arms D/E): the decode win is entirely DFlash drafting; the helper expert tier alone is ~neutral on this pack — its value is the prompt tier, expert capacity, and RAM relief.

| Metric | Fork (prod config) | Stock | Delta |
| --- | ---: | ---: | ---: |
| TG cached decode, median tok/s | 37.7–38.5 | 27.6 | **+36–39 %** |
| 80k-token infill read, tok/s | 553 | 550 | parity (relief on) / **+7.6 %** relief-off |
| Fresh ~18k prefill, tok/s | 272–278 | 337 | **−18 %** (dflash tap-collection cost) |
| Short prompt 214 tok, s | 2.6–2.9 | own flags-off baseline 6.5 | **2.4×** |
| Short prompt 620 tok, s | 6.2 | own flags-off baseline 11.5 | **1.8×** |
| Host anon RAM, GiB | 41.2 | 47.4 | **−6.2** |
| Greedy 80k output | byte-identical | — | ✓ |

> **Bottom line vs stock:** ≈ **1.4× decode throughput** · **1.4–2.4× chat-read latency** · **−6.2 GiB host RAM** · **byte-identical output** — one regression (**−18 %** fresh ~18k bulk prefill) — on hardware where stock, as tagged, does not build.

## Quality gates

What backs the "faster with zero quality cost" claim: the speed wins above were promoted only after these gates passed.

| Gate | What it checks | Result |
| --- | --- | --- |
| 80k greedy SHA | deep-infill (80k ctx, ≥5k out) byte-identical across candidate / flags-off / stock binaries | held **18/18** kept rounds (`1c7d4a6d…`) |
| Teacher-forced yardstick (`tools/quant_quality.py`) | 4995 positions, full-vocab first logits: top-1, KL (p99), PPL — vs stock binary AND vs flags-off twin | top-1 **100.00 %** · KL **0.00000** · PPL **4.115 = 4.115** (relief and CPU-share arms) |
| Probe determinism | fixed prompt re-answered every boot; sha must reproduce exactly per config | reproduces exactly, per-config values documented |
| Speed gates | TG median ≥ 33.1 · 80k frame 583–587 · fresh-18k toward 337 · medians over ≥ 2 kept boots/arm | see measured table above |

The memo knob was gated by the ab6s shallow prompt-mode runs (int8 + fp16 arms; artifacts in `bench/ab-v0210/quality/out/prompt-*`); the full overnight ab6/ab6b run stayed parked.

## Trade-offs and known defects

The wins are not free, and the defects are on record rather than hidden:

- Fresh ~18k prefill pays **−18 %** for DFlash tap collection (long-context reads gain instead).
- `#267` gfx906 lost-completion wedge: transient verify-timeout GPU hangs (~1 in 20 long drives, both cards, also on stock 0.1.45); the bench retries the leg once.
- Small fresh-read stalls (30–140 s on ~200-token cold reads): pre-existing, interactive-UX only; watchdog raised 300 → 900 s so it cannot kill prod.
- iq3_s at 256/256 MiB reserves → non-finite logits on long prefill (V8); fixed policy: 640/640 + fill 7000 (V9/V10) keeps both cards above the 512 MiB paging line.
- Not promoted, default off: tap-overlap F1/F2 (flat), `STRATA_REFILL_OVERLAP` / `STRATA_WINDOW_POPULATE` (net-negative on big prompts), `--short-read` window routing (refill poison).

---

## Build guide — how we built it

### Prerequisites (exactly what this box uses)

- **ROCm 6.1.0, pinned** at `/opt/rocm-6.1.0` — HIP compiler (`/opt/rocm-6.1.0/llvm/bin/clang++`), `libhipblas.so.2`, `libamdhip64.so.6`. Newer ROCm trees on the box (`6.4.4`, `7.2.4`) must **not** leak into the link: the engine then links `.so.3`/`.so.7` SONAMEs it cannot load (exit 127).
- **Custom libs** at `~/rocm-gfx906-xnack` — a self-built rocBLAS (`librocblas.so.4.1`, 82 MB) with gfx906-**xnack** kernels. It sits FIRST on `CMAKE_PREFIX_PATH` and on the runtime `LD_LIBRARY_PATH`, overriding the system rocblas.
- **CUDA toolkit** for the helper only: `/usr/local/cuda/bin/nvcc`, arch 86.
- Ninja + CMake (Release). The pinned ggml source (`3cf03257f`) is fetched automatically at configure.

### Engine — HIP, gfx906 (Radeon VII)

```sh
export PATH=/opt/rocm-6.1.0/bin:$PATH          # and keep nvcc OUT of this configure
cmake -S . -B build-v0210/rpcw-hip -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DSTRATA_HIP_GFX906=ON \
  -DCMAKE_HIP_ARCHITECTURES=gfx906 \
  -DCMAKE_HIP_COMPILER=/opt/rocm-6.1.0/llvm/bin/clang++ \
  -Dhip_DIR=/opt/rocm-6.1.0/lib/cmake/hip \
  -Dhipblas_DIR=/opt/rocm-6.1.0/lib/cmake/hipblas \
  -DROCM_PATH=/opt/rocm-6.1.0 -DHIP_PATH=/opt/rocm-6.1.0 -DHIP_ROOT_DIR=/opt/rocm-6.1.0 \
  "-DCMAKE_PREFIX_PATH=$HOME/rocm-gfx906-xnack;/opt/rocm-6.1.0" \
  -DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF -DSTRATA_BUILD_TESTS=ON
ninja -j11 -C build-v0210/rpcw-hip strata       # -j11 = standing order (git config strata.buildJobs)
```

### Helper — CUDA (RTX 3080 Laptop)

```sh
cmake -S . -B build-v0210/rpcw-cuda -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DSTRATA_ENABLE_CUDA=ON -DSTRATA_BUILD_EXPERT_HELPER=ON \
  -DCMAKE_CUDA_ARCHITECTURES=86 -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
  -DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF -DSTRATA_BUILD_TESTS=ON
ninja -j11 -C build-v0210/rpcw-cuda strata strata-expert-helper
ctest -R helper --output-on-failure               # from build-v0210/rpcw-cuda
```

### Runtime (what the launchers set)

```sh
LD_LIBRARY_PATH=$HOME/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib
HSA_OVERRIDE_GFX_VERSION=9.0.6  HIP_FORCE_P2P=1  HSA_ENABLE_SDMA=1
```

### Lessons baked into the process

- Pin everything; a bare configure finds the newest ROCm and links unloadable SONAMEs. Derived trees are always configured fresh (a reused cache keeps stale `find_library` results).
- Hide `nvcc` from the HIP configure — a new `/usr/local/cuda-*` under PATH silently wins and produces a mixed-header build (231 errors).
- Never pipe `ninja` into `head` (SIGPIPE kills it, exit 141) and never compile during a bench round (starves the engine's watchdog).
- Memory-gentle fallback when the box is busy: `nice ninja -j2`, dropping to `-j1` below 3 GiB MemAvailable.
- Verify with `ldd`: no `libhipblas.so.3`, no `libamdhip64.so.7`.

## Repo map

| Path | What |
| --- | --- |
| `JOURNAL.md` | session-by-session record, verdicts, post-mortems — read this first |
| `bench/ab-v0210/` | arm configs, drivers, `out/RESULTS*.md` verdict docs, quality artifacts |
| `docs/MIXED_GPU_HELPER.md`, `docs/DFLASH*.md` | fork feature docs |
| `docs/work/` | lever calibrations, identity investigations, design docs |
| `tools/mixed_gpu/` | pair supervisor, window harness, acceptance and maintenance tooling |
| `strata_control/`, `tools/` | prod control plane, quality yardstick, setup tests |
| Branches | `main` (canonical, published), `main-pre-publish` (845-commit upstream-main snapshot), `build-latest`, `build-mixed-v0210` (prod line history), `stock-v0210-gfx906-buildfix` (stock build fixes), `tap-overlap-f1f2` (parked) |

Upstream hygiene: upmerges from `upstream/main` (Niko1221/Strata; `origin` is this fork) — v0.1.45 → v0.2.10 kept, upstream machinery winning where it supersedes, fork specifics re-integrated and depth-verified after every merge; fork features are opt-in and the default path stays byte-identical to stock (F1off arm: all shas reproduce, fresh-PP equal); both backends build before any promotion.
