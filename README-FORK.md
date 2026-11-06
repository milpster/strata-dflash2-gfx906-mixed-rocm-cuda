# Strata — gfx906 Mixed-GPU Fork

An engine fork of [Niko1221/Strata](https://github.com/Niko1221/Strata) (upstream **v0.2.10**) that runs **Qwen3.8-Flash-Next ("Swift 1.5")** on hardware stock Strata does not support — and beats stock on it.

- **Runs where stock cannot**: stock v0.2.10 does not compile for AMD gfx906 (Radeon VII) as tagged; this fork builds and serves on it.
- **Faster decode**: **+36–39 % tok/s** vs same-day stock (37.7–38.5 vs 27.6 tok/s; +48 % in the widest same-day frame).
- **Faster chat**: **1.4–2.4×** faster short-prompt reads (214 / 620 / 4k tokens) plus **14–20 %** more from the prompt-stage memo.
- **Less RAM**: **−6.2 GiB** host memory via expert-helper RAM relief, at control speed.
- **Bit-exact quality**: greedy 80k-token infill byte-identical to stock; teacher-forced gates logprob-identical (KL 0.00000, top-1 100 %, PPL 4.115 = 4.115).
- **Measured or it didn't happen**: every claim comes from a same-day A/B/C bench window; artifacts in `bench/ab-v0210/out/`, the story in `JOURNAL.md`.

## The rig it is built and measured on

| Part | Role |
| --- | --- |
| 2× AMD Radeon VII (gfx906, HIP, ROCm 6.1) | main engine, layer-split over both cards |
| 1× NVIDIA RTX 3080 (CUDA, sm_86) | `strata-expert-helper`: expert-row server + prefill prompt tier (~6.5 GiB expert cache) |
| 62 GiB host RAM | engine anon ~41 GiB after relief (~34 GiB registered) |
| Model | Swift-1.5-Qwen3.8-Flash-Next, GSQ-RCO IQ3_XXS / IQ3_S packs |
| Drafter | DFlash2 (inco draft, q4_0), `--spec 5`, engine-side on the VIIs |
| Vision | `strata-vision` sidecar, Swift BF16 mmproj |
| Server | OpenAI- and Anthropic-compatible API (`serve/server.py`), supervised pair on :8009 |

## What the fork adds over stock

### 1. gfx906 (Radeon VII) backend
- Full HIP wave64 port of the engine, kept compiling through every upstream merge since v0.1.45 → v0.2.10.
- Compat shims: `cudaStreamCreateWithPriority`, cublas→hipBLAS, `__ldg` / fp16 bit-casts, `STRATA_SHARED_ALIGN16_I4` int4 fix.
- Tuned gfx906 knobs in prod configs: `STRATA_GFX906_TOPK_FIT17`, `STRATA_GFX906_TOPK_REG`, `STRATA_HIP_PROMPT_F16`.
- Blackwell-only `mmq_w4a4` and known-broken paths cleanly gated off.

### 2. Mixed-GPU CUDA expert helper
- `strata-expert-helper` on the NVIDIA card serves decode/verify expert rows to the HIP engine over a socket pair.
- Prompt tier: prefill rows for helper-held experts computed on the CUDA card (`STRATA_PREFILL_HELPER=1`).
- `--persist` re-listens across engine restarts; v4 fill supports multi-shard packs; transient-pair fallback instead of hard failures.
- Pair supervisor: engine + helper run as one supervised pair — either dies, both restart.

### 3. Host RAM relief
- `--expert-helper-ram-relief` drops helper-held expert pairs from the anon arena (4116/4116 pairs, 6.24 GiB back).
- Deferred-complement `cudaHostRegister`: arena built unregistered, only kept ranges registered, outward span rounding.
- W1a/W1b fixes: lock-free residency pre-checks + dropped-only prefetch forwarding — relief TG 16.0 → 37.65 tok/s (control 38.05), disk reads 40 GB → 2.31 GB per drive.
- `STRATA_ARENA_COLLAPSE`: opt-in post-fill `MADV_COLLAPSE` sweep (25.05 GiB of the arena collapsed in H1-r5), with a scoped fix for inherited `PR_SET_THP_DISABLE`.

### 4. Speculative decoding (DFlash / DFlash2)
- Standalone DFlash block drafter: GGUF draft weights (Q8/Q5/Q4), greedy drafting, per-section timing (`STRATA_DF_EVENTS=1`).
- DFlash2 (official Flash-Next format) built as HIP on gfx906 — the drafter runs on the Radeon VIIs.
- `STRATA_DF_RING_MULT`: scales the SWA draft ring so long-context segment resumes stop re-reading the whole context (deep-stall class fixed by construction; parked as ready mitigation).
- Verified optimum: spec 5 / q4_0 draft / block 5 (q8_0 draft flat, spec 7 −3 %, block 3 flat).

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

## Honest trade-offs and known defects

- Fresh ~18k prefill pays −18 % for DFlash tap collection (long-context reads gain instead).
- `#267` gfx906 lost-completion wedge: transient verify-timeout GPU hangs (~1 in 20 long drives, both cards, also on stock 0.1.45); the bench retries the leg once.
- Small fresh-read stalls (30–140 s on ~200-token cold reads): pre-existing, interactive-UX only; watchdog raised 300 → 900 s so it cannot kill prod.
- iq3_s at 256/256 MiB reserves → non-finite logits on long prefill (V8); fixed policy: 640/640 + fill 7000 (V9/V10) keeps both cards above the 512 MiB paging line.
- Not promoted, default off: tap-overlap F1/F2 (flat), `STRATA_REFILL_OVERLAP` / `STRATA_WINDOW_POPULATE` (net-negative on big prompts), `--short-read` window routing (refill poison).
- In flight: f16-KV variants (launchers staged), helper threading measurement, `promo_check` exact-reply traffic probe.

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
