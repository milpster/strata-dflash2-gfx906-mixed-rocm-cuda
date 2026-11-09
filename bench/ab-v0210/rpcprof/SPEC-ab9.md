# ab9 — Stage 1 window harness spec (PCIe/socket transfer vs prefill gain at 8k + 80k)

Plan of record: `/home/srcds/.opencode/plan/mixed-ipc-pcie-prefill-8k80k.md`.
Harness lives in `bench/ab-v0210/rpcprof/`: `ab9_pcie_ctx.sh` (window) + `ab9_driver.py` (load) +
`ab9_collect.py` (parse/derive). Implement from this spec; nothing here has been run yet.

## Why it can fail (solved first)
`launch-prod-*.sh:75` boots the helper with `env -i ...` (clean environment), so `STRATA_RPC_PROFILE`
exported by the window shell NEVER reaches the helper → `rpcprofile: serve tier=p ... queue_us=... lock_us=...`
lines are absent (exactly what AB7 hit). The engine at `:99` uses `env -u ...` and inherits fine.
Gate: `ab9_pcie_ctx.sh` must fail fast if the helper log has zero `rpcprofile: serve` lines after the warmup
request (prereq: launcher env pass-through, see SPEC-prereq).

## Prod facts to mirror (measured 2026-11-08, `bench/ab-v0210/rpcprof/out/mem-stage2-base-20261108-172208.txt`)
- Engine `v0211/build-mixed-gpu/rpcw-hip/strata`, launcher `launch-prod-swift15-f16kv-dflash-w4-q4_0-mixed.sh`,
  config `prod-config-swift15-f16kv-dflash-w4-q4_0-mixed.json`, port 8009, `--kv fp16`, max-context/kv-resident 262144.
- VRAM at idle: VII#0 16309 MiB/16662 used (~59 MiB free), VII#1 16210 MiB (~158 MiB free), 3080 7145/8192 MiB
  (**698 MiB free**). Engine log already warns `below 512 MiB ... driver can page GPU memory to system RAM`.
- Host: MemAvailable ~9.1 GiB, swap 8.4 GiB used; engine RSS 47.09 GiB (anon 39.37, file 7.72),
  helper RSS 6.75 GiB (file-backed 6.56, Pss 3.51), vision 0.93 GiB.
- Helper: 4324 experts filled = 6.61 GiB; relief drops all 4324 pairs (6.60 GiB back); complement registered
  33.37 GiB; 15124 eligible pairs; slots asked 4600.

## Arms (one variable; same binary/pack/config/boot)
- A `STRATA_PREFILL_HELPER=0`
- B `STRATA_PREFILL_HELPER=1`, `STRATA_PREFILL_HELPER_PIPE=0`
- C `STRATA_PREFILL_HELPER=1`, `STRATA_PREFILL_HELPER_PIPE=1`
All: `export STRATA_RPC_PROFILE=1 STRATA_DBG_PROMPT_HELPER=1` in the window shell before the launcher call.
`STRATA_PREFILL_HELPER*` are engine-side (config `env` block) — per-arm engine restart must override them
explicitly in the window shell; do NOT edit prod configs.

## Legs (contexts 8k and 80k only)
Per arm, same boot: fresh-prefill 8k ×3, fresh-prefill 80k ×3, one TG1024 cached rerun (sanity only).
- Fresh prefill = new unique prompt text per leg (no cache reuse); judge = engine line
  `prompt N tokens = 0 reused + N read in X (PP tok/s)`.
- Deterministic prompt builder (fixed seed, no timestamps) so the 8k/80k byte lengths are identical across arms.
- Concurrency 1 (single request in flight) — Stage 1 measures the serial claim chain.
- Sampling during each leg: `nvidia-smi dmon -s utr -d 1` (3080 PCIe tx/rx + util) and
  `rocm-smi --showmeminfo vram --csv` every 5 s → per-leg sidecar files.
- Per-leg log slicing: record byte offsets in the engine log + helper log before/after the leg; the collector
  reads only that slice (no cross-leg contamination).

## Gates (fail fast, before the drive)
1. Pair healthy: `/health` 200, engine + helper pids alive, both sockets present.
2. Profile lines present: helper log contains `rpcprofile: serve tier=p` after the warmup request.
3. Participation identity: claim counts per context equal across arms (`STRATA_DBG_PROMPT_HELPER` lines);
   mismatch → abort with the two count lists printed.
4. Byte parity: temp 0, thinking off, FIRST request on a fresh boot per arm at 8k → sha256 of the completion;
   A vs B vs C must match, else abort the perf verdict.
5. Host guard: MemAvailable ≥ 12 GiB and swap used < 12 GiB at window start (a swap-debt host killed a prior gate).
6. Free-VRAM guard: any leg that trips `below 512 MiB` on a VII is flagged in the output as a confound.

## Collection + derived table (`ab9_collect.py`)
Per (ctx, arm): PP tok/s (median + spread), claims/leg, rows/leg, Σ`img_kb`, Σ`reply_kb`,
Σ`d2h_us`, Σ`begin_us`, Σ`reply_us`, Σ`lock_us`, Σ`queue_us`, dmon PCIe GB/s (median/p90).
Derived:
- `transfer_share = (Σbegin_us + Σreply_us + Σd2h_us) / prompt_wall`
- `helper_side = Σlock_us + Σqueue_us` vs engine-observed `Σreply_us` (compute vs transfer split)
- `gain = PP(B)/PP(A) − 1`, and the same at 80k
- `pipelining_recovery = (PP(C) − PP(B)) / (PP(A) − PP(B))`  ← the answer to "does pipelining hide most of it"
- `rows_per_claim`, `fixed_cost_per_claim = (Σbegin_us + Σreply_us)/claims` → cap-amortization verdict
Write `bench/ab-v0210/rpcprof/out/ab9/results.tsv` + `results.md` (with git SHAs, rocminfo/nvidia-smi, model/pack,
flags+env per arm, VRAM, uptime) and a benchmark-tagged memory with qualifiers.

## Window protocol (copied from `ab7_rpc_profile.sh`, hardened)
`stop-prod.sh` → drain until VII VRAM < 2 GiB → kill any 8009 owner → manual pair boot (no supervisor) with
the env exported → readiness poll (health 200) → warmup leg → arms → `stop-prod.sh` →
`relaunch-pair-supervised.sh` → health verify. Every failure path restores prod and exits non-zero.
Wrapper cmdlines must contain NONE of: `launch-prod-`, `rpcw-hip`, `rpcw-cuda`, `/serve/server.py`
(stop-prod's pkills self-match killed three wrappers; banked lesson). No silent sleeps: every poll prints.
Budget from measured bases: pair boot 75–180 s, 80k fresh read ≈190–200 s (~411 tok/s), 8k ≈20–25 s,
engine restart per arm 2.5–3 min ⇒ **≈55–70 min** exclusive window. Prod is NOT swapped; restore supervised.
