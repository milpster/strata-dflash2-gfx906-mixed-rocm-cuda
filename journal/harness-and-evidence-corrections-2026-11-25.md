# Harness and evidence corrections, 2026-11-25 (post-reboot session)

**Scope.** After the reboot that followed the fault-fix promotion (`b7d46f9f80`), this session
was meant to finish W4 cells 3–4 and the W3s span pair. Prerequisite 0 refused every boot, so
the GPU lane never opened. What did happen is a set of corrections that matter more than the
missing cells: three of this session's own claims were wrong and are retracted here, and four
harness defects that produced or hid bad evidence are fixed.

## Why no window opened

| cause | reading | waivable |
|---|---|---|
| a Vorta/borg backup (`/usr/bin/borg create`, pid 66182) | ran 127+ min at ~40 % CPU, RSS 1.8–2.1 GiB | no |
| the agent host's MCP python (pid 132596) | 2.03 GiB, over the per-process gate | **no** — that gate is never waived |
| a game (`Conqueror's Blade`) later in the session | non-engine RSS reached 21.9 GiB | no |

The per-process 2 GiB gate is the structural blocker: **while the agent host is running, no
measurement window can open on this box.** Stopping it is an operator decision, not a harness
one. Everything below was done without a GPU.

## Retractions (all mine, all this session)

| stated | actual |
|---|---|
| "the −4.7 % 144k gap is an engine effect, mmq `built=1` vs `built=0` is the lead" | **both arms run mmq off** — `prefill mmq: off (env=0) [built=1]` and `[built=0]`; `built()` is compile-time and B's lineage returns `false` (`engine/src/prefill/mmq_absent.cpp:19`). The flag cannot affect timing |
| "B-engine + A-server ran 285.7, so both cross cells are slower than both native cells" | `boots/W4-B-dec-r1.json` says `arm B, server_arm B`: a **native B control booted under a cross-cell key**, because `--server-arm` was never passed. Only **one** cross cell exists |
| "B's reply is not parked, so A pays a disk restore — that is the −48 % inter-turn" | **both arms park and both evict**; B's "not parked" line concerns a *superseded reply*. And no W2 engine log contains a conversation-cache **restore** line at all (`grep -c` = 0 across twelve logs), so the restore story has no evidence |

Also corrected: the `pp239k` corpus serves **58,423 tokens**, not 239k — it is a byte-name, so
that n=3 sample is at a *shorter* context than the 144k leg, not longer.

## What the eight cold 144k legs actually show

Rates are the engine's own `prompt 143893 tokens = 0 reused + 143893 read` figures, with each
leg's recorded memory.

| leg | engine+server | tok/s | max swap MiB |
|---|---|---|---|
| W2-A-dec-r1 | A+A | 322.4 | 2,968 |
| W2-A-dec-r2 | A+A | 318.9 | 3,372 |
| W2-B-dec-r2 | B+B | 312.5 | 3,599 |
| W2-B-dec-r1 | B+B | 298.7 | **8,011** |
| W4-A-dec-r1 | A+**B** | 279.2 | **11,724** |
| W4-B-dec-r1 | B+B | 285.7 | **13,808** |

`r(swap, tok/s) = −0.90`; under 9 GiB swap 311.3 tok/s (n=6), at/over it 282.4 (n=2).
**Caveat:** both high-swap legs are the later-session W4 legs, so swap and session are
collinear and this cannot separate them. What it does establish is negative and sufficient:
**the 144k spread is not explained by the engine**, the arms overlap inside the low-swap band,
and A's own spread (1.1 %) is narrower than B's (4.5 %) which exceeds the 4.7 % delta.

## Harness defects fixed (each was producing or hiding invalid evidence)

| file | defect | consequence if unfixed |
|---|---|---|
| `bench/ab-v0210/rpcprof/port_ab_prereq0.py` | `ps -eLo` lists the gate's **own child** at 300 % (`%CPU` is cputime/elapsed, unbounded as elapsed→0) | random refusals, ~1 in 15 samples, indistinguishable from a real hog |
| `bench/fault-va/w4_finish.sh` | no prerequisite check, no lock, no identity assertion, no coldness assertion | an hour looping refused boots while logging "boot <cell>"; a stale launcher could serve the wrong tuple; a resumed checkpoint counted as a cold leg |
| `bench/fault-va/w4_finish.sh` | `--server-arm` never passed | **cross cells booted as native controls** — the defect that produced the wrong W4 conclusion above |
| `bench/fault-va/prod_soak.sh` | no `--prompt-file`; corpus per ctx not per leg | every soak leg was warm while labelled a cold prefill; legs 1 and 3 sent identical bytes (`49152 reused`) |
| `bench/ab-v0210/rpcprof/port_ab_legs.py` | `--check` overwrote `boots/<key>.evidence.json` with **current** box state | on 2026-11-25T00:51 one `--check` replaced all twelve W2 evidence files: `cached_delta_mib` 6138 → 3333, `majfault_delta` 3452753 → 9787947, `written_at` moved five hours forward. Restored from git; `write_boot_evidence` now keeps an existing file unless `--refresh-evidence` |

