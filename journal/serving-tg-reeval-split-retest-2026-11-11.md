# Serving TG "collapse" reevaluation + split retest — 2026-11-11

## Trigger

Morning serving on the freshly promoted M3a config (v0219, split-38, 128/128
reserves, border helper 4507 slots) looked collapsed: 27.4–41.4 tok/s decode at
135–144k-token conversations, 293 #2065 VRAM-paging warnings since boot, requests
starting at 103/278 MiB free. Operator: "yesterday we had way better TG."

## Finding 1: there is no regression — the gate was the wrong shape, and so was the comparison

Controlled boots (fresh pairs, exact configs, judge lines from the shared engine log):

| Arm | Stack | Leg | Decode |
| --- | --- | --- | --- |
| R1 | v0219, split-38, 128/128, border helper (prod M3a) | killer250k (ctx 1365950 B, max 1024) | 33.0 tok/s, 1× #2065 during leg |
| R2 | v0219, split-38, 256/256, helper reserve 300 (4484 slots, 404 MiB free) | killer250k | 33.6 tok/s, 1× #2065 |
| R2 | same | longtg (160000 B, 384, x2 + 1) | 33.9 / 38.3 / 37.0 |
| R4 | **v0216, split-38, 256/256, fill 6796/slots 4600/reserve 512 = yesterday's exact stack** | longtg | 33.9 / 35.4 |
| R4 | same | tg1024 (ctx 2048, x2) | 35.8 / 37.3 |

Serving histogram over the whole shared engine log (judge lines, gens ≥ 40 tokens):

| Context bucket | Nov 10 (v0215/v0216) median | Nov 11 (v0219 prod + reeval) median |
| --- | ---: | ---: |
| 0–10k | 39.3 (n=23) | 38.3 (n=9) |
| 10–100k | 30.1 (n=133) | 34.0 (n=7) |
| 100–150k | 33.8 (n=120) | 29.7 (n=13) |
| 150k+ | 32.4 (n=183) | 33.6 (n=2) |

- Every bucket matches across days/versions. Yesterday's 150k+ serving median was
  32.4 — today's "collapse" numbers (27–41 at 135–144k) are inside yesterday's own
  distribution (min 17.0 at 150k+). Long-context decode has always been ~30–34.
- Engine version (v0216 vs v0219) and reserves (128/128 vs 256/256) are both
  non-factors at 250k residency: everything pages there (#2065 in both arms,
  identical TG). The 256 MiB reserve difference cannot discriminate at that depth.
- The felt "way better TG yesterday" is explained by conversation residency, not by
  any config or version change.

## Finding 2: the split is the real TG lever — and the champion was picked against the sweep's own rule

Yesterday's sweep journal (split-sweep-34-36-38-2026-11-10.md):
- TG ordering is **monotone toward lower splits** at BOTH small and long context:
  tg1024: 34 → 44.3/46.1, 36 → 42.5/40.8, 38 → 39.9/39.3;
  longtg: 34 → **41.2/43.2**, 36 → 33.8/35.9.
- PP ordering is monotone the other way (80k: 38 → 347.3, 36 → 335.9, 34 → 318.7).
- The journal's stated rule: "best long-context TG wins" — but it ends at
  "Champion decision: pending split-32" with **no recorded pick**, and split-38
  (the worst-TG split) was subsequently promoted to prod.
- Today's retest (S34/S36 on v0219, sweep occupancy 6796/4600/512, port-isolated)
  re-measures longtg + killer144k (ctx 786800 B ≈ 144k, max 1024 — the operator's
  actual serving shape) alongside the classic lane. Results: below.

## Finding 3: operator traffic contaminated the first retest attempts

- A mystery 246,942-token request stalled S34 gen-1 at 180,224 tokens for 300 s and
  tripped the #481 watchdog (engine killed). The request was the **operator's own
  opencode session** (~247k tokens by now) — it rides :8009 and delivered to the
  trial server while prod was down. The same contention cancelled R4's killer144k
  leg earlier (client disconnect at 32,768 of 142,279 read).
- Remediation: retest boots moved to **:8019** (`STRATA_PORT`), `AB9_URL` plumbed
  through all runners; `run_lane.sh`/`run_longtg.sh`/`run_reeval.sh` health checks
  now derive from `AB9_URL` instead of hardcoded :8009.
- Driver bug found and fixed while isolating: `ab9_driver.py` `post()` built its
  request from the module-level `URL` constant (hardcoded :8009), silently ignoring
  the `--url` argument — every prior leg only worked because everything ran on :8009.

## Watchdog note (#481)

A healthy 247k-token fresh read at ~500 tok/s logs progress every 10 s and should
never trip the 300 s silence watchdog — the kills were triggered by genuine read
STALLS, and the stalls form a pattern (next section).

## Finding 4: big fresh reads stall at 16,384-token checkpoint boundaries (cache-size dependent)

Three incidents, all mid-read freezes at exact multiples of the 16,384-token
conversation-cache checkpoint granularity:

| Boot | Request | Stalled at | Outcome |
| --- | --- | ---: | --- |
| S34 gen-1 (:8009) | operator's 247k session | 180,224 (11×16,384) | #481 kill at 563 s |
| R4 (v0216, split-38) | killer144k (142,279) | 32,768 (2×16,384) | cancelled at 66 s |
| S34 (:8019) | killer144k (142,280) | 32,768 | #481 kill at 388 s |

- Not GPU, not split, not version (v0216+v0219, split-34+38 all affected).
- The morning R1/R2 killer250k legs (246,942-token fresh reads) COMPLETED at
  617–619 tok/s — before the cache grew monsters. The stalls began only after the
  killer legs left 640 MB × 3 conv-*.sess sessions (+ 3×40 MB draft rings) in
  `data/conversation-cache/` — the same lane-era orphans flagged for sweeping.
- Correlation: heavy conversation-cache state → checkpoint machinery (save/park/
  restore at 16,384-token boundaries) stalls the read path. Cache was swept to
  `data/conversation-cache-archive-20261111/` (4.4 GiB) at 12:10 before the S38
  control boot; if killer144k completes there, the mechanism is confirmed.
- Engine-side fix candidate (alongside pack-swap-in): checkpoint save/park must
  not block the read path (async/bounded), or checkpoint granularity needs a size
  cap under big caches.

## Finding 3b: ~3–5 tok/s same-config day shift

Today's boots measure 3–5 tok/s below yesterday's identical configs (R4 = yesterday's
exact stack: tg1024 35.8/37.3 today vs 39.9/39.3 yesterday; S34 longtg 33.8/33.4–36.5
today vs 41.2/43.2 yesterday). Cause unproven — post-reboot machine state and the
dirty cache are both candidates. Consequence: only same-day, same-cache-state boots
are comparable; yesterday's split-34 longtg advantage does not reproduce today.

## Split retest results (v0219, 6796/4600/512, :8019, same-day)

| Arm | PP 8k | PP 80k | tg1024 | longtg fresh | longtg cached | killer144k |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| S34 | ~194–214 | ~312 | 39.8/36.7 | 33.8 | 33.4 / 36.2 / 36.5 | stalled at 32,768 → #481 kill |
| S38 | — | — | 38.1/37.6 | **33.0** | 30.4 / 35.4 | stalled at 32,768 → SIGABRT |
| 38 anchors (same day) | — | — | 35.8/37.3 (R4 v0216) | 33.9 (R2) | 35.4 (R4) / 38.3, 37.0 (R2) | R1/R2 killer250k completed AM |

- **Verdict: no split separation at serving shape.** S34 fresh 33.8 vs S38 fresh 33.0
  (Δ within noise); tg1024 equal; yesterday's split-34 longtg advantage (41.2/43.2)
  does not reproduce — it falls inside the ~3–5 tok/s same-config day shift.
- Split-38 retained for prod on its prefill advantage (80k: 347 vs 319) — TG costs
  nothing.
- Lane note: S34 lane legs ran on the contaminated :8009 boot (concurrent 247k
  operator-session read); judge lines are server-side and trustworthy, walls inflated.

