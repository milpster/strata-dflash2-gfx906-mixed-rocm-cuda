# Phase-2 A/B harness prep (2026-11-23, CPU-only)

**Updated 2026-11-23 (evening): n reduced from 3 to 2 per arm by operator directive.** The
sections above the last one describe the n=3 freeze and are kept as history; the last section is
the current schedule, budgets and claim rule.

Scope: the artifacts the approved measurement contract's Phase 2 needs, built at harness-prep and
committed before any window. Plan: `/home/srcds/.opencode/plan/measurement-contract-speed-attribution-faults.md`.
Lane: fixer (CPU only). No boot, no GPU work, no `:8009` traffic, no process control; prod kept serving
throughout. Branch `recovery-startup-watchdog` (local, unpushed): harness-prep `37e93f467b`,
`c61b533bae`, `afe71f39ff` (journal), `e6e3bb9517` (W4 reps field), `74c23fea9a` (prereq-0 stage-core
rule), then the oracle-review fixes below.

## What was frozen

`bench/ab-v0210/rpcprof/port-ab-manifest.json` (+ `.sha256`), regenerated and re-verified by
`port_ab_prepare.py --prepare|--verify`. Every sha was produced by hashing the file on disk at prepare
time, not copied from a boot line.

| Arm | Engine | Helper | Server tree (digest / files) |
|---|---|---|---|
| A (fork) | `run/fsclamp-bin/rpcw-hip/strata` `4b8251e9e06334a8e2dc8ab78453449a4820dccd1a4d13efe4faf19100233d41` (50,147,224 B) | `run/fsclamp-bin/rpcw-cuda/strata-expert-helper-patient` `9d16742d5f30e9c199989f27643ee434e7aec66407f02e5cec36355109ba57b2` | `v0215` `6d42e742754080f0…` (810 files); server.py `b6463e7b99a0d74c…` |
| B (port) | `run/port0218-bin/rpcw-hip/strata` `936efcc798aab3952551281f2f328c1b8b19311fee9d0298142e702809a58394` (30,555,416 B) | `run/port0218-bin/rpcw-cuda/strata-expert-helper` `58c3ae020a7479a13c622627544507f5b43f284f9be4b5222ac90c3421b88bb2` | `run/port0218-server` `5cc1793283e04207…` (1,473 files); server.py `b593dceb91bd41c3…` |

The `serve/` subtree digests are recorded separately (A 194 files, B 295 files) because that is the code
that actually builds the engine argv; the whole-tree digests also cover `strata_control/` (the drafting
chooser and user-args merge both live there in the port tree).

Tuple written into both arms: reserves 256/192, `STRATA_RAM_BUDGET_MIB` 36864, `--expert-helper-slots`
4600, helper argv `--fill-mib 7000 --reserve-mib 0`, spill 0 (A: flag absent, its parser has no such
option; B: `--conversation-spill-mib 0`), `--spec 5`, `--dflash-block 7`, layer split 30, helper timeout
10,000 ms, matched `model_name`/`reasoning_budget_tokens 0`/`reasoning_effort high`.

## Config generator

`port_ab_configs.py` derives each leg's config from the arm's ADOPTED base (`prod-config-ud-iq3xxs-
dflash-w4-q4_0-mixed-fson.json` for A, `prod-config-port0218.json` for B) and writes only the tuple and
the harness paths, recording each change as a delta. `cross_arm_diffs()` returns the empty dict for the
prepared pair once the harness paths and the declared spill pair are blanked: the arms differ only in
engine/cwd/log/sockets/cache-dir plus that structural flag.

Two base values had to be matched rather than inherited, both recorded as deviations: `model_name`
(A `ud-iq3_xxs`, B `swift-1.5-ud-iq3xxs-managed`) and `reasoning_budget_tokens` (A 55,000, B 2,000).
Neither name matches either tree's drafter regex, so `drafting.apply` leaves the args untouched; the
0 value removes a server-side thinking cap and the request template pins `enable_thinking=false`.

## Legs (exact invocations in the manifest, per window)

* W1: 239,663-token leg (323,200 B frozen head-tagged corpus, `max_tokens 8`), boots A B B A A B plus one
  discarded first-touch boot per arm.
