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
python3 tests/test_champions_cpu.py              # 34 CPU tests
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

Progress format: `[HH:MM:SS] arm leg: ctx N tok, fresh read N @ X tok/s; cached reused M @
Y tok/s; walls [...]`, with the engine, boot, helper and supervisor logs streamed
continuously **during** each request (NUL-stripped, filtered to boot/timing/DONE/error
patterns), not only during boot. Generated text is never matched, so completions cannot
spew; numeric checks read engine counters only, never reply text. An arm whose window
holds more than one plausible line prints the count of continuation-shaped lines excluded,
and the main pair is matched by context and reuse counts, never by last grep hit.

Driver stdout/stderr for every attempt goes to `arms/<arm>/driver/<leg>.out` and is
printed on a non-zero exit.

Completeness is enforced, not implied. A pair is complete only if both timing lines
matched, every number is finite, the fresh and cached legs agree on context tokens, the
prompt digest matches the corpus, and both responses carried text. The first incomplete
pair ends the arm: exact reasons are printed, partial evidence is summarised, and the run
reports failure. `summary/<arm>.summary.json` carries `complete`, `pairs_complete` and
`pairs_planned`; zero or missing samples can never read as success and no fallback number
is invented.

**No retry exists.** Killing the client does not cancel work the server already accepted,
so a second attempt while the first is still executing would overlap benchmark traffic and
corrupt the number being measured. Any timeout, non-zero exit, missing record or unusable
pair aborts the arm, then a controlled stop and restore.

Output per arm, written as soon as it finishes:
`out/champions-20261118/summary/<arm>.summary.{json,csv}` (PP/TG medians + spread + n,
actual context tokens, read/reused/generated, max RSS/VRAM, min MemAvailable,
timeouts/fallbacks, every dropped leg with its reason) and
`arms/<arm>/{boot.json,legs.jsonl,records/,driver/}`.

Evidence is never reused or destroyed. A rerun renames the previous `arms/<arm>`, engine
log and summary into `out/champions-20261118/archive/…`, writes `supervisor.out` fresh
(truncate, not append) so a stale announce line cannot satisfy the boot gate, and starts
`legs.jsonl` empty so old samples cannot inflate `n`. The per-arm conversation cache stays
isolated and is never deleted, so a rerun may reuse it — `boot.json` records the cache dir
in use.

## Safety and restore

- `stop-all.sh` is invoked only from `gpu-rounds/champions_stop.sh`; no generated file is
  named `launch-prod-*` / `launch-trial-*` / `*strata-expert-helper*`, so the sweep cannot
  match the harness that triggers it.
- A start needs stop-all to be **clean**: zero exit *and* the `ALL STOPPED` verdict. Either
  alone passing is not enough to boot another pair.
- Each arm boots through the **canonical shared** supervisor `--state` / `--lock` paths, so
  the existing `stop-all.sh` still reaches the pair.
- Only the two per-arm socket paths under `out/champions-20261118/run/` are unlinked.
- Boot gate: fresh `ready: http://` line **plus** `/proc/<pid>/exe` resolving to the
  arm's frozen engine **plus** both per-arm sockets present. No date-prefix log glob.
- Restore runs in `finally` and on SIGINT/SIGTERM. `gpu-rounds/champions_restore.sh` first
  requires a clean stop (same zero-exit **and** `ALL STOPPED` rule) and refuses to launch
  another pair otherwise. It then boots `/mnt/srcds/Strata/launch-v0238.sh` — original
  `prod-config-v0238.json` plus the `b0ec` engine, never an old launcher — gates on a ready
  line, a `/v1/models` answer, and engine identity. With `--health` it also requires a
  non-empty finite generation (text present, `finish_reason` present, completion tokens > 0)
  **before** printing `RESTORED`; without that it prints `RESTORE-FAIL` and exits non-zero.
  It never prints `RESTORED` on a ready line alone.
- Bounded deadlines only: boot 1500 s, per-shape request deadlines as tabled. No OS or GPU
  reset attempts.
- Historical note: `nov15` predates the #267 wedge fix, so a watchdog abort there is a
  plausible outcome, not a surprise. A hard kernel hang still needs an operator.

## CPU-only validation status

`--prepare`, `--verify`, `--dry-run` and the 34 CPU tests in
`tests/test_champions_cpu.py` pass. The execution tests drive the real
`run_pair` / `run_arm` / `collect` / `stop_all` code with fake children and a sandboxed
copy of the restore script, and they assert: driver failure propagates and is not retried,
a timeout kills without relaunching, the first unusable pair aborts the arm, zero samples
cannot report success, a failed stop prevents any boot, stale announce output and old legs
are archived rather than counted, the engine log is pumped while a child is in flight, and
the restore log-path parse stops at the launcher's `;` separator.

**Test isolation contract — read this before editing the tests.** Once an arm has run,
`out/champions-20261118/` holds the only copy of those measurements, so the suite is not
allowed to write there. Every mutable fixture lives under `out/tests-tmp/` (git-ignored,
removed per test): the panel gets a scratch manifest whose `engine_log` points inside the
scratch tree, so its `arms/`, `summary/` and `archive/` land there too. Read-only inputs
(corpus bytes, frozen launchers and configs, `--verify`) may be opened from the real tree.
`SuiteLeavesBenchmarkDataUntouched` snapshots stat + SHA256 of the real evidence at import
and re-checks it last, so a test that writes, renames or deletes a finished arm fails.
An earlier version of this file constructed its panel on the real tree and would have
destroyed a completed arm on rerun — that is what the guard exists for.

Nothing here has been executed against a GPU: no boot, no request, no production change.
The RAM-policy live acceptance for `current` is still open — the panel captures any
headroom-band/limiter lines into `arms/current/boot.json` as `changed_path_evidence`,
which is evidence to read, not a claim.