## Finding 5: the real reliability bug — checkpoint/park path vs big fresh reads under RAM pressure

- Every killer144k attempt died at exactly 32,768 tokens (2×16,384 checkpoint
  granularity): watchdog kills (S34 :8009 at 180,224 on a 247k read; S34 :8019 at
  32,768) and one SIGABRT (S38, exit −6, wrapper hint: RAM exhaustion).
- It is NOT the GPU and NOT the cache dirtiness (clean-cache S38 boot died the same
  way; morning dirty-cache boots completed 250k reads).
- RAM picture at death time: 62 GB total, 46 GB used, 6 GB free — `strata-host`
  RSS 38 GB + helper 7.5 GB. Each served conversation leaves a ~1.1 GB conv-*.sess
  on disk and a proportional RAM-tier footprint; big fresh reads add park
  transients at 16,384-token boundaries and push past what's left.
- Morning boots completed the same-shaped reads on a fresher machine with fewer
  accumulated conversations — the failure is load-accumulation-dependent.
- Engine fix candidates (queued for the fixer, priority alongside pack-swap-in):
  bound/asynchronize checkpoint park buffers; don't block the read path on park;
  cap or evict accumulated conversation state under RAM pressure.

## Killer144k completions (first-leg-fresh protocol)

Pattern found: big fresh reads complete when they are the FIRST request on a fresh
boot (empty conversation RAM tier: R1/R2 killer250k this morning) and stall/abort at
32,768-token checkpoints when run after other legs have filled the RAM tier
(S34/S38 attempts). Re-run with the fix:

| Arm | Config | Read | Decode @144k | #2065 |
| --- | --- | ---: | ---: | ---: |
| prod38 (live prod, :8009) | v0219, split-38, 256/256, 7400/5000/300 | 142,281 @ 619.8 tok/s | **34.6 tok/s** (523 gen) | **0** |

Zero #2065 at this margin (404 MiB) vs the morning M3a config's warnings at every
request start. Decode curve now flat ~33–35 tok/s from 29k through 250k residency.
S34 (split-34) killer144k under the same protocol: see the final matrix below.

## Final 144k matrix (ctx 786801 guaranteed-fresh variant, twin samples via kind tg)

| 144k killer leg | fresh read | fresh decode | restore decode |
| --- | ---: | ---: | ---: |
| split-38 (live prod) | 619.8 tok/s | 34.6 (523 gen) | — |
| split-36 (S36) | 633.9 tok/s | 33.3 (283 gen) | 36.4 (508 gen) |
| split-34 (S34) | cache-rescued* | — | **39.4 (512 gen)**, 39.9 (162) |

*S36 and S34 legs share the 142,276-token filler prefix (only the arm tag differs),
so S34 restored instead of reading fresh — an accidental but clean restore-path
comparison, and restore is what continuation-heavy serving actually exercises.

- Fresh-path TG: no split separation (33.3–34.6).
- Restore-path TG: split-34 39.4 vs split-36 36.4 on matched samples (~512 gen each);
  yesterday's 41.2/43.2 was also restore-path — 39.4 is the closest same-day
  reproduction, so the "split-34 longtg advantage" was real but path-specific.
- PP cost of 34: ~10% slower reads (312 vs 347 at 80k) ≈ once-per-conversation;
  TG applies to every generated token.

## Champion (operator decision 13:10): split-34 for prod

Rationale: serving is continuation-heavy (restore-path dominant); 34's ~3 tok/s
restore edge compounds over every message vs a one-time read penalty. Config:
v0219, layer_split 34, 256/256 reserves, helper 7400/5000/300 (unchanged).
Expected boot accounting: CUDA0 layers 0-33 (4971 cache slots), CUDA1 34-47
(4444), 404 MiB free, helper ~4484 slots.

## Fix5 validation (14:26–14:45 window) — arm 1 PASSED

Fixer commits on `helper-ipc-diet`: `69f37322d` (checkpoint saves gated by physical
RAM: evict-oldest or skip; a request can no longer stall/abort on a checkpoint
boundary) and `0359cedf6` (batched O_DIRECT pack swap-ins + `--expert-helper-ram-relief-packs`
guard rework). Builds green (engine `f9b6cccb…`, helper `946e0aba…`).

| Leg on the accumulated boot (v0219 same-sequence = death at 32,768 ×2) | fix5 |
| --- | --- |
| killer144kL after longtg+tg1024 accumulation | **COMPLETED**: fresh read 661.2 tok/s, 379 gen @ 36.8; rerun 259 @ 37.8; #2065 delta 0 |
| killer250k fresh on the SAME boot (ultimate shape) | **COMPLETED**: 131,072 restored + 115,874 read @ 300.9 tok/s, 515 gen @ 44.5 |

- longtg decode band on fix5: 39.6–43.0 (v0219 same-day: 33–36) — suggestive
  improvement, not yet repeated.
- Checkpoint-gate lines (`evicted`/`skipped`): none printed — the gate never had to
  bite (tier not full under this accumulation). A/B validity rests on the identical
  leg sequence that killed v0219 twice earlier the same day.
- Window script note: driver died on a `grep \| tail` pipefail trap after arm1b —
  arm 2 (packs lever) not yet run; trap fixed (`\|\| true`). Prod was re-booted
  immediately and verified (split-34, 404 MiB, 4583 slots).

Pending: arm 2 packs-lever window; then the v0220 pin + prod promotion decision.

## Prod outcome (12:20)

- Booted: v0219, split-38, **256/256 reserves**, helper fill 7400 / slots 5000 /
  reserve 300 (the R2 class — 404 MiB free, completed a 250k fresh read earlier
  today, zero #2065 at request starts vs M3a's 103–278 MiB starts).
- The retest legs overwrote the operator's morning session checkpoints (conv slots
  reused), so the next operator message is a fresh 247k read: acceptable on a fresh
  boot (empty RAM tier) per the morning precedent; session checkpoints rebuild.
- Conversation cache: 7×1.1 GB leg artifacts left in place (inert, evictable).

## Arm-2 packs lever + v0220 window and promotion (14:30–16:00)

**Arm-2 base (fix5, no flag) — clean, no regressions:**
PP 305 @ 80k; longtg 37–40; tg1024 41.9/41.1; killer 35.6/35.5; #2065 = 2.
But the packs lever REFUSED: "the arena has no unbuffered pack file to swap in
from" — zero swap-in lines. Root cause: the gate checked `direct_` (the RAM-room
heuristic's fds, intentionally unarmed on a healthy boot) instead of the lever's
own fds.

**Fix `c604afde6` (helper-ipc-diet):** `arm_prompt_direct` arms additive O_DIRECT
fds (`pdirect_`) without unbuffering the tier — `staged()`/`blob()` untouched, so
the decode path is unchanged; `swap_files_ready` accepts `pdirect_`; genuine
O_DIRECT failure prints errno verbatim (no silent buffered fallback). Builds
green: engine `d24d25b41…`, cuda engine `8d820cf17…`, helper `1a2074bcc…`.
Chain: `ae71a6577` → `69f37322d` (stall fix) → `0359cedf6` (swap-ins) →
`c604afde6` (lever arms).

**Lever window (15:49, combined window approved; operator ended it early):**
- Lever ENGAGED mechanically: no refusal; drop set = arena 22.38 GiB (13671
  pairs) + stage caches 16,186 MiB (9415 pairs) ≈ **38 GiB given back**.
- **Performance-disqualified**: 80k PP 171.3 s ≈ 86 tok/s (vs 305 / 48 s on
  every v0219/fix5 boot). GPUs 0%/0% while disk churned; VRAM 231 MiB at request
  start; helper nearly idle (65 reqs / 327 rows vs ~2500 healthy).
- **Zero `adapt swap-in` lines** — the batched swap-in path never fired. Prefill
  expert misses were served by the mmap page-fault path (unbatched, page
  granularity): the swap-in trigger does not cover the prefill borrow path.
