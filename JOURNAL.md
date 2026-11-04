# JOURNAL — ~/dev/Strata branch build-mixed-v0210

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
