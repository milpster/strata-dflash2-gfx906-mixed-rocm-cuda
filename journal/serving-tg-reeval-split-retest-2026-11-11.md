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
