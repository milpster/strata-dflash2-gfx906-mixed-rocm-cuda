# Measurement contract: status by phase — 2026-11-24

Status page for the approved plan `/home/srcds/.opencode/plan/measurement-contract-speed-attribution-faults.md`
(oracle-approved, 5 rounds, 2026-11-23), branch `recovery-startup-watchdog`. Fixer lane: no boot, no GPU work, no
process control, no push, nothing written under `bench/ab-v0210/rpcprof/out/`. **Not measured** means no artifact
exists; it never means "measured and found equal". A *claim leg* is a boot whose record enters a window's sample, a
*warmup* is a discarded boot before it, *descriptive only* means rates and ranges without a confirm /
no-difference / within-delta statement.

| Phase | What the plan requires | State |
|---|---|---|
| 1 Correct the record | Fix the wrong tok/s figure and the harness cells | **Done**, oracle-approved (`2aae16d206`, `87437ea609`) |
| 2 The windows | W1 prefill, W2 short-PP/TTFT/144k decode, W3 240k decode, W4 2x2 | **W1 ran**, descriptive only; **W2, W3, W4 not run** |
| 3 Attribution | Three levers, one variable per boot | **Not started**; gated on W4 |
| 4 Fault track | Minimal repro, shim, then one-variable configs | **Outcome differs**: cause found another way and fixed; part of the track is moot |

## Phase 1 — correct the record: done

The plan requires the journal and `CHAMPION.md` to say that 356.3 tok/s was a truncated pass, that the
239,663-token pair is the only same-prompt pair, and that the harness cells are reconciled or deleted. Done and
oracle-approved: `2aae16d206` (356.3 tok/s = the 182,180-token truncated pass; fork 348.2 vs port 347.0, one run
each, unmatched fields named) and `87437ea609` (review notes). Pointer:
`journal/pool-admission-fix-2026-11-21.md`, "Correction 2026-11-23 (Phase 1)" (line 1319), plus the same change in
`CHAMPION.md`. No figure there carries a claim now.

## Phase 2, W1 — prefill rate: ran, descriptive only

The plan requires the frozen head-tagged corpus (~323,200 B), `max_tokens` 8, n=2 claim boots per arm interleaved
A-B-B-A plus one discarded warmup each, a 5 % delta pre-registered, and gates on 0 reused tokens, equal served
prompt-N, no faulted or restarted boot. The 8-token tail is context only, never a TG rate (floor: 1,000 generated
tokens). Measured 2026-11-23 evening, 6 boots (4 claim, 2 warmup); the corpus served **58,423 tokens**, not the
239,663 the generator estimated, and every leg read 0 reused tokens and the same prompt sha (`210b16b3…`).

| Leg | Leg ended | Read wall | Prefill rate | 8-token tail | Drafts | Helper reqs/rows | Min avail MiB | Max swap MiB |
|---|---|---|---|---|---|---|---|---|
| A-r1 (claim) | 22:37 | 200,883 ms | **290.8 tok/s** | 15.5 tok/s | 4 of 6 | 128 / 441 | 14,031 | 3,557 |
| A-r2 (claim) | 23:43 | 231,777 ms | **252.1 tok/s** | 12.8 tok/s | 4 of 4 | 123 / 360 | 10,539 | 4,418 |
| B-r1 (claim) | 23:50 | 211,714 ms | **276.0 tok/s** | 16.1 tok/s | 3 of 4 | 120 / 313 | 9,594 | 4,495 |
| B-r2 (claim) | 23:56 | 194,995 ms | **299.6 tok/s** | 17.6 tok/s | 3 of 4 | 120 / 313 | 9,714 | 4,507 |
| B-r0 (warmup, discarded) | 22:26 | 193,713 ms | 301.6 tok/s | 8.4 tok/s | 3 of 4 | 120 / 313 | 10,190 | 0 |
| A-r0 (warmup) | 21:42 | — | — | — | — | 0 / 0 | — | — (booted, served no request) |

**Verdict: descriptive only.** A-r1's first boot faulted at 22:32:17 and its rate comes from the replacement
generation. 2v2 gives C(4,2)=6 allocations, minimum exact two-sided p = 0.33, so the `CLAIM` block forbids every
claim: no confirm, no "no difference", no "within 5 %". A 252.1-290.8 (median 271.5) against B 276.0-299.6 (median
287.8) is a direction marked **UNCONFIRMED** that follows the boot as much as the arm. Three deviations: claim
order is A-r1, A-r2, B-r1, B-r2, not the pre-registered A-B-B-A; the window straddled the 22:04 reboot forced by
the reset storm below; A had no discarded warmup leg (its warmup served nothing).

