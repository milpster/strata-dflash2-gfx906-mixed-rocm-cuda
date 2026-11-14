# ud-iq3_xxs spec sweep + optimization ranking (2026-11-14, 17:02–17:47)

Arms via `tools/spec_sweep_arm.sh` (edit --spec/--spec-min-p only, commit, stop-all,
launcher, fill + tg x2 at ctx 442400, ab9_driver, judge lines from
`logs/engine-prod-ud-iq3xxs-v0233.log`). Champion restored from f13c4ce81b at
commit 709f170c0f; prod re-booted and verified (ready 110s, curl answering).

## Per-arm table (all numbers verbatim from judge lines)

| arm | boot-to-ready | PP fill tok/s | TG warm tok/s (3 cached posts) | drafts (cached posts) | reused boundary | determinism |
|---|---|---|---|---|---|---|
| baseline spec5-p0.5 (tonight, operator) | — | 516.3 | 29.1 | 273/404 (68%) | 65536 | not checked (411 vs 422 gen within one invocation) |
| spec4-p0.5 | ~165s* | 120.1 (COLD: 116573 MB storage read, 66272 major faults) | 34.5 / 33.1 / 32.9 | 280/383, 307/457, 261/381 (68–73%) | 65536 | FAIL |
| spec4-p0.65 | 90s | 512.3 | 17.1 / 20.4 / 31.5 | 416/494, 220/277, 212/248 (79–86%) | 65536 | FAIL |
| spec5-p0.65 | 91s | 528.7 | 30.8 / 32.2 / 31.3 | 236/314, 258/326, 209/277 (75–79%) | 65536 | FAIL |
| spec3-p0.5 | 101s | 538.8 | 23.2 / 17.5 / 25.9 | 328/444, 241/297, 277/340 (74–81%) | 65536 | FAIL |

