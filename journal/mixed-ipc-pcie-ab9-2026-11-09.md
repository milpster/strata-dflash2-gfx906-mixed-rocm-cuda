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