* W2: 144k decode boot (`kind=tg`: request 1 fresh prefill, request 2 the 1024-token cached re-run — the
  decode rate read is request 2's), short-PP 2k and 8k (`max_tokens 32`), and a 3-turn TTFT leg measured
  from the server's own event stream (`/v1/telemetry` activity: `first_token - queued`) and from the
  client's first non-empty streamed delta. Pilot boots per arm run the short shapes first; their deltas
  are to be written into the manifest before the claim legs.
* W3: 239,663 prefill plus two 1024-token generations per boot (fresh, then cached).
* W4: one decode shape per cross cell (A-engine+B-server, B-engine+A-server), two boots each.

Budgets: W1 1.5-2.5 h (contract), W2 1.67-2.31 h (DERIVED here for the first time: 6 decode boots + 6
short boots + 2 pilot boots, from the contract's own 287.1-348.2 tok/s prefill and 21.6-22.1 tok/s decode
anchors and 3-5 min boots), W3 1.6-2.05 h derived (contract states 1.2-2 h; the contract's window governs
the gate, the derivation is recorded for planning), W4 0.75-1.08 h derived (~1 h in the contract).

## 2x2 preflight (static, no boot)

Binary literal scan of each engine plus a model of each server's argv builder (config args + layer split,
helper channels, parallel/pool-cores, profile-save, vram-elastic, draft-costs, drafting rewrite):

| Cell | Verdict |
|---|---|
| A-engine + A-server | bootable |
| B-engine + B-server | bootable |
| A-engine + B-server | bootable (with A's config) |
| B-engine + A-server | bootable (with B's config) |
| B's tuple fed verbatim to A's engine | refuses at argv: `--conversation-spill-mib` |

The fork engine (0.1.57 lineage, built from `564e1dbbea`) has no `--conversation-spill-mib` literal; the
port engine (0.2.18 lineage) does. The port server's `--draft-costs` path is self-gated off here (the
engine text probe would find the flag, but `draft_costs` defaults false off Metal and the tuple never sets
it). Per-engine configs therefore make both cross cells bootable while "no spill" holds in both. That is
the manifest's answer to the contract's "port argv is stricter" note: W4 need not be reduced to a pair.

## Prerequisite-0 gate

`port_ab_prereq0.py` exits 0 only when: MemAvailable >= 15 GiB, total non-engine RSS <= 8 GiB, no single
non-engine process >= 2 GiB left unresolved, no non-pinned thread above 50 % of a core on 3,5,7,9,11,13,15,
no stratum serving process alive (the gate is pre-boot), and >= 40 GiB free on the cache filesystem. It
writes a JSON snapshot next to the boots and is called by `port_ab_boot.sh` before anything starts.

Run at prep time it correctly REFUSED: prod serving (4 processes), MemAvailable 11.1 GiB, /tmp 31 GiB —
i.e. the gate works and the box is not yet ready for a window.

## Not built (named)

* No boot, leg or measurement (fixer lane, operator-gated windows).
* The drafting no-op is asserted from both trees' regexes, not from an executed chooser: confirm from the
  first boot line that the engine argv matches the config's args verbatim.
* W4's optional third boot per cell (min exact p 0.10) is planned, not prepared.
* W2 pilot deltas are not pre-registered; the contract requires the pilot legs to run first and their
  deltas to land in this manifest before the claim legs.

## Oracle review round 1 (2026-11-23): execution layer fixed

The oracle accepted the frozen values (6/6 shas, tuple, W2 arithmetic) and found three blocking and
eight non-blocking defects in the execution layer. All eleven plus the trivia are fixed and re-validated
CPU-only (`--verify`, synthetic gates, selftests, `bash -n`, `py_compile`; no boot, no GPU, no
`:8009`, no push).

Blocking:

1. **Boot path joined twice.** `port_ab_boot.sh` treated the engine FILE as a directory
   (`$BIN/rpcw-hip/strata`), so the file checks, `ENG_SHA` and the boot JSON pointed at a path that
   does not exist and `set -euo pipefail` aborted before the supervisor. The boot plan now owns the
   engine path (`P_ENGINE`) and the boot hashes exactly that file.
2. **No `pair_supervisor.py record` calls.** The supervisor reads its peers ONLY from
   `$STRATA_PAIR_RECORD`; without the records it never latches healthy, so at `--startup-timeout`
   it killed the group mid-leg (870-1450 s legs), and stage-CPU re-pinning and the stop script's
   identity checks never ran. The child now publishes the helper peer after readiness and both peers
   after the server starts, exactly as the prod launchers do.
3. **W2 boot-shape key collision.** `leg_paths` keyed on window-arm-rep only, so the decode and short
   boots of W2 shared one key, cache dir, sockets and state file; the second boot either refused (cache
   guard) or reused the first one's state. Boot keys now carry the boot group everywhere
   (`W2-A-dec-r1` / `W2-A-sht-r1`): `leg_paths`, `schedule`, the runbook, boot, stop and the
   manifest's config table. The manifest was regenerated.

Non-blocking:

4. The gate checker now reads the BOOT RANGE (`elog[0:elog_start]`), not the leg slice, and parses
   the engine's `engine started:` argv as flag/value pairs (the old order-sensitive regex matched
   nothing because the real order is spec, reserve, reserve-later, dflash-block). It also enforces the
   helper fill band, the supervisor restart (state generation, events log, engine lines), the GPU fault
   lines and the kernel fingerprint. `port_ab_legs.py --selftest` reproduces the oracle's
   counter-examples: a record with a restart line and a wrong tuple now FAILS.
