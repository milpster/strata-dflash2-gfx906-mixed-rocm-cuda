# TG (decode) residency design — cutting the 12–16 % offload row share

Scope: design only. Source anchors are from `/mnt/srcds/Strata-wt-helper-ipc` @ v0232b lineage
(helper-ipc-diet / branch v0232b-hotfix c7be950984). Numbers from
`logs/engine-r2-v0232b.log` (R2 panel, Nov 13 evening) and `logs/engine-v2-v0231depth64.log`
(V2 arm), journal rounds 14–18 (`journal/serving-tg-reeval-split-retest-2026-11-11.md:1974-2499`).
Sibling file `tg-pp-forensics.md` covers the roofline; this file is the patch-level plan.

---

## 0. How decode actually spends a step (measured, v0232b)

Rig: 2× gfx906 (HIP engine, layer split 34: CUDA0 layers 0–33 with 4989 expert-cache slots,
CUDA1 layers 34–47 + head with 4399 slots = 9388 of 24576 pairs cached), RTX 3080 helper
(3560 mirrored pairs, 6.61 GiB, window tier on sock1, prompt tier on sock2), RAM arena keeps
11578 pairs (22 GiB cap) + 12998 dropped pairs file-served. `--pcie-frac 0` → the logged
"read by the GPU over PCIe or from another GPU" rows are **entirely helper RPC rows**
(verified: `helper=rows` == offload count on every leg, e.g. log:103 vs :102).

Per-layer classification (`expert_pool_dispatch_multi`, src/core/expert_source.cpp:4979–5166):
VRAM hit (`host_res`, :5105) → peer (:5110) → **helper_holds (:5112, claims rows by RPC)** →
Foresight slot (:5114, OFF in this rig) → PCIe staging (:5118) → CPU. The window graph rings a
doorbell per layer and spin-waits on the host plan (flagA), staging (flagB) and the CPU+helper
rows (flagCPU) — src/core/verify.cpp:2418–2459. The helper path per claiming layer:
`RemoteExperts::proxy_begin` (quantize q8_1 on CPU, one kBegin; src/core/remote_experts.cpp:521–613)
then `proxy_finish` (one kRows read; :615–633) inside the doorbell service
(expert_source.cpp:5185–5198 begin, :5403–5441 finish).

Logged per-request facts (v0232b, legs warm→end):

| leg (gen tok / tok/s) | hit % | offload rows (helper) | rows/tok | helper reqs | windows* | RPCs/win | rows/RPC |
|---|---|---|---|---|---|---|---|
| 34 / 21.7 | 93.5 | 2 556 | 75.2 | 518 | 16 | 32.4 | 4.9 |
| 14 / 16.7 | 96.4 | 1 432 | 102.3 | 297 | 10 | 29.7 | 4.8 |
| 515 / 12.0 (cold 144k) | 96.2 | 37 517 | 72.8 | 4 787 | 143 | 33.5 | 7.8 |
| 88 / 24.6 | 94.0 | 9 198 | 104.5 | 1 474 | 46 | 32.0 | 6.2 |
| **515 / 39.5 (hot WS)** | **98.3** | 38 074 | 73.9 | 4 812 | 143 | 33.6 | 7.9 |
| 79 / 27.8 | 95.8 | 7 949 | 100.6 | 1 202 | 37 | 32.5 | 6.6 |
| 400 / 21.2 | 94.0 | 35 298 | 88.2 | 4 138 | 125 | 33.1 | 8.5 |
| 192 / 28.5 … 296 / 30.6 | 94–96 | 14 715–34 530 | 84–98 | — | 58–141 | ~33 | ~7.7 |

\* windows = generated − accepted drafts (the +1 bonus token per window; legs verified:
515−372=143, 400−275=125, 79−42=37, 351−210=141).

Stable structure: **~33 helper RPCs per window (~7 of 48 layers skip claims), ~7–8 rows each,
~240–280 offload rows per window**, offload = 11.9–15.7 % of routed, CPU-computed refusal share
1.4–5.1 % (39.5-leg vs 21.2-leg: 1.4 % vs 5.1 % — the second residency gap).

