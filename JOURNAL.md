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
