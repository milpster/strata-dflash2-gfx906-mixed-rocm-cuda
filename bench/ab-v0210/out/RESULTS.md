# v0.2.10 sync — bench verdict (2026-11-03)

Machine: 2× Radeon VII (gfx906, ROCm 6.1) + 1× CUDA card (expert helper). Model: Swift-1.5B on iq3xxs-15 pack.
Arms (all same corpus, port 8019, identical sampling; PP 16384 / TG 1024×3 / 80k infill; probe = temp-0 first request on a fresh boot):

- **A** — prod build at takedown (worktree `cuda-helper` @ 872e3dac, frozen copy in `armA/`), dflash + helper prompt tier
- **B** — merged v0.2.10 + our mods (`build-mixed-v0210`), dflash + helper prompt tier
- **C** — stock v0.2.10 tag `ffa80d73`, no helper, no drafter (compile-only fixes: branch `stock-v0210-gfx906-buildfix`)
- **D** — B with `STRATA_PREFILL_HELPER=0` (helper prompt tier off; tap collection on)
- **E** — B minus `--dflash` (tap collection/drafter off; prompt tier on)

## Correctness

- **80k infill sha `1c7d4a6d9be118d8` — byte-identical in all 7 runs across all five arms.** This is the parity gate; the deep greedy path is exact everywhere, including stock.
- Probe shas: A `ab1b87dceccd74f5` (4/4 boots), B `96e31a1e6eb19d14` (2/2), D `e36237606e821d91`, E `ab1b87dc…` (== A), C `059227cf1c1746d5`. Per-config deterministic, but **read-path sensitive**: B vs D differ on identical binaries, A == E across different builds. Probe = determinism check only, not a cross-config gate. Divergences are single near-tie token flips deep in the thinking tail (e.g. "prompt"/"question" at char 176).
- Helper lane healthy all rounds: 0 timeouts / 0 fallbacks.

## Speed (medians)

| tok/s | A prod | B merged | C stock | D =B−prompt tier | E =B−dflash |
|---|---|---|---|---|---|
| PP fresh 18233 | 305.8 | 305.7 | 337.2 | 306.7 | **337.6** |
| TG cached 1024 | 35.0 | 33.1 | 23.6 | 32.8 | 23.8 |
| 80k infill read | 458.9 | 444.9 | 550.4 | 483.0 | 541.6 |

## Attribution (the "why is our PP slow" answer)

- **Fresh PP: the whole ~31 tok/s gap vs stock is dflash tap collection** (E recovers it 100%: 337.6 vs 337.2). The helper prompt tier is ~free on fresh reads (D ≈ B).
- **80k read: tap collection ≈ 2/3 of the gap** (E +97 vs B), **prompt tier ≈ 1/3** (D +38). Per-chunk serial cross-GPU staging on CUDA1 is the cost.
- **Decode: the +48% (35 vs 23.6) is entirely dflash drafting.** E (no drafter, helper tier on) = 23.8 = stock — the helper's expert tier adds nothing on this pack (96-97% local cache hit; it only served the miss tail).
- Trade: serial tap collection buys the decode win. Future optimization target = async/overlapped tap staging.

## Anomalies / notes

- One D run hung at the 8192-token chunk boundary (GPUs 0%, 243 s) — transient gfx906 hiccup; clean on relaunch. Config was valid (`=0` is a true off: `std::atoi(env)==1` gate).
- Merge fixes this bench validated: `4436fcfc` tap-plane loan sizer (g_n_taps term), `b9492a7e` brace-drift closers + df2 guards, `e32fca1d` IlQ8_0 traits.
- Never compile during a bench round (a parallel stock build starved one A2 attempt → 300 s engine-silence watchdog; round discarded and rerun clean).

## Reproduce

    bench/ab-v0210/run-arm.sh A|B|C|D|E <round> [tg_runs]   # one arm at a time, needs both GPUs free
    python3 bench/ab-v0210/report.py                        # regenerates out/report.txt

Raw data: `out/{arm}-r{n}.json` (walls/shas/text), `out/{arm}-r{n}-judge.txt` (engine speed lines), `logs/` (full serve/engine/helper logs). Scope notes in `scope.txt`.
