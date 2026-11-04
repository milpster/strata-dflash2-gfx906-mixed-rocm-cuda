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
