# AB9 window — mixed-IPC/PCIe prefill overhead at 8k/80k + Stage-2 memory levers (2026-11-09)

Window executed 12:24–13:31 (5 attempts; runs 1–4 each died on a distinct harness bug,
all fixed and committed; run 5 completed the full protocol). Prod was taken down by
explicit user order (host-guard memory recovery: MemAvailable 7.9→51.5 GiB) and
restored supervised at 13:30 (real restore: loaded:true, engine+helper+supervisor gen 1,
both sockets; health 200 after ~75 s).

## Harness bugs found and fixed (branch `ab9-window-harness`)

| run | failure | fix (commit) |
|---|---|---|
| 1 | driver crashed on `content=None` (reasoning ate the 16-token warm budget — promo_check trap) | `8d9966d9e` reasoning_effort=low + content/reasoning_content fallback, warm mt 64 |
| 2 | gate2 demanded tier=p after a 512-B warm (claims need a real 8k window); engine-pid pgrep raced the fork; teardown drained 5 min against a live orphan engine | `b6cfb5fe8` readiness=`loaded:true`+engine pid+elog; gate2 after first 8k leg; kill_pair pattern fallback |
| 3 | arm A's serve wrapper (server.py) outlived its engine, kept :8009 → arm B "port already in use" | `62e978f68` kill_pair TERMs/KILLs port holders, verifies 8009 free before next arm |
| 4 | stage2 sidecar `"${8000}-${seq}"` → unbound numeric var under `set -u` | `acab85fc2` |
| 1–4 | **phantom restores**: stop-prod kills engine+helper but not the arm serve wrapper → wrapper answers /health 200 with a dead engine; launcher writes `REFUSING: port 8009 occupied`; script exits "restored" while prod is down (this looked like "the pair keeps dying on its own" 4× today — it never was) | `0285a7b8f` restore requires loaded:true + live engine pid + fails fast on REFUSING |
| — | stop-prod.sh stale (v0210 patterns, old sockets) | `3e73ab334` `/v021[0-9]/` + f16kv/ud sockets + STRATA_PROD_TREE default |
| — | gate1 couldn't accept the user-ordered pre-takedown | `34a5658e8` clean-down acceptance path |

## Stage 1 — does the helper help prefill, and does the pipe hide the IPC cost?

Arms: A=PREFILL_HELPER off, B=on, C=on+PIPE. PP = judge-line fresh-read tok/s
(`prompt N tokens = 0 reused + N read in T ms`; 8000 B ≈ 1648 tok, 80000 B ≈ 16480 tok).
Gates: gate3 participation A=0 / B=C=96 claims (both ctx); gate4 8k sha byte-identical
across A/B/C (`01db7a9d…`) in runs 2, 4 and 5; gate6 no flags on A/B/C.

| ctx | A tok/s | B tok/s | C tok/s | gain(B) | C vs A | transfer_share(B) | pipe effect |
|---|---|---|---|---|---|---|---|
| 8k  | 192.25 | 198.0 | 196.4 | **+3.0 %** | +2.2 % | **22.2 %** of prompt wall (Σbegin+Σreply+Σd2h; Σreply 5.20 s of 5.65 s) | par-to-slightly-negative: C 196.4 < B 198.0 — the pipe GAVE BACK ~28 % of B's small gain (the collector's "recovery 0.278" is C sliding from B toward A, not an improvement) |
| 80k | 308.1  | 305.7 | 305.3 | **−0.8 %** | −0.9 % | 0.7 % | no help (C ≈ B, both below A) |

Correction 2026-11-09: the pipe did not measurably help at ANY length — it is par with
plain-helper at 8k (hot walls B 8.49 s vs C 8.49 s) and slightly negative on judge
medians; this matches the banked "M6 pipe par/dead". The +3 % at 8k belongs to the plain
helper (B) alone.

Mechanism: claims are capped at 96 regardless of prompt size — rows/claim collapses
2330→256 from 8k→80k, so helper contribution dilutes to nothing at 80k while its fixed
per-claim cost amortizes (58.7 ms→10.7 ms). The 8k overhead is dominated by the helper
reply payload (rows back over unix socket + PCIe D2H), not by d2h_us of the image
(Σd2h ≈ 10 ms/3 legs — negligible).