Cross-leg regression (10 warm legs, v0232b): **ms/tok ≈ 9.9 + 0.29 × rows/tok** (r² = 0.24 —
rows/tok explains a quarter of the variance; KV/box state owns the rest, so treat as directional).
Extrapolations: rows/tok 85 (today's median, 28.9 tok/s) → 60 = 36.6 tok/s, 50 = 41.0, 45 = 43.5.
Zero-row floor ≈ 100 tok/s. This is the arithmetic the candidates below are judged against; the
operator's 39.5-leg evidence (73.9 rows/tok *and* 1.4 % CPU share *and* 98.3 % hit) is consistent
with the model's direction.

Recurrence evidence (that a readahead/prefetch working set exists):
- Demand is context-stationary: rows/tok stays 74–104 from 2k to 142k context.
- The RAM re-rank converges: swap-in per same-shape request decays 421→33→24 swaps
  (log:80–81,104,109) — the same dropped pairs demanded over and over.
- One pass moves hit rate 93.5→96.4 % on the *same* 347-token context (log:103→108) — the
  admission tier just needs to see the set once.
- The 39.5 leg held 98.3 % hit for 515 tokens on the *converged* set (log:150).

---

## Candidate 1 — Foresight swap-space shadowing the helper's decode claims  ★ PRIMARY

**Mechanism.** `ForesightSwap` (include/strata/core/foresight_swap.hpp:36–107;
src/core/expert_source.cpp:7427–7660) already implements exactly the "readahead into a few VRAM
slots" design: per-layer slots on a private copy stream + filler thread, reuse gated by finished
windows (`depth`), budget per card per window, `take()` serves a landed slot as a plain GPU group
(kind 0, arbitrary device pointer — dispatch :5114–5115 → group list :5133). It is opt-in
(`STRATA_FS_SLOTS`) and OFF here. Three things stop it from helping *this* rig today, all small:

1. **Ordering**: `helper_holds(e)` is consulted *before* `fs->take` (expert_source.cpp:5110–5115),
   so helper-held pairs (the 13–16 %) can never be served from a slot. Fix: try `fs->take` before
   `helper_holds` (move one else-if; ~5 lines).
2. **Claims don't know**: `proxy_begin` re-derives claims from `ids` + `primary_res` +
   `proxy_holds` (remote_experts.cpp:540–548) and never sees the dispatch's `kind[]` (the outer
   `begin` at :702–705 drops it). A Foresight-served row would be computed twice (helper
   overwrites with identical bytes — wasteful, not wrong). Fix: forward the dispatch's
   helper-candidate mask (or `kind`) into `proxy_begin` and skip slot-held entries (~20 lines).
3. **Bytes**: the filler needs a pinned host blob (`request_locked` gate at :7616
   `src->pinned(layer,expert)`); the helper's 12998 pairs are dropped from the arena
   (`MADV_DONTNEED`, expert_source.cpp:6671–6735) → not pinned. Fix: source the blob from the
   helper's own VRAM via `RemoteExperts::copy_cached_blobs` (remote_experts.cpp:850, already used
   by the prefill helper-source path) into a small pinned ring owned by ForesightSwap
   (~16 × 2 MiB), then the existing H2D filler (:7524). ~100 lines.
   Feed the demand signal: for every helper-claimed row of a window, `fs->note_miss`/
   `predict` the (layer, expert) — claims are already enumerated at expert_source.cpp:5191–5197
   (`claimed_rows`), extend the loop (~10 lines). `STRATA_FS_ADMIT=2` (copy on 2nd miss,
   foresight_swap.hpp:49) filters one-off spikes.

**Staging (zero-code first step).** Just setting `STRATA_FS_SLOTS=2..4` today already shadows the
*CPU-computed* refusal rows (arena-resident pairs are pinned, blob from RAM): the 1.4–5.1 % CPU
share shrinks with no engine change. The new code above extends it to the helper's 13–16 %.

**Expected gain (arithmetic).** Per claiming layer the helper demands ~7–8 distinct pairs; slots
catch the recurrent subset. From the recurrence evidence assume 40–60 % of helper rows recur
within the `depth=4` window horizon at 3–4 slots/layer (admit=1) → rows/tok 85 → 50–62.
Regression: **36.6–41.0 tok/s** (+27–42 % over the 28.9 median). A pessimistic 30 % interception
still gives 34.2 tok/s. Metering: the engine already logs `foresight swap: … experts served …`
(`ForesightSwap::report`, expert_source.cpp:7654–7661) and the offload-% clause of the hit-rate
line — the panel reads interception directly.