The gate was wrong twice, fixed in `e4da5f2daa`: the restart check failed **every** boot because its event filter
matched "exited" in the normal bookended stop line, and the fault scan read the whole appended engine log, so a
clean B-r1 was marked faulted by a previous boot's fault line (it now reads the boot's own timestamped segment).
`tokens_expected` was corrected 239,663 -> 58,423. After the fix `--check W1` reports 2 problems, both on A-r1,
which really restarted; not re-run here because it writes evidence under `out/`. Source (the W1 records are the
source of this section): `bench/ab-v0210/rpcprof/out/port-ab-20261123/records/W1-*` with the judge lines in
`.../port-ab-20261123/logs/W1-*.engine.log`; no `journal/w1-prefill-window-2026-11-24.md` exists.

## Phase 2, W2 / W3 / W4 — not run

| Window | What it covers | Blocker |
|---|---|---|
| W2 | 144k decode (n=2/arm) + short-PP 2k/8k + 3-turn TTFT and inter-turn wall | `manifest.windows.W2.pilot_frozen` is `None`: no pilot deltas are frozen, and the contract forbids claim legs without them. No record; `--check W2` returns "no records" |
| W3 | 240k decode, each boot carrying its own 58,423-token prefill | Follows W2 in order; no record |
| W4 | 2x2 engine x server cell, 2 boots per cell | Veto only at n=2 (min p = 0.33); the bootability preflight was not executed |

W2's budget is answered but W2 is still blocked: harness-prep derived 1.54-2.11 h for its 12 boots, satisfying the
plan's "no budget until legs-per-boot frozen" condition; the freeze that gates the run is the other one, the pilot
deltas, and it is empty. W3's derived range is 1.60-2.05 h, W4's 0.76-1.00 h — planning figures, not measurements.

## Phase 3 — attribution: not started

| Lever | Plan | State |
|---|---|---|
| 1 Cache/reserves 144/144 at equal helper | one variable per boot | **Not measured** |
| 2 Prefill code: `STRATA_PREFILL_PIPE_FLOOR` backport | code lane, prefill only | **Not measured** |
| 3 Engine bisect 0.1.57 -> 0.1.64 | only if 1-2 fail | **Not measured** |

W4 is the gate: until the 2x2 cell exists every Phase 2 difference belongs to the engine+server **pair**, and W4
itself can only veto such a claim, never establish one.

## Phase 4 — fault track: the outcome differs from the plan's shape

The plan's intent was to find the recurrent GPU permission fault's cause: minimal HIP repro first (the only control
that can exonerate the application without instrumentation), then an attribution shim, then one-variable configs.

| Step | State |
|---|---|
| Minimal HIP repro + positive control | **Built; the control passes** (`fdb0298b7f`). 11 control logs on disk all end in exit 134 (SIGABRT): A1-A3, B1, C1-C5, D1 and `control-fresh`. The entry and the commit count "10 of 10"; the 11th run is `control-fresh` at 00:14, after the commit. The kernel side (`no-retry page fault`, `PERMISSION_FAULTS: 0x3`, vmid 8, `0000:09:00.0`) is quoted from the kernel journal in `journal/gpu-fault-repro-positive-control-2026-11-24.md`; it is not in the harness logs |
| Cause | **Diagnosed by the maps/huge-pages route**, not by the shim: the fault hit host pages of the engine's 32 GiB RAM pool while they were backed by huge pages (`journal/amdgpu-userptr-fault-diagnosis-2026-11-24.md`) |
| Fix, verification, promotion | `STRATA_POOL_NOHUGEPAGE=1` (`MADV_NOHUGEPAGE` on the pool mapping): 4 cold prefills and a 40-minute soak with 0 permission faults and 0 resets (`journal/pool-nohuge-verification-2026-11-24.md`); live on prod `:8009` from 10:21 (`journal/promotion-pool-nohuge-2026-11-24.md`) |

The moot legs are the differentials that assumed an **unidentified** cause and never ran — `dflash-off`,
`park-on-no-evict`: no record and no question left. What did run, and what it is worth:

