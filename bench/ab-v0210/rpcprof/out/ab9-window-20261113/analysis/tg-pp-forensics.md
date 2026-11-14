# TG/PP forensics — ab9 window 2026-11-13 (log mining + arithmetic only, no GPU contact)

Data: `panel-progress.log` (30 leg records, 6 arms) × byte-offset slices of `engine-*.log`
(r2-v0232b, v1-v0231def, v2-v0231depth64, w3-v0230anchor, w4-v0230park, w5-v0230fill7400),
61 generation requests with full counter lines. Journal round-17 section for base rates
(2.1 GiB/s NVMe ceiling, ~40 GiB unique experts, read-once designs A/B).

Core derived identity (self-consistent on all 61 requests): **verify windows = generated − accepted**
(tokens = windows + accepted), so `ms/window = gen_ms/windows`, `tok/window = gen/windows`,
`tok/s = (tok/window)/(ms/window)`.

---

## 1. TG step-time budget per regime

### 1.1 Observed windows (n=61 gen requests, windows ≥ 30)

| ctx regime | hot ms/win | typical ms/win | stalled ms/win | hot tok/win | hot TG | typical TG |
|---|---|---|---|---|---|---|
| small (<2k, tg1024) | 80.5–87.8 | 83–91 | 119.9 | 2.5–3.2 | 28.8–38.4 | 28.8–32.1 |
| 29k (longtg) | 68.7–92.6 | 87–112 | 119.8–172.5 | 2.4–3.5 | 27.1–40.7 | 25.0–36.8 |
| 142k (killer gen + killer144kL) | 76.8–93.9 | 90.6–125.8 | 137.3–402.1 | 2.1–3.6 | 27.8–39.5 | 21.2–32.1 |

**Hot window time is ctx-independent**: 76.8–93.9 ms at 142k vs 80.5–87.8 ms at <2k. Attention/KV
over 142k ctx costs ≤ ~10 ms/window. The 12.0 / 7.5 / 10.0 tok/s killer gens (R2/V1/W4, 280–402
ms/window) are **not** a 142k-ctx problem — they are state stalls; W5 ran the identical killer gen
at 119 ms/window (30.5 tok/s) with byte-equal miss counters.

### 1.2 Spec-decode arithmetic (nominal spec=5)

| quantity | observed |
|---|---|
| acceptance acc/drafted | median 70.7%, p10–p90 61.6–81.7% |
| drafted/window d | mean 2.58, max 3.26 (drafter fills only ~52–65% of the 5 budget) |
| tok/window = 1 + a·d | mean 2.84, max 3.68 |
| windows = gen − accepted | 37–192 per request, consistent everywhere |

### 1.3 Where a window goes (hot typical window ≈ 85 ms at any regime)

| component | estimate | basis |
|---|---|---|
| base window (draft fwd + verify compute + ~33 helper round-trips) | **~69–80 ms** | hottest windows (68.7–81.6 ms) still carry hit 94–98% and 190–263 rows/window over PCIe — the floor *includes* today's miss path |
| expert miss path (bandwidth view) | ≤ 1–3 ms | 250±40 rows/window × 10.0 KiB/row = 2.0–2.8 MiB/window; at 1–2 GiB/s effective |
| expert miss path (latency view) | 0 – ~65 ms | same traffic in ~33 helper reqs/window (hreq/w is pinned at 32.0–33.8 in *every* request); 33 × 0–2 ms serialized RTT |
| decode-phase adapt swap-in | 0.5–9.8% of gen (median 1.8%) | small post-summary swap-in lines, 1.00–1.24 ms/slot, 24–1518 ms per request |
| unattributed stall | 0 – +320 ms/window | the spread between hot and stalled passes, with hit%/rows/hreq all flat |