**Cost.** VRAM: slots × ~1.95 MiB/pair × layers per card. Card0 (34 layers): 2 slots = 133 MiB,
4 = 265 MiB (card0 rests at 325–372 MiB free — fits). Card1 (14 layers): 2 = 55 MiB, 4 = 109 MiB
(card1 rests at 229 MiB — 2 slots leaves 174 MiB, thin; fund via candidate 3). RAM: pinned ring
~32 MiB + filler thread. H2D filler bandwidth bounded by `STRATA_FS_BUDGET=32` copies/card/window
≈ 64 MiB / ~30 ms ≈ 2.1 GiB/s — fine.

**Risk.** Medium-low. (a) Parity: slot rows compute with the engine's hit-path arithmetic
(R4.2h scaled quantizer, expert_source.cpp:5546–5554) while helper rows use the helper's q8_1
image — same class of mixed-backend variance the engine already tolerates (CPU/GPU/helper paths
differ today); gate on the 61-task suite. (b) #2134 exchange interplay: hold/unhold already wired
(generate.cpp:15560, foresight_swap.hpp:85–90). (c) Slot thrash if recurrence < assumed — the
report line's `busy/over_budget` counters expose it; degrade = today's behavior. (d) All-resident
graph state untouched (slots never mark `host_res`, so `ar_off_` stays true — doorbell graph
keeps running, which is required anyway).

**Effort.** Env-only trial: 0 h. Full helper-shadow: ~10–14 h code + host test (claim-suppression
rule in the swapin_depth_test pattern) + build/pin; 1.5 days with panel.

---

## Candidate 2 — decode-specific swap-in depth/batch  ✗ not a TG lever

**Mechanism.** The "adapt swap-in" lines meter the RAM-arena relief refills (pack-file reads for
dropped pairs) through `swap_blobs_batch` (expert_source.cpp:6647–6662) / `read_direct`
(:3000–3019), window sized by `STRATA_SWAPIN_DEPTH` (include/strata/core/swapin_depth.hpp:28–63,
log at generate.cpp:23490). Decode-time volumes are tiny: 24–129 swaps/request in 2–33 batches at
1.04–1.24 ms/slot, 27–330 ms per request = **1.8–4.9 % of the decode wall** (e.g. 21.2-leg:
286 swaps, 332 ms of 18 824 ms; log:167). The depth-64 arm (engine-v2-v0231depth64.log:106–178)
moved PP (storm 1.97→1.50 ms/slot) but decode legs were 20.9–30.5 vs v0232b's 21.2–31.7 — noise.
The decode demand that matters is already served: only 286 pack reads served a request whose CPU
computed 12 122 rows — the re-rank (expert_source.cpp:7193–7260) keeps the hot pairs arena-resident.

**Expected gain.** ≤ 2–4 % on legs where the re-rank lags (first request after a context switch);
0 % steady-state. **Cost:** none new. **Risk:** none (leave `STRATA_SWAPIN_DEPTH=64` for PP).
**Effort / verdict:** 0 h — keep the knob for PP only; do not spend TG time here.

---

## Candidate 3 — verify-window graph VRAM trim (funds candidate 1, kills #2065)

Note: the window graphs live on the **engine's gfx906 cards** (captures at 229–372 MiB free,
log:82–101); the 3080 is the helper. The #2065 warnings (log:78,105) and the R1 hang
(journal:2353–2408) are the engine card at 173–229 MiB.

**What exists**: per stage, TWO graph families × T∈{1..5} — `exec_[2][T]` (zero-doorbell,
all-resident) and `exec_nr_[2][T]` (doorbell), tables at include/strata/core/verify.hpp:664–666;
capture is lazy on first use (verify.cpp:2778–2810) and `refresh_ar` (:2759–2776) flips families
when residency changes. This rig captures **both families at boot**: caches prefill 100 %
(all-resident graphs at 282/372 MiB free), then the prompt tier borrows 3690+3374 cache slots
(log:64–65) → experts leave VRAM → `ar_off_` flips → the doorbell family is *also* instantiated
(the 229/325 series). The stale family's exec pools (~5 graphs × stage) are never freed.

**What to change** (ranked):
1. **Stale-family teardown**: on an `ar_off_` flip, `cudaGraphExecDestroy` the other family's
   execs (verify.cpp refresh_ar / capture owns the tables; guard on `launched_` in-flight windows,
   verify.hpp:666). Recapture on flip-back costs one 5–56 ms capture (measured, log:83–101).
   Recovers ~50–150 MiB per card in this always-doorbell rig. ~30–60 lines, low risk.
