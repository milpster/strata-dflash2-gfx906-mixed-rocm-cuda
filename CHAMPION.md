# Champion configuration

Everything new starts here. A window, a trial, a baseline or a fix is derived from this
configuration; nothing starts from an older build, an older pack or an older launcher.
If a lane needs a different setting, it changes one thing on top of this and says so.

Last updated 2026-11-21. Supersedes every earlier "winner", "champion" or launcher default.

## What is serving

| Part | Path | Identity |
|---|---|---|
| Prod launcher | `launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh` | supervisor boot, 127.0.0.1:8009 |
| Prod config | `prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json` | see knobs below |
| Engine | `run/trial-managed-bin/rpcw-hip/strata` → `build-906/strata` | sha256 `d534e72a7369...` |
| Helper | `run/trial-managed-bin/rpcw-cuda/strata-expert-helper-patient` | sha256 `553f3bf48219...` (v0233 pin) |
| Winner fallback | `run/winner-pin-38g/strata-engine` | byte copy of the engine that ran the 38 GiB row |
| Pack | `/mnt/srcds/ai/iq3xxs-ud` | UD, 48 layers x 512 experts, `native_experts.txt` v4 (gu 22 / d 20) |
| Helper shard | `--experts ../Qwen3.8-Flash-Next-UD-IQ3_XXS-00002-of-00004.gguf` | the shard verified byte-equal to the UD `experts.bin` planes |

## The knobs that make it the champion

| Knob | Value | Why |
|---|---|---|
| `STRATA_RAM_BUDGET_MIB` | 43008 | 38 GiB pool + 4 GiB outside area |
| RAM pool store | `pool_bytes=40802189312` | must appear in the boot lines |
| `--vram-reserve-mib` (CUDA0) | 256 measured, **320 on prod** | one +64 step per real exhaustion (40 GiB run died at 0 MiB, prod died at 127 MiB free) |
| `--vram-reserve-later-mib` (CUDA1) | 192 | never ran low |
| `--expert-helper-slots` | 4600 | the engine's hello is authoritative; the helper's `--slots` is inert |
| helper `--reserve-mib` | 0 | the component clamps it to 64 |
| helper `--fill-mib` | 7400 | fills 3,785 slots / 7.09 GiB, `fill stopped: budget`, 250 MiB left |
| `--expert-helper-relief-hot-mib` / `-arena-cap-mib` | 2048 / 4096 | host area the helper relief may hold |
| `--kv-resident-mib` | 4096 | 12,864 B per resident cell over 12 QSA layers, 262,144 cells = 3.1 GiB |
| `--max-context` | 262144 | the context every leg must fit in |

## Known behaviour to expect

- Boot identity: `RAM pool store: pool_bytes=40802189312 ... usable_bytes=40799522816`.
- CUDA0 expert cache 4,951 slots (margin 256) or 4,917 (margin 320), CUDA1 4,174 slots.
- "VRAM free with everything loaded": 284 MiB at 256, 348 MiB at 320.
- The pool can refuse a blob under transient pressure (`the expert source could not produce
  a blob`) and the engine can fail a lent-slot refill (`No buffer space available`); both
  are measured failures of this configuration at 230k context, not of an older build.

## How a new window starts

1. Derive the config from the champion with `bench/ab-v0210/rpcprof/make-cold-config.py`,
   which writes a fresh empty conversation-cache dir and a per-window elog.
2. Boot with `bench/ab-v0210/rpcprof/launch-trial-managed.sh` and the env it documents
   (`STRATA_RAM_BUDGET_MIB=43008 STRATA_HELPER_RESERVE=0 STRATA_FILL_MIB=7400`).
3. Do not run prod and a window at the same time: both use 8009, the launcher refuses.
4. Record numbers only from the engine's own lines, with the memory cost beside them.

## Stale, do not start from these

`launch-prod-swift15-*`, `launch-prod-swift-iq3*`, `launch-prod-arena54.sh`,
`launch-trial-iq3s.sh`, `launch-trial-r7.sh`, `launch-trial-spill40.sh` pin older builds
(v0219, v0222, v0233) or older helper reserves (156, 300, 512) and the retired 8019 split.
They are kept for history. Anything new derives from the champion instead.