5. TTFT `inter_turn_wall_s` is `next turn's first delta - previous turn's finish` (absolute epoch
   stamps), not the client's idle gap; selftest asserts it.
6. The TTFT leg reads its own 2k corpus (`ttft2k`, different head tag), so turn 1 is a cold prefill
   even in a short boot that already ran `pp2k`; the docstring says so.
7. W2 gained a discarded decode warmup boot per arm (2 boots), and every window's budget now counts
   its discarded boots: W1 1.93-3.22 h, W2 2.05-2.81 h, W3 2.14-2.73 h, W4 0.76-1.00 h. The
   contract's numbers are kept beside them as `contract_budget_s` (1.5-2.5 h W1, 1.2-2 h W3, ~1 h
   W4); the planning number is the derived range.
8. Manifest fields: W4's interleave is its own four boots (not the six-boot pattern), `reps_per_arm`
   is gone in favour of `boots_per_cell`, W4's budget counts the cached second generation, and every
   window's `budget_source` is the same phrase (derived), with the contract value as its own field.
9. The helper fill band is frozen in `frozen_tuple.helper_fill_band` (rows 3,430-3,970; 6.55-6.90
   GiB; 5 % arm drift), derived from the 7,000 MiB cap and the Phase-1 row size; the GiB upper bound
   is 66 MiB over the cap for the helper's startup accounting, not the 168 MiB the first draft allowed.
   Every boot now also writes `boots/<key>.evidence.json` at `--check` time, carrying the page-cache
   proxy, the kernel-journal fingerprint, the LOW count, the granted FS slots, the cache slot count
   and the restart flag (the boot record itself stays as written at boot and at stop).
10. The freeze is enforced AT BOOT: `port_ab_boot_plan.py` refuses when the derived config is not
    byte-identical to `manifest.configs[key].sha256` or an engine/helper sha left the manifest; the
    helper's entire argv comes from `pc.helper_argv` (no bash defaults) and its env (timeout, frozen
    PATH/HOME/TERM) is recorded per boot and in the manifest.
11. The W2 pilot deltas have their own slots (`short_pp_read_tps`, `short_pp_gen_tps`,
    `ttft_turn1_s`, `ttft_inter_turn_s`) and claim legs fail closed while any is null; `--freeze-pilot
    W2` computes them from the pilot records and writes the manifest.

Trivia: the journal's commit list is corrected above; `settings_sha256_blind` blanks the declared
spill flag before hashing (A and B now hash equal with `diffs == {}`); the boot docstring names
`--run`.

## Final advisories before the first window

The oracle approved the harness (`4c4d95fe8e`) with four non-blocking advisories and two nits; all
are fixed in the commit after it on this branch, CPU-only (no boot, no GPU, no `:8009`):

| # | What was wrong | Fix | Where |
|---|---|---|---|
| 1 | prereq-0 appended FAIL lines for the memory/RSS/CPU gates even with prod serving, so a prod-up run looked like a starved box | one verdict function with two states: serving alive -> the single failure "stop prod first", those four gates reported "not evaluated (prod up)"; disk stays measured; exit code unchanged | `port_ab_prereq0.py:143-184` (`PREBOOT_GATES`, `gate_verdict`), snapshot `gates`, `--selftest` prod-up case |
| 2 | the supervisor-events restart source read `run/<key>-events.log`, but `pair_supervisor.append_event` writes `run/<key>.supervisor-events.log` | correct path, and only `recover|restart|exited` lines count: a clean "stopped: stop requested" event never fails a boot | `port_ab_legs.py:307-327` |
| 3 | the kernel fingerprint ran to `time.time()`, so at window end one fault was attributed to every earlier boot | `port_ab_stop.sh` writes `ended_epoch` + `final_generation` into `boots/<key>.json` (first stop wins); the checker bounds the slice by it, else by the next boot's start; with neither, an empty window plus a warning (never an invented FAIL) | `port_ab_legs.py:264-296,484-511`, `port_ab_stop.sh:72-76` |
| 4 | the per-boot evidence was computed but never written, though this journal claimed the boot record carried it | `--check` writes `boots/<key>.evidence.json` (fingerprint + window, page cache, LOW count, granted FS slots, cache slots, restart, fill, tuple, problems/warnings); item 9 above reworded to match | `port_ab_legs.py:514-535,566` |
| 5 | helper fill band's GiB ceiling was 7.00 GiB (7168 MiB), 168 MiB above the 7,000 MiB cap; `windows.W2.delta.ttft_s` was a superseded null slot | ceiling tightened to 6.90 GiB with the 66 MiB allowance stated in the band's basis; `ttft_s` removed from W2's `delta` (the pilot `pilot_deltas` slots are the TTFT deltas) | `port_ab_contract.py:173-182,125-128`, manifest regenerated |

