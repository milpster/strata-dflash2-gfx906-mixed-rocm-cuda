# arena-54 trial: oracle round 2 (1 blocker + 7 hardening items) - fixer lane

Date: 2026-11-20. Main-tree edits limited to the two authorized files, one commit. The
launcher was **not** touched: item 3 was solved by reading the log path out of the
supervisor's state file, so no launcher hook was needed. No boot, no probe, no GPU work,
no signal to any process, `stop-all.sh` was never invoked (its changed loop was walked
against a sandbox copy of its own listing). Prod on :8009 untouched - it was only read
through `/proc` and its state file (both read-only).

Files changed: `stop-all.sh`, `bench/ab-v0210/rpcprof/gpu-rounds/arena54-window-20261120.sh`.

## BLOCKER #1 - stop-all.sh did not know the arena54 lane

Empirical confirmation of the symptom before touching the script: the live prod supervisor
(pid 1002348, read from `run/prod-swift15-f16kv-mixed-supervisor.json`) has the command line

```
.../python .../pair_supervisor.py run --launcher /mnt/srcds/Strata/launch-v0238.sh --config ...
```

and the sweep pattern is `launch-prod-|launch-trial-|strata --serve|strata-expert-helper|serve/server[.]py --engine strata`
- `launch-v0238.sh` matches none of them, so that supervisor is invisible to both the sweep
and the quiet check. Exactly oracle's described failure.

Changed lines (3 edits, everything else byte-identical):

| lines | what |
| --- | --- |
| 2-6 | header comment now says "every pair supervisor (any lane whose state file sits in run/ as `*-supervisor.json`)" instead of "both pair supervisors (prod + trial-spill40)" |
| 18-24 | `for state in run/prod-...json run/trial-spill40-...json` -> `for state in run/*-supervisor.json` (a glob already expands sorted), preceded by a comment naming the respawn mechanism (`pair_supervisor.py:276`, 30 s capped backoff), the flapping, the false `ALL STOPPED`, and the rule that a new lane must drop a state file in `run/` or this loop is extended by hand |
| 27-29 | one added guard on the existing TERM line, see below |
| 42-45 | the socket sweep gained `run/trial-arena54.sock run/trial-arena54-2.sock` (the names `launch-trial-arena54.sh` uses for `--socket`/`--socket2`) |

Unchanged on purpose: `term_pid`, the sweep patterns, `count_strata`, the 20 s quiet check,
and every output string (`stop-all: supervisors first (state files)`, `  TERM ...`,
`  KILL ...`, `stop-all: verifying 20s of quiet`, `stop-all: before=... after=... — ALL STOPPED`).

**One addition beyond the prescription, for the parent/oracle to veto by deleting the clause:**
a state file outlives its run, and widening from 2 hard-coded files to 8 makes a recycled pid
a live hazard - `kill -0` on a recycled pid succeeds and the loop would `kill -TERM` an
unrelated process. The TERM is now conditioned on that pid's `/proc/<pid>/cmdline` containing
`pair_supervisor.py`:

```
[ -n "${sup:-}" ] && kill -0 "$sup" 2>/dev/null && grep -qa 'pair_supervisor[.]py' "/proc/$sup/cmdline" 2>/dev/null && { ... }
```

It only narrows who may be TERMed; it never adds a target. The cost is that a supervisor
started from something other than `pair_supervisor.py` would now be skipped (there is no such
lane on this box).

Walk of the changed loop against a mocked `run/` (the loop is extracted **verbatim** from the
real file by `mock-stop-all-walk.sh`; `kill -TERM` and `term_pid` are stubbed to print; the
"supervisor" is a throwaway `python3 fake/pair_supervisor.py` that sleeps; no signal was sent
to anything on the box):

| entry in the mocked listing | outcome |
| --- | --- |
| `prod-swift15-f16kv-mixed-supervisor.json` | `TERM ... supervisor <pid>` (would be TERMed) |
| `trial-arena54-supervisor.json` | `TERM ... supervisor <pid>` - the lane the blocker was about |
| `trial-spill40-supervisor.json` | `TERM ... supervisor <pid>` |
| `r7-trial-supervisor.json` (pid 999999, dead) | skipped by `kill -0` |
| `prod-ud-iq3xxs-mixed-supervisor.json` (live pid of a bash) | skipped by the new cmdline guard |
| `prod-swift15-f16kv-mixed-supervisor-affinity-1-1.json` | not matched by the glob (it does not end in `-supervisor.json`) |

Full output: `walk-output.txt`.

## Window script items

