# Champion configuration

Everything new starts here. A window, a trial, a baseline or a fix is derived from this
configuration; nothing starts from an older build, an older pack or an older launcher.
If a lane needs a different setting, it changes one thing on top of this and says so.

Last updated 2026-11-21. Supersedes every earlier "winner", "champion" or launcher default.

## Resolved 2026-11-21 23:59: the 240k non-finite logits (`#879`)

**Cause.** `STRATA_FS_SLOTS="2,8"` used to mean two slots on every card (`std::atoi`). The merge reads it as a
per-card list, so the thin-VRAM card got 8 slots (~293 MiB). That allocation happens after the expert-cache fill
and outside every reserve calculation, so CUDA1 loaded with 140 MiB free instead of 360. Below about 150 MiB free
the first stage-2 GEMM emits scattered non-finite outputs and the request is refused. The same starvation is what
stalled a 240k request at split 31 (156 MiB free, the full-card stall `#2065`).

| Fix | Artifact | 3,058 tokens | 239,663 tokens |
|---|---|---|---|
| Config, no rebuild | `prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed-fs2.json` (`STRATA_FS_SLOTS=2`, margins untouched at 256/192) | pass, 360 MiB free | pass, 814.6 s at 294.2 tok/s |
| Code, `564e1dbbea` | `run/fsclamp-bin`, engine `4b8251e9e06334a8` | pass, 434 MiB free, swap clamped off | pass, 834.8 s at 287.1 tok/s |

Both produce 0 non-finite lines and the same completion the branch engine produced at the same length. Nothing is
promoted: prod's config and binary stay as they are until the operator decides. Full record:
`journal/pool-admission-fix-2026-11-21.md`.

## What is serving

| Part | Path | Identity |
|---|---|---|
**Nothing is serving as of 2026-11-21 17:15** — prod was stopped, and no tuple is crowned for both
workloads, because today's measurements split them:

| Tuple | Engine | Helper | Budget | Proven on |
|---|---|---|---|---|
| A — 240k-proven | `run/trial-managed-bin/rpcw-hip/strata` = **`d534e72a7369fe5d`** | **`553f3bf48219c992`**, reserve 64, fill 7400 | 40,960 (`pool_bytes=38654705664`) | 239,663-token prompt: read in ~1,102 s (≈218 tok/s), decoded, **0 non-finite** |
| B — agent-proven | `run/pool-admit-bin/rpcw-hip/strata` = **`5a699be15a515adc`** | **`9d16742d5f30e9c1`**, reserve 0, fill 0 | 36,864 (`pool_bytes=34359738368`) | a 138-request agent session, 100,291-token prompt, `timeouts=0 fallbacks=0`; **fails every 240k prompt** |

| Part | Path | Identity |
|---|---|---|
| Prod launcher | `launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh` | supervisor boot, 127.0.0.1:8009; `STRATA_PROD_BIN`, `STRATA_PROD_CONFIG`, `STRATA_PROD_HELPER_RESERVE_MIB`, `STRATA_PROD_HELPER_FILL_MIB` override the tuple without editing it |
| Prod config | `prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json` | tuple B (engine `5a699be1`, budget 36,864) |
| 240k test config | `bench/ab-v0210/rpcprof/champion38g-prod-test.json` | tuple A on the prod port and sockets |
| Bisect point | `bench/ab-v0210/rpcprof/pool-a506-prod-test.json` | engine `a506e62008ba...` (end of the pool-admission suite), no verdict |
| Pack | `/mnt/srcds/ai/iq3xxs-ud` | UD, 48 layers x 512 experts, `native_experts.txt` v4 (gu 22 / d 20) |
| Helper shard | `--experts ../Qwen3.8-Flash-Next-UD-IQ3_XXS-00002-of-00004.gguf` | the shard verified byte-equal to the UD `experts.bin` planes |

## 2026-11-21: the 240k regression (open, blocks promoting tuple B for long prompts)

Measured on the same 239,663-token prompt, same port, `max_tokens 8`:

| Setting changed | Result |
|---|---|
| tuple A, budget 40,960, CUDA0 256 | prompt read, `done:`, **0 non-finite logits** |
| tuple B, budget 36,864, CUDA0 256 | `non-finite logits (#879) in a prompt window at position 239658 (row 0)`, 4 of 4 passes |
| tuple B, CUDA0 **704** (730 MiB free at request start) | still non-finite → VRAM is not the cause |
| tuple B, second pass reading the whole prompt through the batched path (`0 reused`) | still non-finite → the prompt path is not the cause |

The helper is not the source: the first failing pass reported `helper=reqs=0 rows=0 bytes=0`. The window is the
seven-commit pool-admission suite between `d534e72a` (built 23:47, before `1ae83e157d`) and `5a699be1` (after
`a2303f4021`); `a506e620` (end of that suite) is the zero-build bisect point and its leg was stopped at 81,920 of
239,663 tokens with no verdict. `STRATA_DBG_NAN=1` localizes a bad chunk but costs a 4× slower prefill
(65 tok/s vs 150–260), which is unaffordable at 240k.

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
