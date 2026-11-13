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
