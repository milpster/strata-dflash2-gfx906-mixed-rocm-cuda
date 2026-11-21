---
name: strata-tuning
description: Tune, measure and serve Strata on this box (2x Radeon VII gfx906 + RTX 3080 helper). Use when changing VRAM margins, RAM pool budgets, helper fills, launching a measurement window, judging a speed claim, or deciding which model a subagent lane runs on. Points at CHAMPION.md/CHAMPION.json and the operator directives; do not start from an older build or launcher.
---

# Strata tuning on GUAM

Everything here was set by the operator or measured on this box. When a rule conflicts with
convenience, the rule wins. Numbers come from the engine's own lines, never from a client.

## 1. Start from the champion, never from a stale build

- Read `CHAMPION.md` and `CHAMPION.json` in the repo root. They name the launcher, the config,
  the engine sha, the helper sha, the pack and every knob.
- Prod: `launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh` + `prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json`.
- Measurement: `bench/ab-v0210/rpcprof/launch-trial-managed.sh` +
  `trial-managed-pool-38g-230k.json`, fresh cache dir through `make-cold-config.py`.
- Launchers that open with a `STALE 2026-11-21` banner pin v0219/v0222/v0233 builds or old helper
  reserves. They exist for history; a new window, trial, baseline or fix derives from the champion.

## 2. The machine does not bend

- gfx906 links and runs against the ROCm **6.1.0** userspace, never `/opt/rocm` (7.x):
  `PATH=/opt/rocm-6.1.0/bin cmake -S . -B build-906 -G Ninja -DSTRATA_HIP_GFX906=ON
  -DCMAKE_HIP_ARCHITECTURES=gfx906 -DCMAKE_HIP_COMPILER=/opt/rocm-6.1.0/llvm/bin/clang++
  -DCMAKE_PREFIX_PATH=/opt/rocm-6.1.0`.
- Runtime: `LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib`,
  `HSA_OVERRIDE_GFX_VERSION=9.0.6`. The real xnack tree is `/home/srcds/rocm-gfx906-xnack`.
- Card names in the engine: `CUDA0` = Radeon VII #0 (layers 0-29), `CUDA1` = Radeon VII #1
  (30-47), the RTX 3080 is the expert helper. CUDA0 is the card that runs out; CUDA1 never has.
- Stage threads stay on `--stage-cpus 3,5,7,9,11,13,15`.

## 3. Margins and pools

- Change a margin only as directed. The standing rule: **+64 MiB on the card that actually
  exhausted VRAM, one step per real failure**. Thin-but-working is not exhaustion.
  CUDA0 `--vram-reserve-mib`, CUDA1 `--vram-reserve-later-mib`, helper `--reserve-mib`
  (clamped 64..4096; 0 means 64 and the fill stops on `budget`).
- Current: pool 38 GiB (`STRATA_RAM_BUDGET_MIB=43008` -> `pool_bytes=40802189312`), CUDA0 320 on
  prod / 256 as measured, CUDA1 192, helper reserve 0, fill 7400, slots 4600.
- A margin change costs cache: +64 MiB on CUDA0 bought 348 instead of 284 MiB free and cost 34
  slot pairs (4,951 -> 4,917). Say both numbers.
- Margins are edited in the config's `args`, never in a running process.

## 4. Lanes: which model runs a subagent

- **One active subagent at a time.** Never two.
- Work that can run while prod serves (code, tests, builds, docs) goes to **swift**
  (`llamacpp/swift-1.5-iq3_xxs`), which the engine on `:8009` serves.
- Work that needs prod down (stop, swap, boot, probe, rollback) goes to **deepseek**
  (`tokenharbor/deepseek-v4.1-flash-1017`). A swift lane dies with the engine it thinks on.
- Only the parent stops or starts prod, and only between subagent turns. A lane never
  restarts the pair; if a step would take prod down it stops and reports.
- Before spawning a swift lane, probe prod with a real completion, not a health check.

## 5. Measure so the claim survives

- One window at a time; prod and a window never run together (both use 8009; the launcher refuses).
- Cold proof is `prompt N tokens = 0 reused + N read in T ms (R tok/s)`. The same line carries
  `helper=reqs=... rows=... bytes=... timeouts=... fallbacks=...`.
- Never report a speed number without its memory cost: MemAvailable start/min/end, swap
  start/peak/end, VmLck, VmRSS, per-card VRAM free, helper counters.
- Boot identity to match before trusting a leg: `RAM pool store: pool_bytes=...`, the expert
  cache line per card, the `--kv-resident-mib 4096` line, and the helper's
  `fill preflight` / `filled N slots` / `fill stopped:`.
- Compare legs on the same pack. The helper must read the UD shard
  (`--experts ../Qwen3.8-Flash-Next-UD-IQ3_XXS-00002-of-00004.gguf`); prove it from
  `/proc/<helper>/maps` and from the fill arithmetic (3,785 slots = 7.09 GiB of UD blobs;
  the old container's 1,746,133-byte blobs would have filled ~4,300).
- Elogs are `logs/engine-*.log` and contain null bytes: always `tr -d '\0'`.

## 6. Teardown and typing

- Teardown is `./stop-all.sh` in its own invocation, then `bench/ab-v0210/rpcprof/check-quiet.sh`
  separately. Supervisors respawn their pair forever: stop them through their state files
  (`run/*-supervisor.json`) before sweeping.
- Never `pkill -f` / `pgrep -f` from an inline command: the pattern sits in the command's own
  cmdline and it kills itself.
- Boot and stop are separate invocations; never one command that does both.

## 7. Land the work

- Journal every window in `journal/managed-ram-pool-2026-11-20.md` with verbatim lines.
- Commit incrementally, with an area tag in the subject (`[Engine]`, `[Benchmark]`, `[Docs]`).
- Specialists never push; the parent pushes to `fork`. Evidence lives under
  `bench/ab-v0210/rpcprof/out/<window>/`, never in `/tmp`; conversation-cache `.sess` files are
  gitignored and never committed.
- The pinned winner binaries live in `run/winner-pin-38g/` with `SHA256SUMS`; restore them before
  trusting a rebuilt engine.
