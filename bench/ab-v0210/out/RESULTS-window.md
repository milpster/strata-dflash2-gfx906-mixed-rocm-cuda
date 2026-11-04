# Window results: DFlash tap-overlap prototype + knobs (2026-11-04)

Prod-down window, user-authorized ("take prod down as u see fit; bring back up"). Same stack as
the five-arm bench: 2x Radeon VII (gfx906, ROCm 6.1.0 shim) + RTX 3080 helper, Swift-1.5 IQ3_XXS
native, dflash q4_0 block 5, layer split 32, spec 5, prompt tier off. Medians not means; every
arm one at a time; the 80k prompt is round-independent so its sha compares across everything.

## Arms

| arm | binary | delta vs B |
|-----|--------|------------|
| F0 | old | + STRATA_DF_EVENTS=1 (decomposition probe only) |
| F1off | new (tap-overlap-f1f2) | none (flags off — the byte-identity gate) |
| F1 | new | + STRATA_DF_DEFER_CTX=1 |
| F2only | new | + STRATA_DF_ASYNC_D2H=1 |
| F2 | new | + both |
| K1 | old | layer split 16 |
| K2 | old | --prefill 16384 |
| B r4/r5 | old | none (same-day anchors; r4 died mid-round, see below) |

## Gates — all held where required

- **80k infill sha `1c7d4a6d9be118d8` in every round of every arm (18/18)**, including the new
  binary, both prototype flags, and both knob configs.
- Probe (fixed corpus, per-config): F0/F1off/F1/F2/F2only/B-r5 all reproduce `1cb480ac086efd06`;
  K1 forks to `819e8c1ecb1200cc`, K2 to `b9ba1310e601d2e3` — each reproduces across its rounds
  (per-config determinism; cross-config forks are expected under different scheduling/splits).
- TG gate (>= B's 33.1, drafts non-degraded): PASS F1off (32.4, drafts clean), F2only (31.6,
  clean), K2 (33.1, clean). FAIL F1 (27.3) and F2 (27.1): cached-rerun drafting collapses to
  `drafts accepted 0 of 0` (F1 r1: 1x; F1 r2: 3x; F2 r1: 2x — dose follows the defer flag).

## Numbers (tok/s; PP = fresh 18233 read, TG = cached decode, 80k = fresh 87879 read)

| arm | PP | TG | 80k |
|-----|-----|-----|------|
| B historical (r1/r2) | 305.4 | 33.1 | 454.0 |
| **B r5 (same-day anchor)** | **305.3** | **33.4** | **482.5** |
| F0 (DF_EVENTS) | 299.8 | 32.1 | 288.1 (instrumented — decompose only) |
| F1off | 305.7 | 32.4 | 482.5 |
| F1 | 306.0 | 27.3 FAIL | 484.0 |
| F2only | 305.9 | 31.6 | 482.5 |
| F2 | 305.5 | 27.1 FAIL | 483.5 |
| K1 (split 16) | 135.1 | 24.5 | 225.9 |
| **K2 (prefill 16384)** | **276.1** | **33.1** | **583.7** |
| C stock (historical) | 337.2 | 23.6 | 550.4 |
| E no-dflash (historical) | 337.6 | 23.8 | 541.6 |

## Verdicts

1. **F1 (defer consumer fusion) does not ship.** Prefill zero delta (306.0 vs 305.7 PP, 484.0 vs
   482.5 80k — the deferred drains relocate to propose()/idle(), the serialization is not removed)
   AND it breaks the decode gate: after cached-rerun prefills the drafter proposes nothing (0-of-0)
   until the deferred drain lands — decode drops to ~24.5 tok/s on those runs. Likely mechanism
   (from the log pattern, unconfirmed in code): propose() sees unconsumed context and refuses.
2. **F2only (async D2H) does not ship.** No gain (flat on both prefill metrics), no harm (TG
   clean). The mutex-held blocking 120 MiB/chunk D2H is NOT the serializer either.
3. **F2 (both) does not ship.** Flat + inherits F1's TG collapse.
4. **K1 (split 16) decisively rejected.** 2.26x slower PP, 2.1x slower 80k, TG 24.5 — layer
   imbalance (16 vs 48 layers across the VIIs) dwarfs any tap-volume saving.
5. **K2 (prefill 16384) is the one positive finding.** 80k 583.7 = **+21.0%** vs same-day/same-
   binary B r5 (482.5), confirmed across two rounds (581.8/585.6); ~parity with stock's
   historical 80k (550.4, and today's session ran the 80k metric ~6-11% fast — see frame below)
   **while keeping dflash drafting** (TG 33.1 vs stock's 23.6). Costs: fresh-18233 276.1 (-9.5%
   — fewer-but-fatter chunks lose at 18k) and a ~3-6% tax on small-prompt prefill. Caveat: one
   unreproduced post-80k follow-up stall (151 tokens at 0.6 tok/s for 268 s, K2 r2 only; r1 and
   every other arm clean).

## Environment frame (why 80k numbers moved vs the five-arm record)

The 80k metric ran faster today across the board: old binary B r5 = 482.5 vs its historical
median 454.0 (r1 454.0, r2 435.9); the new binary flags-off cluster (F1off/F1/F2/F2only)
482.5-484.1 is identical to the old binary's same-day 482.5, and fresh-18233 did NOT shift
(305.3-306.2 vs 305.4-305.7 historical). So: session-level shift on the 80k metric only, and the
two binaries are perf-equivalent where it matters (F1off's fresh-PP 305.7 also matched B's
historical median exactly). All window comparisons above are same-day.

## Findings beyond the arms

- **B r4 died mid-round** to the engine's known `#267` class ("verify: timed out at layer 46; its
  GPU waits were released but the GPU did not finish within 5 s") — generation decayed 41 -> 1.4
  tok/s, watchdog killed the engine, serve 503'd the in-flight request. First occurrence in ~20
  bench rounds + the five-arm record + 5h51m of prod serving. Old binary — not prototype-related.
  GPUs verified healthy after (35-37 C idle, no RAS errors); the retry round (r5) was clean.
- The tap serial cost responds to **chunk count** (K2), not to deferring or async-copying the
  per-chunk work (F1/F2): the per-chunk serial section is not overlap-removable at these sizes —
  it has to be amortized. That is the upstream-relevant conclusion of the window.
- STRATA_DF_EVENTS' section table only dumps at drafter teardown; the harness's teardown kill
  suppressed it (F0's table lost). Its absolute numbers were never usable (per-batch syncs) — no
  re-run spent.

## Recommendation

- **Defaults unchanged, nothing ships.** The prototype branch stays parked (`tap-overlap-f1f2`,
  commits through 2fb73f23 + the two build fixes 66bbaba6/04337556); both flags remain default-
  off and the flags-off path is output-byte-identical (F1off gate).
- **K2 is the one adoptable knob**, opt-in for long-context-heavy serving: ~+21% on 80k-class
  infill for ~-10% on 18k-class fresh prompts, decode unchanged. Owner's call; the unreproduced
  268 s follow-up stall should be watched if adopted (it is a config flag, `--prefill 16384`,
  not a code change).
