# Strata Ops Journal — GUAM (2× Radeon VII gfx906, 64 GB RAM)

Machine-local operations log. Upstream docs live in `docs/`; this file records what we changed on this box and why.

## 2026-10-21

**Overnight crash loop (00:50–09:47):** ~35 engine restarts, one every ~15 min. Watchdog aborts (issue #29, "no progress for 60 s during a request") during batched prefill of 220k+ token prompts, always "waiting for the GPU (attention, router)". Stall reports showed **16 MiB VRAM free at request start** (paging regime, upstream #2065). Kernel GPU reset on 0000:04:00.0 at 10:44 was a separate hardware-side event; most aborts had no kernel reset.

**Fix (12:26–12:43):** `--vram-reserve-mib` 350 → **634**, added `--vram-reserve-later-mib` **794** — the engine's own computed recommendation (`generate.cpp:10402`: `reserve + 512 − free`; later cards target 768 free). Expert cache 6411 → 6233 slots (~3% smaller); hit rate unchanged (~95%). Server restarted 12:43; startup now reports 512 MiB free (CUDA0) / 768 MiB (CUDA1), no LOW warnings. Crash loop stopped.

**14:57 event (post-fix, different class):** decode degraded 22 → 5–8 tok/s over ~7 min, then verify-window timeout at layer 8 → "Unrecoverable native verification failure (#267)" → engine self-SIGTERM (exit −15), auto-recovered. Desktop is NOT on these cards; runtime VRAM dips to 288 MiB during window captures are the engine's own transients. Treating as a rare gfx906 stall (cf. 10:51 kernel "Fence fallback timer expired" on 09:00.0). Reserve stays 634/794; a repeat is the evidence to bump further.

**Staged this session (activated by the server restart):**
- `fit_max_tokens: true` — oversized max_tokens clamps to context room instead of HTTP 400 (required for the defaults below).
- Shared settings `max_tokens: 262143` (`prod-config-swift15.shared-settings.json`) — default output for clients that send no max_tokens (web Chat UI).
- `reasoning_budget_tokens: 55000` — thinking budget. Effective cap is **49,152** with opencode's `max_tokens: 65536` (answer-room guard unconditionally clamps budget to `max_tokens − 25%`); the extra 5,848 is dead config, accepted.

**opencode2 side:** `~/.config/opencode2/opencode.json` → `limit.output` 32768 → **65536** (needs opencode service reload). Chain: output 65536 − 16,384 reserve = 49,152 thinking ceiling, 16,384 answer room. Note: opencode now compacts conversations above ~196k input (reserves output room out of the 262k context).

**Decisions / notes:**
- Sampling left at temperature 1.0 / top_p 0.95 / top_k 20 — matches the model card for qwen4exp thinking mode (`MODEL_CARD_SAMPLING` in `serve/server.py`). The launcher comment claiming "0.6" was stale; fixed.
- `answer_room` left at default (true): removing it risks replies that think to the cap and never answer.
- Model ceiling: `qwen4exp.context_length = 262144` from the GGUF; server already runs `--max-context 262144`. No rope-scaling keys — no extension beyond.
- Non-levers checked: IQ4_XS quant blocked by RAM (~98 GB experts vs 62 GiB memory guard; earlier attempt thrashed at 0.59 GiB/s); min_p redundant under top_k 20 + top_p 0.95; KV fp16 would need max-context ~131k (breaks 190k+ conversations).
- Open quality lever (not taken): `STRATA_ROUTE_TAIL_SKIP=0` — disables the rank≤7 expert-skip speed hack (~1.27M skipped activations per long session, "answers differ slightly"); costs 10–20% decode.
- "[strata] the engine reported an error: I/O operation on closed file" at shutdown = benign teardown noise, appears at the tail of every controlled stop.
- Config backups follow the `prod-config-swift15.json.bak-YYYYMMDD` convention; today's pre-change state saved as `.bak-20261021`.

**Engine binary note:** the server runs `/mnt/user/Strata-2966/build-2966/strata` (engine 0.1.45) with logs in this repo's `logs/`.

**15:08 restart (activates the staged set):** verified live by log lines — `thinking capped at 49152 of max_tokens 65536 ... (budget 55000)` on opencode requests, and `thinking: 8 of budget 55000 (max 261928)` on a no-max_tokens probe (shared default 262143 → fit-clamped to room). VRAM 512/768 MiB free at startup. Caution note: a stray duplicate `server.py` instance (pid 1889468, origin unknown, no log/port/children) was found and terminated during this restart — when targeting the server by pid, match the full command (`serve/server.py --engine strata --config prod-config-swift15.json`), not `pgrep | head -1`.

**Afternoon #267 GPU-hang family (distinct from the VRAM problem):**
- 14:57 — decode degraded 22→5 tok/s over minutes, verify timeout at layer 8 (CUDA0), GPU finished after release; engine self-SIGTERM, auto-recovered.
- 15:33 — decode stopped dead (23→0 tok/s in seconds) at 294 tokens, verify timeout at layer 45 (CUDA1), **GPU did not finish within 5 s**; auto-recovered (try 1 hit the usual VRAM-held OOM, try 2 fine).
- Not config/VRAM: margins correct (512/768), froze early in decode on a 69k prompt, temps 37–40 °C, no kernel reset, no PCIe/AER errors. Morning kernel events on the same cards: 10:44 GPU reset (04:00.0), 10:51 fence fallback (09:00.0) — both Radeon VIIs implicated, escalating severity under sustained load. gfx906/ROCm-level instability; the engine's #267 stop-and-restart is the designed mitigation (~2–3 min per event). Open: report upstream (#267, #29), optional driver isolation vs the custom rocm-gfx906-xnack build, PCIe link/HBM2 temps under load.

**16:0x — VRAM margins reduced by 256 (user decision):** `--vram-reserve-mib` 634 → **378**, `--vram-reserve-later-mib` 794 → **538**; expected steady free ≈ 256 MiB (CUDA0) / 512 MiB (CUDA1), ~+166 expert slots per card. **Risk on record:** 256 MiB on CUDA0 sits exactly at the paging threshold with measured request transients of 210–310 MiB — a return of the morning's #29 stall regime means walking this back up. Activates at the next server restart.

## 2026-10-22

**11:02 — graceful server restart** (clean `stopping…stopped` at the end of the 01:02 overnight log; not a crash). Overnight: 1 engine stop in the 01:02 log. Config at restart: reserves partially walked back up to **550/650** (between the 16:0x cut of 378/538 and the known-stable 634/794). Startup margin: 428 MiB free (CUDA0) / 624 MiB (CUDA1) — still below the 512 MiB paging threshold on CUDA0.

**Three engine deaths in 40 min (11:02–11:42):**
1. #267 verify timeout at layer 39 — exit −15, auto-recovered.
2. #267 verify timeout at layer 0 — exit −15, auto-recovered.
3. **Kernel GPU reset on 09:00.0 (CUDA1) at 11:40:25** — `Fence fallback timer expired on ring comp_1.0.0` → `GPU reset(3) succeeded!` → `device wedged, but no recovery needed`. Same card that threw the fence fallback yesterday 10:51 (and 04:00.0's reset at 10:44) — gfx906 kernel-level instability escalating under sustained load. Engine #29 watchdog fired ~11:41 (no progress 60 s, batched prefill layer 28, chunk from token 45202) — exit −6.
   - First restart attempt (11:41:48) failed: `cudaMalloc(1538035200)` OOM — the dying engine still held VRAM. Second attempt (11:42:04) came up clean. ~2 min outage total.

**Post-recovery state (11:48):** engine up and serving; a 134,650-token prefill ran 247.8 s (543 tok/s), decode 23.4 tok/s, cache hit 92.4%. Small requests queue behind such prefills — looks like a wedge but isn't (health OK, no tokens until the prefill finishes). **VRAM margin collapsed further during operation: 204 MiB free at the last request start; the engine's recommendation rose to `--vram-reserve-mib 896`.** rocm-smi: card0 ~8 MiB free of 16 GiB, card1 ~289 MiB.

**Open decision:** the #29 paging regime is back on the margin's edge (204–428 MiB vs. measured 210–310 MiB transients). Options: raise reserves to 896/~1000 (engine's current advice; costs ~3–5% expert cache) or accept occasional 2–3 min auto-recovery outages plus the kernel-reset family on 09:00.0. Not changed this session.

**14:07 — 09:00.0 (CUDA1) hard wedge; unrecoverable in software → cold power cycle required.**
- 14:07:28 `ring page0 timeout` → kernel GPU reset → `device lost from bus!` → `GPU Recovery Failed: -19`. SMU dead (every powerplay message `response 0xffffffff`); devcoredump on card3 (read-back failed — 14-byte empty bz2; config too dead to dump).
- Software ladder exhausted, in order: strata stack stopped clean (supervisor 510034/engine/vision; /dev/kfd released); `amdgpu_gpu_recover` auto-failed (-19); PCI remove hung inside teardown — 14–16 TTM kworkers stuck in D (unkillable; they also block orderly shutdown, `rmmod`, and any device-lock holder like `/sys .../reset`); card-local SBR impossible — Vega 20's on-card switch ports 07:00.0/08:00.0 are config-dead too; root-port SBR via 00:02.2 (setpci BRIDGE_CONTROL bit 6) executed — card still SMU-dead, workers still stuck. `rmmod amdgpu` never an option: module also serves the Cezanne iGPU (0a:00.0), the healthy 04:00.0, and 4 bridge functions.
- Recovery used: `echo s; echo u; echo o > /proc/sysrq-trigger` (orderly shutdown hangs on the kworkers) + PSU power-off ≥60 s (SMU wedge can survive standby power), then boot + normal engine start.
- New tools kept in /mnt/user/Strata: `gpu-reset.sh` (stop stack → PCI remove/rescan → verify → restart engine; flags --bdf/--skip-stop/--no-start) and `gpu-sbr.sh` (SBR via nearest config-readable ancestor bridge, isolation-checked; walks up to the root port when the card's own switch is dead). Full logs: `logs/gpu-crash-20261022/`.
- Hardware trend on this same card: 10/21 10:51 fence fallback → 10/22 11:40 kernel reset (recovered) → 14:07 permanent wedge. If it recurs: reseat PCIe/8-pin power, try swapping the two VIIs' slots to see if the fault follows the card, then RMA.

**17:50–19:42 — post-reboot evening: two #267 verify timeouts, both on CUDA0 layers; no kernel events.**
- 18:14 (log 181441): #267 `verify: timed out at layer 0` mid-thinking (293 tok); restart try 1 failed once, try 2 clean.
- 19:40 (log 193037, the last crash): 209,927-token prompt, prefill 561 s, decode degraded immediately 2523 → 4.9 → 2.5 tok/s, then #267 `verify: timed out at layer 30` — GPU released but did not finish within 5 s. Engine self-SIGTERM (exit −15), auto-restarted 19:42, serving again within ~2 min; health OK, retried request re-prefilling 210,027 tok.
- Layer 0 and layer 30 are both in the CUDA0 (04:00.0) split range 0–31 — the card that kernel-reset on 10/21 10:44. No kernel amdgpu/ring/fence messages since 17:00 (only USB reconnect spam), so pure software-visible #267 class, not the kernel-reset family that killed 09:00.0.
- VRAM margin still below threshold at current reserves 600/700 (started 11:02 at 550/650; bumped to 600/700 by 19:42 start line): engine warns `only 478 MiB free at request start (below 512 MiB)` and recommends `--vram-reserve-mib 640`; verify-window captures dip to 254–306 MiB free on CUDA0. The 11:48 engine advice of 896 remains un-applied. Recurring pattern: every #267 tonight follows decode degradation on long (≥69k) prompts — consistent with the #2065 paging regime riding on the too-thin margin, on top of gfx906 instability.

**19:51 — fourth crash of the evening (log 194458): #29 watchdog abort during the 210k re-prefill.** Retry of the 210,027-token prompt stalled at 110,751 tokens (chunks stretching to 30+ s), then `no progress for 60 s ... waiting for the GPU (attention, router) at layer 7` → exit −6, auto-recovered 19:55. **This one ran WITH the user's 19:44 bump to 640/700** (config mtime 19:44:32, engine start line 19:44:59 confirms) — engine log showed verify captures at **0–72 MiB free**, full #2065 paging. Conclusion: 640-class reserves do not cover the current 210k-prompt mix; the old "known-stable 634/794" reference point is obsolete for it.

**19:57 — fix applied: reserves 640/700 → 896/1000** (the engine's 11:48 advice; backup `prod-config-swift15.json.bak-20261022-evening` holds the 640 state). Controlled restart: server pid matched by full command, stack fully down, relaunched via `launch-prod-swift15.sh` (log 195722). Verified: engine start line `--vram-reserve-mib 896 --vram-reserve-later-mib 1000`; **772 MiB free (CUDA0) / 974 (CUDA1)** with everything loaded; verify captures 613–860 MiB; engine's live recommendation now **832** (below the applied 896 — margin covers measured transients). Cost: expert cache 6255+5340 → 6069+5195 slots (~3%); hit-rate impact TBD. Serving traffic again (13k prompt in flight at log time).




## 2026-10-22 (evening)

**Q8_0 PLE staged for testing — NOT active; prod untouched (still the model's own IQ4_NL table).**

- Downloaded Delgon's standalone `Qwen3.8-Flash-Next-PLE-Q8_0.gguf` (54,400,262,080 B) to `/mnt/user/ai`; sha256 verified against the repo's `SHA256SUMS` (OK). Download incident: `hf download` (xet) built a full-size file ~48 MB short (preallocation holes) → checksum FAILED → purged; aria2c -x16 redownload verified clean. Always run the sha256 gate.
- Engine 0.1.45 already supports Q8_0 PLE (`kPleFormats`, 170 B rows) — no code changes, no rebuild. `build-2966/ple_reader_test --selftest` OK (all formats incl. Q8_0) and `--gguf` on the real file: "real-table check: OK (bit-identical)", direct reads p50 238 µs / p99 420 µs.
- Integration requirement found in source: `--ple-gguf` files whose `general.architecture` != `strata-ple` get auto-appended to the native dense scan (`generate.cpp:3039`), and `NativeDense::load` then fails `check_engine_architecture` on the sidecar's minimal metadata → the Q8_0 config MUST pin `--native-dense-gguf` to the Swift model file. Done in the new config.
- New files (nothing existing edited): `prod-config-swift15-q8ple.json` (3-line delta: PLE path, dense pin, engine log), `launch-test-q8ple.sh` (prod launcher clone, refuses while prod runs), `ple-q8-default.sh original|q8_0` (prod switch, .bak convention, validates file first), `prod-config-swift15-original.json` (pristine). Evidence + A/B harnesses: `/mnt/user/ple-q8-upgrade/` (report.md, bench.py, monitor.sh, run_quality.py).
- Test procedure: stop the prod stack → `/mnt/user/Strata/launch-test-q8ple.sh` → expect in `logs/engine-swift15-q8ple.log`: "PLE direct read probe: … rows/s (fine)" and "PLE on, table 320001536 rows of /mnt/user/ai/Qwen3.8-Flash-Next-PLE-Q8_0.gguf". Baseline arm runs against the prod stack as-is. Switch prod default only after A/B: `ple-q8-default.sh q8_0` + restart.

**22:23 — prod switched to the Q8_0 PLE (user-directed).** Stack stopped by PID (TERM, clean), `ple-q8-default.sh q8_0` swapped the config (timestamped .bak; the live 896/1000 reserves were folded into the q8ple variant first — the afternoon's config drift), started with the normal `launch-prod-swift15.sh`. Evidence in `logs/engine-swift15-q8ple.log`: `PLE direct read probe: 58204 rows/s (fine)`, `PLE on, table 320001536 rows of /mnt/user/ai/Qwen3.8-Flash-Next-PLE-Q8_0.gguf`; engine cmdline carries `--native-dense-gguf` pinned to the Swift file. API serving on :8009. Rollback at any time: `./ple-q8-default.sh original` + restart. A/B harnesses in /mnt/user/ple-q8-upgrade/. Ops note: matching the stack by pattern from a script self-matches the script's own bash — use PIDs or bracket patterns.

## 2026-10-23

**Upstream survey (read-only; nothing on this box changed): origin Niko1221/Strata is +1308 commits over our merge base 477ac13c (v0.1.45, 2026-10-16) → v0.2.3, tip 5c8dd322 (today 07:57). Engine line: 0.1.45 → 0.1.46 (0.2.1) → 0.1.47 (0.2.2) → 0.1.48 (0.2.3). Our branch build-latest: 9 local commits ahead, machine-local config/launcher commits carry over on a rebase.**

What upstream now has that concerns GUAM:

- **gfx906 builds again upstream (since 0.2.1, #3172/#3271, commit 10715d85):** all 161 targets compile with `-DSTRATA_HIP_GFX906=ON` (source-only, no package, setup.py doesn't build it; upstream built against HIP 7.14 from the mixa3607 community image). Radeon VII still listed "not run" upstream — our box remains the only Radeon VII evidence. Overlap with our port commits: 918781aa (mmq_w4a4/cublas shims) and b7383aae (PREFILL_MMQ gate) appear superseded; 70e0d531 is ROCm-**6.1**-specific (upstream targets 7.14) and needs a re-check before dropping; 6d5e5770 compiles the real moe_f16_kq while upstream ships a **stub** — our port is functionally ahead there. Qwen3.6 (qwen35moe) is excluded from the gfx906 build (fine — we run qwen4exp/Flash-Next).
- **Two-card gfx906 tip (#3173): `--pipeline-windows 2` = +36% decode (49.2 → 66.9 tok/s, 2× MI50, engine 0.1.43.1-class), `--trim-stage-weights` = +5% prompt (353 → 371). Off by default. Both flags are already in our live build-2966 binary** (verified `strata --help`) — testable on the current engine, no upgrade needed. Caution from the engine's own note: without the draft layer on the first card the pipeline lost to the serial loop on 2× R9700 pre-0.1.44; `--batch` did not help a single interactive user there.
- **Stability fixes in our crash classes:** 0.2.2 fixes a rare HIP first-verify-window hang (`STRATA_VERIFY_EARLY_PLE_FLAG`, PLE flag now raised before cudaGraphLaunch — our "verify timed out at layer 0" class); 0.2.2 #3465 retries a NaN prompt window through the batched path instead of failing the request; 0.2.3 watchdog counts engine prompt-read as activity and adds `STRATA_SPEC_WATCH` (stalled-loop watchdog with stage); 4f74de9c keeps pinned-memory headroom while the expert arena pins (our restart-OOM class). Nothing upstream claims to fix the gfx906 kernel-reset/wedge family on 09:00.0 — that stays ours (RMA path).
- **Serving (`serve/`, +4898 lines since base; server.py alone +1784):** our **uncommitted `serve/telemetry.py` mods (198 diff lines) will conflict** — commit or stash before any pull. Upstream added: max_tokens shortened to room left instead of HTTP 400 (f31b6efd) and `-1`/`0` = no cap (5c8dd322) — post-upgrade our `fit_max_tokens` + shared `max_tokens: 262143` workaround simplifies; `thinking_budget_tokens` honored (#2141); conversation cache auto-sized from free memory (#3493, 40% of spare above a floor, ≤8 GiB — 91% prompt reuse vs 0% in a two-agent test; relevant to opencode multi-session serving on 64 GB).
- **File-tier / SSD expert streaming:** 0.2.0 zero-copy page-cached experts + gated I/O prefetch (`STRATA_COLD_OVERLAP`, `STRATA_IO_PREFETCH`, `STRATA_IO_PF_GATE` — decode up to 1.7–2.9× in RAM-limited tests); 0.2.1 drive probe picks file-cache vs unbuffered reads (#3237, `STRATA_FILE_TIER_PROBE`) and gates experts.bin prefetch at ≥55% page cache; prompt chunk now accounts for RAM/SSD (`STRATA_PREFILL_HOST_PLAN`). All measured on CUDA/NVIDIA, Linux-only paths — untested on gfx906, but directly aimed at our 98 GB-experts-on-SSD regime.
- **DFlash2 drafter for Qwen3.8-Flash-Next (0.2.3, opt-in):** Q8_0 pack tool + GPU forward, drafter head reserved before expert-cache sizing, verify window narrows on graph OOM. An MTP alternative for Swift15 once we're on ≥0.1.48.
- **Already in our base (not new to us):** gfx906 old file-cache rule (#1752, `STRATA_FILE_CACHE_OLD_RULE`), the 0FL01 attention switches (#1661/#1718), Q8_0 PLE read support.

Not for us: the app/Terminal/installer/Docker work, NVIDIA/Intel/Mac items, Main/Helper/Off GPU roles, RX 6000–9000 packages.

**~11:36 — engine watchdog abort (exit −6), auto-recovered. No config change.**

- First boot after this morning's 10:36 emergency sync + cold power removal (follow-up to the 10-22 09:00.0 SMU death). Stack up 10:39:22; healthy for ~1 h (27.7 tok/s decode, expert cache ~95 % hit).
- Single issue-#29 abort during batched prefill of a 50,822-token prompt: "no progress for 60 s … waiting for the GPU (attention, router) at layer 7 of the prompt chunk from token 43830". Engine self-aborted (exit −6, by design); server restarted it (~2 min: 39.97 GiB experts at 2.2 GiB/s + cache refill), back ~11:38. The retried prefill passed token 43830 and progressed to 44,989+/50,822 — stall did not reproduce on retry.
- Not the 10-21 paging class: no LOW-VRAM stall report; 772 MiB (CUDA0) / 974 MiB (CUDA1) free with everything loaded; reserves 896/1000 already active. No kernel GPU reset this boot — unlike 10-21 10:44 (04:00.0) and 10-22 (09:00.0 SMU).
- Boot-time anomalies worth one line each: amdgpu powerplay "clock voltage 734 not within allowed range [738–1218]" ×4 at 10:38 (card not identified in log); multi-bus USB re-enumeration burst 11:35:56–11:36:00, coincident with the stall window (times reconstructed from log mtimes/line counts — prod log lines carry no timestamps).
- Disposition: same gfx906-stall family as 10-21 14:57 (layer-8 verify timeout, exit −15) — single occurrence, healthy before/after, retry clean. Watch for a repeat; if it recurs on the same chunk geometry (deep batched prefill, attention/router wait), candidate levers are `STRATA_SPEC_WATCH`-era 0.2.3 watchdog improvements or the upstream pipeline flags, not reserve bumps.

## 2026-10-24

**Engine upgraded 0.1.45 → 0.1.48; DFlash2 A/B live (`launch-prod-swift15-dflash-w3.sh` from 14:30, `-w4` from 18:56).**

**Overnight 00:11–05:07, standard config: three #29 watchdog aborts** (one decode stall; two batched-prefill "waiting for the GPU (attention, router)" at layer 46/47 of chunks from token 37172/61748) — the familiar gfx906 stall family, all auto-recovered. The 05:07 log then ran clean through midday.

**19:28 — first crash on dflash-w4 (log `prod-swift15-dflash-w4-20261024-191828-60903.log`): NEW stall class — not VRAM, not kernel.** ~10 min after the 19:18:28 w4 launch, an 83,441-token prompt arrived and the engine stalled in `request: parking the previous conversation -1` → no progress 60 s → exit −6 (issue #29), auto-recovered ~2 min. Stall report (engine 0.1.48): 0 layers served since the last finished step; expert pool fully quiesced (24/24 jobs done, 7/7 workers parked/sleeping, host idle 65 s); verify window at position 81865 with host step 32 / GPU rang 32 / flags 32 — nothing outstanding on the GPU either; 0 of 36 threads in D (not disk/TTM). VRAM healthy: 561 MiB free at request start, verify captures 561–786 MiB, zero LOW warnings — rules out the 10-21/22 #2065 paging regime. Standout anomaly is host RAM: 44,388 MiB resident, 2,828 MiB in swap, 8,356 MiB available, 1,958 major faults — w4 loads ~76 GB of experts into RAM. Hypothesis: conversation-cache parking (marshaling the previous ~80k conversation out) wedged under that RAM squeeze; w4-specific or 0.1.48-DFlash-specific. **19:30:55 — second crash 3 min later, same 83,441-token request, classic prefill-GPU-wait stage (log 191828, engine start 19:27:29).** After crash #1's recovery the retry prefilled to 15,379 tokens, froze 90 s, then `no progress … waiting for the GPU (attention, router) at layer 42 of the prompt chunk from token 15379` → exit −6 (#29); third engine start 19:31:04. Stall report: 0 layers served, epoch 0 with 0 jobs, 7/7 workers parked (host idle 171 s), verify window 0 tokens / GPU rang 0 — the GPU never signaled anything for this request; teardown shows `#267 GPU waits released, finished in 0 ms` (nothing was in flight). 0/38 threads in D; VRAM 790 MiB free at request start; RAM 45.2 GB resident / 7.5 GB available / ~200 MiB swap. **Layer 42 sits in CUDA1's range 32–47 — the 09:00.0 card (10-22 SMU death); crash #1's verify window sat at host layer step 32, the CUDA0→CUDA1 hand-off boundary.** Both stalls = "GPU silent with nothing outstanding" (lost/never-arrived completion), not slow compute; not the paging class; dmesg unreadable without sudo (kernel side unverified).

**Shared context of both crashes — this is the dflash2 integration build, not build-2966:** `/mnt/user/dflash2/Strata-integ/build-dfsample-1/strata` (engine 0.1.48, "sampled" build) with new paths live: conversation-cache parking (crash #1's stage; flags `--conversation-cache-mib 2048 --conversation-cache-min-tokens 2048 --conversation-cache-park-gain 4096`), dflash cross-GPU tap collection **serial on CUDA1** + HC stream mean capture (in play for crash #2's CUDA1 prefill), `--prompt-cache-spread` and prompt-path cache-slot borrowing (2768 CUDA0 / 2249 CUDA1 slots).

**19:37 — ATTRIBUTION CORRECTED: dflash/w4 is NOT the cause; both stalls are the long-standing gfx906 lost-completion family (supersedes the suspect order above).** Evidence:
- Attempt 3 completed the SAME 83,441-token request on the SAME build/config: `done: 1453 tokens in 291 s (19.9 tok/s), expert cache 94.1% hit` (19:36) — not deterministic, not a build bug.
- `engine-swift15.log` (standard config, engine 0.1.45, **80 stall reports**, all pre-dflash) contains crash #2's exact stage 20+ times: "waiting for the GPU (attention, router) at layer N" at layers 1, 3, 4, 27, 35, 37, 38, 40, 43×3, 44, 45, 47 — **both cards**, several from tokens 0/8192 (chunk start). The "layer 42 = flaky 09:00.0" framing was overreach; the family hits layers 0–47 on both VIIs.
- Crash #1's stall body is line-for-line the 0.1.45 "request -1" stall (engine-swift15.log:11421): 0 layers served, epoch 0, 7/7 workers parked, verify window 0 tokens / GPU rang 0, 0 threads in D, `#267 released, finished in 0 ms`. 0.1.48 only renamed the stage to "request: parking the previous conversation -1"; the conversation-cache flags are in the 0.1.45 standard config too. (That 0.1.45 instance ran at 228 MiB VRAM LOW with 196,903 major faults — the paging era; tonight's ran at 561 MiB / 1,958 faults, so the memory conditions are NOT required.)
- w3's afternoon served 141,709-token prompts with zero stalls — exposure/luck, not immunity.

**Cause (both crashes):** the engine's host thread waits on GPU work (attention/router prefill; a request-setup wait inside parking) whose completion never arrives — GPU shows nothing in flight ("rang 0", released in 0 ms) — and #29 exits −6 after 60 s. Documented on 0.1.44.1 → 0.1.45 → 0.1.48, both cards, since 10-21. Tonight was concentration, not causation: crash → auto-retry cycled the same deep 83k prefill through the vulnerable window repeatedly within minutes. Ruled out: dflash2/w4, VRAM (561/790 MiB free, no LOW), disk/TTM (0 in D), slow GPU (0 ms). Residual: a gfx906-specific signaling race — 0.1.48 already carries upstream's 0.2.2/0.2.3 race fixes (early-PLE flag, prompt-read watchdog), and this box is upstream's only Radeon VII evidence. Escalation: upstream issue with the stall reports; driver isolation vs the custom rocm-gfx906-xnack build.

**Per-GPU attribution (19:41):** crash #2 is attributable — layer 42 is in CUDA1's range (split line: layers 0-31 CUDA0, 32-47 CUDA1) → **CUDA1 = 09:00.0** (mapping from the 10-22 kernel-event correlation; engine logs carry no PCI ids). Crash #1 is NOT attributable from the log: parking snapshots the conversation KV **from both cards** (healthy parks moved 650 MB-1.7 GB in 0.5-2.3 s, e.g. "parked 58592 tokens in 2279.2 ms"; the stalled park never finished) and the stall report names no GPU. Family-wide histogram of all 93 "attention/router" stalls (0.1.45 + tonight): **CUDA1 61 vs CUDA0 32** — per-layer that's ~3.8× CUDA0's rate (CUDA1 owns half the layers); spikes at layer 35 (18×), 47 (10×), 43/45 (6× each); CUDA0 still bleeds across layers 1-31 (32 hits, and it took the 10-21 10:44 kernel reset). Conclusion: **09:00.0 is the primary offender (and tonight's crash #2), but both VIIs drop completions** — swapping/RMAing 09:00.0 would cut ~2/3 of events, not eliminate the family.

## 2026-10-26

**Morning crash loop 10:38–11:20 while background-load on the Cezanne iGPU (0a:00.0); clean since the hostapp closed.** 8 #29 aborts in `prod-swift15-dflash-w4-q4_0-20261026-103807-17408.log`, 3 of them with kernel resets this boot: 10:53 `qcm fence wait loop timeout` (04:00.0), 10:57 + 11:00 `ring page1 timeout` (09:00.0) → BACO reset → "VRAM is lost". Every stall is the batched-prefill watchdog stage: "reading the prompt (batched): waiting for the GPU (attention, router) at layer N" — opencode infill requests.

- **Host RAM is the amplifier since dflash-w4-q4_0 (10-24):** engine RSS ~44 GiB (w4 maps ~76 GB of experts), box at 54/62 GiB used + 10 GiB swap *before* the hostapp. The iGPU hostapp's VRAM is GTT = system RAM → tips the box into reclaim/swap thrash → prefill throughput collapses past the 60 s #29 watchdog; TTM migrations that need host pages stall → page-ring/qcm fence timeout → kernel BACO reset → VRAM lost → watchdog exit −6 → auto-restart. "Used to work during hostapp" = pre-10-24 configs left host-RAM headroom.
- **VRAM margins at inspection (~11:25):** CUDA0 502 MiB free (below the 512 MiB paging threshold again; startup was 790) / CUDA1 755. Margins shrink during operation as KV + expert cache grow — same #2065 regime risk as 10-21/22.
- Fixes proposed (pending user choice): (a) launch the hostapp in a systemd user scope with `MemoryMax≈6G` + CPU quota so the kernel reclaims from the hostapp, not strata; (b) `--vram-reserve-mib` 896→1024, `--vram-reserve-later-mib` 1000→1128 to hold ≥512 MiB steady on CUDA0; (c) fall back from the q4_0 pack to the w3-era host footprint.

**CPU-governor angle (user observation: boot lands in powersave; manual performance alleviates).** Facts: driver `amd-pstate-epp` (`amd_pstate=active`), all 16 policies governor=performance/epp=performance right now (~4.46 of 4.68 GHz); BIOS exposes **only EPP=performance**; `power-profiles-daemon` active, state.ini saves `Profile=performance` but it is dbus-activated (applies late) and `battery_aware=true` can downgrade on battery. So the slow-boot window is the EPP driver's default governor name (`powersave` = hint-driven) before anyone lifts it. Mechanism of harm: host-side expert streaming (39.97 GiB at ~2.2 GiB/s), batch prep, and TTM/swap kworkers all stretch at low clocks → prefill chunks spend longer inside the vulnerable GPU-wait windows → more #29 60 s zero-progress aborts and slower page migrations into the gfx906 wedge/reset family (kernel cmdline already has `amdgpu.lockup_timeout=1000000,1000000`, so today's `ring page1 timeout` = true migration wedge, not slowness alone). The engine "not coping with slower" is by design: #29 fires on **60 s of zero progress**, not low throughput — it cannot distinguish a starved host from a lost GPU completion, and a wedged request would block the single-user engine forever, so it self-aborts (exit −6) and lets the server restart it. Staged `cpu-performance.service` (oneshot, After=power-profiles-daemon) to pin governor+EPP at every boot; install: `sudo cp /mnt/user/Strata/cpu-performance.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now cpu-performance.service`. Optional PPD hardening against battery downgrades: `/etc/power-profiles-daemon/config.json` with battery-aware off.

**Evening (post-reboot timeline; user stop + reboot 18:37–18:50):** server up 18:58:07 → **last kernel crash 19:02:42 `ring page1 timeout` on 09:00.0 (CUDA1) → BACO reset → VRAM lost** → #29 abort ("reading the prompt (batched)"), auto-restart 19:02:45. Then one pure-software #29 at ~19:38 in the same infill stage (restart try 1 at 19:38:42 hit the classic VRAM-held OOM, try 2 at 19:38:58 = current engine, healthy). **83 engine starts today** (afternoon 12:32 log alone: 20 aborts). Governor was already `performance` this boot (PPD restored its saved profile; `cpu-performance.service` still NOT installed) — so 19:02 was **not** the powersave window: points at the gfx906/CUDA1 lost-completion family + the sub-threshold VRAM margin, exactly the case for reserve bump 896/1000 → 1024/1128 and the 09:00.0 slot-swap/RMA test from 10-22.

**2026-11-03 21:58 — fleet moved to merged v0.2.10 (branch build-mixed-v0210), prompt tier OFF.** Bench verdict first (bench dirs in ~/dev/Strata/bench/ab-v0210, frozen binaries + out/RESULTS.md): 80k infill sha 1c7d4a6d byte-identical across A(prod 872e3dac)/B(merged)/C(stock)+D/E diagnostics in all 7 runs; PP 305.7/306.7(B/D) vs stock 337 (the gap = dflash tap collection, all of it); decode 33-35 vs stock 23.6 = dflash drafting's +48%; helper expert tier alone = +0 decode (96-97% local cache hit). **Prompt tier (M2) OFF for prod per user decision**: D-arm measured the tier costing ~38 tok/s on 80k reads (444.9→483.0) with no decode benefit today (its ab2-r1 +7.7% long-decode win did not reproduce in this drive); STRATA_PREFILL_HELPER=0 in both mixed configs, verified in the live engine env. Deploy: /mnt/user/Strata/v0210/{build-mixed-gpu/{rpcw-hip,rpcw-cuda},serve,tools,strata_control} staged from ~/dev/Strata build (validated binaries, unchanged since bench). ALL launchers now use it: mixed ×2 (WORK=$ROOT/v0210; serve cwd no longer the old dflash2 integ tree), plain ×2 (launch-prod, launch-prod-swift15 → v0210/serve), legacy dflash ×3 via config exe (flag-fit verified vs the new binary; --vram-reserve*/--pcie-frac exist but are undocumented in --help). Old trees untouched (cuda-helper worktree = rollback: point WORK back). Restart 21:58: serving in ~70 s, temp-0 smoke 'OK', helper lane up. Earlier same day: one transient gfx906 hang at the 8192 chunk boundary during bench (fits the CUDA1 lost-completion family below).

**Later 2026-11-03 — independent verification of the 21:58 fleet state (second session).** Config flip predates the running engine (config mtime 21:57:35 < engine start 21:58:24 → this generation launched with STRATA_PREFILL_HELPER=0); /v1/models 200 (swift-1.5-iq3_xxs, ctx 262144); helper filled 4116 slots/6.25 GiB; first live request healthy (decode 29.2 tok/s, drafts 13/19 accepted, 92.1% decode cache hit, 0 helper timeouts/fallbacks). Diagnostic note for future ops: the prompt tier's second channel still CONNECTS with the tier off — arm-D's helper signature is `second-channel engine connected` + **0 prompt row requests** at exit, and live prod matches — so that connect line alone is NOT evidence the prompt tier is on; the prompt-row count at helper exit is the discriminator. Deferred: the D-arm r2 confirmation round (needs exclusive GPUs; not worth another prod outage on current evidence, since direction is consistent across eras and ab2's old prompt-tier win did not reproduce). Open targets on record: dflash serial tap collection (~all of the 31 tok/s fresh-PP gap, ~2/3 of the 80k-read gap), a full-helper-removal arm (predicted ≈ D on this pack, would also remove the divergence source), and a corrected parity probe (corpus.py's probe prompt is accidentally truncated — probe shas are a per-config determinism check only).

**2026-11-04 ~04:00–06:30 — tap-overlap prod-down window (user-authorized); fleet restored 06:15.** Arms on the bench harness: F0 (DF_EVENTS probe), F1off/F1×2/F2only/F2 (the tap-overlap-f1f2 prototype), K1/K2×2 (knobs), B r4 (died, see below) + B r5 (same-day anchor). Verdicts in ~/dev/Strata bench/ab-v0210/out/RESULTS-window.md (commit d50ca328; the 80k parity sha 1c7d4a6d held in all 18 rounds): **nothing ships by default** — defer-fusion is prefill-flat and breaks the decode gate (drafts 0-of-0 on cached reruns, dose with the flag), async-D2H is flat, split-16 is 2.26x slower. The one adoptable finding: **--prefill 16384** — +21% on 80k-class infill (583.7 vs same-day 482.5), decode unchanged (33.1), parity byte-identical, at -9.5% on 18k-class fresh prompts; an OPT-IN config knob for long-context-heavy serving, owner's call; one unreproduced 268 s post-80k follow-up stall seen under it (watch if adopted). B r4 died once to the known **#267 verify-timeout at layer 46** — first occurrence on record here (~20 bench rounds + five-arm bench + 5h51m prod), old binary (not prototype-related), GPUs verified healthy after (35-37 C idle, no RAS errors), r5 retry clean; fits the CUDA1 lost-completion family on record. Session frame note: the 80k metric ran ~6-11% fast today vs historical medians while fresh-18233 did not move — cross-day 80k comparisons carry that caveat. Restore 06:15 via the stock launcher: /v1/models 200 (swift-1.5-iq3_xxs, ctx 262144), full process tree on the frozen v0210 binaries, helper listening on both channels (window tier + second channel — prompt tier still off per config), live temp-0 request healthy. Prototype branch parked at tap-overlap-f1f2 (through d50ca328), flags default-off, flags-off byte-identity gate passed.

**2026-11-04 ~07:00–09:35 — K2 ADOPTED (user order) + lever windows; fleet restored on the K2 config 09:23.** prod config `--prefill 8192→16384` (backup prod-config-swift15-dflash-w4-q4_0-mixed.json.bak-prefill8192). Two bench windows: window2 = C-stock r1/r2 + P0 (= adopted config verbatim, prompt tier off) r1/r2; window3 = K3 prefill 24576 / K4 32768 / P1 ring-mult-8. Verdicts (~/dev/Strata bench/ab-v0210/out/RESULTS-window2.md @ 8f4774da): **K2-adopted vs genuine stock, same day: 80k infill +7.6% (584.8 vs 541.9 median), decode +~45% (~35 vs ~23.5 tok/s), fresh-18k −18.2% (277 vs 338) — the accepted trade per the adoption order. Temp-0 parity byte-identical vs stock on the 80k gate in all four rounds (1c7d4a6d…); probes per-config deterministic.** Dose-response: 16384 is the peak (24576: 80k −10%; 32768: −37%) — no further prefill gain available. The window-1 268 s stall root-caused in source: the DFlash2 SWA draft ring is 2048 cells (artifact geometry) and stales segment resumes deeper than ~64 cells → the guard re-reads the whole context through the drafter; NOT config-mitigable (--dflash-window cannot enlarge the physical ring). Mitigation built + benched (STRATA_DF_RING_MULT, opt-in cf653943; armP1 speed-neutral, fixes the class by construction) but NOT deployed — prod keeps the genuine v0210 binary; one-swap deploy recipe in the results doc if the stall ever bites. Watch items: P0 r2 one 80k cancel-and-retry at 99.99% of the read (retried clean in-round, sha held); a state-dependent ~6 s tg-segment flicker (missing exact checkpoint at resume — checkpoint placement, open small lever). Restore verified: /v1/models 200 (ctx 262144), engine cmdline carries --prefill 16384, helper both channels, live temp-0 request healthy.

**2026-11-04 ~10:15 — K2 promoted to ALL prod configs (user order).** All 8 prod configs carrying a --prefill knob now set 16384 (was 8192): swift15, swift15-original, swift15-dflash-w3, swift15-dflash-w4, swift15-dflash-w4-q4_0, swift15-dflash-w4-q4_0-mixed (already K2), swift15-q8ple, swift-iq3_s-dflash-w4-q4_0-mixed. Each has a *.bak-prefill8192 backup and was JSON-validated after the edit; the shared-settings fragment has no prefill knob. The live mixed fleet already serves K2 (engine cmdline verified) so no restart was needed; the other configs are idle alternatives and pick the change up on their next launch. Caveats: the 16384 evidence is measured on the swift15-iq3xxs mixed stack; the iq3_s mixed config and the non-dflash configs inherit it unvalidated for their stacks (blanket order); swift15-original.json may be a frozen reference — one-file revert via its backup if so intended. Next cycle opened: detailed profiling (DF_EVENTS decomposition, rocprof kernel timeline, spec-decode tuning) for further speedups.
## 2026-11-03 — sync prod line to upstream v0.2.10 (session: strata v0.2.10 sync)

Base: `work/mixed-cuda-finish` @ `872e3dac` (prod worktree HEAD, /mnt/user/Strata/worktrees/cuda-helper)
Merged: `origin/main` @ `ffa80d73` (v0.2.10, +3463 commits). Merge commit `371257bc`, fix `c6125e09`.
Telemetry WIP preserved on `build-latest` (`8a32b369`). Stash `gfx906-pre-2174-sync` untouched.

Conflicts: 15 files, 56 hunks. Strategy: upstream machinery wins where it supersedes; our mixed-line
specifics kept (expert-helper RPC `expert_helper_args`, M2 prompt tier, M7 draft lane `df_wire_rows`,
our dflash_inco runtime = STRATA_ENABLE_DFLASH2 with STRATA_HAVE_DFLASH2=OFF, Q8_K wire).
Key semantic merges in `src/program/generate.cpp` (30 hunks):
- upstream moved window selection to `spec::choose_window(WindowRuleIn)` + `--draft auto` bandit +
  gated proposals (`GatedDraftLimit`, `prepare_gated_window`); our drafter integrated as a third path:
  serve `if (has_dflash) T = req_dflash && dflash_ready ? df_pending_k + 1 : 1;` after choose_window;
  CLI `if (use_dflash) T = dflash_ready ? dflash_k + 1 : 1;`
- `drafted` non-const with `|| has_dflash ||` (serve) / `|| use_dflash ||` (CLI) short-circuit so
  our dflash block owns the round; adapt-thread join kept guarded by `if (!use_dflash)` (CLI, double-join guard)
- lookup terms: serve `!(draft_on || (has_dflash && req_dflash && dflash_ready))`; CLI
  `!(draft_on || use_dflash) || (use_dflash && T == 1)`
- `take_kv_chunk`→`grow_take` rename with our `dflash.grow_scratch` kept in both the initial grow and
  the STRATA_PARK_VRAM_MIB retry
- conversation parking: upstream restructure (ckpt pool / park async) + our dflash image extension;
  `live`→`src.ids`
- `arm_prompt_helpers` lambda (upstream re-arm-after-relayout) now carries our M2 `set_prompt_helper`
CMakeLists: our gfx906/HAVE_DFLASH2 guards wrap upstream's expanded targets (dflash2 tools, mmvq
89table/pf tests, verify_parity union guard `(CUDA OR HIP) AND NOT STRATA_HIP_GFX906`, w4a4 native-arch
expansion under our gfx906 guard). One doubled endif fixed (c6125e09).
configure.py/setup.py: upstream `--draft auto`/`local_build_archs` + our `--dflash2` build_engine param
and drafter keys unioned.

v0.2.10 notes: `STRATA_ENABLE_CUDA` now defaults OFF (explicit -D needed for the helper build).
DFlash2 runtime split enforced at configure: exactly one tap producer per verifier.

Builds configured (compile HELD for prod-down window — engine holds ~45G of 62G RSS):
- `build-v0210/rpcw-hip` — Ninja Release, STRATA_HIP_GFX906=ON, gfx906, ROCm 6.1 pins
  (clang++ /opt/rocm-6.1.0, hip_DIR+hipblas_DIR 6.1), STRATA_HAVE_DFLASH2=OFF, STRATA_ENABLE_DFLASH2=ON,
  TESTS=ON. ggml pin fetched: 3cf03257f.
- `build-v0210/rpcw-cuda` — Ninja Release, STRATA_ENABLE_CUDA=ON, arch 86, nvcc /usr/local/cuda,
  HAVE=OFF ENABLE=ON, EXPERT_HELPER=ON, TESTS=ON.

Next (window): `ninja -C build-v0210/rpcw-hip strata` + `ninja -C build-v0210/rpcw-cuda strata-expert-helper`,
ldd check (no `.so.3`), then A/B vs frozen arm A per /home/user/.opencode/plan/strata-v0210-sync.md
(pp16384 / tg1024 / 80k deep infill / temp-0 sha, A,B,A interleaved, run-arm.sh pattern).

## 2026-11-03 v0.2.10 sync — prod-down window, build fix rounds (b9492a7e)
- Prod down 19:34 via supervisor TERM (pgid 1851321); clean teardown, VRAM freed, sockets unlinked. Restore: `setsid nohup /mnt/user/Strata/launch-prod-swift15-dflash-w4-q4_0-mixed.sh &`.
- CUDA helper built clean 73/73 after IlQ8_0 gained HAS_PF=false and SmallTraits<Q80Block,8>::TYPE=8 (upstream's IL pf-table contract).
- HIP engine, three fix classes:
  1. upstream df2 probes unguarded under STRATA_HAVE_DFLASH2=OFF (head_native validation, df2_expect_share, df2_head_bytes, kDrafts, defer_df2 stub) — all #if'd now.
  2. verified_pair: merge stacked BOTH verify blocks; kept one (nib-aware + our first-prefilled-pair semantics), declared the var before the fill loop.
  3. brace drift x2, found by difflib depth-alignment vs origin/main: (a) serve `if (req_prefill_only...)` lost its `continue;}` to our spliced dflash request setup — restored; (b) CLI `if (!use_dflash) {` lost its close per 872e3dac original — restored. File now depth-balanced against upstream 0-offset end to end.
- Lesson: after semantic merges, run the depth-alignment scan (difflib match blocks, compare brace depth at matched lines) BEFORE compiling — two missing closers caused the sp/Prefill shadow cascade.
- Bench harness in bench/ab-v0210 (corpus.py, drive.py, run-arm.sh, armA.json/armB.json); armA frozen (bin+serve+tools, SHA256SUMS tracked, binaries untracked).

## 2026-11-03 v0.2.10 sync — A/B/C verdict (bench/ab-v0210/out/report.txt)
- Rounds kept: A r1,r2 + B r1,r2 (cap 2/arm, user) + C r1 (stock v0.2.10 baseline, no helper/no drafter).
- Parity: probe shas differ across builds (A ab1b87dc 4/4 boots, B 96e31a1e 2/2, C 059227cf) — single token fork at char 176 of an identical prefix (thinking tail, near-tie "prompt"/"question"); each build perfectly self-consistent, and the 80k infill sha is BYTE-IDENTICAL across all five kept runs incl. stock. Verdict: deterministic per-build first-window path difference, not a merge break; the deep greedy path is exact.
- PP fresh 18233: A 305.8 / B 305.7 (merge = old, dead even) / C 337.2 (stock +10% — no tap collection/helper on the read path).
- TG cached decode: A 35.0 / B 33.1 (-5%, overlapping spreads) / C 23.6 (mixed line +48% over stock).
- 80k infill read: A 458.9 / B 444.9 (-3%) / C 550.4 (stock +20%).
- Gotchas hit: helper --experts is pack-relative; serve needs strata_control/ + tools/ beside it; engine config 'log' is serve-cwd-relative; stock v0.2.10 does NOT build for gfx906 as-tagged (alignas int4 #778 rule, dflash2 STRATA_USE_HIP, stream-priority compat defines — three compile-only fixes on branch stock-v0210-gfx906-buildfix); NEVER compile during a bench round (stock build starved A2's prefill -> 300s engine-silence watchdog killed it; round discarded+rerun).

## 2026-11-03 PP-cost attribution (arms D/E, bench/ab-v0210/out)
- D = B + STRATA_PREFILL_HELPER=0 (checks atoi==1, so =0 is a true off). E = B minus --dflash.
- PP fresh 18233: B 305.7 / D 306.7 / E 337.6 / stock 337.2 -> the whole PP gap is dflash tap collection; helper prompt tier costs ~0 on fresh reads.
- Infill 80k: B 444.9 / D 483.0 (+38 prompt tier) / E 541.6 (+97 tap) / stock 550.4 -> tap ~2/3, prompt tier ~1/3, slight overlap.
- TG: E 23.9 == stock 23.6 (drafts 0/0) -> the +48% decode is ENTIRELY dflash drafting; the helper expert tier alone adds nothing on this pack (96-97% local cache hit).
- Methodology: probe sha (first req, fresh boot) is read-path-composition sensitive - B vs D differ with identical binaries, while A==E across builds. 80k infill sha 1c7d4a6d invariant in all 7 runs. Probe = per-config determinism check only; 80k = the parity gate.
- One D run hung at the 8192 chunk boundary (GPUs 0%) - transient gfx906 hiccup, clean on relaunch; config was valid.

## 2026-11-03 Probe prompt fix (bench/ab-v0210/corpus.py)
- PROBE_PROMPT's second line was an orphan string literal, not a concatenation: the probe really ended at "keeps the ", so every arm's probe "answer" was the model asking for the missing ending. Not a serve or template bug.
- Fixed with a parenthesized concatenation, commit bc8c30ce. Future probes produce a real paragraph; probe shas change and do not compare with the recorded round. The probe stays a per-config determinism check, never a cross-config gate.

## 2026-11-04 Tap-overlap window (prod-down, user-authorized): prototype rejected, K2 knob found
- Branch tap-overlap-f1f2 (F1 defer + F2 async-D2H, default-off flags) built INSIDE the window: 2 source fixes (66bbaba6 deferral_context declared as a member; 04337556 definition stuck in an anonymous namespace) then a 3-configure toolchain saga — /usr/local/cuda-13.1 appeared after Nov 3 and configure 1 silently matched nvcc (my cache-diff regex [A-Za-z_]* hid the digit-named flags STRATA_HIP_GFX906/STRATA_ENABLE_DFLASH2); configure 2 matched /opt/rocm's mixed newer headers (231 errors vs the 6.1.0 clang); parity = the original pin set (HIP_GFX906=ON, ENABLE_DFLASH2=ON, HAVE_DFLASH2=OFF, CMAKE_HIP_COMPILER=rocm-6.1.0 clang, hip_DIR/hipblas_DIR=/opt/rocm-6.1.0, nvcc hidden) — verified 0 nvcc entries, identical build.ninja include refs, extended CACHE_MATCH, readelf linkage identical to the original binary.
- Verdicts (out/RESULTS-window.md, commit d50ca328; the 80k sha 1c7d4a6d held 18/18 rounds across every arm): F1off PASSED byte-identity (all shas reproduce; fresh-PP = B's median exactly). F1 prefill flat AND decode gate fail (cached-rerun drafts collapse to 0-of-0, dose follows the flag; r2 cached median 24.6 vs F1off 37.2). F2only flat + clean. F2 flat + inherits the collapse. None ships; both flags stay default-off; branch parked.
- K1 (split 16) decisively rejected: 2.26x slower PP, 2.1x slower 80k, TG 24.5 — layer imbalance. K2 (--prefill 16384) the one positive: 80k 583.7 = +21.0% same-day/same-binary (B r5 anchor 482.5), TG 33.1 = gate, fresh-18233 -9.5% — an opt-in knob for long-context-heavy serving, with one unreproduced 268 s post-80k follow-up stall as caveat.
- Upstream-relevant conclusion: the cross-GPU tap serial cost amortizes by CHUNK COUNT; it is not overlap-removable at these sizes (both deferral and async-copy were flat).
- Side findings: B r4 hit the engine's known #267 verify-timeout (layer 46) once — first occurrence in ~20 bench rounds + the five-arm record + 5h51m prod; old binary (not prototype-related); GPUs verified healthy after; the r5 retry was clean. The 80k metric ran ~6-11% fast this session vs its historical medians while fresh-18233 did not move (session frame, not binary: old binary today = new binary flags-off today, 482.5 both). STRATA_DF_EVENTS' section table dumps only at drafter teardown — the harness teardown kill suppressed it (F0's table lost; its absolute numbers were per-batch-sync inflated and unusable anyway).

## 2026-11-04 (later) K2 ADOPTED; same-day stock frame; stall root-caused; dose peak at 16384
- User order: adopt K2, compare vs stock with temp-0 shas, then hunt more levers autonomously (rerun+ignore on timeout deaths). prod config `--prefill 8192→16384` (backup `.bak-prefill8192`); bench old binary sha256-identical to the prod engine (d6096296…) so bench evidence transfers 1:1.
- Env mismatch found (bench arms ran STRATA_PREFILL_HELPER=1, prod 0) → armP0 = adopted config verbatim, plus same-day C-stock re-run. P0 2/2 CLEAN: 80k 582.9/586.6, segment-2 restored, gates byte-identical vs stock (80k sha 1c7d4a6d… in all four rounds; probes per-config deterministic; pp16384 sha identical everywhere). Same-day vs stock: 80k +7.6%, decode +~45% (~35 vs ~23.5), fresh-18233 −18.2%. All in out/RESULTS-window2.md.
- The 268 s stall root-caused in source: the DFlash2 SWA draft ring is 2048 cells (artifact geometry, dflash_runtime.cpp:453); a segment resume >~64 cells behind the append horizon is stale → the guard re-reads the whole context through the drafter (generate.cpp:16943). NOT config-mitigable (`--dflash-window` cannot enlarge the physical ring for official SWA geometry). Lever: `STRATA_DF_RING_MULT` (cf653943, opt-in, default 1 = stock geometry) scales the ring; armP1 bench = speed-neutral, fixes the deep-stall class by construction at mult 8; PARKED — prod keeps the genuine v0210 binary, deploy recipe in the results doc (binary swap + env) if the stall ever bites.
- Dose-response (window 3): 16384 is the peak — 24576 loses 10% on 80k (527.5), 32768 loses 37% (367.0); the amortization reverses past 16384. K3/K4 rejected.
- Residuals on record: P0 r2 one 80k cancel-and-retry at 99.99% of the read (retried clean in-round, wall ≈ 2 reads, sha held) — watch item; tg-scale segment flicker (missing exact checkpoint at resume → ~535-token re-read ≈ 6 s; state-dependent 0-4 of segments; checkpoint-placement, NOT ring) — open small lever.
- Ops lessons: never edit a script its running chain still reads (bash re-reads incrementally — cost C-r2 its judge txt; metrics survived in engine logs); C engine logs append across sessions (rotate per window); takedown pkill patterns must be self-match-safe (bracket trick — a naive pattern killed my own shell).

## 2026-11-04 window4: S-tuning verdicts, X0 crash, per-request DF_EVENTS dump
- S-arms vs same-day P0 (window2): min-p 0.4 → 80k 518 tok/s (−11%); min-p 0.6 → 434 (−25%), TG 20.3 (−43%);
  dflash-block 4 → 198 (−66%, fresh reads only, warm reads normal — per-chunk draft overhead scales with block count);
  block 6 → TG noisy-bad. ALL REJECTED. Draft acceptance never rose (P0 probe ~63%, S1 52%) — acceptance is
  drafter-mismatch-capped, not sampling-capped. Config space around adopted defaults is exhausted.
- Fresh-probe read walls scatter 7–119 s across arms (X0-r1 8.2 s, X0-r2 110.6 s, same prompt/config): the FIRST
  request races the helper's 6.25 GiB fill. Probe read tok/s is NOISE, never a gate. pp16384 (2nd request) is the
  comparable fresh-prefill metric. X0-r2 instrumented pp16384 265.7 tok/s ≈ P0 266.9 — DF_EVENTS does NOT halve 18k
  prefill (r1's 167.1 was probe-race contamination).
- X0-r1 crash: instrumented engine died in tg-after-park decode — verify GPU waits timed out at layer 16
  (verify.cpp:3492), decode decayed 30→4 tok/s after the 12.2 s/657 MB park. Fresh probe + pp healthy. Park rate
  measured again: 18244 tokens / 12.2 s ≈ 54 MB/s.
- 3rd lost section table → root cause: DFlashDrafter::release() only runs from upload()'s failure bail
  (dflash_runtime.cpp:376) — NO shutdown call site; the 100 s graceful-teardown window was never the fix.
  Patched generate.cpp: df_timing_drain+dump after every request summary (file-scope decls, no-op unless
  STRATA_DF_EVENTS=1). Table is now cumulative + crash-proof.

## 2026-11-04 prod observation: small fresh reads stall (pre-existing, all boots today)
- Every boot's ~200-token FRESH reads take 30-60+ s: 09:22 boot request 2 read 210 tokens in 39+ s;
  11:57 boot: 4 smokes of the same 206-token prompt, 31 s then 30-60+ s each (2 curl timeouts).
- Decode stays healthy in the SAME requests (30.0 / 35.5 / 43.7 tok/s), expert-cache hits 80-92.4%
  (NOT cold cache), and big reads are unaffected (18k fresh 265 tok/s, 80k 583 tok/s same day).
- Same family as the S1 tg-flicker (18-token warm re-read at 5.7 tok/s, 3.2 s) — a FIXED per-request
  cost on the read path that dominates only small prompts. Unconfirmed static analysis: candidate
  mechanisms are the conversation-cache/checkpoint placement path, not expert fetches.
- Practical: interactive UX defect only; throughput gates unaffected. Parked as a separate defect
  from the RAM-relief ladder (which proceeds on its own plan).

## 2026-11-04 12:42 — prod incident: read-stall crossed the watchdog; V-ladder window open

- Incident: a real ~19.8k fresh read hit the read-stall pathology (320 s vs ~75 s healthy), crossed STRATA_WATCHDOG_S=300,
  and the watchdog killed engine #1. serve respawned engine #2, but the launcher never respawns the helper: engine #1's
  death closed both channels and the helper exited cleanly (185 windows served). Prod ran degraded draftless until the
  user noticed. Confirms the small-read stall is prod-destructive under a 300 s watchdog, not cosmetic.
- Mitigation: prod config watchdog 300→900 (backup .bak-watchdog300, JSON validated); pair restarted and verified (helper
  second-channel connected, :8009 200, --prefill 16384). The restart's first smoke then stalled 140+ s on a 194-token
  read — worst yet today (30-60 s on earlier boots). Defect f5ddb808 upgraded: evaluate the tap-overlap binary + ring-mult
  right after the ladder.
- Recon (direct; the explorer child died on a connection error): the socket helper's slots hold MAIN-model experts in the
  profile's global rank order (one mirrored set over both channels); bench AND prod helpers run no drafter lane — the
  drafter is engine-side on the VII. SECOND_GPU.md: helper-held experts are left out of the RAM copy → that exclusion is
  the V0 mechanism. 3080 at its 6400 MiB fill cap, 1094 MiB free.
- V-ladder window open 12:42 (prod down, VRAM 0): run-arm.sh per-arm helper caps + armV0/V1.json (commit 4aecf2b3).
  V0 = fill 7000/slots 5100 (mechanism probe, ~+0.6 GiB); V1 = fill auto/slots 9000 (helper ceiling); V2 = winner +
  --mmap-experts (arena→file-backed, the big lever vs the 42.9 GiB anon baseline); V3 = + --dflash-remote-only (drafter
  to the helper, frees VII VRAM). Gates per rung: TG ≥ 33.1, 80k sha byte-identical, PP 583-587, medians; RSS delta in
  out/<ARM>-r<ROUND>-rss.txt.

## 2026-11-04 13:12 — V0 rejected; 80k read wedges at exactly 81,920 (5×16384); P0 control running

- V0 r1 (helper fill 7000 / engine slots 5100): the fill grew 4116→4340 slots (6.64 GiB) and stopped at the CARD'S
  BUDGET (698 MiB free), not at my caps → the 3080 helper ceiling is +0.39 GiB; V1 (fill auto/slots 9000) is moot.
  Helper-growth route rejected on ceiling alone.
- Gates: decode itself healthy (engine log: one 494-token generation at 35.5 tok/s; TG walls 30.5-62 s are
  read-stall-contaminated), but the read path failed hard: fresh-18k ~137 tok/s (same-day anchor 266) and the 80k
  read WEDGED at exactly 81,920 of 87,879 tokens = 5×16384, zero progress for ~5 min (29 progress lines at 10 s
  cadence) until I killed the arm. First hard-wedge capture of the read-stall defect.
- Escalation across today's boots: probe stalls 39 s (09:22) → 30-60 s (11:57) → 140+ s (12:29 prod) → 163.7 s +
  80k wedge (12:42 V0). X0 r3's 264 s pp16384 vs r2's 69 s this morning fits the same curve.
- Unconfirmed hypothesis (static analysis, not measured): stalls at power-of-2 token boundaries + the 43 GiB anon
  arena under 54/62 GiB pressure fit THP compaction stalls; V2 --mmap-experts (file-backed arena) would be both the
  RAM lever and the escape. THP settings + compact_stall counters recorded at journal time.
- Discriminator running: P0 r4 control (adopted config, same binary, watchdog 300 deliberately kept — a wedge
  reproduces the prod incident deterministically). Clean P0 → V0's exclusion interplay implicated, proceed to V2;
  wedged P0 → the defect owns the window, ladder pauses, defect thread promotes (tap-overlap binary + THP evidence).

## 2026-11-04 13:18 — orphan-chain: a slow-dying engine starves the next boot (P0 r4 void)

- P0 r4 control was VOID: a previous engine wedged-then-killed leaves a 43 GiB anon arena lingering for minutes
  while the process exits. With MemFree at 1.7 GiB, P0 r4's own engine starved during load (its serve 503'd the
  probe within ~2 min; the drive crashed on the 503) and it then became the next 42.4 GiB orphan. It finished dying
  ~13:16 on its own; memory then read 42 GiB free / 48 available — machine healthy again.
- RULE for the harness: before every arm boot, verify no rpcw-hip engine process remains AND MemFree > ~45 GiB.
  Two 43 GiB anon arenas cannot coexist; overlap = starvation-thrash (this is likely a large share of today's
  "escalating read stalls" after the kills began — though the original small-read stall predates them, present
  at 09:22).
- P0 r5 control re-running on the clean machine with the wedge watcher re-armed (two pkill self-match kills of my
  own watcher shells today — lesson: never put a plain process name and its bracketed pkill pattern in one shell).
- THP evidence (recorded 13:13): enabled=[always], defrag=[defer], compact_stall 631k, compact_fail 458k vs
  compact_success 173k — severe fragmentation; supports (does not prove) compaction-stall involvement in fresh
  reads. V2 --mmap-experts remains the candidate that is both RAM lever and escape.

## 2026-11-04 13:23 — r5 void too: leftover serve held :8019; bench fully reset; harness guarded

- P0 r5 was VOID before it started: the r4-era serve survived (its cmdline is dev/Strata/serve/server.py — none of my
  kill patterns matched that path), kept :8019, and r5's own serve died instantly ("port 8019 already in use").
  r5's probe went to the leftover stack and 503'd. The r4 serve's engine respawn was killed (502674, 42.8 GiB) and
  the port holder (pid 19022) killed by port lookup; after that: port free, MemFree 44 GiB, VII VRAM 0.02 GiB —
  first provably clean bench state since 12:42.
- run-arm.sh hardened (commit follows): pre-boot guard refuses a boot while any rpcw engine/serve lives or :8019 is
  held, then waits for MemFree >= 45 GiB and VII VRAM < 2 GiB (a killed engine's 43 GiB anon arena releases over
  minutes); the drain loop's rocm-smi parse fixed for cardN,total,used rows.
- Kill-pattern lessons banked: the bench serve's path is TREE-ROOT/serve/server.py (not build-v0210/serve); never
  put a plain process name and its bracketed pkill pattern in one shell (two self-kill incidents today).

## 2026-11-04 13:57 — path-1 verdict: the helper relieves ZERO host RAM in the plain mode

- V0 r2 measurement (live smaps, engine mid-drive, helper at its budget ceiling 4340 slots / 6.64 GiB of 7842 MiB):
  engine Rss 46.6 GiB, Anonymous 46.0 GiB, Pss_File 0.55 GiB — vs the P0 baseline 42.9 GiB anon with 4116 slots held.
  The +3.1 GiB is the in-flight read working set; the arena did NOT shrink by the helper's share. The documented
  "helper-held experts are left out of the RAM copy" does not fire outside the resident/mmap mode. Helper = compute
  acceleration only, today. Path 2 (extend the exclusion to the plain anon arena, refills from pack files) is the
  only helper-based RAM route; bounded by the 8 GiB card (~6.6-6.9 GiB relief).
- Bonus evidence: AnonHugePages = 0 on the 46 GiB arena despite THP enabled=[always] and compact_fail 460k vs
  compact_success 173k — the arena never gets huge pages; the read stalls are the failed-compaction churn on the
  fault path. (Unconfirmed attribution, but the counters fit.)
- Probe on this boot ran 16.8 s (no stall) vs 163.7 s on V0 r1's boot — the anon-mode stall is boot-dependent,
  consistent with the fragmentation state at arena-build time.
- User sequence approved and executed: path 1 (this measurement) -> STOP (prod restored on old settings) -> path 2.

## 2026-11-04 14:05 — plan approved for step 2; Phase A recon dispatched

- User approved (after plan-mode discussion): W1 = opt-in --expert-helper-ram-relief dropping helper-held pairs from the
  plain anon arena with the PROMPT HELPER READOPTED (the 3080 computes prefill rows for held experts — the user's
  architecture: "the entire point is to make the cuda card perform all the work the rocm cards are able to do"; file
  reads only as the emergency CPU-window fallback); W2 = engine-side huge pages (madvise MADV_HUGEPAGE + arena-first
  boot order; user explicitly declined the hard-reservation/hugetlb-pool route). Standing policies reaffirmed:
  wedge -> rerun; strict serial kills, no grace; autonomous till done; prod toggling at my discretion.
- Prod verified on old settings 14:04 (:8009 UP, helper 2nd-channel, --prefill 16384, no mmap flag, watchdog 900,
  PREFILL_HELPER=0, engine anon 43.0 GiB). Phase A scoping (both workstreams) dispatched read-only to the explorer
  while prod serves.

## 2026-11-04 14:16 — V2 re-adopted by user order after the stall lottery bit again

- The 14:10 plain-mode boot wedged on user traffic (20,066-token read stuck at 16,384 for 240+ s = the ~80 tps report);
  user order: 'get us v2 back'. Both prod configs re-adopted --mmap-experts (watchdog 900; .bak-pre-v2 remains the
  plain-mode restore point). Known trade (bench-measured): decode ~27 vs 33-35, PP 534 vs 583-587, in exchange for no
  stalls (probe 20.1 s, small reads 0.15-0.4 s), bounded 15-30 s boundary pauses, engine anon ~7 GiB + ~36 GiB evictable.
- Found and killed a stray bench serve (pid 987334, .venv python running TREE-ROOT serve/server.py with the armV0-r2
  config on :8019, alive since 13:52 — the same tree-root-path pattern that evaded kills at 13:22; its engine respawns
  during the PP-tank window were an untracked memory/VRAM competitor). Machine verified clean before the V2 relaunch:
  ports free, 51.2 GiB available, 3080 drained.
- W1/W2 recon (revived after a cancellation) still running read-only; W2 (engine-side huge pages) remains the accepted
  path to recover the perf gap without the stalls, once prod is stable.

## 2026-11-04 14:22 — V2 rolled off within minutes; final prod state = plain + prompt helper ON

- User order sequence: 'get us v2 back' (14:16) -> saw the mmap RAM profile -> 'i told you we dont use mmap' ->
  'get us the best performing earlier config back' + 'i want my ram locked and my helper active - prompt helper thing'.
  POLICY CLARIFIED: prod up/down at my discretion; CONFIGS change only on explicit user order.
- Final applied state (verified in JSON): BOTH prod configs --mmap-experts REMOVED, env STRATA_PREFILL_HELPER=1
  (user-ordered; was 0 on the morning config), STRATA_WATCHDOG_S=900 kept; launchers clean (config-driven).
- Self-kill incident #3: the combined takedown+edit shell SIGKILLed itself — the launcher path literal inside the
  python heredoc matched my own 'pkill -f [l]aunch-prod' pattern. No damage (config edits simply never ran; redone
  in a kill-free shell). Rule reaffirmed: kill patterns and the strings they match NEVER share a command.
- W1/W2 Phase-A recon continues SOLO (explorer child died on infra twice — ConnectionRefused both times).
  Findings so far: arena machinery lives in src/core/expert_source.cpp (STRATA_ARENA_MMAP file-backed mode ~5893,
  that is the V2 mmap path); helper_holds is gated on STRATA_DISJOINT_ADAPT (expert_helper_candidates.cpp:189)
  which our env does NOT set — the exclusion machinery exists but was never engaged in our runs.

## 2026-11-04 14:27 — winning config fully restored + verified; three-way gate set

- PREFILL_HELPER flipped 1->0 within a minute of my over-application (user: 'i thought we had prompt helper off at
  our winning config??'). Prod verified 14:25 on the exact winning config: helper 2nd-channel x1, --prefill 16384,
  NO mmap, PREFILL_HELPER=0, engine anon 43.2 GiB (RAM locked), smoke 8.75 s, no wedge. Only deltas vs winning cfg:
  watchdog 900 + helper no-idle-exit (both failure-path-only, user informed).
- FINAL GATE (user order 14:26): final verification compares candidate vs STOCK STRATA and vs THE WINNING PROD
  CONFIG, quality AND speed, as previously: 80k sha 1c7d4a6d9be118d8 byte-identical across all three; TG >= 33.1,
  PP frame 583-587, fresh-18k toward 337, medians, one arm at a time.
- Phase A recon re-delegated to explorer (third attempt; first two died on infra ConnectionRefused).

- W2 machine recon (14:27, prod live): THP enabled=[always] defrag=[defer]; the arena is ONE 40.0 GiB VMA, already
  2 MiB aligned, AnonHugePages 0.00 process-wide (also the 8.7/8.9 GiB VMAs); compaction 199k success / 493k fail
  (+14k fail in ~15 min). Conclusion: eligibility and alignment were never the blocker — under defrag=defer no
  synchronous compaction runs at fault time and background kcompactd cannot assemble 2 MiB blocks on this
  fragmented machine. Engine-side W2 = MADV_COLLAPSE post-fill sweep + MADV_HUGEPAGE + arena-first boot order;
  a plain machine reboot (allowed) resets the fragmentation baseline.
- MADV_COLLAPSE (man 2 madvise, kernel 7.3.0): independent of ALL sysfs THP settings (works under defrag=defer); best-effort synchronous per-2MiB, partial failures continue the sweep; auto-clamps to alignment; requires >=1 resident page per region (post-fill arena qualifies). W2 engine design fully validated as viable — no root needed.

## 2026-11-04 14:35 — Phase A recon complete (explorer); implementation dispatched to fixer

- Full file:line map landed (arena=PinnedArena via pinned.cu:462 anon mmap, arena_off expert_source.cpp:6359;
  absent-not-representable; prompt tier claims HELD experts only via select_prompt_rows socket.cpp:188-254 with
  the blob_stable landmine at prefill.cpp:4963; resident-exclusion code gen.cpp:10413-10436 is mmap-mode-only;
  CPU fallback reads blob() at expert_source.cpp:5221/4796 + fallback.cpp:92; MADV_HUGEPAGE exists at
  pinned.cu:506 but self-disables under THP=always; no MADV_COLLAPSE in tree; disjoint_adapt gates GPU cache
  admission ONLY via helper_holds gen.cpp:9020 used at 13360/13635/21253).
- W2 split: H1a (env STRATA_ARENA_COLLAPSE=1: HUGEPAGE-under-always + post-fill COLLAPSE sweep, no reorder) —
  bench first; H1b (arena-first boot reorder) deferred, high coupling risk (split search, pin-by-need pricing,
  conversation-cache sizing, PLE give-back hook).
- W1 design frozen: post-hoc drop of held pairs (MADV_DONTNEED 2MiB-clamped ranges, VA/offsets unchanged);
  absent table + staged file-read transient copies; prompt eligibility keyed to holds(); one-shot background
  refill from files on helper channel loss.
- Dispatched to fixer (one agent, serial): commit1 W2a, commit2 W1; CPU-only builds, prod untouched. Arms V4/H1
  already scaffolded (68e40044).

## 2026-11-04 16:28 — prod stall-death + VRAM-ghost boot failure; fixer died post-commit; solo mode ordered

- Prod (14:23 boot) died ~16:20 on the pre-existing stall defect (engine self-stop, 'stopped making progress',
  328 s, exit -15). First relaunch failed: the new engine priced card1 VRAM while the dead engine's allocation
  lingered -> expert caches left 1.1 GiB -> weights arena (1466 MiB) split failure -> serve respawn loop. Clean
  takedown + release-wait (52 GiB avail, card1 16.0 free) -> relaunch. The prod launcher needs the bench
  harness's release-wait guard (noted for later).
- Fixer died on ConnectionRefused (3rd child infra death today) at ~15:3x-16:2x: W2a committed (0d661511), W1
  landed uncommitted across 8 files, no post-edit builds.
- USER ORDER: stop delegating; implementation work proceeds solo.

## 2026-11-04 16:55 — H1-r1 sweep void: MADV_COLLAPSE EINVAL root cause + fix

- User spotted nr_anon_transparent_hugepages pinned at 127 (254 MiB). Engine log: '0 of 20463 2 MiB chunks
  collapsed, 20463 refused: Invalid argument'.
- Userspace probe on this kernel (7.3.0-xanmod): MADV_COLLAPSE with a non-2MiB-aligned START returns EINVAL;
  the kernel clamps only the length (the man page's 'automatically clamp' misled the implementation). An aligned
  call on this fragmented box returns ENOMEM (no free 2 MiB block while the H1 engine holds ~45 GiB).
- The sweep stepped a->base + k*2MiB; the H1 arena's base is page-aligned only (the 2 MiB-align dance in
  pinned.cu:460-470 belongs to the plain constructor; this arena took another path) -> every call misaligned.
- Fix (solo): the sweep now derives the aligned window (start rounded up, end rounded down from base+want) and
  steps whole 2 MiB blocks inside it. Hip rebuild in flight; H1 rerun comes after r1's sha/PP/TG legs land.

## 2026-11-04 16:57 — H1-r2 death classified; fail-fast gate added; V4 is next

- H1-r2 (fixed aligned sweep): the sweep STILL refused all chunks — '0 of 20462, EINVAL'. The engine then died
  mid-drive: serve log 'Unrecoverable native verification failure: verify: timed out at layer 39; its GPU waits
  were released but the GPU did not finish within 5 s (#267)' — the team's tracked GPU-hang class, transient;
  the r1->r2 binary delta is 10 inert boot-time lines, so not today's code. Wedge policy: rerun.
- Probes: an ALIGNED MADV_COLLAPSE on a plain fresh mapping returns ENOMEM even with the box quiet at 51.8 GiB
  free (direct compaction cannot assemble one 2 MiB block — the day's ~20 boots shredded the buddy allocator);
  the mlock pinning probe could not run (ulimit -l in this shell). The engine's EINVAL (vs the probe's ENOMEM)
  still points at cudaHostRegister-pinned pages refusing collapse outright — fix shape: sweep before
  registration (deferred-registration path) or unreg-sweep-rereg; likely needs a machine reboot for a clean
  fragmentation baseline to matter at all.
- USER CRITICISM adopted: no more full legs when the verdict is knowable at boot — run-arm.sh now has a
  fail-fast gate (void sweep / helperless / dead engine -> teardown + exit 3 before the drive).
- Pivot: V4 (W1 --expert-helper-ram-relief + prompt helper + 4200/6400) is the primary deliverable and is
  independent of W2's huge-page fight; it runs next (announced). W2 resumes after V4's numbers.

## 2026-11-04 17:02 — UNIFIED ROOT CAUSE: the whole-arena cudaHostRegister blocks W1 AND W2

- V4-r1 boot: helper fill report 4116 pairs / 6.25 GiB, then '--expert-helper-ram-relief: nothing dropped
  (4116 of 4116 pairs are pinned memory the GPU has an alias into)'. On Linux the arena is registered WHOLE
  (#253 'Linux keeps the whole-arena pin'), so every helper-held blob sits inside a registered range and the
  W1 guard (correctly) refuses to release driver-pinned pages.
- Same mechanism explains W2a: MADV_COLLAPSE EINVALs on every chunk because the pages are GUP-pinned by
  cudaHostRegister (VmFlags show no 'lo' — GUP pinning is invisible there; the plain-mapping probe's ENOMEM vs
  the registered arena's EINVAL contrast fits).
- Fix direction (both workstreams): register the arena EXCLUDING the helper-held ranges — a by-need-style
  complement registration whose holes are NOT locked (the existing ByNeed ctor mlocks its holes; a no-lock
  variant is needed). This config runs --pcie-frac 0, so the alias over held pages is likely unused —
  registration of those ranges may be droppable outright, cost measured by the arm.
- V4-r1 drive continues: still measures the prompt-helper half (STRATA_PREFILL_HELPER=1, tier claims held
  pairs) + the 80k sha gate. After it: probe4 on the 3080 (announced) formalizes pinning->EINVAL, then the
  registration surgery, then V4-r2/H1-r3.

## 2026-11-04 17:2x — surgery implemented + both backends green; V4-r1 classified

- Surgery (deferred-complement registration) implemented solo across pinned.hpp/pinned.cu/expert_source.hpp/
  expert_source.cpp/generate.cpp: ReserveOnly arena -> fill on plain pages -> W1 registers the complement of
  the held pairs at the fill report + rebuilds alias tables -> set_unfilled drops the held pages; W2 sweeps
  unregistered then registers the whole span. One compile fix (arena_ is stored void* - static_cast).
  ninja green: rpcw-cuda helper + rpcw-hip engine. Build lesson: never pipe ninja into `head` (SIGPIPE kills it, exit 141).
- V4-r1 VOID on relief (4116/4116 pairs pinned - the root cause above) and its drive died at tg1024 with a 503
  after tg degraded to 18.6 tok/s + 'drafter returned candidates outside the vocabulary' at pos 579. The drafter
  anomaly is PRE-EXISTING (B-r2, F0-r1, F1-r1, F1-r2 logs show it) - not W1 code. Death classed #267 wedge
  (2nd long-drive death today). Not rerunning r1: r2 with the fix supersedes it.
- probe4 SKIPPED as redundant: H1-r3 on the unregistered arena is the direct collapse experiment.

## 2026-11-04 17:5x — V4-r2 root-caused (mlock), fixes landed, r3 ready

- V4-r2 chain closed: harness shells have UNLIMITED ulimit -l -> register_after_fill(comp, lock=true)
  mlocked the held spans -> MADV_DONTNEED on mlocked pages = EINVAL x4116 -> pairs neither dropped nor
  registered -> first lent-slot fill DMA'd from an unregistered host blob -> invalid argument -> serve
  400 at the probe. CPU probe: plain-page inward DONTNEED 2651/2651 OK on this kernel.
- Fixes committed: drop-before-register; complement = everything except what DROPPED; lock_rest=false
  under relief (registration pins; mlock would fault dropped pages back); errno in the refusal line;
  refill_run keeps dropped pairs file-served until restart under the flag (no runtime table rebuild).
- Next: V4-r3 (announced) then H1-r3 (the THP experiment: collapse now runs on unregistered pages).

## 2026-11-04 17:3x — V4-r3: drop WORKS (4116/4116, 6.24 GiB back); residual bug = span-edge alias; fix = outward rounding

- V4-r3 boot gates: '4116 of 4116 pairs dropped (6.24 GiB given back; 0 pinned, 0 madvise refusals)' + 'registered what stayed
  (4116 dropped, 3033 ranges, 33.71 GiB)'. The mlock fix + drop-before-register order work.
- Probe still died 6 s in: SAME 'fill_slot_queued: invalid argument'. Unified cause for r2 AND r3: register_after_fill rounded
  complement spans INWARD -> unheld blobs at span boundaries have partial edge pages outside the registration -> full-blob DMA
  past the alias window. r2's held-undropped theory was incomplete; the edge-pair bug was present in both.
- Fix: outward page rounding in register_after_fill (span-edge pages stay mapped - only wholly-inside pages were dropped - and
  carry kept bytes that must be DMA-able; wholly-dropped pages never reached). Rebuilt -j11 both trees, committed.
- Also: builds now -j11 (user standing order; pinned in git config strata.buildJobs). THP counter ~109 during V4 = expected
  (no sweep on this arm; H1-r3 is the THP experiment).

## 2026-11-04 17:36 — measured: defrag=alone is not enough; V4-r4 classified (wedge #3); sequence set

- THP fault probe (user set defrag=always, verified live in sysfs): fresh 4 GiB anon mmap+fault under the
  running arm -> AnonHugePages 0.00 GiB (0 of 2048 possible 2 MiB pages). Direct compaction cannot build
  even one huge block at this occupancy/fragmentation. Measured -> the reboot is the unlock; the sweep (H1)
  then makes the arena huge on the first boot; defrag=always keeps everything else huge-friendly after.
- V4-r4 FINAL: the two code fixes are CONFIRMED (probe answered 32.1 s, sha 709bd8fb67692e9e = byte-identical
  to r1; pp16384 69.8 s vs r1's 212.9 s; zero serve errors through pp) — then tg1024.0 died with 503, the
  same death point/class as V4-r1 (wedge). Third long-drive wedge today across different code (H1-r2, V4-r1,
  V4-r4) -> machine/driver-class, and the reboot may clear it too. tg/sha legs void.
- SEQUENCE (user reboots when ready): reboot -> H1-r3 FIRST (sweep's best shot = cleanest allocator,
  announced) -> V4-r5 to complete W1 gates (TG >= 33.1 + 80k sha; fragmentation-independent) -> three-way
  report. Gates banked so far: drop 4116/4116 / 6.24 GiB, complement 3033 ranges / 33.73 GiB outward,
  anon 43.8 -> 38.7 GiB, probe sha byte-identical, pp 69.8 s.

## 2026-11-04 17:5x — THP dead kernel-wide; H1 void x3; cmdline defrag param ignored

- H1-r3: sweep 0/20462 EINVAL on the clean post-reboot unregistered arena -> the harness fail-fast gate
  killed the arm before the drive (working as designed). Third void, now on ideal conditions.
- probe2 (userspace, solo): hinted (MADV_HUGEPAGE) dance-mapped 256 MiB fault -> 0 kB AnonHugePages;
  unhinted -> 0 kB; strace confirms madvise(..., MADV_COLLAPSE) = -1 EINVAL from the kernel on a perfectly
  aligned hinted anonymous mapping. Fault-time AND explicit THP both categorically dead on
  7.3.1-x64v3-xanmod1 despite /sys enabled=[always] and CONFIG_TRANSPARENT_HUGEPAGE=y.
- /proc/cmdline carries transparent_hugepage.defrag=always but the runtime value is [defer]: the boot
  parameter was ignored (unknown param on this kernel). The user-visible ~100-150 AnonHugePages are
  early-boot remnants.
- Consequence: W2 (huge pages for the arena) has NO engine-side path on this kernel build - reported as a
  box/kernel defect, not an engine bug. W1 (RAM relief) is unaffected. V4-r5 next for the W1 gates.

## 2026-11-04 18:0x — CRACKED: per-order mTHP [inherit] is broken on this kernel; user root test proves it

- User wrote explicit values to /sys/.../hugepages-2048kB/enabled: with 'always' AND with 'madvise',
  MADV_COLLAPSE returns OK on the probe (fault-time THP stays 0 kB - also inherit-gated - but the sweep
  never needed fault-time). Root cause of all three void sweeps: the xanmod 7.3.1 [inherit] resolution is
  dead for both THP paths while the top-level shows [always]. The cmdline defrag param is ignored too.
- Engine: arena now gets MADV_HUGEPAGE under STRATA_ARENA_COLLAPSE before the sweep (committed) - the
  hinted VMA is the proven-working configuration in both per-order modes.
- Persistent box fix for the user: /etc/tmpfiles.d/thp-2m.conf -> 'w /sys/kernel/mm/transparent_hugepage/
  hugepages-2048kB/enabled - - - - always' (their root, one time).
- NEXT: H1-r4 (announced) - sweep should finally collapse ~20k chunks; then V4-r5 for the W1 gates.

## 2026-11-04 18:2x — THP: LKML dead-end, root-vs-scope discriminator; V4-r5 running for W1 gates

- LKML 2025-11-07 thread: MADV_COLLAPSE EINVAL on DIRTY FILE-BACKED text pages (collapse_file path,
  CONFIG_READ_ONLY_THP_FOR_FS) - first call EINVAL, retry succeeds after writeback. Our arena is anon
  private (different code path), so that bug does not explain us; the meta-lesson (EINVAL can be a
  retryable transient in collapse paths) is noted.
- Eliminated: seccomp (0 everywhere), systemd-run escape (still EINVAL), CapEff is plain-user normal.
  Remaining deltas for the user's two T3-OK vs my deterministic EINVAL on the same binary+sysfs state:
  their run was root (sudo/root shell), or their terminal scope differs from my tmux-spawn scope.
  ASKED USER: run /tmp/agent/collapse5/probe2 as a PLAIN user in their terminal (no sudo).
  - EINVAL => collapse is root-gated on this kernel => the engine can never sweep => W2 report-only.
  - OK => arms must be launched from outside my scope (their terminal wrapper).
- V4-r5 launched (announced): W1 relief gates - TG >= 33.1, PP frame, 80k sha byte-identical, anon delta.

## 2026-11-04 18:3x — THP: collapse NOT root-gated (user plain-user OK); context hunt narrows to env or hint-stickiness

- User ran /tmp/agent/collapse5/probe2 as a PLAIN user in their own terminal (no sudo), per-order [madvise]
  live: T3 collapse OK. Root-gate theory dead. Same binary+sysfs+uid in my shell = deterministic EINVAL.
- Remaining delta = execution context (my opencode tmux-spawn scope). Two theories under test:
  1) inherited environment (LD_LIBRARY_PATH etc.) - test: env -i probe2.
  2) MADV_HUGEPAGE hint not sticking (VM_HUGEPAGE missing) - test: probe4 prints hint return + smaps
     VmFlags 'ht' bit + per-chunk collapse count.
- If env -i fixes: scrub env in run-arm.sh for the engine. If neither: hand the user a one-liner to run
  the H1 arm from their terminal (arms launched from my context inherit the broken context).

## 2026-11-04 — THP process-policy cause measured; earlier kernel claims withdrawn

- This session's child reports PR_GET_THP_DISABLE=1 and THP_enabled=0. The unchanged probe2
  returns EINVAL there. PR_SET_THP_DISABLE=0 succeeds as the ordinary user; no root is needed.
  Linux documents this flag as inherited across fork/exec and overriding even MADV_COLLAPSE.
- Earlier claims that XanMod or per-order inherit was broken were not established: those probes
  inherited a process-wide THP veto. They do not justify a kernel diagnosis or sysfs persistence fix.
- The old 2x256MiB probe stalled after clearing the flag; PID335894 was in D state and received TERM.
  Use a filled single2MiB mapping for further CPU tests, with the hint applied after filling.
- V4-r5: probe11.4s, sha709bd8fb67692e9e; pp16384 then failed HTTP503. No valid TG/PP/80k verdict.
  Wrapper cancelled by the host restart; AMD VRAM had not drained at last inspection.
- Plan: narrowly scoped process-policy override inside STRATA_ARENA_COLLAPSE only, restore before
  registration, CPU before/after evidence, affected backend builds at -j11, then an announced H1 arm
  only after hardware/resource recovery. No production/config changes or performance claims.

### THP CPU result and scoped fix

- PASSED: `timeout --signal=TERM --kill-after=5s 45s python3 -u tools/probe_arena_thp.py`.
  Inherited policy1: MADV_COLLAPSE returned EINVAL. Scoped clear0: collapse succeeded;
  the mapping had2048KiB AnonHugePages. SHA256 of the2MiB payload stayed identical.
  Disabled policy1 and the original process policy were restored. Exit0, no root/sysfs/GPU use.
- Engine change only inside the existing STRATA_ARENA_COLLAPSE opt-in: detect full process disable,
  clear it for the already-filled sweep, restore before string allocations or DMA registration;
  report query/override/restore failures. Already eligible policies0/3 are not changed.
- The default path adds no prctl calls. Incorrect kernel/per-order diagnosis comments removed;
  MADV_COLLAPSE minimum kernel corrected to6.1. HIP and CUDA engine/helper builds queued at -j11.
- Latest process inspection: no benchmark/probe processes remain; MemAvailable46.2GiB.
  Engine-scale huge backing, registration survival, quality and performance are not yet tested.
- Preflight queries: both AMD GPUs use10936320B VRAM each; NVIDIA86MiB; ports8009/8019 clear.
  The2MiB per-order setting is now[inherit], top-level[always], defrag[defer]; no sysfs changes made.
  No configured SYCL tree or icpx compiler is available, so SYCL compilation is not run.
- PASSED: `ninja -j11 -C build-v0210/rpcw-hip strata` and
  `ninja -j11 -C build-v0210/rpcw-cuda strata strata-expert-helper` (both engine backends compiled
  expert_source.cpp; CUDA also linked the helper). Compiler warnings were reported; no build errors.
- H1 harness now saves full engine smaps and selected status fields after registration/readiness,
  before the drive. This evidence survives later HTTP errors and distinguishes real huge backing
  from a successful syscall alone. Other arms are unchanged.
- Fix committed as1abe8b9a on tap-overlap-f1f2 (engine/probe/H1 capture only).
  `bash -n bench/ab-v0210/run-arm.sh`, `python3 -m py_compile tools/probe_arena_thp.py`,
  and `git diff --check` passed. No default-path prctl calls or production setting changes.
- H1-r5 launched after announcement: both AMD gfx906 cards and RTX3080 helper, estimated5minload
  plus25mindrive, shell sh_1a795c36b001A2FvJkDwAhyxtU. Engine-scale THP and performance verdict pending.

## 2026-11-04 — H1-r5 complete; user orders full RAM-relief validation

- H1-r5 completed the full drive, no HTTP failure. Sweep12824/20462 chunks collapsed;
  6719 transient refusals and919 ENOMEM. Post-registration smaps records26263552KiB
  AnonHugePages (25.05GiB); status records THP_enabled0. Thus huge backing survives registration
  and the inherited disable policy is restored. Artifacts: out/H1-r5-pid197525-{smaps,status}.txt.
- H1-r5 fresh18233-token PP180.3tok/s (101.143s), long87879-token PP469.2tok/s (187.300s).
  TG segments16.4–41.2tok/s, variable. The80k hash is
  1c7d4a6d9be118d8f3143f7a8ab74d4b5a39811dafeaa2e6a6fa73e0f726dd73, matching the gate.
  This arm fails historical performance gates; not adopted. A new control is needed to attribute
  the slowdown to THP rather than the session frame. H1 did NOT enable RAM relief.
- USER ORDER: run full speed/quality validation of CUDA-helper RAM relief. Plan: V4-r6 first,
  then winning P0-r7 and stock C-r3, serially. Three TG requests per arm plus fresh18k/long88k,
  full80k output hash/text comparison and end-of-drive host RSS/anonymous/PSS evidence.
  V4 includes --expert-helper-ram-relief and PREFILL_HELPER1; P0 has neither relief nor prompt-helper;
  stock C has no CUDA helper/DFlash and prefill8192. THP is off for all three. No production changes.
- Preflight: code/harness clean before the new gate; both AMD cards10948608B VRAM, NVIDIA86MiB,
  MemAvailable52.07GiB, ports8009/8019 clear, lock limit unlimited. No live benchmark processes.
- Add a V4-only fail-fast gate: a missing/zero dropped-pairs report stops before the drive.
- Gate checks passed: bash-n; previous V4-r5 positive drop accepted; zero-drop fixture rejected;
  git diff-check clean. Harness and H1-result record committedafe96ce5.
- V4-r6 launched after announcement, shell sh_1a7a80b2e001zGoPvdcHVpcXdz:
  `env -u STRATA_ARENA_COLLAPSE -u STRATA_DRIVE_STOP_AFTER -u STRATA_ARENA_MMAP bash bench/ab-v0210/run-arm.sh V4 6 3`.
  Full drive, no THP, no production change; current P0/stock controls follow after reconciliation/drain.
- Existing report.py protocol requires at least2kept boots per arm before performance claims.
  Plan a second clean block V4-r7/P0-r8/C-r4 after V4-r6/P0-r7/C-r3 (unused labels checked at launch).
  Use only new kept rounds for medians; retain failed arms separately. Reuse its steady-TG filter
  (read<400, generated>400) and also report whole-request walls so checkpoint rereads are not hidden.

## 2026-11-04 18:5x — V4-r6 disposition: #267 wedge class; drive retries once; lanes relaunched

- V4-r6's 503 root: `verify: timed out at layer 47; GPU did not finish within 5 s (#267)` at ~1021 tokens of the
  first TG1024 — the box's documented gfx906 lost-completion family (80+ stall reports on stock 0.1.45 since 10-21,
  both cards). Not proven relief-caused: 3 of 6 V4 rounds wedged, while same-day H1-r5 and window-2 P0 were clean.
  Discriminator going forward: a V4 wedge with clean same-day P0/C controls.
- Fix (user order, solo): bench/ab-v0210/drive.py now retries a leg ONCE on HTTP 503 after waiting for /health
  (the server restarts the engine on the next request; reload is minutes). Retry is recorded in the drive log/JSON;
  the retried leg's wall includes the reload, engine judge lines stay clean. py_compile, bash -n, git diff --check
  passed; committed.
- Lanes relaunched serially with fresh unused labels: V4-r7, then winning P0-r7, then stock C-r3; second block
  V4-r8/P0-r8/C-r4 if healthy (report.py protocol: >=2 kept boots per arm for performance claims). THP off; no
  production change.

## 2026-11-04 19:06 — V4-r7 complete: relief works mechanically, TG gate fails 3-for-3

- Full drive finished, no wedge, no 503 retry. Relief: 4116/4116 pairs dropped (6.24 GiB), complement
  3033 ranges / 33.73 GiB. Probe 42.2 tok/s; fresh-18k 271.9; 88k 503.3 (second segment restored in
  526 ms — segment health clean).
- TG (report.py rule, 6 steady segments): 7.9 / 11.9 / 12.4 / 19.6 / 27.0 / 29.4 → median 16.0 tok/s
  vs gate 33.1 — FAIL. Same degraded-TG signature as r1 (18.6) and r6 (19.0 pre-wedge): three relief
  rounds, three sub-gate TGs. 80k decode tail also degraded (129 @ 5.4 tok/s).
- End-of-drive RSS: engine Rss 56.4 GiB / anon 41.2 GiB / Pss_File 15.0 GiB (the 80k prompt-cache
  spread resident — comparable only against the same-point P0-r7 snapshot); helper Rss 0.44 GiB, its
  file pages evicted under memory pressure (re-read on demand).
- Decisive next step: same-day P0-r7 winning-config control (no relief), then C-r3 stock. If P0-r7
  TG holds ~33+ today, the TG loss is relief-caused (helper/file path at decode); if P0-r7 also
  sags, it is the session frame. No adoption either way without user order.

### V4-r7 correction (19:1x)

- My results bank briefly recorded "80k sha = GATE FAIL" with an empty value: the bank script read
  out/V4-r7.json, which by drive.py design carries no `sha` keys (shas go to the drive log/stdout).
  Recomputed from the stored text per report.py convention: probe `709bd8fb67692e9e` and 80k
  `1c7d4a6d9be118d8` — both quality gates PASS, matching the drive log. V4-r7 verdict unchanged:
  quality PASS, TG median 16.0 vs 33.1 FAIL.

## 2026-11-04 19:17 — P0-r7 control lands the verdict: relief costs TG decode

- P0-r7 (winning config, no relief, no THP), full drive clean, no wedge, no retry:
  TG median 38.1 tok/s (35.4/39.1/34.3/40.6/37.0/40.7) — PASSES the 33.1 gate. V4-r7 relief ran
  16.0. The TG degradation is RELIEF-CAUSED (three relief rounds sub-gate vs a passing control),
  not today's session frame. The user's watched "stall" was this control's own fresh-18k leg:
  110.1 tok/s / 165.9 s — fresh-read scatter today (V4 271.9/267.5, H1 180.3), engine finished it;
  not a wedge, no #267.
- 88k: control 576.5 (within 1% of the 583-587 frame) vs relief 503.3 (-13%).
- Quality: probe sha 707704f68c5947f0 (P0 canonical, byte-identical to window-2) and 80k sha
  1c7d4a6d9be118d8 — both arms PASS. Second 80k segment restored in 258 ms.
- Host RAM, end-of-drive anonymous: relief 41.2 GiB vs control 47.4 GiB = -6.2 GiB — the relief
  goal is delivered. (Rss is not comparable: V4's snapshot carries 15.0 GiB Pss_File of resident
  prompt-cache spread vs P0's 0.57 GiB; anon is the like-for-like.)
- Provisional verdict: relief = 6.2 GiB host RAM back, quality byte-identical, TG decode 2.4x
  slower → FAILS the adoption gate as-is. The cost sits in decode-time service of the dropped
  pairs (helper rows / pack-file reads). C-r3 stock next for the three-way table; adoption only
  on explicit user order.

## 2026-11-04 19:4x — Relief TG bottleneck: static chain complete (user order: find it)

- Log forensics first: V4-r7 vs P0-r7 TG legs have IDENTICAL work — decode cache hit rate
  ~96% both, miss rows 39.6-49.7k vs 37.1-47.8k, helper reqs 5.2-7.8k vs 5.8-7.5k. The relief
  arm is not doing more; it is serving the same misses slower (walls 41-62 s vs 12-15 s per leg).
- Root, from expert_source.cpp: under relief `unfilled_` stays 4116 forever (refill_run is a
  deliberate no-op — dropped pairs stay file-served until restart, 6713-6724). Every
  blob/blob_stable/blob_at/transient/copy_blob/note_async_read/begin_layer/prefetch/warm call
  then takes the GLOBAL `file_mu_` — including calls for RESIDENT pairs, which lock just to
  check `unfilled_at()` (6506-6524, 6756-6768). `blob()` performs the pack-file read for a
  dropped pair INSIDE the critical section (6508-6509). `begin_layer` (6787-6791) forwards the
  per-layer decode ids into the file source's io-prefetch/aging machinery on every layer of
  every token — machinery a run without relief never executes.
- Two mechanisms: M1 — a staged pack read holds the global lock for a disk-read's duration
  (matches the bimodal TG segments: 27-29 tok/s page-cache hits vs 8-12 tok/s real reads; the
  80k drive had evicted the pack cache); M2 — convoy: every decode thread calling any of the
  nine entry points queues behind those holds. P0 is immune: `unfilled_==0` short-circuits
  before the lock; all blobs are pointer arithmetic.
- Fix candidates (not implemented — user order needed): (A) lock-free `unfilled_at` snapshot,
  take `file_mu_` only for real dropped-pair reads; (B) small pinned LRU of staged dropped
  blobs so decode never re-reads pack files; (C) piggyback blob bytes on the helper's row
  answers; (D) boot-time posix_fadvise WILLNEED over the held 6.24 GiB (weakest: observed
  page-cache pressure evicts it).
- Live discriminator queued behind C-r3 (GPUs busy): one V4 TG leg sampling /proc/<pid>/io
  read_bytes delta + futex contention. Predicts: read_bytes grows ~100s of MB per TG leg under
  relief, ~0 under P0.

## 2026-11-04 19:35 — C-r3 lands the three-way table; relief verdict final for tonight

- C-r3 (stock, no helper) clean, no retries: TG median 27.6 (segments 26.6-28.1), fresh-18k
  340.2 tok/s (inside the stock 337-339 band — the box is not globally slow today; P0's own
  110.1 fresh-18k was that config's noisy leg), 88k 550.0 (in band), probe sha d7e714573946a47b
  = C-r1 canonical, 80k sha 1c7d4a6d9be118d8 — byte-identical across ALL THREE arms.
- Three-way (same evening, same binary for V4/P0): TG P0 38.1 (passes 33.1) / C 27.6 / V4 16.0
  (2.4x below winning, 1.7x below stock); 88k P0 576.5 / C 550.0 / V4 503.3; engine anon
  V4 41.2 vs P0 47.4 GiB (-6.2, the relief goal) and C 46.2.
- Verdict: relief delivers its RAM goal with byte-identical output but fails the TG adoption
  gate as-is. Bottleneck (static): global file_mu_ convoy + pack-file reads at decode under
  unfilled_>0. Fix candidates A-D staged; nothing implemented without a user order.
- Live discriminator launched as V4-r8 (doubles as the 2nd kept boot): probe_relief_tg.sh
  samples engine /proc/io + context switches every 2 s during the standard full drive.
  M1 predicts read_bytes rising through the tg1024 legs; M2 predicts flat read_bytes with
  context-switch spikes. STRATA_DRIVE_STOP_AFTER only supports "pp" (no TG-leg stop), so the
  probe rides a full ~25 min drive.

### Fix A implemented and committed (20:0x, user order "go"; 1167bd6d)

- All seven lock-taking sites under relief now pre-check the drop bitmap lock-free
  (boot-published, immutable while serving — the refill is the documented no-op) and take
  `file_mu_` only for a dropped pair, re-checking under the lock: blob, blob_stable,
  blob_at, transient, copy_blob, pinned, device_alias (src/core/expert_source.cpp).
- g++ -fsyntax-only with the rpcw-hip build flags: clean (only pre-existing warnings).
  The first check attempt died SIGPIPE-141 (grep -m1 piped from ninja under pipefail) —
  the standing "never pipe into ninja" rule, applied to -m1 readers too now; nothing was
  lost, redone with file capture.
- Rebuild deferred until the V4-r8 arm completes so the block keeps one binary. Next:
  reconcile out/V4-r8-io.csv (M1 vs M2), ninja -j11 rebuild both backends, V5-r1
  (relief + fix A) with the probe sampler attached, compare vs P0-r7.

## 2026-11-04 19:50 — Probe verdict: decode-time pack reads dominate (M1); V4-r8 confirms r7

- V4-r8 (relief, pre-fix binary, full drive + /proc sampler): TG median 20.1 (segments
  6.3-31.7, bimodal), pp18k 270.9, 88k 438.8, 80k sha PASS, anon 41.2 GiB, no retries.
  Second kept V4 boot: medians 16.0 / 20.1 — both far under the 33.1 gate.
- The sampler (out/V4-r8-io.csv; read_bytes counts storage-layer fetches only, page-cache
  hits excluded): the three TG legs sustained 100-500 MB/s of disk reads — roughly 40 GB
  total, against a held set of 6.24 GiB: the same blobs re-read ~6x. The 88k prefill leg
  shows the same signature (~30-40 GB). Involuntary context switches spike to 364k/s while
  the reads run — threads starving behind I/O: the lock convoy (M2) is real but downstream
  of the reads (M1).
- Mechanism: dropped pairs are "transient" — every consumer touch (prompt stager, window
  fill) re-stages the blob from the pack files; under relief the held pairs are the hottest
  experts, so prefill and TG re-read them from disk continuously; the 80k prompt-cache
  spread evicts what the page cache had retained.
- Consequence: Fix A (1167bd6d) removes the resident-pair locking but cannot remove the
  reads themselves. Next: Fix D — fadvise(WILLNEED) over the held pairs' pack ranges at
  relief setup (page-cache-backed, anonymous-RAM goal intact); Fix B (pinned staged LRU)
  reserved for if eviction still bites. Then rebuild both backends and run V5-r1 with the
  sampler against the P0-r7 same-evening control.

### Fix W1b implemented (20:1x): the 40 GB were wasted prefetches of resident pairs

- Found the read driver: ArenaExpertSource::begin_layer/prefetch/warm forwarded EVERY id to
  the relief file source; that source (plain pack open, complement_ready_ false) skips
  nothing — FileExpertSource::warm madvise(MADV_WILLNEED)s (or io-enqueues) each forwarded
  id's pack ranges, resident or not. The decode dispatch calls begin_layer per routed batch
  (expert_pool_dispatch, src/core/expert_source.cpp:4749/4945) and the warm hints ride the
  same path — so the engine continuously prefetched from disk the experts it already held
  in arena RAM. V4-r8's ~40 GB/drive of storage reads were this waste.
- Fix: the three forwards now filter to dropped pairs only (lock-free bitmap check, W1a
  invariant) and return before the lock when none qualify — the decode common case. Dropped
  pairs still stage on demand through blob()'s pull path (bounded by the 6.24 GiB held set).
- Syntax PASS with the rpcw-hip build flags. armV5.json staged (copy of armV4.json). Rebuild
  of both backends next, then V5-r1 (relief + W1a + W1b) with the sampler against P0-r7.

### V5-r1 attempts 2-3: harness plumbing, engine healthy (20:4x)

- Attempt 2 died at the relief fail-fast gate at 20:38. Root cause: armV5.json was copied from
  armV4.json with "log": "logs/V4-rROUND-engine.log" still inside; run-arm.sh substitutes only
  ${ARM}-rROUND per round, so for V5 nothing matched and the engine wrote its log to a literal
  V4-rROUND path under the repo's logs/ dir while the gate grepped the V5 path that was never
  created. The kill wasted an otherwise healthy boot: the serve log shows the engine loaded
  39.97 GiB at 2.48 GiB/s, filled the GPU expert cache, and reached ready — the 19:53 binary
  with W1a+W1b boots clean.
- Fix: armV5.json log field → logs/V5-rROUND-engine.log. Attempt 3 launched with the sampler.

## 2026-11-04 20:50 — V5-r1: the fix pair lands — relief TG at control speed

- V5-r1 (relief + W1a lock-free pre-check + W1b dropped-only forwards), full drive clean:
  TG median 37.2 tok/s (segments 34.5/35.9/36.5/37.9/39.9/40.8) — gate 33.1 PASSED, within
  2.4% of the P0-r7 control (38.1), 2.3x the unfixed relief (16.0/20.1). TG walls
  36.9/26.5/27.0 s against V4-r8's 116.8/104.0/34.7.
- 88k 552.4 (above stock C-r3's 550.0, near control 576.5); 80k decode 38.9 tok/s (the
  unfixed arm degraded to 5.4); probe sha 709bd8fb and 80k sha 1c7d4a6d — quality
  byte-identical, zero timeouts/fallbacks/retries.
- Relief intact: drop line 4116/4116 (6.24 GiB), complement 3033 ranges / 33.73 GiB —
  identical to V4; engine anon 42.1 GiB against the control's 47.4; Pss_File 4.3 GiB of
  retained pack cache.
- Sampler (out/V5-r1-io.csv): drive-phase disk reads 2.31 GiB total, peak 20 s window
  40 MiB/s (baseline) — against V4-r8's ~40 GB sustained at 100-500 MB/s. The wasted
  resident-pair prefetches are gone; the residual is legitimate on-demand staging.
- V5-r2 launched (sampler attached) for the second kept boot; medians and the final
  three-way report follow. Fixes B/C/D held in reserve. Adoption is the user's call.

## 2026-11-04 20:57 — V5-r2 confirms; bench phase complete, final report

- V5-r2: TG median 37.7 (segments 36.7-39.5, tight), 88k 553.4, 80k decode 38.3, shas
  canonical, engine anon 42.1 GiB, no retries. Combined V5 (r1+r2): TG median 37.65 =
  98.9% of the winning-config control (P0-r7 38.05), 36% above stock (C-r3 27.6);
  88k 552.9 vs stock 550.0 / control 576.5; anonymous -5.3 GiB vs control; drop line
  4116/4116 on both boots.
- Full table + verdict written to bench/ab-v0210/out/RESULTS-relief-fix.md. The relief
  TG loss is fixed by W1a+W1b; fixes B/C/D unnecessary. Zero wedges/timeouts/retries
  across every V5/P0/C boot of the evening.
- Session commits on tap-overlap-f1f2: 1167bd6d (W1a), f4e0bc3d (W1b), db8782c0 (V5
  harness wiring), b2e23344 (armV5 log path), af94a27c (V5-r1 docs), plus the earlier
  503-retry/THP/probe commits. Awaiting user: adoption decision and prod restore.

## 2026-11-04 21:35 — V5 promoted to prod, verified live

- User order: promote V5 (relief + W1a + W1b) to prod and update all prod configs.
- Prod repo /mnt/user/Strata (build-latest): committed the live-as-benched baseline
  (8c7a5ed1: prefill 16384, watchdog 900, helper no-idle-exit), deployed the
  V5-validated 19:53 binaries into v0210/build-mixed-gpu (engine 331c2e89d00dd4f1,
  helper afbf19f76e39ac16; old pair backed up in pre-v5-backup-20261104), and
  committed the promotion (3a34d550: --expert-helper-ram-relief +
  STRATA_PREFILL_HELPER=1 in both mixed configs, iq3_s slots 4000->4200 and its
  stale top-level expert_helper_slots 3400->4200). Non-mixed configs have no
  helper; relief does not apply. telemetry.py/.gitignore working-tree changes and
  untracked files left untouched (not mine).
- Relaunch (launch-prod-swift15-dflash-w4-q4_0-mixed.sh, announced) verified:
  health at 46 s; relief drop 4116/4116 (6.24 GiB) + complement 3033 ranges /
  33.73 GiB, identical to the bench boots; helper listening; 64-token temp-0
  smoke answered exactly; engine anon 40.68 GiB against the pre-promotion 47.4.
- Caveats recorded: the iq3_s variant's pack was never relief-validated; prod
  quality byte-identity is inherited from the bench (same binary, pack, flags).

## 2026-11-04 21:40 — V6 (iq3_s) lane: works with relief, slower and tighter

- Arm V6 = V5 flags on the iq3_s pack (Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S, rows
  1.97 MiB = +26% vs IQ3_XXS). Attempt 1 died at helper boot: the PACK/EXPERTS lines
  landed inside the env -i continuation, so the helper inherited CUDA_VISIBLE_DEVICES
  and saw no device (fixed 6c5f9ab6; bash -n cannot catch that class).
- Attempt 2, full drive, 0 timeouts/fallbacks: relief engaged (3377/3377 pairs dropped,
  6.24 GiB; complement 2642 ranges / 40.60 GiB; fill-mib capped at 3377 slots).
- vs V5 (iq3xxs, same evening): TG median 32.9 (segments 22.7-38.9, one slow tail)
  vs 37.65 (-13%); pp16384 228.5 vs 271.7 (-16%); 88k 399.2 vs 552.9 (-28%); 80k
  decode 32.8. Engine anon 49.0 GiB (V5 42.1); host at 54/62 GiB used, 1 GiB free
  at peak — the 88k slowdown and the 22.7 segment are consistent with that pressure.
- New per-model shas established (probe ad5508a8c87b1574, 80k 9f3c8e9718477049);
  r2 required only for a byte-identity claim. Adoption (quality vs -13% TG and
  -7 GiB headroom) is the user's call; prod stays on swift15/iq3xxs meanwhile.

## 2026-11-04 21:55 — V7 limit probe: fastest config of the night, at the paging edge

- V7 = V5 + VII reserves 256/256 MiB + helper fill 6700 (4318 slots staged, 6.53 GiB;
  relief dropped 4318/4316 pairs, complement 33.44 GiB). Full drive, 0 timeouts.
- Fastest measured tonight: TG median 39.1 (beats the P0 control 38.05 and V5 37.65);
  pp16384 277.9; 88k 575.3 (control 576.5); decode hit rate 97.0-97.5% with the PCIe
  share down to 10.8-14%. Engine anon 41.8 GiB.
- The limit itself: 248 MiB free on CUDA1 after load (406 on CUDA0) — below the
  engine's own 512 MiB threshold where the driver may page GPU memory out; the layer
  plan also miscounted 104 MiB. Going lower is not advisable; a later-reserve of 512
  would keep most of the gain with headroom.
- Quality: the 80k sha is byte-identical (1c7d4a6d...); the probe sha diverged for
  the first time (126dc32d..., 229 vs 208 completions) — consistent with
  ROUTE_TAIL_SKIP being coverage-dependent (different resident set skips different
  rank-7 experts), unconfirmed. r2 would show whether the divergence reproduces.

## 2026-11-04 22:25 — V7-r2: 2-boot verdict and the determinism answer

- V7-r2 reproduces r1: TG segments 34.1-41.6 (median 37.0; r1 39.1); combined
  12-segment median 38.5 — +2.3% over V5's 37.65, +1.2% over the P0 control.
  pp16384 277.9/280.3; 88k 575.3/573.2 (control level); 80k sha canonical on both
  boots; relief 4318/4318 (6.53 GiB) identical; free-after-load 406/248 MiB with the
  same sub-512 MiB paging warnings; zero timeouts or failed legs.
- The probe sha 126dc32d... reproduced EXACTLY (229 completions both boots): the
  divergence from the V5 canonical probe text is deterministic under this coverage
  set — consistent with ROUTE_TAIL_SKIP being coverage-dependent. Stability is
  proven; whether the different probe text is acceptable is a judgment call.
- Net: the squeezed config is the best measured (38.5 combined) but the margin over
  the current prod config is modest and it sits at the driver-paging edge. Safe
  adoption variant: reserves 384/512 MiB + helper fill 6700. Prod decision pending.

## 2026-11-04 22:45 — V7-squeezed promoted to prod, verified live

- User order "do it" after the A/B/B' fork. Prod commit 08f5e690: both mixed
  configs at reserves 256/256 + expert-helper-slots 4600; both launchers at
  --fill-mib 6700 --slots 4600. Binaries unchanged (the validated 19:53 pair,
  sha-verified again). Relaunch verified against the bench boots line-for-line:
  relief 4318/4318 (6.53 GiB), complement 3120 ranges / 33.44 GiB, free-after-load
  406/248 MiB (CUDA1 LOW warning expected), helper filled 4318 slots (6.54 GiB),
  smoke exact, engine anon 40.4 GiB.
- Prod now runs the flag-level optimum measured tonight: TG 38.5 combined over
  two bench boots (vs 37.65 for V5-class, 38.05 control), 88k at control level,
  6.53 GiB of arena relief. Rollback: git revert 08f5e690 (+ 3a34d550) + relaunch.
- Caveats on record: CUDA1 at the 248 MiB paging edge (engine warns below 512),
  deterministic probe-text difference vs the V5 canonical (80k byte-identical),
  iq3_s variant unbenched at these settings, dflash-draft-to-3080 parked as an
  engineering follow-up.

## 2026-11-04 22:40 — V8 (iq3_s at the promoted squeeze) FAILS: non-finite logits in long prefill

- User ordered "test iq3s with the promoted conf" (quality lane held). Arm V8 =
  iq3_s pack at 256/256 + fill 6700 + slots 4600 (60a992f9 + whitelist e23e981c).
- Boot healthy: relief 3530/3530 (6.53 GiB), complement 2722 / 40.31 GiB, cache
  3724 slots, helper 3530, free-after-load 400/242 MiB. Probe fine: sha
  55b3e637a4cea9df, hit 95.4%, 39.4 tok/s.
- pp16384: non-finite logits (#879) in the prompt window at ~18077 of 18082
  tokens, then in the first decode window; serve retried 3x, all non-finite,
  then 503. Engine never crashed — drive.py's "engine restart" is a mislabel.
- V6 (same pack, 896/1000 + fill 6400) passed this exact gate => the squeeze
  breaks iq3_s long prefill. Mechanism unconfirmed (paging at 229-242 MiB free
  during first-use window graphs — static analysis).
- Consequence: the promoted iq3_s fleet conf from 08f5e690 is invalid for long
  prompts. swift15/V7 (iq3xxs) unaffected. Proposal (awaiting user): revert
  iq3_s conf + launcher to V6-class settings.
- Harness notes: inline pkill must bracket-escape patterns (my stop shell
  self-matched its own pattern and died mid-sequence); the run-arm whitelist is
  a separate line from the per-arm cases.

## 2026-11-04 23:05 — V9 (iq3_s safe-squeeze) passes the full arm

- User chose option (b). Arm V9 = iq3_s, reserves 640/640, helper fill 7000,
  slots 4600 (8daa8f95). One full arm, rc=0.
- The V8 killer passed: pp16384 read 18082 tokens at 226.7 tok/s, 10 generated,
  zero non-finite logits anywhere in the run.
- Boot: cache ~3576 slots, relief 3583/3583 (6.63 GiB; complement 2752 ranges /
  40.21 GiB), free-after-load 784/626 MiB — both cards above the 512 MiB paging
  line, matching the 770/650 interpolation. Helper filled 3583 slots (6.64 GiB).
- Perf vs V6 (same pack, 896/1000 + fill 6400): TG ~34.7 vs 32.9; 88k read
  519.2 vs 399.2 tok/s. Probe sha 717b8504bc58ab71; 80k sha 1c7d4a6d9be118d8
  (the iq3xxs-canonical value; V6's iq3_s value was 9f3c8e97 — deterministic
  per-config, no r2 byte-gate on a one-shot arm).
- Engine anon 49.0 GiB, same as V6 — the box runs tight (2 GiB free) but held.
- Frontier settled: 256/256 = NaN, 640/640 + fill 7000 = safe and faster,
  896/1000 = safe. Proposal (awaiting user): V9-class becomes the iq3_s fleet
  conf. Prod restored after the lane.

## 2026-11-04 23:25 — V9-class promoted to the iq3_s fleet conf; live prod switched to iq3_s

- User order. Prod commit 1c80795b: iq3_s conf at 640/640 + fill 7000 (slots
  4600 unchanged). swift15 stopped cleanly; prod relaunched on the iq3_s
  launcher (port 8009, its own socket pair).
- Boot verified against the V9 bench boot: relief 3582/3582 (6.63 GiB;
  complement 2722... prod shows 2752 ranges / 40.21 GiB), free-after-load
  784/626 MiB identical, helper filled 3582 slots (6.64 GiB), smoke exact,
  engine anon 44.4 GiB, 3080 at 7144 MiB.
- Switchback path: stop + relaunch the swift15 launcher (V7 conf untouched).
- NaN source dive revived with prod up (no-delegation-while-prod-down rule).

## 2026-11-04 23:35 — NaN source dive (done directly; explorer errored 3x on infra)

- #879/#3465/#2065 are issue references embedded in comments and log lines,
  not runtime counters.
- Detection is symptom-level: src/core/verify.cpp:3708 scans a window row's
  full-vocab HEAD logits with isfinite; src/program/generate.cpp:12700-12711
  reports and ends the request. Nothing identifies the producing layer.
- The low-VRAM warning (generate.cpp:15737-15753, "#2065: said, never
  changed") is warning-only — no cache free, no refusal. Recommended reserve =
  current + (512 − free) rounded to 64 MiB; reproduces V8's printed 384/576.
- Verifier::capture (verify.cpp:2778) has no free-VRAM guard; #1275 records a
  capture-time process death at 313 MiB free.
- Validation asymmetry: helper-served expert rows are isfinite-checked
  (expert_helper_socket.cpp:65, kErrNonFinite; V8 ran timeouts=0 fallbacks=0),
  but GPU-side expert reads (VRAM cache / cross-card P2P / arena) have no row
  validation — garbage propagates FFN → norms → head and is only caught at the
  head logits.
- Conclusion: at 256/256 (3724 slots, 229 MiB free) the over-full card during
  the 18k prefill's second chunk served garbage for the experts hot at
  position ~18077; unvalidated on the GPU path; norms amplified to Inf/NaN.
  The exact garbage source (paging vs P2P vs uninitialized slot) is
  unconfirmed — would need an instrumented boot. 640/640 stays the fix.

## 2026-11-04 23:35 — V10 passes: iq3xxs safety margin at ~zero cost

- Arm V10 = iq3xxs at 640/640 + fill 6700 (a7ec2674), one full arm, rc=0.
- Boot: free-after-load 788/632 MiB (V7 was 406/248) — both cards above the
  512 MiB line with real margin. Relief 4310/4310 (6.53 GiB), cache ~4230
  slots (about -80 vs the 256/256 squeeze), helper 4310 slots, 0 non-finite.
- Perf in-band: TG legs 34.8/34.9/37.1/37.3/39.8/40.4 (median 37.2, inside
  V7's own boot spread 39.1/37.0), pp16384 275.3 tok/s, 88k 562.1 tok/s,
  80k sha canonical (1c7d4a6d), engine anon 41.8 GiB.
- swift15 prod conf updated to 640/640 (fill/slots unchanged) — the fast
  variant no longer rides the 248 MiB edge.
- DF1 (q8_0 draft, V10-exact otherwise) launched before the prod restore to
  save two boot cycles; iq3_s prod comes back right after DF1's verdict.

## 2026-11-04 23:45 — DF1: the q8_0 draft buys nothing; the sweep's first rung closes

- Arm DF1 = V10-exact with the dflash q8_0 draft (adc6aa6e), rc=0, clean
  (0 non-finite, helper timeouts=0).
- TG median 37.3 vs V10's 37.2 — flat. Acceptance ~61.6% vs ~64.6% — slightly
  worse, never ahead on any leg. 88k 550.9, pp 271.8, 80k sha canonical,
  engine anon 41.8 GiB.
- Conclusion: the ~60% acceptance ceiling is the dflash2 draft model itself
  (block 5), not its quantization. The q4_0 draft stays. Remaining options if
  the sweep continues: --spec/--spec-min-p grid, --dflash-block, or a
  different draft model — the likely real fix.
- iq3_s prod restored after the arm.

## 2026-11-05 00:00 — DF2: spec 7 loses; the dflash config sweep is exhausted

- Arm DF2 = V10-exact + --spec 7 (4ac93265), rc=0, clean.
- TG median 36.1 vs V10's 37.2 (−3%); acceptance diluted to ~59.3% from
  ~64.6% — the wider verify windows cost more than the extra accepted drafts
  bought. 88k 562.7 tok/s, 80k sha canonical (quality holds at width),
  engine anon 41.9 GiB.
- Sweep conclusion: DF1 (q8_0 draft) flat/worse, DF2 (spec 7) worse —
  spec 5 + q4_0 is the optimum for the dflash2 draft model. Its ~60–65%
  acceptance ceiling is the model itself. Remaining options: one cheap
  --dflash-block probe, or a different draft model (the real fix).
- Process note: user's "done" meant "show prelim results"; I misread it as
  "wrap up" and TERMed the lane — the TERMs landed after the arm completed,
  so nothing was lost. Lesson: confirm before acting on ambiguous one-word
  orders.
- iq3_s prod restored after the lane.

## 2026-11-05 00:05 — DF3: block 3 flat; the dflash config sweep is fully closed

- Arm DF3 = V10-exact + --dflash-block 3 (cdd09edc), rc=0, clean.
- TG median 37.6 vs V10's 37.2 (+0.4, inside boot noise); acceptance ~63.7% vs
  ~64.6% (same); 88k 562.9 read / 39.1 decode; 80k sha canonical; probe sha
  f889609a; engine anon 41.8 GiB; 0 non-finite; helper timeouts=0.
- Sweep fully closed: quant (DF1) dead, width (DF2) worse, block (DF3) flat.
  spec 5 / q4_0 / block 5 stands — the setting every canonical sha tonight was
  measured at. The ~60–65% acceptance ceiling is the dflash2 draft model
  itself; the only remaining lever is a different draft model (engineering).
- Fresh iq3_s prod restore after the arm (the wedged 23:53 boot was cleared
  before the lane).

## 2026-11-05 00:4x-00:5x - quality gate (ordered after hold; cut by user order) + prod restore
- Yardstick: tools/quant_quality.py, 5x999 teacher-forced positions, CPU-expert mode (expert-cache 1, fp16 kv, no helper => gates the quant/pack layer).
- Design forced by two boot facts: no-pack impossible (canonical Q2_0 fallback needs AVX-512, CPU is AVX2 => packs are the only expert source) and IQ packs require --spec>=2 (native CPU kernels are multi-token). Configs: bench/ab-v0210/quality/{make_qq_configs.py,qq*.json}; stock exe = strata-stock-v0210 tree.
- HEADLINE PASS: REF (stock strata + iq3s pack) vs A (relief binary + iq3s pack): top-1 100.00%, KL 0.00000 (p99 0.0000), PPL 4.115 = 4.115 over all 4995 positions. The relief stack is logprob-identical to stock on the live pack; combined with +36% TG vs stock => zero quality cost for the speed.
- Cut by user order ("bring prod back"): C (stock+iq3xxs15) killed at 2/5 segments (partial TSV committed as evidence), B never ran. Unmeasured: C-vs-B twin check, REF-vs-C quant delta - resumable on order.
- Prod restored (iq3_s, first boot at helper fill 6796): health 200 ~75s, 784 MiB free, helper filled 3582 slots 6.64GiB stopped:fill-mib (one row under the 7000-ask boot = same edge), 698 MiB left on 3080.
- Commits: 651f87a9 (configs v1), aaf53b18 (redesign), 7881dc1d (--spec fix), fc3303e8 (REF+A artifacts).

## 2026-11-05 01:50-03:05 - T-lane: short-prompt PP / TG<->PP turn-around (prod down, ordered autonomous)
- Baseline (T1 trace): batched read has ~3.6s intercept + ~11ms/tok early marginal (185tok 23-33 tok/s vs 313 bulk); refill 0.5-5.8s after prefill (1.27ms/lent-slot); in-request TG<->PP seam <1s (not a problem); helper claims ~0.3s/request (T1b profile, not the whale).
- T2c short-read 256: chat 6.5->2.9s (2.2x) but FATAL refill-from-files poison after window reads (TG128 14.2->57.8s). T5 SPLIT_SMALL: no-refill real (-0.3s) but 1024-geometry destroys 2-4k (23->37/171s). T6c combo: poison gone but 512-geometry +2s on S1k and a 127s restaging catastrophe on first big prompt after small traffic. T3 PIPE: clean null (as profiled).
- T4 STRATA_PREFILL_CPU_SHARE=auto: THE WINNER - S214 2.4x (2.6-2.9s warm), S1k 1.8x (6.2s, read halved), S4k 1.4x (16s), TG128 -5.1s (9.1s), S18k unchanged; zero regressions/outliers; upstream KL~0.004 documented. PROMOTED to both prod configs.
- Window-routing (2.2x for <=256tok) proven twice but needs source work: window reads must populate the arena (refill/restaging root cause); the ~3.6s short-chunk intercept is layer compute/staging (source).
- Commits: 32fcc9c4, a69e7898, 8f9469e3 + results commits; artifacts in bench/ab-v0210/{shortpp,logs,out/RESULTS-shortpp.md}.

## 2026-11-05 03:2x - quality lane for the promoted version (ordered: speed+quality for the new version)
- User order banked as standing rule too: any NEW engine version gets BOTH lanes (speed + quality) before promotion.
- Quality gate for the live promoted config (CPU_SHARE=auto): qqS vs share-off twin A and vs stock REF = top-1 100.00%, KL 0.00000/p99 0.0000, PPL 4.115=4.115 over 4995 positions; yardstick pins --prefill 512 so every scored chunk runs the share path. PERFECT PASS - the 1.8-2.4x short-prompt win carries zero quality cost. Commits: 25ae1864 (qqS config), 60e7afaa (S.tsv artifacts).

## 2026-11-05 13:45 — S-lane: implementation landed, first bench session aborted (host too tired)
- shortpp-src: STRATA_REFILL_OVERLAP=1 (c44c6d07) + STRATA_WINDOW_POPULATE=1 (03d40f8e), both OFF by default; build-v0210 engine rebuilt 331c2e89 -> f5fb0738 (backup strata.pre-s-331c2e89); helper untouched.
- S-harness: make_s_configs.py (S0-S4/SP on CPU_SHARE=auto base) + run_s_session.sh; RAM gate now HARD-FAILS below 30 GiB (bfb28d8d) — it used to fall through and boot anyway.
- First S-session (12:47-13:29) ABORTED after S0+S1 rc=1: MemAvailable ~9 GiB with 15 GiB swap (host exhausted after 24 h of crashes/boots); arms thrashed — S18k chunk stages 360-731 s vs T-lane ~39 s; the helper prompt-tier 10 s reply deadline closed the channel mid-prompt -> "prefill: the helper fallback lost a blob" -> HTTP 400. W1a/W1b ancestry verified: the binary was NOT the regression.
- Promising but not bankable: S2 (populate) short legs hit 2.59/2.91 s (T4-class) before the abort.
- Rerun preconditions: clean host (reboot = gold standard) + the new gate. Prod restored meanwhile (gen-1 supervised, 13:31, health 200 in ~135 s).

## 2026-11-06 — fork README written (README-FORK.md)

- User-ordered: analyze the fork's latest state and write an at-a-glance README (lists over prose).
- Sources: full JOURNAL read, `git log origin/main..HEAD` (340 commits, gfx906 mixed line), bench/ab-v0210
  RESULTS*.md, live prod configs (/mnt/user/Strata, memo + CPU_SHARE confirmed in prod env).
- Deliverable: README-FORK.md at repo root (stock README.md left untouched for upmerges). Contents: rig table,
  six feature groups (gfx906 port, CUDA expert helper, RAM relief, DFlash/DFlash2, short-prompt latency,
  bench+quality discipline), the same-evening fork-vs-stock table (TG +36–39 %, 80k parity/+7.6 %, fresh-18k −18 %,
  short-PP 1.4–2.4×, RAM −6.2 GiB, 80k sha byte-identical), honest trade-offs/defects, repo map, upstream hygiene.
- All numbers cite existing bench artifacts; no new measurements claimed.
- User follow-ups: added "Who this benefits", the one-line bottom line (≈1.4× decode, 1.4–2.4× chat, −6.2 GiB,
  −18 % fresh-18k, byte-identical), a Locations table (dev tree /home/user/dev/Strata, live server
  /mnt/user/Strata on :8009), a dedicated Quality-gates section (80k SHA, yardstick, probe determinism,
  speed gates), and an explicit "ROCm first, CUDA second" statement (engine maps no CUDA runtime; helper
  death is survivable).
- User follow-up 2: expanded the helper-architecture section (two processes, socket pair, slot/fill caps from
  sweeps, decode vs prompt tiers, relief interplay, kill-test fallback, --persist, v4 multi-shard, pair
  supervisor, honest D/E attribution) and the DFlash2-advantages section (arm-E attribution, HIP-side drafter,
  exact-match sampled verification, bounded SWA ring + DF_RING_MULT, parking, DF_EVENTS, perf work, closed
  sweep). README copied to the prod tree /mnt/user/Strata (build-latest) on user question; dev copy canonical.
- User follow-up 3: hardware section expanded to the full box (Ryzen 9 5900HX 8C/16T, 62 GiB, 2× Radeon VII
  16 GiB, RTX 3080 Laptop 8 GiB helper, SN530 NVMe + BX500 SATA, XanMod 7.2.9) so readers can relate to the
  numbers; build guide added (ROCm 6.1.0 pins /opt/rocm-6.1.0, custom ~/rocm-gfx906-xnack rocblas gfx906-xnack
  libs first on prefix+LD_LIBRARY_PATH, exact configure lines from the build-v0210 caches, helper build, runtime
  env, hard-won configure/build lessons). Configure facts verified from build-v0210/{rpcw-hip,rpcw-cuda}
  CMakeCache.txt + tools/mixed_gpu/build_l3.sh; hardware from lscpu/rocm-smi/nvidia-smi/lsblk.
- User follow-up 4: top-of-file callout added — the project is made to be compiled/installed/operated by an
  agent, humans order and read (JOURNAL.md, RESULTS*.md, build guide, AGENTS.md).
- User follow-up 5: "stock does not support" narrowed to the measured claim — stock's gfx906 path is a
  community source build the maintainers have never run (compile-checked only, not in its README table);
  v0.2.10 as tagged does not compile (three fixes, branch stock-v0210-gfx906-buildfix). Fixed in intro,
  bullet, who-benefits, bottom line, section 1.
- Published: fork pushed to github.com/milpster/strata-dflash2-gfx906-mixed-rocm-cuda (origin; old origin
  kept as upstream for future v0.2.10+ upmerges), shortpp-src renamed main per user order. WIP helper files
  stayed uncommitted/local.
- User follow-up 6: fork README is now README.md (GitHub overview shows it); upstream product page moved to
  README-upstream.md. src2.tar (68 MB, GitHub large-file warning) identified: upstream artifact from d2a38c9e
  (2026-10-17, "Linux tester AppImage" repack work) — a staged AppImage payload tarball, deleted from the tree
  since, history-only; left in place (under the 100 MB hard limit). All local branches + tags pushed to origin
  per user order ("push all").
- User follow-up 7: canonical branch renamed main -> master (user preference; master = latest and greatest).
  Fork README.md swap committed (fork page = README.md, upstream product page = README-upstream.md).
- Resolution: user indifferent main-vs-master; remote main (the GitHub default) fast-forwarded to master's
  commit — overview now renders the fork README. master and main kept in sync.
- Final: user ordered ONE branch; decided for main (already the GitHub default, deletable without API auth).
  master deleted remotely, local branch renamed main, tracking origin/main. Remaining remote branches:
  main (canonical), main-pre-publish (845-commit upstream-main snapshot backup), build-latest,
  build-mixed-v0210, stock-v0210-gfx906-buildfix, tap-overlap-f1f2.
- User follow-up 8: helper-loss claim corrected — pair self-heal is the current default (prod line
  pair-self-heal: "Harden mixed-GPU paired recovery by default", "Record verified production paired-recovery
  cutover"; pair-kill-test.sh SIGKILLs the helper and expects generation bump + health 200). README now states
  both layers: in-flight engine fallback (recompute rows) + supervisor full-pair restart.
- User follow-up 9 (self-review for completeness/accuracy; subagent aborted twice on infra, done directly):
  FIXED — STRATA_TESTS → STRATA_BUILD_TESTS (cache: STRATA_BUILD_TESTS:BOOL=ON; CMakeLists option(…)); stale
  branch row (shortpp-src) → main + full branch list; "origin/main" upmerge wording → upstream/main; helper
  slot range 4.3–4.6k → 3.6–4.6k (pack-dependent, iq3_s runs ~3.6k); memo dropped from the perfect-score
  yardstick row (relief + CPU-share only have recorded perfect scores; memo was gated by the ab6s shallow
  prompt-mode runs, full ab6/ab6b parked); "In flight: f16-KV staged" → f16-KV lineage IS live prod since
  Nov 6 (serve --config prod-config-swift15-f16kv…, supervisor attached generation 6); prod Locations row now
  names branch pair-self-heal; self-heal line cites live generation 6. CONFIRMED — all headline numbers
  (TG 37.7–38.5 vs 27.6 / +48% frame; 80k parity-to-+7.6%; fresh-18k −18%; short-PP 2.4/1.8/1.4×; memo 14–20%;
  relief −6.2 GiB, 16.0→37.65, 40GB→2.31GB; yardstick 100%/0.0/4.115 over 4995; sha 18/18; arm-E 23.9≈23.6;
  acceptance 60–65%; sweep verdicts; +13.6% slots; 96–97% hit; H1-r5 25.05 GiB; hardware box; ROCm 6.1 pins +
  ~/rocm-gfx906-xnack; stock-compile-check-only claim) re-verified against JOURNAL.md, RESULTS*.md, CMakeCache,
  prod configs/processes. Live knobs (MEMO=1, CPU_SHARE=auto, PREFILL_HELPER=1, ram-relief) verified in the
  live f16kv config. 
- User follow-up 10 (public-clean pass): removed machine-local info from the README — the Locations table
  (both local tree paths), the :8009 port, the live-generation-6 line, the "f16-KV is live prod" note and the
  "In flight" work list. Build-guide paths that describe HOW it builds (pinned /opt/rocm-6.1.0, system CUDA,
  ~/rocm-gfx906-xnack custom libs) stay by user order ("mention our rocm and custom libs").
- User follow-up 11: README restructured for readability — contents line, one merged rig table, prose lead-in
  per feature section, bottom line as blockquote, repo map + hygiene as one tail; no facts changed. Separate
  "at a glance" feature table produced for the release thread (external copy, not added to the README).

## 2026-11-06 — repo PII scrub: history rewritten, tooling made location-independent

- User order: eradicate machine paths/usernames/agent-tooling traces from the published git history
  (email explicitly out of scope). Full filter-repo pass over ALL branches + 74 tags:
  machine-local home/mnt roots -> neutral /home/user + /mnt/user forms, agent tmp dir -> /tmp/agent,
  worktree-tooling path fragment folded away, the one prose username mention dropped, personal
  host-load phrasing neutralized in JOURNAL + RESULTS + commit messages.
- Verification: pickaxe over --all = 0 hits for the username; 0 in HEAD. Remaining matches are
  upstream's own files (community bench submissions, their binary AppImage tarball) - not ours.
- Follow-up fix commit (this branch): bench/quality/tooling/test scripts now derive the repo root
  from their own location and take external trees from STRATA_* env vars with neutral defaults
  (STRATA_DEV_TREE, STRATA_DATA_DIR, STRATA_PROD_TREE, STRATA_HELPER_WT, STRATA_ROCM_ROOT,
  STRATA_DFLASH2_DIR, STRATA_AI_DIR). All touched scripts bash -n / py_compile clean; ROOT
  resolution probed at every directory depth. Local shell exports live in the user bashrc;
  a neutral tmp symlink keeps the terminal tests working.
- Untracked local bench scripts got the same treatment so future commits stay clean.
- Pre-scrub bundle backup kept outside the repo on local disk (all refs, pre-rewrite state).
- Force-pushed everything; every SHA after this point differs from the pre-scrub publish.
  Old SHAs may stay fetchable on the forge until its GC - full eradication there = delete +
  re-push (user decision pending). The private prod tree is not published and keeps its own copy.
- NOTE for future bench rounds: generated arm/config artifacts record whatever the env resolves
  to; scrub paths before committing new artifacts, or commit only the summarized RESULTS docs.

## 2026-11-06 — README: OS row, main-branch policy, custom-rocBLAS rebuild section (user follow-up 12)

- User order x3: (1) elaborate the custom rocBLAS rebuild if it is not straightforward, (2) state that
  main carries the latest working version, (3) name the OS in the right place.
- Facts gathered before writing: OS = Ubuntu 25.04 (Plucky) from /etc/os-release -> rig table OS row
  (was bare "Linux, XanMod ..."), role now "userland the custom rocBLAS is built against".
- rocBLAS rebuild: NOT straightforward-as-documented. Only the installed prefix survives
  (~/rocm-gfx906-xnack: rocBLAS 4.1.0 from include/rocblas/internal/rocblas-version.h, matches pinned
  ROCm 6.1.0); source tree, CMakeCache and build log are gone; journal has no recorded recipe.
  README therefore documents the canonical upstream side-build shape (gfx906:xnack+ target,
  install-prefix layout, heavy Tensile build) explicitly labeled "not a re-verified run" - to be
  replaced with real flags/wall-time/verification at the next rebuild. No fake precision.
- Branches row: main now marked "carries the latest working version; nothing lands before both
  backends build and the gates pass" (matches the existing promotion wording).
- Addendum (user follow-up 12): rig table gained a rocBLAS row - "custom-built 4.1.0, gfx906-xnack
  (~/rocm-gfx906-xnack), the GEMM path for both Radeon VIIs, pinned ahead of system rocblas at
  build and runtime" - so the at-a-glance table names the custom lib and version, not just the README prose.

## 2026-11-06 — custom rocBLAS provenance found (user follow-up 13): bug report, target, fix mechanism

- User pointers led to the llama.cpp-era docs: uf3_rocm6.1_llama.cpp/BUILD-VEGA20.md plus
  /home/user/rocblas-gfx906-bug-report.md + rocblas-gfx906-workaround.patch.
- WHY the custom rocBLAS exists (from the bug report): stock ROCm 6.1.0/6.4.4 rocBLAS
  (librocblas.so.4.1.60100) SEGFAULTS on gfx906 for any GEMM shape without a tuned kernel -
  fallback kernels ship .hsaco only, the Tensile .dat metadata is missing (packaging regression;
  ROCm 7.15 ships them but different soname and ~25% slower: ~370 -> 277 tps llama.cpp). NULL deref
  in Tensile::PlaceholderLibrary::loadPlaceholderLibrary. Minimal repro: rocblas-bench gemm
  -m 48 -n 384 -k 5120 (gated on m=48/k=5120, any n).
- Fix mechanism identified: the custom prefix (rocBLAS 4.1.0 - exact stock version/soname, md5
  DIFFERS from stock 4.1.60100) is a single-target build: lib/rocblas/library holds merged
  TensileLibrary_gfx906.co (93.5 MB) + .dat (16.4 MB) + Kernels.so-000-gfx906-xnack-.hsaco -
  fallback metadata embedded, nothing external to go missing.
- Target string proven by filenames: gfx906:xnack- (NOT xnack+; earlier README recipe guess fixed).
  Dir name "xnack" is lineage; runtime leaves XNACK off (rocminfo: XNACK enabled: NO; no launcher
  sets HSA_XNACK - verified against the live launcher + prod config).
- Failed experiments kept on disk, NOT the shipping lib: rocblas-mixed/ (graft of 7.15 metadata
  into 6.1 - lazy-index format changed A- vs H-prefixed entries; documented "didn't work") and
  rocblas-6.1-custom/ (a 4.1.2 prefix, earlier attempt).
- Still missing: the literal cmake invocation / source tree (gone). README recipe remains
  labeled "not a re-verified run", now with the evidence-backed target and merged-library layout.
- Preserved in-repo: docs/work/rocblas-gfx906-bug-report.md + rocblas-gfx906-workaround.patch
  (both machine-path clean; provenance header notes llama.cpp-era origin).
- README updated: prerequisites bullet, rebuild section (why-it-exists + mechanism + fixed
  recipe), rig-table rocBLAS row. "Better GEMM performance than stock" (BUILD-VEGA20) is
  superseded as the primary story: stock does not run at all on these shapes; correctness first.

## 2026-11-06 — user correction: transient GPU stalls are host co-tenancy (follow-up 14)

- User statement: the transient GPU stalls are caused by them - the box is their daily machine and
  their own activity loads it while prod serves. Recorded as user-attributed operational fact.
- Distinction kept: the one-off 268 s stall was root-caused in source (DFlash2 SWA draft ring,
  K2/JOURNAL earlier) - that one stays an engine finding. The recurring small fresh-read stalls
  (30-140 s cold reads) are the transient class -> now attributed to host co-tenancy.
- README trade-offs row updated: stalls = host co-tenancy, not an engine defect; watchdog 300->900 s
  and the helper reply-patience deadlines both exist to ride out exactly this.

## 2026-11-06 — repo consolidation: mnt tree is THE repo (scrubbed, published); home dev tree retired

- User orders: mnt is the only relevant repo; full mnt rewrite + publish; port home's NON-upstream
  content only (upstream v0.2.10 upmerge explicitly IGNORED for now - merge + validate/test comes
  later on explicit user command); delete /home/user/dev/Strata (nothing else in home).
- mnt history scrubbed (3 filter-repo passes): all machine paths/username/tooling paths neutralized,
  background-load vocabulary + hostapp titles + store names neutralized, hostapp-* journal files renamed to
  host-*, host-coexistence branch renamed host-coexistence. Verify: pickaxe user=0, hostapp/background-load/
  steam/title hits=0 across journal+docs+src+tools.
- Ported from the dev line (content-level, no upstream upmerge): README, docs/work rocBLAS gfx906
  bug report + workaround patch, JOURNAL tail (2026-11-03 onward - the two journals were
  complementary, 0 duplicate headings after the fix), 81-file location-independent script fix
  (ROOT probes resolve to /mnt/user/Strata).
- Pre-scrub backups: /home/user/dev/strata-pre-scrub.bundle (dev line) +
  strata-mnt-pre-scrub.bundle (this line). Fixer script: /home/user/dev/fix_strata_roots.py.
- The v0.2.10 dev line is NOT lost: archived on the fork as branch v0210-dev-line (pushed from the
  home tree before its deletion) - the base for the pending upstream merge.
- TODO (on user command): upmerge upstream into this line + validate/test.

## 2026-11-06 — prod restart after the 10:57 SIGTERM death (user follow-up 15)

- Prod had been down since 10:57: engine got SIGTERM (-15) mid 140k-token prompt read (37.9k/140.7k
  in, 675 s); not OOM, not a crash - polite terminate, never restarted. Morning context: reboot
  10:26, xxS cutover round 2 (stages A/B pass, hiccup test fail -> helper reconnect broken on engine
  restart), restore fell back to the iq3_s lane on CPU expert path.
- First restart attempt (iq3_s lane) crash-looped 8 generations: root cause = the history scrub had
  rewritten the TRACKED prod config jsons (model/tokenizer paths -> neutral /mnt/user forms),
  and serve reads them literally -> "tokenizer is missing". Launcher env vars were fine; configs
  were not.
- Fix applied: all 14 prod-config-*.json restored to real machine paths (sed), json-validated.
  NOTE: these are LOCAL runtime modifications - do NOT commit them (would re-leak paths).
  Proper fix for the repo (env-interpolating config values or boot-time path rewrite) belongs to
  the pending QA round.
- Real prod brought up per user correction: swift15 f16-KV iq3xxs lane (not iq3_s).
  Boot: KSM on, helper up 4341 slots / 6.64 GiB, SECOND CHANNEL ENGINE CONNECTED (no CPU
  fallback), health 200 in ~36 s, supervisor generation 1, smoke completion OK.
- Delegation resumes against prod; takedown for QA (upstream merge + validate/test) on explicit
  user command.

## 2026-11-06 — upstream merge landed: v0.2.10 line into main (user order "do the upstream merge")

- Merged the archived dev line (fork branch v0210-dev-line) into main: 2059 commits in, merge-base
  in the v0.1.45-era upstream, only 11 conflicts (the scrub made both trees converge). Strategy:
  theirs for shared engine/tooling (newer + location-independent), ours for journal/JOURNAL/prod
  state. Deliberate keeps: sidecar helper_main.cpp took OURS (theirs lacks the reply-patience fix),
  prod-config-swift15.json content kept ours (current local tuning, committed form neutral).
- Verified post-merge: reply-patience x4, supervisor stall-restart x12, stage-affinity x8 present;
  src/include delta vs the v0.2.10 tag = 58 files/+12,752 (the gfx906 port); bash -n 10/10,
  py_compile 10/10, ROOT probe -> this tree.
- Delegated sessions added 5 good engine commits (stage affinity x2, supervisor in-flight-restart)
  AND two deliberate path-reversion commits (configs/launchers committed with real host paths).
  Those paths were re-neutralized by one more scrub pass (SHAs after merge rewritten; pickaxe 0);
  real paths live ONLY as uncommitted runtime state (24 files). RULE for future lanes: committed
  defaults stay machine-neutral; host paths are local uncommitted state - never commit them.
- Prod stayed up through the merge (delegation served continuously). QA (build + gates + restart +
  self-heal test) runs next with prod down.

## 2026-11-06 — QA round on the merged tree: builds, 231/233 ctest, live self-heal PASS

- Fresh builds (build-qa/, both backends, -j11): HIP engine + CUDA engine/helper configure+build
  clean, 517 s total (CUDA leg 315 s). ldd SONAME guards PASS (no libhipblas.so.3 / libamdhip64.so.7).
- ctest (233 tests): 231 PASS after three environment fixes, 2 classified:
  1. The user bashrc exports CUDA_VISIBLE_DEVICES="" globally (hides the NVIDIA card); launchers
     env -u it, raw ctest does not -> 77 "no CUDA device" failures. Run tests with
     `env -u CUDA_VISIBLE_DEVICES ctest`. Recorded as THE pitfall for future QA lanes.
  2. The scrubbed engine/test tmp base (/tmp/agent) must exist: reboots clear /tmp; mkdir -p it
     before suites (ownership test failed on connect until then).
  3. Guard allowlists needed two v0.2.10-upmerge entries (commit on main): iq_avx512.cpp's
     iq512_reference_gu_rows/iq512x_rows (runtime-gated family, portable.cpp stubs verified) and
     dflash_inco.cu's select_kernel STACK 256 (both instantiations; pointer-pass noted as follow-up).
  Classified (not regressions): ple_parity (Q2_0 test shard never present on this box) and
  dflash_refusal_test (3 of 112 static text pins drifted in the dev line itself - v0210 line's own
  generate.cpp fails the same pins; behaviour present, 64 production checks pass).
- Live phase: supervised boot, health 200 in ~42 s, helper fill + second-channel engine connected,
  smoke completion OK. Self-heal test: SIGKILL the helper -> supervisor generation 1 -> 2, health
  back to 200, helper refilled and reconnected, post-kill smoke OK.
- Prod left UP on the swift15 f16-KV iq3xxs lane; delegation can resume.

## 2026-11-06 — Broken tests fixed; same-day fork-vs-stock A/B on the merged tree

- Test fixes (commit 7e3bd3d): ple_parity now returns 77 (SKIP_RETURN_CODE) when the optional
  Q2_0 table shard is absent - absent data is a skip, present-but-incompatible still fails.
  dflash_refusal_test re-pinned 3 drifted static checks to the v0.2.10 serve-branch shape
  (readiness gate gained !df_draft_dead; recovery branch re-anchored to the draft_tier_p
  remote-lane form with 3 latches; notice count 2->3; window widened past the new arm).
  112/112 checks pass. Suite end state: 231 pass, 2 skip, 0 fail.
- A/B rerun of bench/ab-v0210 on the merged mnt tree (arm B, helper active: 64 windows,
  32 prompt rows) vs stock v0.2.10 (arm C, no helper), same night, exclusive box:
  TG 494-tok: fork 37.9/36.9/40.1 vs stock 23.3/23.4/23.0 tok/s (+60-74%);
  TG 513/514-tok: fork 35.8/36.0 vs stock 23.9 (+50%);
  tg1024 driver walls: fork 32.5-33.4 s vs stock 43.0-47.7 s (1.34x);
  PP read 16k: fork 306.5 vs stock 338.2 tok/s (stock reads ~10% faster, no helper hops);
  PP read 88k: fork 480.7 vs stock 549.0 tok/s; infill-80k completion sha identical both arms.
  Stock matches its own older rounds (C-r1/r3: PP 337-340, TG 23.9-28.2) - clean baseline.
- Incident during the first C attempt: kernel amdgpu reset (fence fallback timer, comp ring,
  23:05:05) wedged one VII mid-prefill; the stock engine stayed alive but crawled (0% GPU,
  8354/8373 expert routes skipped). Root cause chain: the prod pair supervisor survived every
  run-arm teardown (its process names never matched the harness pkill patterns) and cycled
  31 generations, its engines loading on the VIIs while arm C prefilled -> oversubscription
  -> fence timeout. Operational fix applied: quiesce the pair supervisor before any arm run.
  Hardening gap recorded: recovery exists (generation bump, QA-verified) but detection does
  not - a wedged-but-alive device is invisible to liveness watchdogs. Candidates: forward-
  progress watchdog (segment N x over budget -> engine self-exit rides the existing recovery)
  and/or amdgpu reset-counter check between requests.
- Ops pitfalls re-confirmed this round: export CUDA_VISIBLE_DEVICES="" in the user bashrc
  poisons helper/ctest GPU visibility (env -u it); bench arms need a fully drained pair
  supervisor, not just engine/helper kills; arm jsons/run-arm.sh carry neutral paths in git
  and are translated locally (uncommitted) like the 24 prod config/launcher files.

## 2026-11-06 — Quality gate on the merged tree: fork vs stock prompt-logit parity (exact)

- Modernized quality/ab6_qgate.sh (its October sha-pinned binaries are gone) into
  run/qgate-20261106.sh + run/qgate-20261106-armB.sh: prompt-mode first logits (20 prompts,
  iq3xxs-15 pack, --kv fp16 --max-context 1512 prompt lane), fork-vs-stock pcompare, prod
  stopped supervisor-first and restored on the swift15 lane after.
- Verdict: merged fork (build-qa/rpcw-hip, sha16 01621127ea680105) vs stock v0.2.10
  (sha16 a6b862e5b46fd6aa): top-1 agreement 100.00%, KL mean/median/max 0.00000 across all
  20 prompts. The v0.2.10-upmerge kernels (iq_avx512, dflash_inco select) are numerically
  transparent on the prompt path.
- Gotcha recorded: quality/qqB.json carried the OLD home repo root after generic path
  translation (/home/user/dev/Strata -> /home/srcds/dev/Strata is a deleted tree); fork-side
  configs must translate to the mnt repo root. qqB.json, qqC.json, armB/C.json and
  run-arm.sh now carry local real paths (uncommitted, same convention as the prod configs);
  git keeps the neutral forms.

## 2026-11-07 — Upstream v0.2.11 merged, gated, and serving (round 2)

- Merge (delegated to a subagent lane while prod served, per directive): upstream tag v0.2.11
  into main (eea228b86). Method per the v0.2.10 journal entry, adapted: pristine upstream
  v0.2.10 fetched as refs/tags/upstream-v0.2.10 (KEPT - graft base for v0.2.12+), graft/replace
  base made the 3-way merge resolve against upstream v0.2.10 content. 327 files (+21381/-1178),
  2 conflicts both additive (generate.cpp fork timing decls + their gemm.hpp include; setup.py
  their copy_gb math + our dflash2_fetch). Fork features depth-verified (refusal latch x12,
  helper patience x4, pair supervisor, serve helper paths); scrub scan 0 new hits.
- QA (fresh build-qa2/, prod-down window): HIP + CUDA full builds 1419 s, SONAME guard PASS.
  ctest now 291 tests (v0.2.11 adds ~58): all pass except gemm_w16_parity - a NEW upstream test
  whose X/Y scratch is ~8.2 GiB (engine output-head shape), more than the 8 GiB helper card;
  fixed to exit 77 on insufficient free VRAM (566f4c9), same convention as its no-device exit.
- Speed pair (arm B = v0.2.11 build, 2 rounds + arm C stock, exclusive box):
  TG 494-shape median ~35.1 vs stock 23.2 (+51%); honest giveback: the v0.2.10 build held
  ~37.9 on the same shape (-7%), tg1024 walls at parity (30.5-36.4 s vs 33.3 s), PP 16k 299
  (was 306/338), PP 88k 472 (was 480/549). Helper stats now inline in serve judge lines
  (v0.2.11 upstream serve change): 0 timeouts / 0 fallbacks, decode tier 37-50k rows/round.
  Greedy 80k sha 1c7d4a6d9be118d8 identical across v0.2.11 build, v0.2.10 build, and stock.
- Parity gate: prompt-mode first logits, 20 prompts, fork d01d2ea42225c391 vs stock
  a6b862e5b46fd6aa: top-1 100.00%, KL mean/median/max 0.00000 (run/qgate-20261107.sh).
- Prod cutover: old engine archived (strata.66446e21-aside), new engine d01d2ea42225c391 +
  helper-patient 2204741d231232d6 (patience env verified in-binary). Health 200 in ~40 s,
  smoke OK, generation 1, helper 4341 slots / 6.64 GiB, second channel connected, serving
  sha verified via /proc/<pid>/exe. build-v0210 symlink now -> build-qa2.

## 2026-11-07 — Teacher-forced yardstick re-run on v0.2.11: perfect pass incl. PPL

- Full 4995-position teacher-forced yardstick (run/yard-v0211.sh, prod-down ~8 min window):
  v0.2.11 build vs stock on the iq3xxs-15 pack, stock as reference. Verdict over all 4995
  positions: top-1 100.00%, KL 0.00000, KL p99 0.0000, PPL 4.052 = 4.052. The PPL gap vs the
  historical 4.115 is the pack (that run used iq3s); fork-vs-stock identity is exact.
  Per-segment scoring ~33-35 s x5 per arm (code/docs/english/multilingual/agent).
- Also answered: the pack's chat template remains qwen3.8-froggeric-v22.5.0 (pack-side,
  untouched by any merge); the only v0.2.11 serve/frontend.py change is upstream #5040
  (streamed </tool_call> tag no longer ends the value early) - the Jinja rendering contract
  is unchanged.

## 2026-11-09 — Prompt-tier claim batch on the wire (kBeginPB/kRowsPB), default OFF

- `helper-ipc-diet` 1ea98dae3: K consecutive prompt-tier claims in ONE socket
  exchange, served by the CUDA helper under ONE device-lock acquisition.
  `STRATA_CLAIM_BATCH` (read per client, clamped 1..16) AND the helper's
  `kFeaturePromptBatch` (HelloAck bit 5); default 1 = today's kBeginP/kRowsP
  bytes and code path exactly (the ack's `features` field already existed).
- Wire: `kBeginPB`=18 is `BeginPBReq{n_parts,flags,reserved0}` + K parts
  concatenated in claim order, each part byte-for-byte a full kBeginP payload;
  `kRowsPB`=19 is `RowsPBResp{n_parts,reserved0}` + K parts, each byte-for-byte a
  full kRowsP payload.  Caps (16 parts, 64 MiB per direction) refuse BEFORE any
  write; the helper length-checks every slice before device work; each part is
  validated against ITS OWN claims by the existing whole-or-nothing validator.
- Refusals: part beyond the queue cap = kErrGeometry (silent wire); a caller
  whose claims differ from the queued front part = kErrSeq checked BEFORE the
  flush, so no reply is burned and no row lands out of order; a new claim after
  the batch's reply is answered = kErrBusy.
- Engine-side finding (the reason K>1 is gated, not a win yet): a layer's reply
  is consumed INSIDE that layer's walk (gather into single-slot m.Dm/hp_stage,
  same-layer fallback on single-slot ext_x_), so a per-layer begin/finish walk
  flushes a ONE-part batch.  The transport and the single-lock serving are in
  place; giving the prefill walk the depth to run K begins ahead of the first
  finish is follow-up and needs a GPU window.
- Evidence (CPU-only, prod untouched): expert_helper_socket_test PASS, 288
  checks (new section 20a-20d); build_l3.sh both trees green - CUDA guard PASS
  (helper fatbin sm_86) + ctest -R helper 5/5 (socket 7.03 s), HIP guard PASS
  (gfx906 HIP: libamdhip64 + .hip_fatbin + STRATA_HIP_GFX906) + ctest -R helper
  1/1 (that tree's frozen conf has STRATA_BUILD_TESTS=OFF).  Log
  `build-mixed-gpu/claim-batch-build.log`.  Goldens, launcher and /mnt/srcds/Strata
  untouched; wire documented in docs/work/MIXED_P2_REFERENCE.md.

## 2026-11-09 — Claim batch after the GPU windows: one-part flush = batch=1; the lever is structural

- Operator ran the two GPU windows on prod (arms of ab9-20261109-215940/220431-D):
  STRATA_CLAIM_BATCH=1 parity exact (greedy sha 01db7a9d… every leg) and perf
  identical to the pre-batching build; STRATA_CLAIM_BATCH=8 parity exact on real
  hardware (kBeginPB/kRowsPB proven end-to-end) but 8k prefill ~6% SLOWER
  (8.60 vs 8.11 s) - all 256 exchanges flushed as parts=1, so K>1 bought only
  wrapper cost.
- VERIFIED CLAIM STRUCTURE (code + the D helper logs): exactly ONE prompt claim
  per MoE layer - the layer's whole pair list in one dense plan (begin
  prefill.cpp:5377 / M6 lane :5317, finish :6450, single-slot hp_rows/hp_claims/
  hp_stage).  256 'serve tier=p' lines = 32 layers x 8 walks, layers strictly
  ascending per walk, 490-1245 rows per line = the layer's full row set; the
  batch=8 arm logged parts=1 on 256/256 lines.  Layer l+1's routing reads layer
  l's MoE output, so cross-layer begin-ahead is not definable, and within a
  layer there is exactly one claim.  => TASK B (within-layer coalescing) has
  nothing to coalesce: the lever is dead in this pipeline, not unimplemented.
  Engine-side change: the claim-site comment now records the structure and the
  evidence; no pipeline change (a forced split of one layer's rows would add
  bytes and lock round-trips, not remove them).
- TASK A (the perf fix): a queue of exactly ONE part flushes as a plain
  kBeginP/kRowsP exchange (write_beginp shared with begin_prompt; the part
  carries its begin deadline; the reply goes through the single-claim read and
  validator; the claims-order refusal runs before the flush).  begin_outstanding_/
  req_id_/seq_/prompt_requests_/prompt_rows_returned_ bookkeeping identical.
  K>=2 keeps multi-part batching for callers whose shape supplies it.
- Test: socket section 20e pins the one-part flush's frame TYPE and its byte
  equality with a batch=1 frame captured in the same run, twice (the walk's next
  claim flushes alone too), and that the channel stays open.
  expert_helper_socket_test PASS, 305 checks.

## 2026-11-09 — Serve-path sub-spans: where the 44 ms per prompt claim actually goes

- Instrumentation ONLY (no behavior change), CUDA helper `serve_prompt_core` /
  `serve_prompt` / `serve_prompt_batch`: steady_clock sub-spans, gated on the
  existing STRATA_RPC_PROFILE (the span sink is a nullptr when profiling is off,
  so the default path reads no clock and writes no extra text).
- Spans named for the phases the code actually has: `parse_us` (BeginPReq header,
  geometry caps, layer/format gates), `lookup_us` (pair loop: primary bitmap +
  cache.slot_of + per-expert cap, AND the grouped entry map), `quant_us` (the
  fp32->q8_1 twin, image_format 1 only), `upload_us` (the three async enqueues:
  q8 H2D, GroupMetaP H2D, scratch clear), `launch_us` (grouped kernel enqueue +
  cudaGetLastError), `ksync_us` (cudaStreamSynchronize - THE GEMM span: it drains
  the queued uploads, the clear and the kernels), `scan_us` (H14 all_finite over
  the mapped-host rows), `pack_us` (reply serialize = the rows' PCIe read out of
  the mapped cudaHostAlloc d_out_p).  Their sum is lock_us; reply_us stays the
  socket-write span.
- The serve line grows ADDITIVELY after the unchanged prefix (and after the
  batch's `parts=N`): single-claim and batch lines both append
  `parse_us= lookup_us= quant_us= upload_us= launch_us= ksync_us= scan_us=
  pack_us=`; the batch line reports the WHOLE batch's spans (slice pass folded
  into parse_us, every part summed).  Each line is written in ONE fprintf so two
  --socket2 serve threads cannot interleave it (the reason the first draft,
  which split the line across writes, was replaced).
- Readers: ab9_collect.py matches the line PREFIX (search/finditer) and ignores
  what follows - confirmed unaffected, unchanged.  claim_breakdown.py extended
  (tolerantly): a SPAN regex collects the 8 fields per prompt claim when present,
  prints sum/median/share-of-lock per span plus the spans-sum vs lock gap, and
  behaves exactly as before for logs without them.  Verified on tonight's
  220431-D log (256 claims, no spans -> identical output) and on a synthetic
  mixed log (5 of 6 claims with spans -> share table, ksync 95%).  The two bench
  scripts are left UNCOMMITTED in /mnt/srcds/Strata for the operator to commit.
- CPU-only verification: build_l3.sh both trees green, guards PASS (helper fatbin
  sm_86 + -patient alias refreshed; gfx906 HIP), ctest -R helper 5/5 and 1/1.
  The GPU window that reads these fields is the operator's to run.

## 2026-11-10 — Format 0: the engine quantizes the prompt image, the helper's lock stops paying for it

- WHY format 1 today (the answer, with evidence): the chooser is
  `src/core/expert_helper_socket.cpp` `build_beginp_payload`:
  `f32_img = prompt_f32_ && !q8k_img`, and the CUDA helper's hello ack advertises
  `kFeaturePromptF32` unconditionally (`tools/mixed_gpu/sidecar/helper_main.cpp`),
  so an f32-capable peer always wins the choice. M4c (b) made that deliberately:
  its own comment says the quantize "leaves THIS critical path; the helper
  quantizes in its own process, overlapped with the primary's compute". Format 0
  was never unwired (the else branch is `quantize_q8_1_cpu` per row) - only
  shadowed by the peer's fp32 bit.
- The span profile killed that premise: the helper quantizes INSIDE its device
  lock, so nothing overlaps it. 256 claims, `ab9-20261109-235338-D-helper.log`:
  quant 2.630 s = 44.9% of lock (med 6036 us/claim), ksync 39.1%, pack 11.1%.
- Implementation is a GATE, not a new path: `STRATA_HELPER_Q8_IMAGE` (read per
  call, default OFF = today's bytes) makes the engine send image_format 0 - the
  tier's original wire, uploaded verbatim by the helper, quant span ~0, no
  helper change. Q8_K keeps precedence; a peer without the fp32 bit was already
  format 0 (env inert); env unset = byte-identical default.
- Parity verdict: NO shift. `quantize_q8_1_cpu` (src/core/expert_helper_socket.cpp
  :126) and the helper's `quantize_q8_1_twin` (helper_main.cpp :120) are the
  same statements - a mechanical diff of the two bodies differs only by the
  engine's 3 comment lines. Same constants/clamps/amax+sum order/fp16 scales/
  std::roundf/36-byte block layout, so the grouped kernel gets the exact bytes it
  makes for itself. No re-baseline needed.
- Size math (real numbers, same window): image 1469 MiB fp32 -> 413 MiB q8_1
  over 256 claims = 3.56x, ~1056 MiB and ~0.25 s of wire at the measured
  4.42 GB/s. Per row 10240 B -> 2880 B. The quantize cost is MOVED (helper lock
  -> engine begin path), not deleted; the lock gain is the point (it is the M7
  serialization point, measurable in queue_us).
- Tests: socket section (21) pins the format-0 round trip (frame type stays
  kBeginP; image_format 0, n_q8 = n_sel*(H/32)*36, image = quantize_q8_1_cpu of
  the tier's rows byte-exact, kRowsP reply applied in one exchange in claim
  order), the env's inertness (no fp32 bit -> format 0; Q8_K keeps precedence),
  and the frame byte totals with the delta pinned to n_sel*(H*4-(H/32)*36).
- CPU-only: build_l3.sh both trees, guards, ctest -R helper. No GPU program run.
- Build note (transparency): the first run FAILED - my gate edit dropped the
  existing `q8k_img` declaration (compile error at expert_helper_socket.cpp:691,
  log `build-mixed-gpu/q8-image-build.log`). Restored verbatim, amended
  (`fa733f62d`), rebuilt green. Final: socket test 326 checks (21 new), PASS;
  CUDA guard PASS + ctest -R helper 5/5; HIP guard PASS + 1/1
  (`build-mixed-gpu/q8-image-final-build.log`).

## 2026-11-10 — Engine-side per-layer spans: where the ~256 ms MoE layer actually goes

- Q1 answered: the engine's claim line is `src/prefill/prefill.cpp:6501`
  (sync lane only, gated on STRATA_RPC_PROFILE; :6462 before this commit),
  fields `layer rows sel d2h_us begin_us reply_us img_kb reply_kb fallback`
  (+ `img_fmt` now); the M6 PIPE lane never emits it (the worker owns the tail).
  The span line is emitted at `:6746`.  The claim lines ARE in
  ab9-20261110-005056-D-engine.log (256 of them, begin_us sum 2.370 s) - the awk
  that found 0 was reading a pattern, not the file;
  `awk '/begin_us=/{split($0,a," ");for(i=1;i<=NF;i++) if(a[i]~/^begin_us=/) s+=substr(a[i],10)} END{print s}'`
  prints 2.37 s.
- L4 spans (`STRATA_ENGINE_SPANS=1`, default OFF, no clock read and no line when
  off): non-overlapping wall segments per MoE layer of the batched walk, marked
  at boundaries the walk already has, emitted at the layer's end as a SIBLING of
  the claim line: `rpcprofile: span layer=N pipe=P layer_us attn_us router_us
  claim_us expert_us wait_us apply_us helper_us rest_us`.  attn = layer start ->
  the router mark; router = router -> the helper block's entry (top-k + the
  residency/selection scan); claim = block entry -> the frame on the wire (the
  quantize sits in here when STRATA_HELPER_Q8_IMAGE is on); expert = send -> the
  reply read begins (the primary's own expert work, where a helper answer is
  free); wait = finish_prompt (or the PIPE outcome wait) = the TRUE helper
  exposure; apply = reply -> the block staged and its H2D queued; rest = the
  remainder; helper = send -> staged (overlay, the round trip the layer feels).
- Also fixed a reporting defect the fmt0 window exposed: the claim line's img_kb
  reported the FP32 image size regardless of what the wire carried.  It now
  reports `prompt_last_image_bytes()` (the bytes the frame built) and appends
  `img_fmt=0|1|2`.  Measure-only: a store of two members per frame build.
- Reader: `bench/ab-v0210/rpcprof/engine_breakdown.py` (main tree, uncommitted -
  operator commits).  Works on the existing logs today (claim-line summary) and
  prints the span table + helper-wait share with span lines present.
  ab9_collect.py is untouched: its regexes are prefix-anchored search/finditer
  and ignore appended fields and the new sibling line.
- Evidence from the fmt0 window's helper log (read with claim_breakdown.py, CPU
  only): quant 0% (was 44.9%), lock 5.856 -> 3.376 s, ksync now 70% of lock, pack
  20%, scan 5%, upload 3%.  Helper reply_us sum 12.2 s = the socket write
  blocking on the engine (the engine is busy quantizing + computing) - the write
  is outside lock_us, so it does not stall the GEMM, but with a second channel
  it is the thing to watch.
- CPU-only verification: build_l3.sh both trees, guards, ctest -R helper.  No new
  unit test: the span emission is inside the GPU-only layer walk (no CPU-only
  render of it exists); the compile plus the socket suite are the check, and the
  reader was dry-run on a synthetic span log and on the two real engine logs.
- Build note (transparency): the first run FAILED - the claim line reached the
  new accessors through `m.hp`, which is `RemoteExperts`, not the client; added
  the forwarding accessors (`include/strata/core/remote_experts.hpp`,
  `prompt_chan()`-based, 0/-1 with no channel).  Failed log
  `build-mixed-gpu/engine-spans-build.log`; the green run is the rerun on the
  amended commit.