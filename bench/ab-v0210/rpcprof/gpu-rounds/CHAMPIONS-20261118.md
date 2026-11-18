# Champions TG/PP comparison — 2026-11-18

Three daily champion stacks, same model, same corpus, one variable. Requested
2026-11-17; the approved latest candidate finished after midnight, so "today" is the
2026-11-18 build and the historical window stays 2026-11-16 / 2026-11-15.

Parent owns GPU execution. Everything in `--prepare` and `--dry-run` is CPU-only: no
endpoint request (not even a health probe), no engine, no process control.

## The three arms

| arm | engine SHA256 (16) | source | adopted config (git blob) |
|---|---|---|---|
| `current` | `8f19baaf150237e5` | `af1e64a5cb4b` (conv-cache-ram-guarantee, oracle APPROVED, 6 CPU tests passed) | `27728dc1ff:prod-config-v0238.json` / `82dbd3adc502` |
| `nov16` | `b0ec0109ebc50c98` | `42cb41b314` | `d2e94b9b08:prod-config-v0238.json` / `35117683a0ee` |
| `nov15` | `36ecbe983837828d` | `81e023bfa0` (lineage v0233.2 + 457a7e95d8) | `fcd1b55325:prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json` / `1b6d2497acce` |

Shared by all three, verified by hash at prepare time: expert helper
`553f3bf48219c992…` (`v0238p0/rpcw-cuda/strata-expert-helper-patient`), serving server
`b6463e7b99a0d74c…` (`v0215/serve/server.py`), model/pack paths, all cache budgets.

**Measured, not assumed:** the three adopted configs differ in *no* quality-bearing key.
Byte-level comparison of the frozen configs shows the only differences are the five
harness retargets (`exe`, two helper sockets, engine log, conversation-cache dir). So the
panel isolates the **engine binary and its lineage** — a TG/PP delta between arms is not
confounded by a config, precision, capacity or cache-budget change. `manifest.json`
records `config_identical_across_arms: true` plus every quality key that was compared.

Historical configs are `git show` snapshots of the adoption commits, not the mutated
on-disk files: the on-disk `prod-config-v0238.json` had already moved the
conversation-cache dir on 2026-11-17.

## Frozen workloads

| shape | prompt bytes | max_tokens | pairs | measured | request deadline |
|---|---|---|---|---|---|
| `warm512` | 512 | 64 | 1 | 0 | 600 s |
| `short16k` | 88 400 | 512 | 4 | 3 | 2400 s |
| `long80k` | 442 400 | 1024 | 4 | 3 | 5400 s |

Arm order `current → nov16 → nov15`; pair 0 of each shape is warm-up and excluded.
One driver call per pair issues the **same prompt twice**: request 0 is the fresh prefill
(PP + TG), request 1 the cached re-run (TG only). Per arm: 9 calls, 18 requests.
Boot budget 1500 s per arm.

The corpus puts the unique identity tag at the **head** of the prompt
(`[champs work=short16k pair=2 ctx=88400]`), so the leading tokens diverge and a fresh
leg really is fresh. The old ab9 tag sat in the tail, where the prefix still matched and
a "prefill" could be served from cache. Prompts are byte-identical across arms and carry
no arm name.

## Commands

Prepare (CPU only, re-runnable, idempotent):

```
cd /mnt/srcds/Strata/bench/ab-v0210/rpcprof
python3 champions_prepare.py --prepare
python3 gpu-rounds/champions_panel.py            # default dry-run, exit 0 = fit to run
python3 tests/test_champions_cpu.py              # 19 CPU tests
```

Run — **parent only**, one arm at a time, in a terminal it can watch:

```
cd /mnt/srcds/Strata/bench/ab-v0210/rpcprof
python3 gpu-rounds/champions_panel.py --run --arm current 2>&1 | tee out/champions-20261118/logs/panel-current.log
python3 gpu-rounds/champions_panel.py --run --arm nov16    2>&1 | tee out/champions-20261118/logs/panel-nov16.log
python3 gpu-rounds/champions_panel.py --run --arm nov15    2>&1 | tee out/champions-20261118/logs/panel-nov15.log
```

`--run` re-verifies every pinned hash and refuses to start an engine on drift.
Omit `--arm` for a single background session over all three.

Progress format: `[HH:MM:SS] arm leg: fresh read N @ X tok/s; cached reused M @ Y tok/s;
walls [...]`, interleaved with NUL-stripped engine lines matching boot/serve/error
patterns. An arm whose timing window contains more than one plausible line prints
`unmatched timing line(s)` and the leg is dropped, never guessed.

Output per arm, written as soon as it finishes:
`out/champions-20261118/summary/<arm>.summary.{json,csv}` (PP/TG medians + spread + n,
actual read/reused/generated, max RSS/VRAM, min MemAvailable, timeouts/fallbacks, every
dropped leg with its reason) and `arms/<arm>/{boot.json,legs.jsonl,records/}`.

## Safety and restore

- `stop-all.sh` is invoked only from `gpu-rounds/champions_stop.sh`; no generated file is
  named `launch-prod-*` / `launch-trial-*` / `*strata-expert-helper*`, so the sweep cannot
  match the harness that triggers it.
- Each arm boots through the **canonical shared** supervisor `--state` / `--lock` paths, so
  the existing `stop-all.sh` still reaches the pair.
- Only the two per-arm socket paths under `out/champions-20261118/run/` are unlinked.
- Boot gate: fresh `ready: http://` line **plus** `/proc/<pid>/exe` resolving to the
  arm's frozen engine **plus** both per-arm sockets present. No date-prefix log glob.
- Restore is in `finally` and on SIGINT/SIGTERM: `gpu-rounds/champions_restore.sh` boots
  `/mnt/srcds/Strata/launch-v0238.sh` — original `prod-config-v0238.json` plus the
  `b0ec` engine — then gates on a ready line and engine identity. A failed restore exits
  non-zero and prints `RESTORE-FAIL`; the panel then appends
  `RESTORE FAILED — production may still be down` to its outcome. It never prints
  `RESTORED` without a verified ready line. `--health` adds the finite-output check.
- Bounded retries: one retry of a pair, and only while the engine is still alive; a dead
  engine aborts the arm and restores rather than poking the OS.
- Historical note: `nov15` predates the #267 wedge fix, so a watchdog abort there is a
  plausible outcome, not a surprise. A hard kernel hang still needs an operator.

## CPU-only validation status

`--prepare`, `--dry-run` and the 19 CPU tests pass. Nothing in this section has been
executed against a GPU: no boot, no request, no production change. The RAM-policy live
acceptance for `current` is still open — the panel captures any headroom-band/limiter
lines into `arms/current/boot.json` as `changed_path_evidence`, which is evidence to read,
not a claim.
