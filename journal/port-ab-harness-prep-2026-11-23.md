# Phase-2 A/B harness prep (2026-11-23, CPU-only)

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
9. The helper fill band is frozen in `frozen_tuple.helper_fill_band` (rows 3,430-3,970; 6.55-7.00
   GiB; 5 % arm drift), derived from the 7,000 MiB cap and the Phase-1 row size. Every boot record
   now also carries the page-cache proxy, the kernel-journal fingerprint, the LOW count, the granted
   FS slots, the cache slot count and the restart flag.
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