| Fault-differential arm | Legs | Prefill rates | Faults |
|---|---|---|---|
| `default-margins` (huge pages on, the faulting tuple) | 2 completed cold prefills, 23:03-23:12 | 287.4, 294.4 tok/s | faults in the same window |
| Huge pages off by sysctl (`nothp`, `nothp-b3`, `nothp2`, `cb-1`-`cb-4`) | 4 clean cold prefills, 00:57-01:20 | 249.6, 264.5, 241.1, 221.7 tok/s | **0** |
| `pool-nohuge` (the knob) | 5 boots, 4 clean cold prefills, 01:45-02:16 | 264.8, 274.8, 195.7, 280.1 tok/s | **0** |

Those arms answered the fault question and carry **no speed result**: one leg per boot, five sysctls changed at
once, unmatched pressure (swap 4.37 GiB faulting against 7.15-8.75 GiB on the knob arm); the diagnosis journal
says "No speed claim" for the same numbers. Still valuable, and unrun:

| Item | State |
|---|---|
| `hip-trace` `LD_PRELOAD` shim | **Built, never run against the engine** (`tools/hip-trace`, `7a134f5142`); the cheapest direct evidence of call order if the fault returns |
| `parking-off` (`--conversation-cache-mib 0` + no dir + no idle-save) | **Not measured** |
| `single-card`, `xnack` | **Not measured** |
| `pool-huge` control twin, the knob's A/B partner (`2c63683e7d`) | Config exists, **never booted** |

Separate and unresolved: the SMU/powerplay reset storm — 692 resets on `0000:09:00.0` at `Source: 1` from
2026-11-23 21:43:15 to 22:02:24 with **zero** `no-retry page fault` records in it. The SMU had answered every
message `0xffffffff` 59 s earlier and the card was already off the bus on the first block; nothing links it to the
translation fault, and no fix was tried (`journal/pool-nohuge-verification-2026-11-24.md` §7).

## Guardrail deviation, recorded as a deviation

The contract says "No promotion without a measured win". On 2026-11-24 the pool-fix engine (`15d330ea`) was
promoted on a **stability** win with **no speed measurement of the knob**, under an operator directive:

| Evidence | Detail |
|---|---|
| Permission faults | 0 in 4 cold 58,423-token prefills on `09:00.0` (kernel count 0, 01:44-02:16) and 0 in a 40-minute soak (2,873 s up, no fault, no reset) |
| Numerics | Byte-identical to the fork reference: teacher-forced 4,995 positions, one md5, top-1 100 %, KL 0.00000 |
| Speed | **Not measured for the knob.** Nearest records are the 4 knob-on prefills (195.7-280.1 tok/s) against the 2 knob-off ones (287.4, 294.4), with unmatched pressure and no interleave; the `pool-huge` twin that would make it an A/B was never booted |

An explicit, operator-authorised exception on stability grounds, not compliance with the guardrail.

## Arm re-pin (`e39905a674`), and what it does to W1

| Item | Before | After |
|---|---|---|
| Arm A | fork engine `4b8251e9`, v0215 server, helper `9d16742d` | unchanged |
| Arm B engine | `936efcc7` (`run/port0218-bin`) | `15d330ea` (`run/port0218-nohuge-bin`); helper `58c3ae02` unchanged |
| Declared structural pairs | spill flag only | spill flag **and** the pool knob (A has no consumer; B carries `STRATA_POOL_NOHUGEPAGE=1`); `settings_sha256` and `cross_arm_diffs` blank both, and `check_tuple` fails closed if a B config lacks the knob or an A config carries it |

W1's B numbers (276.0, 299.6 tok/s) were measured on the **old** B, `936efcc7`: the pre-amendment baseline, not
re-derived. A leg on `15d330ea` differs from them by the pool knob as well as by the boot, so the two do not pair.

## Next, in order

1. **W1f** — the n=3 follow-up on the re-pinned arms. In progress; no `W1f-*` record yet. The only extra prefill
   window on disk is `W1d` (engine-default margins, 2 warmup-only boots: A-r0 served 262.8 tok/s, B-r0 faulted
   23:25-23:30), which is neither W1 nor the n=3 follow-up and needs a decision — re-run or drop.
2. **W2**: pilot legs -> their deltas written into `manifest.windows.W2.pilot_frozen` -> commit -> claim legs. **Not measured.**
3. **W3**: 240k decode. **Not measured.** Then **W4**, then Phase 3. **Not measured**; no engine-vs-server
   statement before the cell exists.
4. Open: the knob's speed A/B (`pool-huge` twin vs `pool-nohuge`, same day, matched pressure, at least 1,000
   generated tokens per leg for any TG figure). **Not measured.**
5. Open: the SMU/powerplay reset class (692 resets). **Unresolved**; no leg run.
