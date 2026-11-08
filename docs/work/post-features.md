# Strata gfx906 mixed-GPU fork — achievements at a glance

*~47 GiB MoE (Swift-1.5 / Qwen3.8-Flash-Next) as a daily server on 2× Radeon VII (gfx906, 2019) + one 8 GiB NVIDIA laptop card.*

**What base Strata can't do — this fork does**

- **Runs on gfx906 at all** — full HIP backend, built and served through every release v0.1.45 → v0.2.11 (stock: compile-check only, doesn't build as tagged)
- **GEMM works** — custom single-target rocBLAS replaces the stock library that segfaults on gfx906 on first inference
- **Mixed AMD + NVIDIA serving** — the NVIDIA card serves expert/prefill rows to the AMD engine over a socket; stock has no cross-vendor path
- **Speculative decoding on HIP** — DFlash2 drafter runs on the AMD cards (stock: CUDA-only), with exact-match verification for sampled requests, not just greedy
- **Self-healing production pair** — SIGKILL the helper: requests finish anyway, supervisor rebuilds the pair, health returns; no human touch

**Speed (vs same-day stock, exclusive box, gated)**

- **+49 % decode** — 34.7 vs 23.3 tok/s pooled median; 1.3× on 1024-token completions
- **1.4–2.4× short-prompt reads** (200–600 token agent traffic; ≥18k prompts unchanged)
- **−6.2 GiB host RAM** — helper-held experts dropped from the host arena
- Honest cost on record: fresh ~16k prefill reads −12 %; long-context reads gain instead

**Quality (proven, not assumed)**

- Greedy 80k-token infill **byte-identical to stock**
- Teacher-forced gate, 4995 positions: **top-1 100 % · KL 0.00000 · PPL equal**
- Full test suite green on fresh builds (291 tests)
- Every speed win promoted only through both the speed lane and the quality lane

**Engineering**

- Latest upstream (v0.2.11) with all fork features intact and re-verified after every merge
- A/B/C-vs-stock bench harness, sha parity gates, quality yardstick, full session journal — all in-repo, anyone can re-run every claim
- OpenAI + Anthropic compatible API, supervised, generation-tracked