Selftests cover each: prereq-0 clean/starved/prod-up; legs' correct-path restart, filtered normal
stop (and the superseded path ignored), fault after a boot's end not failing it, no-end fallback to
the next boot's start, and the evidence file's fields.


## Window W1 open (2026-11-23 21:1x)

Operator directive: the 12.54 GiB non-engine RSS (browser, VS Code, Hindsight, opencode host - normal desktop usage) is waived as the idle baseline for this window. RAM headroom with prod stopped: ~50 GiB. The prereq-0 snapshot (prereq0-20261123-211245.json) is retained as the record; the waiver is operator-authorized, the per-leg memory sampler still tracks every leg. W1 budget: the derived 1.93-3.22 h (warmups counted) per the operator's go.


## n=2 per arm (operator directive 2026-11-23 evening): descriptive-only window

Operator directive: reduce the A/B arms from n=3 to n=2. Everything below is CPU-only work on
`recovery-startup-watchdog` (no boot, no GPU, no `:8009`, no push); the previously open W1 is
re-planned, not re-measured, at the reduced sample.

**Statistical consequence, stated plainly.** 2v2 has C(4,2) = 6 allocations and a minimum exact
two-sided p = 1/6 + 1/6 = **0.33**. No interval at this n reaches 90 % (or 95 %), so this window
supports **no claim at all**: `confirm`, `no difference` and `within ±delta` are all
unsupported. Its honest outputs are the per-leg rates, full ranges and memory, plus a directional
observation marked **UNCONFIRMED**. Any of those three statements requires a **follow-up window at
n>=3 per arm** (3v3: C(6,3) = 20 allocations, min exact p = 0.10 — the 90 % exact/HL rule the
contract already states); n=4 would restore 95 %. The manifest's claim block now says exactly this,
and `port_ab_prepare.py --verify` fails if the block drifts from `port_ab_contract.CLAIM`.

Schedule (claim boots interleaved A B B A, replicates 1..2 per arm):

| Window | claim boots | discarded boots | total boots | measured legs | claim legs |
|---|---|---|---|---|---|
| W1 | A1 B1 B2 A2 | 2 first-touch warmups (A0 B0) | 6 | 6 x pp239k (1/boot) | 4 |
| W2 | dec A1 B1 B2 A2, sht A1 B1 B2 A2 | 2 sht pilots (A0 B0) + 2 dec warmups (A0 B0) | 12 | 4 decode + 6 x 3 (2k/8k/ttft) | 4 decode + 12 |
| W3 | A1 B1 B2 A2 | 2 first-touch warmups (A0 B0) | 6 | 6 x pp239k tg (1/boot) | 4 |
| W4 | unchanged: 2 cross cells x 2 boots | none | 4 | 4 x dec144k | 4 (veto only) |

The config tree shrank from 36 to 28 frozen configs; the eight `*-r3.json` configs of W1/W2/W3
were deleted. `port_ab_boot.sh` and `port_ab_stop.sh` took the replicate as an argument and
needed no change; the schedule, runbook, manifest and configs all derive from
`port_ab_contract.REPS`, so the reduction is one edit plus a regeneration.

New derived budgets (arithmetic shown; `contract_budget_s` stays in the manifest as the
contract's reference value, and the planning number is always the derivation, which counts every
discarded boot):

| Window | boots | new derived | arithmetic | old n=3 derived | contract |
|---|---|---|---|---|---|
| W1 | 6 (4 claim + 2 warmup) | 1.45-2.42 h | 6 x (690-1,150 s leg + 180-300 s boot) = 5,220-8,700 s | 1.93-3.22 h (8 boots) | 1.5-2.5 h |
| W2 | 12 (6 dec = 4+2, 6 sht = 4+2) | 1.54-2.11 h | 6 x 686.2-896.4 s decode + 6 x 237.5-367.6 s short = 5,543-7,584 s | 2.05-2.81 h (16 boots) | none (this manifest is the first) |
| W3 | 6 (4 claim + 2 warmup) | 1.60-2.05 h | 6 x 960.96-1,229.59 s = 5,766-7,378 s | 2.14-2.73 h (8 boots) | 1.2-2 h |
| W4 | 4 (2 cells x 2) | 0.76-1.00 h | 4 x 686.2-896.4 s = 2,745-3,586 s (unchanged) | 0.76-1.00 h | ~1 h |