TG sanity (no loss; C fastest both runs): A [22.3/21.9], B [20.1/24.4], C **[17.0/10.6]** s
(run 4: A 63.2 / B 30.9 / C 26.9 — A/B vary, C consistently fast).

Anomaly: A-8000-2 wall 106.7 s with VII#0 at ~128 MiB free mid-leg (decode-phase VRAM
paging stall; vram sidecar). Judge-line PP unaffected (215 tok/s) — PP medians clean.

**Verdict: adoption bar (≥5 % PP win, no TG loss, parity) NOT met. C best case +2.2 % at
8k, negative at 80k. `STRATA_PREFILL_HELPER_PIPE` stays off. The "caps dominate" branch
fires → next lane = claim amortization → bf16 reply wire (halves Σreply bytes, the
measured 22 %-of-wall term).**

## Stage 2 — memory levers with no perf compromise

Arms (single-var diffs, validated): m1 helper slots 4600→4400 (+reserve trim),
m2 `--kv int8`, m3 helper fill 7200/reserve 128, m4 layer-split 32→36.
Protocol caveat: stage-2 legs have NO warm request (stage-1 arms did), so every m-arm
leg-1 is cold: 122–168 tok/s vs hot leg-2 193–230 tok/s. Hot legs put ALL four arms
within noise of C (196–200): m1 193.2, m2 217.9, m3 195.4, m4 230.2. The collector's
drift_vs_C (median of cold+hot: m1 0.80, m3 0.87 → "fail") is the cold-leg artifact, not
a perf cost. TG512 all on par (29.6–33.3 s vs C-shape legs).

- **Parity: m4 FAILS** — its cold 8k sha `c2e533c4…` breaks the canonical `01db7a9d…`
  that every other arm/leg/run produces (layer placement changes reduction order →
  different logits on first inference). m1/m2/m3 keep parity.
- **KV policy (user, 2026-11-09): KV stays fp16 — `--kv int8` is NOT adoptable**, so m2's
  measured perf-neutrality + parity are experiment evidence only, not a keeper
  candidate. This supersedes any earlier "m2 keeper" note.
- **No VII headroom materialized**: every m arm ended 8k legs at 229–489 MiB free with
  sub-512-MiB warnings that A/B/C never hit — freed VRAM is absorbed by adaptive caches
  (expert-cache auto) or offset by other growth. Levers as configured buy nothing
  measurable on the VIIs; next Stage-2 step = pair an fp16-compatible lever (m1/m3 class)
  with an explicit cache cap.
- Harness gap: `mem-baseline.sh` found no processes during window arms (its discovery
  patterns target prod launchers) — all before/after baselines empty; VII-only vram
  sidecar (no 3080). Fix before the next memory lane.

## Artifacts

- `bench/ab-v0210/rpcprof/out/ab9/results.tsv` / `results.md` (rows=10, tg-legs=7; git `acab85fc2`)
- per-leg `out/ab9/<arm>-<leg>.json|-dmon.txt|-vram.csv`; engine/helper logs
  `bench/ab-v0210/rpcprof/logs/ab9-20261109-130510-*`
- SPECs: `SPEC-ab9.md`, `SPEC-stage2.md`; plan: `~/.opencode/plan/mixed-ipc-pcie-prefill-8k80k.md`

## Wire-map corrections (2026-11-09, post-window recon)

- **"96 claims" was a misread**: claims are per (chunk × layer); 96 = 3 legs × 32 MoE
  layers × 1 chunk (both harness prompts fit one 16 384-tok chunk). A 262k-token prompt
  would be 16 chunks → 512 claims. Deep (T ≤ 2048) vs shallow row caps flip at
  `prefill.cpp:5088-5093`.
- **Arm C's "claims 0" in the table is a profiling blind spot**, not helper-off: the PIPE
  path (`prefill.cpp:6434` "sync path only") never emits engine rpcprofile claim lines.
