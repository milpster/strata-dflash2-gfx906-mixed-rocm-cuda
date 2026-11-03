# The pre-helper product versus today's mixed product

Question: does the mixed backend (two Radeon VII for the engine, an RTX 3080 running the
CUDA expert helper) return the same text as the product users ran before the helper
existed, on the same requests? Earlier windows (how1, how2) turned the helper off inside
the mixed engine. This window runs the REAL pre-helper product next to the REAL
production-mixed product.

Measured on the production machine: two Radeon VII (gfx906) run the engine in a 32-layer
split; the mixed product's expert helper runs on the RTX 3080. Engine 0.1.48. Window
family `sab3`, 2026-11-03, about 14 minutes of GPU time.

## What ran

Three arms, one after another, each a fresh engine load, each serving three requests:

- Arm a and arm c: the pinned pre-helper product. The `build-dfsample-1` engine
  (sha256 `15c8d0b7…`), the integration clone's server (sha256 `7199ce6b…`), the stock
  config and launcher. No helper at all: no helper sockets, no `--expert-helper-slots`,
  no `STRATA_PREFILL_HELPER`. A private launcher derived from the real stock launcher
  ran it under the window supervisor on a private port.
- Arm b: the production-mixed product, unchanged: the `rpcw-hip` engine
  (sha256 `1c27a00d…`), the worktree server, the `rpcw-cuda` helper (sha256 `c35f4a85…`),
  4200 helper slots (filled 4093, 6.25 GiB), both products with FP16 prompt GEMMs on
  (`STRATA_HIP_PROMPT_F16=1` - so that override is NOT the difference measured here).

The three requests are the frozen `ppf1` bytes, replayed byte-identically on every arm:
three distinct prompts of 10,465 tokens, 512 new tokens each, temperature 0, thinking
off. The runner hard-checks that the request bytes match across arms for every
repetition. Every arm also wrote the logits of each request's first verify window to
disk (`STRATA_DUMP_FIRST_LOGITS`, one row of 248,320 raw floats per request - the row
that picked the first returned token) so the two products' first decision could be
compared by KL divergence offline.

## Results

Text identity (sha256 of the returned message):

| comparison | rep 1 | rep 2 | rep 3 |
|---|---|---|---|
| stock a vs stock c (two fresh loads) | equal | equal | equal |
| mixed b vs stock controls | differs | differs | differs |

The pre-helper product returned byte-identical text from two independent engine loads
on all three requests, and its first-window logit rows were bit-identical (every float
equal). The mixed product differed from stock on all three requests. Verdict recorded
by the harness: `OUTPUT_MISMATCH` with a stable baseline.

First-window logits, mixed versus stock (mean over the three requests; the two stock
arms are bit-identical, so both stock-versus-mixed rows are the same numbers):

| measure | value |
|---|---|
| KL(stock ‖ mixed), forward | 0.0037 nats |
| KL(mixed ‖ stock), reverse | 0.0028 nats |
| largest single-logit difference | 2.34 |
| L2 difference over the 248,320 logits | 182 |

So the divergence is present in the very first decision the engines make, before any
drafting or helper traffic can accumulate: tiny in KL, but with individual logits
differing by up to ~2.3, which greedy (temperature 0) decoding eventually turns into
different text.

Speed (recorded, not evidence: the logit dump's device sync perturbs scheduling, and
both deltas are inside the ±5% bar anyway):

| arm | decode tok/s (median) | prompt tok/s (median) |
|---|---|---|
| stock controls (6 probes) | 36.0 | 237.9 |
| mixed | 36.7 (+1.9%) | 230.7 (−3.0%) |

Control drift between the two stock arms: decode 1.000, prompt 1.0004. No speed claim.

The mixed arm's helper ran clean the whole window: 6905–6974 helper requests per probe,
zero timeouts, zero fallbacks, the prompt channel closed once with 192 requests /
46,944 rows. Fallback noise is excluded as the cause of the mismatch.

## What this settles, and what it does not

Settled (measured, this window):

- The stock product is deterministic here: two fresh loads, identical text and identical
  first-window logits, three of three requests. The earlier unstable control (ppf1's
  rep 2, a mixed-versus-mixed control) does not describe the stock product.
- The mixed product's text differs from the stock product's on the same bytes, three of
  three, and the difference exists from the first verify window's logits onward.

Not settled:

- Which difference causes it. This is a product-level comparison: the engine build, the
  server and the helper all differ together (the mixed engine computes some experts on
  the RTX 3080; the stock engine computes everything on the two Radeon VIIs). No single
  knob is isolated, and no root cause is claimed.
- Numerical acceptance. The pooled numerical relative-error gate remains FAIL; text
  equality and first-window KL are additional evidence, not a replacement for it.
- Anything about other prompts, sampling temperatures, or longer generations.

## Limits and artifacts

Three prompts, one machine, one day, both products with FP16 prompt GEMMs on. The stock
server may ignore the per-request seed (greedy at temperature 0); its measured
determinism shows that did not matter here. Timings are from an instrumented window and
are not gain evidence.

Everything is retained under `build-mixed-gpu/cap1-validation/sab3-*`: the status
record, per-arm configs/launchers/logs, per-repetition request and reply files, and the
per-arm first-logit rows (`sab3-{a,b,c}-first-logits.{0,1,2}`). The two failed attempts
are retained too: `sab1-*` (a harness path bug, fixed in `33d5c90c`) and `sab2-*`
(cancelled by a reboot two arms in). No settings were adopted; production was restored
and independently re-audited after the window.