*arm1 boot timed by hand (script's first run had a ready-line detection bug; legs attached live).
Re-prefill first pass (turnaround): all arms ~51.5–55.0 s re-read at 262.9–280.6 tok/s (65536 reused + 14453/14454 read).

## Verdict

- **No valid winner**: all 4 arms failed the within-arm sha256 determinism guard
  (temp 0). The nondeterminism is stack-wide, not spec-specific: generation length
  for the SAME prompt varies 334–680 tokens across posts, and the operator's own
  baseline shows 411 vs 422 within one invocation. Per protocol rule 6 every arm
  is invalidated; promotion of any arm is blocked until temp-0 determinism is fixed.
- Numerical leader (provisional, invalid): **spec4-p0.5**, warm posts 32.9–34.5
  tok/s vs baseline 29.1 (+~4), consistent across all 3 cached posts; PP unaffected
  by spec knobs (arms 2–4: 512.3–538.8, within ±5% of 516.3). Acceptance is NOT the
  throughput driver: spec4-p0.65 had the highest acceptance (79–86%) and the worst
  warm TG (17.1–31.5); per-step draft/verify overhead dominates.
- Champion config (spec5-p0.5) restored and serving. Operator decides promotion
  only after the determinism root cause lands.

## Key judge-line evidence (for the ranked levers below)

- `conversation cache: checkpoint at 16384/32768/49152/65536 is target-only (no legal pair boundary ...)` — checkpoints at multiples of 16384 = `--prefill 16384` chunk.
- `prompt 79989 tokens = 65536 reused + 14453 read in 51.5–55.0 s (262.9–280.6 tok/s)` — cross-prompt restore reuses only to the last completed pair checkpoint (floor(79989/16384)=4 → 65536); same-prompt restore hits the exact anchor (`79984/79985 reused + 5 read in ~220 ms`).
- `the draft ring no longer retains a rewind to 65536 (draft miss: no pair at this checkpoint); mounting target-only with a cold drafter` — first TG window runs cold-drafter.
- `conversation cache: ... snapshot 1804–1813 MiB over the 512 MiB RAM ceiling; parked to disk only` + `disk park streamed 80xxx tokens idle ... in 2.0–7.5 s` every post — `--conversation-cache-ram-mib 512` forces a disk-park cycle between every request.
- `helper=reqs=4745–11813` per post (≈13–14 reqs/token); `foresight swap: ... no free slot` counts balloon 8630→36607 within a leg; `adapt swap-in: ... depth=64` at ~1.1 ms/slot.
- `WARNING: only ~300 MiB of VRAM free at the start of this request (below 512 MiB) ... Add --vram-reserve-mib 832 ... (#2065)` — printed on every arm's fill.
- Cold vs warm fill: 120.1 (116 GB storage, 66272 major faults) vs 512.3–538.8 tok/s.
- Upstream candidates: notes/2026-11-14-residency-and-upstream-recommendations.md section E.

## Ranked levers

### (a) PP
1. VRAM headroom at fill start (config arg): every fill ran at ~300 MiB free VRAM and logged the #2065 warning; raise `--vram-reserve-mib` 576→832 per the warning's own prescription (cheap arm, one boot).
2. gfx906 WMMA prompt-kernel port (upstream #4859, section E "track": +84% PP on gfx1100, same skipped-arch argument) — biggest conceivable PP lever, research-sized.
3. Layer-split pipelined pricing (upstream #4534/#4522, section E "track": 34→48→66.5 tok/s 2-card).
4. Cold-start expert streaming: fill swings 120→512+ tok/s purely on expert-file page-cache warmth; a post-boot GGUF shard pre-read (or wave-2 #5327 arena-from-file-cache/hugetlb, reporter 47→124 tok/s @1M ctx) would make first-request PP honest.

### (b) Infill→TG turnaround
1. Re-prefill boundary: `--prefill 16384` sets the checkpoint/pair grid; 79989 tokens → 65536 reusable + 14453 re-read (51.5–55 s). `--prefill 8192` would move the boundary to 73728 (≈6261 re-read, ~-50 s) at the cost of 2× checkpoint overhead during fill — untested, one boot each. (`--conversation-cache-min-tokens 2048`, `--conversation-cache-park-gain 4096`, `--prompt-cache-spread` govern retention/parking, not the grid.)
2. Re-read speed: the 14453-token re-read runs at ~280 tok/s vs 512–539 for a fresh full read (swap-in batches only 5927/6109 slots vs 54379 pre-staged on fill). Raising `STRATA_SWAPIN_DEPTH` (64) or `STRATA_FS_SLOTS` (3,2 → 4,3) to pre-stage more of the tail during the cached prefix replay.
3. Kill the per-request disk-park cycle: `--conversation-cache-ram-mib 512` vs 1.8 GiB snapshots parks every 80k conversation to disk (2.0–7.5 s each way). 2048 MiB (RAM budget permitting) removes the cycle from every turn.

### (c) Warm TG
1. **Temp-0 determinism first** (correctness gate for any spec tuning): same prompt, temp 0 yields 334–680 tokens and differing shas across posts/arms; expert-source variance (`NNN more read by the GPU over PCIe or from another GPU`) + near-tie argmax flips. Section E wave-1 `f225844d19` MMQ row-id OOB fix ("verified OOB read in our exact gfx906 prefill path, STRATA_DENSE_MMQ=1") is the top suspect — OOB reads can perturb logits directly. Zero-conflict cherry-pick.
2. Spec/min-p (after determinism): spec4-p0.5 provisional leader 32.9–34.5 vs 29.1; high min-p (0.65) raised acceptance but slowed decode (17.1–31.5) — per-step overhead dominates, don't chase acceptance.
3. Foresight-slot pressure: warm TG degrades on long generations ("no free slot" 8630→36607, over-budget 1406→5808); helper reqs ~13–14/token (4745–11813/post). `STRATA_FS_SLOTS` / relief hot 15000 / arena-cap 22000 rebalance; upstream wave-3 peer-tier ADAPT_OVERLAP/ADAPT_FETCH targets this but conflicts with local lanes.
4. KV int8 resident budget: `--kv-resident-mib 4096` vs measured 1.8 GiB KV snapshot at 80k — fits now, but 180k-class contexts (~4+ GiB int8) would spill mid-conversation; raise budget or cap context per the #2065 warning.

Raw driver records: bench/ab-v0210/rpcprof/out/spec-sweep-20261114/<arm>/{fill,run1,run2}/.