- **The IPC overhead is helper-side serialization, not bandwidth**: engine reply_us
  1.734 s/prompt vs helper lock 1.567 s, queue 0; visible wire transfer ≈ 166 ms/prompt
  at ~4.4 GB/s. Cost sits in the helper's scalar finite scan (2330×2560 ≈ 6 M float
  checks/claim), per-claim memsets, 24 MB payload zero-fill+copy per claim, the engine's
  O(rows×claims) claim lookup (5.4 M comparisons/claim), and per-4-KiB poll loops
  (no SO_SNDBUF/SO_RCVBUF anywhere).
- **bf16 reply wire is DEAD**: reply rows are fp32 compute outputs (10 244 B/row) —
  bf16 = parity break by construction, saves only ~83 ms of an 8.5 s prompt (~1 %), and
  the L8 bf16 tap-wire precedent measured 5.5 % slower (`docs/work/L8_BF16_TAP_WIRE`).
- **shm reply ring**: parity-safe but highest structural cost (epoch/stale protection,
  channel-death semantics) for a modest copy share — deferred behind the cheap bundle.

## Next lanes

1. **Helper serialization diet** (parity-safe, no wire changes): SO_SNDBUF/SO_RCVBUF,
   streaming fast-path past per-chunk poll, payload zero-fill elimination, sorted claim
   lookup, SIMD finite checks (both sides), memset-only-written-rows. Target: a slice of
   the 1.57 s helper lock + 1.73 s engine reply per 8k prompt — the only path that could
   push the 8k helper gain (+3.0 %) over the 5 % bar.
2. Stage-2 redo with mem-baseline fixed (arm-process discovery) + cache-cap pairing on
   fp16-compatible levers only (m1/m3 class; KV stays fp16 per user policy; m4 parity
   failure means layer-split is off the table unless parity is re-gated).

## Diet validation window (A D) — started 17:16

- Prod down (user-ordered 17:08): port free, VRAM drained, MemAvailable 52.7 GiB.
- Diet trees built from `helper-ipc-diet` (build-qa2 cache templates, `-j11`):
  - `rpcw-cuda-diet` (16:50): 5/5 helper tests; `-patient` alias = copy of cmake `strata-expert-helper` (timeouts come from harness cmdline, `AB9_HELPER_TIMEOUT=63115200000`).
  - `rpcw-hip-diet` (17:15): 5/5 helper tests incl. ownership (mkdir fix built in).
- En-route fixes: `build_l3.sh` double-dash cmake arg (`929bc2632`); ownership test mkdirs `/tmp/agent` (`3a80ed0be`, reboot wiped it — baseline failed identically, NOT a diet regression); harness arm-scoped engine/helper pgrep paths (`89ce527eb`); arm D + `AB9_ARMS` support (`883a097ad`).
- Run-5 outputs archived to `out/ab9-run5-20261109` (stale m4 sha would poison gate4).
- Window: `AB9_ARMS="A D"` — A = v0214 helper-off control, D = diet binaries helper-on. Decision: gate4 byte-parity A↔D + PP delta vs ≥5% bar (B measured +3.0% @8k, −0.8% @80k).

## Diet validation window (A D) — COMPLETE 18:41, attempt 5

Attempt history: 1 (17:16) D boot died on 4 conversation-cache-disk flags (disk-cache-streaming branch only — stripped from arm D, `7c5cb8490`); 2/3 (17:26/17:59) box hung mid-arm-A (user SysRq reboots — GPU hiccups, out of scope); 4 (18:13) D helper died at fill: `native_expert_grouped/gu: no kernel image` — rpcw-cuda-diet fatbin was sm_120 (CMake default) because build_l3.sh's replication grep misses `CMAKE_CUDA_ARCHITECTURES` (stored :UNINITIALIZED=86 in qa2). Fixed (`84eaac524`) + rebuilt for sm_86 + **golden frozen build confs** (`freeze_build_conf.sh`, `golden/rpcw-{cuda,hip}.cmake`, build_l3 `-C` mode, `9765bac6c`) — template-cache drift ends here.

### Results (out/ab9, run of 18:31:20)

| arm | ctx | PP med tok/s | gain vs A | claims | parity |
|---|---|---|---|---|---|
| A (v0214, helper off) | 8000 | 195.3 | — | 0 | sha 01db7a… |
| D (diet, helper on) | 8000 | **208.2** | **+6.61%** | 96 | same sha |
| A | 80000 | 308.4 | — | 0 | same sha |
| D | 80000 | 306.9 | −0.49% (within noise; A spread 1.9%) | 96 (256 rows) | same sha |