2. **Pressure valve already exists**: `evict_window_graph` / `instantiate_evicting`
   (verify.hpp:517–523, #3702) frees the largest other-T graph when an instantiate OOMs. Wire it
   also to a free-VRAM floor at capture time (evict T=5 first — it serves only full 5-draft
   windows) instead of only on OOM. ~20 lines, low risk.
3. **Buffer sharing across T** (the 70.2 MiB/stage device buffers, log:67): real surgery, ≤ 40 MiB,
   not worth it now.

**Expected gain.** No direct TG gain; it (a) funds candidate 1's card1 slots (+55–109 MiB),
(b) moves card1 off the 229 MiB knife edge (R1-class hang insurance — worth more than tok/s),
(c) marginally grows nothing else (the expert cache is sized at boot; freed VRAM is headroom).
**Cost:** frees VRAM. **Risk:** low; flip-flop thrash if residency oscillates — add hysteresis
(only tear down after N windows stable). **Effort:** 3–5 h for items 1+2.

---

## Candidate 4 — helper-side decode fast path (window-claim pipelining)

**Mechanism.** One synchronous kBegin/kRows per claiming layer, ~33 per window, ~7–8 rows each
(expert_helper_protocol.hpp:75–88 caps; the *prompt* tier already has claim batching
`kFeaturePromptBatch`/`STRATA_CLAIM_BATCH`, expert_helper_socket.hpp:142–149 — the *window* tier
does not). The RPC latency sits in the doorbell critical path: `proxy_finish` waits the helper
(remote_experts.cpp:615–633) while the graph spin-waits flagCPU (verify.cpp:2450–2459). With hits
at 94–98 % the CPU pool's own work per layer is small, so most of each RPC's RTT+compute is
exposed: window wall 91–151 ms vs a regression GPU+draft floor of ~32 ms/window ⇒ the helper
dependent share is plausibly 50–80 ms/window (matches "residency, not compute").

**Variants:**
- **4a. Stripe window claims across the two sockets** (sock2's prompt tier is idle during
  decode): pipeline layer L+1's kBegin on the other channel while L finishes. Needs a second
  outstanding claim set in `RemoteExperts` (`proxy_claims_` is single-flight today) and
  confirmation the helper serves connections on separate threads. ~0.5–1 day. Halves the
  serialized RPC count: save ~15–30 ms/window → **+15–30 % TG** if unintercepted rows remain.
- **4b. Window-tier claim batch** (reuse the kBeginPB/kRowsPB frames for window claims, queued 2
  layers deep): same effect as 4a without the second socket; protocol + both state machines —
  ~1.5–2 days.
- **4c. Lookahead-predicted claims** (send predicted L+1 ids, reconcile misses): only if 4a/4b
  insufficient; the predictor isn't even running in this rig (no "routing-aware prefetch" line —
  its start condition failed, generate.cpp:10090–10103) — measure first with
  `STRATA_LOOKAHEAD_STATS=1`. ~3+ days. Defer.

**Cost.** None in RAM/VRAM. **Risk:** helper concurrency assumptions (4a), protocol compat (4b —
feature-bit negotiation exists). **Effort:** 4a = 6–10 h. **Expected gain:** proportional to the
*residual* offload after candidate 1 — do it second, not first.

---

## Candidate 5 (own) — tail-skip is silently OFF on helper rigs  ★ QUICK WIN

**Mechanism.** The boot note says `STRATA_ROUTE_TAIL_SKIP=7 is ON (default)` (generate.cpp:15638,
log:74) — but the dispatch **hard-disables the skip whenever a helper is registered**:
`if (tail_rank > 0 && d.peer == nullptr && d.remote_count == 0)` (expert_source.cpp:5065),
because the skip loop (:5073–5080) would also zero entries the helper could compute. The
counter log "route tail skip: N missed experts skipped" (generate.cpp:23498–23500) never fires in
either log — confirmed inert all day. The +10–20 % decode the note promises was measured on rigs
*without* a helper.

**Fix, two arms:**
- **5-conservative**: apply the skip only to entries the plan would leave to the CPU — add
  `!helper_holds(e)` to the skip condition at :5075. Cuts part of the 1.4–5.1 % CPU share.
  Expected +1–3 % TG. Same answer-drift non-helper rigs already accept (KL ≈ 0.038 vs Q8 ref,
  comment at :15619).
