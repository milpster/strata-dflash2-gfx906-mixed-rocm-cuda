# Strata — gfx906 Mixed-GPU Fork

An engine fork of [Niko1221/Strata](https://github.com/Niko1221/Strata) (upstream **v0.2.10**) that runs **Qwen3.8-Flash-Next ("Swift 1.5")** on hardware stock Strata does not support — and beats stock on it.

> **Made to be built and run by an agent, not by humans.** Compiling, installing, benching, promoting and operating this fork are agent workflows: pinned toolchains, custom libs, harness scripts and a mandatory speed+quality measurement discipline. A human gives orders and reads results — the agent executes and documents. Start at `JOURNAL.md` (session record), `bench/ab-v0210/out/RESULTS*.md` (verdicts) and the build guide below; `AGENTS.md` is the technical reference for AI tools.

- **Runs where stock cannot**: stock v0.2.10 does not compile for AMD gfx906 (Radeon VII) as tagged; this fork builds and serves on it.
- **Faster decode**: **+36–39 % tok/s** vs same-day stock (37.7–38.5 vs 27.6 tok/s; +48 % in the widest same-day frame).
- **Faster chat**: **1.4–2.4×** faster short-prompt reads (214 / 620 / 4k tokens) plus **14–20 %** more from the prompt-stage memo.
- **Less RAM**: **−6.2 GiB** host memory via expert-helper RAM relief, at control speed.
- **Bit-exact quality**: greedy 80k-token infill byte-identical to stock; teacher-forced gates logprob-identical (KL 0.00000, top-1 100 %, PPL 4.115 = 4.115).
- **Measured or it didn't happen**: every claim comes from a same-day A/B/C bench window; artifacts in `bench/ab-v0210/out/`, the story in `JOURNAL.md`.

## Who this benefits

- **gfx906 owners (Radeon VII)** — stock v0.2.10 does not compile for these cards; this fork turns them into a production local-LLM server. Other gfx906 cards (Instinct MI50/MI60) share the target but are untested here.
- **Mixed AMD + NVIDIA rigs** — stock has no cross-vendor serving path; here the NVIDIA card pulls expert and prefill duty for the AMD HIP engine.
- **Big MoE model, small cards** — a ~47 GiB expert set serving from 2× 16 GiB VRAM + 62 GiB host RAM (layer split + expert helper + RAM relief).
- **RAM-tight hosts** — 6.2 GiB of anonymous memory handed back to the OS.
- **Interactive agent traffic** (OpenCode, Claude Code-style clients) — 200-token reads in ~3 s instead of 6.5 s; exactly the request shape coding agents produce, on the OpenAI/Anthropic-compatible API.
- **Quality-critical users** — byte-identical greedy output vs stock, plus KL / top-1 / PPL gates before every promotion.
- **Bench & tuning work** — the A/B/C harness, sha parity gates and quality yardstick are reusable for any further Strata engine work.

## Hardware — the box behind every number

Every number in this README was measured on exactly this machine; nothing is claimed for other hardware.

| Part | Detail |
| --- | --- |
| CPU | AMD Ryzen 9 5900HX — 8 cores / 16 threads, up to ~4.7 GHz |
| RAM | 62 GiB |
| GPU 0+1 | 2× AMD Radeon VII (Vega 20, gfx906, 16 GiB HBM2 each) — **first: the ROCm/HIP engine** |
| GPU 2 | NVIDIA GeForce RTX 3080 Laptop, 8 GiB (sm_86) — **second: the CUDA helper** |
| Storage | 1 TB WD SN530 NVMe + 2 TB Crucial BX500 SATA SSD — model packs, relief reads |
| OS / kernel | Linux, XanMod `7.2.9-x64v3-xanmod1` |

Serving stack on that hardware:

| Part | Role |
| --- | --- |
| Radeon VII ×2 | all model execution, layer-split over both cards, DFlash2 drafter included; the engine process maps no CUDA runtime |
| RTX 3080 Laptop | `strata-expert-helper`, a subordinate expert-row server + prefill prompt tier (~6.5 GiB expert cache), attached over a socket pair |
| Model | Swift-1.5-Qwen3.8-Flash-Next, GSQ-RCO IQ3_XXS / IQ3_S packs (~47 GiB expert set) |
| Drafter | DFlash2 (inco draft, q4_0), `--spec 5`, engine-side on the VIIs |
| Vision | `strata-vision` sidecar, Swift BF16 mmproj |
| Server | OpenAI- and Anthropic-compatible API (`serve/server.py`), supervised pair on :8009 |

The point of the rig: a laptop-class CPU, two 2019-vintage Radeon VIIs and an 8 GiB laptop GPU together serve a ~47 GiB MoE model at coding-agent speed.

**ROCm first, CUDA second.** The AMD cards run everything; the NVIDIA card only serves expert rows and prefill rows on request. If the helper dies, the engine recomputes its rows itself and finishes the request — the engine never depends on CUDA.

## What the fork adds over stock

### 1. gfx906 (Radeon VII) backend
- Full HIP wave64 port of the engine, kept compiling through every upstream merge since v0.1.45 → v0.2.10.
- Compat shims: `cudaStreamCreateWithPriority`, cublas→hipBLAS, `__ldg` / fp16 bit-casts, `STRATA_SHARED_ALIGN16_I4` int4 fix.
- Tuned gfx906 knobs in prod configs: `STRATA_GFX906_TOPK_FIT17`, `STRATA_GFX906_TOPK_REG`, `STRATA_HIP_PROMPT_F16`.
- Blackwell-only `mmq_w4a4` and known-broken paths cleanly gated off.

### 2. Mixed-GPU CUDA expert helper — architecture
- Two processes, strictly separated: the HIP engine (AMD-only, **no CUDA runtime mapped**) and `strata-expert-helper` (NVIDIA-only, owns all of that card's expert VRAM).
- Transport: a local socket pair. The helper fills ~4.3–4.6k cache slots (~6.5 GiB) and keeps them filled (fill-mib / slot caps from bench sweeps: **+13.6 %** decode median vs the old 4000-slot cap); it answers with one grouped launch per decode/verify window (≤ 8 tokens).
- Two serving tiers:
  - **decode/verify tier** — expert rows for token windows;
  - **prompt tier** (`STRATA_PREFILL_HELPER=1`) — prefill rows for helper-held experts: the CUDA card does work the AMD cards would otherwise stage from host RAM.
- Relief interplay: helper-held pairs are dropped from the host arena (`--expert-helper-ram-relief`); pack-file reads exist only as the emergency fallback.
- Resilience: helper killed mid-decode → the engine recomputes the claimed rows and finishes the request (measured kill test); `--persist` re-listens for the next engine instead of dying with it; v4 fill supports multi-shard packs; transient-pair fallback instead of hard failures.
- Supervision: `tools/mixed_gpu/pair_supervisor.py` runs engine + helper as one pair — either dies, both restart; a long idle no longer kills the helper.
- Honest attribution: the decode tier alone is ~neutral on this pack (96–97 % local cache hit, arms D/E); the helper's measured value is the prompt tier, expert capacity, and RAM relief.

### 3. Host RAM relief
- `--expert-helper-ram-relief` drops helper-held expert pairs from the anon arena (4116/4116 pairs, 6.24 GiB back).
- Deferred-complement `cudaHostRegister`: arena built unregistered, only kept ranges registered, outward span rounding.
- W1a/W1b fixes: lock-free residency pre-checks + dropped-only prefetch forwarding — relief TG 16.0 → 37.65 tok/s (control 38.05), disk reads 40 GB → 2.31 GB per drive.
- `STRATA_ARENA_COLLAPSE`: opt-in post-fill `MADV_COLLAPSE` sweep (25.05 GiB of the arena collapsed in H1-r5), with a scoped fix for inherited `PR_SET_THP_DISABLE`.

### 4. Speculative decoding — DFlash2 advantages
- **The decode win is DFlash2.** Attribution arm E (drafter off) decodes at stock speed (23.9 vs 23.6 tok/s, same day); drafter on = 33–38.5 tok/s — the entire gain is drafting, at byte-identical output.
- Runs **HIP engine-side on the Radeon VIIs** (stock DFlash2 is CUDA-only) — drafting never touches the helper card.
- Official Flash-Next draft format (inco draft, q4_0 GGUF); Q8/Q5/Q4 draft weights supported — q4_0 measured optimal, q8_0 flat: the ~60–65 % acceptance ceiling is the draft model itself, not its quantization.
- Tuned optimum, sweep fully closed: `--spec 5` / block 5 / q4_0 (spec 7 −3 % TG, block 3 flat, acceptance never bought back by sampling arms).
- Beyond stock drafter behavior:
  - sampled requests get **target exact-match verification** (`STRATA_DFLASH_SAMPLED`) — non-greedy traffic keeps draft guarantees;
  - **bounded SWA draft ring** with honest window defaults and recoverable deep rewinds; `STRATA_DF_RING_MULT` scales the ring so long-context segment resumes stop re-reading the whole context (deep-stall class fixed by construction, speed-neutral);
  - conversation parking keeps the draft image across park/save;
  - per-section timing (`STRATA_DF_EVENTS=1`) dumps the section table after every request;
  - perf: batched KV append with device-owned positions, attention probabilities reused across value lanes, batched BF16 projections, confidence gating for fixed blocks.
- DFlash standalone (the prerequisite layer): GGUF drafter artifacts via `tools/dflash_gguf.py`, target taps [3,15,23,35,43], private draft K/V, block execution + target verification; MTP stays available, mutually exclusive.

### 5. Short-prompt latency
- `STRATA_PREFILL_CPU_SHARE=auto` (promoted): 214 tok **2.4×**, 620 tok **1.8×**, 4k **1.4×**, TG128 −5.1 s, ≥18k unchanged, zero regressions.
- `STRATA_PROMPT_STAGE_MEMO` (promoted): a dropped prompt expert is read once per request — medians **14–20 %** faster across short-prompt legs.
- `--prefill 16384` dose (promoted): measured peak for long-context serving (24k −10 %, 32k −37 %).

### 6. Bench + quality discipline
- `bench/ab-v0210/`: A/B/C arm harness (`run-arm.sh`), traffic driver (`drive.py`), verdict docs, RAM/IO samplers, fail-fast guards, port / VRAM / MemFree boot gates.
- Quality yardstick (`tools/quant_quality.py`): 4995 teacher-forced positions, full-vocab KL / top-1 / PPL vs the stock binary:
  - relief binary vs stock: top-1 100.00 %, KL 0.00000, PPL 4.115 = 4.115;
  - CPU-share and memo knobs: identical perfect scores vs their twins and vs stock.
- Parity gates: greedy 80k-infill SHA byte-identical across fork, fork-flags-off, and stock in every kept round (18/18 in the tap-overlap window alone).
- Standing rule: a new engine version passes BOTH the speed lane and the quality lane before promotion.

## Measured: fork vs stock v0.2.10 (same day, same box, same pack)

| Metric | Fork (prod config) | Stock | Delta |
| --- | ---: | ---: | ---: |
| TG cached decode, median tok/s | 37.7–38.5 | 27.6 | **+36–39 %** |
| 80k-token infill read, tok/s | 553 | 550 | parity (relief on) / **+7.6 %** relief-off |
| Fresh ~18k prefill, tok/s | 272–278 | 337 | **−18 %** (dflash tap-collection cost) |
| Short prompt 214 tok, s | 2.6–2.9 | own flags-off baseline 6.5 | **2.4×** |
| Short prompt 620 tok, s | 6.2 | own flags-off baseline 11.5 | **1.8×** |
| Host anon RAM, GiB | 41.2 | 47.4 | **−6.2** |
| Greedy 80k output | byte-identical | — | ✓ |

Attribution (arms D/E): the decode win is entirely DFlash drafting; the helper expert tier alone is ~neutral on this pack (96–97 % local cache hit) — its value is the prompt tier, expert capacity, and RAM relief.

**Bottom line vs stock:** ≈ **1.4× decode throughput** · **1.4–2.4× chat-read latency** · **−6.2 GiB host RAM** · **byte-identical output** — one regression (**−18 %** fresh ~18k bulk prefill) — on hardware stock does not build for at all.

## Quality gates

Standing rule: every promotion passes BOTH the speed lane and the quality lane, same-day, against stock.

| Gate | What it checks | Result |
| --- | --- | --- |
| 80k greedy SHA | deep-infill (80k ctx, ≥5k out) byte-identical across candidate / flags-off / stock binaries | held **18/18** kept rounds (`1c7d4a6d…`) |
| Teacher-forced yardstick (`tools/quant_quality.py`) | 4995 positions, full-vocab first logits: top-1, KL (p99), PPL — vs stock binary AND vs flags-off twin | top-1 **100.00 %** · KL **0.00000** · PPL **4.115 = 4.115** (relief, CPU-share and memo arms) |
| Probe determinism | fixed prompt re-answered every boot; sha must reproduce exactly per config | reproduces exactly, per-config values documented (incl. coverage-dependent texts) |
| Speed gates | TG median ≥ 33.1 · 80k frame 583–587 · fresh-18k toward 337 · medians over ≥ 2 kept boots/arm | see measured table above |

That is what backs the "faster with zero quality cost" claim: the speed wins above were promoted only after these gates passed.

## Honest trade-offs and known defects

- Fresh ~18k prefill pays −18 % for DFlash tap collection (long-context reads gain instead).
- `#267` gfx906 lost-completion wedge: transient verify-timeout GPU hangs (~1 in 20 long drives, both cards, also on stock 0.1.45); the bench retries the leg once.
- Small fresh-read stalls (30–140 s on ~200-token cold reads): pre-existing, interactive-UX only; watchdog raised 300 → 900 s so it cannot kill prod.
- iq3_s at 256/256 MiB reserves → non-finite logits on long prefill (V8); fixed policy: 640/640 + fill 7000 (V9/V10) keeps both cards above the 512 MiB paging line.
- Not promoted, default off: tap-overlap F1/F2 (flat), `STRATA_REFILL_OVERLAP` / `STRATA_WINDOW_POPULATE` (net-negative on big prompts), `--short-read` window routing (refill poison).
- In flight: f16-KV variants (launchers staged), helper threading measurement, `promo_check` exact-reply traffic probe.

## Locations

| Path | What |
| --- | --- |
| `/home/user/dev/Strata` | dev source tree — branches, `bench/`, `docs/`, `JOURNAL.md` (README canonical here) |
| `/mnt/user/Strata` | **the live Strata** — `launch-prod-*.sh` launchers, `prod-config-*.json`, supervised engine+helper pair on `:8009`, binaries in `v0210/build-mixed-gpu/`, `logs/` |

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
  -DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF -DSTRATA_TESTS=ON
ninja -j11 -C build-v0210/rpcw-hip strata       # -j11 = standing order (git config strata.buildJobs)
```

### Helper — CUDA (RTX 3080 Laptop)

```sh
cmake -S . -B build-v0210/rpcw-cuda -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DSTRATA_ENABLE_CUDA=ON -DSTRATA_BUILD_EXPERT_HELPER=ON \
  -DCMAKE_CUDA_ARCHITECTURES=86 -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
  -DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF -DSTRATA_TESTS=ON
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
| Branches | `shortpp-src` (current), `build-mixed-v0210` (prod line), `stock-v0210-gfx906-buildfix` (stock build fixes) |

## Upstream hygiene

- Upmerges from `origin/main` (v0.1.45 → v0.2.10) kept: upstream machinery wins where it supersedes; fork specifics (helper RPC, prompt tier, dflash runtime, gfx906 gates) re-integrated and depth-verified after every merge.
- Fork features are opt-in flags; the default path stays byte-identical to stock (F1off arm: all shas reproduce, fresh-PP equal).
- Both backends (HIP gfx906 engine, CUDA helper) build before any promotion.
