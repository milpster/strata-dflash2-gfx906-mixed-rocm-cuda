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
| Engine | `run/pool-admit-bin/rpcw-hip/strata` (copy of `Strata-wt-helper-ipc/build-906-fixadmit/strata`) | sha256 `a506e62008ba...`, worktree `helper-ipc-diet` @ `9358921670` |
| Engine rollback pin | `run/winner-pin-38g/strata-engine` | sha256 `d534e72a7369...` (`build-906/strata`, untouched) |
| Helper | `run/trial-managed-bin/rpcw-cuda/strata-expert-helper-patient` | sha256 `553f3bf48219...` (v0233 pin) |
| Winner fallback | `run/winner-pin-38g/strata-engine` | byte copy of the engine that ran the 38 GiB row |
| Pack | `/mnt/srcds/ai/iq3xxs-ud` | UD, 48 layers x 512 experts, `native_experts.txt` v4 (gu 22 / d 20) |
| Helper shard | `--experts ../Qwen3.8-Flash-Next-UD-IQ3_XXS-00002-of-00004.gguf` | the shard verified byte-equal to the UD `experts.bin` planes |

## The knobs that make it the champion

| Knob | Value | Why |
|---|---|---|
| `STRATA_RAM_BUDGET_MIB` | 43008 | 38 GiB pool + 4 GiB outside area |
| RAM pool store | `pool_bytes=40802189312` | must appear in the boot lines |
| `--vram-reserve-mib` (CUDA0) | **640, provisional** | raised 384→448→512→576→640 on 2026-11-21 on a misread failure: `refilling a lent slot failed: No buffer space available` is errno ENOBUFS from the RAM pool (`ram_pool.cpp` pool_alloc), not a card out of VRAM, and the refill loop takes the store directly. No margin step was ever justified by it, and a larger margin shrinks the GPU expert cache, which sends more pairs to the pool. It is kept because two clean legs passed on it; the measurement back down is queued for the operator, not taken unilaterally |
| `--vram-reserve-later-mib` (CUDA1) | 192 | never ran low |
| `--expert-helper-slots` | 4600 | the engine's hello is authoritative; the helper's `--slots` is inert |
| helper `--reserve-mib` | 0 | the component clamps it to 64 |
| helper `--fill-mib` | 7400 | fills 3,785 slots / 7.09 GiB, `fill stopped: budget`, 250 MiB left |
| `--expert-helper-relief-hot-mib` / `-arena-cap-mib` | 2048 / 4096 | host area the helper relief may hold |
| `--kv-resident-mib` | 4096 | 12,864 B per resident cell over 12 QSA layers, 262,144 cells = 3.1 GiB |
| `--max-context` | 262144 | the context every leg must fit in |

## Known behaviour to expect

- Boot identity: `RAM pool store: pool_bytes=40802189312 ... usable_bytes=40799522816`.
- CUDA0 expert cache 4,951 slots (margin 256) or 4,917 (margin 320); at margin 640 it is smaller
  again. CUDA1 4,174 slots.
- "VRAM free with everything loaded": 284 MiB at 256, 348 MiB at 320.
- **The three failure classes, with the text that identifies each** (2026-11-21; the second was
  mislabelled "VRAM" for a whole night):
  | Text | Class | Answer |
  |---|---|---|
  | `the expert source could not produce a blob` | host pool admission | code (a lease that was never given back), never a margin |
  | `refilling a lent slot failed: No buffer space available` | host pool refill — ENOBUFS only comes from `ram_pool.cpp`/the pool cache; the refill loop takes `g_pool_expert_store->acquire()` directly | code, never a margin |
  | `exit code -6` / SIGABRT | engine abort (OOM hint in the harness text) | separate investigation |
- The pool's own evidence line at a refusal:
  `managed pool: borrow refused layer L expert E: borrowers=N completed=N pending=N
  resident_pairs=N evictions=N`. `borrowers` near the pair count with `completed=0 pending=0` is a
  pin leak in the engine. `STRATA_POOL_BORROW_TRACE=1` prints the borrowing caller's frames.
- A request-level pool failure currently ends the **engine process** (`refill_issue` prints and
  returns 1), so one bad request takes every concurrent request with it. That is a defect of its
  own and is on the backlog; the fix above removes the trigger, not the amplification.

## How a new window starts

1. Derive the config from the champion with `bench/ab-v0210/rpcprof/make-cold-config.py`,
   which writes a fresh empty conversation-cache dir and a per-window elog.
2. Boot with `bench/ab-v0210/rpcprof/launch-trial-managed.sh` and the env it documents
   (`STRATA_RAM_BUDGET_MIB=43008 STRATA_HELPER_RESERVE=0 STRATA_FILL_MIB=7400`). The 8019
   trial port is retired: tests run on 8009, one at a time.
3. Do not run prod and a window at the same time: both use 8009, the launcher refuses.
4. Record numbers only from the engine's own lines, with the memory cost beside them.

## Stale, do not start from these

`launch-prod-swift15-*`, `launch-prod-swift-iq3*`, `launch-prod-arena54.sh`,
`launch-trial-iq3s.sh`, `launch-trial-r7.sh`, `launch-trial-spill40.sh` pin older builds
(v0219, v0222, v0233) or older helper reserves (156, 300, 512) and the retired 8019 split.
They are kept for history. Anything new derives from the champion instead.