- longtg was mid-flight at termination; killer144kL-under-lever skipped (moot —
  already disqualified at 80k). Artifacts: `out/v0220-lever-20261111/`.
- Fix directions for a future round: (a) selective cold-only drop (keep the hot
  working set), (b) route prefill borrow-path misses through the batched
  swap-in, (c) per-adapt-round byte pacing. Not urgent: fresh boots hold 15+ GiB
  free since the stall fix; the lever's value is long-uptime headroom.

**v0220 promotion (15:58):**
- Pinned `/mnt/srcds/Strata/v0220/build-mixed-gpu/` (SHAs verified against the
  fixer's build report), config `exe` + launcher `BIN` → v0220; packs flag NOT in
  the prod config (lever inert).
- Prod up 15:58:38, verified: split 34–47 / 4444 expert slots; helper 4583
  mirrored; 404 MiB free; plain relief 4583 pairs / 7.10 GiB; "the adaptive tier
  is on: the split's caches keep their host copies".
- v0220 = `c604afde6` build: stall fix (checkpoint saves gated by physical RAM)
  + swap-in machinery + lever arming, flag-off in prod.

## Packs lever round 2 — fixed and validated (16:55–17:07)

Fixer round 2 on `helper-ipc-diet` (pushed to fork): `7638ca74b` (prefill/borrow
misses batch through one O_DIRECT `read_direct` window; request-scoped stage memo;
per-request prefill swap-in telemetry) and `b749c246c` (cold-only stage drop:
`--expert-helper-relief-hot-mib`, default 7000; hot/cold split in boot report).
mlock diff review: 0 new locks, no new buffers, both `pinned && !blob` guards
intact; no-flag path byte-identical. Binaries: hip `5d0b625d…`, cuda `f1fed53a…`,
helper `4519885…`.

**Lever2 window (b749c246c, packs ON, hot budget 7000, :8019, ~11 min):**

- Drop split: hot kept 4495 pairs (6999 MiB), cold dropped 4920 (9187 MiB);
  arena still wholesale → ~31.6 GiB total freed.
- **Swap-ins fire and run at the arena floor**: 0.74–1.15 ms/slot across all
  rounds (naive path 52; arena floor 1.27) — 45–70× better than naive; pack
  reads ~1.6 GiB/s effective.
- 80k PP: **61.4 s** (broken round 1: 171.3; no-flag: 48) — recovered ~78% of
  the gap. killer144kL **completed**: 125,896 fresh read @ 522.8 tok/s, 311 gen
  @ 34.4; rerun @ 33.6. #2065 = 2.
- Decode cost is real: longtg 32.4/39.4, tg1024 34.5/33.2 (no-flag arm-2:
  41.9/41.1) ≈ 15–19% slower. Driver: tg1024 fresh pulled 1.06 GiB over the
  helper socket (reqs=13097) — the wholesale ARENA drop (13671 pairs) pushes
  decode misses to the helper; the stage split alone was clean.

**Verdict:** mechanically complete (arming, batched swap-ins at floor, cold-only
stage drop, no refusals, zero stalls, killer completes). Performance: acceptable
as an opt-in RAM-relief mode (≈31.6 GiB freed for ~15% decode / ~28% fresh-PP),
not a prod default. Next refinement if wanted: apply the hot/cold split to the
arena drop too (the remaining socket traffic), then re-measure.

Window artifacts: `out/lever2-20261111/` (gitignored). Prod returned to v0220
17:06:35, verified (404 MiB, split-34, health ok).

## iq3xxs fastest-tuple sweep (17:21–18:03, + rerun)

Question: fastest (binary × helper class) for iq3xxs at fixed split-34 / 256-256.
One protocol, one day: warm, 8k×3, 80k, longtg+tg1024, killer144k per arm; helper
classes = prod (7400/5000/300, engine reserve-mib 0) vs trial (6796/4600/512).
Arms: `run-iq3xxs-sweep.sh`, artifacts `out/iq3xxs-sweep-20261111/`.

| arm (h7400) | 8k wall | 80k wall | longtg wall | longtg decode | tg1024 walls | tg1024 dec | killer144k | #2065 |
| --- | ---: | ---: | ---: | --- | ---: | --- | ---: | ---: |
| **b16 (v0216)** | 7.53-7.68 | 47.23 | **89.98** | 40.8/33.7 + 41.5/37.5 | 14.55+10.88 | 43.9/44.1 | 280.1 (read 537.7, dec 41.4/39.4) | 2 |
| b19 (v0219) | 7.56-7.65 | 47.09 | 93.06 | 40.4/36.8 + 44.4/36.5 | 13.99+10.12 | 42.3/46.2 | **267.3** (read 537.4, dec 41.7/41.0) | 2 |
| b20 (v0220) | 7.55-7.63 | 46.98 | 90.54 | 39.8/42.2 + 41.2/46.4 | **13.25+8.90** | 41.4/43.9 | 350.2 (read 486.3, dec 34.5/**44.7**; rerun prefill anomaly 37 s) | 2 |

- **Binary dimension ≈ wash**: longtg walls within ~3%; PP identical; tg1024
  walls favor v0220, killer totals favor b16/b19, decode bands overlap.
- **Stage-3 killer250k on b16-h7400 (fresh boot): COMPLETED** — 246,946 read in
  369.3 s (**668.7 tok/s**, fastest 250k read measured), 785 gen @ 33.0, rerun
  560 @ 36.3. Caveat: fresh boot — v0216 has NOT faced the accumulated-RAM
  sequence that killed v0219 twice at this leg (only v0220 passed it, arm-1).
- Stage 2 (b16-h6796): longtg completed at **98.51 s** (~8.5 s slower than the
  7400 class) then the service threw HTTP 503 + connection reset during tg1024
  and killer144k — operator confirmed a **GPU hiccup** in that window; rerun
  queued (`run-rerun-b16h6796.sh`). Even pre-hiccup, 6796-class longtg is
  slower than 7400-class on v0216.
- Prod returned to v0220 18:02:59 (b20-h7400 numbers above = the current prod
  tuple, first full measurement of it).

## Rerun b16-h6796 (18:03–18:13, post-GPU-hiccup) + final verdict

Rerun (`run-rerun-b16h6796.sh`) completed clean: longtg 96.92 s (40.9/37.1 +
37.5/37.1), tg1024 walls 18.53+11.39, decode 37.8/39.6, killer144k 275.0 s
(read 535.2, dec 38.7/35.9), #2065 = 2. Confirms the hiccup-contaminated
stage-2 was real data for longtg: **the 6796 class is genuinely slower**, not a
hiccup artifact (same-binary 7400 vs 6796: longtg 89.98 vs 96.92, tg1024
25.43 vs 29.92 with decode 43.9/44.1 vs 37.8/39.6; killer total a wash).

**Helper class winner: 7400/5000/300 (prod class).** Binary dimension: wash
within ~3% day-noise, no consistent per-metric winner. Stability breaks the tie:
only v0220 has passed killer250k under the accumulated-RAM sequence (the class
that killed v0219 twice); v0216's 250k pass was fresh-boot and it lacks the
checkpoint-stall fix; tg1024 walls (short-request serving) also favor v0220.

**FINAL: fastest serving tuple for iq3xxs = split-34, 256/256, v0220,
helper 7400/5000/300 — exactly the running prod config. No promotion reboot
needed; prod verified healthy after the rerun window (18:13:00).**

## iq3xxs max-occupancy sweep (18:26–19:16) — 7400/300 IS the ceiling

Question (operator): max GPU occupancy on every GPU. Protocol per arm: **250k
infill (ctx 1365950) then tg1024 ×2 under the loaded tier**, same boot — v0220,
split-34, helper slots cap 5000. 3080 = 8192 MiB (7400/300 uses 7656 = 93.4%).
Scripts `run-iq3xxs-occ.sh` + `run-occ150.sh`, artifacts
`out/iq3xxs-occ-20261111/`.

| arm | helper res | engine res | VII free | 250k infill | tg1024 under load | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| A-ref 7400/300 | 300 | 256/256 | **450** | ✅ 378.7 s | ✅ dec 39.6–43.2, reqs 5,405 | healthy reference |
| B 7400/0 | 0 | 256/256 | 150 | ❌ 503 | ❌ 503/reset | **wall** |
| C 7600/0 | 0 | 256/256 | 150 | ❌ | ❌ | same wall; fill irrelevant |
| D 7600/0 | 0 | 128/128 | 150 | ❌ | ❌ | engine reserves don't move VII free |
| E 7600/0 | 0 | 64/64 | 150 | **stalled** (16,384 freeze class) | — | killed per operator; no data needed |
| F 7400/**150** | 150 | 256/256 | **404** | ⚠️ 740.6 s (2× slower — prefix-cache confound suspected) | ~ dec 33.2–42.3; reqs 8,068 (+50%) | viable but no gain |

Findings:
- **Helper reserve coupling is a cliff at 0, not a slope**: reserve 300 → VII
  450 free; 150 → 404 (healthy, like 300); 0 → 150 and every big request fails
  (first-use graph capture has no room). Engine reserve changes (256→128) do
  not move VII free at all. Mechanism hypothesis: at reserve 0 the helper's
  staging lands engine-side (~300 MiB of VII), documented for the fixer if
  pursued.
- **Reserve 150 buys nothing**: no slot-count gain visible in traffic (helper
  reqs +50% — worse, not better), infill wall 2× (confounded by disk-tier
  prefix state — F booted after E's partial read), decode band no better.
- Fill above 7400 was untestable at reserve 0 (dead before fill mattered); at
  reserve 300 the 3080 sits at 93.4% with 536 MiB card-free — no room for a
  meaningful step.
- E's stall at 64/64 + reserve 0: the freeze class returned under VII-150
  conditions even on v0220 — noted as a diagnostic datum, not a serving case.

**Occupancy verdict: 7400/5000/300 + 256/256 = max viable; the champion config
is already at the ceiling. No config change.** Prod returned 19:15:51 (v0220).

## iq3_s whole panel × splits on v0220 (19:18–20:11)

`run-iq3s-splits.sh`, arms s36/s34/s38, all v0220 + helper 7400/5000/300,
panel = warm, 8k×3, 80k, longtg+tg1024, killer144k. Warm page cache made boots
~90 s (the Nov-10 "13-min boot" was cold cache). Artifacts
`out/iq3s-splits-20261111/`. Note: the iq3_s engines log to their own elog
(`engine-swift-iq3s-…`), not the shared swift15 one.

| (iq3_s, v0220) | s36 | s34 | s38 |
| --- | ---: | ---: | ---: |
| 8k walls (s) | **6.63–6.87** | 7.52–8.23 | 6.69–11.9 |
| 80k wall (s) | 258 (cold-boot state) | 91.1 | 149.5 |
| longtg wall (s) | **117.0** | 190.5 | 133.0 |
| longtg decode | **37.5/34.0** | 35.3/32.9 | 28.6/33.1 |
| tg1024 walls (s) | **46.1** | 45.6 | 54.8 |
| tg1024 decode | 37.9/36.0/37.4 | 37.8/34.8 | 39.0/31.4/33.2 |
| killer144k total (s) | 626.7 (abort+re-read) | 520.8 (abort+re-read) | **370.3 (clean)** |
| killer fresh decode | 30.3 | 33.6 | 31.4 |
| #2065 | 2 | 2 | 2 |

**NEW DEFECT (v0220 + iq3_s, splits 36 and 34): killer144k fresh read completes
("125,740 of 125,745 read"), then `0 generated in 0 ms` — generation produces
nothing after the read; the engine re-reads the full prompt and generates
fine.** Both s36 and s34 hit it (killer walls contain two full reads);
s38 did not. Also s38's killer rerun showed a 16 s / 5-token prefill mini-stall
(0.3 tok/s), same class as v0220-iq3xxs's 37 s anomaly. Fixer follow-up
material; greppable as `0 generated in 0 ms`.

Split reading: **s36 wins serving TG** (longtg wall + decode, tg1024, 8k);
**s38 wins big-context robustness** (only abort-free killer, fastest total);
**s34 loses on v0220** (longtg 190.5 — Nov-10's stall-contaminated numbers
underestimated how far it trails). 80k numbers are boot-state-confounded
(s36 ran first/cold) — not comparable across arms. Nov-10 v0216's "36 best"
is confirmed for the TG priority on v0220.

Recommendation: s36 for the serving tuple (operator's TG priority), with the
read-abort defect filed for the fixer; s38 the fallback if big-context
robustness outranks TG. Decision + promotion pending operator.

## iq3_s split decision + tuple finalized (21:0x, operator: "34 it is")

Operator picked **split 34** — optimizing big-context fresh reads (best 80k
160.8 tps, best killer read 609.3 tps, best killer decode 33.6/34.3) over the
longtg fresh wall (its 190.5 s reflects its ~160 tps fresh-read rate at 29k;
decode itself 35.3/32.9 was mid-panel). Files updated, promotion-ready
(NOT booted — serving switch is a separate call):

- `prod-config-swift-iq3s-…json`: layer_split 36 → **34**, exe v0219 → **v0220**
- `launch-prod-swift-iq3s-…sh`: BIN v0219 → **v0220**, helper reserve-mib 0 →
  **300** (the launcher still carried the cliff setting; 7400/5000/300 is the
  validated class)

iq3_s tuple: **split-34, v0220, helper 7400/5000/300, engine 256/256**.
Carry-forward risk: s34 showed the read-abort defect (0-gen after full read,
re-read recovers) once in the panel — fixer follow-up queued; also 80k-class
early-boot read slowness on v0220/iq3_s (56–161 tps vs 287–326 on v0216).

## Machine reboot + prod recovery (21:0x–21:24)

Operator rebooted the box (~21:0x). Everything died: prod (iq3_s, up since
20:47), the first fixer spawn (ConnectionRefused at 20:5x — spawned seconds
after the promotion; lesson: agent spawns wait for prod verified-stable, not
just health-ok), and the second fixer (died with the reboot, worktree clean at
`b749c246c` — zero partial work).

Recovery: re-ran `launch-prod-swift-iq3s-…sh` at 21:07 (cold cache), health
ok/loaded at **21:24:12** — identity re-verified: v0220 engine, split-34
(CUDA1 34–47, 3,918 slots), helper 3,847/5,000 mirrored, 398 MiB VII free.
End-to-end serving verified with a warmup request: HTTP 200, judge
`59 tok fresh read 42.5 tps, 11 gen @ 41.6–45.3 tps, 8/8 drafts`, no abort
signature. (Note: this model answers in `reasoning_content`; `content` is
null on short max_tokens — harness artifact, not a defect.) Fresh fixer
spawned after verification.

## Agent-lane correction (21:35, operator: "you spawned two fixers")

The second fixer session's post-reboot death notice proved unreliable — a
third spawn overlapped it in the UI. Both prior sessions confirmed terminal
(`error` / `stopped`) before this round's spawn; exactly ONE fixer active
since. Lesson: after host-restart artifacts, confirm terminal state via
task_status enumeration before spawning a replacement.

## iq3_s memory pressure measured (21:38, operator-flagged)

Live prod measurements (62 GiB box): **engine RSS 55.1 GiB** + helper 5.9 GiB
→ available **8.6 GiB**, free 2.2 GiB, **7.4 GiB in swap**. iq3xxs fit
comfortably; the iq3_s pack/blob class pushed the engine into pressure.
Conversation cache parks/restores correctly (26k tokens parked in 32.7 s,
17k restored in 435 ms; 1.4–1.9 GiB snapshots) but bounds nothing at the
engine level. Working hypothesis (fixer to verify): pressure → swap-ins on
cold paths → defect-2 early-boot read slowness and rerun mini-stalls;
possibly contributes to the read-abort. Fixer round scope = analyze AND
fix/bound the pressure, plus the read-abort fix itself.

## Rollback to iq3xxs (21:38–21:41, operator: "get us back iq3xxs")

Operator called iq3_s "degrading to uselessness" under the pressure — rolled
prod back mid-fixer-round (fixer clean-cancelled first; its :8009 teardown
would have crashed it). First switch attempt died host-side before the
launcher spawn (~5 min dark), re-launched 21:37, **health-ok 21:41:08** —
identity verified: v0220, split-34, helper 4,597/5,000 (7.14 GiB),
404 MiB VII free. Serving warmup HTTP 200 (5.3 s first-use).

**Pressure delta quantified by the switch itself:** identical machinery and
helper mirror, but iq3xxs engine RSS **37.3 GiB** (16.5 GiB available) vs
iq3_s **55.1 GiB** (8.6 GiB available) — the iq3_s pack class alone costs
~18 GiB of engine footprint. iq3_s stays parked until the diet round lands.

## Diet round relaunched (21:45, operator: "more of that")

Fixer revived on stable iq3xxs prod with the diet lane as PRIMARY:
(1) why iq3_s holds +18 GiB at same split/slots — bound the RAM tier vs pack
class; (2) conversation-cache residency bounds; (3) mmap/page-cache
double-holding + engine↔helper fill overlap. Read-abort + early-boot 80k
slowness remain in scope (pressure-chain hypothesis first). iq3_s re-promotion
waits on this round's validation.

## Diet round results (22:0x, fixer `4294d8545`+`ed88a6499`+`fe6c29cd2`, pushed)

**Root causes found (both confirmed against engine's own boot lines):**
- **The 18 GiB = the expert arena's host copies, bounded by nothing.** The
  arena copies EVERY pack expert into RAM sized by pack bytes: iq3_s
  `loaded 46.84 GiB` vs iq3xxs `39.97 GiB` at identical ranked pairs/split/
  slots. Existing relief only bounds what GPU tiers serve (iq3_s 38.29 GiB
  registered complement unbounded). GGUF mapping delta (83.7 vs 75.9 GiB) +
  pressure chain completes the reconciliation to 55.1 vs 37.3 RSS.
- **Read-abort is NOT a truncated read.** 16384+125740=142124 = the window
  position; the "5 short" = the `--dflash-block 5` tail read through verify
  windows. The abort = non-finite logits (#879) in that tail window;
  recovery (#3465) forces the NEXT request to re-read all 142k (293 s).
  Plausible trigger: 398 MiB VRAM free → driver pages GPU memory into a box
  with 8.6 GiB available / 7.4 GiB swapped. Explains split-36/34 hits,
  never iq3xxs.
- Early-boot 80k slowness = same pressure signature (reads through swap with
  38.3 GiB arena locked).

**Fixes (all opt-in, default off, named log lines):**
- `--expert-helper-relief-arena-cap-mib N` (needs relief + packs lever +
  layer split; refuses single-GPU named): coldest-first byte-exact cap on
  arena host copies, drops join before `set_unfilled()`/DMA registration.
  New `expert_helper_arena_cap_test` in both trees (cuda 6/6, hip 2/2).
- `--prompt-window-retry`: on #879 in a prompt verify window, re-read the
  ≤64-token tail batched IN-REQUEST keeping cached state (one retry; falls
  back to today's abort unchanged if the tail also fails).
- Context diagnostics: `non-finite logits (#879) context:` and
  `slow read context:` (below 200 tok/s) — read-only sampling, no device
  calls on the read path.

**Builds:** canonical `build_l3.sh` ALL GREEN, guards PASS, ctests green.
SHAs (worktree `build-mixed-gpu/`): hip `eb61acc0…`, cuda `a750a8f0…`,
helper `8ad751ee…`.

**Validation window recipe (fixer's arithmetic, runtime unverified):**
- iq3_s: `--expert-helper-ram-relief --expert-helper-ram-relief-packs
  --expert-helper-relief-hot-mib 15000 --expert-helper-relief-arena-cap-mib
  20000 --prompt-window-retry` → expect RSS 55.1→~28 GiB, available ≥25 GiB,
  swap <2 GiB, `arena cap:` boot line (~9700 tail pairs/~18.5 GiB join the
  drop), killer retry lines instead of `0 generated in 0 ms`, 80k legs
  ≥250 tok/s.
- iq3xxs live: same minus cap 22000 → RSS 37.3→~19-24 GiB, available ~30.
- **Open risk the fixer can't predict: decode/PP cost of the cap** — dropped
  arena pairs become swap-in/file-served reads (round-2 lever cost ~15%
  decode fresh / ~28% fresh-PP; cap 20000 keeps ~7300 pairs resident so the
  cost should be smaller, but the window MUST measure longtg/tg1024/killer
  decode, not just memory).

Window needs prod down (trials share the 3080); awaiting operator go +
window choice.

## Diet windows: "2 then 1" (23:15–23:58)

**Phase 2 — iq3xxs live diet (23:15–23:41, PASS, still serving):** v0221 pin
(hip `eb61acc0…`, cuda `a750a8f0…`, helper `8ad751ee…`), config/launcher →
v0221 + `--expert-helper-ram-relief-packs --expert-helper-relief-hot-mib
15000 --expert-helper-relief-arena-cap-mib 22000 --prompt-window-retry`
(commit `3411c5967`). Boot lines match the fixer's arithmetic: cap 22000 →
1,686 tail pairs join, arena keeps 13,849 (21,998 MiB); 10,727 pairs /
18,926 MiB dropped; hot 15,000 MiB resident. Memory: RSS 37.3→**34.2**,
available 16.5→**19.5**, free 5.5→11, swap 7.4→5.0. Decode: first request
27.1 (cold pass — graphs + cold file-served pairs), second **37.1 tok/s**
healthy band. No regression measured.

**Phase 1 — iq3_s diet validation (23:43–23:55, `run-iq3s-diet.sh`):**
split-34, v0221, cap 20000, hot 15000, packs, retry. Diet engaged: cap keeps
10,520 pairs (19,998 MiB); 14,056/14,056 helper pairs dropped — 26,507 MiB
total (vs 8.55 GiB pre-diet). Panel vs pre-diet s34:

| metric | pre-diet s34 | diet s34 |
| --- | ---: | ---: |
| boot RSS / avail | 55.1 / 8.6 | **31.9 / 24.4** |
| post-panel RSS / avail | 55.1 / 8.6 | 52.0 / 13.0 (swap flat) |
| 80k read | 56.4 tps | **243.6 tps** (4.3×) |
| longtg fresh read | ~160 | **276.2 tps** |
| killer144k total | 520.8 | **314.4 s** (best ever, any arm) |
| killer fresh read | 609.3 | 462.1 tps |
| `0 generated in 0 ms` | 1 | **0** |
| longtg decode | 35.3/32.9 | 21.0/23.5 ⚠️ |
| tg1024 decode | 37.8/34.8 | 28.1/32.3 ⚠️ |
| killer decode | 33.6/34.3 | 28.7/28.9 ⚠️ |
| 8k PP | 197–222 tps | ~128 tps ⚠️ |

**Abort root cause CONFIRMED = memory pressure, not the read path**: with
the diet's pressure gone, the exact conditions that aborted (16384-reuse +
tail read) completed clean — no zero-gen, and not even a tail-retry line
(the #879 trigger never fired). The retry flag rides as belt-and-suspenders.

**The diet trade on iq3_s**: reads/robustness massively better (killer 314 s
is the best of ANY arm ANY day), decode taxed 15–40% and small-PP ~50% by
swap-in servicing of dropped pairs. Post-panel RSS rebounds to 52 as file
pages return, but the available floor 13 GiB (vs 8.6) with flat swap held.

Prod returned to iq3xxs v0221 diet 23:58:04 (one designed pair restart on
the way up), verified healthy. iq3_s diet tuple = servable opt-in big-context
config; decode cost makes it the wrong default while iq3xxs diet is free.

## Overnight soak + morning probes (Nov 12, ~07:1x)

iq3xxs v0221 diet idle all night (0 requests). Morning probes: warm decode
**37.1 tok/s** (identical to last night — stable, no diet cost), RSS 34.2
flat, avail 17–20, swap draining 7.4→6.3, all health greps zero. Caveat
noted: this boot has served only a handful of requests — no full panel on
iq3xxs-diet yet.

## Round 3 + sweep ordered (operator: "both, 2 first")

Decode-improvement plan: (2) round-3 arena hot/cold split first, then (1)
cap/hot knob sweep on the round-3 binary (phase-1 tuple re-run + buy-back
arm) — sweeping the final code, not pre-round-3. Fixer resumed (same
session, exact-topic continuation) with phase-1 numbers to beat at same
relief: longtg ≥28, tg1024 ≥32, killer ≥31, 8k PP ≥170. Serena MCP
availability in the project fixed (operator) — fixer informed (queued note).
## Round 3 landed + v0222 pinned + sweep launched (08:1x–08:2x)

Fixer `b4514b0cd` (chain `4294d8545→ed88a6499→fe6c29cd2→b4514b0cd`, pushed):
`--expert-helper-relief-arena-cap-decode-mib` — decode floor inside the cap,
15-case ctest, builds/guards green. **Honest correction it forced:** a
coldest-first drop keeps hottest-first, so the decode budget is a FLOOR
(`resident = max(cap, hot+D)`), not a re-ordering; cap 20000/D 6000 and cap
24000/D 6000 land on identical residency. **The cap axis is the knob** — it
spends on the hottest pairs first, buying decode AND PP. Model (estimates,
anchored on the two measured points): targets longtg ≥28/PP ≥170 need
resident ~30k (cap 30000, avail ~14.6); at avail ≥20 the ceiling is
longtg ~24.

v0222 pinned (SHAs verified: hip `b5e4f1e3…`, cuda `385b43d5…`, helper
`226e8c51…`). Sweep launched (driver 2533349, `run-iq3s-sweep-v0222.sh`,
env-armed):
- arm 1 `cap20k-dec6k`: round-3 delta at phase-1 memory (floor-bound,
  resident 20,633 vs 19,999)
- arm 2 `cap30k-dec6k`: the buy-back (resident 30,000, est avail ~14.6,
  model longtg ~29, tg1024 ~33, killer ~31, PP ~158)
Prod (iq3xxs v0221 diet) auto-returns after. `hot` stays 15000 (dropping
below the caches' backing is the prefill collapse `b749c246c` fixed).

## v0222 sweep results (02:11–02:40, `run-iq3s-sweep-v0222.sh`)

Both arms: abort still gone (zero-gen 0), #2065 = 0, all legs completed.

| (iq3_s s34, v0222) | cap20k-dec6k | cap30k-dec6k | phase-1 (cap20k, no D) | pre-diet |
| --- | ---: | ---: | ---: | ---: |
| boot avail / RSS | 22.6 / 31.9 | 14.5 / 41.7 | 24.4 / 31.9 | 8.6 / 55.1 |
| longtg wall / decode | **106.4** / 27.4+29.9 | 119.4 / 35.6+35.1 | 141.9 / 21.0+23.5 | 190.5 / 35.3+32.9 |
| tg1024 decode | 18.1(cold)+29.7+32.5 | **37.8+42.0+41.0** | 28.1+32.3 | 37.8+34.8 |
| killer wall / decode | **313.0** / 30.5+28.8 | 369.3 / 34.4+36.9 | 314.4 / 28.7+28.9 | 520.8 / 33.6+34.3 |
| 80k wall | **60.6** | 127.8 | 60.4 | 91.1 |
| panel-floor avail / swap | **13.2 / 6.8** | 7.8 / 10.6 ⚠️ | 13.0 / 7.3 | 8.6 / 7.4 |

**Round 3's decode floor is a free win**: at identical memory (resident
19,998 vs phase-1's 19,999), longtg decode +6–7 (27.4/29.9 vs 21.0/23.5),
longtg wall −35 s, killer/tg1024/80k unchanged — and it BEAT the fixer's
model (which capped longtg at ~24 for avail ≥20).

**cap30k fully recovers decode** (35–42, pre-diet band or better) **but the
pressure relapses**: panel-floor avail 7.8 GiB, swap grows to 10.6, and the
big reads pay for it (killer 369, 80k 128, rerun tail 84 s). The pressure
chain is symmetric: more resident bytes = better decode = slower reads and
the danger zone returns.

**iq3_s serving tuple finalized: split-34 · v0222 · 7400/5000/300 ·
relief+packs · hot 15000 · cap 20000 · cap-decode 6000 · retry.** Files
updated (config exe v0222 + flags; launcher BIN v0222), promotion-ready,
NOT booted — iq3xxs v0221 diet remains the default prod (decode 37 free).
cap30k documented as the decode-max variant with the pressure caveat.

## Nov 12 midday: prod pair-restart + round-4 respawn (11:0x)

Prod iq3xxs v0221 diet restarted on its own (pair generation 2; trigger not
surfaced in the rotated launcher log — supervisor recovered both peers
unattended). First round-4 fixer spawn died to the restart window
(ConnectionRefused, zero work lost); respawned after health + serving
warmup (HTTP 200). Lesson reaffirmed: prod pair-restarts are also teardown
windows for agent spawns — verify serving, not just health, before spawning.
v0221-vs-v0222 on prod: behavior-identical at current flags (floor defaults
0); pin bump folds into the next natural window.

## Nov 12 afternoon: TG-regression diagnosis + diet-off armed (operator)

Prod iq3xxs v0221 diet regressed under live load (decode median 25–27 vs 37
band; 6 slow-read contexts; 1 zero-gen; avail 9.7, swap 14, RSS 51.7).
Mechanism: conversation KV + parked-snapshot page cache + pack-page rebound
ate the diet's headroom; capped pairs then faulted inside the token loop.
Pre-diet decode was pressure-immune (unbounded arena) — the cap traded that
immunity for headroom. Panels never catch this: single-pass fresh-boot legs
never build accumulated conversation state; floors were 13.0–13.2, prod
went to 9.7. Operator directive: **next prod boot = diet-off iq3xxs (known
fast)** — config edited + pushed (`9385ba0f6`): plain relief + retry kept,
cap/packs/hot removed, exe v0221. Prod stays up as-is until the next boot.
Round 5 scope (queued): conversation-cache bounds — but note config already
has `--conversation-cache-mib 4096`, `--kv-resident 262144`,
`--conversation-cache-disk-mib 24576`: the gap is page-cache release on
park + kv-resident sizing + LRU across conversations. Soak validation
compressed to ~10–12 min (chunked KV growth + park hammering + LRU pass +
assertions) per operator (72-turn replay too long).

## Round 4 landed + v0223 window (Nov 12 ~15:xx)

Fixer `7daa7c94e` + `179e45878` (pushed): the killed session's re-rank plan
was sound and became the core; its composition bug fixed (floor-above-cap
blocked every promote — ceiling now `max(cap, hot+floor)`, floor guards only
net-reducing swaps). Runtime decode-heat counters (`blob()` resident touches,
swap-in demand, window-file demand), re-rank at request-end boundaries
(≤64/side, counters halve = aging), `--expert-helper-relief-arena-cap-rerank`
(opt-in, needs cap), checks 16–23. v0223 pinned (hip `8e221728…`, cuda
`02719871…`, helper `7cc4d602…`). Fixer's stated trade: longtg 32–33,
tg1024 34–35, killer 32–33 at the 20k price; 8k PP stays ~120 (prefill path
untouchable by design); long-read band unchanged.

Window running (driver 3347244): arm1 `cap20k-rerank` (tuple + rerank),
arm2 `cap20k-control` (tuple, no rerank — isolates the new code's effect).
**Prod return at window end = diet-off iq3xxs** per operator directive
(config armed at `9385ba0f6`).

## v0223 window results + records ledger (13:00–13:31, Nov 12)

**Matched-conditions verdict: the re-rank works.** Same window, same boot
state, v0223 both arms:

| | cap20k-rerank | cap20k-control | delta |
| --- | ---: | ---: | ---: |
| killer144k | **305.7 s** (iq3_s-class record) | 379.8 | −19% |
| longtg | **115.6 s** | 154.6 | −25% |
| tg1024 | **69.1 s** | 82.9 | −17% |
| decode median | **~27** | ~21 | +6 |
| zero-gen / #2065 | 0 / 0 | 0 / 0 | — |
| memory (boot/post-killer) | 31.9/22.2 → 53.0/11.4 | same | — |

**Cross-window caveat, honestly stated:** this window inherited a degraded
memory baseline — swap ~10.2 GiB at start (prod's pressured day) vs 7.3–7.4
for the v0222 window. The control arm regressed vs v0222's identical-flag
arm (longtg 154.6 vs 106.4; killer 379.8 vs 313.0), so absolute numbers and
the fixer's longtg 32–33 target are NOT cleanly testable from this window
(measured 25.7/27.6). A clean-baseline re-measure rides the round-5 window
(reboot clears swap).

**records.py ledger built** (`9b51589`): sha-guarded best-ever per leg across
all windows, `--update` prints NEW RECORD/REGRESSION with 3% guard. First
audit corrected the record: "killer 313 best ever" was iq3_s-class only —
iq3xxs killers ran 256.5–280.1 s on their own prompt class (all-time read
best 680.8 tps, prod38 Nov-11). Today's rerank arm set the iq3_s-class
killer record: **305.7 s**.

**Prod returned diet-off 13:31:55** (operator directive): verified — v0221
binary, zero arena-cap lines, plain relief, RSS 37.1 GiB / avail 16.3 /
swap lingering 11.4 (stale pages, drains slowly). Decode should hold the
35+ pressure-immune band again.

## Gaming collision + diet re-arm (13:55–15:16, Nov 12)

Operator started Conqueror's Blade (~13:55) → gen-3 pair died → gen-4 boot
completed 15:03 but sat health-ok-NOT-serving (120s+ first token, avail 8.7,
swap 15.0, RSS 42.7 climbing unbounded). **Not a code regression — same
v0221 binary both days.** Three stacked causes:

1. **First-ever diet-off boot at 13:31** — unbounded arena = RSS 42→52
   trajectory vs the cap's 34. The "known fast 35+" reference was measured
   on an idle box; the directive assumed one.
2. **Game costs system RAM, not VRAM** (runs on the iGPU; iGPU framebuffer
   is host memory, ~3–5 GiB with Proton+Steam). Yesterday's gaming ran
   against the **capped diet (3411c5967: cap 22000, hot 15000, packs,
   retry)** — 34 GiB + game fit. The cap's decode tax and its memory
   ceiling are the same knob; removing it for speed removed gaming
   coexistence.
3. **Stale swap hangover**: the morning's 126k-conversation degradation
   left 11→16 GiB in swap; prod restarts never clear swap (only reboots
   do). Yesterday's boots started clean.

Operator chose **A: re-arm the capped diet** (config restored verbatim from
`3411c5967`, commit `c2978b7b5`). Verified 15:15:58: arena cap line present
(`arena keeps 13857 pairs ≈ 21.9 GiB`), RSS 36.9 bounded, HTTP 200 in 9.5s.

**Ops lesson (hit three times in a row):** `pkill -f`/`pgrep -f` from a
self-containing command line matches the invoking shell whenever the boot
step's launcher path (or any searched string) is genuinely in that command
line — bracket tricks don't help when the text is really there. Fix:
`stop-prod.sh` on disk (state-file supervisor PID first, orphan patterns
live inside the file where no scanned cmdline contains them). Stop and boot
must be separate invocations.

## Round 5 landed — conversation-cache bounds (15:20–16:10, Nov 12)

Fresh fixer session (the first connect-looped on infra churn — 3×
ConnectionRefused with zero progress while prod verifiably served; killed,
respawned with WIP carry-over, ran clean end-to-end). Four commits on
`helper-ipc-diet` after `179e45878`, pushed to fork; **v0224 pinned**
(hip `02ec299c…`, cuda `579aaadc…`, helper `56b35827…` — verified at pin).

1. `fc7d072a5` core: `posix_fadvise(DONTNEED)` page release on park writes/
   restore reads/ring sidecars (`pages_released` reported from the kernel);
   LRU `at()/drop()` (park+evict, active/pinned/guarded-donor refused);
   `qsa_kv_bytes_per_cell` geometry (24576 B/resident cell → 4.2 GiB at
   `--kv-resident-mib 4096`, 174 762 cells)
2. `9b98fce1b` serve: `--conversation-cache-evict-page-cache`,
   `--conversation-cache-ram-mib` (LRU ceiling at park-admission/commit/
   restore/idle-publish), `--kv-resident-mib` (only lowers asked residency,
   4096-cell floor); `--prompt-window-retry` extended to the merged
   first-window class (the #879 tail abort)
3. `cb424d6b4` tools: `conv_cache_diet_boot.sh` + `conv_cache_diet_soak.py`
   (grow 120k, park hammering, LRU pass; asserts avail ≥15, RSS ≤40,
   tg ≥34, zero-gen 0, parked page-cache ≤0.6 GiB, 0 slow-reads)
4. `2bd6f791a` docs: targets + boot/judge lines + arithmetic

Builds ALL GREEN (L3 fix5), guards PASS, helper ctests 6/6+2/2,
conversation-cache tests 4854+219 checks. Targets: RSS ≤40 GiB at 130k ctx
(34.2 baseline + ~4 host KV), avail floor 15, decode back to 35 band.

Fixer's live observation mid-run: **prod capped-diet RSS already 47 GiB /
avail 10.1** with the operator's session active — the conversation side
growing exactly as diagnosed; round 5 is the missing half.

**Validation window PENDING operator scheduling**: soak (8019 trial) +
clean-baseline re-rank re-measure want prod down AND a reboot first
(clears the 16.5 GiB stale swap) — reboot ends the game session, so the
window waits for game end.

## Nov 12 evening window — repro PASS, round-5 soak FAIL, prod on S34 (17:00–17:33)

Operator: no reboot, validate now, S34-repro first, "I only care for tps
in tg and pp"; later: "yesterday's fastest is what we use for prod".

**S34 repro (v0219 · trial-s34.json · 6796/4600/512 · plain relief): PASS**
— killer144k: PP 648.8 (−1.8% vs 660.9), TG 44.3 (+2.8% vs 43.1), both
inside the 3% band. Wall 233.6 vs 464.9 not comparable (killer prompts
embed a run-unique tag — anti-cache-rescue by design; tps is the metric).
RAM: engine RSS 47.5–48.1 during the 142k read, avail 12.7 — the
unbounded tuple's signature at full context.

**Accidental v0224 datapoint** (leg fired at the surviving soak trial
before identity check — lesson: ALWAYS verify trial identity from boot
lines first): killer PP 472.7 / TG 34.0 cached-rerun, wall 349.5.

**Round-5 soak on v0224: FAIL, worse than pre-diet.** At 9.6k-token
context: RSS 51.3 (pre-diet needed 142k to hit 48.1), avail 7.8, climbing
monotonically to 54.0. Three defects, evidence in engine-soak-v0224.log:
1. RAM-tier park bytes accumulate per park of the same conversation:
   406 MB → 936 MB → 1.59 GB → 2.48 GB (19k tokens); snapshot_bytes
   ~894 MB at 19k ≈ 47 KB/token vs the old path's 33.
2. Draft-ring rewind broken: "reading the prompt again (16384 tokens)
   instead of restoring" → PP collapse (149.7 tok/s; slow-read events at
   19k tokens, avail 8.0).
3. adapt swap-in churn: 20 GiB file reads mid-soak.
Working as designed: disk-park page release (pages-released=6/8, 1.7–2.6
GiB released), ceiling/LRU wiring, boot flags. Operator killed the soak:
remaining measurements would only document the defect.

**Prod returned on the S34 tuple** (operator directive): config derived
verbatim from trial-s34.json (prod sockets/log/port/cache-dir, vision
kept, exe v0219, plain relief); launcher retuned 4600/512/6796/BIN v0219.
Verified: v0219 + exact helper tuple from boot identity + ps, gen 1,
serving HTTP 200 in 5.5 s. Expect uncapped-arena RSS growth under long
sessions — accepted pending round 6.

**Root cause of the evening's restart-loop shitshow:** every interrupted
boot left its setsid'd supervisor alive, and supervisors respawn their
pair forever; stop-prod.sh never TERMed the TRIAL supervisor (pattern
only matched prod launcher), so cleanups murdered children while the
orphan supervisor respawned them. Fix: `stop-all.sh` — both supervisors
via state files FIRST, then launcher/engine/helper/wrapper sweep, then a
20 s quiet verification. Also: four separate self-SIGTERM incidents from
`pkill/pgrep -f` patterns matching my own command line — the rule is now
absolute: kill logic lives in on-disk scripts only, and stop/go calls are
never mixed in one command.

## Standing coarse goal — the game-coexistence profile iq3_s must serve in (17:45, Nov 12)

Operator: "note the current system profile... considering the game is
active and xxs is serving you on prod at the same time: this is where we
want to see iq3_s perform ultimately." Measured live, mid-session:

- **System**: 63.6 GiB total, avail **8.2** GiB, swap used **15.0** GiB,
  load **15.8** on 16 logical cores (saturated: game + 7 pinned stage
  cores + hostapp on CPU 0)
- **Engine** (iq3xxs, S34 tuple, ~45k-token live conversation):
  RSS 46.8 / HWM 48.4 GiB — the uncapped signature under a real session
- **Helper**: RSS 1.9 (HWM 7.4; fill partially swapped), 3080 VRAM 7.2 GB
- **VIIs**: 16.3 + 16.2 GB (full), iGPU 464 MB
- **Game stack** (Conqueror's Blade + Proton + Steam): 4.8 GiB RSS
- **Live decode on this very session** (the honest part): TG **25.2–29.8**
  tok/s, PP deltas 100–191 tok/s — i.e. even yesterday's-fastest tuple,
  at 45k context under game coexistence, serves in the SAME 25–30 band
  the morning regression showed. The 44.3 repro number was an idle box
  and a short fresh context; sustained coexistence serving is the real
  bar, and nothing passes it yet.

**The coarse goal, precisely:** iq3_s serving the operator's session in
THIS envelope (game active, avail ~8, swap ~15, load ~16, 45k+ context)
at decode visibly above the 25–30 band — with per-leg max-RAM/VRAM
tracking proving it. Round 6's two gates (PP 648.8/TG 44.3 floor AND
RSS ≤40 @130k) are the mechanism; this profile is the venue.

## Round 6 landed — park path fixed forward, v0225 pinned (Nov 12 evening)

Fresh fixer, clean run. Four commits on `helper-ipc-diet` after `2bd6f791a`
(pushed to fork; **v0225 pinned**: hip `88460f5f…`, cuda `0ec4e794…`,
helper `852ddd35…`, verified at pin):

1. `36d3aa500` `drop_superseded` live-prefix rule — a conversation parks
   ONCE; superseded copies dropped (defect 1: bytes 406 MB→2.48 GB
   accumulation)
2. `7789e0a9d` `DFlashDrafter::rewind_cold` — a legal horizon the ring
   cannot serve mounts cold instead of re-reading (defect 2)
3. `284baf8f2` over-ceiling park is disk-only (no host snapshot built);
   RAM ceiling now 512 MiB default — a 120k conversation's ~5.3 GiB
   snapshot never touches RAM (defect 1's structural half)
4. `12bba2791` soak rewritten: per-leg VmHWM/avail/swap/tier-bytes
   assertions, restore-vs-reread (`reused>0` cycles, zero re-reads)

Builds ALL GREEN, guards PASS, helper ctests 6/6+2/2, conv_cache 4861
checks. Fix-forward rationale (accepted): page release/ceiling wiring
worked in round-5; the defects were pre-existing behaviors round-5
exposed (sparse checkpoint chains vs deepest-match, ceiling == cache
budget so it never bit, draft-miss full re-read). Nothing new runs on
the token path — the over-ceiling path builds less, the cold mount
reads less. Perf floor structurally preserved.

**Honest conflict (fixer-flagged)**: avail at 130k ≈ 13 GiB with the
expert tuple untouched; the 15 GiB soak floor needs the trim arm
(`--expert-helper-relief-hot-mib 13000 --kv-resident-mib 3072`,
~16 GiB avail @ ~37.7 GiB RSS) — tg on that arm must be measured before
accepting it.

Prod DOWN after pin (standing rule: up only before the next subagent).
Window plan awaiting operator go: soak + tps panel vs S34 floor
(PP 648.8 / TG 44.3) + game-coexistence context (game active now —
avail floors read against the venue, mem-tracked per leg).

## KV policy reversal (operator, 2026-11-12 ~20:0x)

- SUPERSEDES the 2026-11-09 note in mixed-ipc-pcie-ab9: `--kv int8` IS adoptable now.
- Reason: priority shifted to memory footprint; int8 halves the KV budget (~6.25 -> ~3.0 GiB),
  fits entirely under kv-resident-mib 4096 -> pinned streaming tail should vanish, VII pressure eases.
- m2 evidence (perf-neutral hot 217.9 tok/s, parity sha kept) never blocked on measurement.
- Operator also: NO more soak arms - measurement via tps panel only.
- kv-resident-mib 4096 stays (rule: only dropped if #2065 pathology fires AND traces to the CUDA card - not met).

## v0225 int8 KV panel (2026-11-12 20:18-20:25, arm V25I8, post-reboot box: swap 0, boot avail 51.5)

Boot: v0225 + S34 tuple (fill 6796/slots 4600/reserve 512) + capped diet flags + cache flags +
--kv int8 + kv-resident-mib 4096 -> 12864 B/cell, resident 262144/262144 (3.1 GiB, FULL residency,
pinned tail GONE). Boot avail 17.3 (fp16 same tuple: 13.0). Pinning verified: 64 stage threads on
3,5,7,9,11,13,15 (config 0,1 inert, CLI wins).

Results vs S34 floor (v0219 fp16 full-residency, PP 648.8 / TG 44.3):
- killer144k-tg1024: PP 645.8 (-0.5%, parity). TG 33.9-35.0 (-21..-23% REGRESSION).
- TG vs context: tg1024 (1k ctx) 44.8 (parity!); longtg (29k) 35.0-42.9; killer (142k) 33.9-36.1.
  -> TG cost is LONG-CONTEXT-SPECIFIC, not flat: small-ctx decode holds the floor.
- Cache-warm second passes decode faster (41.0-44.8) than right-after-cold-read (33.9-36.1):
  heavy reads depress following decode (matches soak TG 21-34 pattern).
- Checkpoint I/O during killer gen: 6 checkpoints, 408 MB (conversation-cache machinery writes
  during decode at 142k ctx) - candidate TG cost, unproven.
- Draft acceptance healthy 69% (332/480). Helper clean: timeouts=0 fallbacks=0.
- Mem per killer leg: maxRSS 48.9 GiB (fp16 capped soak plateau: 52-53), min_avail 10.1 GiB
  (fp16: 7.8-8.6), swap 3.1, VII VRAM 16.2+16.2 packed, 3080 7144 stable.

Memory verdict: int8 wins decisively - RSS ~-3 GiB, avail +2.5, boot avail +4.3, full KV residency.
Throughput verdict: PP parity, TG -21% at 142k = the open regression; 3-way confound remains
(int8 KV attention at long ctx vs capped tuple file-serving vs v0225 checkpoint overhead).
Deconfound options: one fp16 panel boot on the same tuple, or checkpoint-overhead source read.

Tooling nits: ab9_driver wrote no judge blocks this run -> records.py table empty (raw JSONs +
elog complete); elog_start/end are BYTE offsets not line numbers.