- gate3 participation: A=0, D=96 both ctx ✓. gate4 byte parity A↔D ✓. gate6 flags: none ✓. Fresh-judge: True.
- TG 1024: D 47.86s vs A 50.22s — **no TG loss** (slightly better).
- Helper-side @8k: Σlock 1.40s/leg (B was 1.57), Σreply 1.40s/leg (B was 1.73) — wire+lock diet landed; fixed_cost/claim 48.0ms (B ~54ms).
- **Verdict vs bar (≥5% PP, no TG loss, byte parity): CLEARED at 8k (+6.61%); 80k neutral; adoptable.**

### Open anomaly (follow-up arm E, one cheap window)

Attempt 4's D ran one 8k leg with the helper DEAD (sm_120 crash → engine CPU-path fallback): 7.672s ≈ 250 tok/s pace — FASTER than helper-on D (8.03–8.19s) and A (8.48–8.62s). Single leg, but beyond A's spread. If it holds, diet engine + PREFILL_HELPER=0 beats helper-on by ~3% more at 8k (and frees the 3080's 6.8 GiB). Proposal: arm E = diet engine, helper off, same protocol (~10 min window).

Prod restored supervised 18:41 (verified health 200, loaded:true, engine pid 450257, sockets).

## Arm E (diet engine, helper OFF) — COMPLETE 18:57

Window A/E 18:46-18:56 (all legs ran; gate3 initially misfired counting E as helper-on — collector fixed to derive helper-on from run cfgs, gates now pass offline, `results.md` regenerated).

| arm | 8k PP | 80k PP | parity |
|---|---|---|---|
| A (v0214, helper off) | 194.5 | 307.5 | sha 01db7a… |
| E (diet engine, helper off) | 195.1 (+0.3%) | 309.7 (+0.7%) | same sha |

**Verdict: the diet engine binary alone is par with prod. Attempt-4's 7.67s leg was a fluke. The full +6.61% @8k (arm D) is the helper/IPC diet's contribution.** Attribution clean: A≈E < D. Cutover config if adopted: diet binaries + STRATA_HELPER on (D shape). TG: E 45.9s vs A 46.8s — no loss.

Prod restored 18:57 (verified loaded:true); pair self-restarted once after (supervisor recovered).

## Post-E prod blip + clean relaunch 19:12

The 18:57 restored pair died ~19:00 (lifeline request_stop + helper SIGTERM; likely my 19:05 relaunch racing the 18:56 supervisor's restart cycle — both died; stale empty lock files removed). One clean supervised launch at 19:12: prod UP, loaded:true after 60s (launcher 746169, engine 746167).

## Cutover decision pending (user)

D shape (diet binaries + helper on) is the validated winner: +6.61% @8k, parity, no TG loss, 80k neutral. Blocker: the diet engine lacks the 4 conversation-cache-disk flags (disk-cache-streaming branch). Options: (a) merge disk-cache-streaming + helper-ipc-diet, build from golden confs, quick A/D' validation window, cut over with disk cache intact; (b) cut over now with disk flags stripped (loses prod disk conversation cache until merged); (c) hold.

## 21:40 window attempt 6 (A B D) - D boot fail; root cause: golden hip conf was a CUDA conf

**Window**: A ok (8k 8.61/8.41/8.61, 80k 47.75/47.53/47.54, tg 48.6; gate2 helper-off PASS, gate4 sha all `01db7a9d…`).
B ok (8k 8.57/8.45/8.37, 80k 49.29/48.92/48.90, tg 51.5; gate2 helper-on 64 lines PASS, sha same).
D **boot fail exit 2**: `conversation parking does not support a layer split on one GPU (--split-device 0)`.
Watcher killed the auto-restore per user order (prod stays down; results first).

**Chain** (each step verified, not guessed):
1. B.json ≡ D.json except exe/helper -> not a config diff. v0214 boots it, merged build refuses.
2. Check fires iff split_devs==[0] iff cudaGetDeviceCount()<=1 at option-parse.
3. ldd: merged "hip" engine links **libcudart.so.13 + libcublas** (v0214: libamdhip64 + libhipblas);
   fatbin 70x sm_120, **zero gfx906**; sm_86 boot line absent in v0214 logs, present in D.
   => real CUDA runtime, saw only the 3080, n_dev=1. Deterministic (probe x2 on idle GPUs).
4. Not the merge, not rocm: `golden/rpcw-hip.cmake` had GGML_HIP=OFF/STRATA_ENABLE_CUDA=ON
   (those are actually fine - build-906r = v0214's exact build tree, md5 b9d8162f, says the same)
   but was missing **STRATA_HIP_GFX906** (CMakeLists:93 - the option that picks
   enable_language(HIP) at :295 over enable_language(CUDA) at :333) and **STRATA_ENABLE_DFLASH2**.
5. Root cause: `freeze_build_conf.sh` grep `STRATA_[A-Z_]+` lacks `0-9` (GGML_ pattern had it).
   Digit vars silently dropped; tree configured pure CUDA sm_120. "ALL L3 BUILDS GREEN" was
   false confidence - helper tests pass in a CUDA-built tree too.

**Fixes** (worktree helper-ipc-diet, commit `3f62ee815`):
- freeze regex -> `STRATA_[A-Z0-9_]+`; golden/rpcw-hip.cmake re-frozen from **build-906r**
  (the tree whose strata md5-matches running prod v0214): STRATA_HIP_GFX906=ON, DFLASH2=ON,
  HIP clang rocm-6.1.0, gfx906, zero CMAKE_CUDA keys.
- build_l3.sh post-build backend guard: hip trees must show cache ON + no nvcc key +
  ldd libamdhip64 + .hip_fatbin section + gfx906 strings; cuda trees must show helper sm_86.
  (cuobjdump --list-elf shows nothing useful on HIP binaries - "does not contain device code".)

**Rebuild** rpcw-hip-diet from fixed golden in flight; then boot probe, then full A/B/D rerun.
Window verdicts table for attempt 6: A/B numbers above stand as the same-window baseline for the rerun.

## 20:05 window attempt 7 (A B D) - CLEAN, all gates green; verdict: diet = +2.2% vs prod, below the 5% bar

Rebuilt merged HIP engine from fixed golden (guard PASS) + v0214-mirror B + helper-off A, same window.
Gates: g2 PASS (A off 1284 lines; B/D 64), g3 PASS (A 0 claims, B/D 96 each), g4 exact sha
`01db7a9d…` across A/B/D/E 8k, g6 none. Watcher intercepted restore (19:59:40); prod stays down per user order.

| arm | 8k PP med (spread) | 80k PP med | tg walls |
|---|---|---|---|
| A helper-off | 195.6 (3.7)  | 309.1 | 26.3/23.5 |
| B prod mirror| 199.7 (23.5) | 305.4 | 25.8/8.8  |
| D merged     | 204.1 (1.1)  | 306.3 | 19.5/20.8 |

- D vs B: **+2.2% @8k**, +0.3% @80k (wash), TG no loss (D fastest consistent legs).
- D vs A: +4.3% @8k. Helper internals confirm the diet: fixed_cost/claim 56.8->49.1 ms (-13.5%),
  Sigma-reply -14.8%, Sigma-lock -7.7%, transfer_share 0.209->0.192. D spread 1.1 vs B 23.5 (B leg-1 9.37s outlier).
- **Why run-5 showed +6.61% and this shows +2.2%**: run-5's pre-merge tree ran the old rpcw-hip conf;
  tonight's D runs EXACTLY v0214's build conf (build-906r golden). Same-conf, same-window: the true
  diet effect on wall-clock @8k in prod's conf is ~2.2% vs prod mirror, ~4.3% vs helper-off control.
  The IPC savings are real but largely overlap engine compute in this configuration.
- ADOPTION BAR (>=5% PP, no TG loss, parity): parity + TG met; PP +2.2% vs prod - **NOT met**. User decides.

Lever analysis (claim_breakdown.py, 256 claims from attempt-5 D logs): transport stack ceiling ~+4-5%
(batching x8 <= ~+3%, wire overlap ~+1.4%, begin overlap ~+1.5%); compute (HIP helper on VII2) remains the big lever.
