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

The soak fix was validated against real records rather than by inspection: `--check-leg` returns
**0** on the genuine cold `W2-A-dec-r1/A-dec144k.json` and **1** with `warm: reused=49152` on the
genuine broken-soak record `prod-soak-n2.json`, reproducing the original defect from the record
alone.

## Commits

| commit | what |
|---|---|
| `93f88ecebb` | prereq gate: a thread too young for %CPU to be a rate is not a hog |
| `1d3c06deac` | w4_finish and prod_soak refuse to run what they cannot verify |
| `0244af5714` | prefill static diff: mmq off in both arms; kernel class and trim default differ |
| `231aabee88` | optimisation report: container/session state, not the engine, explains the spread |
| `04d37d313e` | soak: one corpus per leg, warm legs marked instead of fatal |

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
