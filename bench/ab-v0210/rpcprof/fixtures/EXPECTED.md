# EXPECTED — hand computation for the ab9 collector fixtures

All fixtures come from `build.py`; every value below is arithmetic a human can do by hand.
`ab9_collect.py` must reproduce it exactly. µs sums are Σ over the leg slice.

## Fixture inputs

| arm | ctx | PP tok/s (judge) | wall s | claims | Σbegin_us | Σreply_us | Σd2h_us | Σqueue_us | Σlock_us | DBG |
|---|---|---|---|---|---|---|---|---|---|---|
| A  | 8000  | 400.0 | 20.0  | 4 | 4·500 = 2000  | 4·1500 = 6000  | 4·2500 = 10000 | – | – | 0  |
| B  | 8000  | 200.0 | 40.0  | 4 | 4·500 = 2000  | 4·1500 = 6000  | 4·2500 = 10000 | 4·4000 = 16000 | 4·1000 = 4000 | 4 |
| C  | 8000  | 350.0 | 23.0  | 4 | 4·250 = 1000  | 4·800 = 3200   | 4·2500 = 10000 | 4·1000 = 4000  | 4·500 = 2000  | 4 |
| m2 | 8000  | 360.0 | 22.0  | 4 | 4·500 = 2000  | 4·1500 = 6000  | 4·2500 = 10000 | 4·3000 = 12000 | 4·900 = 3600  | 4 |
| A  | 80000 | 410.0 | 200.0 | 8 | 8·1000 = 8000 | 8·3000 = 24000 | 8·2500 = 20000 | – | – | 0 |
| B  | 80000 | 205.0 | 400.0 | 8 | 8·1000 = 8000 | 8·3000 = 24000 | 8·2500 = 20000 | 8·8000 = 64000 | 8·2000 = 16000 | 8 |
| C  | 80000 | 360.0 | 230.0 | 8 | 8·500 = 4000  | 8·1500 = 12000 | 8·2500 = 20000 | 8·2000 = 16000 | 8·1000 = 8000 | 8 |

rows = 10 per claim (rows=10 in every claim line); img_kb = 40, reply_kb = 20 per claim.
Arm A carries the tier=w helper lines only (prompt tier off), so no `tier=p` sum exists for A.
Arm A's 8k engine slice carries exactly one `below 512 MiB` line (the gate-6 flag path).

## Derived columns (the collector's formulas, by hand)

`transfer_share = (Σbegin + Σreply + Σd2h) / (wall_s · 1e6)`

| arm | ctx | numerator µs | denominator µs | transfer_share (6 dp) |
|---|---|---|---|---|
| A  | 8000  | 2000+6000+10000 = 18000 | 20.0·1e6 = 20 000 000 | 0.000900 |
| B  | 8000  | 18000 | 40 000 000 | 0.000450 |
| C  | 8000  | 1000+3200+10000 = 14200 | 23 000 000 | 0.000617 |
| m2 | 8000  | 18000 | 22 000 000 | 0.000818 |
| A  | 80000 | 8000+24000+20000 = 52000 | 200 000 000 | 0.000260 |
| B  | 80000 | 52000 | 400 000 000 | 0.000130 |
| C  | 80000 | 4000+12000+20000 = 36000 | 230 000 000 | 0.000157 |

`helper_side_us = Σqueue + Σlock` vs `engine_side_us = Σreply`:
B 8k 20000 vs 6000 · C 8k 6000 vs 3200 · m2 8k 15600 vs 6000 · B 80k 80000 vs 24000 · C 80k 24000 vs 12000 · A: 0 vs 6000 (8k) / 0 vs 24000 (80k) — the helper-off baseline.

`rows_per_claim = Σrows / claims` = 40/4 = **10.0** and 80/8 = **10.0** (every arm, every ctx).

`fixed_cost_per_claim_us = (Σbegin + Σreply) / claims`:
A 8k (2000+6000)/4 = **2000.0** · B 8k **2000.0** · C 8k (1000+3200)/4 = **1050.0** · m2 8k **2000.0**
A 80k (8000+24000)/8 = **4000.0** · B 80k **4000.0** · C 80k (4000+12000)/8 = **2000.0**
The cap-amortization reading: the per-claim fixed cost is flat in context (2000 µs at 8k, 4000 µs at
80k on the sync path) while a claim carries 10 rows — C's pipelined 1050/2000 µs is the half.

`gain = PP(B)/PP(A) − 1`:
8k 200/400 − 1 = **−0.5** · 80k 205/410 − 1 = **−0.5**

`pipelining_recovery = (PP(C) − PP(B)) / (PP(A) − PP(B))`:
8k (350−200)/(400−200) = 150/200 = **0.75** · 80k (360−205)/(410−205) = 155/205 = **0.7561**
(155/205 = 0.75609756…, rounded to 4 dp by the collector.)

`drift_vs_C = PP(arm)/PP(C)` for Stage-2 arms: m2 8k 360/350 = 1.02857142… = **1.0286** (in .9..1.1).

`pp_spread` = max−min over the legs of a cell: single-leg fixtures → **0.0**.

## dmon sidecar (`A-8000-1-dmon.txt`, rxpci/txpci MB/s → decimal GB/s)

rxpci 1000, 1500, 2000 → median 1500 → **1.5** GB/s; p90 index int(3·0.9)=2 → 2000 → **2.0** GB/s.
txpci 2000, 2500, 3000 → median 2500 → **2.5** GB/s; p90 → 3000 → **3.0** GB/s.
All other legs have no sidecar → 0.0. The TSV carries all four (`rxpci/txpci_gbps_median`, `rxpci/txpci_gbps_p90`).

## Gates on these fixtures (`--gates`)

gate3: DBG per (arm,ctx) = A 0 (helper off, required), B = C = 4 at 8k and 8 = 8 at 80k → **PASS**.
gate4: every `8000-1` leg carries the same sha256 (`ab9-fixture-parity`) → **PASS**.
gate6: one `below 512 MiB` line in the A 8k slice → printed as a flag, not an abort.
Expected exit code: **0**.