- **5-aggressive** (gate `STRATA_ROUTE_TAIL_HELPER=1`): drop the `d.remote_count == 0` guard —
  helper rows whose every window token routes them at rank ≥ 7 are skipped like CPU ones. If
  20–30 % of helper rows qualify (the non-helper rigs' measured benefit implies a substantial
  all-tokens-rank≥7 fraction of misses), rows/tok 85 → 62–70 → **32–36 tok/s alone**. Quality must
  be re-gated (the drift is larger than 5-conservative because it also zeroes helper-served
  contributions).

**Cost.** Zero RAM/VRAM. **Effort.** 5-conservative ≈ 2–3 h incl. counter verification (the
`tail_experts` counter starts counting, log:23499); 5-aggressive + the quality-suite run ≈ +3–4 h
(the suite run is the cost).

---

## Recommended implementation order

| # | candidate | effort | expected TG | dependency |
|---|---|---|---|---|
| 1 | C5-conservative (tail-skip for CPU misses) | 2–3 h | +1–3 % | none; env-verifiable same boot |
| 2 | C1 env-only arm (`STRATA_FS_SLOTS=2` card0-heavy) | 0 h | +2–5 % (CPU share) | none — do it in the same panel |
| 3 | C3.1 stale-family teardown (+hysteresis) | 3–5 h | 0 direct; funds C1 card1 | none |
| 4 | C1 full helper-shadow (order/claims/bytes) | 10–14 h | **+27–42 % → 36–41 tok/s** | C3 for card1 slots |
| 5 | C4a window-claim stripe/pipeline | 6–10 h | +15–30 % of residual | after C1 (residue shrinks) |
| 6 | C5-aggressive arm | 3–4 h + suite | up to +10–25 % alone | quality gate |
| — | C2 decode swap-in depth | 0 h | ≤ 2–4 %, non-steady | skip for TG |

## The 1–2 day patch plan (best single bet)

**Day 1 (code, no GPU needed until the build window):**
1. C3.1: graph-family teardown on `ar_off_` flip with N-window hysteresis (verify.cpp:2759–2776 +
   capture; verify.hpp:664–666) — frees ~50–150 MiB/card.
2. C1 plumbing, default-off behind `STRATA_FS_HELPER_SHADOW=1`:
   a. dispatch: `fs->take` before `helper_holds` (expert_source.cpp:5110–5115 reorder);
   b. claim suppression: forward the dispatch's candidate mask into `proxy_begin`
      (remote_experts.cpp:540–548) — slot-held entries not claimed;
   c. demand feed: `fs->note_miss` per helper-claimed (layer, expert) at :5191–5197;
   d. blob source: pinned ring (16 × 2 MiB) filled via `RemoteExperts::copy_cached_blobs`
      (:850) for dropped pairs; relax the `pinned()` gate at expert_source.cpp:7616 for
      slot-fills sourced through the ring.
   Host test: claim-suppression + slot-accounting rules in the tests/core no-links pattern.
3. C5-conservative (2 lines + counter check), env-gated arm.

**Day 2 (panel + gates):** build/pin v0233 in the GPU-idle window (HIP engine only — no helper
change, no wire change). Panel arms on the ab9 shapes (warm / tg1024 / longtg / killer144k /
killer144kL): (a) v0233 default (regression), (b) +`STRATA_FS_SLOTS=2` (env-only effect),
(c) +`FS_HELPER_SHADOW=1` `FS_SLOTS=3` (card0) / `2` (card1, funded by C3.1), (d) +tail-skip
conservative. Success criteria per leg: offload share 13–16 % → ≤ 8 %, `foresight swap: N served`
> 0 with busy/over_budget low, TG ≥ 35 on the killerL/tg1024 warm legs, killer144k NO hang at
≥ 250 MiB card1 free, then the 61-task quality suite vs REF with shadow on/off. If interception
< 30 % (recurrence weaker than assumed), C4a is the next round — the same panel's RPC/window and
window-wall numbers size it exactly.

Failure containment: every piece is env-gated off by default (`STRATA_FS_SLOTS` absent ⇒ today's
bits; shadow flag default 0; teardown hysteresized); the helper and wire are untouched, so a
revert is an env change, not a rebuild.
