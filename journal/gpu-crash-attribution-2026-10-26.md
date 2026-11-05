# GPU crash attribution — last 4 strata crashes, GUAM, 2026-10-26

Question: which GPU caused the last ~4 strata crashes?

## Card map (lspci + rocm-smi)
- CUDA0 = 04:00.0 — Radeon VII (rocm card0), runs layers 0-31
- CUDA1 = 09:00.0 — Radeon VII (rocm card1), runs layers 32-47 + expert cache split; the card that hard-wedged 2026-10-22 14:07 (see logs/gpu-crash-20261022/)
- 0a:00.0 = Cezanne iGPU (desktop), 01:00.0 = RTX 3080 Mobile (not used by strata)

## The last 4 kernel-attributed crashes

| # | When | GPU | Kernel signature | Victim run |
|---|------|-----|------------------|------------|
| 1 | Oct 25 17:44 (boot -3) | CUDA0 04:00.0 | `qcm fence wait loop timeout expired` → `Failed to evict process queues` → BACO reset → `VRAM is lost` → ret -5 | v023 run 13:19 stopped 17:56, machine down 17:56:49 |
| 2 | Oct 26 10:35 (boot -2 end) | CUDA1 09:00.0 | engine stalled from 08:24:56, then `ring page0/page1 timeout` → reset -19 ×2 → `GPU Recovery Failed: -19`, SMU `Failed to send message` | run 18:09 (Oct 25) never finished starting; reboot 10:36 |
| 3 | Oct 26 10:53 (boot -1) | CUDA0 04:00.0 | identical qcm-fence signature → BACO → `VRAM is lost` | engine death #1 of run 10:38 (wrapper retried) |
| 4 | Oct 26 12:29 (boot -1 end) | CUDA0 04:00.0 | `Fence fallback timer expired on ring comp_1.0.0` → reset(7) → `device wedged` while engine tok/s collapsed 23.8→3.6 | run 10:38 log stopped 12:29:57, machine down 12:30:16 |

Score: 3 of the last 4 = **CUDA0 (04:00.0)**; 1 = CUDA1 (09:00.0).

## Older history for contrast
- Oct 22 14:07: 09:00.0 hard wedge (SMU dead, reset -19, recovered via gpu-sbr.sh SBR)
- Oct 25 02:00 (boot -4 end): 09:00.0 ring page0/1 timeouts → recovery failed ×3 → reboot (killed the 21:02 Oct 24 run era)

So 09:00.0/CUDA1 is the *chronic* offender (SMU deaths, unrecovers), but the *recent cluster* (3 of last 4) is 04:00.0/CUDA0 with a milder BACO-reset/VRAM-lost signature.

## Not GPU-attributed
- Oct 26 12:32 run (current boot): first engine attempt died with NO amdgpu kernel event; wrapper restarted engine 14:04, currently up. Log: logs/prod-swift15-dflash-w4-q4_0-20261026-123206-11692.log

## Addendum — the very last crash (engine attempt started 14:04, died ~14:53, Oct 26)

