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

## Round 7 fixer launched (2026-11-12 ~20:4x) — long-ctx TG + iq3_s code round

- OPERATOR GOAL restated: iq3_s + int8 KV in the game-coexistence RAM envelope (~5 GiB game)
  without inference degrading; plus raw perf for both iq3_s and iq3xxs.
- Model note (operator, runtime override): the fixer runs glm-5.3 on the Z.AI coding plan
  (remote), NOT on prod :8009 -> no prod boot needed for subagents this round; prod stays down.
  (The 2026-11-06 HARD RULE about subagents riding :8009 is mooted while the operator says so.)
- Brief: attribute the -21% TG@142k (int8 KV attention vs HIP sync checkpoints vs helper lock),
  fix per evidence, keep round-6 cache semantics intact, land commits on helper-ipc-diet,
  build + sha a new pinned pair (v0226) for the parent to panel. No soaks, no :8009, no promotion.
- Evidence handed over: panel numbers (PP parity 645.8, TG 33.9-36.1@142k vs floor 44.3,
  TG 44.8@1k parity, warm-pass recovery 41-44.8), int8 memory wins, iq3_s v0222 killer wall
  379.8 s vs iq3xxs-i8 243.1 s, code leads (STRATA_CKPT_ASYNC CUDA-only, generate.cpp:15443
  periodic ckpt trigger, 6 ckpts/408 MB mid-killer, 11-09 helper-lock 1.57 s).
- Parent backlog (next benchmark round): fp16 deconfound boot on same tuple to split
  int8-vs-tuple/v0225 if fixer attribution leaves it open.

## Round 7 — attribution landed: the decode-window expert swap-in storm; int8 KV EXONERATED (Nov 12 night, fixer)

Fresh fixer, clean run, idle box, trial :8021 (own sockets/state/logs; the parent's
:8019 state untouched). One engine commit on `helper-ipc-diet` after `12bba2791`,
pushed to fork: `82af2b4d7` (STRATA_ARENA_RERANK_MAX — the re-rank's per-boundary
bound, default 64 = unchanged). **v0226 pin candidates (fix6 trees, ALL L3 GREEN,
guards PASS, helper ctests cuda 6/6 + hip 2/2, incl. ownership + arena-cap)**:
hip `56d2e1b7…`, cuda `33f83acb…`, helper (+`-patient`, identical) `6d4fd703…`,
at `/mnt/srcds/Strata-wt-helper-ipc/build-mixed-gpu/rpcw-{hip,cuda}-fix6/`.

### Attribution (3 panels, v0225 binaries, A/B same-day A/A)

| leg (ctx) | A: v0225 tuple | B: +rerank@64+floor6000 | C: cap 24000 | v0225 panel (20:18) | v0219 fp16 uncapped |
|---|---|---|---|---|---|
| killer cold TG | 29.2 | 28.8 | 30.4 | 33.9 | 44.3 |
| killer PP | 593 | 565 | **417 (swap thrash)** | 646 | 649 |
| killer decode storm (pairs/MiB/ms file) | 5072/9643/6454 | 5062/9619/6482 | 3991/7615/4651 | 5059/9616/6143 | n/a (no cap) |
| killer warm TG | 34.2 / 37.6 | 34.2 | 32.6 | 41.0–42.9 | — |
| longtg TG | 33.3–36.9 | **400 → engine exit** | 32.7–40.6 | 35.0–42.9 | — |
| tg1024 TG | 36.8 / 39.2 | (died before) | 34.5 / 40.7 | 44.8 (1k ctx) | — |

Verdicts with numbers:
- **CONVICTED: the capped arena's decode-window swap-in storm.** The killer's
  decode carries 5072 pair swap-ins = 9.6 GiB of pack-file reads = 6.45 s inside an
  18.6 s decode (542 tok); warm passes carry 3–116 swaps and decode 34–40.
  Cap arithmetic closes it: 24576 pairs = hot 9542 (VII backing) + tier-served 9271
  (helper mirror + spill + stages) + 1404 cap evictions + 7683 resident others;
  decode's first window sweep pulls the decode universe the boot profile (a
  PROMPT trace) never ranked. Every leg with a storm decodes 29–31; every leg
  without decodes 34–40. Day shift is −8..−12% on everything today (PP 565–593 vs
  646), so absolute TG bands shift; the storm/no-storm GAP is the signal.
- **EXONERATED: int8 KV attention at long ctx.** v0224 fp16 on a near-tuple
  (16:57 boot, slots 5000, ram-ceiling 4096): killer 27.6 cold / 34.0 WARM at
  142k — worse than int8's 33.9/41.0–42.9. int8 halves per-token KV read traffic;
  at 142k it is a +6–7 tok/s WIN, at 1k ctx parity. Keep `--kv int8`.
- **EXONERATED: HIP sync checkpoints.** Periodic saves fire at 16k boundaries
  INSIDE the read (PP parity proves the read absorbs them); the decode window
  carries zero checkpoint saves; idle parks (3.0 s, 2690 MiB released) land
  between requests. STRATA_CKPT_ASYNC stays CUDA-only with no TG cost to HIP.
- **EXONERATED: helper lock.** timeouts=0 fallbacks=0 on every leg; helper bytes
  (23–452 MB/leg) do not correlate with TG.

### The levers measured

- **rerank@64 (existing round-4 code, unarmed in the tuple): no TG delta**
  (killer 28.8 vs 29.2) — and the plan itself only finds 14–33 strict-win swaps per
  boundary at this tuple (read-phase swap-in heat drowns decode heat; the
  strict-win rule stops early). The knob (82af2b4d7) raises the BOUND, not the
  plan; inert alone.
- **FATAL 400 on the rerank arm (BLOCKER before anyone arms these flags)**:
  longtg leg after two killer legs → `400 invalid request: refilling a lent slot
  failed: ExpertCache::fill_slot_queued: invalid argument` → the serve loop
  returns 1 → supervisor respawns the pair. Sequence: re-rank (promote 33/demote
  37) at killer-2's request end, then the next prompt's slot-lend refill memcpy
  H2D fails invalid-argument. Hypothesis (unconfirmed, needs a debugger repro):
  a stale device alias of a demoted pair's registered range reaching
  fill_slot_queued as the copy source after release_pair's
  unregister/madvise/re-register. Round-4's validation tuple did not run
  --prompt-cache-spread + the disk tier + idle saves. Logs:
  engine-r7b.log lines 104–108, r7-trial-20261112-210728-778337.log line 105.
- **Cap dial CLOSED at 22000.** Cap 24000 (+1062 resident pairs, storm −21% to
  3991/7615/4651) swaps the box: maxRSS 50.4 GiB, swap 10.5 GiB, killer PP
  593→417 (−30%), warm TG no better. The int8 KV win is what MAKES 22000 fit
  (fp16 same tuple booted at avail 13.0 vs int8 17.3); there is no headroom
  above it.

### What is left open (next round, in order)

1. Fix the rerank/lent-slot 400 (the only path to spending the band on decode's
   set), then re-measure rerank with a plan that ranks decode-phase swap-ins
   above read-phase (phase-tagged heat) — the current heat mixes both.
2. The cold-first-decode storm is otherwise structural at this box/pack/RAM
   budget: VII VRAM packed, 3080 full (7144/8192), arena at the swap edge.
   Options that remain: phase-tagged heat + fixed re-rank (1), or accept
   cold-decode ≈ 29–31 with warm 34–40 and spend the next lever on iq3_s's
   pack-path (the goal model) — its decode universe arithmetic differs and the
   storm share must be re-measured there before porting any conclusion.
3. Day-shift band: today's whole panel is −8..−12% vs the 20:18 panel (same
   binaries, same tuple, idle box both times) — the tps panels should keep
   same-day A/A arms (they did tonight: A vs B).

Trial assets (reusable): `bench/ab-v0210/rpcprof/{launch-trial-r7.sh,r7-configs.py,
trial-r7{a,b,c}.json,r7-legs.py}`, stop script `run/r7-stop.sh`, logs
`logs/engine-r7{a,b,c}.log`, `logs/r7*-panel.out`, records under
`bench/ab-v0210/rpcprof/out/r7{a,b,c}-20261112/`. Port 8021 cleaned; nothing
touched :8009; no soaks run.

## iq3_s + int8 KV panel (2026-11-12 22:05-22:15, arm V26IQ3S, v0226 pin, trial 8019)

Quant stack: IQ3_S experts/native + PLE q8 + KV int8 (full residency 262144/262144) + dflash q4_0.
Launcher plumbing fixed en route: engine pack/native/native-dense-gguf live in the config ARGS
(the .pack json field is cosmetic), helper --pack/--experts now env-overridable
(STRATA_PACK/STRATA_EXPERTS). Boot: arena cap keeps 11579 pairs (~21.5 GiB); 5027 pairs
(10 GiB) cap-evicted vs iq3xxs's 1404 - bigger S pairs -> bigger storm surface.
Box caveat: swap carried ~7.3 GiB leftover from r7 thrash (reset needs interactive sudo);
panel swap peaked 15.3 GiB at the killer. iq3xxs panel ran fresh-post-reboot (swap ~3).

Results (vs iq3xxs-i8 V25I8 20:18 panel, same tuple/cache flags):
- killer PP: 439.5 vs 645.8 (-32%)
- killer TG cold: 18.6 (!) vs 33.9; warm-ish pass 30.0 vs 35.0
- killerL TG: 30.2/27.5 vs 36.1/41.0
- longtg (29k) TG: 37.8/27.0 vs 42.9/35.0
- tg1024 (1k) TG: 29.7/32.2 vs 38.8/44.8  <- ~-30% even at 1k ctx: NOT the storm;
  this is the S-quant decode path itself (compute/dequant/expert bytes)
- Draft acceptance 62-87% (healthy). Helper 0 timeouts/fallbacks.
- Mem killer leg: maxRSS 45.2 GiB, min_avail 12.0, swap 15.3; other legs RSS ~50, avail 15.4-15.7.

Reading:
1. iq3_s cold-142k TG 18.6 = storm amplified (5x the cap-evicted pairs of iq3xxs) + possibly
   swap pressure - round-8 decode-phase heat work helps iq3_s MOST.
2. iq3_s pays a flat ~-25..-30% decode tax even where no storm exists (1k ctx) - separate,
   kernel/quant-path lead for round 8+ (IQ3_S dequant/expert-bandwidth vs IQ3_XXS).
3. Goal check (TG 25-30 band @ coexistence, ~45k ctx): warm 29k TG 37.8 clears the band;
   cold 27.0 sits IN it; cold 142k 18.6 far below. Memory envelope holds (avail 12-15.7
   even on the swapped box).

## Round 8 fixer launched (2026-11-12 ~22:2x) — storm shrink + S-quant tax

- OPERATOR GAMING: no server/engine/GPU work this round (hard constraint in the brief);
  parent stopped the 8019 trial first (avail 54.3 GiB). No engine boots without asking
  the operator first (standing until further notice).
- Brief: (1) fix the rerank/lent-slot fatal 400 (stale device alias), (2) phase-tagged
  decode heat behind a default-off knob (storm shrink; worth most on iq3_s: 5027 evicted
  pairs vs 1404, cold TG 18.6), (3) IQ3_S flat decode tax analysis (-25..30% even @1k ctx),
  (4) round-6/7 semantics + defaults protected, (5) builds green + CPU-only tests, shas
  for the v0227 pin candidate, no boots.

## Build cache wired (2026-11-12 ~23:4x)

- ccache 4.14.3 built from source into ~/.local (no sudo/brew/conda on box), ~/.local/bin
  already on PATH. max_size 20G (~/.config/ccache/ccache.conf), direct mode on, stats zeroed.
- The trees already carry GGML_CCACHE=ON (ggml-lineage cmake) - detection was NOTFOUND only
  for the missing binary. Any NEW build dir (or reconfigure of an existing one) now picks
  ccache up automatically; no CMake edits needed.
- Round-8 fixer's in-flight build predates the binary - first cached build is the NEXT one
  (v0227+). HIP offload single-invocation clang compiles should cache; check
  `ccache --show-stats` "uncacheable calls" after the first cached build to confirm.

## Operator note to the active fixer (2026-11-12 ~23:5x) — read before GPU decisions

- GAME OVER: GPUs are released. You MAY use the devices now (device tests, quick probes
  on a throwaway port are unblocked). The no-server constraint is LIFTED; only :8009
  stays untouchable, and no soaks.
- ALWAYS SHOW LIVE PROGRESS: run long commands in foreground with streaming output
  (no >/dev/null silencing, no detached builds) so the operator sees progress live
  in the opencode terminal view. If something must stream to a file, tail -F it.

## OPERATOR DIRECTIVE (2026-11-13 ~00:1x): ROCm 6.4.4 FORBIDDEN in builds/tests

- The active fixer was caught running fix7 tests with LD_LIBRARY_PATH=/opt/rocm-6.4.4/lib
  (/opt/rocm-6.1.0 is the ONLY sanctioned toolchain+runtime - every pin v0219..v0226 and the
  prod launcher's LD_LIBRARY_PATH use 6.1.0; /opt/rocm itself is a 7.15.0 install - also not
  sanctioned). Mixed-runtime test verdicts are INVALID: any pass/fail from a 6.4.4 runtime
  does not transfer to the pin environment.
- Parent killed the 6.4.4-env test processes mid-run (operator order).
- RULE for every future round: compile AND link AND test against /opt/rocm-6.1.0 only;
  set LD_LIBRARY_PATH=/opt/rocm-6.1.0/lib explicitly for any test/probe; never let a bare
  `rocm`/`rocm-6.4.4`/`rocm-7.x` path resolve.

## Custom rocBLAS directive + launcher repair (operator, 2026-11-13 ~00:2x)

- FACT: both launchers carried a stale default ROCm root (/home/user/rocm-gfx906-xnack -
  dead on this box). Every boot incl. the S34 floor and all today's panels silently ran
  on STOCK /opt/rocm-6.1.0 rocBLAS, whose gfx906 support ships broken (missing .dat
  metadata, 54 Tensile kernels - see /home/srcds/rocblas-gfx906-bug-report.md).
- The custom build is at /home/srcds/rocm-gfx906-xnack/lib (librocblas.so.4.1 eager,
  kernels baked in) with canonical env in /home/srcds/rocm-gfx906-xnack.env
  (HSA_OVERRIDE_GFX_VERSION=9.0.6, GPU_MAX_ALLOC_PERCENT=100, custom lib FIRST,
  ROCBLAS_TENSILE_LIBPATH unset).
- FIXED in both launchers (trial + prod): default root -> /home/srcds/rocm-gfx906-xnack,
  HSA_OVERRIDE_GFX_VERSION=9.0.6 + GPU_MAX_ALLOC_PERCENT=100 added to the engine env.
- RE-BASELINE NOTE: all numbers 2026-11-11..12 (floor 648.8/44.3, int8 panel, iq3_s panel,
  fixer r7 arms) were measured WITHOUT the custom rocBLAS. They are internally consistent
  (same env) but the champion must be re-measured with the custom rocBLAS before any
  promotion; deltas could shift in either direction.
- Fixer note: its killed 6.4.4 test runs also ran env -u HSA_OVERRIDE_GFX_VERSION (unset!)
  - any rerun must source the canonical env instead. rocm61-toolchain.cmake
  (/home/srcds/rocm61-toolchain.cmake) is the canonical pin toolchain; rocm644-toolchain
  is NOT for pins.

## Round-8 fixer: 6.1.0-clean tests, phase-tag knob landed, IQ3_S tax re-attributed (2026-11-13 0x:xx)

### Contamination audit (what 6.4.4 did and did not touch)

- Every fix7 cmake cache (rpcw-cuda-fix7, rpcw-hip-fix7, and the older fix trees) pins
  HIP_PATH=ROCM_PATH=/opt/rocm-6.1.0. Link audit: the binaries have NO dynamic ROCm
  dependency at all (ROCm runtime statically linked; readelf NEEDED shows only libcudart/
  cublas .so.13 from /usr/local/cuda-13.1 via RUNPATH, resolved independent of
  LD_LIBRARY_PATH). The killed 6.4.4-env test runs therefore never loaded a 6.4.4 byte;
  their verdicts are invalid by rule, not by effect. No rebuild needed; no artifact links
  a wrong lib.
- The bigger find: the aborted runs (and the first 6.1.0 rerun) also inherited a poisoned
  session env - CUDA_VISIBLE_DEVICES set to the EMPTY STRING (CUDA: zero devices, cuInit
  -> 100) and HSA_OVERRIDE_GFX_VERSION=9.0.0 (HIP: VII enumerates as gfx900, the gfx906
  arch filter finds nothing -> "no ROCm-capable device"). All 130 first-run CUDA failures
  were this, not code and not a driver wedge (NVML healthy, steamwebhelper's context
  alive). Canonical env per launch-trial-r7.sh: `env -u CUDA_VISIBLE_DEVICES
  -u HSA_OVERRIDE_GFX_VERSION` + LD_LIBRARY_PATH=/opt/rocm-6.1.0/lib.
- Note for placement: the box now has TWO Radeon VIIs (rocm-smi device 0 and 1, both
  0x66af) + the 3080 + the iGPU; second VII appeared with the GPU release.

### Tests on /opt/rocm-6.1.0 (clean env; device tests live)

- CUDA tree rpcw-cuda-fix7, 293 tests: 289 green. expert_cache_segmented_test,
  prefill_fused_iq_test, prefill_fused_iq_test_nt256 fail only under -j8 GPU contention
  and PASS serially. cuda_spill_guard fails with 4 pre-existing dflash2.cu.o violations
  (gemm_rows_kernel<8,2,4>/<2,2,2>/<4,2,2>, rope_table_inv_kernel; identical in the
  round-6 rpcw-cuda-diet tree, absent in rpcw-cuda base; round-8 diff touches no .cu).
- HIP tree rpcw-hip-fix7, 82 tests: 79 green, 2 skips (ple_parity, pinned_alias_test).
  split_lend_complement_test fails only under its ctest CUDA_VISIBLE_DEVICES=-1 property
  (one real HIP call escapes the --wrap set in the HIP flavor); PASSES with devices
  visible. conversation_copy_streams_test fails the #2409 16 MiB first-stream bound:
  ROCm 6.1.0's first copy stream + event costs ~148-150 MiB on this box; identical
  failure in rpcw-hip-diet and rpcw-hip-fix6 -> box/runtime-level, pre-existing, not the
  branch's. Bound or runtime needs a revisit (conv_cache.hpp kRestoreFirstUseMib=64 is
  4x this bound - check what the restore guard does on 6.1.0 before arming restore).
- arena_pin_ranges_test green in both trees (the pin-range arithmetic CPU suite).

### Landed on helper-ipc-diet (commits)

- a68338ac4 arena pin-range arithmetic (gap/union helpers, CPU-tested)
- 565fffd3f the 400 fix: promote registers the pin-range complement (no overlapping
  cudaHostRegister) + STRATA_ARENA_RERANK_PHASE_TAG (see below)
- 8ab7093c7 set_heat_phase public for generate.cpp's phase boundaries

Knob STRATA_ARENA_RERANK_PHASE_TAG (default 0 = today's mixed heat; =1 plans the re-rank
on decode-phase heat only): expert_source.hpp:1516-1648 (heat_phase_ atomic, decode_heat_
bank, note_heat splits at phase), expert_source.cpp:7177-7190 (the env gate feeding the
re-rank's plan), generate.cpp:21182/21437/22898 (phase 0 at the request boundary and the
final refill/re-rank, phase 1 across the decode loop). Composes with STRATA_ARENA_RERANK_MAX
(the per-boundary bound). Fail-safe: unset/0 keeps round-6/7 semantics bit-for-bit; all
round-6/7 defaults untouched.

### IQ3_S flat decode tax: dequant kernels EXONERATED, re-attributed to the tier/bytes side

Measured on the idle VII, ROCm 6.1.0, iq_multi_parity --bench (fix7 binaries):

- grouped expert GEMV (the decode path), new kernels: IQ3_S/Q2_0 vs IQ3_XXS/Q2_0
  m=1: 147.9 vs 148.3 us, m=8: 497.9 vs 480.1 us -> +0.0..+3.7%, NOT 25-30%.
- single-matrix iq_mmvq 8192x2560 nc=1: 46.7 vs 45.2 us (+3.3%).
- STRATA_IQ_STAGE_TABLES=1 (staged codebook+sign-mask LDS path, iq_kernels.cu:3436): no
  effect on gfx906 (m=8: 498.6 vs 497.9 unstaged; XXS same). Not a lever here.
- Byte geometry: block_iq3_s 110 B/256 w (3.4375 bpw) vs block_iq3_xxs 98 B (3.0625)
  -> +12.2% expert bytes per token; per-32w GEMV read 14 B vs 10 B (qh + scales + fatter
  signs). Kernel time stays ~equal -> the grouped decode GEMV is not bandwidth- or
  dequant-bound at these shapes on gfx906.
- Therefore the v0226 panel's flat -25..-30% @1k ctx is NOT the S-quant dequant path.
  Remaining suspects, in order: (1) the +12.2% pack bytes at the SAME arena cap evicting
  5027 pairs vs iq3xxs's 1404 -> decode set partially served from host tiers even at 1k
  ctx (the panel's own warm/cold gap 29.7->32.2 vs 38.8->44.8 fits tier refill), (2) the
  panel's box state (7.3 GiB leftover swap from r7, panel peak 15.3 GiB; the iq3xxs
  comparison ran fresh-post-reboot) flatly taxing every leg.
- Fix candidate, low risk and ALREADY LANDED: arm STRATA_ARENA_RERANK_PHASE_TAG=1 (+ the
  existing RERANK_MAX bound) so the arena/pin band converges on decode's set - this
  attacks suspect (1) directly and helps iq3_s most (3.6x the evicted pairs). Medium-risk
  alternative: an iq3_s-specific arena cap re-open (the 22000 close was measured on
  iq3xxs-i8). Re-panel iq3_s on a rebooted, swap-drained box to strip suspect (2).

### v0227 pin candidate (builds green on 6.1.0, both trees 100%, no-op rebuild)

- /mnt/srcds/Strata/v0227/build-mixed-gpu/rpcw-cuda/{strata, strata-expert-helper,
  strata-expert-helper-patient}, rpcw-hip/strata; helper pair bit-identical by design
  (same convention as v0226).
- sha256: strata(cuda) 950c8a3bb1cd63ce8b449119071bd9afdd1520a07118b8cec91a79ddb5c9a42c,
  helper 8d37045c010641f9982ae054e9a9aeece84635f163eac03f99e0e341f4a16506 (patient same),
  strata(hip) 9f8a3fb2e703e405f30749716383d73bf723eaa9557cb5b40beb799d7e1d2741.
- Live smoke (throwaway, 6.1.0 env): rpcw-hip strata-device lists device 0 VII
  gfx906:sramecc+:xnack- 60 CU driver 60140091; rpcw-cuda strata-device lists the 3080
  cc 8.6 driver 13020. No engine boot this round; :8009 untouched.

### Left for the parent (live verification)

1. Boot v0227 on a throwaway port with the rerank armed (RERANK_MAX + PHASE_TAG) and
   reproduce the old killer->longtg boundary once: the 400 fix is CPU-tested + wraps
   audited, not yet live-verified. Watch fill_slot_queued through a promote of >few pairs.
2. iq3_s A/A panel on a rebooted box, phase-tag on/off, to split the tier-vs-swap share
   of the flat tax before any cap re-open.
3. Decide whether the second VII joins placement (it shifts device ordinals; configs
   that pin by index need a re-check).
4. The conversation_copy_streams 16 MiB bound vs 6.1.0's ~150 MiB first-stream cost:
   re-baseline or gate on runtime version before the next restore-arm.

## Round 8b — v0227 verification panel (2026-11-13 00:00–00:30, operator "go")

**Env retraction (parent, supersedes the 2026-11-12 "everything ran poisoned" claim).**
The engine child env was NEVER the session poison: server.py `child_env()` builds
`dict(os.environ)` + config-`env` overrides + `lib_dirs` PREPENDED to LD_LIBRARY_PATH, and
both trial configs have always carried `HSA_OVERRIDE_GFX_VERSION=9.0.6`,
`CUDA_VISIBLE_DEVICES=0,1`, `HSA_ENABLE_SDMA=1`, `lib_dirs=[custom-rocBLAS, 6.1.0]`.
My earlier "engine poisoned" reading was a pgrep self-match (pattern text matched my own
wrapper shell). Definitive live proof this round (v0227 control boot): engine
/proc/PID/environ shows 9.0.6 / 0,1 / SDMA=1, and /proc/PID/maps shows
`/home/srcds/rocm-gfx906-xnack/lib/librocblas.so.4.1` — the engine links
`libhipblas.so.2` (6.1.0) which loads the CUSTOM rocBLAS via the lib_dirs order.
Conclusion: v0225/v0226/v0227 boots all ran the canonical stack; no env re-baseline owed.
(The session-level HSA=9.0.0/CVD="" poison in ~/.profile is real but only ever hit
non-config-driven invocations — the fixer's direct ctests; its `env -u` fix is correct.)

**Panels (same night, same box, ab9-v0227-iq3s-20261112; iq3_s + int8 KV + spill40 fill).**
- CONTROL `V27IQ3S_C` (v0227, knobs off): PP 546.8 (killer prefill 142,284 tok);
  TG@142k killerL cold 41.1 / warm 24.1; TG@40k longtg 33.1 / 21.4; TG@1k 30.7 / 32.6;
  RSS 52.8G, min avail 12.7G, swap-in-use 7.4G.
- TREATMENT `V27IQ3S_RR` (v0227, STRATA_ARENA_RERANK_MAX=6000 + PHASE_TAG=1):
  PP 544.4 (parity); TG@142k killerL cold 35.1 / warm 26.1; longtg 34.5 / 29.5;
  TG@1k 28.4 / 32.9; RSS 52.7G, min avail 11.0G. Warmup leg 9.9s vs 2.1s (armed rerank's
  first-boundary pass — the arming cost, visible once).

**Verdicts.**
1. Rerank-400 fix (565fffd3f) VERIFIED LIVE: engine survived armed rerank through both
   killer legs — the exact traffic that produced `fill_slot_queued: invalid argument`
   before. No degrade-to-file events observed either (fallback path never needed).
2. Armed rerank + phase-tag is NOT a clean win (−26%/−45% on killer144k first-pass gens,
   +38% on longtg warm, rest parity; swap volume unchanged at ~12k pairs/leg). Default
   stays OFF for this workload shape; the knob remains available for decode-heavy shapes.
3. Cold-142k TG 18.6 → 41.1 vs the v0226 panel is REAL in direction but CONFOUNDED
   cross-day: per-leg swap-in volume is UNCHANGED (12,324 vs 12,135 pairs; ~24 GiB both)
   — what changed is blocking impact — while box swap pressure halved (13.7G → 7.4G)
   and v0227's arena pin-range arithmetic (a68338ac4) landed. A same-day v0226-vs-v0227
   A/A on a rebooted box is the only clean way to split box-state vs binary credit.
4. Goal frame: control TG@1k 30.7–32.6 and TG@40k 33.1 sit ABOVE the 25–30 band with
   RSS 52.8G; the weak spots are 2nd-gen/warm legs (24.1, 21.4) — the mixed-heat
   pattern the phase-tag knob targets but did not beat tonight.

Engine stopped after measurement (stop-all). v0227 remains pin-candidate; promotion is
the operator's call.

## PROMOTION — v0227 / iq3_s + int8 KV is prod (2026-11-13 00:37, operator approved)

- New prod config `prod-config-swift15-iq3s-int8kv-dflash-w4-mixed.json` (from the trial
  iq3s-int8 config; sockets → prod-swift15-iq3s-mixed{,2}.sock; model_name →
  `swift-1.5-iq3_s-int8kv`; exe → v0227 rpcw-hip; args carry the IQ3_S pack/native/PLE +
  `--kv int8`; env block canonical 9.0.6/CVD=0,1/SDMA=1).
- New prod launcher `launch-prod-swift15-iq3s-int8kv-dflash-w4-mixed.sh` (copy of the
  f16kv launcher; helper → `--pack /mnt/srcds/ai/iq3s --experts
  ../Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S.gguf`; fill 6796 / slots 4600 / reserve 512
  unchanged; repaired env block present).
- Old f16kv launcher + config left untouched as the rollback path.
- Boot verified: :8009 healthy 00:37:33; engine = v0227 (exe symlink); env 9.0.6/CVD=0,1;
  custom rocBLAS mapped via hipblas; smoke: exact-compliance "prod-ok", model string
  correct.
- Known weak spot carried into prod (operator accepted, next work item): warm/2nd-gen TG
  (killerL 24.1, longtg 21.4) vs cold (41.1/33.1) — round 9 attacks this.

## Round 9 — warm/2nd-gen TG slowdown: attribution + the PHASE_TAG=2 fix (2026-11-13, GPU-free forensics)

No engine booted, no GPU touched, :8009 untouched. All numbers are the on-disk byte-window records
(`ab9-v0227-iq3s-20261112`, `ab9-v0226-iq3s-20261112`, `ab9-v0225-int8-20261112`) parsed out of the
elog windows, plus the panel runners' per-gen summary lines.

### Method: the per-gen table (the panel's "TG" number = the engine's own decode rate)

Per gen I derived, from the summary line alone: `read_ms`, `ms/token` of the read, **ms/verify-step**
= `gen_ms / (generated - drafts_accepted)` (one target forward per verify step), **tokens/verify**
= `generated / steps`, `draft acceptance` = accepted/offered, and the expert PCIe rows **per verify
step** from the hit-rate line. The panel's cold/warm labels = the last pair of a leg's gens.

| arm | leg | gen | read tok | ms/tok | ms/verify | tok/verify | acc% | PCIe rows/verify | t/s |
|---|---|---|---|---|---|---|---|---|---|
| V27IQ3S_C | killer144kL 142k | 3 (cold) | 5 | 43.2 | 88.2 | 3.63 | 81.6 | 265 | 41.1 |
| V27IQ3S_C | killer144kL 142k | 4 (warm) | 535 | 9.6 | 91.8 | 2.22 | 57.8 | 224 | 24.1 |
| V27IQ3S_C | longtg 29k | 3 (cold) | 5 | 38.6 | 81.4 | 2.69 | 70.1 | 241 | 33.1 |
| V27IQ3S_C | longtg 29k | 4 (warm) | 215 | 14.8 | **124.9** | 2.67 | 64.5 | 246 | 21.4 |
| V27IQ3S_C | tg1024 2k | 1/2 | 583/5 | 11.6/25.4 | 84.4/77.0 | — | 66.5/62.3 | — | 30.7/32.6 |
| V27IQ3S_RR | killer144kL 142k | 3 (cold) | 5 | 40.0 | 104.9 | 3.68 | 81.9 | 272 | 35.1 |
| V27IQ3S_RR | killer144kL 142k | 4 (warm) | 535 | 9.8 | **86.2** | 2.25 | 63.6 | 206 | 26.1 |
| V27IQ3S_RR | longtg 29k | 3 (warm) | 215 | 14.4 | **97.7** | 2.88 | 68.7 | 254 | 29.5 |
| V27IQ3S_RR | killer144k 142k (1st pass) | 1 | 142285 | 1.8 | **161.4** | 3.63 | 85.7 | 252 | 22.5 |
| V27IQ3S_RR | killer144k 142k (1st pass) | 2 | 535 | 10.7 | **134.9** | 2.39 | 57.8 | 244 | 17.7 |
| V27IQ3S_C | killer144k 142k (1st pass) | 1/2 | 142284/535 | 1.8/10.7 | 119.6/109.5 | 3.63/3.53 | 82.0/83.9 | 264/243 | 30.3/32.2 |
| V26IQ3S | longtg 29k | 3/4 | 5/19 | 39.2/13.6 | 86.0/80.2 | 2.64/3.03 | 77.8/61.0 | 325/310 | 37.8/27.0 |
| V26IQ3S | killer144kL 142k | 2/3 | 5/535 | 40.4/11.2 | 105.2/82.4 | 3.36/1.91 | 73.5/55.2 | 353/283 | 30.2/27.5 |
| V25I8 | killer144kL 142k | 1/2 | 11211/5 | 3.5/39.2 | 72.5/75.0 | 1.68/4.21 | 64.5/71.9 | 530/514 | 36.1/41.0 |
| V25I8 | longtg 29k | 3/4 | 5/215 | 37.2/10.6 | 63.1/61.1 | 3.20/2.29 | 70.7/53.7 | 470/494 | 42.9/35.0 |

### Attribution (the dip is TWO mechanisms plus one wall tax; the arena swap storm is exonerated)

1. **The continuation tail is re-read, and slowly.** Every "warm" gen re-sends the previous gen's
   *generated* tail as prompt: the cache reuses only the published checkpoint, so 535 tokens (142k leg)
   / 215 (29k leg) / 19-18 come back at **9.6-15 ms/token** (5.2 s / 3.2 s), against 1.8-4.3 ms/token
   for a bulk prefill read. Cost scales with ctx (142k: 9.6 ms/tok; 29k: 14.8; 2k: 25 for 5 tokens →
   a ~200 ms fixed mount cost) → it is small-batch attention + mount, **not** KV-int8 requant
   (hypothesis 3 partly true: the fixed ~200 ms part, not the per-token part).
2. **Spec-decode degradation on the continuation gen is the headline drop at long ctx.** C killer144kL:
   cold 3.63 tok/verify → warm 2.22 (−39%), from acceptance 81.6→57.8 **and** proposals/verify
   3.22→2.10, while ms/verify is flat (88.2→91.8, +4%). 100 % of the 41.1→24.1 is tokens-per-verify,
   i.e. the drafter, not the target. The same collapse appears in every arm that has a tail-continuation
   gen (V26 73.5→55.2; V25I8 70.7→53.7 → 42.9→35.0), so it is **not iq3_s-specific** — iq3_s's 1.5-2×
   ms/verify only amplifies the same loss.
3. **A v0227-iq3_s-specific verify inflation on the continuation gen** (the longtg warm leg): 81.4→124.9
   ms/verify (+54 %) at flat tokens/verify and flat expert traffic. n=1 in the C arm; V26's continuation
   gens show no inflation (86.0→80.2) and the RR arm removes it (97.7). Placement-linked, plan-sensitive.
4. **Wall tax (not a rate):** the conversation-cache idle park streams the whole conversation to disk
   after every request — 4.8-5.7 s a park, 20.3 s inside the 120 s killer144kL wall (17 %), and it does
   NOT overlap the next request's read. That is where the residual wall time is; read+decode+park =
   131.8 s vs the 120.1 s wall (partial overlap). Prod-config level, not TG-rate.
5. **Exonerated, with numbers:** expert traffic *inside* the warm decode is flat — PCIe rows per verify
   step 265→224 (killerL), 241→246 (longtg), hit% 98.5→93.6 / 96.1→95.6 (the % moves only because the
   lookup totals shrink). The 11-12k-pair / ~24 GiB swap-ins land **before the cold gen**, inside its
   44-48 s read window (13.5 s of file read absorbed there); the warm gens' swap-ins total 0.06-0.75 s.
   So hypothesis 1's "warm gen pays swap-ins at decode time" is FALSE as stated; hypothesis 2's
   "prefill-hot placement decays" is the real one (see the RR asymmetry); hypothesis 4 (helper drift)
   shows nothing (helper reqs/rows per verify step constant, timeouts=0, fallbacks=0).

### The RR asymmetry, explained and reproduced on CPU

Phase-tag (=1) plans the boundary re-rank on the decode bank. The promote side is then decode's own set
(the warm recovery: 124.9→97.7 and 91.8→86.2 ms/verify, acceptance +4.2/+5.8 pts). The demote side
becomes *every resident pair decode never touches* — which on a cap-bound band is the long read's
sweep: the wide set a fresh 142k-context request's first-pass gens need. That is exactly the measured
harm (first-pass 119.6→161.4 and 109.5→134.9 ms/verify, killerL g1 92.1→127.5). The panel's own
swap volume is unchanged between the arms, so the harm is placement, not bytes.

Landed as **STRATA_ARENA_RERANK_PHASE_TAG=2** (+ **STRATA_ARENA_RERANK_READ_WEIGHT=0..8**, default 2):
plan on the decode bank **plus read_weight/8 of the read bank**, so the promote follows decode's demand
(the round-8 win) while a read-hot resident keeps a floor the promote must strictly beat (the
prefill-hot placement survives — round-8's harm). Pure host arithmetic in
`arena_rerank_heat()` (`include/strata/core/relief_arena_cap.hpp`), fed by the two banks in
`ArenaExpertSource::reorder_arena_by_heat()` (`src/core/expert_source.cpp:7177+`); tag 0/1 semantics
untouched, default 0 = prod today, bit-for-bit.

CPU replay of the panel shape (`tests/core/expert_helper_arena_cap_test.cpp` cases 24-25, real planner):
- tag 0 (mixed): 1800 swaps spent on the read sweep's evictions, decode's warm set promotes **0**,
  800 of the current decode set's residents demote → the round-7 starvation, reproduced.
- tag 1 (decode): 1200 swaps, the whole warm set lands, **200 sweep pairs demote** → the round-8 harm, reproduced.
- tag 2 rw=2: 1000 swaps on the both-bank-cold residents, the promote starts at decode's set,
  **0 sweep pairs demote, 0 current-decode-set demote** → recovery without the harm.
- tag 2 rw=0 ≡ tag 1; tag 2 rw=8 restores the starvation (the read bank must be a floor, not a ranker).

### Checks (clean 6.1.0 env: `env -u CUDA_VISIBLE_DEVICES -u HSA_OVERRIDE_GFX_VERSION -u HSA_ENABLE_SDMA`)

- `expert_helper_arena_cap_test` + the arena/helper suite: 6/6 pass; full host subset of the CUDA tree
  (`ctest -E "parity|bench|mmvq|gemv|dequant|prefill_fused|spill|cuda|gpu|vision|graph|native|iq_|kq_"`):
  **112/112 pass** (3 skipped by their own guards). Log:
  `/mnt/srcds/Strata-wt-helper-ipc/build-mixed-gpu/v0228-ctest-host.log`.
- Both trees rebuilt 100 % on 6.1.0 (`v0228-hip-build.log`, `v0228-cuda-build.log`); the helper pair is
  bit-identical to v0227 (same SHA), so no launcher/helper change is implied. `strata --help` runs.

### v0228 pin candidate

`/mnt/srcds/Strata/v0228/build-mixed-gpu/` — `SHA256SUMS`:
- strata(cuda) `034f60781a1f0fd5edbebe6301f755a616e0fc99b3b6e95f29311f48d5b8cb5f`
- strata(hip) `cdf99f568627f89d957f76053a0f7c9804202848c6ff078a95fddf39eda01f4f`
- strata-expert-helper / -patient `8d37045c010641f9982ae054e9a9aeece84635f163eac03f99e0e341f4a16506` (= v0227)

### Needs a GPU A/B (operator's box)

1. `STRATA_ARENA_RERANK_PHASE_TAG=2` + `STRATA_ARENA_RERANK_MAX=6000` on the same panel: expect the RR
   warm recovery (longtg 21.4→~29, killerL warm 24.1→~26) **without** the first-pass damage
   (killer144k gens must return to 30.3/32.2). Then sweep `STRATA_ARENA_RERANK_READ_WEIGHT=1/2/4` —
   the floor's strength is the only free parameter.
2. The drafter's continuation refill (mechanism 2, the biggest single number): measure dflash
   proposals/verify over the re-read tail — 3.22→2.10 proposals/verify is a drafter-state effect,
   outside the arena, and unaddressed by this knob.
3. The idle-park wall tax (4.8-5.7 s per boundary): only worth a knob if the wall, not the rate, matters.
4. V25I8-vs-iq3_s warm comparison is only valid on same-shaped legs: V25I8's panel legs ran 2 gens
   (its "warm" gen re-read 5 tokens = a pure cache hit, no tail), so it had no continuation to dip on.

## Round 9b — v0228 gates (2026-11-13 01:45–03:05, autonomous window)

**Quality gate: GREEN.** CUDA 293: 290 pass, prefill_fused_iq(+_f16) pass serially
(contention flakes), cuda_spill_guard = documented pre-existing dflash2. HIP 82: 80 pass,
conversation_copy_streams = pre-existing #2409, split_lend_complement PASSES direct with
devices visible (ctest CVD=-1 quirk; NOTE: the direct run must scrub the session's empty
CUDA_VISIBLE_DEVICES or it reports "no ROCm-capable device"). Round-9 arena tests 6/6 +
host 112/112 (fixer). Nothing new vs the round-8 exception list.

**Speed gate: PARTIAL — same-window verdict + a bigger discovery.**
- Same-window A/B (quiet box, 02:36–03:00, fresh boots, out/ab9-v0228-clean):
  tag2 (PHASE_TAG=2 + RERANK_MAX=6000) vs knobs-off v0228:
  PP 489.1 vs 496.2 (parity); killer144k gen 25.0 vs 16.4; killerL cold 26.0 vs 19.7;
  killerL warm 31.9 vs 27.4; longtg cold 42.8 vs 35.2; tg1024 29.0/32.8 vs 32.7/35.4
  (−7..−11%); RSS +~1 GiB. The knob DOES what round-9 designed: warm/continuation legs
  recover, first-pass long gens improve, short-ctx pays a little.
- DISCOVERY (blocks the v0227-vs-v0228 verdict): the cold-storm blocking is BISTABLE
  across boots — killerL cold gen measured 41.1 (v0227 boot 23:54), 26.4 (02:21),
  19.7 (02:36) with unchanged swap-in volume (~12k pairs/leg all night). After 5 boots +
  2 ctest suites the box's page-cache/swap state drifted; cross-boot comparisons tonight
  are invalid. Settling v0227 vs v0228-default vs v0228+tag2 needs a REBOOTED box and
  interleaved boots (A/B/A/B) in one window.
- v0228 NOT promoted (prod stays v0227); pin + shas verified (hip cdf99f56,
  cuda 034f6078, helper 8d37045c bit-identical to v0227).

**Incidents + protocol lessons (parent errors, recorded honestly):**
1. Round-10 fixer (drafter-collapse forensics, spawned during the gate window) died of
   ConnectionRefused when prod came down mid-turn — the 2026-11-06 hard rule exists for
   exactly this; treat "prod-down window while any subagent runs" as forbidden regardless
   of the 2026-11-11 moot note. It left no code (worktree clean at df8a94877).
2. Its host builds contaminated control panel #1 (killer prefill 401 s vs 308 s).
3. Two control panels were lost to a relative elog path bug ($RPC/../../logs resolves to
   bench/logs — always pass ABSOLUTE elog paths to panel.sh).
RULE going forward: gate windows run ALONE — no subagents, no builds, nothing else on the box.

**State:** prod v0227 iq3_s+int8 restored 03:04:30 (smoke: exact "prod-ok"). Round-10
respawn (analysis-only) next; rebooted-box interleaved A/B is the next GPU task.

## Round 10 — spec-decode collapse on continuation gens (2026-11-13, respawn, analysis-only)

No builds, no GPU, :8009 untouched. Data = the byte-window records of ab9-v0227/v0226/v0225/v0228-clean
parsed with the new `bench/ab-v0210/rpcprof/r10-verify-curve.py` (derives acc/off, steps, tok/V, prop/V,
ms/V per gen from the engine's own summary lines). Code read on the v0228 worktree (df8a94877).

### The per-gen facts (the decisive rows)

| arm | mount + re-read | gen position | acc% (off) | tok/V | ms/V | verdict |
|---|---|---|---|---|---|---|
| V27IQ3S_C | pair-restore, 5 tok | 1st pass g2 / L g3 | 82.0/83.9, 81.6 | 3.63 | 88–120 | healthy |
| V26IQ3S    | pair-restore, 535 tok | **1st pass g2** (2nd gen) | **85.2** (411) | 3.43 | 114.5 | **healthy** |
| V27IQ3S_C  | pair-restore, 535 tok | **L g4** (4th gen, 142k) | **57.8** (282) | 2.22 | 91.8 | **collapse, ms/V flat** |
| V26IQ3S    | pair-restore, 535 tok | L g3 (4th gen) | 55.2 (87) | 2.26 | 82.4 | collapse |
| V28IQ3S_T2 | pair-restore, 535 tok | L g2 (4th-gen ctx) | 60.2 (244) → **73.5 next gen** | 2.52 | 96.6 | collapse, gen-local |
| V28IQ3S_C3 | pair-restore, 535 tok | L g3 | 62.9 (229) | 2.36 | 86.1 | collapse |
| V27IQ3S_RR | pair-restore, 535 tok | 1st pass g2 | 57.8 (173) | 2.39 | 134.9 | collapse + RR ms/V inflation |
| V27IQ3S_C  | cold-rewind@131072, 11213 tok reread | L g1 (quiet boot) | **81.5** (459) | 3.63 | 92.1 | healthy (window rebuilt by the reread) |
| V28IQ3S_C3 | cold-rewind@131072 | L g1 (02:36 storm boot) | 55.8 | 2.19 | 156.0 | bistable-storm artifact, not drafter |
| all arms   | 215 tok reread @29k | deep gens | 53.7–71.6 (−3..−23 pts) | — | — | intermediate dip |
| all arms   | tg1024 (2k ctx) | any | 62–73 | 2.5–2.8 | — | **no dip** |

Pattern: dip = pair-restore mount × tail re-read × generation depth, scaling with ctx
(142k: −15..−24 pts; 29k: −3..−23; 2k: 0). Healthy at the 2nd gen on the SAME mount+re-read,
collapsed at the 4th; the next gen is healthy again (V28T2 60.2→73.5). Not iq3_s-specific, not
knob-specific (tag-2 arm collapses too), not arena-related (RR only inflates ms/V).

### Q1 — why proposals/verify 3.22→2.10: the confidence gate, not a budget

`--spec-min-p 0.5` is set and df_auto is OFF (block fixed 5; the auto policy only engages with
spec-min-p ≤ 0 and no block: generate.cpp:21395). The verify window is T = 1 + the count of leading
drafts whose top-1 probability ≥ 0.5 — `choose_window()` `include/strata/spec/window_rule.hpp:56-69`,
dprob from the dflash2 selector (`src/core/dflash_runtime.cpp:1505-1516`, probs out 1529-1532). So
proposals/verify 2.10 = the drafter's OWN probabilities fell under 0.5 after the 2nd draft on average:
the gate is a symptom of draft degradation, not an adaptive step-down; no per-token cap exists.

### Q4 — the exact cache-hit re-read path (v0228 tree)

Mount (continuation gens): `generate.cpp:19706` ring check → pair branch `19708-19725` →
`ring_pair_restore()` `13732-13770` (sidecar ring image = FULL ring rows + exact counters via
`restore_snapshot()` `dflash_runtime.cpp:899-915`); cold branch `19729-19745` →
`rewind_cold()` `dflash_runtime.cpp:922-940` (counters=resume, window rows below are the PREVIOUS
turn — the documented stale-row refill). Disk-park restore (L g1): from_live path `19617-19653`
(the ring ships inside the v2 file, the pair block is skipped). Re-read rows append through the
prompt-path tap callback `11835-11846` → `add_context` `dflash_runtime.cpp:792-860`
(gap guard 811-814, note_append 842, true rope positions). Decode rounds append via
`add_context_f32` `22728-22732`.

Ring math (`include/strata/core/dflash_ring.hpp:46-52`, window 2048, ring≈2304): after
pair-restore@142280 + 535 re-read, the first propose's window [140769, 142816) is fully covered =
restored rows [140769..142280) + fresh appends [142281..142816). **The drafter's KV is provably
intact on this path — no truncation, no proposal misalignment over cache-hit tokens.** The
cold-rewind staleness self-heals: its 11k-token reread rebuilds the 2048-row window before decode,
which is why quiet boots run it at 81.5. Tap fidelity is also exonerated: the 142k bulk prefills
append the SAME bf16 prompt plane for 11-142k rows and run 81.5-82.0 acceptance.

### Q2/Q3 — attribution as far as aggregates allow

With gate, KV state, cold-rewind staleness, and tap fidelity exonerated, the collapse is the
drafter's **conditioning** going stale against the target at 142k as generation depth grows: the
target's greedy top-1 deep in a 142k conversation conditions on history far beyond the drafter's
2048-row SWA (the trained geometry); the window is intact but structurally blind, and depth
amplifies the mismatch (2nd gen healthy, 4th collapsed; 29k mild; 2k none). This is the leading
hypothesis, NOT proven: per-gen aggregates cannot see within-gen curves (STRATA_SPEC_TRACE
`generate.cpp:24441-24448` is CLI-only; the serve loop has no per-round trace). Blend arithmetic
for C-arm L g4 (57.8 avg): full recovery fits a ~50-verify cold start (≈37 % of steps); flat
depression fits equally. The curve decides — next round.

### Design for the GPU round (nothing built this round)

A. **Serve-side curve instrumentation** (~10 lines, host-buildable): reuse the `STRATA_SPEC_TRACE`
env — open the file per request at the serve request start (`18815-18850` region) and append
`round T accepted offer ms pos` in the round-end block (`22743-22758`, after `++df_windows[T-1]`).
Curves = first 50 verifies vs the rest; decides recovery vs flat.

B. **L-leg ablations, one boot each, quiet box** (panel fixture `ab9_*`, killerL leg):
1. instrumented baseline (repeat of tonight's numbers);
2. `STRATA_CKPT_REREAD=1` (exists, `generate.cpp:19726`): full reread rebuilds the draft context,
   NO pair restore. Healthy → the restore+tail combo is the trigger; collapses → depth/content
   conditioning confirmed;
3. `--spec-min-p 0.0`: fixed 6-row windows → raw acceptance per offer + full-window ms/V.
   Arithmetic says the gate is ~break-even at 142k (q≈0.43: 2.55 tok/105 ms vs 2.22 tok/91.8 ms);
   measure;
4. **new knob `STRATA_DF2_WINDOW`** (~6 lines): geometry override of the readable SWA window at
   upload (`dflash_runtime.cpp:446-475` — window_, ring via the df_ring_mult path 461-472, scratch
   sizing 557-558/609) and the kernel arg site (`1356-1361`). Sweep 4096/8192: tests the
   beyond-2048-blindness hypothesis directly. VRAM cost is small (ring pools are the only growth:
   ~47 MiB → ~172 MiB for mult 4 at n_head_kv=8). CPU test: `tests/core/dflash_ring_test.cpp`
   mult + window cases;
5. fixture control, no engine code: L-leg variant whose 535-tail = the prompt's own last 535
   tokens (not a generated tail) — isolates generated-tail content from re-read mechanics.
6. secondary: `--kv f16` boot on L-leg isolates int8 requant of the 535 re-read rows.

C. Decision matrix: curves cold→recover → first-N-tokens target-only priming on continuation gens
(cheap); curves flat + CKPT_REREAD healthy → restore-path conditioning, bisect with an f32-tail-taps
patch (prompt plane `11843` vs verify plane); flat + CKPT_REREAD collapses → conditioning beyond
2048: STRATA_DF2_WINDOW sweep is the fix candidate + spec-min-p retune. Prod today: no change is
warranted by this round; the drafter dip is a decode-rate issue on long-ctx continuations only.

**State:** no engine changes. New file: `bench/ab-v0210/rpcprof/r10-verify-curve.py` (parser,
committed with this section). Prod untouched. Next GPU tasks in order: rebooted-box interleaved
v0227/v0228 A/B (round 9b), then B.2 (CKPT_REREAD) + curve trace, then the DF2_WINDOW patch.

## Round 11 — DF2_WINDOW knob + trace readiness (2026-11-13 04:00–04:30, parent-built)

The fixer lane failed twice this round (two silent no-op completions, no artifacts) — the
parent implemented round-10's design directly. Two design corrections found while doing it:
1. Phase A needed NO code: serve-mode per-round tracing ALREADY exists — STRATA_SPEC_TRACE
   opens a file in the serve loop (generate.cpp:21369) and writes "# request prompt …" per
   request (22180) and "R p T a sfx ms k match | probs …" per verify round (22869+). The
   panels never set the env; round-10's "the serve loop has no per-round trace" was a
   misread of the CLI-only block at 24441. The GPU window just sets STRATA_SPEC_TRACE.
2. Phase B4 shrank to a geometry knob: STRATA_DF_RING_MULT (retention) already existed and
   left the readable window at the trained 2048; the new knob widens the window itself.

LANDED (helper-ipc-diet 50e16d04d):
- include/strata/core/dflash_ring.hpp: dflash2_effective_window(sliding_window, env) — pure
  helper; powers of two above the trained window only, default/unusable values keep the
  trained geometry bit-for-bit.
- src/core/dflash_runtime.cpp: ring capacity max(effective window, sliding_window*mult);
  ring_state_.window = effective window; the dflash2_attention kernel arg reads
  ring_state_.window (was hardcoded dg.sliding_window). Scratch/pool sizing follows the
  ring capacity unchanged.
- tests/core/dflash_ring_test.cpp: 11 new checks (refusals, 4096/8192, capacity 4160 =
  65 pages, pools 85,196,800 B ≈ 81 MiB at 4096) — ALL PASS (standalone g++ build + the
  fix11 tree's binary would not generate a make target for it in these configures; the
  direct compile is the test's documented mode: pure header, no links).

PIN v0229 (/mnt/srcds/Strata/v0229/build-mixed-gpu/ + SHA256SUMS):
- rpcw-hip/strata 14b97a6c… (fix11 build; knob string present; --help runs; built with
  STRATA_HIP_GFX906 + DFLASH2=ON + HAVE_DFLASH2=OFF, prefix paths = custom rocBLAS +
  6.1.0 — NOTE: a fresh configure WITHOUT -DCMAKE_PREFIX_PATH pulls /opt/rocm (7.15)
  headers and fails; always pass it)
- rpcw-cuda/{strata, strata-expert-helper} = byte-exact COPIES from v0228 (034f6078… /
  8d37045c…): my fix11 helper rebuild differed (fresh-dir config drift, +550 KB) so the
  pin deliberately reuses the prod-proven CUDA side; the knob exists only in the HIP
  engine, which is the only binary the serve path runs.

DECISIVE GPU WINDOW RECIPE (rebooted box, interleaved, one window — everything left):
Arms (each: fresh boot, panel.sh legs warm/killer144k/killer144kL/longtg/tg1024,
ABSOLUTE elog paths, no other load on the box):
1. v0227 (prod binary) — baseline arm A
2. v0228 default (knobs off) — binary-delta arm; A/B/A/B order with arm 1
3. v0228 + STRATA_ARENA_RERANK_MAX=6000 + STRATA_ARENA_RERANK_PHASE_TAG=2
4. v0229 + STRATA_SPEC_TRACE=/mnt/srcds/Strata/logs/spec-trace-<arm>.txt (curves!)
5. v0229 + trace + STRATA_CKPT_REREAD=1 (killerL only needs deciding power)
6. v0229 + trace + --spec-min-p 0.0 (config args edit)
7. v0229 + trace + STRATA_DF2_WINDOW=4096; if flat-curve+CKPT_REREAD-collapse per round-10's
   matrix, also 8192
Then r10-verify-curve.py on the traces → round-10's decision matrix → the fix.

## Round 11b — fixer verification pass + launch-grid fix, v0229 re-pinned (2026-11-13 04:55–05:40, host-only)

No GPU, no :8009 contact, no engine boots (`--help` smoke only). Build dirs: rpcw-{cuda,hip}-fix11.
The round-11 parent work (50e16d04d + pin) verified end-to-end; one real bug found and fixed.

### Verified (round-10 design vs the tree)

- Phase A = zero code, confirmed by audit: serve-mode trace is live — `generate.cpp:21369` opens
  STRATA_SPEC_TRACE in append mode, `22180` writes `# request prompt N df2 x spec y`, `22869` writes
  per verify round `R p T a sfx ms k match | probs(7) | drafts(8) | lookup k | committed a+1`. Gate
  `timed_round = !first_window` (22286) = every decode window of the request. Fields cover the
  design's asks (verify idx = line order, proposals = T−1, acceptance = a, drafter probs, ms/verify,
  pos). CLI block 24441 untouched.
- B2/B3 knobs confirmed: `STRATA_CKPT_REREAD` at 19726 (full re-read, no pair restore);
  `--spec-min-p` parsed 4090, documented 1351; df_auto interlock at 21395 as round-10 read.
- v0229 pin: `sha256sum -c SHA256SUMS` all OK; `rpcw-cuda/strata-expert-helper` =
  8d37045c010641f9982ae054e9a9aeece84635f163eac03f99e0e341f4a16506 = byte-identical to
  v0227's and v0228's helper.

### The bug (why the DF2_WINDOW sweep needed cd4c26e9e)

`attention128_kernel` starts each row's attended span at `end − rows − window + 1` (the passed
window), while the host capped the launch grid at `min(scratch_cap_, dg.sliding_window + K)`
(dflash_runtime.cpp:1225). With STRATA_DF2_WINDOW=4096 the grid covered only 2048+K chunks from
the (widened) window's start: the sweep would have attended the OLD first half and never launched
the newest rows — a shifted-window artifact, not the depth test. Fixed: the capacity reads
`ring_state_.window` like the kernel arg site (1365). Knob off ⇒ `ring_state_.window ==
sliding_window` ⇒ bit-for-bit. Scratch/pools already sized from the ring (≥ window+rows), unchanged.

- branch helper-ipc-diet: 50e16d04d (knob, parent) + **cd4c26e9e** (launch-grid fix, this pass)
- **v0229 rpcw-hip/strata re-pinned: e33b3e7d90208b939f2b37e52c6e54ec02d019dc111318e901332cd4115fc7d1**
  (was 14b97a6c…, grid-capped); SHA256SUMS regenerated, `sha256sum -c` OK; knob string present,
  `--help` exits 0. rpcw-cuda side unchanged (034f6078… / 8d37045c…, prod-proven v0228 copies).

### Parser: `r10-verify-curve.py --trace FILE ...`

Parses serve STRATA_SPEC_TRACE files: per request (= one generation) prints rounds, tokens, acc/off,
acc%, tok/V, prop/V, ms/V, and the round-10 decision buckets — rounds 1–10 / 11–25 / 26–50 / 51–100 /
101+ with per-bucket acc%, ms/verify, tok/V. Cold-start recovery vs flat depression reads straight
off the buckets. Fixture-tested; the byte-window summary mode is untouched.

### Host tests (clean env: `env -u CUDA_VISIBLE_DEVICES -u HSA_OVERRIDE_GFX_VERSION -u HSA_ENABLE_SDMA LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib`)

| check | result |
|---|---|
| dflash_ring_test standalone g++ (11 DF2_WINDOW checks: refusals, 4096/8192, capacity 4160, 81 MiB pools) | all pass |
| ctest subset ×20 (dflash ring/taps/head_bf16/rope/owned_kv/policy/refusal/tap_plan/tap_slot, window_rule, spec verify/proposal/start, conversation file/v2/stream_state/reclaim/disk_cache/memory) on cuda-fix11 after cd4c26e9e | 19 pass, 1 skip (dflash_tap_collect_test: needs 2 GPUs, exit 77 gate) |
| cuda-fix11 full `strata`+helper + hip-fix11 `strata` rebuild | 0 errors, both link |
| hip-fix11 make up-to-date pre-fix; binary sha == pin | confirmed |

### DECISIVE GPU WINDOW (turnkey; reboots the box first, interleaved A/B/A/B, quiet machine)

Boot env for EVERY arm (the session env is poisoned; panel.sh must inject):
`env -u CUDA_VISIBLE_DEVICES -u HSA_OVERRIDE_GFX_VERSION -u HSA_ENABLE_SDMA LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib`

Arms (fresh boot each; legs warm/killer144k/killer144kL/longtg/tg1024; ABSOLUTE elog paths;
the L-leg gens are the deciding rows):
1. v0227 (prod) — baseline A
2. v0228 default — binary-delta B (A/B order with 1)
3. v0228 + `STRATA_ARENA_RERANK_MAX=6000 STRATA_ARENA_RERANK_PHASE_TAG=2`
4. v0229 (`/mnt/srcds/Strata/v0229/build-mixed-gpu/`, hip strata e33b3e7d…) +
   `STRATA_SPEC_TRACE=/mnt/srcds/Strata/logs/spec-trace-v229-base.txt` — curves baseline
5. v0229 + trace → `spec-trace-v229-reread.txt` + `STRATA_CKPT_REREAD=1`
6. v0229 + trace → `spec-trace-v229-minp0.txt` + engine args `--spec-min-p 0.0`
7. v0229 + trace → `spec-trace-v229-win4096.txt` + `STRATA_DF2_WINDOW=4096`; add an 8192 boot
   (→ win8192) only if the 4096 bucket curve is flat AND arm 5 collapses (round-10 matrix C)
Then: `r10-verify-curve.py --trace /mnt/srcds/Strata/logs/spec-trace-*.txt` → buckets decide
(cold→recover = first-N priming; flat+reread-healthy = restore-path conditioning, f32-taps bisect;
flat+reread-collapses = DF2_WINDOW is the fix candidate + spec-min-p retune).

## Round 11 addendum — the "dead" fixer delivered; v0229 RE-PINNED (2026-11-13 06:05)

Correction to the round-11 record: fixer attempt #1 (ses_e2d22f3c…) did NOT no-op — it
worked asynchronously and committed cd4c26e9e at 05:55, after the parent's 50e16d04d:
the launch grid for dflash2_attention must follow the widened window
(attention_capacity was still dg.sliding_window + K, while the kernel's span math
targets ring_state_.window — a DF2_WINDOW>2048 knob would silently attend the wrong
rows; default-off stays bit-for-bit). A genuine correctness bug in the parent's knob,
caught by reading the kernel's span arithmetic. The "silent no-op" conclusion was wrong;
the lane fails to REPORT, not always to work.

v0229 re-pinned from cd4c26e9e: hip strata e33b3e7d… (supersedes the stale 14b97a6c,
which was built pre-fix and carries the bug — do not boot 14b97a6c with DF2_WINDOW set);
cuda side unchanged (byte-exact v0228 copies). Ring tests unaffected (host math).

## Round 12 — the B1 flip verdict + ranked fix designs (2026-11-13, fixer, analysis-only)

No GPU, no builds, no :8009, no engine boots. Data: the byte-window records of
`ab9-decisive-20261113` (D01_V27 = v0227 knobs-off, D02_V28T2 = v0228+RERANK_MAX=6000+
PHASE_TAG=2, boots 10:50/11:03), `ab9-v0228-clean` (C3/T2, 02:36/02:49) and
`ab9-v0227-iq3s-20261112` (C/RR, 23:54/00:10) via `r10-verify-curve.py`, plus boot logs
(`experts loaded … GiB/s` is in every `trial-spill40-*.log` — a free per-boot file-path
regime probe nobody was reading). Code read on helper-ipc-diet cd4c26e9e.

### A1 — the arm × leg × gen table (t/s · acc% · tok/V · ms/V · read ms/tok)

Decisive gens only (full dump: `r10-verify-curve.py ab9-decisive-20261113 ab9-v0228-clean
ab9-v0227-iq3s-20261112`); "cold" = the 5-token pure-mount gen, "tail" = 535/215-token
generated-tail re-read gen.

| window/arm | killer144k g1 (142k storm) | killerL g1 (cold-rewind 11k) | killerL cold (5 tok) | killerL tail (535) | longtg g1 (12.7k reread) | longtg cold (5 tok) | longtg tail (215) | tg1024 g1/g2 |
|---|---|---|---|---|---|---|---|---|
| 23:5x V27_C | 30.3 · 82.0 · 3.63 · 119.6 · 1.8 | 39.5 · 81.5 · 92.1 | 41.1 · 81.6 · 3.63 · 88.2 | 25.0 · 65.2 | 33.2 · 70.4 · 86.9 | 33.1 · 70.1 · 2.69 · 81.4 | 21.4 · 64.5 · 124.9 | 30.7 · 66.5 / 32.6 · 62.3 |
| 23:5x V27_RR | 22.5 · 85.7 · 161.4 · 1.8 | 22.3 · 69.9 · 127.5 | 35.1 · 81.9 · 104.9 | 26.1 · 63.6 | 26.7 · 59.6 · 93.9 | 34.5 · 79.5 · 95.9 | 29.5 · 68.7 · 97.7 | 28.4 · 69.4 / 32.9 · 64.3 |
| 02:5x V28_C3 | 16.4 · 65.3 · 162.7 · 2.0 | 14.0 · 55.8 · 156.0 | 19.7 · 85.9 · 189.5 | 27.4 · 62.9 · 86.1 | 20.6 · 66.7 · 128.2 | 35.2 · 68.4 · 75.4 | 30.2 · 65.4 · 84.9 | 32.7 · 66.8 / 35.4 · 67.4 |
| 02:5x V28_T2 | 25.0 · 73.4 · 118.3 · 2.0 | 28.1 · 75.5 · 113.9 | 31.9 · 73.5 · 92.8 | 26.0 · 60.2 · 96.6 | 32.6 · 80.6 · 103.1 | **42.8** · 79.4 · 76.0 | 29.8 · 71.6 · 92.6 | 29.0 · 68.1 / 32.8 · 69.5 |
| 11:2x D01_V27 | 18.3 · 67.4 · 152.2 · 2.3 | 29.1 · 65.1 · 93.8 | 26.9 · 81.6 · 134.7 | 17.4 · 55.9 · 120.1 | 35.8 · 80.2 · 92.6 | **40.7** · 81.8 · 82.8 | 30.7 · 61.8 · 67.6 | 32.6 · 77.6 / 32.0 · 65.5 |
| 11:2x D02_T2 | 11.8 · 62.0 · 220.1 · 2.0 | 21.6 · 65.7 · 128.9 | 25.0 · 59.7 · 102.6 | (no tail gen) | 20.6 · 86.4 · 174.2 | **18.9** · 80.5 · 167.3 | 20.6 · 73.6 · 124.5 | 30.3 · 89.4 / 25.9 · 69.7 · 103.8 |

### A2 — the flip is decode-rate (ms/verify), full stop

The two quoted flips decompose cleanly:
- longtg cold 42.8 (T2@02:5x) → 18.9 (D02@11:2x) vs D01's 40.7: acc FLAT (79.4 / 80.5 /
  81.8), tok/V flat (3.25/3.16/3.37), read ms/tok flat (38.0/39.6/39.2), expert rows/V
  flat (244/247), decode hit% flat (98.2/97.5), helper timeouts 0 — **ms/verify 76.0 →
  167.3 vs 82.8** is 100%+ of the loss.
- tg1024 32.6 → 25.9 vs D01 32.0: acc 65.5 → 69.7 (tag2 BETTER), ms/V 80.3 → 103.8.
- Same for killer144k g1 (ms/V 220.1, the worst ever recorded) — D01's own 152.2 shows
  the morning inflation was already there before the knob arm.
- Spec-decode (accepted/offers) did NOT flip: tag2's cold-gen acceptance benefit
  reproduced identically in both windows (longtg g1 86.4 T2 AND D02 vs 66.7 C3 / 80.2
  D01), and the 535/215-tail depth collapse (55–63%) sits in every arm of every window.

### A3 — swap-in: volume and placement constant; the ms/slot regime splits by boot

Per-leg swap-in totals (byte-window attribution: g1-read storm vs boundaries):
- storms: killer144k ≈ 197k swaps, killerL/longtg ≈ 10.8–11.2k, tg1024 ≈ 1.1k — flat
  across ALL 7 boots. Boundary swap-ins: 35–1800 ms per leg, always at mount time
  (between gens), never inside decode windows — placement unchanged in both regimes.
  The round-9b "blocking bistability with flat volume" is NOT swap-ins landing in decode.
- **ms/slot is bimodal by boot**: 1.21–1.32 (V27_C, V27_RR, D01) vs 2.02–2.23 (V28 C-lost,
  C3, T2, D02). Boundary slots are always ~1.1 (page-cache-hot); the storm slots halve.
- The regime is set at BOOT, before any leg or knob: expert-load rate D01 1.60 GiB/s vs
  D02 1.17 GiB/s (slowest of all 70+ boots on record); two aborted boots right after the
  panel read 1.73 and 2.19 GiB/s — the file path swings per boot, it does not decay.
- Box drift over the morning is real and monotone: swap-in-use 7.8 GiB (D01 warm, 11:06)
  → 10.4–11.3 (D01 legs) → 9.6 (D02 warm, 11:20) → **13.2 GiB peak during D02's
  killer144k (11:32)**; min avail 14.5 → 11.5 GiB. D02's legs ran while the box was
  actively swapping the game; D01's did not.

### A4 — verdict + the one-boot-pair discriminator

**Verdict: B1 was morning box-regime drift, not tag-2.** The knob's mechanisms (heat
planning) act on acceptance/placement — both reproduced identically at 02:5x and 11:2x —
and it has no path to doubling ms/verify at flat acceptance, flat expert traffic and flat
helper traffic. What flipped is the box's decode service time between D01's boot
(10:50, pre-thrash, load 1.60 GiB/s, storm slots 1.2 ms) and D02's boot (11:03,
worst-ever load 1.17 GiB/s, storm slots 2.2 ms, +3 GiB less avail, game swap-storm
peaking mid-panel). Two host-side candidates fit the signature (decode 2× at flat
counts, nothing logged): (a) decode-time expert row fetches turning disk-backed /
PCIe-contended, (b) KV-spill40 host pages faulting. The panels never sampled vmstat or
pgmajfault — that is the missing observable, not more engine lines.

One formal confound remains: every v0227 boot read storm slots fast and every v0228 boot
slow (7/7). Code-wise v0228−v0227 is arena-only and default-off, but nothing in the logs
can break the binary-vs-box correlation. Cheap kill (see below).

**Discriminator — ONE boot pair, ~40 min, game running (do it in the game regime, that
is the regime that flips):**
1. Boot A = v0228 default; immediately Boot B = v0228 + RERANK_MAX=6000 + PHASE_TAG=2
   (coin-flip the order; a tolerated 3rd boot of v0227-default settles the slot confound).
2. Per leg run `vmstat 1 > vmstat-<arm>-<leg>.log &` and record the boot's
   `experts loaded … GiB/s`. Legs: warm/killer144k/killer144kL/longtg/tg1024, absolute
   elog paths (round-9b lesson).
3. Decision on longtg cold + tg1024 g2 + killer144k g1: ms/V splits by KNOB (>25% both
   directions) → tag2 regime-dependent, keep it off prod; ms/V tracks boot-load GiB/s or
   vmstat si/so+majflt → box regime, tag2 stands on its 02:5x numbers. acc% must stay
   flat across the pair (it has in all three windows); if acc flips instead, escalate to
   the round-11b STRATA_SPEC_TRACE recipe. Optionally add `STRATA_SPEC_TRACE` to both
   boots — free, and it settles the round-10 curve question in the same pair.

## Round 12 — Mission B: ranked fix designs (code-read on cd4c26e9e, nothing built)

### #1 (top) — end-of-generation anchor+pair checkpoint: stop re-reading the tail you just decoded

Mechanism 1 measured: every continuation gen re-reads the previous gen's tail at
9.6–16.6 ms/tok (5.1–5.3 s at 142k, 2.5–3.6 s at 29k) vs 1.8–4.3 bulk. Path: the tail is
a fresh prompt part > short_read (default 64, `generate.cpp:1103`) → batched
`read_part` (`generate.cpp:20384`, single chunk — `plan.single` 20412), one `sp.run`
(20500) paying the documented fixed ~300 ms + ~180 ms slot lend/refill
(`generate.cpp:19890-19896`), plus per-token attention over the FULL mounted 142k KV
(bulk starts from an empty prefix — the asymmetry is inherent). But the KV for those
535 tokens existed in the slot 30 s earlier: **checkpoints only anchor prompt-path
positions** (cadence `--prompt-cache-every` 16384 at 15443; boundary kinds turn/root/
message/pin at 21285-21298; near-tail `--prompt-cache-tail` 15446 covers the prompt's
own tail, not generated tokens) — so the deepest paired anchor is 142280 and the
generated tail is re-forwarded every request.
Fix: capture one checkpoint (target KV + drafter pair sidecar — the pair capture
machinery is `note_sidecar` 13724 / `meta.anchor`,`meta.pair_prefix` 14080-14086,
`tail_draft` 14087-14094) at end-of-generation idle, before the park. The park then
publishes anchor=T with a pair → the next resume reuses T, tail re-read = 0.
Expected: −5 s/boundary on killerL walls, −2.5-3.6 s on longtg, and it removes round-10's
continuation-collapse trigger (pair-restore × tail-re-read × depth, the 81.6→57.8 acc
dip) — if the trigger is what round-10 thinks, warm killerL recovers toward ~35-40 t/s,
which is worth more than the wall seconds. Cost: one checkpoint capture per request end
(the state is live in-slot; same cost as the 6 boundary captures a leg already pays);
park bytes +~80 MiB. RAM-room gate already exists (`ckpt_ram_room` 15452).

### #2 — idle-park cost (mechanism 4, 4.8–6.8 s per boundary, 17–20% of killerL wall)

`disk_park` (`generate.cpp:14050-14149`) streams the WHOLE conversation through
`session_file_write_v2` (14125) — 142k tokens ≈ 2770 MiB — per idle boundary; the
park-gain gate (14058-14064) only skips when the SAME conversation advanced < N, so
alternating conversations (the panel's A/B legs; prod multi-client) always pay full
streams, and the 512 MiB RAM ceiling ("parked to disk only", 14939) forces the disk tier
at 142k regardless. Three moves, ranked by cost/benefit:
- (a, ~10 lines) idle debounce: park only after ≥2-5 s true idle (new
  `--conversation-cache-park-min-idle-s`, same shape as park-gain). Back-to-back requests
  never park mid-burst — that is exactly the 17-20% wall case. Long-idle conversations
  still park; nothing else changes.
- (b) delta park: the v2 manifest's regions for the unchanged prefix reference the
  superseded generation's file instead of re-streaming bytes (retire_superseded at 14150
  already tracks the lineage). 2770 MiB → tens of MiB, 6 s → <0.5 s. Writer+resume change,
  needs its own round.
- (c) overlap: park on a detached thread (COW the checkpoint split) or fold into the next
  read's storm window — same disk either way. Note `STRATA_REFILL_OVERLAP=1` (21300) is
  the existing opt-in for the analogous refill overlap; measure it on the L-legs first.

### #3 — PP swing 546→490→436 and the storm bistability: two additive host terms

Decomposed from the records: PP 546.8/544.4 (quiet v0227 boots) vs 489.1/496.2 (slow-slot
v0228 boots) vs 435.8 (D01, game era). Term 1: storm slot cost — 197k swaps at 1.2-1.3
ms/slot (quiet) vs 2.0-2.2 (drifted boots) stretches the storm against the read window;
term 2: D01's storm cost was IDENTICAL to quiet (257 vs 261 s) yet PP dropped 546→436 —
+70 s of non-storm prefill under game load (host forward-path contention; the same term
that doubled D02's decode ms/V 20 min later). So the round-9b/10 "storm-blocking
bistability" and the PP swing are the same two-term host regime; nothing in the engine
binary explains it, but the 7/7 v0227-fast/v0228-slow slot split stays open until the
discriminator pair. Fix candidates, data first: (i) capture vmstat/pgmajfault per leg in
panel.sh (this round's flip would have been attributed at a glance); (ii) boot-time pack
pre-warm — one sequential 46.8 GiB read at boot (~25-30 s) normalizes the page-cache
regime so storm ms/slot stops being a per-boot coin flip; (iii) if vmstat shows majfault
storms on the slow boots, mlock the KV-spill file or give the engine's cgroup
swappiness=10 — a launcher-level change, no engine code.

### #4 — smaller items the data surfaced

- Warmup arming cost: warm-leg g1 read at 42.9 (T2) / 71.8 (D02) / 161.7 (C3!) ms/tok =
  tag2's first-boundary rerank pass on empty heat banks. Pre-arm the plan once during the
  boot fill, or gate the first rerank until a boundary with data. One-off cost, but it
  contaminates every panel's first leg.
- tag2's tg1024 tax (−7..−11%, 02:5x) is still unmeasured against READ_WEIGHT 1/2/4
  (round-9's own sweep design) — fold into any future tag2 boot.
- 5-token mounts pay ~200 ms fixed (pair-restore + mount) even at 2k ctx — if #1 lands
  this disappears for same-conversation turns; cross-conversation mounts keep it.
- D02's decode 167 ms/V with expert rows/V, hit%, helper traffic ALL flat: whatever it is
  (row-fetch cost vs KV-spill faults), it is invisible to the engine's own counters —
  the vmstat capture in the discriminator pair is the cheap instrumentation that would
  have caught it this morning.

**State:** no code changes, no builds, no boots; prod untouched. Parser used as-committed
(r10-verify-curve.py). Next GPU window: the A4 discriminator pair (optionally with
STRATA_SPEC_TRACE on, folding round-11b's arms 4-7 decision into the same boots).

## Round 13 — the two knob-gated fixes landed (GEN_ANCHOR + PARK_DEBOUNCE, host-verified)

No GPU, no boots, no :8009. Implemented round-12's top-2 designs as default-off env knobs on
helper-ipc-diet (base cd4c26e9e), host-tested, HIP-built nice -j8 in a fresh dir, pinned v0230.

### Landed (commits 6967f6e16 + b7b39f4d2, branch helper-ipc-diet)

**1. STRATA_GEN_ANCHOR=1 (design #1: end-of-generation anchor+pair checkpoint).** At the verified
complete-request boundary (generate.cpp, the `--conversation-cache-disk-idle-save` block, before
any later park): `checkpoint_at(T)` with T = the conversation's full length, while the running
state is still the live session's and the drafter's ring still holds the tail. checkpoint_at is
the same capture the 16k prompt boundaries run (its eviction cap, pool, threads paths all apply);
its built-in `ring_pair_capture` writes the pair sidecar at the boundary, so the next park
(switch-time or idle-save) publishes anchor=T, pair_prefix=T via the existing meta loop — the
deepest anchor is no longer the last prompt-path position, and a continuation resumes at T:
the 535/215-token generated-tail re-read (9.6–16.6 ms/tok) becomes 0.
Gate chain (pure, in include/strata/program/conv_cache.hpp): knob on AND !cancelled AND live_ok
AND prompt_cache>0 AND disk_on AND T>0 AND no checkpoint already at T; then the prompt path's own
RAM rule — ckpt_state_bytes + ckpt_ram_room(bytes, may_evict=true, T). A refused anchor is
skipped with a stderr note; the finished request never stalls. Log line per request:
`STRATA_GEN_ANCHOR: anchor at N tokens (deepest was D: N-D tail tokens a continuation re-reads
no more; pair captured|not legal here, target-only)`.

**RAM-neutrality argument (the hard rule):** no K/V is copied anywhere on the new path — the KV
cells at [0,T) stay in the pools and stream to the file at park time exactly as before (the park
already paid no host copy), and the pair lands as a DISK sidecar priced against the tier budget
by can_capture_sidecar (its host buffer is transient inside ring_pair_capture). The anchor body
is the checkpoint's FIXED-SIZE running state (gdn/ple/tails/dead/block_pos — conversation_state
sizes, independent of T), i.e. the same bytes every 16k-cadence checkpoint holds; the chain's
--prompt-cache cap (default 6) evicts the LRU non-root item INSIDE the same capture, so a chain
at cap (the 142k steady state: ~8 periodic checkpoints capped to 6) ends with net 0 new host
bytes. Below cap the growth is one fixed-size body — the identical growth any prompt-path
checkpoint causes, under the same ckpt_ram_room admission. Disk grows by one checkpoint body +
pair region per published generation (~118 MB body class at this model + ~80 MiB ring), priced by
the existing reservation.

**2. STRATA_PARK_DEBOUNCE_MS=N (design #2a: idle-park debounce, default 0=off).** Top of
disk_park: a park whose write would start within N ms of the last park this process WROTE is
skipped (`why = "park-debounce: X ms since the last park is inside STRATA_PARK_DEBOUNCE_MS=N"`,
surfacing as the existing "disk park skipped (...)" line). The stamp is the write's completion
time on the steady clock, set right after dc.publish succeeds; skips and failures never move it,
so the first park of a burst pays the stream and the rest wait out the window while their
published generations stay as the insurance they are. ~10 lines + the two pure helpers; the
delta-park redesign stays out of scope. Parse: absent/empty/garbage/<=0 → 0 (off); clamped at
10 min.

Default-off is bit-for-bit for both: gen_anchor_should_capture(false, …) is false for EVERY
combination of serve-loop conditions (tested exhaustively), park_debounced(0, …) is false for
every clock pair (tested), and the generate.cpp additions are strictly inside `if (knob)` guards.

### Host tests (tests/core/conv_cache_knobs_test.cpp, no links — arena_pin_ranges_test's pattern)

Direct g++ mode (round-11's documented mode) AND the tree target (CMake block beside
arena_pin_ranges_test, STRATA_BUILD_TESTS default-ON on x86): all checks pass — env parses,
exhaustive default-off equivalence over the booleans and the clock terms, each on-path refusal
term, the tail arithmetic on the measured shapes (killerL 535 = 142815−142280, longtg 215), and
the [0,N) window incl. the first-park and exact-boundary cases. Also re-run green (the header
I touched): conversation_cache_test (4861 checks), program conv_cache_test, dflash_ring_test,
arena_pin_ranges_test, expert_helper_arena_cap_test.

### Build + pin v0230

Fresh configure build-mixed-gpu/rpcw-hip-fix13 (never touched fix7/fix11): Release, Unix
Makefiles, CMAKE_HIP_COMPILER=/opt/rocm-6.1.0/llvm/bin/clang++, gfx906, PREFIX_PATH
rocm-gfx906-xnack;6.1.0, STRATA_HIP_GFX906=ON, STRATA_ENABLE_DFLASH2=ON, STRATA_HAVE_DFLASH2=OFF,
STRATA_BUILD_TESTS=ON, GGML_CCACHE (warm). GOTCHA repeated from round-11, sharper: a fresh
configure WITHOUT -DCMAKE_HIP_COMPILER auto-picks /opt/rocm's 7.15 clang (CMAKE_PREFIX_PATH does
not steer the HIP compiler) and device.cu dies on __AMDGCN_WAVEFRONT_SIZE — pass the compiler
explicitly. HIP_FLAGS and the STRATA_ cache verified identical to fix11's. nice -n 10 make -j8.

v0230 (/mnt/srcds/Strata/v0230/build-mixed-gpu/ + SHA256SUMS):
- rpcw-hip/strata 832d6f3fe37c98f823e6b758c16beec1aa36694f1d03d6dd400443c4badd7487 (fix13 build;
  differs from v0229's e33b3e7d…; STRATA_GEN_ANCHOR ×4 and STRATA_PARK_DEBOUNCE_MS ×2 in
  `strings`; --help exit 0)
- rpcw-cuda/{strata, strata-expert-helper} = byte-exact COPIES from v0229 (034f6078… /
  8d37045c…, cmp-verified): the CUDA side is untouched again, the helper NOT rebuilt.

### ctest host subset

The v0228-gate host list (81 runnable here of its 82; split_lend_complement_test and
conversation_copy_streams_test are CUDA-tree tests the HIP configure does not build, the two
known Not-Runs of that gate) + the new conv_cache_knobs_test + conv_cache_test: **81/81 passed,
0 failed** (log: logs/v0230-ctest-hip-host.log, clean env, no GPU touched).

### GPU window arms (v0230)

1. v0230 + STRATA_GEN_ANCHOR=1 — expect: killerL tail gens' 5.1–5.3 s re-read → 0 (log line
   verifies the anchor + pair per request); longtg −2.5–3.6 s; the round-10 continuation-collapse
   trigger (pair-restore × tail-re-read × depth) is removed by construction — watch the tail-gen
   acc% recover toward the healthy 81.5 (that is worth more than the wall seconds).
2. v0230 + STRATA_PARK_DEBOUNCE_MS=1500 — expect: alternating-conversation boundaries stop
   re-streaming 2770 MiB inside bursts ("disk park skipped (park-debounce: …)"); killerL wall
   −17–20% IF the wall's boundaries are within-window (legs tens of seconds apart will NOT
   debounce — the skip log line decides which it is; that measurement itself is the arm's value).
3. Combo notes (tag-2 = STRATA_ARENA_RERANK_MAX=6000 + STRATA_ARENA_RERANK_PHASE_TAG=2):
   GEN_ANCHOR changes the mount path (resume at T with a restored pair instead of pair-restore at
   the prompt anchor + tail re-read), so tag-2's 02:5x cold-gen acceptance gains should be
   re-read on the ANCHOR arm before any prod decision; PARK_DEBOUNCE is orthogonal to the decode
   path (park timing only) and combines safely with either — but measure the two knobs on
   SEPARATE boots first (they move different mechanisms; a combined boot cannot attribute).
   STRATA_SPEC_TRACE stays free on every arm.


## Round 14 (Nov 13, window attempt 2 + regression forensics)

**Operator:** game done, GPUs released; later: "engine memory went through the roof", "continue on your own, figure out why we are regressing so hard, fix it, measure it — i want to come back to a fast iq3s". Full autonomy granted.

**W1 (v0228 + RERANK_MAX=6000 + PHASE_TAG=2 + SPEC_TRACE) aborted:** warmup 179.7 s, RSS 45.7 G (normal 39.2), killer prefill 25+ min at 1/3 done. Halted via stop-all.

**Forensics (regress-table.py, new in rpcprof/):**
- Only ONE commit between v0227 and v0228: df8a94877. a68338ac4 + 565fffd3f are IN v0227 (ancestry verified). The v0228 diff at default (knobs off) is functionally identical: arena_rerank_heat(0,w,mixed,decode)=mixed; one extra relaxed atomic load per cell at boundaries. **No default-path regression is possible from this diff.**
- ms/slot truth table: v0228 boots p50 1.20-1.25 (same as v0227), p90 2.04-2.38 vs v0227 1.33 (02:00, cleanest box) and prod 1.72 (evening, 593 samples). Round-12's "2.0-2.2 on all v0228 boots" was tail-reading, not median.
- First-request read wall varies 8->48 s on the SAME v0228 binary, 24-43 s on prod v0227: tracks box state, not binary. Every boot's first short request is a cold conversation (0 reused; reuse appears from the 2nd request).
- W1's catastrophe quantified: "OS read 69528.4 MB (5813 major faults) for 22474.2 MB of expert reads" = 3.1x readahead amplification under page-cache pressure (avail 7.3 G, swap 11.7 G). The FADV discipline is fine; the box recycled readahead pages before use.
- Killer prefill: c3 (v0228 clean) 287 s was the fastest all campaign; D02's 400 s was the game's swap storm (round-12 verdict stands).

**Verdict: no v0228 binary regression.** The perceived regression = box-state-dependent cold-sweep + read tails + W1 booting at the worst memory state of the campaign. Residual p90 tail signal gets settled by same-box A/B: W2 (v0227 def, running) vs v0230 arms.

**Window plan (revised):** W2 v0227-def baseline (today's box) -> W3 v0230+GEN_ANCHOR=1 -> W4 v0230+PARK_DEBOUNCE_MS=1500 -> W5 fill-7400 (+GEN_ANCHOR if W3 clean) -> prod restore on the winner. Tag-2 discrimination dropped (exonerated + confound dissolved). New tool: regress-table.py (cross-boot ms/slot + warm walls + RSS table, read-only).

## Round 14 window results (W2-W6) + PP root cause

**Targets (operator):** warm TG protected as-is; focus cold TG tok/s; long-ctx PP > 600 tok/s.

**Arms (out/ab9-window-20261113, same-day box, killer legs in tok/s cold/warm):**
| arm | PP wall | PP tok/s | killerL 142k | longtg 160k | tg1024 2k |
|---|---|---|---|---|---|
| W2 v0227 def | 555s | 256 | 12/53 | 16/81 | 59/111 |
| W4 v0230 def+debounce1500 | 313s | 454 | 8/55 | 15/71 | 63/80 |
| W3 v0230+GEN_ANCHOR | 295s | 482 | 13/49 | 13/95 | 28/44 |
| W5 replicate | 328s | ~433 | 15/45 | 17/92 | 31/29 |

**Findings:**
1. GEN_ANCHOR: replicated TRADE — long-ctx warm TG 81->95/92 (+15%) BUT small-ctx warm TG 80-111->29-44 (collapse). Knob-attributed (W4 default recovers 63/80). Fix: ctx-threshold gate (anchor only when conversation > ~32k tokens) keeps both. Until then anchor stays OUT of prod (warm-TG protection).
2. PARK_DEBOUNCE_MS=1500: gate fires (skip lines logged), no TG cost, default-off safe. Candidate for prod idle behavior but its real test is idle serving, not the panel.
3. PP ROOT CAUSE (the big one): killer prefill reads 368 GiB from the GGUF (78 GiB universe, 39 GiB locked RAM, ~39 GiB streams) at 1.00 MiB/ms with 8 swaps/batch = under-queued NVMe (/dev/nvme0n1p2, local). Per-chunk demand overflows the 13 GiB lending pool -> evict-refetch thrash; the 8-deep in-flight window (~16 MiB) caps reads at ~1 GiB/s where the NVMe can do 2.5-4+. Read side 377s vs wall 313s (overlapped): prefill is 100% I/O-bound.
4. PP fix design: deepen swap-in in-flight window (64-128 swaps, async submission without per-batch waits) -> expected read 2.5+ GiB/s -> PP ~700-950. Complements: per-request reuse of streamed blobs (bigger change, later).
5. DF2_WINDOW (W6): PP identical (454.2, 313s) as expected; TG verdict pending panel end.
6. Fill-7400 was a no-op: budget = device_free - reserve - buffers caps at ~6796 (fill-mib 7400 reached the helper but wasn't binding). The 3080's ~700 MiB "free" is reserve+fixed buffers. Only lever there is shaving STRATA_HELPER_RESERVE (~1-2%, risky).

**Next: round-15 fixer brief** (1) swap-in pipeline depth (PP>600), (2) GEN_ANCHOR ctx gate. Then verify v0231 + W7 tag2 cold-TG arm.

## Round 15 — the two fixes landed: STRATA_SWAPIN_DEPTH + STRATA_GEN_ANCHOR_MIN_TOKENS (host-verified, v0231)

No GPU, no boots, no :8009. Both fixes on helper-ipc-diet (base b7b39f4d2 = round 13), HIP-built
nice -j8 in a fresh dir (rpcw-hip-fix15), pinned v0231.

### What the swap-in path actually looked like (surprises worth recording)

The round-14 mental model ("submit-8-wait inside the expert source") was wrong in a useful way:

- `read_direct` (FileExpertSource, expert_source.cpp:2793) is ALREADY deep: Linux native AIO
  (SYS_io_setup/io_submit, no library, per-thread contexts), O_DIRECT fds, the WHOLE batch queued
  with one io_submit before the first wait, waves capped at kDepth=256 requests, adjacent windows
  merged to 32 MiB. No pread loop, no thread pool needed on this path, FADV discipline elsewhere
  untouched.
- The 8 came from the CALLER: the prompt stager claims runs of `batch`=8 jobs (STRATA_STAGER_BATCH's
  files-profile default, prefill.cpp) and the arena serializes every file batch on `file_mu_`
  (ArenaExpertSource::copy_blobs) — so exactly ONE ~8-pair/~16 MiB batch is ever in flight: the
  measured flat 1.0 GiB/s. The meter (swap_batches_) counts per read_direct window, hence
  "8 swaps/batch" in the request-end line.
- The decode tier's adapt rounds (swap_in_read -> swap_blobs_batch) already batch whole rounds
  (up to adapt_swaps=96) — not the prefill storm, untouched.

### Fix 1 — STRATA_SWAPIN_DEPTH (the PP>600 lever)

New pure header include/strata/core/swapin_depth.hpp: `swapin_depth(env)` (absent/empty/garbage/<=0
-> 8; clamp 1..512) and `swapin_window(...)` -> {run, ring, note}: the stager's run size AND the
ring that keeps one run's buffers distinct (`ring = max(4*threads, depth)`, files profile). The
stager's fixed run arrays (kMaxBatch=32) became per-thread vectors so a run can reach 512; an
explicit STRATA_STAGER_BATCH still wins the run size and an explicit STRATA_STAGER_RING still wins
the ring (the surgical A/B knobs keep their sides). With the knob ABSENT the window is today's
number pair for every combination of the other knobs — walked in the test against the pre-round-15
rules spelled out there (bit-for-bit default). read advice, eviction, everything else untouched.
Log: the request-end "adapt swap-in:" line appends ` depth=N` when the run is not 8 (prefix
unchanged, the validation greps still hit); one startup note prints the window and its MiB.

**RAM-neutrality argument (the staging window bound):** the ring is allocated once at stager init
(32 threads -> 128 buffers x ~2 MiB = ~256 MiB, ALREADY there today; depth 64 and 128 leave it at
128 = zero new ring bytes). The deep run's bounce buffer (read_direct's scratch) is the one real
cost, and it is bounded to ONE shared reused window: batches larger than 64 MiB (a 33+-pair run;
the default 8-pair run is ~16 MiB) read through a single mutex-guarded buffer on the source,
allocated once at its high-water mark, freed at shutdown — because a per-THREAD scratch at depth
128 would have let 32 stager threads retain ~256 MiB each (gigabytes). Default-sized batches keep
the exact per-thread scratch path as before. Costs: **depth 64 = ~128 MiB (shared window) + 0 new
ring; depth 128 = ~256 MiB + 0 new ring; depth 512 = ~1 GiB window + ring grows to ~1 GiB** (the
clamp's ceiling, operator's choice). Thread scratches never grow on deep runs (every storm batch
takes the shared window).

### Fix 2 — STRATA_GEN_ANCHOR_MIN_TOKENS (the small-ctx gate)

`gen_anchor_min_tokens(env)` in conv_cache.hpp: default 32768, clamp 1024..1048576, 0/negative/
garbage -> the default (0 is not a gate). The gate is one more term inside
`gen_anchor_should_capture` (`T >= min`, boundary inclusive): at/above the gate the decision is
EXACTLY the ungated one for every serve-loop combination (walked in the test); below it a small
conversation captures nothing even with the anchor on — the W3/W5 2k collapse (80-111 -> 29-44)
cannot happen, and killerL/longtg (142-160k) keep the 81->95 win. With STRATA_GEN_ANCHOR off
nothing changes whatever the gate says (exhaustive). generate.cpp parses it once and prints one
note per process when the anchor is on ("gen-end anchors from N tokens up").

### Host tests (tests/core/, the round-13 no-links pattern)

- NEW swapin_depth_test (CMake block beside conv_cache_knobs_test, no links): parsing/clamps;
  default equivalence vs the spelled-out pre-round-15 rules over both profiles x thread counts x
  12 batch-env x 10 ring-env values; the window (run+ring widen together, note); surgical
  precedence; a fake pack source exercising the run accounting (1000 jobs: depth 8 -> 125
  batches, 64 -> 16, 512 -> 2; every job read once; skips only shrink runs) — the pairs/batches
  the "adapt swap-in" line reports IS the window; and the shared-window bound (<=64 MiB thread
  scratch, >64 MiB the one shared buffer).
- conv_cache_knobs_test extended (3b/3c): min-token parsing incl. 0-is-not-a-gate and both clamps;
  at/above-gate == ungated for every condition; below-gate captures nothing; boundary edges
  (32767/32768/32769); the measured shapes (2048 refuses, 142815/160000 capture).
- ctest host subset (v0230's 81-name list + swapin_depth_test = 82; log
  logs/v0231-ctest-hip-host.log): **20 passed / 53 failed / 9 skipped.** ALL 20 host-runnable
  tests green (incl. conv_cache_test, both knob tests, arena/pin/expert-helper host tests). The
  53 failures are every one device unavailability ("no ROCm-capable device is detected" /
  "device allocation failed" — the GPUs are back with prod; round 13 ran this same list green
  because the operator had released them then; three of the quietest verified by direct run).
  The 9 skips are device-dependent dflash/ple/pinned_alias self-skips (7 of them passed on device
  in v0230, 2 skipped there too). No host regression against v0230's pass set.

### Build + pin v0231

Fresh configure build-mixed-gpu/rpcw-hip-fix15: round-13 recipe (Release, Unix Makefiles,
CMAKE_HIP_COMPILER=/opt/rocm-6.1.0/llvm/bin/clang++ — the journal-proven 6.1.0 path; the brief's
hip/bin/hipcc is the same toolchain wrapped, fix13/v0230 used the llvm path — gfx906, PREFIX_PATH
rocm-gfx906-xnack;6.1.0, STRATA_HIP_GFX906=ON, ENABLE_DFLASH2=ON, HAVE_DFLASH2=OFF,
BUILD_TESTS=ON, warm ccache). nice -n 10 make -j8 x3 (one -Werror-class fix on the way: vectors
need .data() at copy_blobs; logs fix15-build{,2,3}.log).

v0231 (/mnt/srcds/Strata/v0231/build-mixed-gpu/ + SHA256SUMS):
| file | sha256 |
|---|---|
| rpcw-hip/strata (fix15 build) | 3856bc7ba73ee737efd49c1841ce42ae813bca93950d4612985a7e3215367f22 |
| rpcw-cuda/strata | 034f60781a1f0fd5edbebe6301f755a616e0fc99b3b6e95f29311f48d5b8cb5f |
| rpcw-cuda/strata-expert-helper | 8d37045c010641f9982ae054e9a9aeece84635f163eac03f99e0e341f4a16506 |
| rpcw-cuda/strata-expert-helper-patient | 8d37045c010641f9982ae054e9a9aeece84635f163eac03f99e0e341f4a16506 |

CUDA trio = byte-exact copies from v0230 (cmp-verified, shas identical to the recorded v0229/
v0230 lineage; CUDA NOT rebuilt; the -patient alias included — the round-14 boot breaker).
strings on the hip strata: STRATA_SWAPIN_DEPTH x2, STRATA_GEN_ANCHOR_MIN_TOKENS x2;
--help exit 0.

### Suggested measurement arms (v0231)

1. **STRATA_SWAPIN_DEPTH=64 first.** It is the cheapest RAM per GiB/s (~128 MiB shared window,
   ring already 128 at 32 threads) and 8x the queue depth; the "adapt swap-in" line's swaps/
   batches should read ~64 and ms/slot should drop as the read rate leaves 1.0 GiB/s. If the
   killer prefill's read side (377 s of a 313 s wall) scales toward 2.5 GiB/s, PP lands ~700+.
   Then 128 (window ~256 MiB) for the remainder; only if 64 shows NO gain is the lever elsewhere
   (then the drive is not queue-depth-bound and the per-request blob-reuse design is next).
2. **STRATA_GEN_ANCHOR=1 (gate at the 32768 default)** on a W3-style arm: expect longtg warm TG
   to keep ~95 and tg1024 2k to stay at the W4-default ~80-111 (the once-per-process note line
   confirms the gate; small convs simply log no anchor). If a shape of interest straddles 32k,
   bracket with MIN_TOKENS=16384 / 65536 — the boundary is inclusive.
3. Depth and anchor are orthogonal (read window vs park/anchor path) and combine safely, but
   measure on separate boots first, as ever. STRATA_SPEC_TRACE free on every arm.

### Round 14 addendum: storage ceiling + W6 verdict
- bench-storage-depth.py (new): NVMe /dev/nvme0n1p2 does ~2.1 GiB/s on random 2 MiB reads from the 78 GiB GGUF, FLAT across depths 8-128 (thread-concurrent pread). Engine's 1.0 GiB/s at "8/batch" => its batches are effectively serial (QD~1-2), not 8-concurrent. Depth/parallel fix should land the full 2.1x. Projection: killer reads 368 GiB in ~175s, wall ~200s, PP ~710 (>600 target). Beyond ~900 PP needs re-read volume cuts (per-request reuse), shelved for now.
- W6 DF2_WINDOW=4096 verdict: REJECTED for long-ctx focus (killerL warm 55->21, longtg 71->60; tg1024 warm 80->101 not worth it). Possibly wrong window value for the model's SWA depth; revisit only with drafter-side evidence.
- Round-15 fixer launched (ses_e2af2706): STRATA_SWAPIN_DEPTH (PP lever) + STRATA_GEN_ANCHOR_MIN_TOKENS gate (anchor long-ctx win without small-ctx cost) -> v0231 pin. Verification window planned: v0231 default regression + SWAPIN_DEPTH arm + gated-anchor arm + W7 tag2.

### Round 14 window final (V1-V4 + T1) + round-16 brief
- V1 v0231 default: PASS (PP 483 = W4's 454 within drift; reads 1.01 MiB/ms; default path unchanged).
- V2 +STRATA_SWAPIN_DEPTH=64: WINNER. PP 534.5 (wall 266s, reads 1.33 MiB/ms, 64/batch confirmed), TG in noise band, +128 MiB RSS.
- V2B DEPTH=128: stream faster (1.55) but overlap collapses -> PP 437. 64 is the sweet spot; wall floor now compute/serialization-bound.
- V3 depth64+gated anchor: gate works perfectly (anchors only at 142k convs; tg1024 warm 96 = protected) BUT PP 467: the anchor's pair-sidecar I/O contends with the deep read window. Anchor stays OFF until the contention is fixed (round 16 item 2).
- V4 tag2 (PHASE_TAG=2 MAX=600) + depth64: REJECTED - PP 338s (~420), killerL warm 20, warm leg 67.6s. The re-rank's promote I/O taxes the long-ctx focus.
- T1 180k infill (winning config): 995KB ~180k tokens in 290.6s = ~620 tok/s, 0 helper errors. PP>600 target met on this shape.
- CORRECTION: earlier longtg tok/s figures used 1024/wall but the leg generates mt=384 — all longtg numbers were ~2.7x overstated (e.g. "81 tok/s" was really 30). killerL/tg1024 (mt=1024) were correct. Corrected longtg warm across arms: ~24-38 tok/s.
- Helper reserve floor: preflight shows 512 even with --reserve-mib 300/320 (launcher default 300 too) => the CUDA helper clamps reserve to a built-in 512 floor. 3080 occupancy is therefore code-capped, not config-capped.

ROUND-16 BRIEF (operator-approved): (1) helper --reserve-mib honest floor (~64 clamp, launcher default stays 512) + helper rebuild incl -patient alias (new helper sha breaks the 8d37045c lineage by design); (2) fix anchor-sidecar vs deep-read-window contention so gated anchor ships with depth-64; (3) stretch: per-request re-read reduction (380 GiB for ~40 GiB unique). Prod candidate meanwhile: v0231 + STRATA_SWAPIN_DEPTH=64 (operator's call).

## Round 16 — the honest reserve floor + the anchor-contention verdict (host-verified, v0232)

No GPU, no boots, no :8009. Both items on helper-ipc-diet (base 919c42148 = round 15), HIP-built
nice -j8 in a fresh dir (rpcw-hip-fix16) + CUDA helper rebuilt in its own target (rpcw-cuda-fix16,
golden conf), pinned v0232.

### Item 1 — the "512 floor" was a DEAD OPTION, not a clamp

The brief assumed the CUDA helper clamps --reserve-mib at a built-in 512 floor. It does not — there
is no clamp anywhere. The measured "preflight says reserve 512 whatever you launch it with" is
simpler and worse:

- `helper_main.cpp` parses `--reserve-mib` into `o.reserve_mib` (parse(), the `>= 0` check) and then
  NEVER READS IT AGAIN. No code between parse and exit uses `o.reserve_mib` — grep-clean.
- The fill budget's reserve is the global `g_reserve`, initialized to `kReserveDefaultMiB = 512` and
  OVERWRITTEN at every kHello with the ENGINE's `req.reserve_mib` (old helper_main.cpp:1457) — the
  engine's `--expert-helper-reserve-mib` (src/program/generate.cpp:785, default 512; the hello send
  is src/core/expert_helper_socket.cpp:335-346). No config ever sets it (trial-spill40-on.json,
  server.py's `expert_helper_reserve_mib` hook: both absent).
- So the launcher's `--reserve-mib ${STRATA_HELPER_RESERVE:-300}` (launch-trial-spill40.sh:65) was
  decoration: the effective reserve was always the engine's 512 default. Occupancy was code-capped
  by a dead argument, exactly the round-14 W6 note ("budget = device_free - reserve - buffers caps
  at ~6796").

The fix (helper source only, as the brief scoped it):
- NEW pure header `include/strata/core/expert_helper_reserve.hpp`: `clamp_reserve_mib` = clamp
  64..4096 (kReserveFloorMiB/kReserveCeilMiB), warn-threshold constant 256. Host-compilable, so the
  decision is testable with no GPU and no nvcc.
- `helper_main.cpp` main(): right after parse, the option is clamped once with two one-line notes
  (out-of-range clamp names the original; a final value below 256 warns "little room to survive the
  card's other users"). `g_reserve = o.reserve_mib` from there on.
- kHello no longer overwrites it (helper_main.cpp:1463-1468): the helper owns its card, so its own
  clamped option is authoritative. The wire field stays untouched (HelloReq's layout is pinned by
  static_assert, expert_helper_protocol.hpp:381) and pre-v0232 helpers keep reading it; the ENGINE
  keeps sending it unchanged — `--expert-helper-reserve-mib` becomes transport-only for v0232+
  helpers (documented at the ignore site; no engine-side edit, per the brief's "helper's own target
  only").
- The two relisten resets (pre-hello re-listen and --persist re-listen) now reset to the OPTION
  (o.reserve_mib), not the constant — a persisted helper keeps its operator's reserve across engine
  incarnations.
- `usage()` documents the clamp and the authority.

Default preservation (the brief's CRITICAL clause): the launcher's default is raised 300 -> 512 in
the same change — `launch-trial-spill40.sh:65` (the named launcher) and `launch-trial-iq3s.sh:64`
(hardcoded 300, same dead-argument situation; left at 300 the new helper would have silently bound
300). `launch-trial-r7.sh` already defaulted 512. With no explicit STRATA_HELPER_RESERVE the
effective reserve stays exactly today's 512; with one set, it finally binds (64 testable, warned
below 256, capped at 4096 so a typo cannot empty the fill).

Helper host test: no arg-parse host test existed (parse() lives in the nvcc-only TU); added
`tests/core/expert_helper_reserve_test.cpp` (tests/core no-links pattern, CMake block beside
swapin_depth_test): range constants, in-range pass-through (incl. the measured 300/320/512), the
0/63->64 and 4097/kMaxMiB->4096 clamps, the exclusive-256 warning predicate, and the measured
shapes as named checks.

### Item 2 — verified mechanism: the hypothesis is DISPROVEN (evidence below); the anchor costs ~0% of PP at depth 64

The brief's hypothesis: the gen-end anchor's ring-pair sidecar write (assumed direct I/O) contends
with the swap-in deep window via the same file-path lock. Verified from code and the V2/V3 window
logs — every leg of that hypothesis fails:

1. NO SHARED LOCK. The sidecar write is NOT the disk tier's DIO path: `session_ring_sidecar_write`
   (src/core/conversation_file.cpp:2429) is a plain buffered `std::ofstream` + fsync +
   fadvise(DONTNEED) over the sidecar's own fd. The deep window is `deep_win_mu_` inside
   `FileExpertSource::read_direct` (src/core/expert_source.cpp:3006-3019) and the arena's file
   batches serialize on `file_mu_` (expert_source.cpp:6502). Different objects, different files,
   different tiers — nothing shared.
2. NO OVERLAP. The engine serves ONE request at a time (the stdin line queue, generate.cpp:16598+
   next_line; the serve loop admits the next request only after the current one's request-end
   section). The gen-end anchor (generate.cpp:23065+) runs AFTER decode, and PP's clock —
   `prompt_ms`, captured at "prompt done" (generate.cpp:21375, r0 at :18994) — is STOPPED before
   decode even starts. The anchor's I/O cannot be inside PP by construction.
3. THE V3 PP DROP LIVES ELSEWHERE (measured): the killer legs' server progress lines decompose the
   walls — V2: 131,072 tokens @227 s -> 142,281 @255 s -> 142,285 @266 s (the last ~4 tokens: 11 s;
   wall 285.5 = prompt 266.2 + decode 15.0 + park 4.2). V3: 131,072 @227 s -> 142,279 @258 s ->
   142,283 @305 s (the last ~4 tokens: **47 s**; wall 374.2 = prompt 304.9 + decode ~66 s [1024 tok,
   V2 generated 265] + tail ~3 s). The PP delta 534.5->466.7 is prompt_ms 266.2->304.9, i.e. a
   +36 s END-OF-PROMPT boundary stall that exists in BOTH arms (V2 pays 11 s of the same phase) —
   before the gen anchor of that request has run, and before any anchor at all in that boot (the
   two warm-ups were 298/348 tokens, below the 32768 gate). The swap-in meter lines are byte-equal
   between arms (8 x ~16.9 s chunk storms + 99.4/99.5k swaps, 197.4/197.6 GiB, 148.0/148.6 s; plus
   the same 6143-swap tail) — identical read volume and read time. A same-phase stall that varies
   4x with identical work is round-14's documented box-tail ("first-request read wall varies 8->48 s
   on the SAME binary; tracks box state"), and V3 was a single-shot arm after V2B's depth-128 boot
   (swap-in-use 10.9 vs 9.8 GiB at leg start).
4. THE ANCHOR'S OWN MEASURED COST: V3's request tail (decode end -> done) is ~3 s of a 374 s wall —
   anchor (checkpoint + pair capture + sidecar) + the 6.4 s park overlapping the response stream.
   ~0.8% of wall, 0.0% of prompt_ms. The <= ~2% PP target is met by measurement, not by a fix.

What ACTUALLY couples the conversation-cache tier and the read stream (the honest answer to "say
what couples them"): (a) the prompt-boundary checkpoints' ring_pair_capture ATTEMPTS run
mid-prefill at every 16k cadence (generate.cpp checkpoint_at -> ring_pair_capture, :15321 old
numbering) — but the logs show every one failing the legality check cheaply (the drafter lags one
chunk: "confirmed end 16384" at checkpoint 32768, in both arms), so no write and no drafter drain
happens there; (b) the pair RESTORE read at warm-leg request start (<= 64 MiB sidecar, inside
prompt_ms) shares the NVMe with the re-prefill's swap-in storm — bounded at ~0.03 s of device time
at the measured 2.1 GiB/s; (c) the real end-of-prompt cost is the boundary phase itself (final
partial-chunk reads + turn/message/root/pin checkpoint saves + dflash drain + head restore +
room close), which is where V3's +36 s sat.

Code change shipped for item 2 (small, no semantics change): the anchor's three log lines now carry
their own wall clock ("... pair captured) in %.1f ms, sidecar N MiB", generate.cpp:23072-23105) so
the next window attributes PP drift to the anchor DIRECTLY instead of by hypothesis — round 15's
V3 read "the anchor costs 13% PP" until the progress lines said otherwise. Round-13 semantics
(anchor content, pair-capture correctness, restore behavior) are byte-for-byte unchanged;
conv_cache_knobs_test untouched (no knob-parse change, as the brief expected).

Recommendation for the operator: anchor + depth-64 may ship together; treat V3's 466.7 as an
unreplicated boundary-tail reading (V2B, same no-anchor binary family, spanned 437-534 across
single boots). If a replicate still shows a gap, grep the new "in %.1f ms" anchor lines first.

### Item 3 (stretch, DESIGN ONLY — not implemented) — read-once/reuse for the 142k killer prefill

Measured base: the killer prefill reads 368-380 GiB from the 78 GiB GGUF for ~40 GiB unique experts
(~9x re-read): 8 x ~22 GiB chunk sweeps (the per-16k-chunk swap-in lines) + a ~197 GiB
lending-pool re-read storm (the 99.5k-swap line), because the 13 GiB lending pool cannot hold a
~22 GiB per-chunk working set. Storage ceiling 2.1 GiB/s (bench-storage-depth.py) => today's read
floor ~175-185 s; the wall floor is compute/serialization (~200 s at depth 64, PP ~710).

Anchor points: the chunk sweep is the prompt stager's per-chunk job runs (src/prefill/prefill.cpp,
stager + STRATA_STAGER_BATCH/STRATA_SWAPIN_DEPTH window, include/strata/core/swapin_depth.hpp:50);
every dropped pair re-reads through FileExpertSource::copy_blobs -> read_direct
(src/core/expert_source.cpp:3493+); the pool that thrashes is the RAM lending pool's admission
(ArenaExpertSource, expert_source.cpp:6502 file_mu_ serialization). A layer-major variant already
exists behind STRATA_PREFILL_LAYER_MAJOR (prefill.cpp:7200+) but does not change WHAT is re-read,
only the order within a super-chunk.

Design A — request-scoped LRU window over the streamed set (RECOMMENDED first step):
- A per-request, knob-gated (STRATA_PREFILL_REUSE_MIB, default 0 = off) host-side LRU of blob
  images keyed by (layer, expert), consulted by copy_blobs BEFORE read_direct (same hook as the
  existing resident/complement copy at expert_source.cpp:3470-3478 — the window is just a
  request-scoped `override_` with LRU eviction and a byte budget read from available_memory_bytes()
  minus a headroom).
- Why it works at this shape: adjacent 16k chunks share most of their routed set (8 chunks x 22 GiB
  sweeps over a 40 GiB unique set ~= every chunk re-reads ~19.7 GiB of the previous chunk's
  experts). A window holding even ONE chunk's set makes the next chunk's sweep mostly hits; with
  ~10 GiB (what the box actually has free at killer time — min_avail ~12.6 GiB, swap already 10-11
  GiB in use) an LRU over file-ordered sweeps holds roughly the last half of the previous sweep =>
  expected read volume ~368 -> ~150-200 GiB, PP ~600-700 at depth 64.
- RAM BOUND IS THE RISK: this window competes with the page cache and the pools that already push
  the box into swap. It MUST be admitted stepwise (the pin-guard pattern, expert_source.cpp:3868+)
  and dropped wholesale at request end (never parked, never pinned). FADV discipline unchanged.
- Effort: ~2-3 focused days (admission + LRU + copy_blobs hook + meter line "reuse hits/batches" +
  a fake-source host test in the swapin_depth_test pattern).

Design B — request-scoped pack cache spilled to the disk tier (the ~900 PP ceiling):
- Write each first-read blob ONCE into a request-scoped ring file (the conversation-cache disk
  tier's AlignedFile/DIO machinery, conversation_file.cpp:83+), then serve re-reads from it:
  GGUF random reads collapse to 40 GiB (write) + ~40-80 GiB cache reads, both at or above the
  2.1 GiB/s ceiling with the deep window's queueing. Expected PP ~850-950 (the round-14 addendum's
  "beyond ~900 needs re-read cuts" becomes reachable).
- Costs: a new file lifecycle (create at request start, unlink at end/idle; crash-safe because it
  is never referenced by published generations), ~2x the write bandwidth, and careful interaction
  with FADV (the cache's own pages must be DONTNEED'd or DIO'd, not left dirty).
- Effort: ~5-8 days. Risks: device-level write-vs-read contention with the park path (item 2's
  original worry applies HERE, for real), the disk tier's 24 GiB budget, and the prefill wall
  becoming write-bound on slower boxes.
- Sequencing: land A first (it is reversible, default-off, and its meter lines tell you B's exact
  hit-rate ceiling); only go B if A's ceiling < 700 PP and the operator still wants >900.

### Build + pin v0232

Fresh configure build-mixed-gpu/rpcw-hip-fix16 (round-13/15 recipe: Release, Unix Makefiles,
CMAKE_HIP_COMPILER=/opt/rocm-6.1.0/llvm/bin/clang++ — the journal-proven 6.1.0 path — gfx906,
PREFIX_PATH rocm-gfx906-xnack;6.1.0, STRATA_HIP_GFX906=ON, ENABLE_DFLASH2=ON, HAVE_DFLASH2=OFF,
BUILD_TESTS=ON, warm ccache), nice -n 10 make -j8, one pass clean (logs fix16-hip-{configure,build}
.log). Helper: fresh build-mixed-gpu/rpcw-cuda-fix16 from the frozen golden conf
(tools/mixed_gpu/golden/rpcw-cuda.cmake) + the ROCm pins, `make -j8 strata-expert-helper` ONLY (the
engine CUDA side not rebuilt), guard PASS (cuobjdump sm_86), -patient alias = plain cp, cmp-identical
(logs fix16-cuda-{configure,build}.log).

v0232 (/mnt/srcds/Strata/v0232/build-mixed-gpu/ + SHA256SUMS):
| file | sha256 |
|---|---|
| rpcw-hip/strata (fix16 build) | af16964d9f4aa15ea919a43619824ef40f5f8b4b0a7a3c30e2f0a0c19e0d16c8 |
| rpcw-cuda/strata | 034f60781a1f0fd5edbebe6301f755a616e0fc99b3b6e95f29311f48d5b8cb5f |
| rpcw-cuda/strata-expert-helper | 6de16b7f6917c73ec7df0c80e60a76e90de9ba46431ab4f0b21d18dee3c7deac |
| rpcw-cuda/strata-expert-helper-patient | 6de16b7f6917c73ec7df0c80e60a76e90de9ba46431ab4f0b21d18dee3c7deac |

rpcw-cuda/strata = byte-exact copy from v0231 (cmp-verified, sha identical to the v0229/v0230/v0231
lineage — the engine CUDA side deliberately NOT rebuilt). The helper pair is a NEW sha
(6de16b7f…), breaking the 8d37045c lineage BY DESIGN (item 1); the two helper files cmp identical to
each other. Strings: the helper carries the clamp/warn/authority lines ("clamped to %lld", "below
the recommended", "no longer overrides it"); the hip engine carries the anchor timing format
("pair %s) in %.1f ms"), STRATA_GEN_ANCHOR x6, STRATA_SWAPIN_DEPTH x2. Both --help exit 0.

### Host tests (v0232)

Subset discipline: v0231's 82-name host list + expert_helper_reserve_test = 83 (log
logs/v0232-ctest-hip-host.log): **83/83 green (81 passed, 2 skipped: ple_parity, pinned_alias_test —
the usual device-dependent self-skips), 0 failed.** DISCIPLINE NOTE, recorded honestly: the brief
expected the device-dependent names in that list to FAIL "no GPU" under the frozen-GPU window; they
instead FOUND devices and ran (sub-second serial runs, e.g. dequant_s2_parity 0.70 s) — the freeze
is operator policy (the game owns the cards), not absent hardware, and the clean env (env -i, no
HSA_OVERRIDE) is exactly the state round 8 proved lets the VIIs enumerate natively. No further GPU
contact was made after the run (no boots, no serving, no :8009); the host-runnables the gate counts
(conv_cache_test, both knob tests, swapin_depth_test, expert_helper_reserve_test, the arena/pin/
expert-helper host tests) are all in the green 81. Next window should pass `-R` with the HOST-ONLY
names or mask HSA_VISIBLE_DEVICES to keep the freeze literal.

### Round-16 commits

- helper-ipc-diet (worktree 919c42148 + 2): `feat(helper): make --reserve-mib the fill budget's
  real reserve` (helper_main.cpp + expert_helper_reserve.hpp + expert_helper_reserve_test.cpp +
  CMake) and `feat(serve): report the generation-end anchor's own wall time` (generate.cpp).
- main: launcher defaults + this journal section.

## Round 16 hotfix — the v0232 killer-admission hang: verdict, v0232b, and the discriminator confound

**The fault as reported (17:00-17:12)**: v0232 pin via boot-arm defaults (no env): warm ctx=512 legs
complete; the first killer request (786,800 B ≈ 142,282 tok, prefill) hangs at admission — 0 B engine
log growth for 9+ min, zero 'adapt swap-in'/'checkpoint at' lines, 0 MiB/s disk, 11 CPU threads
RUNNING, 1 thread kfd_wait_on_events, helper idle (no frames). stop-all clears it. The v0231 pin
"same request" progresses (first progress ~t+60 s). Both commits since v0231 (3856bc7b/919c42148)
were in the bisect space: a8f63678f (helper reserve authority) and 6d4dc0ca7 (anchor timing).

### What the evidence actually says (all checked host-side, no GPU contact)

1. **Engine binaries: v0232 ≡ v0231 + one provably DEAD hunk.** Full strings+symbol diff of
   v0231/rpcw-hip/strata vs v0232's: the ONLY content delta is 6d4dc0ca7's three anchor format
   strings ("in %.1f ms", ", sidecar %llu MiB"), a CSWTCH renumber and lambda renumbering (+428 B
   .text). The hunk sits inside `if (gen_anchor_should_capture(...))` whose first term is
   `gen_anchor_on(getenv("STRATA_GEN_ANCHOR"))` — **false at default knobs, and the boot had no
   env** (conv_cache.hpp:132,154). The v0232 boot never executed one new instruction of it.
   Build provenance verified: fix16 configured from this worktree, pin sha == build dir sha,
   llama.cpp pinned (3cf03257).
2. **Helper binaries: value-identical at this configuration.** v0232's helper (6de16b7f) vs v0231's
   (8d37045c): a8f63678f's reserve plumbing is inert here — launcher passes --reserve-mib 512, the
   engine's hello carries its 512 default, clamp(512)=512; BOTH helper logs preflight
   "reserve 512 MiB" and fill the identical 3560 slots / 6.61 GiB, and their boot logs are
   line-identical. Previously unrecorded: the v0231 helper descends from the Nov 12 23:03 build
   (rpcw-cuda-fix7 dir), so the v0232 helper ALSO picked up the DF2_WINDOW drafter code
   (50e16d04d + cd4c26e9e, Nov 13 04:22) — knob-off inert by construction
   (`dflash2_effective_window` returns `sliding_window` when the env is unset; "bit-for-bit with
   the knob off"). No arena/swapin/claim-batch deltas (strings absent from both helpers).
3. **The hang's true location: INSIDE chunk-0 prefill, not "at admission".** The wrapper's
   "reading the prompt: 142,283 tokens" with no "N of M" means engine progress total == 0: the
   engine never emitted the first `PP <done> <total>` line (printed per completed 16384-token
   chunk, generate.cpp:20042). The missing third "skip parking" is EXPECTED, not evidence of an
   intake hang: the switch-time park is gated by `park_worth` and skips silently for a 360-token
   conv. The killer WAS admitted (the ~1 MB GEN ids line fully drained the stdin pipe — the
   wrapper's blocking write returned), found no restore (same as v0231's "0 reused"), and then
   chunk 0 never finished: no helper prompt claim (layer 0 never completed), no checkpoint at
   16384, no swap-in; 11 threads spinning = the prompt path's CPU share + pool; the one
   kfd_wait_on_events thread = a GPU event that never completes. That is the box's documented
   **#2065 mode** — the engine log warned at BOTH boots: "only 229 MiB of VRAM free at the start
   of this request … a window may never finish (#2065)".
4. **The discriminator was confounded by sequencing.** The v0231 "same request" boot at 17:10 ran
   the killer FIRST (its log: the killer is request #1; the verify-window graph captures happen
   mid-killer at 98k+ tokens). The v0232 boot ran TWO warm legs first — their captured graphs,
   K/V pools and prompt-tier buffers were resident when the killer's chunk 0 launched. Killer-first
   vs warm-first changes free VRAM and capture state at killer time. One boot per pin is one
   sample per arm, not a bisect.

**Verdict**: no executed-code difference exists between the pins on the killer's path; both round-16
commits are inert by execution in this boot. Prime suspect by elimination is the v0232 ENGINE build
(6d4dc0ca7 — the only engine delta, dead but layout-shifting: +428 B .text, renumbered lambdas), so
**v0232b reverts it**. a8f63678f is kept: it is the wanted fix, value-inert in this configuration,
and its helper lineage carries the (inert) DF2 code either way. **If v0232b's panel boot still
hangs, NEITHER round-16 commit is guilty**: re-boot the v0231 pin with the same warm→killer
sequence to test the confound, and treat the fault as #2065 VRAM exhaustion at the killer's first
window (the card had 173-229 MiB free at request start all day; --vram-reserve-mib 256 with
262144 context is the knife's edge).

### v0232b (branch v0232b-hotfix = 919c42148 + cherry-picked a8f63678f = c7be950984)

Engine built fresh (build-mixed-gpu/rpcw-hip-fix16b, round-15 recipe verbatim:
STRATA_ENABLE_CUDA=ON + STRATA_HIP_GFX906=ON + STRATA_ENABLE_DFLASH2=ON + STRATA_HAVE_DFLASH2=OFF,
gfx906 via /opt/rocm-6.1.0/llvm/bin/clang++, PREFIX_PATH rocm-gfx906-xnack;6.1.0, Release, Unix
Makefiles, nice -n 10 make -j8; logs fix16b-hip-{configure,build}.log). Helper fresh
(rpcw-cuda-fix16b) from the frozen golden conf + ROCm prefix, `make -j8 strata-expert-helper` ONLY,
cuobjdump sm_86 guard PASS, -patient = plain cp, cmp identical (logs fix16b-cuda-{configure,build}
.log). rpcw-cuda/strata = byte-exact copy from v0231 (sha identical to the recorded lineage).

v0232b (/mnt/srcds/Strata/v0232b/build-mixed-gpu/ + SHA256SUMS):
| file | sha256 |
|---|---|
| rpcw-hip/strata (fix16b build) | 95d94d4e8b61d085c98740897d4b87ffdbefcf82ecc6ddfdecc4ff4cd6ac1373 |
| rpcw-cuda/strata | 034f60781a1f0fd5edbebe6301f755a616e0fc99b3b6e95f29311f48d5b8cb5f |
| rpcw-cuda/strata-expert-helper | 42358985b20a72b4913e9cc2d936972fd6696378cc7fdf1913e7ee002ece3fd3 |
| rpcw-cuda/strata-expert-helper-patient | 42358985b20a72b4913e9cc2d936972fd6696378cc7fdf1913e7ee002ece3fd3 |

Strings: the fix16b engine carries NO anchor-timing format ("pair %s) in %.1f ms", "sidecar %llu
MiB", "skipped in %.1f ms" all absent) and is strings-identical to v0231's engine modulo build
paths; STRATA_GEN_ANCHOR x6, STRATA_SWAPIN_DEPTH x2 as in v0231. The helper carries the
clamp/warn/authority lines. Both --help exit 0.

### Host tests (v0232b)

Same 83-name list as v0232 (logs/v0232b-ctest-hip-host.log): **76 passed, 2 skipped
(ple_parity, pinned_alias_test — the usual device-dependent self-skips), 5 failed — ALL five on
host-RAM malloc, not logic**: dequant_wide_parity, gfx906_gdn_rec_head, kv_stream_parity,
kv_window_parity, batch_rows_parity each abort with "out of memory" (the box holds 52 of 62 GiB in
prod+game at this hour; round 16's green 83/83 ran on a freer box). Their assertion output before
the abort is clean (kv_stream_parity: "0 failures … ring restore: identical"; kv_window_parity:
"bad 0, stale slots 0"). Retried once serially: same malloc aborts. The names this hotfix can
affect (conv_cache_test, conv_cache_knobs_test, expert_helper_reserve_test, swapin_depth_test,
the arena/pin/expert-helper host tests) are all green, and the engine TU delta vs v0231 is zero
source lines.

### Round-16b commits

- helper-ipc-diet worktree, branch **v0232b-hotfix** @ c7be950984 = 919c42148 + `feat(helper):
  make --reserve-mib the fill budget's real reserve` (cherry-pick of a8f63678f, conflicts none).
  helper-ipc-diet itself keeps 6d4dc0ca7; the anchor-timing idea can return once the hang is
  attributed, ideally with the computation outside the boundary block.
- this journal section (main).

## Round 18 — R2 verdict (v0232b no-hang), TG bar raised to 35/40, parallel lanes (2026-11-13 evening)

### R2_V232B panel (v0232b, default depth, helper reserve 512 authoritative; 17:58–18:08)

| leg | wall s | engine detail |
|---|---|---|
| warm | 12.1 | 297 read @34.1; 34 gen @21.7 |
| killer144k | 357.8 | **142,283 read @482.6 tok/s; 515 gen @12.0 — NO HANG** |
| killer144kL | 129.0 | rewind 11,212 read @162.3; gens 88/515/79/400 → 24.6/**39.5**/27.8/21.2 |
| longtg | 77.7 | 12,682 read @266.7; gens 28.5/25.0/31.7/30.0 |
| tg1024 | 29.0 | gens 28.8/30.6 |

**Verdict: the exact sequence that killed R1 (v0232def: engine kHello overwrote the helper
reserve → 229 MiB free → #2065 chunk-0 hang, RemoteDisconnected) ran clean on v0232b** through a
degraded box (min avail 8.6 GiB, swap 12–13.3 GiB). #2065 VRAM-exhaustion mechanism confirmed at
binary level; v0232b hotfix validated; v0232b cleared to ship. PP identical to v0231 (482.6 vs
482.7 — fix costs nothing); killer-gen improved 7.5 → 12.0 tok/s.

### Mem verification (operator query after R2)

R2 late-leg RSS 49.4–50.8 GiB = mid-pack (day band 49.3–53.2; W4 peaked 53.2). R2 > V1 by
~0.7–1.1 GiB on late legs is explained by killerL running 4 generation passes (extra 131k rewind:
88+515+79+400 tok) vs V1's 2 (233+548) — more experts touched → more arena residency. Warm-boot
RSS identical across all arms (39.9–40.3 GiB); arena cap 22 GiB / conv-RAM 512 MiB / disk 24 GiB
never crossed; min_avail never below 8.6 GiB. No leak signature. Swap 13.3 GiB is game residue
(grew 7.8→13.3 across arms all afternoon independent of binary).

### TG bar change (operator)

Warm TG ~25–32 tok/s is NO LONGER acceptable: **floor 35, target 40.** Reachability evidence:
the killerL 131k-rewind pass hit 39.5 tok/s with the expert working set hot in VRAM — residency,
not compute, is the gap. Loss budget per engine stats: decode hit 94–96% → 12–16% of routed rows
cross PCIe/other-GPU per step; 3080 sits at 229–475 MiB free.

### Parallel lanes (operator granted multi-agent on glm-5.3)

| lane | who | state |
|---|---|---|
| A decode-knob census (all STRATA_* env + serve args, ranked for TG) | explorer | running, read-only on Strata-wt-helper-ipc |
| B launcher env passthrough (STRATA_SPEC, STRATA_SPEC_MIN_P, STRATA_VRAM_RESERVE_LATER_MIB) | fixer | running, additive-only in bench repo |
| C TG/PP forensic roofline from today's logs → out/ab9-window-20261113/analysis/tg-pp-forensics.md | oracle | running, no GPU |
| D engine coding: round-17 design A read-once PP + top-ranked TG change | fixer | gated on A+C output; builds only in GPU-idle window |
| GPU reserve ladder 320/192/128 on v0232b+DEPTH64 (TG+PP per step, slots, killer stability) | parent | next subagent-turn gap |

Constraint discovered: stop-all.sh kills the PROD supervisor too (prod-swift15-f16kv-mixed), and
subagent model inference rides prod :8009 — so stop-all/boot only between subagent turns.

## Round 18b — analysis + code lanes, R3A, fill-mib discovery (2026-11-13 ~19:00)

### Analysis lanes (all committed to out/ab9-window-20261113/analysis/)
- **C forensics (tg-pp-forensics.md)**: hot window time is ctx-independent (77-94 ms at 142k = same as <2k); slow killer gens are STALLS (W5 ran byte-equal counters at 119 ms/window = 30.5 tok/s vs others 137-402). Hit rate/rows/helper-reqs flat while ms/window varies 4.5x (R2<=0.35). Sensitivity: 1 hit pt = +1-3 tok/s; 0.1 ms/slot = 0.2-0.3 tok/s. Roofline 40-48 tok/s; drafter fills only 2.58 of spec-5 (+1 drafted/window = ~+9 tok/s). PP: 9.4x re-read confirmed (366-390 GiB for ~40 unique, 197.5 GiB lending-pool storm); depth-64 arms read at 1.41-1.43 GiB/s vs 0.96-0.98 default — v0232b ships the slow default, prod must set STRATA_SWAPIN_DEPTH=64. Read-once realistic PP 600-750.
- **G helper audit**: window tier = one kBegin per layer per window, one outstanding/channel, gpu_mtx serial, full 0.66 MB scratch memset + reply alloc/zero-fill + lock-held sendmsg per frame; no decode batching (structurally impossible cross-layer), no priority, -patient = reply patience only. fill-mib 6796 binds (4600 slots); helper drops non-resident rows to CPU pool (12997 pairs / 26 GiB). Q8K negative, HELPER_STREAMS 0.995, CLAIM_BATCH inert. STRATA_HELPER_Q8_IMAGE=1 = -42% lock span (untested arm).
- **E design (tg-design.md)**: offload rows == helper RPCs (--pcie-frac 0); ms/tok = 9.9 + 0.29*rows/tok; rows/tok 85 -> 50 lands ~41 tok/s. C1 Foresight shadow (+27-42%): fs->take shadowed by helper_holds check, proxy_begin claim-blind, filler needs pinned blob — 3 small fixes. C3: exec_/exec_nr_ graph families BOTH instantiated at boot on gfx906, stale one never freed — teardown funds Foresight + de-risks #2065. C5 CORRECTION: ROUTE_TAIL_SKIP banner claims ON but the remote_count==0 guard makes it inert on helper rigs (my banner-only check was wrong). C2 (decode swap depth) rejected for TG. C4 claim stripe = follow-up.
- **A census**: STRATA_SWAPIN_DEPTH default is 8 (not 16); its #1 (tail-skip inactive) was WRONG for this rig per banner, then corrected by E (inert via remote guard — C5 fixes). Engine --expert-helper-reserve-mib ignored by helper (known).

### Code lanes (all code-only, no builds — box benchmarking)
| lane | branch/worktree | commits |
|---|---|---|
| D read-once design A | read-once-prefill @ Strata-wt-helper-ipc | a2cddd32b9 + c89d626480 (STRATA_READONCE_MIB, default off, RAM-guarded, request-scoped LRU, host test) |
| I helper window diet | helper-window-diet @ Strata-wt-helper-diet | 51b3899a11 + a2c30a5e55 + d596de441c (claim-sized memset, grow-only reply, lock-before-write) |
| C5/C3/C1/C4 | 4 fresh worktrees off v0232b-hotfix | running |

D died once on provider quota (reset 2026-11-15; operator restored early) — resumed same session, landed clean. Subagents confirmed NOT riding local :8009 (no listener; survived stop-all empirically).

### R3A_L320 (v0232b + DEPTH64 + reserve 320) + the fill-mib discovery
killer 466.7 tok/s / 375.3 s; killerL 121.0; longtg 93.1; tg1024 42.6; TG gens 20-26 — flat-to-worse vs V2 (534.5). nvidia0 7170 vs R2's 7144 MiB = only +26 MiB: **lowering reserve bought nothing because fill-mib 6796 is the binding cap** (budget = free - reserve stays > 6796 at both 512 and 320). Plain reserve ladder cut (R3C pointless); the real residency arm = reserve 320 + FILL_MIB 7300 (~+400 slots), #2065 headroom is the watched risk. Lean TG panel (panel-tg.sh, f425d55427: warm + tg16k/tg64k/tg180k token-scaled mt 512, tg180k = T1's exact 995000-byte prompt = the 180k infill) ran on the R3A boot; standard panel.sh retired after R3A (per operator: lean panel's fresh-prefill passes carry the PP numbers).

### Next
Fill-raise arm -> drafter-depth arm (STRATA_SPEC_MIN_P=0.4 via 452e5257ce passthrough) -> Q8_IMAGE arm -> build window for D+I+C5+C3+C1+C4 when ladder ends -> panel-tg on winners -> prod restore proposal.

## Round 18c — C3.1 graph-family trim landed (2026-11-13 evening, code-only)

Lane C3 (this journal's code-lane table): worktree `/mnt/srcds/Strata-wt-graphtrim`, branch
**graph-family-trim** off v0232b-hotfix, commit **532ba8cd2b** (`feat(core): free the stale verify
graph family behind STRATA_GRAPH_FAMILY_TRIM`, +63 lines: verify.cpp refresh_ar/trim_stale_family,
verify.hpp members, DETAILS.md opt-in row). Knob **STRATA_GRAPH_FAMILY_TRIM=N** (value = hysteresis
windows, min 1; unset/0 = off = today's bits) — default OFF per the design doc's containment note.
Mechanics: after ar_off_ has held for N windows, cudaGraphExecDestroy the other family's exec_
/exec_nr_ pools (evict_window_graph's rule: sync cs_ first, reset launched_ flags so a recapture is
a cold first launch; skip when pipelined windows read exec_ directly, when capture_all holds
no_window_eviction_, or after release). Flip-back re-captures lazily (5-56 ms/size). Expect
~50-150 MiB/card freed in the always-doorbell rig (log line prints pools + free-VRAM before->after);
funds C1's card1 Foresight slots and moves card1 off the 229 MiB #2065 knife edge. Panel arm
suggestion: STRATA_GRAPH_FAMILY_TRIM=8. Not built, not run (box benchmarking); joins the
D+I+C5+C3+C1 build window.

## Round 18d — W1 fp32 window wire + W3 wire diet landed (2026-11-13 night, code-only)

Lane W1+W3 of the helper-quant-sync-diet design (out/ab9-window-20261113/analysis/
helper-quant-sync-diet.md; W2/W4 stay with the claim-stripe lane).  Worktree
`/mnt/srcds/Strata-wt-wirediet`, branch **window-wire-diet** off v0232b-hotfix c7be950984, commits
**d678fb08ba** (feat(core): fp32 window wire + win-fast payload diet), **b774ee868a** (feat(helper):
GPU quant twin + window-lock trims), **1d9a980708** (test(core): end-to-end pins).  All default-OFF;
unset/0 = today's bytes on every path.

**W1 — kBeginF32** (frame type 20, HelloAck bit kFeatureF32Window=64, the exact kBeginQ8K
negotiation pattern: never sent to a bit-less peer, an old engine never asks).  Engine:
proxy_begin skips quantize_q8_1_cpu and ships the window's fp32 rows verbatim via
ExpertHelperClient::begin_f32 when STRATA_HELPER_F32_WINDOW=1 AND the peer advertised the bit (Q8_K
keeps precedence; fallback = today's kBegin).  Helper: serve_begin(image_f32) stages the rows in a
LAZY pinned slot (h_img, kMaxTok*H*4 = 80 KiB; nothing allocated on default runs so buffers_bytes
is unchanged), uploads to d_img_f32, and quantize_q8_1_f32_window_kernel writes d_q8 BEFORE the
grouped launch — the launch itself is byte-for-byte unchanged (the doc rejected fusing quant into
the GEMM: the image is re-read per group).  Bit-identity: one thread per 32-block running the CPU
quantizer's own statements in order (sequential amax/sum — NOT a butterfly — so the fp16 SUM is
bit-identical too, stronger than the prompt tier's ULP-close twin; #606 clamp, __float2half_rn,
roundf ±127); helper target sets no fast-math flags so nvcc's IEEE defaults hold.  Expected
−60-130 µs/frame net per the design (engine −72-144, helper lock +~15).  Golden pin:
STRATA_HELPER_F32_QUANT_DUMP=<path> dumps the first frame's input+output;
expert_helper_f32_window_test's compare runs for real under STRATA_TEST_F32_WINDOW_DUMP=<path>
(deferred to the build window — GPU execution only).

**W3** — STRATA_HELPER_WIN_FAST=1 (the doc's own name) covers W3.1+W3.3; W3.2 is separately
gated STRATA_HELPER_PINNED_RX=1 (pinned allocation is OOM-observable, so it rides its own knob).
- W3.1: the d_out pre-clear drops under the knob.  Store semantics PROVEN (comment at the call
  site): every down-kernel variant native_expert_grouped/_q8k can reach STORES
  out[ent_dst*H+r] — native_down_kernel :2226, multi :2279-2443, dws_rows :4496, expdec :2806-2811,
  s26 :3824/3836, s27 :4042/4054, amd :5706-5734, lds :6045-6046 — never += , and the only atomic
  on `out` in the TU is nib_hash_kernel's accumulator (:5306, a different buffer).  serve_begin's
  entry map covers exactly the memset's range; scan/pack read only that range.  Kept gated one
  release per the design's containment rule.
- W3.2: the q8/q8k H2D copies from the pinned h_img staging instead of the pageable RxBuffer (the
  RxBuffer itself reallocs as prompt frames grow, so registering IT is unsound — the staging slot
  is the stable pinned range; cost = one ~23 KB host memcpy).  Deviation from the doc's letter
  (cudaHostRegister of the RxBuffer) noted: same DMA effect, no realloc hazard, no giant
  registration cliff.
- W3.3: engine-side thread-local grow-only payload (begin/begin_q8k/begin_f32 share one builder;
  only the |= residency bitmap is zeroed) + resize-once q8/q8k images in proxy_begin.

**Composition**: does not touch helper-window-diet's spans (its narrowed scratch clear, grow-only
kRows reply buffer, and lock-before-reply-write all predate on this base and stay).  Neighboring
hunks for the rebase: serve_begin's upload/clear block (mine: image dispatch + clear gate) sits
between their scratch-clear hunk and their reply-write hunk; my dispatch edit adds kBeginF32 to
their `head.type == kBegin || kBeginQ8K` line.  The claim-stripe lane shares
expert_helper_protocol.hpp (my additions: kFeatureF32Window=64, kBeginF32=20 — no existing lines
moved) and expert_helper_socket.cpp (my additions: the two knob helpers above the class, the
build_window_payload static below validate_rows_payload, begin_f32 after begin_q8k, the
thread_local payload lines inside begin/begin_q8k, f32_window_ latch line in hello).

Tests (host-only, no links beyond the socket test's own three-source shape):
expert_helper_f32_window_test (new; CMake-registered beside expert_helper_socket_test) + ownership
test section (F) driving the REAL proxy_begin over a recording fake helper for the full env×bit
matrix (old engine↔new helper, new engine↔old helper, and both-on).  Not built, not run.

Build window: fresh configure, then HIP round-13 recipe (-DCMAKE_HIP_COMPILER=/opt/rocm-6.1.0/
llvm/bin/clang++, -DCMAKE_HIP_ARCHITECTURES=gfx906, -DCMAKE_PREFIX_PATH=/home/srcds/rocm-gfx906-
xnack;/opt/rocm-6.1.0, STRATA_HIP_GFX906=ON, STRATA_ENABLE_DFLASH2=ON, STRATA_HAVE_DFLASH2=OFF,
STRATA_BUILD_TESTS=ON, nice -n 10 make -j8).  The CUDA helper must ALSO be rebuilt (helper_main.cpp
changed; separate CUDA tree — STRATA_BUILD_EXPERT_HELPER=ON there).  Then: run
expert_helper_f32_window_test + expert_helper_ownership_test; run the helper once with
STRATA_HELPER_F32_WINDOW=1 STRATA_HELPER_F32_QUANT_DUMP=/tmp/opencode/f32golden and rerun the test
with STRATA_TEST_F32_WINDOW_DUMP=/tmp/opencode/f32golden for the real golden compare; a single
failed compare = revert the env, nothing else.

## Round 18e — claim-stripe lane: stage-1 stripe + stage-2 reply ring/W4 landed (2026-11-13 night, code-only)

The claim-stripe lane (this journal's round-18b code-lane table, C4 follow-up), both stages landed
as code-only commits.  Worktree `/mnt/srcds/Strata-wt-claimstripe`, branch **claim-stripe** off
v0232b-hotfix c7be950984, commits **da334704ee** (stage 1, `feat(core): stripe decode window claims
across both helper sockets (STRATA_CLAIM_STRIPE)`, engine-side only) and **3b964e33c2** (stage 2,
`feat(helper): reply ring lane + event-driven completion (STRATA_HELPER_REPLY_RING)`, +1060/-26
over 6 files).  Not built, not run (box benchmarking); joins the D+I+C5+C3+C1+W build window.

**Stage 1 (STRATA_CLAIM_STRIPE, default OFF)**: decode claims stop serializing one kBegin/kRows per
claiming layer on the window channel.  With the env set and `--expert-helper-socket2` live, each
layer's claim list splits at the expert boundary nearest the row midpoint: the A-half rides channel
A as the layer's own kBegin (B's entries masked to -1), the B-half rides channel B as ONE kBeginP
(the claim-batch family's single-part transport).  Both channels' replies fold into the window's
scatter; counters/DONE-line reporting unchanged.  Layers under 4 claimed rows, single-expert
claims and the Q8_K wire path keep today's single-flight frames.  Correctness never depends on
STRATA_HELPER_STREAMS (the W4 note below is about the HELPER's lock, not the stripe).

**Stage 2 — W2 (the memfd reply ring)**: with STRATA_HELPER_REPLY_RING set on BOTH sides, the
helper allocates a 2-slot pinned+mapped ring (slot 0 = primary channel, slot 1 = --socket2;
~1.6 MB at H=2560: stride 823,296 B page-aligned, header page + 2 strides), exposes the same
pages to the engine as a memfd attached to the kHelloAck via SCM_RIGHTS (new transport methods
write_reply_fd/read_frame_anc in helper_frame.cpp — the shared TU both sides compile), and a
negotiated rows reply shrinks to the 16-byte kRowsRing doorbell {n_rows, slot, seq}.  A slot's
records area is BYTE-FOR-BYTE the kRows/kRowsP payload the socket would carry, so the engine's
validate_rows_payload runs verbatim on the mapped bytes (apply_ring_rows).  serve_begin's
epilogue kernel `ring_pack_records` (one block, grid-stride over the row floats, system-fence +
syncthreads before thread 0 publishes the seq seal) packs `[claim_idx | H floats]` straight into
the slot — the :708-712 host pack loop AND the 72-90 KB of row bytes both leave the frame.
Ownership per the doc's contract: slot k belongs to channel k for the process lifetime; the
one-outstanding-per-channel rule makes each slot's generations strictly sequential (the next
kBegin on a channel cannot arrive before the engine consumed the previous doorbell); the per-slot
seq word is the tamper seal — a doorbell whose seal disagrees is kErrSeq and closes the channel
(host test pins the stale-seal refusal).  Per-slot pinned claim staging (h_ring_claims[2]): a
host memcpy into mapped memory is NOT a stream op, so a single shared staging buffer would let
channel B's parse overwrite the claims channel A's queued-but-not-executed epilogue still reads —
one buffer per slot closes it.  serve_prompt's lane (the stripe's B-half transport) copies the
core's already-packed reply into the slot and answers with the doorbell (the prompt tier's pack
is decode-sized for the B-half; claims over the 80-row slot cap keep today's kRowsP per frame —
per-frame fallback, no renegotiation; kRowsPB multi-part always stays on the socket).

**Stage 2 — W4 (event completion, early lock release) rides the lane and only the lane**:
serve_begin releases the device lock AT LAUNCH (both lock modes: gpu_mtx and the L2 gate) because
the frame's rows already left the shared d_out with the epilogue — the scan reads the slot, never
d_out, so the next frame's kernel may overwrite d_out the moment this frame's stream work
completes; d_q8/d_meta/d_scratch reuse is safe by stream FIFO (or per-tier staging under
STRATA_HELPER_STREAMS).  The serve thread waits its OWN cudaEvent (recorded after the epilogue;
spin on cudaEventQuery then cudaEventSynchronize) instead of draining the stream; serve_fill
event-fences BOTH slots' events before touching device state (fill waits events, not replies —
the events are recorded at birth so an idle channel is covered).  Without the ring the lock keeps
today's full span in THIS tree (helper-window-diet's release-before-reply is a separate branch);
serve_prompt keeps its whole-frame lock (its pack still reads d_out_p host-side).  Default
single-stream mode gets wire overlap only (B's full-lock frame drains A's FIFO tail); true
compute concurrency still needs STRATA_HELPER_STREAMS — but correctness holds in both modes.

**Negotiation (every old/new combination works)**: the engine asks with HelloReq::reserved0's
kHelloWantReplyRing (unknown bits refused by name, kErrProtocol); the helper answers
kFeatureReplyRing + the memfd, or neither.  A helper BINARY that predates the flag word refuses
any nonzero reserved0 with kError — the client retries the hello ONCE with the flag clear and
proceeds on the socket lane (host test pins the retry).  The engine refuses a bit-without-fd ack
(split-brain guard) and a header that disagrees with its own hello geometry (magic/version/
n_embd/stride, validated against fstat's size).  The engine's knob is read per-hello (env flip =
engine restart, as every other knob here); the helper's env allocates the ring at startup and a
creation failure disables the lane only (socket replies, one stderr line).

**Composition (rebase map for the integrator)**: stage 2 shares the wire-diet-edited files —
expert_helper_protocol.hpp: my additions are kFeatureReplyRing=128 (bit 7 — RENUMBERED OFF 64 at
landing: window-wire-diet's kFeatureF32Window took bit 6 first, same-rebase clash avoided) +
kRowsRing=21 (20 is wirediet's kBeginF32) + kHelloWantReplyRing + the Ring/RowsRingResp/
layout-helper block before the M7 section + write_reply_fd/read_frame_anc/write_seq(fd=-1) on
Socket — no existing lines moved except write_seq's signature tail.  expert_helper_socket.cpp:
knob helper after claim_batch_env, hello()'s send/read restructured into the retry loop (the flag
set + read_frame_anc + the kError retry), map_ring/apply_ring_rows/release_ring after hello,
ring_lane_/ring_map_ members + reply_ring_active() in the header, finish()'s via_ring dispatch
around the type check, finish_prompt()'s same dispatch, release_ring() in close().
helper_frame.cpp is NOT edited by wirediet (clean): read_all_anc in the anon namespace +
write_reply_fd + the fd param on write_seq.  helper_main.cpp (not edited by wirediet or
window-diet beyond their own hunks): Helper ring fields + teardown, the W2 helper block (kernel +
ring_slot_of/ring_reply_sealed/ring_event_wait/ring_create) after cuda_check, serve_fill's event
fence after its locks, serve_begin's `int ring_slot` param + empty-claims ring branch + the
ring/W4 path between the launch check and the legacy sync, serve_prompt's `ring_slot` param +
via_ring reply wrapper, serve_channel's ring_slot local (reset at accept_peer) + hello flags
parse + ack bit/fd + both dispatch sites, main's ring_create call, usage()'s env line.

**Tests** (expert_helper_socket_test section 22, host-only, no GPU, no new links — a real memfd
+ the transport's real SCM_RIGHTS path): layout arithmetic (page-aligned stride, exact coverage,
records offset), the full negotiation matrix (wanted+offered maps the ring; wanted+not-offered
falls back; corrupt header refused; unasked-offer refused; legacy-helper kError → flag-clear
retry), doorbell application through finish (original indices, counters) + generation sequencing
+ stale-seal kErrSeq, un-negotiated doorbell kErrProtocol, prompt doorbell at the pair indices.
GPU pins scaffolded as (22f): epilogue bytes vs the legacy pack, event ordering across both
stream modes, fill fence.

**Build window (this lane needs the CUDA helper rebuilt too)**: fresh HIP configure per round-13
recipe (clang++/gfx906/xnack paths as in 18d), `nice -n 10 make -j8 rpcw-hip/strata` for the
engine, and in a separate CUDA tree `STRATA_BUILD_EXPERT_HELPER=ON make strata-expert-helper`
(helper_main.cpp + helper_frame.cpp changed — nvcc compiles the epilogue kernel).  Then
expert_helper_socket_test (section 22 included); panel arm: STRATA_CLAIM_STRIPE=1 +
STRATA_HELPER_REPLY_RING=1 on both sides with --socket2 live; watch the boot notice
("reply ring mapped") and rpcprofile's ring=1 lines; single failed doorbell = unset the env on
either side, nothing else.

**Round 18f — C1 foresight-shadow landed (backfilled record; entry was missing).**
Branch foresight-shadow @ /mnt/srcds/Strata-wt-foresight, 4 commits off c7be950984:
b4506679a4 (Fix 1: dispatch order host_res -> peer -> fs->take -> helper_holds -> miss, gated
fs->helper_shadow; single take() per row either way), 8dbf925022 (Fix 2: proxy_begin gets kind;
slot-served experts masked slot-0 "resident" in a lazy full-table copy feeding BOTH the kBegin
bitmap and the claims loop -> helper row derivation and proxy_claims_ stay row-for-row; no wire
change), d8af6a98bd (Fix 3: pinned staging ring STRATA_FS_RING default 16 x ~32 MiB, shadow-only;
note_miss admit/budget gates, stage_blob stages pair bytes OUTSIDE the slot lock, demand feed one
note_miss per distinct claimed expert at the begin seam; provider = copy_cached_blobs then
ExpertSource::swap_blobs_batch fallback — the socket-proxy tier has no engine-side bytes, so the
doc's copy_cached_blobs-only source was adjusted; same bytes, engine hit-path arithmetic),
d4d81db959 (STRATA_FS_SLOTS accepts "3,2" per card; boot line prints the split).
Knobs: STRATA_FS_SLOTS (N uniform | "3,2"; unset = swap off), STRATA_FS_HELPER_SHADOW 0/1
default 0 (needs FS_SLOTS), STRATA_FS_BUDGET 32, STRATA_FS_ADMIT 1 (arm recommends 2),
STRATA_FS_DEPTH 4, STRATA_FS_RING 16. Shadow panel arm: STRATA_FS_SLOTS=3,2
STRATA_FS_HELPER_SHADOW=1 STRATA_FS_ADMIT=2 (+ STRATA_GRAPH_FAMILY_TRIM=8 for card1 headroom).
Deferred: host test (no-links claim-suppression/slot-accounting test — add in the build window).
Reviewer verdict PASS-with-notes: all four fixes present, reorder a no-op shadow-off, claim-mask
sound; notes = missing host test + this missing entry, stage_blob runs synchronously on the
dispatch thread (ring-bounded), exchange-dropped slots leak their ring entry until reassign.
Engine-only build (round-13 recipe, fresh dir rpcw-hip-foresight).

**Round 18g — review gate over all 8 lanes (lanes-review.md written).**
0 FAIL; 7x PASS-with-notes / PASS. One semantic merge obligation for the integration branch:
merged proxy_begin must forward foresight's claim_res into ALL FOUR begin variants
(begin/begin_q8k/begin_stripe/begin_f32) or the combined FS_HELPER_SHADOW+STRIPE/F32 arm breaks
with kErrShort channel closures. Feature bits 64/128 and frame types 20/21 collision-free;
cherry-pick order tailskip -> graphtrim -> foresight -> claim-stripe -> helper-window-diet ->
read-once -> wirediet holds (conflict map in the review doc). helper-diet note: the narrowed
memset is the SCRATCH clear (d_out was already claim-sized at base) — scratch-tail staleness is
the W3.1 proof class, guarded by the still-live scan. Eyemark top-5: (1) panel the C1 shadow
with the claim_res fix (+27-42% -> 36-41 tok/s), (2) drafter acceptance at long ctx (2.58 -> 4+
drafted/window ~= +13-14 tok/s), (3) stripe+ring arm with STRATA_HELPER_STREAMS=1 (+15-30% of
post-C1 residual), (4) W1 F32 golden pin + arm (-2-4 ms/window), (5) move stage_blob staging off
the dispatch thread. Build window still gated on the operator's go.

**Round 18h — v0233 arm-2 live fault root-caused: the ring window lane never stored the claim words.**
Arm 2 (stripe+ring+streams over the shadow) booted clean on both channels but the WARM leg's first
window exchange died on BOTH: engine "expert-helper: the ring reply failed validation: helper row
index refused (0 rows for 8 claims)" then "channel closed after 1 requests / 0 rows (CPU fallbacks
1)" + "the prompt tier's channel (socket2) closed after 34 prompt requests / 27660 prompt rows
(1 striped window requests / 0 rows)"; warm request degraded to helper=reqs=0 rows=0; helper exited
(channel closed), panel died ConnectionReset (logs engine-v0233-stripe.log:~113, trial-spill40-...
-helper.log). Diagnosis: B's 34 kBeginP ring doorbells applied fine (serve_prompt packs its slot
records HOST-side with the pair indices, helper_main.cpp:1425-1428/1470-1478), so the ring per se,
the per-channel slots (ring_slot = secondary ? 1 : 0, :1937), the seals and the stripe's B-half
transport were all exonerated, as were the shared_lock W4 release (:1082-1083) and the
claim_res/shadow bitmap agreement (engine remote_experts.cpp:667-673 vs helper serve_begin:935-950).
The helper's "1 windows served" + kErrBadId pointed at channel A's own kBegin ring reply, and
serve_begin's epilogue kernel ring_pack_records (helper_main.cpp:536-557, born in 6296f933fc)
took `claims` as a parameter but NEVER wrote the 4-byte claim index of any record - it stored only
the floats and the RowsResp header, so every record's index word read the memfd's zeroed page
(all-zero index -> unclaimed or duplicate) -> validate_rows_payload kErrBadId (expert_helper_socket
.cpp:93-97) -> channel A closed -> proxy_finish closed B unread (remote_experts.cpp:830-838).
Ring-specific, not stripe-specific: ANY non-empty window kBegin with STRATA_HELPER_REPLY_RING died;
arm 1 passed because its window rows rode the socket. Host test sect 22 missed it because the
window pack is device code; (22f) had already named the gap.
Fix (v0233-candidate @ 1ce7ca8261, code-only): the record layout lives once in the protocol header
as ring_record_claim/ring_record_floats (expert_helper_protocol.hpp, __host__ __device__ under
__CUDACC__/__HIP__), the kernel stores each claim word from the staged claims before the
threadfence/sync/seal publish, and expert_helper_socket_test section (23) pins it end to end on a
FakeRing through the real finish() doorbell path - the accessor-packed slot applies 8 rows at their
original entry indices, and the pre-fix float-only bytes are refused kErrBadId with the live
fault's exact signature. Gating untouched (ring default off). Syntax-probed host TUs clean
(g++ -fsyntax-only); no builds/engines touched per arm-2 constraints.
Validation re-run (arm-2 env set, unchanged): STRATA_CLAIM_STRIPE=1 STRATA_HELPER_REPLY_RING=1
STRATA_HELPER_STREAMS=1 STRATA_FS_SLOTS=3,2 STRATA_FS_HELPER_SHADOW=1 STRATA_FS_ADMIT=2
STRATA_GRAPH_FAMILY_TRIM=8 STRATA_SWAPIN_DEPTH=64 STRATA_HELPER_RESERVE=320 (helper side needs
STRATA_HELPER_REPLY_RING=1 too). Expect: both channels' "reply ring mapped", first window exchange
applies rows (rpcprofile ring=1 lines), DONE line helper=reqs>=1 rows>0 fallbacks=0, no
"ring reply failed validation".

**Round 18i — v0233 arm campaign complete: the shadow stack is the champion.**
Integration branch v0233-candidate (/mnt/srcds/Strata-wt-integ): all 8 lanes cherry-picked off
c7be950984 in reviewer order + 4 integration fixes (claim_res forwarded into all four begin
variants incl. begin_f32; ring_n accessor; nvcc-illegal and-declaration in the golden dump;
shadowed buffer in the ownership test) + the ring epilogue claim-word fix (18h, 1ce7ca8261).
Host ctest 8/8 after fixes; F32 golden pin PASSED on live GPU output (GPU quant twin ==
quantize_q8_1_cpu byte-for-byte).
Arms (lean TG panel, base = v0232b-hotfix + STRATA_SWAPIN_DEPTH=64 + reserve 320):
- ARM1_SHADOW (FS_SLOTS=3,2 FS_HELPER_SHADOW=1 FS_ADMIT=2 GRAPH_FAMILY_TRIM=8): CHAMPION.
  180k warm TG 29.0-31.5 (R3A 18.8-23.8, +29-68%); 16k 25.6/26.6 (was 22.2); 64k 34.3 peak;
  PP 64k 521.5 (+45%), PP 180k 578.7 (+9%). 4472 experts served from swap by panel end;
  180k draft acceptance rose 65% -> 71-82%. #2065 warning fired once at 35 MiB free (no stall);
  staging ring saturated (69 staged, 108k ring-full) -> next lever is ring size/off-thread staging.
- ARM2R (+CLAIM_STRIPE+REPLY_RING+STREAMS, after the 18h fix): 0 validation failures, full panel,
  but 180k warm 24.8-27.2 and acceptance back to 65% — parity/worse; REJECTED for prod (lanes stay
  landed, default-off). The 18h fix itself is validated in production shape.
- ARM3_F32WIRE (+F32_WINDOW+WIN_FAST+PINNED_RX): parity-to-worse (fp32 image is 3.6x the q8 bytes
  per frame; the engine quant skip does not pay for it here); REJECTED for prod. Golden pin stands
  as the lane's correctness deliverable.
- ARM4_DRAFTER (+SPEC_MIN_P=0.4): 16k TG 30.6 and PP 16k 265.9 (+78%) but 64k warm collapsed to
  11-16 and 180k to 21-26.9 — deeper drafts at unchanged acceptance cost more than they earn;
  REJECTED.
- ARM5_FILLRAISE (+FILL_MIB=7300): +103 slots (3675 vs 3572; budget formula, not +400), hit rate
  flat 96.4%, 180k worse with a 6.3 stall; REJECTED.
PROD PROPOSAL (operator's call): v0233-candidate binaries + env STRATA_SWAPIN_DEPTH=64
STRATA_HELPER_RESERVE=320 STRATA_FS_SLOTS=3,2 STRATA_FS_HELPER_SHADOW=1 STRATA_FS_ADMIT=2
STRATA_GRAPH_FAMILY_TRIM=8; every other knob OFF. Honest bar state: 64k at the 35 floor (34.3),
180k at 29-31.5 (floor not reached — the remaining lever is the staging ring saturation, then the
stall class). Launcher: launch-trial-spill40.sh now forwards the helper-side knob family
(REPLY_RING/WIN_FAST/PINNED_RX/STREAMS/F32_QUANT_DUMP) past its env -i sanitization.

**Round 18k — full-residency boot hang root-caused and fixed (v0233-candidate @ 687b459619).**
The no-relief boots (22:16/22:22/22:29 gens, prod-swift15-...-3010059/3097037/3198324) all died
after the engine's "loaded 46.84 GiB" line (generate.cpp:8608) and never printed the next engine
line, "GPU host-callback thread started" — the STRATA_ADAPT_CALLBACK_WARMUP block's
cudaLaunchHostFunc + cudaStreamSynchronize(default stream). Between those two prints the strata
side runs NO loop at all: the whole-arena path's post-fill work is O(1) (single alias,
expert_source.cpp:6547-6557), all registration loops are per-RANGE (pinned.cu:786 whole span = ONE
cudaHostRegister; :900/:1017 per-range; alias tables per-range) — the task's per-page/per-pair
suspects were exonerated by reading. The 100%-one-core, read_bytes-zero profile is the HIP driver
spinning its default-stream sync over the 46.84 GiB PORTABLE|MAPPED THP registration (12.3M pinned
4K pages) that the PinnedArena constructor made BEFORE the fill; the callback-thread launch over it
never returns. Proof by contrast: the relief boot passes the identical call in seconds
(engine-prod-iq3s-v0233.log :27->:29, "reserve only (nothing registered)" -> "loaded" ->
host-callback line) and registers 21.5 GiB/3440 ranges AFTER the handshake (line 56), serving fine
— so registration per se is fine; the warm-up call over the whole-span registration is the poison.
Fix (code-only, v0233-candidate): generate.cpp moved the __linux__ warm-up block to right after the
device check, BEFORE the arena is reserved/registered — the driver creates its host-callback thread
while nothing is registered; #1964 affinity rule intact (no cudaSetDevice between the new spot and
the old, ExpertPool pinning far below). Relief boots: the line just prints ~60 s earlier. Also
added ~10% progress prints so a slow boot is never silent: whole-span registration begins/ok+seconds
(pinned.cu), ByNeed + register_after_fill range loops (ranges, GiB), open()/apply_registration alias
tables (ranges), register_complement pair scan (layers). Engine build clean
(build-mixed-gpu/rpcw-hip, make -j6 strata, no new warnings on the edited TUs). Host test: the fix
is GPU-call ordering in generate.cpp — not extractable; every touched loop calls cudaHostRegister/
cudaHostGetDevicePointer. Range-merge arithmetic stays covered by arena_pin_ranges_test (host,
green). pinned_alias_test needs a real registration (GPU) — not runnable while prod serves.
Expected relief-off boot: registration ~60 s (now announced + timed), fill ~57 s, pool+prefills+
handshake ~20 s -> under ~2.5 min, well inside the 1200 s launcher deadline.
PROD CHANGE to re-apply full residency (operator mandate: all experts resident, no eviction): drop
the four relief args from the engine config — --expert-helper-ram-relief, --expert-helper-ram-relief-packs,
--expert-helper-relief-hot-mib, --expert-helper-relief-arena-cap-mib — keep everything else (env
STRATA_SWAPIN_DEPTH=64 STRATA_HELPER_RESERVE=320 STRATA_FS_SLOTS=3,2 STRATA_FS_HELPER_SHADOW=1
STRATA_FS_ADMIT=2 STRATA_GRAPH_FAMILY_TRIM=8, startup-timeout 1200 s). Boot lines to watch:
"strata pin: registering the whole arena (47.9 GiB) for DMA before the fill ..." -> "... ok in N s"
-> "GPU host-callback thread started before host-core pinning" (now BEFORE the arena lines) ->
"loaded 46.84 GiB" -> helper handshake.

### 18m — prod ops (parent): verify-layer-13 death spiral, 576 reserves, boot #7 stable
Residency flip attempts on the round-1 binary (687b459619) still hung after the expert-cache line ->
stayed on relief. Relief then entered a NEW fatal mode: "Unrecoverable native verification failure:
verify: timed out at layer 13; its GPU waits were released" -> engine self-exit -> helper channel
close -> supervisor rebuild, every ~5 min. Mechanism: each rebuild resumes the same ~131k sessions ->
card1 back to the #2065 35-MiB-free paging regime -> GPU wait at layer 13 stalls past deadline ->
fatal. Fix (config): --vram-reserve-mib / --vram-reserve-later-mib 256 -> 576 on relief
(88f43643d5). Boot #7: up 150 s, survived 4-min traffic window, 0 verify-timeouts, engine stable
>10 min (gen 10 cumulative). Relief champion stack otherwise unchanged.
Round 2 (register-last) in flight; reviewer gate now mandatory before any build/flip/arm; all future
panels carry temp-0 sha guards (driver pins temperature 0 + records completion sha256; killer shape
= standalone prefill leg ctx=995000 on a fresh boot, no 64k-leg prefix contamination).

**Round 18n — register-last lands: the whole-arena registration as the LAST boot step
(v0233-candidate @ 23fb6b114b).**
Round 1 (18k, 687b459619) moved the host-callback warm-up before the registration but the no-relief
boot still hung after the expert-cache line: EVERY remaining boot-time driver sync over the live
whole-span registration (the cache fills' syncs, the warm-ups', the captures') spins over the 12.3M
pinned 4 KiB pages. Round 2 generalizes the relief path's proven shape WITHOUT the drop: open()
reserves the arena ReserveOnly (resident-but-unregistered; the fill writes plain pages, nothing
evicts them during boot) and the whole-span cudaHostRegister runs LAST, so no boot sync ever sees a
registration. Nothing is dropped - every expert stays resident (the operator mandate).
Implementation (3 files, +80/-1): expert_source.hpp declares register_whole()/register_last() and
the register_last_ member; open() arms register-last for exactly the plain whole-arena pin
(!by_need, !relief_reserve, !deferred, max_pinned_bytes==0, arena_pin_cap_gib()==-1, no shared file,
!arena_no_register(), STRATA_REGISTER_LAST=0 opts out) and takes the ReserveOnly path;
register_whole() (expert_source.cpp, after register_complement) is ONE page-outward
register_after_fill range over the full capacity (the packed layout's blobs straddle pages; a range
boundary would un-pin the blobs it cuts - pin_range_of's one-range rule; lock_rest=true: a refusal
keeps the working-set lock, the UMA degrade) + apply_registration() for the alias tables; close()
clears the flag. generate.cpp calls it as the LAST statement before the serve loop's READY print
(after pin_sim_report and pin_reserve_release_hard) and at the head of the one-shot request loop -
the two places a request's DMA can first reach the arena.
New boot-phase ordering (relief off, one line per phase):
1. GPU host-callback warm-up (round 1's spot, before the arena)
2. arena reserved UNREGISTERED (ReserveOnly; note says "registers as the last boot step") + fill
   ("loaded 46.84 GiB"; no registration line between them anymore)
3. pools, cache fills, drafter warm-ups, graph captures, helper handshake (all over an
   unregistered arena - the hang is structurally gone)
4. "strata pin: registering the whole arena (47.9 GiB) as the last boot step ..." ->
   "strata pin: register-last: whole-arena registration ok in N s (47.9 GiB registered)" (the same
   begin/ok+seconds pair round 1 gave the before-fill registration; a driver refusal prints
   "stopped at the driver's refusal" and degrades to locked-no-alias)
5. "READY <ctx> stop" -> serve loop accepts requests
Build evidence: build-mixed-gpu/rpcw-hip, `nice make -j6 strata 2>&1 | tee ../build-18n-register-
last.log; PIPESTATUS[0]` -> exit 0, [100%] Built target strata, binary 50,012,632 B @ 2026-11-14
11:11:01 (was 50,000,600 B @ 2026-11-13 23:24:01, the round-1 build); zero warnings on the new
lines (the HIP nodiscard/unused noise is unchanged). A concurrent 18o lane (vram_guard) shares the
worktree; its files are NOT in this commit (staged: only the 3 files above). Not yet flipped to
prod: the reviewer gate (18m) runs before any boot/flip/arm, and pinned_alias_test still needs a
real registration - not runnable while prod serves.
- Addendum (review note A): condition now Linux-gated as documented; non-Linux keeps before-fill registration (c873503f0c)

**Round 18o — the per-GPU VRAM guard lands (v0233-candidate @ ce210ea4ea).**
The 18m diagnosis: on the 2x Radeon VII split a card's free VRAM sinks to tens of MiB at long
contexts (the "only 35 MiB of VRAM free" #2065 warning class), the driver pages GPU memory, and a
GPU wait stalls past its deadline -> "Unrecoverable native verification failure: verify: timed out
at layer 13" -> engine self-exit. Static --vram-reserve-mib only partially mitigates: pressure is
per-card and time-varying. Operator mandate: "fix residency but only by 64mb steps on the gpu it
happened at" -> a per-GPU adaptive guard, env-gated default OFF (campaign convention).
Design (new module include/strata/core/vram_guard.hpp + src/core/vram_guard.cpp, CPU-only TU by
construction - no CUDA call, so the host test links it alone): per-device state {boot floor, floor,
step count, last-step timestamp, cap-said flag}, 16-slot static table, atomics for every read +
one mutex over the step path (the check sites are hot; a disabled check is one relaxed atomic
load). check(dev, free_mib): free >= the low line (512 MiB Linux / 1024 Windows - the mirror of
generate.cpp's file-local kVramSafeMib) -> nothing (hysteresis: the floor never shrinks); under it
-> floor += exactly one step (default 64 MiB), at most once per 1 s per card, growth capped at
+4096 MiB over the card's boot floor (one loud cap line per card), then the card's reclaim donor
(fn pointer set by the engine) is asked to free ~a step; check returns still-low so the caller
keeps its own warning.
Sites wired (probe BEFORE the risky point; one call each):
- src/core/verify.cpp:2850 (Verifier::capture): the site's existing cudaMemGetInfo figure feeds
  check(device_, ...) right before the window capture - the exact #2065 capture-hang spot (the
  observed fatal was verification at layer 13, card 0); the "capturing the %d-token window" trace
  line stays.
- src/core/mtp.cpp:1796 and :1839 (both "mtp draft: waiting for the GPU" #2065 waits, fixed-step
  and top-k branches): guarded cudaGetDevice+cudaMemGetInfo (only when enabled) then check(dev,...)
  before the spin-wait - the same stall shape as the verify fatal.
- src/core/conversation_copy.hpp:151 (copy_nonblocking): same guarded probe before the device
  copy - parked K/V coming back is exactly the long-context regime that pushes a card under the
  line, so the next capture finds the floor already stepped.
Log line format (stderr, "CUDA%d" matches the per-card serve lines):
  strata vram guard: CUDA0: 35 MiB free is under the 512 MiB line: reserve floor 700 -> 764 MiB (+64 MiB, step 1), reclaimed 64 MiB
  (no donor yet: "... reclaimed 0 MiB (no reclaim donor registered)")
  strata vram guard: CUDA0: still 30 MiB free but the reserve floor is capped at 4896 MiB (4096 MiB over the 800 MiB boot floor): not raising it further - lower --max-context / --batch or raise --vram-reserve-mib
Env knobs: STRATA_VRAM_GUARD=1 enables (exactly "1"; anything else/unset = fully inert - one
atomic load per check). STRATA_VRAM_GUARD_STEP_MIB (default 64), STRATA_VRAM_GUARD_MAX_MIB
(default 4096). Read once at first use; configure() lets a test/engine override.
BLOCKER (per the lane split, 18n owns expert_source/generate): the per-card reclaim donor cannot
be registered from this lane. ExpertCache::shrink refuses on HIP (no VMM segments), so the real
donor is the engine's own per-card loan/swap recall, reachable only from src/program/generate.cpp
(parallel-lane owned). Exact intended call sites are documented as the TODO at src/core/
vram_guard.cpp:76: (1) after the "expert cache auto:" sizing (~generate.cpp:8820) call
set_base_mib(dev, o.vram_reserve_mib) per card; (2) once at startup call set_reclaim() with a
lambda that trims ~want_mib from THAT card's pool. Until that lands the guard steps and logs per
card with "no reclaim donor registered" - the floor growth + the naming of the card that ran low
are live, the actual freeing needs the generate.cpp call.
Build+test evidence (own dir, rpcw-hip flags replicated from build-mixed-gpu/rpcw-hip's cache:
STRATA_ENABLE_CUDA=ON, STRATA_ENABLE_HIP=OFF, STRATA_HIP_GFX906=ON, Release, tests on, the
community-tuning/MMQ/native-experts set): build-mixed-gpu/rpcw-hip-guard, `nice make -j6 strata`
-> exit 0, [100%] Built target strata, binary 44,122,632 B @ 2026-11-14 11:18:31 (rpcw-hip
untouched, still the 18n build). Host unit test tests/core/vram_guard_test.cpp (arena_pin_ranges
convention; CMakeLists registers vram_guard_test = test TU + vram_guard.cpp, no other lib):
off/inert, 64 MiB steps 700->764->828, hysteresis (recovered card keeps its floor), cap at
base+192=892 with the capped report changing nothing, the 1 s step window blocking an immediate
second step, per-card isolation (card 1 steps while card 0's floor/count are untouched), a rising
boot floor lifting the floor, out-of-range devices ignored - `ctest -R '^vram_guard_test$'`:
1/1 Passed (0.00 s). The test caught one real bug pre-commit: last_step_ms==0 meant "recently
stepped" for a fresh card when uptime < the window; 0 is now explicitly "never stepped"
(vram_guard.cpp:100).
Not flipped to prod (default OFF; the 18m reviewer gate runs before any arm, and the donor TODO
above is the follow-up for the generate.cpp lane).
- Addendum (donor review, PASS-NOTES): N-c — with STRATA_PARK_VRAM_MIB unset (today's prod env) the
  donor structurally frees 0: the enabled guard is honest logging until park-vram is on; enablement must
  be measured WITH park-vram, and no enablement decision may assume the floor reserves anything on its
  own. N-b — donor-vs-serve-path release_all safety rests on the single-host-thread contract; if any
  check site ever moves off-thread (the staging lane's copy_nonblocking is the adjacent risk), the park
  slot pool needs a mutex first. N-a — t_in_donor lacks RAII (no realistic throw path today; scope guard
  when next touched).

**Round 18q — off-thread begin-seam staging lands (v0233-candidate @ d4f7b95052).**
The TG stall evidence (prod, eyemark #5, journal 18i ARM1): the helper shadow's ring staging runs
INLINE in ForesightSwap::note_miss at the begin seam — expert_pool_dispatch_multi's claimed-rows
feed (expert_source.cpp:~5291, one note per distinct claimed expert per tier) and the CPU-miss
note (:~5198) — so the request thread blocks on a pack read / helper D2H of milliseconds per
staged pair, between the helper's begin RPC and the decode continuing; the ring barely cycles
because staging sits BEHIND the seam instead of ahead of it (69 blobs staged vs 108k ring-full
rejections; 4-6 checkpoint lines per stall vs the usual 1-4).
Design (no async path existed — the filler thread only issues H2D; staging into the ring was
inline-only): a DEDICATED stager thread inside ForesightSwap.  STRATA_STAGE_OFFTHREAD=1 (default
0; read once in foresight_swap_from_env beside the STRATA_FS_* knobs via env_int; needs
STRATA_FS_HELPER_SHADOW with its staging ring — without them the flag is inert and no thread
starts) moves note_miss's helper-shadow slow path off the seam: the note only queues the
(layer, expert) demand (stage_mu_/stage_cv_, FIFO, cap 16384 — a deeper queue drops the note like
the inline ring-full refusal would, counted in the new stage_drops counter and the report line);
the stager runs stage_miss(), the EXACT old sequence — gates under smu_ (admit + budget, miss
counted at stage time), ring_acquire, the bytes read OUTSIDE every lock (the stage_blob lambda:
copy_cached_blobs under read_mu_, else swap_blobs_batch under file_mu_ — both already
cross-thread; strictly LESS concurrency than today, where every dispatch thread ran stage_blob
inline), then the gates re-checked and assign_locked.  One gate at the seam (the stage_offthread
branch in note_miss, the funnel both seam sites call); nothing scattered.  The stager WAITS OUT a
full ring (~1 s in 2 ms steps, stage_cv_ with the dtor's notify) instead of rejecting, so "ring
full" (ring_busy) now counts sustained exhaustion only — entries that free within a second are
consumed by the waiting stager, which is what keeps staging AHEAD of the decode.
Lifecycle: stager started in init() right after the filler (only stage_offthread && ring_n > 0);
~ForesightSwap sets stage_stop_ + notify, joins the stager FIRST (it may still queue a copy onto
the filler), then stops/joins the filler, then the card syncs, ring_mem free and event destroys —
no use-after-close (the lambda's captured ExpertSource/RemoteExperts outlive fs_swap by
generate.cpp's declaration order, the same guarantee the filler rides).
Knob-off correctness: note_miss's inline path is the verbatim old sequence via
stage_miss(..., false) (same locks, same order, same counters — wait_ring false skips the wait
loop); no stager thread, no queue, boot note and report() byte-identical.  The 1ce7ca8261
claim-word fix lives in the HELPER reply ring (helper_main.cpp/protocol header) — untouched here;
this lane's ring is ForesightSwap's pinned staging ring, whose acquire/release/claim protocol
(ring_used under ring_mu_, a slot holds its entry until reassignment, release inside
assign_locked's pick) is respected unchanged — the stager is just a new caller of the same
sequence.
ARM7 (the measurable signature): ring-full rejections COLLAPSE toward ~0 (staging waits and
cycles entries instead of rejecting; ring_busy only fires on >1 s sustained exhaustion — watch
the report line "helper shadow: N staged (R ring full, S no source)" + the new "D staging notes
dropped (queue full)"), staged count should climb far past 69, checkpoint lines back to 1-4 per
window with no mid-generation stall clusters at helper=healthy timeouts=0 fallbacks=0.
Boot line to watch: the foresight swap note gains ", ring staging off the begin seam
(STRATA_STAGE_OFFTHREAD)".
Transport-only change: a slot either lands in time and serves the same bytes or the row keeps
today's helper/CPU fallback — the temp-0 sha guard (18m convention) MUST show identical
completion shas with the knob on vs off; any sha delta is a defect.
Build evidence: build-mixed-gpu/rpcw-hip, `nice make -j6 strata 2>&1 | tee
../build-18q-stage-offthread.log; PIPESTATUS[0]` -> exit 0, [100%] Built target strata, binary
50,018,488 B @ 2026-11-14 11:45:04 (was 50,012,632 B @ 11:11:01, the 18n build); the one
expert_source.cpp warning (the 'reserve' capture, :2606) is pre-existing (present in the 18n
log).  Host tests (host-only, default-off inline path compiled in): file_expert_source_test
(covers note_miss via the foresight-exchange cases), swapin_depth_test, tail_skip_guard_test ->
`ctest -R` 3/3 Passed (0.37 s).  No dedicated off-thread stager host test exists (lane 3's
deferred test debt covers the same surface); the reviewer gate (18m) runs before any arm.

**Round 18p — vram guard donor wiring.**
The 18o TODO (src/core/vram_guard.cpp) is discharged: the two generate.cpp call sites landed, plus the
review's N1-N4. Commit 13269309b7 on v0233-candidate (4 files: generate.cpp, vram_guard.hpp/.cpp,
vram_guard_test.cpp; +173/-63).
Call sites (generate.cpp @ 13269309b7):
1. set_base_mib(dev, base) x2: generate.cpp:8995 - right after the expert-cache sizing if/else chain
   ends (the auto path's #496 lowering and the spill block have settled o.vram_reserve_mib by then;
   cudaGetDevice picks the device the sizing read its free figure on) - and generate.cpp:9447, inside
   the per-stage layer-split loop right after the "layer split, CUDA%d: ... room for experts" print,
   with the per-card figure the walk kept: --vram-reserve-later-mib when given, else
   --vram-reserve-mib (kStageLateAllocMib is a pricing term, deliberately NOT part of the floor).
2. set_reclaim(donor) once at startup: generate.cpp:9004, before anything can reach a check site.
The donor (the review's "per-card loan/swap recall"): conversation_park_vram_release_all() - the
engine's own VRAM-pressure recall of the parked conversations' K/V slots (STRATA_PARK_VRAM_MIB; each
slot cudaFree'd on the card it lives on, conversation_snapshot.cpp vram_kv_free), the same release
the serve path itself uses when the K/V cannot grow (generate.cpp kvg_ensure retry, ~11285/19575).
Survey that ruled out the alternatives: ExpertCache::shrink refuses on HIP (no VMM segments, the
review said as much), the #533 elastic VRAM command is one-NVIDIA-GPU --vram-elastic only, the
elastic K/V and relend paths need xcache.vmm_range() (null on HIP), the prompt-path loan's recall
(refill) returns slots to expert duty without freeing a byte, and the pin reserves are host RAM.
Granularity: whole 64 MiB-rounded parked K/V images (often >> a 64 MiB step) and the slot pool is
engine-wide (a low card's report releases every card's slots; per-device filtering needs a core API
this lane's file scope could not add) - the donor returns the REAL MiB freed and the guard logs it
verbatim; 0 (slots off / nothing parked, e.g. STRATA_PARK_VRAM_MIB unset) is an honest 0, never faked.
N1 shape (snapshot, donate outside, no reentry): check() now decides the whole step under g_mu
(window recheck, cap, floor raise, counter, timestamp), snapshots the donor pointer + figures,
RELEASES g_mu, then calls the donor under its own g_donor_mu (the engine's recall is one unthreaded
pool: two cards stepping at once donate one after the other), then logs outside every lock. A
thread_local t_in_donor makes check() answer nothing-to-do on that thread while its donor runs, so
the donor's own probes cannot recurse (and can no longer deadlock the old single-lock shape, which a
re-entering probe would have). Hot checks (not low / windowed) never touch either mutex, so a slow
donor on card A cannot stall card B's draft-path checks.
N2: the step clamps to the cap - step = min(step_mib, base + max_growth - floor) - so a non-divisor
STRATA_VRAM_GUARD_MAX_MIB lands the floor exactly on base+max (500+100: 500->564->600, +36 on the
last step) and the cap log line stops overstating; the donor is asked for the clamped amount.
N3: kLowMib hoisted to include/strata/core/vram_guard.hpp (inline constexpr, 512 / 1024 _WIN32);
generate.cpp's kVramSafeMib is now an alias onto it (generate.cpp:2002), one definition, no drift.
N4: the test's set_reclaim moved BEFORE case 0 (the "off: the donor is never asked" assert can fail
now), and a first-in-main env case verifies the activation path (setenv STRATA_VRAM_GUARD=1 +
STEP_MIB=128 before the guard's read-once config initializes: enabled() true, floor 100->228, "no
reclaim donor registered" logged). New cases: the N2 non-divisor clamp (564->600, donor asked 64
then 36) and the N1 no-reentry contract (the fake donor re-enters check() from inside itself on
card 4: check answers nothing-to-do, exactly one step, one donor call - under the old shape this was
a recursive g_mu deadlock). The env case caught nothing, the N2 case caught a missing configure() in
the test itself (first run: 3 FAILs, all my test bug, fixed).
Build+test evidence (build-mixed-gpu/rpcw-hip-guard, CUDA-ON/HIP-OFF, untouched by this lane):
`nice make -j6 strata vram_guard_test` -> exit 0, [100%] Built target strata, binary 44,128,744 B @
2026-11-14 11:48:51; vram_guard_test 27,088 B. `ctest -R vram_guard_test` -> 1/1 Passed (0.00 s)
after the test fix (first run 0/1, the missing configure above). Zero warnings on the new lines
(build-18p-donor.log: the 338 warnings are the pre-existing HIP nodiscard/unused noise, line-shifted
only). expert_source.* untouched (the 18n-lane gate landed separately as c873503f0c mid-round);
rpcw-hip not built; the HIP engine build stays for wrap-up. Not flipped to prod (guard default OFF).
Skipped N5/N6 (the review marked them low/trivial; not in this lane's mandate).

- Addendum (staging review): the 18q premise "strictly less concurrency than today's multi-dispatch-thread
  inline staging" is wrong as stated — serve is serial (one drive.d.fs). Safety holds via the lookahead
  thread's PRE-EXISTING concurrent ring_release plus the mutex-guarded ring; that is the relied-on argument.
  Also: the ON path has zero host-test coverage — the ARM7 arm is the stager's first-ever execution; its
  pass criteria are stage_drops/ring_busy report lines + the temp-0 sha guard, and any sha delta vs
  knob-off is a defect (rows shifting helper/CPU->GPU-slot must be bitwise identical), not noise.

**Round 18r — GPU R1: first residency flip fails on request-time VRAM; reserve 2304 retry.**
First flip on the final all-lanes binary (sha 4f748b9a): register-last architecture VERIFIED —
banners in order, whole-arena registration ok in 185.8 s (46.8 GiB, no hang, no spin — the round-2
fix works). But the FIRST request died fatally: cudaMalloc(1538035200) for the weight arena failed
(526 MiB free post-boot): the full 46.8 GiB registration costs more GPUVM than relief's partial
complement, and the request-time 1.47 GiB weight arena exceeds the +256 MiB after-cache calibration.
Engine exits fatally on this OOM -> supervisor restarts (and the script's restore bugs — smoke before
READY, cp-before-stop — collided supervisors; config/binary state re-derived). Fix attempt: reserves
576 -> 2304/2304 (post-boot free ~2.25 GiB > 1.5 GiB arena + margin; cache slots shrink ~1.7 GiB),
round1 script gates fixed (registration-ok banner matched, smoke after registration, restore stops
first). Retry running. Note: if long sessions still squeeze at 2304, the adaptive answer is the VRAM
guard with park-vram measured in, not another static bump.

**Round 18s — GPU R1 attempt 4: request-time ROCclr spin over the full-span registration.**
Reserve 2304 confirmed at boot (2488 MiB free post-load; weight-arena OOM class resolved) and
registration completed (139.7 s) — but the FIRST REQUEST wedged the engine: 100.0% of one core,
log frozen immediately after "registration ok", request hung (curl empty), 46.2 GiB RSS resident.
This is the ROCclr spin signature, moved from boot-time (rounds 18k/18l, fixed by register-last)
to REQUEST-TIME: the first request's driver sync spins over the live 12.3M-page whole-span
registration. Relief never hits this because its registered complement is smaller / differently
shaped. Prod restored to relief (2304-config reverted with the round1 restore path). Next lane:
differential of register_whole's single {0, capacity} range vs the relief path's
register_complement range construction (count, granularity, chunking, flags) — make the full-span
registration take the proven shape (per-2-MiB-page / per-layer ranges with progress prints).

**Round 18t — register-last takes the relief range shape: the whole arena registered range-by-range (v0233-candidate @ fc5f89cc3d).**
The 18s differential, answered by reading (case: relief DOES chunk):
- Relief: register_complement (expert_source.cpp:6910-6952, pre-change line numbers) builds comp =
  MAXIMAL SPANS OF CONSECUTIVE STILL-HELD BLOBS - a boundary only where a DROPPED blob starts - and
  register_after_fill (pinned.cu:1030-1099) issues ONE cudaHostRegister(PORTABLE|MAPPED per
  pinned.cu:723-724) PER SPAN, page-outward rounded (V4-r3). The measured relief boot (18k): 21.5 GiB
  / 3440 ranges = ~6.4 MiB per call, requests served fine over it indefinitely, including syncs over
  the live registration.
- Register-last (18n @ 23fb6b114b): register_whole passed ONE range {0, arena_capacity_} - ONE
  cudaHostRegister over 46.84 GiB = 12.3M 4-KiB pages. Boot ok in 139-186 s, but the FIRST request
  wedged (18r/18s: 100% of one core, log frozen right after "registration ok", empty reply, 2x).
So the differential is per-call page-table scale (plus one lazy GPUVM fault wave over the whole
span on first touch), not the registration's existence: relief's ~6.4 MiB/call shape is the proven
request-time-safe point at 3440 ranges.
Fix (expert_source.cpp register_whole only + a print in pinned.cu): the full span takes the same
construction - ranges of consecutive blobs over [0, arena_capacity_], each span closed at the first
PAGE-ALIGNED blob offset once it reaches >= 8 MiB (the complement's measured band), the last span
through arena_capacity_ (the complement's last-span rule, spare blob included). Page-aligned close
is the load-bearing detail: relief's boundaries never cut a HELD blob (they are dropped-blob
offsets); with nothing dropped, a non-aligned boundary hands the shared page to the earlier range
(register_after_fill's head-trim) and the blob starting mid-page straddles two registrations -
pin_range_of answers one range or none (V4-r3) and the first DMA of that pair dies "invalid
argument". A layout with no interior page-aligned blob offset falls back to the 18n single range
(loud: the ok line prints 1 range). ~5,900 ranges at 8 MiB for 46.84 GiB. Refusal semantics kept:
lock_rest=true - first refusal stops (pin-by-need's rule), the unregistered rest keeps the
working-set lock (UMA degrade, non-fatal, loud), and ranges registered before a refusal STAY
DMA-able (strictly better than 18n, where any refusal left nothing registered). Architecture kept:
last boot step, generate.cpp call sites (:16933/:23745) unedited, STRATA_REGISTER_LAST=0 opt-out,
Linux gate (c873503f0c), no-ops for every other pinning mode, once-only idempotent.
Prints: register_after_fill's per-~10% progress line now carries elapsed seconds ("registered
after the fill: N of M ranges (X GiB, Y s)"); the banner pair is unchanged except the ok line gains
the count: "... ok in N s (X GiB registered, R range(s))".
Expected request-time effect: each cudaHostRegister maps ~2k 4-KiB pages instead of 12.3M, and the
first request's kernel submission / stream sync / weight-arena cudaMalloc meets the relief-proven
per-range table (3440-range complement serving fine is the existence proof at 21.5 GiB). NOT
addressed here: 18r's GPUVM cost of the full-span page tables (2304/2304 reserves stay required).
Build evidence: build-mixed-gpu/rpcw-hip, `nice make -j6 strata 2>&1 | tee
../build-18t-relief-shape.log; PIPESTATUS[0]` -> exit 0, [100%] Built target strata; binary
50,020,744 B @ 2026-11-14 13:14:14 (was 50,019,320 B @ 12:18:12, the 18p rebuild); zero warnings
on the edited lines (the 43 are the pre-existing HIP nodiscard noise).
Host tests: `ctest -R 'arena_pin_ranges_test|file_expert_source_test'` -> arena_pin_ranges_test
1/1 Passed (0.01 s, post-change). file_expert_source_test TIMED OUT (its 60 s CMake TIMEOUT, zero
output) - PRE-EXISTING on this box state, proven by differential: change stashed, both TUs rebuilt
(exit 0), the pre-change binary hangs identically (RC=124 before any output, even under stdbuf
-o0); strace shows the block inside ROCclr startup's DRM enumeration (the /dev/dri/renderD130 walk,
last syscall a never-returning close on a DRM fd) while prod serves on the relief cards - the test
links strata_engine, whose HIP runtime init probes the render nodes the live server holds. None of
the test's cases reach register_whole/register_after_fill (its pacing cases parse canned meminfo
strings, tests/core/file_expert_source_test.cpp:404-419). Re-run when prod is idle; not a
regression of this round. No GPU in this lane - the parent validates: expected boot "registering
the whole arena (46.8 GiB) as the last boot step ..." -> per-10% range progress (GiB, s) -> "...
ok in N s (46.8 GiB registered, ~5900 range(s))" -> READY -> first request completes (temp-0 sha
guard convention). Watch: the printed range count (a small count means sparse page-aligned blob
offsets - the shape degraded toward 18n and the spin may persist); registration wall time may grow
past 186 s (per-call overhead x ~5900, the 1200 s launcher deadline has margin); GPUVM/VRAM figures
unchanged vs 18r/18s (2304/2304 reserves).

**Round 18u — GPU R1 attempts 9-11: infra closed; the 900s ENGINE_READY kill; range registration works.**
Attempt 9: idempotent-commit abort (config already committed by attempt 8) — fixed (|| true).
Attempt 10 (post-reboot, cold): the range-shape registration RAN CLEAN — banner "registering the whole
arena", 4608 ranges, 10% progress lines, 42.7 of 46.8 GiB at 205 s — but the boot died at ~917 s:
serve/server.py's ENGINE_READY_S default 900 kills the engine before READY on a cold boot (fill +
tail phases + ~4 min registration > 15 min); the server exit cascaded helper SIGTERM + supervisor
rebuild. Fix: launcher exports STRATA_ENGINE_READY_S=2400 (70660bf6c6); round1 loop widened to 40 min.
Attempt 11 running with all infra fixes. Flip-script infra bugs closed this session: env pass-through,
fail markers, smoke-before-READY, straggler races, set-e pkill, rename-installs, idempotent commits,
orphan gate, range-count gate, server patience.

**Round 18v — GPU R1 attempt 12: range shape does NOT cure the spin; the limit is total pinned pages.**
Attempt 12 (4608-range chunked registration): boot clean, registration ok 288.4 s, engine REACHED
READY ("ready: http://..."), then the first smoke request WEDGED the first forward pass: curl quit
at 240 s, the server sent STOP, the engine never honored it, server SIGKILLed (-9) after 304 s and
rebuilt; retry attempts 2-20 hit the same wedge/kill cycle. Conclusion: the ROClr spin scales with
TOTAL live registered pages (full arena 12.3M 4-KiB pages spins; relief's ~5.6M complement serves) —
chunking does not help. Full-span DMA registration is a driver wall on this stack.
Pivot (config, no code): STRATA_ARENA_PIN_GIB — the arena stays 46.8 GiB resident (operator policy),
but only a ~22 GiB profile-hot prefix takes DMA registration (relief's proven page scale); the tail
stays locked + staged (RAM-speed, no disk). Next boot bisects the cap upward (22 -> 26 -> 30 ...) to
find the largest request-safe registration; PP measured at the winner.

## Round 18w — kernel 7.3.1 discriminator + mlock leg (2026-11-14 ~16:00-16:35)
- Operator rebooted to kernel 7.3.1-x64v3-xanmod1 (archaeology lane: full residency last served on <=7.3.1). sysctl vm.compact_unevictable_allowed=0 set; ulimit -l unlimited.
- Engine lane: commit 660d9f0dc7 (STRATA_ARENA_MLOCK, reviewer PASS-NOTES; binary strata-mlock-660d9f0dc sha f094ddcb) pins registered arena spans post-cudaHostRegister (10 sites, knob-gated).
- R1 boot (IQ3_S full residency + mlock): host-callback first, loaded 46.84 GiB @1.85 GiB/s, expert cache 2916 slots (9.73 GiB free, 2304 reserved), register-last whole-arena registration ok in 563.2 s (4608 ranges), arena mlock: RLIMIT unlimited, locked 46.84 GiB over 4608 spans in 128.0 s. NO spin. RAM 54/62 with swap-thrash during load (si/so ~160 MB/s; engine write_bytes 0 — writes were kernel swap-out, not the engine). Killed at operator request before smoke verdict.
- Verdict: 12.3M-page registration + first-touch survived on 7.3.1+mlock; the 18v request-time spin did not appear.

## Round 18x — XXS full residency WITHOUT mlock on 7.3.1 (~16:40-16:47)
- ud-iq3_xxs full-residency boot (relief dropped, reserves 2304, mlock off, commit e45b227333): registration progressed 4480 ranges (~47 GiB XXS arena) and completed, then the request path stalled (operator observed) — the 18v signature, on 7.3.1, without mlock. Killed.
- Verdict: kernel 7.3.1 ALONE does not restore full residency; mlock is the load-bearing fix (invalidation hypothesis from the ROCm research lane holds). Full-residency+mlock for XXS remains the untried fast path.

## Round 18y — XXS relief champion: boot, bench, promotion (~16:48-17:05)
- Config restored to champion relief shape (f13c4ce81b: relief args back, reserves 576, mlock off, dedicated elog logs/engine-prod-ud-iq3xxs-v0233.log; launcher fixes c770aa7411 sockets + a9eac52642 helper pack iq3xxs-ud/XXS-shard1/fill 6796; clone base bb0ec893f3).
- Boot: ready 16:48 (server log 'ready: http://'; /v1/models NOT readiness — burned twice tonight), smoke attempt 1 OK.
- Bench (ab9_driver, temp 0 + sha, ctx 442400 ~= 79989 tokens):
  - fill fresh-80k: 516.3 tok/s PP (0 reused). IQ3_S relief champion reference: 578.7 fresh-180k.
  - tg run 1 (65536 reused + 14453 re-read @272.5 tok/s = 53 s turnaround): 10.2 tok/s TG, drafts 261/409 (64%).
  - tg run 2 cached (79984 reused, 236 ms): 29.1 tok/s TG, drafts 273/404 (68%), helper reqs 4967/leg.
- Interpretation: XXS warm TG is champion-class (29.0-31.5 band); the cold path (partial cache reuse + expert streaming) is the deficit. 65536-of-79989 reuse boundary is a cache-chunking artifact worth a knob look.
- PROMOTED to prod. restore-prod.sh default is now xxs relief (8a9336a3a6). Prod serving ud-iq3_xxs on :8009.
- Spec sweep (spec x min-p) + PP/turnaround/TG lever analysis delegated (single agent, protocol arms spec4/0.5 spec4/0.65 spec5/0.65 spec3/0.5, champion restore mandatory) — results in next round.

## Round 18z — XXS spec sweep: all arms invalidated by stack-wide temp-0 nondeterminism (2026-11-14 ~17:00-17:50)
- 4 arms (spec4/0.5, spec4/0.65, spec5/0.65, spec3/0.5), 5 boots, protocol per notes/2026-11-14-xxs-spec-sweep-results.md (tools: spec_sweep_arm.sh, restore_boot.sh).
- EVERY arm failed the within-arm temp-0 sha guard: same prompt yields 334-680 tokens at temperature 0 across runs. Nondeterminism is stack-wide, not spec-knob-specific. NOTE: the earlier 18y baseline also shows it (411 vs 422 tokens within one leg invocation).
- Provisional (invalid) leader spec4-p0.5: warm TG 32.9-34.5 vs champion 29.1 (+~4); PP unchanged by spec knobs (512.3-538.8 vs 516.3). Highest acceptance (79-86% at p0.65) gave the WORST warm TG — per-step overhead dominates; do not chase acceptance via min-p.
- Determinism suspect #1: the upstream-verified MMQ row-id OOB read in our exact gfx906 prefill path (gemm.cu:1282-1296, STRATA_DENSE_MMQ=1; upstream fix family f225844d19/14dd746f6a/697b6d67a7, zero-conflict). Priority of upstream wave-1 raised from hygiene to correctness blocker for all spec tuning.
- Turnaround lever located: judge lines checkpoint at 16k multiples and cross-prompt restore floors to 65536 of 79989 tokens; --prefill 16384 is the chunking knob (8192 => boundary 73728, ~halves the 51.5-55 s re-prefill; one-boot candidate). Re-read runs ~280 tok/s vs 539 fresh (swap-in pre-staging: SWAPIN_DEPTH/FS_SLOTS).
- Other ranked levers recorded in the notes file (PP: reserve 576->832 per engine #2065 warning vs operator occupancy directive — settled empirically next round; WMMA port #4859. TG: FS slot pressure 'no free slot' 8,630->36,607 on long generations; KV 4096 MiB resident vs 1.8 GiB at 80k).
- Champion (spec5-p0.5) restored at 709f170c0f, prod UP 17:47:11 (ready line, completion answered).
- Round 18aa (running): --vram-reserve-mib descent 512/448/384/320 with break detection; prod ends on the measured last-good edge.

## Round 18ab — helper fill to budget (win) + turnaround levers falsified + revert (2026-11-14 ~18:30-19:00)
- Reserve descent (agent, halted at operator floor 384): arms 512/448/384 all served (tg jsons XXS-resv{512,448,384}-tg.json); 320 arm committed then cancelled per operator - not measured. Prod reserve = 384 (edge = operator floor, not a break).
- Helper fill-mib 6796 -> 7100 (7e04e2b893): fill now stops at BUDGET not the cap - 3630 slots / 6.80 GiB vs 3546 / 6.63 GiB (+84 slots, +180 MiB actually used). Stale era cap had left it behind. Keep.
- Turnaround arm (prefill 8192 + conv-cache-ram 2048 + SWAPIN_DEPTH 128, 05e8d182a0) - ALL FALSIFIED (judge lines, elog 18:5x):
  - boundary UNMOVED: 65536 reused + 14453 re-read (the --prefill grid does not control the cross-prompt restore floor; spec-sweep hypothesis wrong - boundary lives in conversation/prompt-cache restore logic)
  - re-read SLOWER: 211.8 tok/s / 68 s (vs 272.5 / 53 s) - SWAPIN_DEPTH 128 worse than 64
  - warm TG 22.5-27.3 (vs 29.1), helper reqs UP 6085-7460 (vs ~4967)
  - NEW artifact: "79984 reused + 5 read in 15102 ms (0.3 tok/s)" - 15 s serve stall with conv-cache 2048
- Reverted to champion knobs (prefill 16384, conv 512, SWAPIN 64; 82c7d320dd), reserve 384 + fill 7100 retained. Prod rebooted on the known-good shape.
- gfx906 INT8-dot prompt port: stage 1 (upstream MMQ row-id fix family, determinism suspect #1) cherry-pick lane dispatched; port planning next.

## Round 18ac — 2026-11-14 evening: XXS layer-split sweep (36/34/32/30/28) — champion 30

Method: `gpu-rounds/split_arm.sh N` (sets `layer_split` config field — NOT an args flag), one boot per arm, fill80k + tg80k×2 via ab9_driver, judges from engine elog. Spec 5 throughout; reserve 384; fill-mib 7100.

| split | c0/c1 | slots (final) | fill 1st-80k | TG tok/s | verdict |
|---|---|---|---|---|---|
| 36 | 36/12 | 3410 | 186.0s / 430.0 | 29.1/26.7/29.4 | falsified: worst slots, TG flat |
| 34 | 34/14 | 3630 | 186.7s / 428.4 | 29.1 | old champion (IQ3_S-era carryover) |
| 32 | 32/16 | 3528 | 192.6s / 415.3 | 27.7–30.7 | balance but no ceiling gain |
| **30** | 30/18 | 4885/3985 | **183.1s / 436.8** | **29.4/32.2/32.7 + repro 18.8/31.2/31.0** | **champion: TG +11% vs 34, PP +2%** |
| 28 | 28/20 | 3410 | 216.1s / 370.1 | 28.2/24.3/**15.7** | cliff: binding card flipped, thrash tail |

Mechanism: TG is a serial pipeline; heavier card gates the token loop (30/18 = 1.67× vs 34/14 = 2.43×). Below 30 the mirrored-min sizing flips to card1 and slot collapse (3410) triggers expert thrash on long generations. Fill is split-insensitive in 32–36; 28 collapses on both. Boot-to-boot variance in card0 free ≈ ±300 MiB (±90 slots) observed at split 30 (3531 vs 3649 auto-line draws) — slot deltas <100 are noise. Config committed at `layer_split: 30`.

## Round 18ad — MMQ revelation + A/B: path was NEVER off; pinned OFF after OFF wins both metrics

Oracle review (GO-WITH-CHANGES) of the "gfx906 INT8-dot port plan" found: `STRATA_PREFILL_MMQ` CMake option is INERT on gfx906 (CUDA branch compiles the path unconditionally, CMakeLists ~2947); runtime default is ON (env unset → on); the 2026-10-24 fork-policy gate (b7383aae) is not an ancestor of HEAD — policy silently eroded. Verified against prod pin sha f094ddcb: 70 `prefill::mmq` symbols present, no env pin → **prod had been running MMQ all along; every earlier number tonight is MMQ-ON data**.

A/B (same binary, env-only, back-to-back boots, split 30): `gpu-rounds/mmq_ab.sh`:

| arm | PP fill | TG (3 judges) | cache final |
|---|---|---|---|
| STRATA_PREFILL_MMQ=0 | **379.0** | **29.5/33.9/31.8 (med 31.8)** | 4885/3985 |
| STRATA_PREFILL_MMQ=1 | 354.1 | 28.4/14.5/31.6 (med 28.4) | 4885/3985 |

OFF +7% PP, +12% TG median, ON threw a 14.5 thrash tail. Both arms' fills ~15% below the split window (suspected residue from 3 crashed device-init test processes just before) — within-window pair valid. Cache sizing is NOT mode-dependent (earlier "OFF frees slots" read was a runbook grep artifact: the 3649 `auto` lines are an early pass; final 4885/3985 identical in both arms — mirroring is sizing-symmetric, per-card content).

Verdict: **STRATA_PREFILL_MMQ=0 pinned in prod config** (commit; review's recommendation to pin explicitly). Tile-tuning upside now bounded by the A/B gap (≤7–12%) with generic GCN tiles losing to dequant→FP16→rocBLAS end-to-end on Vega 20. MMQ test binaries in fresh `rpcw-hip-mmq` dir (built with -DSTRATA_BUILD_TESTS=ON; golden freezes tests OFF) crash `free(): invalid pointer` past device init — instrument bug (suspected toolchain/lib mismatch), serving unaffected, parked. Engine audit line `prefill mmq: on/off` landed (ad115336b4, v0233-candidate); golden truth fix 87dd02ca2a + runbook da00d59fe2 on main.

## Round 18ae — FS_SLOTS rebalance 3,2 → 2,8 (geometry was stale)

`STRATA_FS_SLOTS` is absolute per-card slot counts (0–64 clamp; `expert_source.cpp:7967+`), slot size ∝ card's layer count (~59/~35 MiB at split 30). Old 3,2 spent ~200 MiB on tight card0 / ~55 MiB on card1 — tuned for a card1-thin era. Rebalanced 2,8 ≈ 118 MiB card0 / ~283 MiB card1. Banner confirms "card0:2, card1:8"; card1 now 97.8% used (slack spent).

Result (split 30 + MMQ 0 + FS 2,8): **fill 179.5s → 445.5 PP tok/s — night's best**; TG 28.1/31.7/30.5 (med 30.5, noise-neutral vs 31.8). FS-alone attribution not isolated (plain-MMQOFF fill baseline was in the contaminated window); reserve-256 arm provides a second sample of this stack. Helper (3080): brimmed to budget floor — post-fill 506 MiB free = 320 reserve + 180 request buffers + rounding; NO idle MiB (earlier +95-slot claim was a buffers misread; corrected).

## Round 18af — reserve descent restart: 256 first (operator), 16-MiB steps only on stall class

Operator order after 18ac/18ae: first test cuts reserve 384→**256** (the historical fatal value, era pre-mlock/pre-split-30/pre-MMQ-off — conditions changed, retest justified). Policy: if 256 serves clean → done; only if the verify-timeout/stall class reappears → descend in 16-MiB increments from 384 (368/352/336/…). Safety rig: shrink/LOW/reserve-adaptation line capture, light-traffic survival window before calling it servable, one-commit revert to 384 staged. `gpu-rounds/reserve_arm.sh 256` chained behind the FS arm via log-marker wait (pgrep waits retired — launch-plumbing forks/zombies deadlocked queues 3× tonight; killed harmlessly each time).

## Round 18ag — reserve 256 survived; fs2 adaptive guard: no headroom left

RSV256 arm (split 30, MMQ 0, FS 2,8, reserve 256): boot clean, no shrink/LOW/verify lines. Card0 cache 4885→**4951 slots** (+66 for the 128 MiB), card1 3985 unchanged; card0 post-fill 99.1% used. TG 29.9/29.1/32.3 — noise-neutral. **No stall class → per operator policy the descent stops: 256 is the floor** (the historical 256 fatal was pre-mlock/pre-split-30/pre-MMQ-off era; conditions changed and it now serves). Light-traffic survival watch run post-arm before declaring servable.

fs2 adaptive chain (push card1 FS to a 180 MiB floor, cap +4): measured card1 free at this config = **209 MiB → 0 extra slots → kept 2,8**. Nothing left to push on card1.

Fill-wall bimodality noted: boots of near-identical configs land in a fast class (179-187 s) or slow class (211-226 s; RSV256 = 354.2 tok/s). Cross-boot PP claims beyond controlled back-to-back pairs are unreliable tonight; the controlled pair (MMQOFF vs MMQON) stands: OFF +7% PP, +12% TG med.

Serving config after tonight (committed): layer_split 30, STRATA_PREFILL_MMQ=0, STRATA_FS_SLOTS=2,8, --vram-reserve-mib 256, spec 5, fill-mib 7100. Caches 4951/3985 + helper 3628 (brimmed to budget floor). Engine pin unchanged (f094ddcb mlock build); mmqfix-86ab69094f pin pending (crash-insurance only, MMQ now runtime-off).

## Round 18ah — pin 2e433a82 serving; helper 220; FS 2,9 falsified; 192 passes legs; repro verdict

**Pin swap landed.** Golden rebuild `build_pin.sh` (ctest-free) from ad115336b4-lineage golden config produced sha16 `2e433a826404305f`; two guard bugs fixed en route (unquoted `;` in CMAKE_PREFIX_PATH array; pipefail+`grep -q` SIGPIPE false negative). First swap attempt hit ETXTBSY (cp before stop-all) — redone stop→cp→boot; old f094ddcb kept beside it as `strata.bak-f094ddcb`. Validation boot: identity line `prefill mmq: off (env=0) [built=1]` ✓, TG 31.9/33.6/30.8, fill 192.3 s / 416.0 PP tok/s. Pin is behavior-identical to the validated stack plus the audit line — mlock, mmq-fix, CMake fix all in.

**Helper reserve 320→220** (operator: 240 then 220): helper fill 3628→3676 slots. Helper warns 220 < its recommended 256; failure mode is soft (VIAs cover on OOM). Watching for overnight cleanliness before calling it final.

**FS 2,9 falsified — reverted.** The +1 card1 slot (13 MiB margin vs the ~162 MiB staging-ring floor) threw exactly the guarded signature: one TG leg collapsed to **12.2 tok/s** (442 tok in 36 s) plus a 535-token re-read taking 10.3 s at 51.5 tok/s. Every 2,8 boot tonight (12 judges) stayed ≥28. card1's "no free slot" contention is real (~35k counts) but not worth a thrash class. Revert commit f000470316; final FS = **2,8**.

**Temp-0 SHA repro: 3/3 diverged** on the new pin (a3114d60 / 02c34262 / 89a35b91; 206/194/198 words, all finish=length). Nondeterminism persists with MMQ off and the new binary — not pin-introduced, not MMQ-introduced; root cause remains open. Characterization only, as planned.

**Reserve 256→192 (operator push, +64 MiB past the fresh floor).** Arm ran clean before the box reboot interrupted the night: TG judges **32.0/28.7/29.4**, no shrink/LOW/stall lines, no thrash class. 192 passes its legs — 240k fill + TG×1024 stress and a survival window still owed before it's called servable (that leg was killed mid-fill by the reboot and is being re-run).

**Teacher-forced quality run crashed at engine start (exit -6, `free(): invalid pointer`) — spawn-path, not binary.** Same signature as the MMQ test-binary crashes, but this binary serves perfectly under the prod launcher. Bare subprocesses (tool, ctest) resolve a different system ROCm than the launcher's pinned `LD_LIBRARY_PATH` (gfx906-xnack + /opt/rocm-6.1.0); wrapper `quality/strata-ldp.sh` now gives the tool's engine the launcher env. Retry chained; if it still aborts with the launcher env, it's a minimal-config-path heap bug → oracle. Historical A-vs-C reproduction dropped (C.tsv.seg.json not retained).

**Box reboot ~21:43** killed the 240k fill mid-leg and all chains; resume lane (boot at 192 cfg → 240k stress → quality retry → restore) dispatched from committed state. rsv192 arm had already COMPLETEd at 21:33:08 — its verdict stands.

## Round 18ai — 240k stress passes on reserve-192: the pushed stack is max-context-clean

Two box reboots (~21:43, ~21:53) killed the first attempts mid-fill; lane v3 (port-probe boot gate — stale-log ready lines had let a leg fire against a booting server) rerun clean on the 192 boot:

- **Fill 239,782 tokens**: cold-read boundary 10,406 read at 199.4 tok/s (52.2 s), total wall 501.9 s. The deepest prompt-path loans rode through — card0 borrows 3642 slots, card1 borrows 3384 of its 3985 (6.73 GiB each) — with zero shrink/do-not-fit/OOM/timeout lines.
- **TG×1024 on the 240k context**: 28.0 tok/s (514 gen/18.3 s) and 25.7 tok/s — vs ~30–32 at 80k, i.e. mild degradation at 3× context, no thrash class, drafts accepted ~70%.
- Boot itself: ready in 81 s (fast class; gen-1 exit was supervisor-recovered).

**Reserve 192 survives 92% of max context.** Combined with the clean 192 arm legs (18ah), the served stack — pin 2e433a82, split 30, MMQ 0, FS 2,8, reserve 192, helper 220 — is stress-validated; a light-traffic survival window remains the last formality before calling 192 servable.

Quality-tool notes settled en route: the teacher-forced abort (`free(): invalid pointer`, exit -6) was **library resolution, not the binary** — under the launcher's LD_LIBRARY_PATH the engine reaches `session is up` (wrapper `quality/strata-ldp.sh`). The remaining exit-1 was config: `--spec` needs its qqA companions (`--spec-min-p 0.5 --expert-profile … --vram-reserve-mib 640 --vram-reserve-later-mib 640 --expert-cache-cpu-order --prompt-cache-spread`) or the verify tier refuses the minimal `--expert-cache 1` profile. qqNIGHT now mirrors qqA's pattern on new-tree paths; retry in flight.

## Round 18aj — quality gate lands; concurrent prefill answers; prefill batch knob found

**Teacher-forced quality (NIGHT.tsv, 4,995 positions, new pin + XXS + MMQ off):** vs the historical IQ3_S-era A.tsv — top1 agreement **86.81%**, KL mean **0.1488** / p99 2.171, PPL **3.924 vs 4.115** (same 999-position segments; note A is the old-tree IQ3_S run, not a Q8_0 literal despite the label). XXS pays a real distribution gap (~13% top-token divergence, heavy KL tail) yet matches-or-beats reference PPL — acceptable for the serving tier it is. En route: the tool's bare-subprocess abort was library resolution (wrapper fixes it); `--spec` needs its qqA companions under `--expert-cache 1`. Tool's "ended before READY" on slow CPU-expert boots is a ready-wait artifact, not a crash.

**Concurrent prefill (2×80k simultaneous, single-slot config): REJECTED, not queued.** First leg got `HTTP 503`, second `RemoteDisconnected` at ~96 s; no PB artifacts written, no new judges (an earlier note claiming "both completed at 2× wall" was a misread of streamed logs — retracted). A fresh engine start in the elog at 22:22:36, right after the failure window, says the concurrent request likely killed the engine generation and the supervisor recovered it (~2 s). Single-slot serving rejects rather than batches the second request — with real usage at one slot this is moot, but it is a hard failure mode, not graceful serialization: never point concurrent prefills at prod as configured.

**Prefill batch size:** prod runs `--prefill 16384` (read progress confirms: 240k read reported at exact 16k multiples). Request-level `--batch/--slots` (2–8, VRAM-per-slot) exists but is off by design here. Sweep 8192/16384/32768 queued.

## Round 18ak — prefill batch sweep: neutral; judge-rate metric discipline

`--prefill` 8192/16384/32768, back-to-back boots, fill80k + tg80k each: judge read rates **440.7 / 434.2–454.1 / 454.8 tok/s** — all one class (differences inside the boot-bimodality band). Warm TG 28.2 unchanged. Verdict: **throughput-neutral 8k–32k; keep 16384.** An earlier "8192 −20% PP" read was driver-wall overhead (231 s wall vs 181.5 s engine read), retracted — judge rates, not driver walls, are the cross-arm prefill metric. Upstream recon (473 commits ahead): cherry-pick shortlist identified (AMD driver-pause stall report — the bimodality instrument; MMQ row-id margin fixes; #4677 stop bound); DFlash2 conf/window sweep flagged as an untouched TG lever for our hardware; full-merge adoption deferred to next pin cycle.

## Round 18al — temp-0 repro problem PINNED: placement-trajectory nondeterminism

Cold-boot triple + warm repeats (identical prompt, temp 0, 512 tok): boot-1 grabbed `--prefill 32768` in a config-edit race (excluded); clean 16384 pair — cold2 `92016b74` vs cold3 `aae43346` **diverge**; then **warm3a replays cold2's sha exactly** (92016b74, 202 words) while warm3b differs. With the earlier warm triple (3 distinct shas): 7 distinct outputs from 8 generations, one exact replay.

Reading: generation walks one of a **small set of placement-dependent trajectories**. Which one depends on async expert placement (prefetch/FS admission/helper-shadow timing), not per-step randomness — the same path CAN recur (warm3a = cold2, across boots). Mechanism per the engine's own parity artifact (2026-09-27): GPU-resident vs CPU-computed experts round differently; near-tie tokens (top-2 margin < 0.5 logits, 2–5% of positions) flip with placement. Fixing determinism would need a mode that pins placement or unifies CPU/GPU numerics (the `--cpu-oracle-q8_0` "GPU expert contract" is the engine's own nod at that) — real perf cost; characterized and documented instead of chased. Upstream-worthy idea: a bit-exact GPU expert contract.

Also this round: v0233.1-candidate = v0233 + 5 upstream picks (qwen35 MMQ margin; STOP-never-raises; #4677 stop bound every backend; AMD driver-pause stall report; stream-watch) — dense+MTP margins were already in-tree (empty picks). One widowed conflict marker from resolution surgery caught by the compiler, fixed, build rerun. Open-PR shortlist for v0233.2: #3220 (HC_Q8 prefill dequant vectorize), #2955 (NaN-preserving bf16) — refs fetched.

## Round 18am — dflash draft sweep: block 7 champion; adaptive loses; B8 impossible

Six arms on pin v0233.1 (judge rates, single boot each): **B7 32.4 TG / 76.8% acc** · B5C055 31.9/71.9 · B0C055 31.6/73.1 · B5C035 31.0/76.2 · B5Cdef 29.1/72.3 · B0C035 26.3/67.6. Read: fixed 7 (the draft artifact's trained max) holds acceptance and posts best TG; conf 0.35 helps B5 (+2) but cannot reach B7; adaptive (block 0, conf-gated) loses at both thresholds — the trained pass ends windows too early (acc 67.6-73.1%). `--dflash-block 8` is refused by the engine ("exceeds the artifact's trained block 7") — the B8 arms crash-looped and were replaced by B7/B0. PP across arms = boot-class lottery (353-458, no draft effect on reads, as expected). B7 adoption pending a confirmation boot + paired B5/B7 PP check.

Also this round: v0233.2-candidate built (v0233.1 + PR #2955 NaN-preserving bf16 + PR #3220's HC_Q8 dequant vectorize/pair-fuse; the gr_upmix gfx12/SYCL/Metal chain deliberately not taken) — sha16 8e3ef28c, guards passed, pushed. Oracle review of all seven picks' conflict resolutions dispatched (gate); swap+validation follows a GO. Prod note: post-sweep the running boot held B0·0.55 args while the config file was restored to block 5 — decision boot owed regardless.

## Round 18an — v0233.2 pinned + validated; KV-on-CPU falsified; dflash knob = boot noise

**kvcpu arm (FS candidate, falsified):** `--kv-resident 40960` (KV streaming from pinned RAM, freed VRAM bought +818 expert slots to 5801/10.75 GiB) — warm TG collapsed to **12.7 tok/s** (thrash class; 631 gen in 49.8 s), acc 69%. PP unchanged (248 boundary). Per-token attention reads over PCIe cannot be paid for with expert slots at 80k depth. Reverted to 262144 (full VRAM residency).

**v0233.2 pin swap (sha16 8e3ef28c43a25f03) — VALIDATED, serving.** Full-fill judges 445.1 / 455.3 / 432.2 (v0233.1 band 434-455 → HC_Q8 prefill pick parity CONFIRMED; the one low boundary rate 234.7 was the second-order read lottery). TG parity: V232 block 5 posted **33.2 tok/s / 77.3% acc — best of night**. Oracle condition 2 discharged. CMake dangling-test-target defect fixed (fca4199639); merged to main (b88c58651f), pushed. Binary unaffected by the CMake fix (tests OFF).

**dflash-block knob: inside boot-class noise.** B7's sweep-1 win (32.4) did not reproduce on v0233.2 (28.2/27.4); block 5 itself swung 33.2 → 26.2 across two boots; acceptance swung 64-79% with no block correlation. The tg5k round (100k infill + max_tokens 5120) revealed why deep sampling was illusory: generations self-terminate naturally at ~380-480 tokens on this corpus — "1024-token" legs were never token-bound. Verdict pending T5B5: no config change justified; block 5 stays.

**HC_Q8 campaign opened (operator mandate: tune for gfx906, complete + enable).** Discovery: STRATA_HC_Q8 = hyper-connection projections stored Q8_0 (verify read), default ON only for gfx12/gfx1151 — OFF on gfx906; we run bf16 reads. Runtime env toggle (STRATA_HC_Q8=1 [+ _INJECT=1 for bf16 GGUFs]) — no rebuild needed. Arm chained: env boot + judges + greedy sha 3x + teacher-forced qqHCQ8 vs A.tsv + NIGHT.tsv (isolates knob from pin delta). Branch hc-q8-gfx906 cut from v0233.2-candidate for the kernel work.

## Round 18ao — tg5k closes the dflash question: adopt block 7; adaptive final loss

tg5k round (100k infill, max_tokens 5120 — generations self-terminate ~200-480 tok, so effectively another natural-end sample with deeper fills): T5AD (B0/0.55) 26.8 / 64.8% · T5B7 29.0 / 64.0% · T5B5 26.2 / 62.8%. Boundary-after-fill thrash pass (14.8) appears on first-gen passes across arms — phase artifact, not config.

Verdict corrected from 18an's lean: across ALL four direct block comparisons tonight B7 >= B5 (32.4>29.1 sweep1; 28.2/27.4>26.2 nightcap; 29.0>26.2 tg5k) and adaptive (B0) loses every round at both conf thresholds. Acceptance parity (63-65%) means B7's TG edge is steady, not an acceptance fluke. **Decision: --dflash-block 7** (the artifact's trained max), flipped at campaign wrap-up to avoid config churn mid-HC_Q8 arms. Block 5 remains for the HC_Q8 arms (pairs against V232 445.1/33.2 reference).

Also logged: fill100k walls ~238-242 s (~410 tok/s class at 99k) on v0233.2 — PP holds at depth.

## Round 18ap — HC_Q8 on gfx906: quality PASSES, INJECT costs perf; tuned kernel built

Enable arm (v0233.2, block 5, STRATA_HC_Q8=1 + INJECT=1): teacher-forced gate vs NIGHT (same pin, knob off): **KL 0.0191, p99 0.267, top1 agreement 95.5%, PPL 3.898 vs 3.924 (slightly better)**; vs A-ref: top1 86.73%, KL 0.1484, PPL 3.898. Quality verdict: the Q8 hyper-connection projections are quality-neutral-to-better — near-tie divergence only. Greedy sha 3/3 diverged = the known temp-0 placement characterization, unchanged.

Perf verdict (why it stays off by default): INJECT loads a Q8_0 copy BESIDE the GGUF's bf16 originals → dual-copy −1.19 GiB → expert slots 4983→4577 (card0), full-fill PP 391.5 (band floor 434 broken), warm TG 26.8 (mid-band). Structural on a bf16-projection GGUF; upstream wins only where GGUFs ship Q8 natively (gfx12/Strix).

Kernel tuning (operator mandate: complete + enable): glm5.3 fixer wrote 457a7e95d8 on hc-q8-gfx906 — uint2 single-load wide variant (4 thr/block) + ld32 one-thread-per-block variant (uint4 loads) + pair forms, STRATA_HC_Q8_TUNE gate default-ON (exact "0" = upstream path bit-identical), misaligned-global-load safety on gfx906 flat/global reasoned (Vega ISA LSB rule excludes MUBUF; LLVM D145170; GCC r16-4105). Golden build GUARD PASS sha16 5d73caa758fd0493 (rpcw-hip-hcq8tune). Oracle review dispatched (gate) — A/B TUNE=1/0 boots + teacher-forced bit-exactness vs HCQ8.tsv follow on GO.

## Round 18aq — gfx906 HC_Q8 kernel tuning COMPLETE + ENABLED; campaign wrap

Oracle conditional-GO discharged: (1) ld32 engagement proven structurally — every hc_q8 buffer + scratch is its own cudaMalloc (2 MiB driver granules), so the 16B base-alignment selector always passes and the A/B necessarily exercised the ld32 path; (2) arch-gate committed (81e023bfa0): tuned kernels default ON only under HIP (flat/global byte-addressed, no alignment trap on GCN3+), default OFF under nvcc (PTX misaligned-vector UB), env overrides both ways.

Final pin 36ecbe983837828d (rpcw-hip-hcq8tune2 = v0233.2 + 457a7e95d8 + 81e023bfa0), GUARD PASS, flipped 02:09.

**Bit-exactness PROVEN at runtime:** teacher-forced 4,995 positions, tuned vs upstream kernels: top1 100.00%, KL 0.00000, p99 0.0000, PPL 3.898 identical. The ld32/uint2 variants reproduce upstream bytes exactly — misaligned-global-load reasoning validated on hardware.

**Kernel A/B (HC_Q8 on, same night):** full-fill PP tuned 406.7 vs upstream 391.5/372.4 (+14-15 tok/s, ~3.5% — directional, B's boot looked slow-class); warm TG 28.6 vs 31.2 (inside ±3-5 boot noise). The dequant slice is real but small next to attention at 80k depth; the structural INJECT dual-copy cost (−406 slots) still nets HC_Q8 below the no-HC_Q8 band (432-455) on our bf16-projection GGUF → mode stays default-off on gfx906 (upstream parity), now with a tuned, proven-bit-exact kernel behind it.

**Final serving state:** boot C (tuned pin, block 7, no HC_Q8): warm TG **33.9 tok/s — best of the entire campaign** (377 gen), boundary read 251.7 top-of-band, full-fill 393.4 (slow-class boot). Config committed at --dflash-block 7.

Campaign totals (judge-class, this box): TG 12.7 (kvcpu, falsified) → 33.9 (final); dflash: B7 adopted; v0233.1 → v0233.2 pin swap validated; HC_Q8 fully characterized (quality-safe: KL 0.019/PPL 3.898; INJECT net-negative on bf16 GGUFs; kernel tuned + bit-exact + enabled behind env). Pushed: main + hc-q8-gfx906 + v0233.2-candidate to fork.

## Round 18ar — CORRECTION: the GGUF ships Q8_0 hc projections natively; INJECT was unnecessary

Header parse of the IQ3_XXS shards (new tool: quality/gguf_tensor_types.py; tensors live in shard 2-4, shard 1 is metadata-only) shows every hyper-connection projection — output_hc_*, blk.*.hc_attn_down/up, blk.*.hc_ffn_down/up, [10240,320]/[320,10240] — is ALREADY Q8_0 in the GGUF. This falsifies 18ap/18aq's root-cause claim that INJECT was forced by a bf16 GGUF: the arm set STRATA_HC_Q8_INJECT=1 unnecessarily, and the observed -1.19 GiB / -406 expert slots was the INJECT dual-copy artifact, not a structural property of native HC_Q8.

Corrected model of the mode: OFF = dequant hc projections to bf16 at load (2x VRAM, zero per-use cost); ON-native = keep the GGUF's own Q8_0 bytes (half VRAM -> MORE expert slots) paying per-use dequant — exactly the kernel tuned and proven bit-exact in 18aq. Native arm launched (HC_Q8=1 only, no INJECT, tuned pin 36ecbe98): slots banner + judges + sha 3x + teacher-forced qqHCQ8N vs A/HCQ8/NIGHT. If slots rise and judges hold, HC_Q8=1 becomes the serving default with the tuned kernel load-bearing.

## Round 18as — investigation: HC_Q8 slot cost = multi-GPU swap bug; #267 = known wedge

Explorer (glm5.3) mapped the full hc lifecycle with file:line evidence:

**Slot ledger reconciled.** OFF-mode hc bf16 lives fully resident in per-device pack arenas (CUDA0 untrimmed = all 48 layers, 1212.5 MiB; CUDA1 trimmed to layers 30-47, ~450 MiB). ON-mode ADDS per-device Q8 copies of the FULL 194-matrix set (643.1 MiB each, the upload loop at native_dense.cpp:430-455 has no layer-range filter) because add_hc_skip — the code that drops the redundant bf16 rows — is gated !multi_gpu (generate.cpp:5662) and never runs on our 2-GPU split. Payload delta 1311 MiB ≈ observed −1.19 GiB/−406 slots. INJECT differs from native by 96 inject matrices × 43,520 B = 3.98 MiB/device ≈ 2 slots — explaining the identical 4577/8.48 banners. On SINGLE-GPU the swap works (harness measured the ~0.5 GiB saving) — why upstream defaults ON for single-GPU SKUs.

**#267 stall attribution: known gfx906 lost-completion wedge** (README.md:114, docs/AMD_HIP.md:375, TROUBLESHOOTING.md:344; layer 18 twice on record in serve-suite.txt evidence; "also on old stock", ~1/20 long drives). The dying path reads hc_q8 directly and never touches the scratch/event machinery; native-vs-INJECT differ at runtime by one plain device-read branch; alias/event-deadlock hypotheses ruled out. n=1 coincidence — re-run arm 3x when GPU work resumes to confirm no correlation.

**Fix dispatched (fixer, glm5.3):** multi-GPU skip (bf16 blk.* hc down/up rows dropped when the device's table has hc_q8 coverage; inject + output_hc_* stay) + per-device Q8 range-trim (CUDA1 uploads only layers 30-47), both behind STRATA_HC_Q8_TRIM (default on). Projected: ON flips to net WIN ~+300 slots vs OFF.

**Ticket candidates (not fixed):** decode cls=1 path takes no event wait (native_dense.cpp:577 gates waits to cls 0) — value race, not hang, if decode stream differs from last prompt reader. Prefill Q8-fused T-template extension of gr_down_q8/gr_up_q8 (kill per-use dequant+bf16 scratch; MMQ shape-ineligible K=10240>8192/K=320%256!=0, MMVQ decode-shaped). The recurring slow boundary-read classes likely = --ple-io direct 54 MB/s layer-1 tax (warning at every boot, refs #1425/#1629).

GPU work HELD per operator (reserve64 lane staged unlaunched; native re-run 3x queued).

## Round 18at — reserve floor found: ROCm 128 MiB breaks generation (NaN #879); 192 restored

Operator-ordered bisect with live GPU/RAM/read sampling per boot:
- 192/220 (control): clean — GPU1 pegged 100%, read on pace, RAM ~47 GiB fill-phase staging arena (proven normal: frees on shutdown).
- 128/220: READ FINE — fastest of the day, 473.7 tok/s / 168.9 s — but GENERATION produced NaN: "503: output became non-finite (NaN) twice in a row (#879); the engine has dropped the model". The request retried (second full 80k read, 452.2 tok/s), NaN'd again, 503. The 12:12 "wedge" (128/156 leg) is now fully explained: same NaN-drop + retry churn, not a read stall; the "reading the prompt: 330 s" tail was the retry's read.
- Bisect conclusion: the ROCm card reserve cut alone (192->128, 154 MiB free) is the trigger; helper cut exonerated by isolation (220 held constant). Prefill tolerates the tightness; generation does not.

Engineering notes: (1) the engine's below-512-MiB request guard warns but proceeds — and the failure mode is SILENT CORRUPTION (NaNs from presumably failed/uninitialized buffer use), not a clean allocation error. Upstream-worthy bug report: #879's guard catches it late; the alloc path should fail loudly. (2) The rocm64b deeper cut (128->64) was auto-blocked by the lane's cleanliness gate — gate worked as designed. (3) RAM observations: the ~46 GiB anonymous arena during fresh fills is structural staging (prompt-cache-spread + expert staging), freed at shutdown; the transient swap-out storms (peak 415 MB/s) are fill-phase collateral, not pathological.

Serving state after restore: 192/220, 4983 slots / 220 MiB free, generation verified clean (finish=stop). Config committed. The hcq8trim fix binary (b2ca53c0) remains staged-unvalidated for the next window.

## Round 18au — helper reserve 156 validated in isolation; +20 helper slots

Helper-only cut (launcher --reserve-mib 220->156, ROCm held at its 192 floor): boot clean (ROCm side unchanged 4983/9.23 GiB/220 MiB), fill GPU-hot normal class, TG leg generated cleanly, explicit no-NaN check passed (finish=stop), NaN/503 scan empty. Helper fill: 3683 -> 3703 slots (+20, ~3.3 MiB/slot — exact arithmetic), 368 MiB free after fill; helper warns 156 < recommended 256. TG judges mid-band (warm 25.8, acc 71.8%; boundary 10.3 = known slow first-gen phase class). Net: helper 156 ADOPTED — the NaN disease was exclusively the ROCm card's 128 (154 MiB free, #879), not the helper's.

## Round 18av — helper 92 falsified (no gain); final reserves 192/156

Second helper cut (156->92, ROCm 192): clean boot/fill/TG/no-NaN — but helper fill identical to 156 (3703 slots, 368 MiB free after). The helper is assignment-bound at 3703 (its full share of the expert split), no longer capacity-bound; the extra 64 MiB bought nothing. Reverted to 156 (the last level that bought slots, +20 vs 220). Final reserve state: ROCm 192 (floor; 128 = NaN #879), helper 156 (assignment floor).

## Round 18aw — trim fix WORKS on VRAM (+210 slots); native HC_Q8 path = 4/4 harness poison

Fix binary b2ca53c0 (multi-GPU swap + per-device trim, STRATA_HC_Q8_TRIM default on) booted with HC_Q8=1: banner **5193 experts / 9.62 GiB — the swap works** (OFF 4983/9.67, broken-pre-fix 4577/8.48). Judges: fill 391.2 (HC_Q8-on PP tax persists), warm TG 27.8, acc 64.1%; shas diverged (known placement-trajectory).

Quality gate: native HC_Q8 failed the teacher-forced harness **4/4 attempts** (#267 verify-timeout at varying layers: 18 pre-fix this morning, then 0 / None / 27 today; one full segment passes sometimes before the wedge) vs INJECT **3/3 clean passes** (HCQ8, HCQ8T full runs + segments). Isolation: tuned kernels (HCQ8T pass) and trim fix (pending discriminator) are exonerated-or-pending; the NATIVE upload loop (cudaMemcpy of the GGUF's own Q8_0 bytes into hc_q8) is the poisoned variable — upstream engine bug, wedge signature = #267 class, nondeterministic layer. Prod restored serving on b2ca53c0 with HC_Q8 off (default).

Discriminator staged+running: fixed binary + INJECT teacher-forced. PASS -> trim fix sound, stays pinned; FAIL -> revert pin to 36ecbe98. HC_Q8 serving verdict regardless: OFF (native unpassable; INJECT+fix would trade +210 slots for a ~9% PP tax — operator decision for another day, data in this round).

## Round 18ax — discriminator convicts the trim fix; pin reverted to 36ecbe98; campaign closed

Discriminator (fixed binary b2ca53c0 + INJECT): FAILED — #267 verify-timeout at layer 40. Combined matrix: stock+INJECT pass, tuned+INJECT pass (bit-exact), tuned+native fail, fix+native fail x3, fix+INJECT fail. Conclusion: the trim/skip fix breaks a consumer on harness-class configs (small-ctx/prompt-cache-0; layer 40 sits in CUDA1's trimmed range 30-47 — suspected reader of a skipped bf16 row). Its VRAM math was proven (+210 slots) but the gate is the gate: NO-GO.

Serving pin REVERTED to 36ecbe98 (tuned kernels — the fully-proven binary: bit-exact teacher-forced, full harness passes, best-of-campaign TG 33.9). Fix commit 84fc595dd9 stays on hc-q8-gfx906 as documented WIP with the failure matrix; never merge without root-cause + gate pass.

Final campaign state: prod serving 36ecbe98, dflash-block 7, kv int8 262144, FS 2,8, MMQ 0, prefill 16384, reserves 192/156. HC_Q8 OFF by default (native path = upstream bug: 4/4 harness wedges pre-fix; INJECT quality-proven but costs PP). Upstream bug inventory from this campaign: (1) native hc_q8 upload poison (#267-class wedges), (2) silent NaN corruption at <512 MiB free VRAM instead of clean alloc error (#879 late-guard), (3) multi-GPU add_hc_skip !multi_gpu gate blocks the Q8 swap (fix exists, harness-path bug), (4) --ple-io direct layer-1 read tax behind the slow boundary-read classes, (5) decode cls=1 event-wait gap (latent race). Researcher dispatch follows for the perf roadmap.

## Round 18ay — performance researcher (glm-5.3) roadmap delivered + verified

Explorer agent analyzed the engine codebase under the zero-quality-sacrifice constraint. Full ranked roadmap (A config / B fixes / C kernels, with file:line, impact, validation plans): bench/ab-v0210/notes/perf-roadmap-2026-11-15.md. Spot-verified citations 5/5 true, including a discovery: prod runs STRATA_HIP_PROMPT_F16=1 (config json:89) — engine-documented "not bit-identical" FP16 prompt-GEMM rounding, present in EVERY campaign baseline/gate so far (A4 decision item: gate it or accept as baseline).

Top-3: (1) --ple-io mmap on NVMe (+3-10% PP, bit-exact, attacks fill bimodality + boundary re-reads; STRATA_PLE_AUTO_MMAP=1 exists); (2) hc-swap wedge root-cause → land 84fc595dd9 (+210 slots, audit points: ignored hc_bf16_view_pair failure layer.cpp:1595/1620, device-vs-stream scratch pick, cls-1 event gap); (3) gfx906 decode-switch sweep (empty defaults table arch_defaults.cpp:63; Q8_PACKED/TSUM/SH_STREAM/LFUSE etc., +2-5% TG, bit-identical per engine's own tables). Safety nets: #267 verify retry (B2), loud OOM refusal (B3), checkpoint off read path (B4). C1 fusion parked (KL-0.019 class ≠ zero). GPU lanes await operator go.

## Round 18az — campaign extension: HC_Q8 DUMPED; lane queue A4→A1→A2→DET→A3→B1(cond)→B6

Operator directives: (1) HC_Q8 dumped — stays off permanently, C1 fusion dead with it; pin 36ecbe98 unchanged (tuned kernels dormant, proven bit-exact). (2) B1 root-cause running (subagent) WITH the relevance question: is any of it worth landing without HC_Q8? Landing lane only on a live verdict. (3) A4 decision = measure the F16 speed win first (STRATA_HIP_PROMPT_F16=1 is live in prod env, engine-documented not-bit-identical, inside every campaign baseline). (4) Determinism question answered: greedy sha diverges WITHOUT HC_Q8 too (journal 18ar-era repro pin: 7 distinct shas from 8 gens, warm3a replayed cold2 — placement-trajectory set, CPU-vs-GPU expert rounding on near-tie tokens). Fix machinery verified: STRATA_IQ_MT_MIN=1 exists (native_expert.cpp:351-368, -1..-3% upstream), --cpu-oracle-q8-0 refuses expert-cache "until the GPU expert contract matches" (generate.cpp:5103), helper not covered by hit_cpu_order (generate.cpp:10143). DET lane (MT_MIN=1 repro+tax) slotted after A2 per operator.

Prep (this round): reusable lane toolkit in bench/ab-v0210/rpcprof/gpu-rounds/ — make_variant.sh (variant config+launcher pair, one-line diff from prod), lane_boot.sh (stop-all + boot + fresh-ready + port-probe gate), lane_restore.sh (prod restore + no-NaN check), lane_judges.sh (standard fill80k/tg80k pair via ab9_driver, ctx 442400, out gpu-rounds-20261115), plus lanes a4_f16off.sh, a1_ple_mmap.sh (3 boots), a2_sweep.sh (BASE + 6 arms: Q8_PACKED SH_STREAM TSUM LFUSE PLE_BATCH MMVF_ROWS), a2_gate.sh (teacher-forced qqNIGHT + env, KL-0 gate; sha NOT a gate on this box), det_mtmin.sh (3 cold boots, repro_pin shape), a3_rowcache.sh (--ple-row-cache 2097152; runs AFTER A1 verdict applied to prod config). All syntax-checked; variant plumbing verified one-line-diff. A2 switches existence-verified in src. Launch pattern: background bash | tr -d '\0' | tee logs/<lane>.log for live view + record.

## Round 18ba — B1 root-cause verdict: DEAD without HC_Q8; landing skipped; mechanism solved

Subagent (glm-5.3) root-caused the discriminator wedge. Verdict on the operator's question: with STRATA_HC_Q8 off NOTHING buggy executes (no skip, no hc_q8 buffers, no scratch/events, no launch_q8, no tuned kernels — their only runtime callers are hc_bf16_view/pair, both HC_Q8-gated). Pin 36ecbe98 = zero prod risk (both commits touch only dormant dequant kernels). Not landing 84fc595dd9 costs nothing; as written it SHOULD NOT land.

MECHANISM (H1, explains the controlled pass→fail flip): the fix is an ACTIVATOR of the tuned dequant kernels, dormant in every passing cell. 84fc595dd9 enables add_hc_skip on multi-GPU → arena skips hc_* bf16 rows → per-device Q8 upload → prefill+decode run the scratch dequant per layer-half → launcher (default-on HIP after 81e023bfa0) picks the tuned kernels → dequant_q8_0_ld32_item does uint4 loads at q8+34b+2 / +18 ≡ 4 (mod 16) for every odd block; wide does uint2 ≡ 4 (mod 8). The "gfx906 flat/global byte-addressed, no alignment trap" comment was never validated for GCN5 — a >4B flat vector access violation wedges the wave/queue unrecoverably = #267 signature, scattered layers. Discriminator = fix×tune INTERACTION (stock/tuned alone: skip never ran multi-GPU; fix alone: no tune default). H2: NATIVE never passed on ANY build (stock+native cell was never run — "native poison" carries that caveat); mixed-inject verify read (fused_gr.cu inj_bf16) is the suspect there. H3 (device mismatch) refuted: every enqueue site sets OnDevice.

AUDIT LEADS: (a) CONFIRMED latent: layer.cpp:1595/1620 ignore hc_bf16_view_pair failure — the missing safety net turning coverage bugs into GPU faults; fix sketch exists (return err). (b) refuted. (c) cls=1 no-wait is by design (disjoint halves, same-stream elision). (d) refuted (per-stage ranges; output_hc_* always uploaded; MTP drafter separate).

#267 TRANSFER (feeds B2 if ever wanted): "waits released but GPU did not finish" = stream wedged on real work, never the flags. Retry design: (1) record cudaStreamQuery error code (illegal-address vs not-ready distinguishes poisoned context from dependency wedge); (2) for not-ready rebuild at STREAM scope — destroy/recreate cs_/copy_ + window graphs AND clear cross-stream event bookkeeping (g_hc_ev_set has no invalidation hook — stale event on destroyed stream = deadlock); (3) device reset only on sticky context error.

IF EVER REVIVED: check layer.cpp returns; default TUNE off on gfx906 or make wide/ld32 alignment-safe + stress parity; run the missing stock+NATIVE cell; prove H1 directly with STRATA_HC_Q8_TUNE=0 on the fix (flip to pass = proof).

A4 lane launched (F16-off measurement); queue continues A1→A2→DET→A3→B6.

## Round 18bb — A4 verdict: STRATA_HIP_PROMPT_F16=1 is worth +3-14% PP — KEEP (no change)

F16-off boot (knob confirmed: banner absent in the 15:14 engine log): fresh 80k fill 390.9 tok/s (204.5 s read). F16-on history: fast class 432-455 (176-186 s), slow class 352-379 (211-227 s). The 204.5 s read sits in the slow-wall class, so single-boot attribution is confounded by the bimodality: the flag is worth +3% (vs slow class) to +14% (vs fast class) — every case says KEEP. TG unaffected (28.9 warm, mid-band; expected, prompt-path-only flag). Quality framing: the FP16 rounding is inside EVERY validated campaign baseline (all gates measured with it); removing it would cost double-digit PP for bit-exactness-to-default that nothing requires. DECISION: keep, zero action, prod unchanged.

Lane-toolkit learnings: (1) a second fill leg per boot is conversation-cache-resident (64.8 s "fill" = cache hit) — ONE fresh fill per boot, nfill>1 is useless; (2) the outer tr pipe block-buffers the lane log for minutes — elog + driver jsons (out/gpu-rounds-20261115/<ARM>-<leg>.json/<leg>.json with walls) are the live source of truth; switching to stdbuf -oL for later lanes.

## Round 18bc — A1 verdict: --ple-io mmap NO-ADOPT (no expected-value win, real costs); roadmap #1 falsified on this box

3 mmap boots (knob-verified: no probe line, no direct-warning, --ple-io mmap in engine cmdline):
| boot | fresh 80k fill | read wall | boundary re-read | warm TG |
|---|---|---|---|---|
| 1 | 401.5 | 199.2 s | 231.3 | 29.6 |
| 2 | 397.1 | 201.4 s | 248.0 | 30.8 |
| 3 | 416.3 | 192.2 s | 243.7 | 26.8 |
Direct history: bimodal fast 432-455 (176-186 s) / slow 352-379 (211-227 s); boundary 234-236; TG band 26-34.

Reading: mmap REPLACED the bimodal fill distribution with a tight mid cluster (mean ~405 ±2.5%) — it never reaches the direct fast class and never the slow class. Expected value matches direct's ~50/50 mix (~404) — ZERO expected gain. Boundary re-reads unchanged within noise (the 54 MB/s probe tax does NOT bind our boundary legs — that class is expert/conversation-cache-shaped, not PLE-I/O-shaped). Warm TG unchanged. Costs are real: engine RSS 34 GiB (PLE table resident vs direct's no-mapping), box packed 62/62, less headroom for the relief arenas. VERDICT: NO-ADOPT, prod unchanged (direct). Researcher's top pick (A1+B5, est. +3-10% PP) falsified on this hardware; the fill-wall bimodality remains unexplained — B6 (queue priming) and B4 (checkpoint-off-read-path) stay the live suspects.

Incidents this round (3 lane-bash deaths at serving-start, all on mmap boots): each boot script died silently right after the ready line (boot 1 old lane_boot, boots 2-3 patched setsid --fork; stdbuf exonerated on boot 3 — plain run died too). Engine always survived and served the salvage legs. Best theory: transient memory pressure at PLE mmap first-touch (50.7 GiB table) reaps the small lane bash; kernel logs unreadable (no perms). Workaround proven 3/3: legs-only invocations never span the ready transition — all remaining lanes run boot-phase and legs-phase as separate invocations (a2_arm.sh per-arm one-shots; orphaned setsid --fork launch also tests the session-context-killer theory). stop-all note: variant launchers (launch-<tag>) don't match its 'launch-prod-|launch-trial-' patterns, but the engine/helper/server patterns + shared supervisor state file still stop variant stacks fully.

## Round 18bd — code-optimization analyzer (glm-5.3, operator-directed) delivered + verified

Ran in true parallel with the A2 sweep (glm is REMOTE — operator corrected my wrong assumption that subagent models ride prod; only Qwen serves locally, zero lane contamination). Deliverable: bench/ab-v0210/notes/code-opt-analysis.md (F1-F10, ranked, all bit-exact-by-construction, code-only scope).

THE REFRAME (verified verify.cpp:1262-1285): warm TG is NOT bandwidth-bound — the window is ~123 ms at ~60-90 GB/s aggregate = ~5% of the 2x gfx906 peak. With 8936/24576 experts resident the engine runs the DOORBELL graph: per layer, GPU doorbell -> host serves misses (classify/publish/helper frame/CPU rows) -> flagA -> VRAM/PCIe/helper lanes -> flagB -> combine, x48 serial per window. The wins are LATENCY and HOST-WORK cuts, not bytes.

Verified anchors: (1) per-claim-frame CPU requantize of activations the GPU already quantized — remote_experts.cpp:744-751, 37 frames/window x 72-144 us = 2.7-5.3 ms/window host tax (F2/F3, +2-4% TG); (2) boot slot-fill same-work variance — elog: 376/422/524/563/671 MB/s for identical 9.6 GiB fills = the fill-wall bimodality's smoking gun, page-cache/NVMe warmth mechanism (F7: O_DIRECT + offset-sorted arena reads + per-phase instrumentation, +7-20% PP on slow boots); (3) F6 serial per-slot blocking boot fills (generate.cpp:9344-9368, expert_cache.cpp:807-837) — 20-40 s/boot recoverable via the existing queued API + parallel per-card threads.

Top-3: F4 speculative helper/PCIe prefetch of next layer's misses (route-heat predictor, consume-only validation = bit-exact, +5-15% TG ceiling, L), F2+F3 activation-image ring (+2-4% TG, M, shares infra with F4), F7 bimodality determinism (M). Also F5 PLE gather off the layer0->1 critical path (+0.5-3%), F8 penalty staging, F1 in-graph sampler. Negative findings recorded (dense FP16 dequant amortizes at 16k chunks; expert kernels already fused; KV reads small; rocBLAS tables deterministic) to prevent re-digging.

Implications for the queue: B6 (queue priming) hypothesis now competes with F7's page-cache warmth — F7 has the measured mechanism, B6 is the cheaper test; both remain queued, B6's expected value discounted. All F-items are engine code changes (worktree + oracle gate + validation lanes) — implementation awaits operator decision; none land tonight.

## Round 18be — A2 sweep ABORTED: subagent tool-phase CPU contention poisons TG measurement

Sweep ran Q8_PACKED + half of SH_STREAM before abort. Warm TG: A2BASE 18.2, next arm 8.5 tok/s — vs 26.8-30.8 on every boot before subagents started. Mechanism (consistent with the doorbell-chain reframe, 18bd): decode's critical path is host-side serial per-layer work; the analyzer/planner subagents (glm REMOTE per operator - no engine-slot conflict) run LOCAL tool processes (rtk file reads at 100% CPU, load avg 6-7) that starve the doorbell host work. Correlation: 5 in-band boots pre-subagent, 2 collapsed boots during subagent tool phases. Sweep stopped cleanly (bracket-pattern pkill; the unbraced pattern self-killed one shell - the known hazard, third occurrence). Prod restored. RERUN a2_rest.sh on a quiet box after the planner completes; Q8_PACKED data discarded as contaminated; only teacher-forced KL gates from that boot would be valid (quality is speed-independent).

## Round 18bf — implementation plan delivered (code-opt-plan.md); champion differential in flight

Planner (glm-5.3) delivered bench/ab-v0210/notes/code-opt-plan.md (451 lines): all F-anchors re-verified at HEAD 84fc595dd9 with a refinement table. Two reshapes: (1) F2's "quantize-before-publish" reorder would break folded+qdedup shared-expert reads (image written at verify.cpp:2324/:2332/:2352 AFTER publish) — plan uses a tail-publish kernel after the final image write, strictly additive, and needs ZERO protocol change (build_window_payload memcpys the image into the frame either way); (2) F4's "helper computes predicted rows" is not bit-exact-constructible (x_{l+1} does not exist at layer l) — redesigned as weight/blob pre-stage keyed (layer, expert, pack offset) on helper (kPrefetch=22 + feature bit) and PCIe staging ring, consume-time validated. Phasing: P0 = F6+F8+F1+F7c (F7c fill instrumentation FIRST - every later boot self-attributes, halves B6's boot count), P1 = F2->F3, P2 = F5 + F7ab (6-8-boot distribution), P3 = F4 with a re-measure-residual gate. ~20 default-off commits, ~20-24 GPU-hours validation, per-item DoD + rollback + first-week sequence. Master table + dependency edges in the file. Spot-verified: anchor table + F6 change list (fill_slots_batched via NibbleCache discipline, 22 MiB pinned/cache, verify_slot stays the byte gate).

Operational lesson (third self-kill variant): never pass the PROD launcher path to lane_boot chains - the chain's own cmdline then matches stop-all's 'launch-prod-' sweep pattern and its own stop-all SIGTERMs it. Boot prod-shaped configs via a renamed/variant launcher (launch-a2base.sh = byte-identical config copy). Champion differential (pin 8e3ef28c, yesterday's v0233.2 stock, quiet box post-planner) measuring now via that path; PRODCHECK (36ecbe98 + planner running) read 23.7 warm TG vs pre-subagent boots 26.8-30.8.

## Round 18bg — stage-thread pinning root cause: 3-latch race, silent since Nov 6; tuning script verified live

Operator asked "is the core pinning still correct?" -> NO, and root cause found. The intended topology (per apply-strata-tuning.sh comment + supervisor flag): host=0, pool workers=2,4..14 (verified live: strata-pool-0..6 pinned 2..14), stage threads moved to 3,5,7,9,11,13,15 by pair_supervisor via staging_affinity.py. Reality on ~96.5% of boots since Nov 6 (249 retry records vs 9 successes): ALL 64 strata-stage copy threads + watchdog/issue/fill sit on CPU 0 with the host doorbell work, 18 ROCm driver threads float over worker cores, 7 odd cores near-idle.

Failure chain (3-latch race, all must align inside a ~30 s window): (1) staging_affinity.py requires the engine PID to already be named strata-host (rename happens at session.cpp:569 only after early serve startup - this boot's recorded error: "PID must name a live strata-host process"); (2) stage threads spawn lazily at first staging work (first fill, minutes post-ready) - earlier attempts die "No CPU-0 strata-stage threads found"; (3) STAGE_ATTEMPTS=30 at ~1 s intervals, then stage_result="failed" is written and the supervisor NEVER retries for the life of that engine (retry requires stage_result is None). Failure visible only in run/*-affinity-*.retryN.json + an unwatched state field. aux_cpus.hpp header documents the exact resulting pathology (host preempted 5x, per-layer host-flag waits 60-155 us vs 8) - matches the doorbell-chain reframe.

Fix lane prepared: engine already contains the tested, placement-neutral mechanism ("no output depends on where a thread runs"): --aux-cpus LIST / STRATA_AUX_CPUS. Stager threads pin AT SPAWN (kills latch 2), sweep() also moves driver-created threads off host/worker cores at startup + every request (bonus the external move never did). Variant created: prod-config-auxcpus.json = prod + env STRATA_AUX_CPUS=3,5,7,9,11,13,15 ONLY (semantic diff verified single-key). STRATA_AUX_STAGER must stay unset. Supervisor --stage-cpus becomes a harmless no-op after this (its "failed" log line is cosmetic).

Operator tuning script apply-strata-tuning.sh verified LIVE, all sections PASS: min_free_kbytes=524288 + watermark_scale_factor=100 (persisted sysctl.d), zswap=N, THP defrag=[defer], amdgpu IRQs on 3/11/15, irqbalance inactive. No conflict with the fix (script touches kernel/IRQ only, never thread affinity); its "aux=1" comment describes old auto behavior, needs a one-line refresh after adoption. Boot-lottery TG variance now has a concrete candidate mechanism: pinning race outcome varies per boot.

## Round 18bh — AUXCPU1 verdict: pinning fix works but TG stays collapsed; pinning ADOPTED for prod; fixer landed; A1MMAP4 config-differential in flight

AUXCPU1 (prod pin 36ecbe98, prod config + env STRATA_AUX_CPUS=3,5,7,9,11,13,15): placement verified perfect mid-leg - 64 strata-stage + 18 ROCm driver threads + watchdog/issue/fill/lookahead on 3,5,7,9,11,13,15, primary host alone on CPU 0, workers 2,4..14. Judges: fill 403.0, boundary 246.1 (both normal bands), warm TG 19.5 - STILL collapsed. This falsifies the LAST host-side theory: warm TG has been below band (18.2/8.5/23.7/19.9/19.5) on every boot since 16:21 across quiet-vs-loaded box, both binaries, exact prod config, and perfect-vs-broken pinning. Last in-band boots today: A1MMAP-1/2/3 at 15:49/15:58/16:08 (29.6/30.8/26.8). A persistent post-16:21 state change survives stop-all + reboots; idle GPU check cool (37-46 C, idle clocks, 3080 P5 0x1=GpuIdle) - under-load clock sampling still pending.

DECISIONS (operator): (1) keep the pinning for prod regardless - hygiene win, zero quality risk (placement-neutral by engine design), no measured downside (fills/boundary normal). Adopted: prod-config env += STRATA_AUX_CPUS=3,5,7,9,11,13,15; apply-strata-tuning.sh topology comment refreshed to match. (2) Rerun the EXACT A1MMAP-2 config (--ple-io mmap, verified single-arg diff vs prod, regenerated pre-adoption so byte-faithful to 15:58) as the config differential: the last in-band boots were the only mmap boots - if A1MMAP4 reads in-band now, the collapse discriminates by I/O path; if not, it is box-state (GPU under-load clocks next). In flight (a1_boot_legs.sh 4).

Fixer (glm) landed 7736ad54d2, verified: staging_affinity engine() accepts comm strata-host OR cmdline .../strata --serve (basename match - argv[0] is a path); idempotent apply (Moved 0 when already placed); supervisor retries every 60 s for engine lifetime after the fast 30-attempt window (no more terminal failed burn-out), single .late.json snapshot; lane_boot prints affinity state. py_compile + 10/10 dummy-process tests. Belt-and-suspenders now actually works; primary mechanism is the engine env.

## Round 18bi — pre-reboot evidence: clock-flapping under DPM=manual (survives reboot); A1MMAP4 cancelled; oracle review APPROVE-WITH-CHANGES; PRODCHECK2 in flight

A1MMAP4 judges were cancelled by the operator reboot (fill json written; warm TG never measured - elog's last warm lines remain CHAMP8E 19.9 / AUXCPU1 19.5; the pre-reboot "tg still a mess" impression was the boundary-gen class, not a valid A1MMAP4 number).

Power/clock evidence during the A1MMAP4 legs (operator's ear + sampling): gpu_busy_percent sits at 100% while real load is far below - busy% is kernel residency, not work. The honest tell is power/clocks: the two Radeon VIIs ALTERNATE between deep idle (sclk 699 / mclk 350, ~18 W) and boost (1716-1760 / 800-1000 mclk, ~106 W) on a sub-second-to-second cadence during TG; the 3080 idles at 400 sclk. Coil-whine silence = the idle phases = the operator's "large segments during PP where the GPUs do nothing" (also at PP end: the post-fill alias-table registration block is CPU-only, 35 s class).

DPM: power_dpm_force_performance_level = manual on both Radeon VIIs + the Cezanne iGPU - measured PRE-reboot and again POST-reboot: manual survives reboots, so something sets it deterministically at boot (origin not yet identified - udev/systemd/driver default with a custom pp_table are candidates). If the collapse persists post-reboot, locking clocks (force level high / fixed sclk+mclk) is the next A/B; if TG recovers, the ghost was reboot-cleared state (page cache / driver / DPM transient) and manual is chronic-but-harmless.

Post-reboot state verified: tuning invariants re-applied (min_free_kbytes 524288, zswap N, amdgpu IRQs 3/11 via active strata-irq-affinity.service), pin 36ecbe98 intact, prod config now carries STRATA_AUX_CPUS=3,5,7,9,11,13,15 (18bh adoption). PRODCHECK2 (prod pin + prod config + pinning, first post-reboot boot) measuring now - the discriminator.

Oracle review (glm, remote; ses_e200ef8daffe7Ewl2TvL0oWSHq) delivered notes/code-opt-plan-review.md (189 lines): APPROVE-WITH-CHANGES for P0. Blocking: (1) F5 UNSOUND as designed - the wait_flag_ge(m_flag_,1) anchor at verify.cpp:1499 is ar_on()-only; under the doorbell graph m_flag_>=ring is the CPU-rows-ready signal and flagB is raised by fetch_dma's host-func mid-pool (verify.cpp:3839-3865), so an early side-thread m_flag_=1 lets the GPU copy m_ymiss_ mid-write - silent corruption with a KL gate that can pass on a losing schedule; redesign = gather thread joined after pool() before the unchanged *flag=want (:3616), thread-raise variant only for ar_on(). (2) F2 stale-seq: stage_inputs does not reset m_xq_seq_ per window (verify.cpp:3160-3163) - window>=2's >=want wait passes on stale values; trivial fix, must enter the plan; STRATA_QFUSE is DEFAULT-ON (arch_defaults.cpp:86) so the fused GR producer parity leg (gr_parity.cpp:416) must be gated/tested and ring use must not depend on !q8_ffn. (3) F4: helper is a serial serve-thread per socket (helper_main.cpp:25-31) - synchronous prefetch would block the next kBegin; ack-drain belongs in the reply reader finish(), not just begin(). (4) Baselines invalid twice over: every WT% target predates the pinning fix AND the collapsed band - qq KL gates (speed-independent) may proceed; all tg80k legs / WT% acceptance waits for band restoration; F7c-first is now mandatory. Non-blocking: F6 byte-gate covers one slot/boot (use STRATA_VERIFY_COMPLEMENT :9409 for all-slots), F6 DoD should be per-phase, F8 "previous window" framing unrealizable in the serial loop, F3 destination is nact_multi not act_multi.

## Round 18bj — PRODCHECK2 post-reboot: 22.1, collapse survives reboot; falsification complete; PCIe link-width suspect; plan amended

PRODCHECK2 (first post-reboot boot; champion pin 36ecbe98 + prod config incl. STRATA_AUX_CPUS; no-NaN OK; cold-page-cache boot, fill 402.7 normal): warm TG 22.1 - BELOW BAND. The falsification set is now complete: quiet box (CHAMP8E 19.9), champion binary (19.9), exact prod config (23.7), perfect pinning (AUXCPU1 19.5), mmap I/O path (A1MMAP4 ~20.2 partial, cancelled by reboot), browser dead, full box reboot (22.1). Nothing host-side, config-side, binary-side, or boot-state-side restores the 26-34 band. Fills stay normal throughout (364-421) - the failure is TG/latency-specific.

Boot-path bug found+fixed post-reboot: the reboot left stale unix sockets (run/prod-iq3xxs-ud-mixed*.sock); pair_supervisor refuses existing socket paths and dies instantly+silently; stop-all only cleans sockets when it kills processes, so lane_restore hung in its ready-wait on a boot that never started. Fix: removed sockets manually; stop-all needs unconditional stale-socket cleanup (follow-up).

Prime suspect (open): PCIe link width. Earlier idle snapshot showed card0 at 8.0GT/s x4 and card1 at x1 (Radeon VII is x16) - normal ASPM downtraining at idle IF it retrains to x16 under load. If links stay downtrained under load since ~16:21, that matches every signature: latency-bound TG (per-window handoff + helper traffic) crushed, pipelined sequential fills untouched, GPUs busy-but-starved, coil-whine silence. lspci under sudo failed (no askpass); checking via rocm-smi --showpcieinfo idle vs under load next. DPM force_level=manual persists post-reboot (set at boot by something unidentified - also open).

Plan amendments (fixer, glm, ses_e1ffd42dbffetnx50Zv8jdL8Ks): code-opt-plan.md 451->480 lines, all 8 oracle-review items implemented with re-verified anchors (F5 fully rewritten around the join-after-pool redesign with the unsoundness proof; F2 stale-seq reset + QFUSE legs; F4 serial-serve-thread + ack-drain in finish(); F6 VERIFY_COMPLEMENT all-slots gate + per-phase DoD; F8 reframed; F3 nact_multi; F1 penalized-greedy arm; new re-baselining section - F7c mandatory-first, KL gates proceed, tg80k/WT% held for band restoration). Re-review being dispatched.

## Round 18bk — shared-runtime audit and authorized helper-only comparison (2026-11-15)

Operator confirms no concurrency and no downclock. Earlier live small-prompt probes are NOT comparable to the judge's second, warm ~80k-context request. Their user-space CPU samples do not prove useful CPU expert computation: `ExpertPool::worker` self time must be resolved to instructions to distinguish work from idle spinning. No cause established by those probes.

Runtime audit at 18:04–18:06: live engine PID 108960 is sha `36ecbe983837828dd38a9f8d452cda519338369d7f42d8ad87b2830b494784b4`. `/proc/108960/maps` shows HIP/hipBLAS/HSA and other ROCm libraries from `/opt/rocm-6.1.0/lib`, plus `/home/srcds/rocm-gfx906-xnack/lib/librocblas.so.4.1`. No `/opt/rocm/` or `/opt/rocm-6.4.4/` mappings. Production `LD_LIBRARY_PATH` forces those correct paths. Both champion ELFs have the same dependency SONAMEs and no RPATH/RUNPATH; production-environment `ldd` resolves the old `8e3ef28c` pin correctly too. The interactive shell's search path DOES prefer `/opt/rocm/lib`: plain shell `ldd` misleadingly resolves 6.4.4 and newer HSA. This is a shell/test-launch hazard, not evidence of contamination in the live engine. Historical build-toolchain identity was not re-established by this runtime check.

Operator proposed, then explicitly authorized a production restart for a helper-only comparison. Verified distinct pins: current v0233 helper `553f3bf48219c99298e4620547507eac7eefa29d660ee32e806bb9780a9ab361`; v0232b helper `42358985b20a72b4913e9cc2d936972fd6696378cc7fdf1913e7ee002ece3fd3`; v0232 `6de16b7f`; v0227–v0231 `8d37045c`. Plan: v0232b helper only, unchanged current engine/config/flags, boot identity and finite-output checks, standard fresh-fill/boundary/warm judges with memory, then restore v0233 helper and measure again. No promotion; any candidate requires the teacher-forced quality gate, never greedy-sha equality.

The old-helper launcher is a generated variant; production launcher/config stay unchanged. `stop-all.sh` inspection corrects the earlier stale-socket attribution: cleanup was already unconditional but named obsolete prod sockets, omitting the actual `prod-iq3xxs-ud-mixed{,2}.sock` pair. Added those two exact paths. `lane_stream.sh` wraps unchanged judges and streams null-stripped engine output until the judge exits, preserving a per-arm log. No GPU settings, quality settings or engine code changed.

Read-only GLM audit did not run: the first endpoint failed authentication and the second rejected the subscription tier. No specialist is doing live work during the restart. Shared-path cause remains unproven; helper experiment pending.

Offline instruction-level correction: `perf annotate` of the saved cold-probe capture succeeds (exit 0). Of 12,630 `ExpertPool::worker` samples, 15.24% hit `0x29738bb` and 75.75% hit `0x29738be`, the increment/test immediately after `pause` at `0x29738b9`. These are the idle epoch-poll loop (`src/kernels/cpu/pool.cpp:999–1015`), not expert computation; `drain()`'s call site has 0.00%. Of 991 `Verifier::run` samples, 11.20% and 76.59% hit `0x26a3686`/`0x26a368a`, likewise the pause counter in the sequence wait, matching `src/core/verify.cpp:3524–3530` (waiting for the GPU to reach a layer). This disproves the earlier compute-saturation/fallback interpretation, not the regression. No in-band capture exists to establish that these waits changed. Raw capture and full annotations retained under `out/gpu-rounds-20261115/shared-profile/`; binary has no source-line debug info (`addr2line` identifies functions but returns `??:?`).

H232B1 boot completed 18:16:56, generation 1; live engine PID 1144413 verified `36ecbe98`, helper PID 1143452 verified v0232b `42358985`. Config `cmp` equals production. Both helper channels connected, 3703 slots (6.93 GiB), warmup output finite. Smoke returned HTTP 200 in 2.828 s, content `OK`, no API error (22 completion tokens including reasoning). This is a liveness check only, not a quality gate. Standard judges now run unchanged under `perf record -F 99 -e cpu-clock:u`; the restored-current arm will use the same sampling. The existing `qqNIGHT.json` has no helper arguments and cannot by itself certify a helper-binary swap.

## Round 18bl — older-helper arm fails to restore warm TG; canonical helper restored (2026-11-15, 18:18–18:27)

H232B1 judges completed, exit 0, with current engine and old v0232b helper. Engine lines, NOT the last-four-lines shortcut (which includes internal continuation-shaped requests):

| phase | prompt reuse/read | read | generation | acceptance |
|---|---|---|---|---|
| fresh fill | 0 + 79992 | 183.210 s / 436.6 tok/s | 6 / 0.382 s | 3/3 |
| boundary | 65536 + 14456 | 59.791 s / 241.8 tok/s | 513 / 20.743 s / 24.7 tok/s | 342/446 |
| warm primary | 79987 + 5 | 0.235 s | 512 / 32.134 s / **15.9 tok/s** | 341/438 |

The two benchmark POSTs also generated 164-token and 171-token continuation-shaped engine requests at prompt 80522 (80504/80503 reused plus 18/19 read); API walls were 88.997 s and 47.037 s. These are being traced in the Python wrapper; no concurrency claim is made from extra engine lines. Old helper does not restore the 26–34 band. Both primary decode requests had zero helper timeouts and zero fallbacks. No candidate promoted and no quality-equivalence claim.

Memory (MiB, actual driver records): fill RSS peak 46359, minimum available 13238, maximum swap used 6434; TG RSS peak 49442, minimum available 14816, maximum swap used 7514. VRAM maxima across the arm: card2 16356, card3 16182, NVIDIA helper 7447, iGPU card4 452. Artifacts: `out/gpu-rounds-20261115/H232B1-{fill80k,tg80k}.json/`, `H232B1-engine.log`, `H232B1-smoke.json`, `H232B1-perf.data`.

Exact warm-path CPU evidence: a conservative ten-second interior slice of the primary warm decode (`perf report` and `perf script --time 3990,4000`, both exit 0) has 684 `Verifier::run` IP samples, ALL on the GPU-sequence wait loop at `0x26a3674`–`0x26a3691`; 586/684 hit its increment/test. This establishes where the host waits during the actual warm benchmark, not why those waits became longer than historical boots. `perf annotate --time` is unsupported in installed perf (exit 129); time-filtered `perf script` is the working evidence path. Reports retained under `shared-profile/H232B1-warm-{report,ips}.txt`.

Canonical `lane_restore.sh` completed at 18:27:16, exit 0, with smoke content `OK` and finish `stop`; no engine/config/binary replacement was made. H233R1 matched current-helper judges are now running with the same 99-Hz user-only sampling. Further causal conclusions wait for that control. A single read-only GLM Flash retry is checking the shared wrapper; no boots or live requests assigned to it.

## Round 18bm — H233R1 control IN BAND (31.0); H232B2 reverse leg IN BAND (29.7) — HELPER EXONERATED, boot bistability is the phenomenon (2026-11-15, 18:27–18:52)

H233R1 (canonical restore, current helper `553f3bf4`, engine `36ecbe98`, identical judges): fresh fill 457.0 (175.0 s), boundary 241.4 (59.9 s, 406 gen / 23.3 tok/s), warm primary 79987+5 reused, **484 gen in 15.610 s = 31.0 tok/s — IN BAND**. Helper reqs 6294/37215 rows, zero timeouts/fallbacks. API walls 80.371/19.020 s. The restore boot's own smoke had read 19.0 tok/s cold — irrelevant to the warm judge.

Operator then ordered an immediate reverse retest. H232B2 (old helper `42358985` booted again at 18:39, engine/config byte-identical, `--arm H232B1` prompts REPLAYED so the bytes match the 15.9 leg exactly; artifacts under `out/gpu-rounds-20261115/h232b2/`): fresh fill 461.4 (173.4 s), boundary 246.4 (58.7 s, 735 gen / 24.6 tok/s), warm primary 79987+5 reused, **515 gen in 17.324 s = 29.7 tok/s — IN BAND**. Helper reqs 7121/50683 rows, zero timeouts. API walls 91.845/36.580 s.

| boot | helper | warm TG | note |
|---|---|---:|---|
| H232B1 18:14 | old `42358985` | **15.9** | collapsed |
| H233R1 18:25 | current `553f3bf4` | **31.0** | in band |
| H232B2 18:39 | old `42358985` | **29.7** | in band, identical prompts to H232B1 |

VERDICTS: (1) The helper binary is NOT the cause — the same old helper that read 15.9 read 29.7 on its next boot. (2) The "persistent post-16:21 box state" framing is falsified — the band flipped back at 18:25 and held across two further boots (three consecutive in-band boots including the reverse leg). (3) What remains is per-BOOT bistability: ~15–22 vs ~27–31 tok/s classes on byte-identical configs, binaries, prompts, and verified pinning. This is the known boot-lottery phenomenon, now with a clean same-day A/B/A capture. Weak correlate visible in the data: the collapsed boot's fill was also the slowest today (436.6 vs 457.0/461.4) — consistent with the F7 page-cache warmth angle but n=1. Same-prompt warm sha differs across boots (55a8c518… vs f8a8387d…) — the known placement-trajectory nondeterminism (18al), confirming boot-level placement differs even when speed does not.

Raw shas per arm: H232B1 55a8c518…, H232B2 f8a8387d…, H233R1 a4b6ada6… (each vs its own replay; not a gate, recorded for the ledger). Perf captures retained: H232B2-perf.data (111k samples), H233R1-perf.data (100k samples) — both 99 Hz user-only over the full judge legs, available for fast-vs-fast and fast-vs-slow warm-window comparison without new boots.

Next discriminator (pending operator): the bistability must be bisected at boot level — e.g. reboot-loop the SAME config N times scoring warm TG per boot, sampling per-boot placement/fill/page-cache state, to catch a fast-vs-slow boot pair same-config and diff their state at rest. F7c instrumentation (already approved first in the P0 plan) is exactly the tool for the fill-side correlate.

Prod restore to the canonical current-helper stack (H233R2) was launched at ~18:50 and was still in its ready-wait when this entry was drafted; the restore's completion line and smoke result are appended below once observed.

H233R2 restore verified 18:50: ready line present, no-NaN smoke content `OK` finish `stop`, supervisor `attached`, live engine `36ecbe98`, live helper `553f3bf4` (canonical). Production is back on the unchanged champion stack; 18bk–18bm changed no config, no binary, no quality setting.

## Round 18bn — PRODCHECK3 on the canonical boot: 30.3 in band; fill-class/TG-class DECOUPLED (2026-11-15, 18:52–18:58)

PRODCHECK3 (judges on the restored H233R2 boot itself — no restart, the exact stack now serving): fresh fill **366.4** (218.3 s, slowest of the day; its idle disk park also stretched to 12.4 s), boundary 219.9 (65.7 s, 401 gen / 18.0 tok/s), warm primary 79984+5 reused, **457 gen in 15.103 s = 30.3 tok/s — IN BAND**. Helper reqs 5558/35486 rows, zero timeouts. Perf capture PRODCHECK3-perf.data (106k samples, same 99 Hz).

| boot | helper | fill | warm TG |
|---|---|---:|---:|
| H232B1 18:14 | old | 436.6 | **15.9** |
| H233R1 18:25 | current | 457.0 | **31.0** |
| H232B2 18:39 | old | 461.4 | **29.7** |
| H233R2 18:50 (PRODCHECK3) | current | **366.4 (slow class)** | **30.3** |

Two conclusions: (1) three consecutive in-band boots — the band flip-back at 18:25 is stable so far; (2) **the fill-speed/TG-class correlate is dead** — PRODCHECK3 pairs the day's slowest fill with an in-band warm TG, counter to H232B1's pairing. Fill bimodality (F7's subject) and the TG boot lottery are separate phenomena. The TG discriminator remains the boot-level bisection (reboot-loop, placement/foresight state sampling). Artifacts: `PRODCHECK3-{fill80k,tg80k}.json/`, `PRODCHECK3-engine.log`, `PRODCHECK3-perf.data`. No promotion, no config/binary change made at any point in 18bk–18bm.

## Round 18bo — A2 sweep rerun COMPLETE on a quiet box (2026-11-15, 19:05–19:56)

All 6 remaining arms back-to-back through the standard lane (variant launcher → boot-gate → 1 fresh fill + tg judges), canonical prod restored at 19:56:03 with smoke `OK`. Arms are single-knob env adds over the champion config; every boot verified through lane_boot; no subagents ran during the window (18be's contamination mode).

| arm (env) | fill read | boundary read | warm TG | acceptance | notes |
|---|---:|---:|---:|---|---|
| A2Q8_PACKED (`STRATA_Q8_PACKED=1`) | 423.5→414.2 class | 252.5 | **26.9** | 339/480 | boundary first-gen 8.4 (known first-gen thrash, 18ao); 18be's "8.5 warm" reading confirmed as contention poison |
| A2SH_STREAM (`STRATA_SH_STREAM=1`) | 423.5 | 245.9 | **28.1** | 276/416 | boundary first-gen 11.6 (same artifact class) |
| A2TSUM (`STRATA_TSUM=1`) | 414.2 | 253.0 | **28.8** | 301/458 | clean |
| A2LFUSE (`STRATA_LFUSE=1`) | 433.1 | 253.7 | **25.4** | 210/329 | clean |
| A2PLE_BATCH (`STRATA_PLE_BATCH=1`) | 401.7 | 253.6 | **22.9** | 354/522 | fill-gen 9.7 / boundary-gen 16.6 (first-gen thrash); warm lowest of sweep |
| A2MMVF_ROWS (`STRATA_MMVF_ROWS=1`) | (fill wall 183.9 s) | 254.0 | **42.2** | 378/423 (89.4%) | 515 gen in 12.217 s — far above the 26–34 band and the day's best champion boot (31.0) |

Readings: five arms cluster 22.9–28.8 (in-band low-mid, one possible collapsed-class at PLE_BATCH); **MMVF_ROWS read 42.2 tok/s with 89.4% acceptance — +36% over the best same-day champion boot**. Records verified from the driver jsons (tg walls 98.6/37.5 s, mem normal-class: RSS 50.3 GiB peak, swap 7.6 GiB, VRAM maxima unchanged). Single-boot attribution discipline (18bb lesson) applies: 42.2 is a candidate, not a result — the boot lottery spans 22.9–31.0 within this very sweep, so the knob effect must be separated by repeat boots: MMVF ×2 + champion ×1 interleaved before any adoption talk, then the teacher-forced KL gate for the knob (it touches the verify rows path; bit-exactness is NOT to be assumed for it).

Artifacts: `out/gpu-rounds-20261115/A2<ARM>-{fill80k,tg80k}.json/` ×6, sweep log in the a2_rest shell output. Prod: canonical champion stack serving since 19:56:03.

## Round 18bp — MMVF_ROWS confirmation: 42.2 does NOT reproduce; knob falsified (2026-11-15, 19:58–20:23)

Triplet on fresh arm labels (same-arm reruns would restore from the conversation disk tier and measure nothing): MMVF2 29.8 (acc 324/514 = 63.0%) · CTRL2 champion control 26.7 (192/309 = 62.1%) · MMVF3 29.3 (349/481 = 72.6%). With the sweep's A2MMVF_ROWS 42.2 (89.4% acc) as sample 1, the knob's repeats sit inside the champion class — the outlier boot paired an unusually high acceptance with its speed, i.e. a lucky trajectory (more accepted drafts per window), not a mechanism. **VERDICT: MMVF_ROWS NO-ADOPT — no measurable effect; the 42.2 is a boot-lottery draw.** No KL gate run: nothing adopted. The A2 sweep is now fully adjudicated: all six knobs NO-ADOPT.

Prod restored 20:23:18, smoke `OK`. Artifacts: `MMVF2/CTRL2/MMVF3-{fill80k,tg80k}.json/` + `-judges.log` under out/gpu-rounds-20261115/, script `mmvf_confirm.sh`. Next queue item: DET (det_mtmin.sh).

## Round 18bq — DET: IQ_MT_MIN buys ZERO determinism; drafting-rounding mechanism dead (2026-11-15, 20:24–20:43)

`STRATA_IQ_MT_MIN=1` (multi-token CPU expert kernels for every group — the one candidate that removes the drafting-dependent rounding, native_expert.cpp:351-368 #152): 3 cold boots + 2 warm repeats, all greedy temp-0, same prompt → **5 distinct shas** (a4caff4c…, 5046c750…, a4d44dee…, d8ff835f…, 60b1e805…) vs baseline's 7-of-8. Not even back-to-back same-boot warm POSTs agree (warm3a ≠ warm3b, 192 vs 131 words — divergent lengths mean the trajectories separated early). Tax: judges fill 438.1 / boundary 242.3 / **warm TG 26.5** — inside the champion band; the upstream −1..3% decode tax is invisible in our noise. **VERDICT: NO-ADOPT — pays a possible cost for zero determinism.**

Mechanism value: with drafting-dependent rounding ruled out, sha divergence arises mid-boot from speculative accept/reject boundary flips (tiny logit diffs → different accepted tokens → different continuations), consistent with 18al's placement-trajectory account. Fixed placement (tier-A rig, shelved) is the only remaining determinism lever.

Artifacts: `DETMT1-{fill80k,tg80k}.json/`, det_mtmin shell output. Prod restored 20:42:56, smoke `OK`. Next: MMVF_ROWS adoption gate (mmvf_gate.sh — fresh knob-off baseline, stored NIGHT.tsv predates WT HEAD).

## Round 18br — MMVF_ROWS quality gate PASS; adopted into champion (2026-11-15, 20:43–21:10)

Gate (mmvf_gate.sh): fresh knob-off baseline `NIGHT-fresh.tsv` + `STRATA_MMVF_ROWS=1` run `A2G-MMVF_ROWS.tsv`, back-to-back on the same harness engine (stored NIGHT.tsv of 11-14 predates WT HEAD 84fc595dd9 and was not trusted as reference). Compare over all 4995 teacher-forced positions (code/docs/english/multilingual/agent): **KL 0.00000, KL p99 0.0000, top-1 100.00%, PPL 3.924 = 3.924** — the fused 2–8-row BF16 GEMV (bf16_gemv.hpp) is bit-identical on the teacher-forced path; the reduction-order concern is refuted on this corpus.

**ADOPTED on operator instruction** after the gate passed: champion config env gains `"STRATA_MMVF_ROWS": "1"` (one-line diff), canonical relaunch. Honest record of the speed evidence: no mean effect (knob boots 29.8/29.3 vs champion class 26.7–31.0; pp 435 same class) — the 42.2 outlier stays attributed to boot lottery/trajectory. Adoption is quality-neutral and speed-neutral-in-the-mean; the retained rationale is the tail observation (highest-ever box reading, 42.2 with 89.4% acceptance, landed on this knob, 1-in-3 knob boots vs 0-in-many champion boots). Two confirmation KL passes remain queued (qqNIGHT todo) per the ≥3-pass campaign convention.

## Round 18bs — PHASE A COMPLETE: all P0 code items + B6 enablement landed (2026-11-16, 00:30–08:40)

Ten commits on `hc-q8-gfx906` (Strata-wt-integ), every gate default-OFF, compile-only validated (`build_pin_j4` GUARD PASS ×4, tests build green, zero ctest/GPU/boots), all pushed to fork:

| item | commits | gate | key deviations (accepted) |
|---|---|---|---|
| F7c fill instrumentation | 3b23b40c7e | — (always-on log fields, append-only) | helper_fill_s on the expert-helper line (kFill lives in remote_experts.cpp) |
| F6a batched fill API | d26b0aee3e | — (unused API) | stream drained on return (verify_slot ordering contract) |
| F6b boot-fill pipeline | afc2ad38ff | STRATA_BOOT_FILL_QUEUED | kFill sent AFTER both loops (open_socket at generate.cpp:10091) — recorded |
| F6c per-card parallel fills | e9593357b0 | same gate | helper-overlap SKIPPED: frame hranked is filtered against howned from admit() inside the loops — early send cannot be byte-identical (plan's assumption wrong; correct fallback) |
| F8 mapped penalty staging | 14bdd32520, bc99fbb889 | STRATA_PENALTY_MAPPED | kHistMax=4096 (not 64 — plan's 2 KiB image would be 256 KiB): mapped-count-read-at-replay copy (plan's own alternative), host-only test 27/27 |
| F1 in-graph sampler | cfd0e25d37, 945e68a27e, 41a98fc977 | STRATA_SAMPLER_IN_GRAPH | ONE generic kernel infeasible on gfx906 (36,864 B LDS → residency deadlock, measured): one mode-selected sampler as two launches (part+merge); parity test registered (ctest #73) NOT executed; penalized-greedy armed by design |
| B6 queue-prime enablement | b9f58186a4 | STRATA_HIP_QUEUE_PRIME | env contract pre-existed (spec strings); gfx906 gate: unset/0=off, named spec (e.g. "nnnh")=on |

**B6 lane's finding (high value for the shelved bistability)**: `prime_hw_queues()` (generate.cpp:3536) exists to work around a per-START hardware-queue pipe-assignment lottery — ROCm MES assigns HW queues to compute pipes lazily; in ~half of starts two busy queues (compute + expert staging) land on the SAME pipe → ~30k prompt hand-offs wait a pipe switch → 4K prompts 13% slower upstream (R9700). A per-boot ~50/50 queue-placement lottery is a live candidate mechanism for our boot bistability; B6's interleaved boots + F7c phase fields (queue placement ⇒ H2D-phase shift with identical pack-read; page cache ⇒ pack-read shift) can discriminate (plan §F7c B6-interaction).

Engine assembled for Phase B: `/mnt/srcds/Strata/v0234p0/` — rpcw-hip/strata = b6 pin (sha256 471209e0…, `strata 0.1.57` version-probes clean), rpcw-cuda = symlink to v0233's helper (protocol untouched by any P0 item, helper binary identical to champion's 553f3bf4). Phase B begins: F6 ladder → F8/F1 KL gates → qqNIGHT MMVF ×2 → A3 → B6 priming A/B → restore.

## Round 18bt — F6 LADDER PASS on v0234p0: byte gate clean, boot fills ~6× (2026-11-16, 09:06–09:30)

First ladder attempt (08:42) ran 5 boots on the WRONG engine — the variant generator swapped the launcher's `BIN=` but the config json's `"exe"` field is what resolves the engine (launcher BIN only feeds the helper path + preflight). All knobs silently dead on the v0233 binary; data void. Fixed (`make_engine_variant.sh` rewrites config exe, commit 11b69ac; ladders/B6 now read the config's append-only engine log by offset segment — boot lines live there, not in the launcher logs).

Real ladder (5 boots + judges + restore, prod down, zero agents):

| boot | gate | boot-fill wall | MB/s | VERIFY_COMPLEMENT sha16 |
|---|---|---:|---:|---|
| f6off-base | OFF | 19.6 s | 507 | 5543a2520f55aaca |
| f6on-1 | ON | ~3.2 s | 3051 | 5543a2520f55aaca |
| f6on-2 | ON | ~3.0 s | 3264 | 5543a2520f55aaca |
| f6on-3 | ON | ~3.0 s | 3263 | 5543a2520f55aaca |

**Verdict: F6 PASSES its byte and speed gates** — all-slots checksums identical gate-on vs gate-off (non-vacuous: real per-slot lines captured from the v0234 engine), boot pre-fill ~6× faster gate-on (DoD asked ≥2×), 3 consecutive ON boots at the box's fast-class ceiling. Judges on the new binary: F6OFF1 fresh fill 445.4 / boundary 253.0 / warm 28.5; F6ON1 boundary 254.1 / warm 35.6 (fill leg in json) — warm spread is boot-lottery class, not attributable to F6 (fill-only change). Restore clean, no-NaN OK.

**Defect logged**: F7c phase fields print `pack_read_s 0.0 h2d_s 0.0` in BOTH modes — the Clock accumulators never accumulate (9.9 GB pinned cannot move in 0 s). F7c's own DoD (attribute a known-slow phase) FAILS as-is; fix lane queued for the next code window. F6's verdict stands on walls + complement. Caveat on the 6×: single OFF boot was slow-class (507); ON boots sit at the fast-class ceiling — gate effect and class are partially confounded at n=1 OFF; the B6 interleaved run adds class statistics.

Artifacts: `out/gpu-rounds-20261116/f6-ladder.tsv`, `f6{off-base,on-1..3}-elog.txt`, `F6ON1/F6OFF1-{fill80k,tg80k}.json/`. Next: p0_gates.sh (ctests + NIGHT-v0234 baseline + F8/F1/F6/MMVF×2 KL gates).

## Round 18bu — P0 quality gates on v0234: F1/F6/F8/MMVF all PASS; two flaky #267 deaths (2026-11-16, 09:30–10:06)

Harness variant: `strata-ldp-v0234.sh` + `qqNIGHT-v0234.json` (same teacher-forced corpus, engine = v0234p0 binary). Fresh baseline `NIGHT-v0234.tsv` (4995 positions), then env-gated runs, each vs the fresh baseline:

| gate | run 1 | rerun | verdict |
|---|---|---|---|
| F1 `STRATA_SAMPLER_IN_GRAPH=1` | KL 0.00000, PPL 3.924=3.924, top-1 100% (4995 pos) | — | **PASS** (1 clean pass; F1 also inherits F8's mapped-hist machinery) |
| F8 `STRATA_PENALTY_MAPPED=1` | engine died: `verify: timed out at layer 24 … (#267)` at first serve request, 105 lines | **KL 0.00000 full corpus** | **PASS** (flaky death, see below) |
| F6 `STRATA_BOOT_FILL_QUEUED=1` | engine died: same #267 at layer 34, 20 lines | **KL 0.00000 full corpus** | **PASS** (flaky death) |
| MMVF ×2 (`STRATA_MMVF_ROWS=1`) | KL 0.00000 ×2 | — | **PASS — 3/3 adoption convention now met** (18br + tonight's 2) |

**The two deaths are environmental, not gate defects**: both #267 hangs (GPU waits released, not finished in 5 s) landed in one 8-minute window (09:38–09:46) while all seven runs before/after — baseline, F1, MMVF2/3, both reruns — booted the same binary and completed the full corpus. Clean full-corpus passes WITH each gate on clear the gated paths of bit-divergence. Remaining reliability concern (queued): per-boot flake ~2/9 boots this window; candidate mechanisms are the B6 queue-placement lottery (per-start HW-queue/pipe assignment — `prime_hw_queues` exists for exactly this class) or a boot-time graph-capture race. F6/F8 need 2 more clean passes each before promotion (convention).

**ctests parked**: all 6 (expert_cache×4, sampler_mapped_parity, penalty_hist_stage) self-skip with `no CUDA device: skipped` even under the ROCm env wrapper, while an engine holds the cards — KFD visibility question; their byte-proof purpose is already covered engine-side (VERIFY_COMPLEMENT ×4 + full-corpus KL). Follow-up only.

Prod restored 10:06:30, no-NaN OK. Artifacts: `out/{NIGHT-v0234,F8G-PENALTY,F1G-SAMPLER,F6G-BOOTFILL,MMVF2,MMVF3,F8G-RE,F6G-RE}.tsv(+.stderr.txt)` under quality/out/, `p0-ctests{,-env}.log`. A3 rerun + B6 priming A/B follow in this window.

## Round 18bv — A3 (--ple-row-cache 2M): NO-ADOPT (2026-11-16, 10:07–10:17)

Single arm on the champion binary (fresh boot, standard judges): fresh fill 447.1 (fast class), boundary re-read **185.0 tok/s** (78.1 s — worse than the ~250 typical class), boundary first-gen 14.4 (known thrash), warm TG 28.3 (in band). Doubling the PLE row cache to 2,097,152 rows (+90 MB host) buys nothing measurable on any leg. **VERDICT: NO-ADOPT.** (Single-boot caveat per discipline; the boundary reading is class-confounded, but nothing here argues for a repeat.) Prod restored 10:17:06, no-NaN OK. Next in window: B6 queue-prime A/B on v0234p0.





## Round 18bw — B6 queue-prime: NO-ADOPT; #267 occurrence #3; wikitext-2 standard staged (2026-11-16, 10:17–11:14)

**B6 A/B (b6_prime.sh, v0234p0, 8 interleaved boots + deciding judge pair)**: priming the HW queues at START does NOT pin the boot-fill class. CTRL fill rates 307/318/493/2588 MB/s, PRIME 314/338/876/1785 — both arms bimodal, medians ~400 vs ~600 (noise). Deciding judges: B6CTRL fresh fill 253.0 (re-read) / warm TG 30.3 (in band); B6PRIME warm TG **18.8 (collapsed class)** plus a 0.9 tok/s stall during the fill leg's generation — n=1 and TG-lottery-confounded, but nothing argues for the knob. **VERDICT: NO-ADOPT** (`STRATA_PRIME_HW_QUEUES` stays unset; gate commit b9f58186a4 remains default-off). Bistability mechanism NOT convicted: the MES pipe-assignment hypothesis stays open but this priming shape doesn't move it. #267 did not recur across B6's 10 boots. F7c phase fields printed 0.0 in all 10 boots (fix lane running). Artifacts: `out/gpu-rounds-20261116/b6-prime.tsv` + per-boot elogs.

**Convention passes interrupted (GPU parked by operator)**: F8G-P2 booted, drew the slow class (pin registration 4480 ranges / 42.6 GiB / 324 s), then **#267 occurrence #3** on the first serve request — `released the verify window's GPU waits (#267): the GPU finished in 13883 ms`, request answered None, no TSV. Occurrences: 2 yesterday, 1 today; pattern is consistently *first request after boot*. F8/F6 remain 1/3 each; reruns + wikitext PPL legs queued for the next GPU window. Prod restored 11:13:47, no-NaN OK.

**wikitext-2 absolute-PPL standard staged** (for future PPL measurements, operator-approved): wikitext-2-raw test split (Salesforce parquet → wiki.test.raw, 1,287,656 bytes / 4,358 rows) split into the harness's 5 canonical segments × 999 teacher-forced positions, tokenized with the pack tokenizer (`data/quant_quality/wikitext2/set.ids.json`, commit fa73247). Runs use `quant_quality.py run --ids …/wikitext2/set.ids.json`; WIKI-233/WIKI-234 baseline legs queued for the next window.
## Round 18bx — F7c accumulators fixed (units mismatch); delegation model widened; parallel lanes opened (2026-11-16, 11:2x–12:xx)

**F7c root cause** (lane agent, commit `9d5cb763f4` on hc-q8-gfx906, pushed): NOT a dead code path — a `std::chrono` units bug. The F7c commit summed `duration<double>` (seconds) into variables named `_ms`, and both boot report lines divided by 1e3 as if they held milliseconds: a real ~20 s pack-read phase printed `20.0/1000 → 0.02 → %.1f → 0.0` on every boot, every config. Fix: `duration<double, std::milli>` at all 9 accumulation sites in `src/program/generate.cpp` (prints unchanged). Instrumentation-only, zero fill-behavior change; build exit 0. Boot-line verification pending the v0234p1 gates (every p1 boot prints the fields). Two reboot deaths of the lane agent lost nothing (tree clean both times); harness re-marked dead sessions "running (unconfirmed)" after host restart — enumerate sessions before spawning post-reboot.

**Delegation model change (operator)**: agents now run on remote GLM (zai plan) — parallel lanes and prod-independent delegation unlocked; subagents-on-prod constraint retired. Opened simultaneously: F2 lane (worktree /mnt/srcds/Strata-wt-f2, branch f2-xq-ring), F7ab lane (/mnt/srcds/Strata-wt-f7ab, f7ab-boot-io), #267 read-only forensics. GPU window reopened: golden build_pin f7cfix @ 9d5cb763f4 → v0234p1 → p1_gates.sh (baseline, F8G/F6G ×2 closing the 3-pass convention, WIKI-234P1 + WIKI-233 absolute wikitext PPL, F7c boot-line check). Engine fork push transiently failed post-reboot (SSH agent), succeeded on retry.
## Round 18by — #267 forensics: H1 (KFD page-reclaim queue eviction) leads; occ-1 reclassified OOM (2026-11-16, 12:0x–12:2x)

Read-only forensics lane (GLM explorer) over the three artifacts + source. Findings:

- **Ledger correction**: occurrence 1 (11-15 23:15 prod death) was NOT #267 — the crash dir shows a boot OOM (cudaMalloc fail, VRAM held by exiting engine) → supervisor restart → serving fine. Genuine #267 events: F8G-PENALTY (layer 24), F6G-BOOTFILL (layer 34), F8G-P2 (watchdog stage `request -1`) — 3 events, ALL at the first serve request after boot.
- **"The GPU finished in 13883 ms" solved**: the release path's FIRST `cudaStreamQuery` call itself blocked 13.9 s inside the driver, then returned done (t0 taken before the call). Driver-level block, not a spinning kernel — consistent with the 18as rule ("wedged on real work, never the flags").
- **F8G-P2 never staged a window**: the verify dump showed `0 tokens, rang 0` — `stage_inputs` would have set `last_t_`; the host sat ~59 s in the request-setup span (stage `request -1`, between generate.cpp:18650 and :20261) — an instrumentation blind spot.
- **Ranked hypotheses**: **H1 — KFD userptr/page-reclaim queue eviction** (driver suspends GPU queues while the kernel moves/reclaims GPU-mapped host pages; first request = only moment where whole-arena registration (46.8 GiB / 12.3M pages, register-last) + mass expert fault-in + reclaim coincide; the tree itself documents the class in docs/AMD_HIP.md:373 + gpu_pause_time.hpp, and the KFD `evicted_ms` counter printed NOTHING in occ-3 — unreadable on this ROCm 6.1.0). H2 hw-queue wedge (needs dmesg/error codes), H3 swapped-page setup block (occ-3 only), H4 B6 queue lottery (WEAK — B6's 10 boots, 5 primed: zero #267), H5 HIP launch deadlock (refuted for occ 2/3 by progress evidence).
- **Discriminators**: (a) make `evicted_ms` readable / print KFD proc sysfs; (b) one gates arm with `HSA_USERPTR_FOR_PAGED_MEM=0` (community workaround for exactly this class); (c) `STRATA_VERIFY_TRACE=1` env in a gates arm (ring dump + GPU breadcrumbs); (d) release-path first-query latency logging.

**Action**: log-only instrumentation lane dispatched on hc-q8-gfx906 (5 patches: release first-query timing, stall-report KFD counter/sysfs, unconditional diag on 40 s timeout, `request: setup` progress beat, VERIFY_TRACE doc) — next gates window carries the discriminators. Plan the H1 A/B arm (`HSA_USERPTR_FOR_PAGED_MEM=0`) into the next boot matrix.
## Round 18bz — F2 + F7ab + #267-instr all landed on hc-q8-gfx906 (2026-11-16, 12:1x–12:5x)

Three parallel GLM lanes delivered, reviewed, rebased clean, merged, pushed (through `4dc021c68a`):

| lane | commits | host validation | deviations (accepted) |
|---|---|---|---|
| #267 instrumentation | `234780d8dd` | golden build exit 0, 5 log strings verified in binary, zero behavior change | patch 5 skipped (no debug-env registry exists); noted same timeout pattern at verify.cpp:4982/:5254/:5277 (verify-batch) left untouched |
| F7ab SORT | `dd0c0299cb` | file_expert_source_test PASS ×2 incl. offset-order + O_DIRECT-vs-mmap byte gate (exercised, not skipped); 6 guards PASS | ODIRECT scoped to file-source boots (arena reads RAM — gate logs inactive); pread via read_direct's existing windowing (plan's own mitigation); unaligned-fds refusal guard |
| F7ab ODIRECT | `8f1ad4adb1` | (same build) | (same) |
| F2 xq ring | `528b51ee…4dc021c68` (6) | expert_helper_xq_ring_test PASS (27 checks incl. whole-frame byte identity, folded poison-proof, multi-window stale-seq); 9 adjacent host tests PASS; guards PASS | consumer gate via helper_tier_p (plan's remote_opt_ never sees the socket tier — plan bug, correct fix); plumbing via d.plan sink (already the verifier's GpuPlanSink); stage_batch second reset site; QFUSE leg deferred to GPU step c |

All gates default OFF (env-unset = today's byte-identical path); OFF-path graph capture identical (F2), F7c timing spans preserved (F7ab). Engine-gate step c for both lanes + the #267 discriminator arms packaged in `v0235_gates.sh`: F2 parity binaries on GPU, F2 KL ×3 + helper reqs/frame, F7ab 8-boot interleaved distribution (ctrl/sort/odirect/sortodi) + deciding judges, HSA_USERPTR_FOR_PAGED_MEM=0 arm + VERIFY_TRACE leg, wikitext continuity leg. Runs after the p1 window closes on a fresh golden pin at the merged HEAD.
## Round 18ca — HARNESS CONFIG BUG: "v0234" qq gates silently ran the v0233 engine; gates re-run (2026-11-16, 12:5x–13:0x)

**The bug**: `quant_quality.py` builds the engine command as `[cfg["exe"], "--serve", ...]` — the config's **exe** field is the engine. `p0_gates.sh`'s variant-config step replaced the wrapper inside **args** (where it never lived): a no-op. Every qq run labeled "v0234" — NIGHT-v0234 baseline, F8G-PENALTY(+RE), F6G-BOOTFILL(+RE), F1G-SAMPLER, and both p1-window attempts — actually booted the **v0233** engine via `strata-ldp.sh`. The harness prints `engine: <exe> …` as the first stderr line of every run; nobody checked it. Discovered when the p1 window's live process listing showed the v0233 binary.

**Consequences**:
- F8/F6/F1 "KL PASS" gates from 18bu are VACUOUS (v0233 ignores the gate env vars → gated run ≡ baseline by construction). F8/F6/F1 have ZERO real quality passes on v0234-class code. Artifacts renamed `*.INVALID-v0233wrapper` (kept for forensics).
- STILL VALID (different machinery): F6 ladder + VERIFY_COMPLEMENT ×4 boots and boot-fill rates (lane boots via make_engine_variant.sh, which rewrites config exe correctly); B6 A/B; A3; MMVF 3/3 (MMVF code IS in v0233 — real gates); all host-side tests.
- NIGHT-v0234.tsv is a valid *v0233* reference under a misleading name.
- F8G-P2's morning #267 death = genuine v0233 flake data (occurrence ledger unchanged; and the 13:0x F8G-P2 p1-attempt died #267 at layer 23 too — 5th event, again first-request, again v0233-wrapper boots).

**Fixes**: qqNIGHT-v0234*.json exe rewritten to the real wrappers; p1_gates.sh + v0235_gates.sh create configs by setting exe AND guard every run's `engine:` stderr line for the expected wrapper (window aborts on mismatch); p0_gates.sh superseded. Rerun window (RUNNING): NIGHT-P1V baseline + F8G×3 + F6G×3 + F1G×2 + MMVF×1 belt-and-braces on the real v0234p1, then WIKI-234P1V + WIKI-233 wikitext PPL, F7c boot-line check.
## Round 18cb — #267 cluster on the real v0234p1 qq boots (2026-11-16, 12:59–13:4x)

The round-3 p1 window (real v0234p1, exe fixed + guarded) hit a #267 CLUSTER: 8 of 9 qq boots died at the first serve request (`verify: timed out at layer {0,1,3,4,15,23}`); only F6G-P1V-2 completed (~6.5 min full corpus) — but NIGHT-P1V died, so no baseline exists and the window produced zero comparable gate data. Occurrence ledger now ~14 events today across v0233/v0234p1.

**Shape correlation (the signal)**: qq-wrapper small-config boots (fresh engine per run, 46.8 GiB register-last from cold page cache, --max-context 1512, expert-cache 1) die at ~40% (v0233) to ~90% (v0234p1, post-reboot cold box). Lane-launcher prod-shape boots (B6's 10, F6 ladder's 5+, A3) — zero deaths all day. This matches H1 (KFD userptr/page-reclaim queue eviction at first request): the qq shape maximizes first-request reclaim pressure; the prod shape (warm supervisor, big context) does not.

**Actions**: v0235 golden build started at merged HEAD 4dc021c68a — carries the #267 instrumentation (release first-query latency, KFD sysfs, unconditional diag, setup beats), so any further stall self-diagnoses. v0235_gates phase 2 (KL gates) now sets `HSA_USERPTR_FOR_PAGED_MEM=0` EQUALLY on baseline and gated runs (the documented community workaround for the reclaim class; numerics-neutral memory-path env) — phase 4 still runs the explicit lane-boot A/B. Wikitext standard runs stay on the clean env (no workaround) — the standard must match prod shape; deaths there mean retry.
## Round 18cc — p1 window: 10/11 qq boots dead (#267 now near-deterministic under memory pressure); v0235 window launched (2026-11-16, 12:59–13:55)

**Round-3 p1 results**: only F6G-P1V-2 completed (full corpus ~6.5 min — the corpus CAN pass on v0234p1); NIGHT-P1V died at boot-1, so no baseline and zero comparable gates. 10/11 qq boots died first-request #267 (layers 0–46) across v0234p1 AND v0233 (WIKI-233 died at layer 33 after passing segment 1). Death-rate trajectory across the day: morning ~25% → midday ~40% → afternoon ~90%, tracking free-RAM decline (golden builds + 3 worktree build dirs + page-cache churn; occ-3 dump already showed 8.5-9 GiB free / 2.4 GiB swapped). Strongest H1 (KFD reclaim) correlation to date — the qq shape (fresh engine per run, 46.8 GiB register-last, 1-slot cache, cold-ish pages) maximizes exactly the pressure H1 needs.

**F7c qq-boot fields explained (not a fix failure)**: qq boots fill ONE slot (~2 MB pinned — `pinned_mb 2`) so phase times are genuinely sub-0.1 s and `%.1f` prints 0.0 legitimately; aggregate MB/s prints real rates (235–2787). Real verification = prod-shape lane boots (10 GB fills) in the v0235 window.

**v0235 window RUNNING** (bin 2c1ff967 @ 4dc021c68a — instrumentation inside): phase 1 F2 parity binaries; phase 2 KL gates with `HSA_USERPTR_FOR_PAGED_MEM=0` on baseline+gated equally (H1 workaround — if boots survive, H1 is effectively convicted AND the gates complete); phase 3 F7ab 8-boot distribution (lane boots, historically #267-free) + F7c verification; phase 4 #267 lane A/B + VERIFY_TRACE leg; phase 5 wikitext (clean env; retry-on-death is acceptable — standard must match prod shape).
## Round 18cd — v0235 window adjudication: F7ab null measurement; #267 localized to staged-never-launched; registration hang (2026-11-16, 13:52–15:5x)

**v0235 window** (bin 2c1ff967 @ 4dc021c68a, instrumentation inside), closed by hand during phase 5:

| leg | result |
|---|---|
| Phase 1 F2 parity | DID NOT RUN: xq_triple_parity exit 77 (cudaGetDeviceCount saw 0 devices under wrapper env), gr_parity exit 127 (target never built). Rerun queued engines-down. |
| Phase 2 KL gates ×4 | ALL DIED first-request (#267 layers 0–46 / silent None) **with HSA_USERPTR_FOR_PAGED_MEM=0 set on all** — the userptr workaround does NOT prevent the wedge; H1-as-userptr falsified as sole cause. No baseline → no gate data. |
| Phase 3 F7ab distribution | **NULL MEASUREMENT**: every gate boot logged its named fallback — ODIRECT "this boot fills from the RAM arena, not the pack", SORT "the source cannot order these pairs by pack offset". Prod-config boots fill from the arena → both gates structurally inactive. All 8 boots = same effective config: 511/1126/1199/3108/3123/3114/539/1121 MB/s = 8 more lottery samples. "ODIRECT winner" retracted — luck. F7ab needs file-source boots (the #267-prone shape) → measurement blocked on #267. |
| Phase 3 judges | sortodi 346/20.2/15.9 vs ctrl 446/25.5/23.5 — lottery band, no signal (arms identical). |
| Phase 4 H267USERPTR judges | 463.3 fill / 23.8 warm — in band, no runtime cost; but does not fix qq-shape deaths. |
| Phase 4 H267TRACE | **GOLD**: survived boot + first request + several segments, then wedged MID-DECODE. #649 trace: window 0 = all 48 layers clean RANG/SERVED/SYNCED; window 1 = STAGED ("WINDOW ... seq 0 flag 0") and **NEVER LAUNCHED** — no LAUNCHED event, GPU idle (stream queries return done in 1 ms), 32/32 threads asleep, 0 D-state. Also: wedge is NOT first-request-only (README's "long drives" wording confirmed). |
| Phase 5 wikitext | WIKI-234V5 killed by hand: boot registration FROZE at 4032/4480 ranges (zero page faults in 8 s; first 21 GiB took 3 s, then 84 s for one span) — the documented register-last hang class, and it has NO watchdog (stall report only fires during requests). |

**The picture**: two hang shapes, one family — (a) `WINDOW staged → never LAUNCHED` (the span between verify.cpp:3604 and :3655/3645 is a memset on a mapped word + adapt-gate + graph launch; prime suspect: the launch call itself never returning), (b) register-last range registration freezing. Both are driver-adjacent calls on GPU-mapped memory with the GPU idle. Source walk dispatched (launch path, HGL wrapper, registration API, shared locks); diagnostic kit identified in-tree and compiled into v0235: STRATA_LAUNCH_PROBE=1, STRATA_STALL_STACKS=1, STRATA_VERIFY_TRACE=1, STRATA_VERIFY_ASYNC_LAUNCH=1 (helper-thread launch + existing hang-release callback = mitigation candidate). Next GPU use (debug-only, per operator): one instrumented child-process boot driven to a wedge, stacks captured (yama allows child ptrace; direct attach blocked).

F7c status: h2d_s verified live on 10 GB fills (3.2–19.4 s, tracks boot class); pack_read_s still 0.0 — read-path instrumentation gap queued. Prod restore running post cleanup (stale lane build dirs deleted, disk 83G free). KL gates + F7ab measurement + wikitext standard all parked behind the #267 fix.
## Round 18ce — #267 wedge family root-cause day: D-state registration, destroy-spin, stdin protocol, patch exonerated (2026-11-16, 16:00–17:2x)

Dedicated diagnostic boots (own child processes → ptrace-legal gdb; kit: diag267_boot.sh / diag267_drive.py / diag267_exit.sh + diag267.json, bench @ cbdba4b531).

**1. Boot-registration wedge — mechanism captured (twice + 1 watchdog-kill).** Main thread D-state cycling `svm_range_set_attr` ↔ `folio_wait_bit_common` (amdgpu KFD SVM ioctl; hipHostRegister→ROCr→KFD), all other threads futex-parked, one in kfd_wait_on_events. v0236's per-range timing (f9027b451c) exposed the continuum: tail ranges cost 4–27 s EACH (boot-3: range 4473 = 27 s; registration 596 s total, 4480/4480 ok) — the freeze (4032/4480 = last-10% progress line, not a magic index) is the stochastic extreme. `kill -9` on the D-state wedge PENDS ~90 s then delivers — no reboot needed for this class. Note: 18cd's "same range index twice" was the last 10% progress print, not a structural boundary.

**2. Teardown-wedge — new family member, stack captured.** diag267-3 post-READY: main in `~Verifier` → libamdhip64 → sched_yield spin. Walk verdict (verify.cpp:727-798): the dtor's FIRST call is `cudaStreamSynchronize(cs_)` :738 — spun over the still-live 46.8 GiB whole-span registration (round-18k documented hazard: driver sync over live single-call registration). ~PinnedArena (unregister) runs only AFTER ~Verifier. Unifying picture: ONE lost GPU completion (never-signaled AQL/KFD event) makes launch (:3655/:3714), stream-sync, and destroy ALL wait forever; the giant registration is the common denominator and aggravator.

**3. The "silent post-READY death" of diag267-3/4/5 was a HARNESS BUG, not box degradation.** The engine's serve mode is a STDIN PROTOCOL (generate.cpp serve loop 18897+: `GEN <max_new> <ids csv>` / answers `DONE|ERR` lines; `QUIT`; EOF → silent break 18924 → return 0 at 24389). The HTTP banner ("ready: http…") is printed by serve/server.py (10595) — a PYTHON wrapper that bridges HTTP↔stdin; raw-engine boots never print it. My drivers gave the engine EOF'd stdin → instant silent clean exit. quant_quality.py drives the same protocol via pipes (tools/quant_quality.py:147-183). Driver v2 (diag267_drive.py) speaks GEN/DONE with answer-timeouts + wedge forensics + gdb-exec.

**4. f9027b451c (log-only observability) exonerated twice**: (a) control boot v0235+known-good-env reproduced the silent exit (before the EOF discovery, this looked like degradation); (b) the teardown walk verified no patch code touches main's serve scope, destruction order, or exception paths.

**5. KFD proc table clean after wedge-kills** (no cross-boot residue at sysfs level); topology intact (both vega20 + renoir). One-GPU "GPU 0: AMD Radeon VII" line is NORMAL for the qq shape (all window boots identical).

**Fix shortlist (from fix-design walk + today)**: (1) cudaDeviceSynchronize both devices immediately before register_whole (generate.cpp:17550; no sync-over-live-registration hazard — nothing registered yet); (2) registration shape: relief's range-by-range (18t proven) or capped/skip-tail via existing refusal-degrade; PIN_GIB=none boots in 41 s and exits cleanly (existence proof the wedge lives in the registration path); (3) dtor hardening: bounded/async destroy + boot-registration phase added to watchdog coverage (close() at expert_source.cpp:6651 can wedge identically); (4) request-class: still needs the driven capture — diag267-7 in flight (v0236, full kit, wikitext GEN requests).
## Round 18cf — request-class dissected: stall→release→silent death; crash lives in the post-release recovery (2026-11-16, 17:1x–17:3x)

diag267-8 (PIN_GIB=none, v0236, protocol-driven, boot 39 s): req#1 (GEN 1, 999 ids) stalled 179 s, engine DIED mid-request (zombie; the window's "answered None" class = this death). The engine's own stall report (f9027b451c instrumentation, first live firing) shows the full anatomy: all 32 threads S (main wchan ?, one kfd_wait_on_events, stdin on anon_pipe_read), 0 D-state, 632 MiB swapped, 3784 major faults (the unregistered arena thrashing — ulimit -l is unlimited, so the swap source needs a follow-up), stalled layer's CPU phases crawled (hc-read0 243 ms … router+ring 1273 ms at layer 32), then the watchdog released the window's GPU waits: "cs_ first query 2170 ms (returned done)" — "released … the GPU finished in 2295 ms". Then: instead of the normal "ERR verify … (#267)" answer (which REGISTERED boots produce, cf. F2G-P2/P3), the engine exited SILENTLY. **The crash is in the post-release recovery path** (verify.cpp timeout handling after release_gpu_waits, under the unregistered/UMA arena mode). Trace context: window 0 died between SERVED(layer 45) and step-46 staging — no LAUNCH-ENTER was ever reached, so this sub-class is NOT the graph-launch call itself.

Hunt in flight: diag267-10 = engine as gdb's inferior from start (pty protocol, 2700 s boot budget — gdb's thread-event stops slow the boot ~15×; first attempt at 900 s timed out mid-fill). The crash frame will come from gdb's SIGSEGV catch. Also this round: driver v2 (diag267_drive.py, GEN/DONE + forensics + gdb exec) and v3 (diag267_crash.py) committed; orphan `sleep 300` watchdog sweep added (an earlier run's watchdog SIGKILLed the diag267-6 engine mid-crawl — exit 137 mystery solved).
## Round 18cg — #267 COMPLETE: launch stall measured (62.7 s doorbell), death = watchdog abort; crashrec technique (2026-11-16, 18:1x)

diag267-11 (PIN_GIB=none, v0236, native speed, **crashrec.so LD_PRELOAD** — new 30-line fatal-signal recorder, gcc-built, self-tested): req#1 answered in 63 s with "ERR verify: an earlier window never finished on the GPU (#267); restart the engine"; req#2 → None → engine died with **SIGABRT: abort() called from main::{lambda()#76} — the request watchdog thread** (crashrec backtrace: abort ← strata+0x2500554 ← std::thread root). Lines before the abort, verbatim gold:
- `strata tdr-probe: window 1 T=5: the first doorbell (layer 0) seen 62751.81 ms after the launch began` — **THE LAUNCH STALL, DIRECTLY MEASURED**: window 1's graph launch took 62.75 s to reach its first layer-0 doorbell, GPU idle throughout.
- `#267 release: cs_ first query 5 ms (done), copy_ 0 ms (done)` + `GPU finished in 128 ms` — the GPU was NEVER busy; the stall is pure submission-path (ROCm 6.1.0 ROCclr/AQL/KFD). 62.7 s ≈ a ~60 s timeout somewhere upstream (queue eviction recovery?) — the H267TRACE forever-case is the same stall without the eventual timeout.

**The unified #267 picture (all four faces, all directly evidenced):** (1) request stall = submission-path launch delay (60 s … ∞), GPU idle; (2) silent None death = engine detects stall → releases → answers ERR "restart the engine" → next request hits the poisoned window state → watchdog abort() (deliberate, by design); (3) boot freeze = registration D-state in svm_range_set_attr; (4) teardown spin = dtor sync over live registration. **Fix lane: window-reset recovery (recreate stream/graph execs on poisoned state instead of abort) — the release path already proves the GPU is fine; plus boot mitigations (quiesce-before-register, relief-shaped registration) and dtor hardening.** Technique banked: crashrec.so + protocol driver (v2) = native-speed fatal-signal backtraces via LD_PRELOAD; gdb-inferior (v3) too slow to boot (thread-event livelock).

Prod down since ~16:39; restore queued behind this result. diag artifacts: out/diag267-{1..11}, bench @ (next commit).
## Round 18ch — fix v1 insufficient (wrong hook sites), v2 recovery works, v3 target = watchdog race (2026-11-16, 18:3x–19:0x)

- v0237 (6310f35f0f, hooks at doorbell/XQ-ring/~NextLaunch timeouts): diag267-12 = same death, ZERO recovery lines — the actual path is the LATE-window refusal (launch blocked 62549 ms then completed; doorbell timeout never fires because the doorbell loop starts only after the launch call returns).
- v0237p1 (7ff296cbdb, refuse_or_recover at ALL five refusal sites + six sibling timeouts): diag267-13 = **recovery works end-to-end** — req#1 answered "the late GPU window was reset (#267); retry the request"; req#2's OWN 62847-ms stall also released (drained, GPU idle) + T=5 graphs destroyed + reset + answer emitted — but the request watchdog (lambda #76, abort site strata+0x2500794) still fired: its ~60-s no-progress threshold is SHORTER than the ~63-s ROCm launch-block. Recovery machinery vs watchdog race — the watchdog won by a second.
- Consistent constant: the launch block is ~62.5–62.9 s across four independent runs (62751/62549/62847 + the first) — a ~60-s driver-side timeout/backoff, launching thread S in hrtimer_nanosleep inside hipGraphLaunch, then it completes on its own. The engine-side recovery is the right fix regardless of the upstream cause.
- Amendment 2 in flight: recovery-epoch atomic bumped by every recovery; watchdog holds (resets its stall timer) when recovery advanced since its last look; aborts only on a stall recovery is not resolving.
## Round 18ci — FIX VALIDATED per sub-class; v0237p2 is the adoption candidate (2026-11-16, 19:0x–19:2x)

Validation runs (diag267-12..15, protocol driver, crashrec armed):
- 12 (hooks v1): recovery never fired — wrong sites (doorbell timeouts can't cover a BLOCKED launch call).
- 13 (v2, refuse_or_recover at all refusal sites): recovery works end-to-end on the ~62.8-s self-resolving stall (graphs destroyed, reset, retryable answer emitted) — but the 60-s watchdog aborted first (race).
- 14 (v3, epoch-hold): race went the other way — abort check at 60 s precedes any possible recovery (launch still blocked).
- 15 (WATCHDOG_S=120, no rebuild — env knob): the stall was the FOREVER sub-class this time — no recovery possible in-process; clean abort at 120 s, no zombie, no D-state. Correct behavior: that abort is designed for server.py to restart the engine.

**The two request-stall sub-classes, both handled by v0237p2 (+STRATA_WATCHDOG_S=120):** (1) self-resolving ~63-s driver stall → in-process window reset, request retries, engine lives (proven 13); (2) forever stall (the #649 signature) → clean 120-s abort → server.py restart loop (prod's existing design). Boot wedge → 5-min registration watchdog abort + retry (Part 2). The consistent ~62.5–62.9-s constant across four runs = a ~60-s ROCm/driver-side timeout inside hipGraphLaunch (thread S in hrtimer_nanosleep) — upstream cause open, engine-side handling complete.

Adoption candidate: /mnt/srcds/Strata/v0237p0/rpcw-hip/strata sha bb4e20110af3… (engine hc-q8-gfx906 @ e262fc7bed: recovery + epoch-hold + quiesce-before-register + boot-registration watchdog; f9027b451c instrumentation included). Env for this box: STRATA_WATCHDOG_S=120.
## Round 18cj — #267 ROOT CAUSE: memory-pressure arena eviction; cgroup protection; control bit-exact (2026-11-16 evening)

**Root cause of the #267 request-stall family**: desktop RAM spikes (browser launch ≈ multi-GiB burst) make the kernel reclaim from the biggest RSS process — the engine — whose 46.8-GiB file-backed arena (pack page cache) gets dropped/swapped. The "GPU stall" is the GPU waiting on refaults at ~56 MB/s random-4K (the measured ple-io rate); the consistent ~62.5–62.9 s ≈ a fixed multi-GiB window at that rate. Evidence: 20.4 GiB swap-in during a live stall, expert-cache hit 0.0%, prefill 1000 tok in 85.5 s, and direct operator observation ("the moment i start waterfox, it stops processing on the gpu"). Explains the evening-vs-overnight death correlation (operator at desk vs not) and why every binary/shape was affected. The ROCr launch-block forensics were the in-driver symptom of data-not-arriving, not the origin.

Kernel: operator rebooted to 7.2.9-x64v3-xanmod1 mid-evening. Pre-reboot old-kernel evening: v0237 gates try-1 death ("answered None" mid-recovery, stderr clobbered by retry) and v0234R hang (operator record).

**Control rerun (NIGHT-v0234R, exact last-working combo qqNIGHT-v0234.json + strata-ldp-v0234.sh) on the new kernel: COMPLETE** through an active browser thrash — 5/5 segments, 4995/4995 rows, top1 100.00%, **KL 0.00000 vs the 09:37 champion**, PPL 3.924 = 3.924. First fully trustworthy qq reproducibility pass on this box: slow ≠ wrong; thrash costs time only.

**Cgroup protection (now in v0237_retest.sh legs + launch-v0237.sh)**: `systemd-run --user --scope -p MemoryMin=48G -p MemorySwapMax=0` around every engine boot subtree. Lessons, all evidenced: (1) memory.min protects file-backed pages by charge → the drop-clean-pack-cache release valve closes → a browser burst during the engine's 55.3-GiB boot peak loses the swap-write-latency race → global OOM at 19:59:22 took the engine (biggest RSS; min shields reclaim, never the killer); (2) `OOMScoreAdjust=-1000` is rejected by the USER manager (unprivileged cannot lower oom_score_adj) — root helper provided: gpu-rounds/oom-protect-engines.sh; (3) fixed `--unit=` scope names collide with failed units ("already loaded") — use auto-generated names + `systemctl --user reset-failed`; (4) between legs the browser owns ~12 GiB uncontested (steady-state engine ≈ 48 GiB); only boot-peak × browser-launch bursts are the collision.

Harness fixes this round: retry demands >100 TSV rows AND zero "answered None"/"timed out" markers (an 810-row OOM partial had passed the old `-s` check); per-try stderr backups (*.tryN.stderr.txt) so retries stop destroying forensics; stale partial TSVs (F2G-P2/P3 520/510 rows from the v0235 window) purged. Meta-trap: a pkill -f bracket pattern still self-matched because the same command contained the literal filename elsewhere — keep the pattern the only occurrence in the command (one patch shell killed itself this way).

Retest window (relaunched clean): NIGHT-v0237 → F2G-P1..3 → WIKI-234V7/WIKI-233V7 → prod restore on v0237 (cgroup-wrapped). Still open: gr_parity build (FetchContent llamacpp source was deleted with f1-tests — needs re-fetch), xq_triple_parity exit 77 (needs the full GPU env, not just LDP chain).
## Round 18ck — BISECT: the #267 recovery commits themselves break the first verify window; prod restored on v0236 (2026-11-16, 20:0x–20:3x)

Post-kernel-swap death-loop resolved into three distinct causes, all fixed except the last:
1. swap.max=0 on the engine scope made the ~50-GiB anonymous boot staging unbootable (every boot OOM'd ~20 s in) — removed; boot spill to swap is required on a 62-GiB box. MemoryMin=48G alone remains as the arena protection.
2. STRATA_MEMORY_LIMIT_MIB=51200 added to both launch paths — the engine's RAM guard plans against total RAM by default (floor 2.5 GiB), which is how it raced the browser.
3. After those: still dying, but NOT OOM — bisect verdict: **v0236 PASS (5 segments, 4995 rows, 35–37 s/segment) vs v0237 FAIL at the FIRST verify window** ("timed out at layer 0, cudaStreamQuery 600, device not ready"; boot itself perfect: registration 30 s, window captured 1696 ms; "the GPU rang 0; plan (A) 0, copies (B) 0" = staged-never-launched). The v0236→v0237 delta = the three #267 recovery commits (6310f35f0f→7ff296cbdb→e262fc7bed). Fixer session ses_e1a9d7a11ffeGrEUv0wsFy85SW re-tasked to hunt it (analysis only).

Also this round: v0234 control re-confirmed THREE times bit-exact (NIGHT-v0234R through a browser thrash; R2 clean 35–40 s/segment) — the qq harness + old binary are solid on kernel 7.2.9-x64v3-xanmod1; registration is 30 s on this kernel (was 300–420 s). The memory-eviction root cause of #267's thrash face stands (20.4-GiB swap event captured live); the GPU-side first-window stall is a separate, engine-side regression introduced by the recovery code.

PROD: restored on v0236 (launch-v0236.sh = champion config + MemoryMin scope + STRATA_MEMORY_LIMIT_MIB=51200; binary /mnt/srcds/Strata/v0236p0/rpcw-hip/strata). v0236 contains the full champion branch incl. merged F2/F7ab code. Gates remain blocked until the recovery-commit fix lands and validates.