| # | item | how it is implemented |
| --- | --- | --- |
| 2 | signal trap | `on_signal()` (:123-133) removes `$OUT/pressure.hold`, `kill -TERM $PRESSURE`, calls `restore_prod`, exits 1, and clears the trap itself first so it cannot fire twice. Armed at :299 (`trap on_signal INT TERM`), immediately after the T0 probe and before the holder is started; cleared at :311 (`trap - INT TERM`) once the holder is stopped, so the scripted teardown at :316 owns the normal path. |
| 3 | stale-log false-ready | Both polls now go through `wait_ready <state> <pre-boot-log> <port> <tries>` (:76-95). The path comes from `engine_log` in the lane's supervisor state file - the same field `pair_supervisor.py:342` uses for its own attach test - and a path still equal to the snapshot taken before the boot is refused. A previous boot's log that already contains `ready: http://` can therefore never pass for this boot. Trial poll: :234-237 (snapshot `PREV_TRIAL_LOG`, 150 tries). Prod poll: inside `restore_prod` (:114-116, 120 tries). `ls -t \| head -1` is gone from both. |
| 4 | engine pid assert | :249-262. `engine_pid_ready` allows a one-minute grace (the supervisor publishes `engine_pid` when it attaches, and it attaches on the same ready line the readiness poll waits for - without the grace a correct boot could abort on a race), then the gate fails on pid 0, a dead pid, or a `/proc/<pid>/cmdline` that does not carry the trial's identity. Deviation, flagged: the prescription's literal `trial-config-arena54` cannot appear in an engine command line - the engine is started by `serve/server.py` from the parsed config, and the live prod engine's command line contains no config path at all (verified by reading `/proc/1003553/cmdline`). The accepted markers are therefore `trial-config-arena54` **or** `trial-arena54.sock`, the latter being the trial's helper socket path that `serve/server.py` really does pass in `--expert-helper-socket` (seen in the same read). The gate dumps that command line before restoring prod. |
| 5 | `restore_prod` verifies | :111-122. It logs stop-all's last two lines, boots prod, then runs `wait_ready` against `run/prod-swift15-f16kv-mixed-supervisor.json` and returns 0 only after prod answers on :8009, echoing `prod restored: <ready line> ... log: ...`; otherwise `PROD RESTORE FAIL — INTERVENTION NEEDED <time>: prod did not answer on :8009` and status 1. Six abort paths and the trap call it, so none of them can end unverified; the scripted teardown is `restore_prod \|\| exit 1` (:316), replacing the duplicated inline poll. |
| 6 | experts.bin content | `record_bin_sha()` (:107-110) writes `$OUT/experts-bin.sha256`. First run (the boot that wrote the file): recorded at :223-226. Later run: the skip branch (:172-186) compares the file's sha256 against the record and aborts with `experts.bin content changed - recorded X, now Y` on a mismatch. With no record and the right size, the run continues and the record is written only after the boot gate has proved this boot really maps the file (:288-293). |
| 7 | wrong-size pre-flight | :140-155, before any lane is touched. A wrong-size file makes the pre-gen boot unwinnable (with the file present `from_gguf` is false, so the wrote line can never print), so it aborts immediately with the "stale/unpadded experts.bin blocks pre-gen: delete it or re-derive it" message, the `iq_pack.py --experts-bin` warning, `restore_prod`, and exit 1. |
| 8 | evidence dir | The header and `OUT` already named the same directory (`.../out/arena54-trial-20261120`); the header now says so explicitly (`$OUT, i.e. bench/...`), and the window echoes `evidence dir: $OUT` at start so the log proves which directory a run used. |

Supporting changes in the same file: `sup_field()`, `size_of` reuse, `engine_pid_ready()`,
`TRIAL_STATE`/`PROD_STATE`/`BIN_SHA_FILE` variables (and `snap()` now reads `$TRIAL_STATE`
instead of repeating the literal path).

## Validation

| check | result |
| --- | --- |
| `bash -n stop-all.sh` | exit 0 |
| `bash -n arena54-window-20261120.sh` | exit 0 |
| `bash -n` on both mock walkers | exit 0 |
| `mock-stop-all-walk.sh` (6-entry mocked `run/`) | exit 0 - table above |
| `mock-window-guards.sh` #3 | exit 0 - stale path refused, new path accepted, no-port refused |
| `mock-window-guards.sh` #7 | exit 0 - absent pass, exact pass, wrong size ABORT + status 1 |
| `mock-window-guards.sh` #6 | exit 0 - no record defers, match passes, same-size/other-bytes ABORT + status 1 |
| `mock-window-guards.sh` #4 | exit 0 - pid 0 / dead / wrong-cmdline fail, trial-marked pid passes |
| trap placement grep proof | `trap on_signal INT TERM` :299 < holder start :301 ; `trap - INT TERM` :311 < final `restore_prod` :316 ; handler clears itself :127 |

The two mocks extract the pieces under test **verbatim** from the real scripts (a literal
`grep -F` picks the line, awk walks to the matching `fi`/`}`) and run them with the
destructive parts stubbed, so the behaviour shown is the behaviour in the scripts.

Two fixes made during validation, both in the mocks (not the scripts): gawk reads `/\bif\b/`
as backspace, so the block extractor tokenises instead; and `exit 1` inside an extracted block
had to run in a subshell so one case could not end the walk.

## NOT RUN

- `stop-all.sh` was never invoked - not against real state files, not in any form.
- No boot (neither lane), no probe, no request, no port or signal operation. Prod :8009 was
  only read: its supervisor's state file and `/proc/<pid>/cmdline`.
- No measurement of anything: no tps, RSS, swap, VmLck, THP or memory.events numbers. The
  46.84 GiB sha256 path was exercised only on 4 KiB stand-ins, so its real duration (a few
  minutes of I/O per window) is estimated, not measured.
- The one-minute `engine_pid_ready` grace and the `wait_ready` try budgets are untested against
  a live boot.