Counter headline: hit rate (93.3–98.3%), rows/window (190–292), helper reqs/window (32–34) are
nearly constant across all 61 requests while ms/window varies **4.5×** (68.7→402.1). The exposed
counters are rate-blind: none of them sees the stall. Stalled passes are exactly the first gen
after a big prefill storm in a boot (killer gens of R2/V1/W4; W4's killer144kL gens) — consistent
with page-cache/swap fallout of a 370 GiB stream + park writeback (W4 = park arm is the slowest),
not with any expert-cache quantity.

---

## 2. Cross-arm sensitivity

### 2.1 Regressions (n=61, OLS)

| model | result | R² |
|---|---|---|
| ms/win ~ hit% | −7.57 ms per hit point | 0.025 |
| ms/win ~ rows/win | +0.84 ms per row | 0.091 |
| ms/win ~ helper-req/win | +33.6 ms per req | 0.070 |
| ms/win ~ log10(ctx) | +16.1 ms per decade | 0.128 |
| ms/win ~ tok/win | +53.2 ms per tok | 0.143 |
| ms/win ~ hit + rows + hreq + drafted + log10 ctx | hit −15.2 ms/%, hreq +2.1 ms, drafted +92.7 ms | 0.301 |
| tok/s ~ same | hit +2.76 tok/s per % | 0.346 |

**1 hit-rate point**: matched pairs disagree by an order of magnitude — R2-vs-W3 killer144kL
(98.3% vs 96.0% → 90.6 vs 96.9 ms/win) implies **−2.7 ms/win ≈ +0.9 tok/s per point**; W4-vs-R2
longtg (98.0% vs 96.1% → 81.6 vs 107.0) implies −13 ms/win ≈ +4 tok/s per point; OLS says +2.8.
Mechanistic bandwidth bound is ≤ 0.1 tok/s per point (1% of lookups ≈ 17–25 rows ≈ 0.2 MiB/window).
Honest verdict: **hit rate is worth ~1–3 tok/s per point at most and is not identified from this
data** — any value above the bandwidth bound is per-request helper *latency*, not volume.

**0.1 ms/slot (decode-side)**: decode swap-ins are ~600–2700 slots per request → 60–270 ms ≈
0.5–3% of gen → **≈ 0.2–0.3 tok/s**. Decode-phase swap-in cost is second-order.

**0.1 ms/slot (prefill-side, storms)**: killer storms are ~99.5k + ~88–117k slots → 0.1 ms/slot
≈ 19–22 s of stream time per killer prefill (see §3).

**VRAM free**: not identifiable — the #2065 warning shows the same `400/229 MiB` at every boot of
every arm (constant knife-edge, no variance → no slope). **ctx**: ≤ +10 ms/window hot-vs-hot
across 2.4 decades.

### 2.2 Empirical roofline (100% hit, zero swap wait)

Floor window from hottest passes: **69–80 ms** (these passes already absorbed today's miss path,
so the true floor is at most this). With observed tok/window 2.84–3.68:

> **TG roofline ≈ 2.84/0.080 … 3.68/0.069 ≈ 35.5–53 tok/s; practically 40–48 tok/s.**
> Observed best hot pass: **39.5 tok/s** (R2 killer144kL: 90.6 ms × 3.58 tok/win at 142k ctx,
> 98.3% hit). The roofline is only ~1.15–1.25× above the best observed pass — the target band
> 35–40 is reachable **by killing stalls and raising acceptance×depth, not by chasing hit rate.**

Two modelable levers quantified:
- drafter depth: +1 drafted/window at a=0.78 adds +0.78 tok/window → **+9 tok/s** at 85 ms windows
  (verify-batch cost unknown — needs the timer below). Today d=2.58 of the 5 budget.
- stall removal: killer gens 280–402 → ~90–120 ms/window lifts 142k TG from 7.5–12 to 25–31.

---

## 3. PP: the serialized-read story on today's arms

### 3.1 Confirmed — every arm reads ~366–390 GiB at ~half the ceiling

| arm | PP tok/s | prompt_ms | storm vol (GiB) | stream time (s) | stream G/s | wall G/s | preMs/promptMs |
|---|---|---|---|---|---|---|---|
| V2 depth64 | **534.5** | 266.2 | 379.8 | 292.4 | 1.30 | 1.43 | 1.10 |
| W3 anchor | **526.6** | 270.2 | 382.0 | 288.7 | 1.32 | 1.41 | 1.07 |
| W5 fill7400 | 487.6 | 291.8 | 368.7 | 382.7 | 0.96 | 1.26 | 1.31 |
| R2 v0232b | 482.6 | 294.8 | 368.3 | 375.1 | 0.98 | 1.25 | 1.27 |
| V1 def | 482.7 | 294.7 | 365.9 | 372.7 | 0.98 | 1.24 | 1.26 |
| W4 park | 454.2 | 313.2 | 367.1 | 376.1 | 0.98 | 1.17 | 1.20 |

Storm composition identical in all arms: 8 × ~20–23 GiB chunk sweeps (~16.3–21.9 s each) +
**~197.5 GiB lending-pool storm (99.4–99.5k swaps, 148–198 s)** + 12.4 GiB tail (7.9–15.7 s).
The pool storm alone is **52–54% of volume and ~50–66% of stream time**; sweeps 43–48%; tail 3%.
Per-stream rate 0.96–1.32 GiB/s vs 2.1 ceiling; overlap factor (stream-time/wall) 1.07–1.31.

**Confirmed**: per-stream ~1.0 GiB/s QD≈1 exactly as the journal says; aggregate wall rate never
exceeds 1.43 GiB/s (68% of ceiling). The read path is the PP wall in every arm.

**New arm split**: the two depth-64-config arms (V2, W3) read at 1.30–1.32 G/s stream / 1.41–1.43
wall with 1.44–1.54 ms/slot; the default-config arms (R2, V1, W4, W5) sit at 0.96–0.98 / 1.17–1.26
with 1.90–2.07 ms/slot — a **~33% faster read path worth +40–80 PP tok/s** (535/527 vs 454–488).
v0232b (today's intended pin) still ships the slow default. Footnote: W4's warm leg read 1.75 GiB
in 11.5 s (0.16 G/s — cold-boot box tail, same 8–48 s variance round-14 documented).

### 3.2 Read-once (round-17 Design A idealized: ~40 GiB, overlapped at 2.1 GiB/s)

Read floor: 40 GiB / 2.1 = **19.4 s** — never the wall again. Compute+boundary residual
(prompt_ms − volume/2.1, i.e. what today's wall does *beyond* ceiling-rate reads):

| arm | residual (s) | implied PP ceiling if reads hidden |
|---|---|---|
| V2 | 85.4 | 1665 |
| W3 | 88.3 | 1612 |
| W5 | 116.3 | 1224 |
| R2 | 119.5 | 1191 |
| V1 | 120.5 | 1181 |
| W4 | 138.4 | 1028 |

Scenarios for the 142,283-token killer:

| scenario | read vol | rate | read wall | total wall | **PP** |
|---|---|---|---|---|---|
| today | 366–390 GiB | 1.17–1.43 (wall) | 266–313 s | — | **454–535** |
| A-ideal (read-once, fully overlapped) | 40 GiB | 2.1, hidden | ~0 | residual 85–138 s | **1030–1670** |
| A-serialized (read-once, QD≈1 stream) | 40 GiB | 1.4 | 29 s | 114–167 s | **850–1250** |
| A-realistic (journal: 10 GiB LRU ⇒ 150–200 GiB) | 150–200 GiB | 1.3–2.1 | 71–155 s | partially overlapped | **~600–750** |
| journal round-17 estimate | — | — | — | ~200 s compute floor | ~710 |

Read-once makes PP compute/boundary-bound; the residual (85–138 s, spread ±53 s across arms,
includes the 11–47 s end-of-prompt boundary tail) becomes the next PP frontier. Secondary PP
target: killer144kL continuation prefills run 162–250 tok/s (11.2k tokens, 45–69 s, staging
~21.6 GiB) — same read-once machinery applies.

---

## 4. Ranked verdict

### TG (get 25–32 → 35–40+)

1. **Kill the post-storm gen stall** (numbers: killer gens 280/402/300 ms/win in W4/V1/R2 vs 119
   in W5 with byte-equal hit%/rows/helper-reqs; hot floor 69–94 ms at the same 142k ctx; roofline
   40–48 tok/s). This is worth +13 to +23 tok/s on the worst legs and is *invisible to every
   current counter* — likely page-cache/swap fallout of the 370 GiB stream (box swap rises 8→13
   GiB across the killer leg) plus park writeback (W4 = park arm is uniformly slowest).
2. **Raise drafted/window toward spec=5** (d=2.58 observed; +1 drafted ⇒ ~+9 tok/s at 85 ms;
   acceptance 70.7% median is at the low end of the 75–82% design band — the cold-drafter refill
   after rewinds is visible: first-window "no drafts claimed" legs show a=50–62%).
3. **Do NOT spend on expert-cache hit rate** (1 point ≈ ≤3 tok/s, bandwidth bound ≤0.1; 5-point
   cross-arm spread bought nothing measurable). Decode swap-ins are 1.8% median of gen — also
   second-order.

### PP (get 454–535 → 600+)

1. **Cut the re-read factor** (366–390 GiB read for ~40 GiB unique, ~9.4×; pool storm 197.5 GiB
   = 52–54% of volume). Design A read-once ⇒ ~850–1670 ceiling; even the conservative 10-GiB-LRU
   cut (150–200 GiB) ⇒ ~600–750. Biggest single number in this whole report.
2. **Ship the fast read path (swap-in depth 64) as default** (V2/W3 1.41–1.43 G/s wall vs
   0.96–0.98 default ⇒ 527–535 vs 454–488 PP, +8–17%, free).
3. **Attack the compute/boundary residual** (85–138 s, ±53 s arm spread incl. 11–47 s
   end-of-prompt tail) — it caps read-once PP at ~1030–1670 and is the next wall after Design A.

### Cheapest instrumentation that would most sharpen the model

1. **Per-window phase timer** (one line per N windows): draft-fwd ms, verify ms, expert-wait ms,
   helper-wait ms. Splits the 69–402 ms/window spread into compute vs stall — today's counters
   cannot (all flat). This is *the* missing measurement; every conclusion above sharpens with it.
2. **Helper RTT + queue-depth histogram per request** (p50/p95 RTT, in-flight depth): tests the
   "33 reqs/window × latency" hypothesis directly; converts the 1–3 tok/s-per-hit-point range
   into a number.
3. **getrusage major/minor fault deltas per request** (one line, no GPU): tests the page-cache
   fallout story for the post-storm stall — the top TG suspect has no counter today.
4. (already shipped in v0232+) anchor `in %.1f ms` lines — keep reading them first on any PP drift,
   per round-16's lesson.

---
*Method note: swap-in lines between a request's summary lines and the next prompt line are
attributed as next-request staging/decode-tail (±1 line ambiguity, ≤3% of gen time either way);
helper rows == rows-over-PCIe cross-checked equal on every request; all 6 engine logs are
null-byte-free today, offsets applied raw.*
