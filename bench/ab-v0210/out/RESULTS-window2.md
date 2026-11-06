# Window 2 results: K2 adopted — same-day stock comparison + temp-0 shas (2026-11-04)

User order: adopt K2 (`--prefill 16384`), compare against genuine stock Strata with temp-0 sha
checks. Prod config adopted verbatim before the window (backup `.bak-prefill8192`). Arms:

| arm | binary | config |
|-----|--------|--------|
| C | stock v0.2.10 (`ffa80d73` build) | no helper, no drafter, prefill 8192 |
| P0 | v0.2.10 mixed (sha256 = prod engine) | AS ADOPTED: prefill 16384, prompt tier OFF, --vision, helper window tier |

Note: window-1's K2 evidence was measured with the helper prompt tier ON (bench env
`STRATA_PREFILL_HELPER=1` vs prod `0`); P0 re-measures with the adopted env.

## Gates — all held

| round | 80k infill sha | probe sha | pp16384 sha |
|---|---|---|---|
| C r1 | `1c7d4a6d9be118d8` ✓ | `d7e714573946a47b` | `e3b0c44298fc1c14` |
| C r2 | `1c7d4a6d9be118d8` ✓ | `d7e714573946a47b` | `e3b0c44298fc1c14` |
| P0 r1 | `1c7d4a6d9be118d8` ✓ | `707704f68c5947f0` | `e3b0c44298fc1c14` |
| P0 r2 | `1c7d4a6d9be118d8` ✓ | `707704f68c5947f0` | `e3b0c44298fc1c14` |

**Temp-0 parity vs stock is byte-identical on the deep greedy path (80k) in every round**;
probes fork per config (expected — read-path composition) and reproduce within each config.
pp16384's sha is the empty-string sha (the model puts all 8-16 tokens in reasoning) — identical
everywhere, so still a valid determinism marker.

## Numbers (tok/s; PP = fresh 18233 read, TG = cached decode, 80k = fresh 87879 read)

| arm | PP | TG | 80k |
|-----|-----|-----|------|
| C stock r1 | 337.2 | 23.2-24.1 | 534.8 |
| C stock r2 | 339.5 | 22.8-24.1 | 549.0 |
| **P0 r1** | **276.8** | **29.4-40.8** | **582.9** |
| **P0 r2** | **276.5** | **31.9-37.1** | **586.6** |
| (B r5 ref, prefill 8192) | 305.3 | 33.4 | 482.5 |

**K2-adopted vs stock, same day:** 80k **+7.6%** (584.8 vs 541.9 median), decode **+~45%**
(~35 vs ~23.5), fresh-18233 **−18.2%** (276.7 vs 338.4). The prefill-tier mismatch did not
matter: P0's 80k (582.9/586.6) matches window-1's tier-ON K2 (583.7).

## Segment health (the 268 s stall question)

- **P0 r1 and r2 both CLEAN**: the 80k request's second segment restored KV
  (`88025 = 88007 reused + 18 read in ~290 ms`), all tg segments restored
  (`736 reused + 19 read`), zero `no longer retains a rewind` lines.
- The window-1 K2-r2 stall did not reproduce on the adopted config (2/2 clean; total K2-class
  sample now 1-degraded-in-4 rounds).
- Root cause (static analysis + logs, mechanism verified in source): the DFlash2 SWA draft ring
  is 2048 cells (artifact geometry, `dflash_runtime.cpp`); a segment resume more than ~64 cells
  behind the append horizon is stale → the guard re-reads the whole context through the drafter
  (268 s at 80k). NOT config-mitigable (`--dflash-window` cannot enlarge the physical ring for
  the official SWA geometry). Code lever: `STRATA_DF_RING_MULT` (opt-in, default 1) — benched
  as arm P1 in window 3.
- **P0 r2 one-off (new signature)**: the first 80k attempt was cancelled at 87874-of-87879 read
  (`(cancelled)`, 0 generated) and retried cleanly in the same boot (second attempt 580.4 tok/s,
  sha gate-ok; wall 305.9 s ≈ 2 reads). No park/rewind lines involved. Watch item; per user
  policy (rerun-and-ignore) the in-round retry already covers it.

## Hygiene notes

- C's engine logs from the five-arm record were appended to (not truncated): each contains
  yesterday's sequence before today's. Today's numbers above are the last sequence in each log;
  yesterday's C r1 80k (550.4) appears in the r1 judge tail. Harmless, but bench rounds should
  rotate logs.
- C r2's judge txt was lost: run-arm.sh was edited while the window-2 chain was still reading
  it (bash re-reads scripts incrementally → parse error at the judge step). Metrics live in the
  engine logs; nothing was re-run. Lesson recorded: never edit a script its running chain reads.

## Window 3 (levers) — complete (09:22)

**Dose-response — 16384 is the peak; K3/K4 rejected.**

| prefill | chunks @80k | 80k tok/s | fresh-18233 | verdict |
|---|---|---|---|---|
| 8192 (B r5 ref) | 11 | 482.5 | 305.3 | old default |
| **16384 (P0)** | 6 | **582.9 / 586.6** | 276.8 | **adopted — peak** |
| 24576 (K3 r1) | 4 | 527.5 | 321.0 | reject: 80k −10% |
| 32768 (K4 r1) | 3 | 367.0 | 272.3 | reject: 80k −37% |

The amortization gain saturates past 16384 and reverses — per-chunk serial work grows faster
than the chunk-count saving. K3's PP 321 (18233 fits one 24576 chunk) is single-round and does
not outweigh its 80k loss. Gates held in both (80k sha `1c7d4a6d…`; zero rewind-loss).

**P1 = ring-mult 8 (the stall mitigation, benched):** speed-neutral everywhere (80k 576.1 vs
582.9-586.6; PP 272.9; TG 30-37; sha gate held; pools fit VRAM; zero rewind-loss lines; the
80k second segment restored). The deep-resume stall class is fixed by construction at mult 8
(safe resume depth ~14.3k cells ≥ chunk-16k drift). NOT deployed to prod autonomously: prod
keeps the genuine v0210 binary; the stall did not reproduce in either adopted-config round
(P0 2/2 clean). Ready mitigation if it bites: swap prod engine to
`build-tap-overlap/rpcw-hip/strata` (cf653943+, flags default-off, F1off-gated byte-identical)
and set `STRATA_DF_RING_MULT=8` in the prod config env.

**Residual (accepted):** the tg-scale segment flicker (a second segment finding no exact
checkpoint at its resume → re-reads ~535 tokens at ~90 tok/s ≈ 6 s) is state-dependent
(K2-r2 4-of-4, K3 1-of-3, P1 1-of-4, P0/K4 0) and is NOT fixed by ring-mult — it is
checkpoint placement, a separate small lever, left on the table. The P0-r2 80k
cancel-and-retry one-off (wall ≈ 2 reads, retried clean, sha held) is a watch item.
