# Short-prompt PP / TG↔PP turn-around — the T-lane (2026-11-05, 01:50–03:00)

Setup: bench armV9 lineage (iq3_s pack, helper relief 4600 slots, dflash spec 5, prefill 16384),
engine 331c2e89d00dd4f1 / helper afbf19f76e39ac16, 2× Radeon VII + RTX 3080, prod down for the window.
Probe: fixed deterministic prompts (unique marker per request so caches cannot mask the fixed cost),
max_tokens 8, 3 rounds each; TG leg = 620-token prompt + 128 generated. All arms carry STRATA_TRACE=1
(constant overhead). Server-side numbers from the engine trace; client walls include HTTP+JSON.

## Measured baseline (T1)

| prompt tokens | batched read | rate | refill after | client wall |
|---|---|---|---|---|
| 185 (S214)   | 5.6–7.9 s | 23–33 tok/s | 0.53 s | 6.5–8.8 s |
| 620 (S1k)    | 10.4–10.5 s | 60 tok/s | 0.72 s | 11.5 s |
| 2325 (S4k)   | 20.6 s | 113 tok/s | 1.85 s | 23.0 s |
| 10139 (S18k) | 32.5 s | 313 tok/s | 5.83 s | 39.0 s |
| 55 (warmup, windows path) | 0.98 s | — | none | 1.34 s |

- The batched path has a ~3.6 s intercept + ~11 ms/token early marginal (improving ~7× with size):
  short prompts pay ~10× more per token than bulk.
- The in-request TG↔PP seam is NOT the problem: decode starts <1 s after the read, warms 20→42 tok/s.
- Refill scales ~1.27 ms per lent slot. Checkpoints ~95 ms. First-ever request +2.2–2.3 s.

## Arm verdicts

| Arm | Delta vs T1 | Verdict |
|---|---|---|
| T1b (RPC profile) | per-layer helper claims: d2h 30–160 µs, begin 0.4–0.9 ms, reply 2.1–6.3 ms → ~0.2–0.3 s/request | **Helper claims are NOT the whale.** The ~10.3 s short-chunk read lives in layer compute/staging. (Instrumentation free on short legs; 18k legs contaminated.) |
| T2c (short-read 256) | **S214: 6.5 → 2.9 s (2.2×)**; S1k/S4k wash; S18k r1/r2 +5.7 s; TG128 14.2 → 57.8 s (refill 0.72 s → 29.8 s = 41×, pack-FILE fallback) | **Value proven, delivery fatal.** Window reads do not populate the arena; later refills re-read from drives. Not promotable solo. |
| T5 (SPLIT_SMALL 1024/4096) | S1k −0.3 s (refill gone, `lent 0 slots`); S4k 23 → 37/37/171 s; checkpoint saves balloon to 2.8 s | **Not promotable.** No-refill confirmed real; 1024-chunk geometry destroys 2–4k prompts. |
| T6c (short-read 256 + SPLIT_SMALL 512/1024) | S214 2.9–3.2 s holds; TG128 15.4 s (poison GONE — no refill exists); S1k +2 s; S4k +1–3 s; **first big prompt after small traffic 127 s (checkpoint save 48 s = arena/checkpoint restaging from drives)** | **Not promotable.** Fixes the refill poison, exposes the next layer: restaging catastrophes after mixed traffic. |
| T3 (PIPE) | all legs within noise of T1 (S214 6.5 s, S1k 11.5 s, S4k 22.5 s, S18k 38.4 s, TG128 14.2 s; 620-tok read 10200 vs 10268 ms) | **Clean null** — the helper claim lane was never the bottleneck, exactly as the T1b profile predicted. |
| T4 (CPU share=auto) | **S214 5.6/2.9/2.6 s (2.4×), S1k 6.2–6.4 s (1.8×, read 10268→5261 ms), S4k 15.8–16.2 s (1.4×), TG128 9.1 s (−5.1 s), S18k unchanged 38.9 s**; refills/checkpoints byte-identical, no outliers | **THE WINNER — promotable, config-only.** Host-CPU staging of short chunks is a straight win ≤3k tokens, neutral above; upstream-documented KL ≈ 0.004 and default-on where measured. |

## Promotion

`STRATA_PREFILL_CPU_SHARE=auto` promoted to both prod configs (iq3_s live + swift15) — chat 2.4×,
1k 1.8×, 4k 1.4×, long prompts and TG non-regressing. T2c/T5/T6c window-routing stays UNPROMOTED
(source-level arena work needed); T3 null; inert env cleanup (PRIMARY_COMMIT_OVERLAP/NO_PCIE_GROUP)
left for the next conf touch.

## Quality gate for the promoted knob (2026-11-05 03:2x)

`qqS` (relief binary + pack iq3s + `STRATA_PREFILL_CPU_SHARE=auto`) vs the share-off twin `qqA`
and vs the stock anchor `REF`, 4995 teacher-forced positions, yardstick-pinned `--prefill 512`
(every scored chunk exercises the share path):

```
S vs A:   all 4995 100.00%  0.00000  0.0000  4.115 4.115
S vs REF: all 4995 100.00%  0.00000  0.0000  4.115 4.115
```

The promoted config is logprob-identical to share-off and to the stock binary on this workload.
Artifacts: `bench/ab-v0210/quality/out/S.tsv{,.seg.json,.run.log}` (commit 60e7afaa).

## Conclusions (final)

1. Chat-sized prompts can be **2.2× faster** (6.5 → 2.9 s) by window-routing them — proven twice
   (T2c, T6c), stable across rounds.
2. Every config-knob delivery of that win poisons another traffic class through arena/checkpoint
   state: refill-from-files (T2c), bad 2–4k geometry (T5), restaging catastrophe (T6c).
3. The remaining costs are source-level (the T6 engineering family):
   - window-path reads should populate the arena (kills the refill/restaging disease at its root);
   - the ~3.6 s batched short-chunk intercept + 11 ms/token early marginal (layer compute/staging);
   - refill (0.5–5.8 s) could overlap with the first decode window instead of blocking.
4. Bonus (from the source map): `STRATA_PRIMARY_COMMIT_OVERLAP` and `STRATA_NO_PCIE_GROUP` in the
   prod env are no-ops in current builds (no `getenv` in `src/`) — cosmetic cleanup only.

## Artifacts

- Configs/prober/runner: `bench/ab-v0210/shortpp/` (commits 32fcc9c4, a69e7898, 8f9469e3)
- Logs: `bench/ab-v0210/logs/{T1,T1b,T2c,T5,T6c,T3,T4}-r1-{serve,engine,helper}.log`
- Probe data: `bench/ab-v0210/shortpp/out/*/probe.jsonl`