- Kernel: **zero amdgpu events in boot 0** — no ring timeout, no reset. Not a driver-level GPU death.
- Engine stall report (issue #29, exit -6 SIGABRT): stage `request: parking the previous conversation -1`, 60 s no progress.
  - `verify window: 5 tokens at position 8655, host at layer step 32; the GPU rang 32; flags: served 32, plan (A) 32, copies (B) 32` → pipeline frozen exactly at the **layer-32 boundary = the CUDA0→CUDA1 hand-off**. Layers 0-31 (CUDA0) all served; host idle 89 s; 0 threads in D state; no OOM (8.3 GiB RAM available).
  - Teardown `#267: released the verify window's GPU waits: the GPU finished in 0 ms` → the fence was not pending at release; could be a missed completion signal (engine-side sync bug) as easily as a stalled card.
- Attribution: **CUDA1 = 09:00.0** (wait on its first layer / hand-off side), medium-high confidence — same card as the 10-22 #29 crash. Not kernel-proven this time.
- Aftermath: first restart at 14:54:38 died `cudaMalloc OOM: 612 MiB of 16368 free` (old engine hadn't released VRAM); second at 14:54:53 is the engine now running.
- This boot-0 run (12:32 wrapper) has had 4 #29 aborts so far: decode ×2, 0-tokens ×1, parking ×1.

## Addendum 2 — crash #6 (engine started 14:54:53, died ~15:22, Oct 26)

- Kernel: still **zero amdgpu events** in boot 0. Not driver-visible.
- Wrapper: `stopped unexpectedly (exit code None). Unrecoverable native verification failure: verify: timed out at layer 3; its GPU waits were released but the GPU did not finish within 5 s (#267)` — different family from the 14:53 crash (#29/60 s no-progress); this is the #267 verify-timeout that also struck twice on 10-22.
- Engine log ends mid-flight after a series of successful 85k-103k-token long-context requests (6 checkpoints each); no stall report — hard stop.
- Attribution: **CUDA0 = 04:00.0** — layer 3 is inside CUDA0's range (split: layers 0-31 CUDA0 / 32-47 CUDA1), and unlike the 14:53 crash the released waits did *not* complete (GPU kept not finishing for 5 s) → genuinely non-progressing GPU work on CUDA0, not a missed-signal artifact.
- Context: long-context workload (90k+ tokens, 790 MiB VRAM free on CUDA0). Kernel stays silent because the engine dies at 5 s while `amdgpu.lockup_timeout=1000000` needs ~16 min.

## Running tally for boot-0 run (12:32 wrapper): 5 engine deaths
- #29 no-progress aborts ×4 (decode ×2, 0-token ×1, parking ×1 — last at the layer-32 boundary → CUDA1 side)
- #267 verify timeout ×1 at layer 3 → **CUDA0**
- Both cards stall under this dflash-w4-q4_0 long-context config; none of the 5 produced a kernel amdgpu event.

## Addendum 3 — crash #7 (engine started 15:23, died ~16:02, Oct 26)

- Wrapper: `verify: timed out at layer 41; its GPU waits were released and the GPU finished (#267)`, exit -15 (wrapper's cleanup SIGTERM after the verdict). Last request had collapsed to 3.6 tok/s.
- Engine log ends with normal summaries at **156k-token contexts** — workload climbed 90k → 104k → 156k across the afternoon.
- Attribution: **CUDA1 = 09:00.0** — layer 41 ∈ 32-47. Variant differs from the 15:22 CUDA0 crash: the GPU **finished after release** → late completion (slow), not a dead wait.
- Kernel: one soft event — `15:33:01 amdgpu 0000:09:00.0: Failed to send message 0x10, response 0x0` (single SMU soft-fail, not the 0xffffffff death spam) — weak corroboration on the CUDA1 side.

## Running tally for boot-0 run (12:32 wrapper): 6 engine deaths
- #29 no-progress ×4 (last at layer-32 hand-off → CUDA1 side)
- #267 @ layer 3 → **CUDA0** (GPU never finished)
- #267 @ layer 41 → **CUDA1** (GPU finished late)
- Both cards stall under escalating contexts; only soft kernel signals. Engine-side timeouts under VRAM/KV pressure, not clean single-card hardware deaths.

## Change — 2026-10-26 16:07: #267 verify deadlines raised 20 s → 60 s

- `src/core/verify.cpp` in /mnt/user/dflash2/Strata-integ: all 5 #267-family wait deadlines (lines ~2843, 3056, 3869, 4070, 4093) `seconds(20)` → `seconds(60)`; comment at ~3825 updated. The 5 s release grace is unchanged (user choice).
- Rebuilt incrementally in build-dfsample-1 (ninja, exit 0). Old binary preserved as `strata.old-20s` (was built 10-24 15:26). Running engine (pid 3004082, started 16:03) keeps the old inode — the 60 s deadline activates on the next engine restart (automatic: the wrapper relaunches on death).

## Addendum 4 — crashes #8 and #9 + the 16:18 restart (2026-10-26, 16:00-16:19)

- ~16:02 — crash #7 (#267 @ L41, CUDA1, late-finish) [wrapper 4736]
- 16:03 — engine restart (old binary)
- ~16:08 — **crash #8**: #29 hang reading prompt batch "chunk from token 152316" [wrapper 4801], no kernel event; in-flight request died 0 tokens / 313 s
- ~16:10 — engine restart
- 16:10:57 — kernel: `0000:09:00.0 Fence fallback timer expired on ring comp_1.0.0` → `GPU reset(1) succeeded` → `device wedged` (**CUDA1, kernel-proven**)
- ~16:12 — **crash #9**: #29 hang reading prompt batch "chunk from token 45820" [wrapper 4849] ← the 16:18 outage was THIS crash's recovery, not a new crash
- 16:18:27 — engine up on the NEW binary (60 s #267), healthy, reading a 157k prompt

## Addendum 5 — crash #10 (engine 16:18 — FIRST on new 60 s binary — died ~16:20)

- Wrapper: `#29 no-progress (exit -6)`: "reading the prompt (batched): **waiting for the GPU (attention, router) at layer 46** of the prompt chunk from token 8192" — layer 46 ∈ 32-47 = CUDA1's range.
- Kernel 16:20:20-21: `0000:09:00.0` re-init + `Fence fallback timer expired on ring comp_1.0.0` → `GPU reset(2) succeeded` → `device wedged` — **CUDA1 = 09:00.0, kernel-proven, second reset in 10 min** (reset(1) 16:10:57, reset(2) 16:20:21; plus soft SMU msg fail 15:33).
- rocm-smi telemetry now returns empty values (queries failing — same symptom family as the 10-22 wedge when the card wedges).
- The card is wedging every ~10 min under this workload. Not a #267 timeout — the 60 s raise is irrelevant to this failure class. Restart at 16:21.

## Addendum 6 — crash #11 (engine 16:21, died ~16:28)

- Wrapper: `#29`: "reading the prompt (batched): waiting for the GPU (attention, router) at **layer 45** of the prompt chunk from token 70396" → layer 45 ∈ 32-47 = **CUDA1**.
- Kernel: silent this time (no reset between 16:21-16:29) — engine-side attribution only, but identical failure class to #10 (attention/router wait in CUDA1's upper layers during batched prefill).
- Pattern: #8-#11 all died in batched prompt reading; #10/#11 both "waiting for the GPU (attention, router)" at layer 45/46 — a reproducible CUDA1 prefill-path stall, not random. Restart 16:29.

## Addendum 7 — crash #12 (engine 16:29, died ~16:33)

- Kernel 16:32:16-18 on **09:00.0**: `Failed to quiesce KFD` → PSP UNLOAD_TA fail → **BACO reset** → **`VRAM is lost due to GPU reset!`** → fence fallback → `GPU reset(4)` → wedged. Third/fourth kernel resets on this card today, first with VRAM loss (the harsh variant).
- Wrapper: `#29 — "finishing the chunk from token 127740"` [line 5269] — mid-prefill when the card's VRAM vanished.
- Aftermath: engine start banners every 3-9 min from 16:32:30 to 17:15 — repeated failed/short-lived startups while the card recovered from BACO. Current engine since 17:15.
- Attribution: **CUDA1 = 09:00.0, kernel-proven**. Card now in 10-22-style instability (resets 16:10, 16:20, 16:32 + SMU soft-fail 15:33 + empty rocm-smi telemetry).

## Addendum 8 — crash #13 (engine 17:15, died ~17:24, restart 17:25)

- Wrapper: `#29 — "finishing the chunk from token 135932"`. Engine-side: this engine served **zero work in its 10-min life** — stall report shows epoch 0, 0 jobs, host idle 542 s, "the GPU rang 0" at layer step 1: hung on its **first prefill** from the start, GPU never executed a single step.
- Kernel: silent (no reset 17:15-17:25). Startup itself was normal (both cards loaded, prompt path up on both).
- Attribution: no direct evidence. **CUDA1 = 09:00.0 by pattern** (all four provable prefill hangs today were its territory; it was 45 min past BACO/VRAM-lost; silent-no-execute is consistent with a half-recovered card) — probable, NOT proven; CUDA0 not excludable from this log alone.

## 2026-10-27 — crash #14 (~13:02)

- Machine was rebooted twice last evening (boots at 22:10 and 22:25); wrapper run since 22:26.
- Wrapper: `#29 — "waiting for the GPU (attention, router) at layer 46 of the prompt chunk from token 65481"` — layer 46 ∈ 32-47 = **CUDA1 = 09:00.0** (same signature as yesterday's #10 @ L46 and #11 @ L45).
- Kernel: silent 12:50-13:03. Engine restarted 13:02. Card `09:00.0` still stalls in upper-layer prefill after a full night off.

## Notes
- Both cards fail under the same workload (dflash-w4-q4_0, expert cache, 101k-token prefills); identical qcm-fence signature on 04:00.0 twice suggests driver/workload stress, not necessarily one dying card.
- Kernel cmdline has `amdgpu.lockup_timeout=1000000,1000000` (≈16 min per ring) and overdrive enabled — hangs persist long before the kernel reacts, which matches engine stalls preceding every kernel event.
- Evidence commands: `journalctl -k -b N | grep amdgpu` for N in -4..0; prod log mtimes anchor each death.