The n=2 window is ~25 % shorter than the n=3 plan in W1-W3 (4 claim boots instead of 6) and W2 is
nearly halved (12 boots instead of 16). The W1 window opened at 21:1x is now gated on 1.45-2.42 h,
not the 1.93-3.22 h recorded above.

Validation (CPU only, exit codes as run):

| Check | Command | Exit |
|---|---|---|
| manifest re-verify after regeneration | `port_ab_prepare.py --verify` | 0 (OK) |
| leg/boot gate selftests | `port_ab_legs.py --selftest` | 0 (12/12 ok) |
| 2x2 argv preflight | `port_ab_preflight.py --strict` | 0 (4 bootable cells, port tuple verbatim refuses at argv) |
| TTFT parser selftest | `port_ab_ttft.py --selftest` | 0 |
| prereq-0 selftest | `port_ab_prereq0.py --selftest` | 0 |
| shell syntax | `bash -n port_ab_boot.sh port_ab_stop.sh` | 0 |
| python syntax | `python -m py_compile port_ab_{contract,configs,legs,prepare,boot_plan,preflight,prereq0,ttft}.py` | 0 |

Not done, on purpose: no boot, no leg, no measurement, no GPU, no `:8009` traffic, no push. The
n=3 fallback remains one edit (`REPS`) away if the operator reverses the directive.


## Prerequisite-0 waiver (2026-11-23, fixer lane, CPU only)

The W1 boot was blocked at prerequisite 0: the only failing gate is the non-engine RSS sum
(12.54 GiB at 21:12, 13.29 GiB at 21:23) while MemAvailable was ~48 GiB, no single non-engine
process reached 2 GiB, no stage-core thread was hot and the disk was 61 GiB free. The operator
has waived that reading as the idle baseline (see "Window W1 open" above), so the gate now has
one explicit, per-invocation override instead of a manual bypass:

* `port_ab_prereq0.py --waive-nonengine` (or `PREREQ0_WAIVE_NONENGINE=1`;
  `--no-waive-nonengine` forces the gate back on) moves ONLY the non-engine RSS sum failure
  into `waived` in the snapshot, with the reading, `by: operator`, the directive text and this
  journal reference. The verdict becomes `OPEN (WAIVED: nonengine_rss by operator)` and the
  exit code is 0. Every other failure (MemAvailable, per-process >= 2 GiB, stage cores,
  serving processes, disk) still appends to `failures` and still exits 1; the prod-up state
  waives nothing because those gates are not evaluated there.
* `port_ab_boot.sh ... --waive-nonengine` (or the same env var) passes the flag to that one
  prerequisite-0 call. Nothing is global: a later invocation without it refuses again, and the
  printed output always shows the actual reading.
* The manifest was re-pinned deliberately (`port_ab_prepare.py --prepare`): only the two
  harness script shas, `prepared_at` and `git_head_at_prepare` changed; `--verify` is OK and
  no arm, corpus, config or budget value moved.

Validation (CPU only; no boot, no leg, no GPU, no `:8009`, no push):

| Check | Command | Result |
|---|---|---|
| python syntax | `py_compile port_ab_prereq0.py` | 0 |
| prereq-0 selftest (unwaived refusal, waived pass, memory/stage-core/disk/prod-up still refuse) | `port_ab_prereq0.py --selftest` | 0 |
| shell syntax | `bash -n port_ab_boot.sh` | 0 |
| live gate with the waiver | `port_ab_prereq0.py --out out/port-ab-20261123 --cache-dir out/port-ab-20261123 --waive-nonengine` | 0, `OPEN (WAIVED: nonengine_rss by operator)`, snapshot `prereq0-20261123-212629.json` (12.53 GiB reading) |
| live gate on a 31 GiB filesystem with the waiver set | same, `--cache-dir /tmp/opencode/prereq0-check` | 1, refused on the disk gate (waiver covers the sum only) |
| manifest | `port_ab_prepare.py --verify` | 0 (OK) |

One reading printed by the live run is worth keeping: the `/tmp` probe refused at 31 GiB free
while the sum gate was waived, which is the intended shape - the waiver is one gate, not a
blanket pass.