The soak fix was validated against real records rather than by inspection: `--check-leg` returns
**0** on the genuine cold `W2-A-dec-r1/A-dec144k.json` and **1** with `warm: reused=49152` on the
genuine broken-soak record `prod-soak-n2.json`, reproducing the original defect from the record
alone.

## Two W4 boots are not what they were published as

| boot | what its record says | what was published | consequence |
|---|---|---|---|
| `W4-A-dec-r1` | `arm A`, `server_arm B` | "A-engine + B-server, 279.2 tok/s" | correct — a genuine cross cell |
| `W4-B-dec-r1` | `arm B`, `server_arm B` | "B-engine + A-server, 285.7 tok/s" | **wrong** — a native B control under a cross-cell key |

Both boot records are committed (they were untracked) so the mistake stays auditable. Their
`*.evidence.json` files are **not** committed and were moved to
`bench/fault-va/out/w4-evidence-stale-20261125/` with a README: one was written 27 min after
its boot ended and the other 5 min **before** its boot started, so neither carries boot-time
page-cache readings. The rates stand (they come from the engine's own judge line), but those
two boots are excluded from any claim needing a clean kernel fingerprint.

## The measurement rank 1 is a flag, not a build

The 6.9 s vs 3.6 s re-read gap looked like it needed new engine instrumentation. It does not:
`STRATA_TRACE` already prints a per-admission decomposition
(`src/program/generate.cpp:23493-23506`) — drain, prompt (read, windows, K checkpoints
= sync + save + evict, rest), first window, slot copy, slot freed, checkpoint copy. No W2 boot
had it set (`grep -c 'strata trace:'` = 0 in all twelve logs), which is the only reason the gap
is unattributed. One ttft3 pair with the flag names the term for two boots and no build.

## Test suite: 5 failures are pre-existing

`python3 -m pytest bench/ab-v0210/rpcprof/tests` gives **5 failed, 257 passed**. All five are in
`test_reserve_panel_cpu.py` and all are the same frozen-profile drift
(`prod-config-v0238.json on disk is 316e6128…, not the baseline b221140f…`), already recorded in
`journal/port-ab-w3p-prodshape-tg-2026-11-24.md:116`. Reproduced unchanged at the session-start
commit `b7d46f9f80` in a throwaway worktree (**5 failed, 51 passed**), so they are not from this
session's changes. The two suites that import the changed modules
(`test_port_ab_prodshape_cpu.py`, `test_port_ab_w4_read_metric_cpu.py`) pass **27/27**.

## Commits

| commit | what |
|---|---|
| `93f88ecebb` | prereq gate: a thread too young for %CPU to be a rate is not a hog |
| `1d3c06deac` | w4_finish and prod_soak refuse to run what they cannot verify |
| `0244af5714` | prefill static diff: mmq off in both arms; kernel class and trim default differ |
| `231aabee88` | optimisation report: container/session state, not the engine, explains the spread |
| `04d37d313e` | soak: one corpus per leg, warm legs marked instead of fatal |
| `f3ae1a25dd` | w4_finish: derive the server arm, prove the tuple that answers the port |
| `563b04a0d4` / `f0174ab159` | prereq gate: CPU by consumption + a second sample; a listing it could not take is not a quiet box |
| `a2d4b7a960` | `--check` must not rewrite a finished window's evidence |
| `5f631b6118` | record the two W4 boots that ran, with the mis-keyed one kept visible |
| `20afcbe323` | w4_finish: resolve the serving pid by exact exe path, not name prefix |
| `17410aea01` | report: the phase decomposition already exists behind STRATA_TRACE |

## Open, and who owns it

| item | owner |
|---|---|
| W4 cells 3–4 **with `--server-arm`**, W3s span pair | needs a window → operator must free the 2 GiB per-process gate (stop the agent host) and the backup/game |
| instrument the post-eviction 99-token continuation (6.9 s vs 3.6 s) | engine work, unstarted |
| a `max_tokens 2048` leg so a legitimate TG rate exists at all (6 of 12 W2 legs yielded none) | needs a window |
| interleaved 144k noise floor in one session (4–5 legs per arm, swap recorded) | needs a window |

**Honest status:** no speed claim in this session is stronger than a direction, and the ones
that were stronger have been retracted. What is solid is the harness: the four gates now refuse
what they cannot verify, and the report says which measurements would actually decide anything.
