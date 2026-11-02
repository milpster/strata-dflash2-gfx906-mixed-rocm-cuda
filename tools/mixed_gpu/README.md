# Mixed HIP/CUDA maintenance harness (tools/mixed_gpu)

Phase 1 preparation only. `maintenance.py` captures a read-only baseline of the live host and
performs guarded, idempotent VRAM-swap maintenance. **Nothing changes without `--execute`.**

Not validated on hardware. No coexistence, parity, benefit or measurement was performed in this
lane; prod and the NVIDIA-backed swap stayed up throughout. Read the "Not validated" section
before planning any window.

## What the installed scripts really do (read, not assumed)

From `/etc/systemd/system/vram-swap-nbd.service` and `/usr/local/bin/nbd-vram-*.sh`:

  * `ExecStartPre` sets `vm.swappiness=150 vm.min_free_kbytes=1048576`; `nvidia-smi -pm 1` wakes
    the GPU. `ExecStart=/usr/local/bin/nbd-vram` (notify daemon, owns the CUDA backing).
    `ExecStartPost=nbd-vram-connect.sh` finds a free `/dev/nbdN`, connects to
    `/run/nbd-vram.sock`, `mkswap`, `swapon -p 1500`, writes `/run/nbd-vram-dev`.
  * `ExecStop=nbd-vram-disconnect.sh` runs `swapoff "$NBD_DEV"` first and **aborts (exit 1) if
    swapoff fails**, by design: disconnecting NBD with live swap pages mapped panics the kernel.
  * Stop defaults after a successful drain: `vm.swappiness=60 vm.min_free_kbytes=65536`.
  * `TimeoutStopSec=1800`, `SendSIGKILL=no`, `KillMode=process`, `Restart=on-failure`.

**The two hazards this harness exists for:**

 1. `nbd-vram-disconnect.sh:8-11` is **not idempotent**: after a manual `swapoff`, a later
    `systemctl stop` runs `swapoff` again, it fails, the script exits 1, and systemd then
    SIGTERMs the daemon (`KillMode=process`). SIGTERM page safety is unproven.
 2. A stop that fails mid-drain leaves the unit failed with `Restart=on-failure`, boot
    `WantedBy=multi-user.target` and the suspend hook `nbd-vram-sleep.sh pre` able to re-reserve
    VRAM; nothing in the unit knows a manual drain happened.

So `drain` is **drain-first**: `swapoff <confirmed device>` → verify absence in `/proc/swaps` →
only then `nbd-client -d`, remove the state file it owns, `systemctl stop`, then **re-read the
sysctls after the stop** and restore the captured 150 / 1048576 only if the installed ExecStop
changed them to 60 / 65536. A failed swapoff, or a device that stays in `/proc/swaps`, aborts
(exit 3) with **no** disconnect, **no** stop, **no** kill, **no** state cleanup, so the operator
can free RAM and retry the same command. A fully successful pass ends at `phase=cleaned`, not a
partial: the sysctl decision is never taken from a pre-stop cache.

**Durability.** The operation record (`--opstate`) is written to disk **before the first
mutating command** and updated after every step; nothing is cleaned up in a `finally` clause.
Writes are atomic: temp sibling in the same directory, `flush` + `fsync`, `os.replace`, then
`fsync` of the parent directory. A write failure is reported honestly - if a mutation already
happened the run exits **5** and the last durable record stays on disk (no cleanup afterwards),
and a record whose directory cannot be synced is refused, because a recovery record that is not
durable is not a recovery record.

`drain` **reuses** an existing record: it loads and validates it first, keeps its `started_iso`,
`started_btime`, `steps` and `operations` history, records the prior phase as `prior_phase` and
appends this operation. A foreign, unreadable, malformed, non-object, other-device, other-unit or
other-snapshot record is refused **before any mutator** - the tool never truncates a prior
receipt and never replaces another operation's record. Every mutating command's return code is
checked *and* the fact it was supposed to change is re-probed. Success is claimed only from the
re-probe. A cleanup that half-succeeded exits **5** with the phase that names the exact step
(`drain-failed`, `drain-disconnect-failed`, `drain-stop-failed`, `drain-sysctl-failed`,
`drain-precheck-*`, `drain-postcheck-*`), keeps the marker it has not proved safe to remove, and
the same command resumes from the live state - it never demands a marker the tool itself removed,
and it never deletes an unverified recovery record. The record is bound to the snapshot by sha256,
and to the unit, device, phase and priority.

**Unknown is never success.** An unreadable `/proc/swaps`, a blank swap table, a malformed row, an
`is-active` that exits oddly with empty stdout, an `is-active`/`ActiveState` contradiction, an
unreadable `/proc` enumeration, an unreadable `/proc/<pid>/stat`, an unreadable
`/run/nbd-vram-dev`, an unreadable sysfs client record, an unreadable sysctl: all are **unknown**
and the tool refuses. Only `ENOENT` counts as a known-absent file; only `is-active rc=3` with
stdout `inactive` (or a successful `show` with `ActiveState=inactive`) counts as a known-stopped
unit. A header-only `/proc/swaps` table is a **known empty** table, deliberately distinct from an
unreadable one.

## Identity, causality and the launcher: what V1 refuses (read before any window)

`drain`/`restore` act only when the backing they are about to touch is **identified**, not
inferred:

  * **Same boot.** The snapshot records `/proc/stat` `btime`. Live `btime` must match, or the
    recorded PIDs, start ticks and daemon identity are not comparable (a reboot makes a
    coincident tick offset meaningless) and the action is refused.
  * **Same daemon (swapon path).** Unit `MainPID` must equal the snapshot's recorded `MainPID`,
    with the same start ticks **and** the same absolute start epoch (`btime + ticks/Hz`) in the
    same boot. A restarted unit is a fresh CUDA context: the swapon path refuses.
  * **New daemon (unit-start path)** is a different, legitimate identity: it must have started
    **after** the restore-intent timestamp this tool recorded, in the same boot as that intent
    (`restore_intent_btime`). A daemon started by someone else, earlier, refuses.
  * **Socket identity.** `/run/nbd-vram.sock` must be a socket object (`S_IFSOCK`) owned by
    uid 0, matching what the snapshot recorded. Existence alone is not identity.
  * **Causal backing proof.** The harness must prove that the daemon it is acting on is the
    process serving that socket. It tries two supported read-only proofs: the daemon's own
    descriptor table (`/proc/<pid>/fd/*` links to the socket) and `ss -x -p` (socket owner pid).
    Both are **root-only**. Measured on this host as a normal operator: `/proc/<daemonpid>/fd`
    is `Permission denied`, and `ss -x` shows the socket with no owning pid; the kernel client
    record in `/sys/block/<dev>/pid` names a pid with no `/proc` entry at all.
    **When no proof is obtainable the harness refuses with `unsupported in V1` and mutates
    nothing.** There is no trust flag, no manually asserted boolean, no fake-only guarantee, and
    no documentation permission to proceed anyway: client presence + socket existence are
    proximate, not causal. Running the harness as the daemon's owner (root) makes the proof
    obtainable; running it as this operator does not, and on this host real maintenance stays
    blocked.
  * **Recovery launcher.** The launcher must be recorded with its full sha256 (`files` entry
    labelled `launcher`). A snapshot with `launcher=missing` refuses every action path.

## Operator sequence (the primary runs this alone, in a bounded window)

The numbered sequence below is the shape of a window, **not an authorization**. Real maintenance
stays forbidden on this host until every prerequisite passes: the causal backing proof (root
access, see above), a proved recovery launcher, the RAM capacity guard, the independent Gate 1
result, the Oracle gate, and the primary's own decision. It uses a **fresh, current, verified
snapshot** taken in the same boot the window runs in - never the 2026-1025 baselines below, and
never the v023 launcher.

 1. Preparation, this lane: capture the baseline, run the fake-backend tests, read the report.
    Finish **all** subagents first: every helper must run while prod and the NVIDIA-backed swap
    are up.
 2. An independent primary (not a subagent) confirms the captured baseline is the build it
    intends to maintain, and the Oracle gate has passed.
 3. Open the bounded window. Announce it. No reboot, no suspend, no `systemctl suspend`, no
    `systemctl reboot` during the window (`nbd-vram-sleep.sh pre` tears swap down on suspend and
    the resume path re-reserves VRAM).
 4. TERM the wrapper FIRST, never KILL, and let the engine drain (~5 minutes):

    ```sh
    ss -H -ltn 'sport = :8009'                       # note the PID owner first
    kill -TERM <wrapper-pid>
    ```

    Then require: `ss -H -ltn 'sport = :8009'` empty **and** the engine PID gone (PID + start
    time from the snapshot, so a reused PID is not mistaken) **and** GPU used returned to its
    baseline. A literal `0 MiB` is not expected: display and driver allocations remain.
 5. Recheck live memory, then plan the drain. The budget is the caller's explicit post-shutdown
    number; free disk swap is not a substitute for RAM. `<snapshot>` means a **fresh capture
    taken in this boot with the launcher proved** - the `baseline-20261025*.json` names used in
    this file are historical examples, not files to run:

    ```sh
    grep -E 'MemAvailable|SwapFree' /proc/meminfo
    python3 tools/mixed_gpu/maintenance.py drain \
        --snapshot <fresh-current-snapshot> \
        --mem-budget-kib 8000000 --staging-kib 2097152 --opstate build-mixed-gpu/opstate.json
    ```

    No `--execute` = plan only, nothing runs.
 6. Drain the confirmed device with the backing daemon alive, then verify:

    ```sh
    python3 tools/mixed_gpu/maintenance.py drain --snapshot <snapshot> \
        --mem-budget-kib 8000000 --staging-kib 2097152 \
        --opstate build-mixed-gpu/opstate.json --execute
    python3 tools/mixed_gpu/maintenance.py verify --snapshot <snapshot> --expect drained
    ```

    `exit 3` = drain aborted: swap backing is still running, nothing was cleaned. Free RAM and
    re-run step 6. `exit 5` = a later step (disconnect, stop, sysctl) is not proved: the record
    names the exact step, nothing beyond it ran, and re-running the same command resumes there.
    Never respond with `swapoff -a`, `kill -9`, `systemctl stop --force`, `nbd-client -d` by
    hand, or `reboot`.
 7. Run the window's test work (coexistence/parity, helper-off CPU/I/O opportunity) as planned.
    No automatic feature enabling, no long stress, no benchmark marathons in this window.
 8. Release CUDA (stop the helper and any CUDA client) and confirm the device is free:

    ```sh
    nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader
    ```

    Expect no compute clients beyond what you will name with `--allow-compute-pid`.
 9. Restore only what this tool offlined, from its own opstate + the captured snapshot:

    ```sh
    python3 tools/mixed_gpu/maintenance.py restore --snapshot <snapshot> \
        --opstate build-mixed-gpu/opstate.json --execute \
        --confirm-cuda-released --expect-prod-stopped
    python3 tools/mixed_gpu/maintenance.py verify --snapshot <snapshot> --expect restored
    ```

    Two restore paths, chosen from proven live state, never guessed:

    * **Backing daemon alive and its device connected** (same daemon the snapshot measured,
      same start time): `swapon <dev> -p <captured priority>` only. This allocates no new VRAM.
    * **Unit stopped, device not connected**: `systemctl start <unit>`. Its own connect script
      connects, `mkswap`s, `swapon -p 1500` and **re-picks the first free `/dev/nbdN`**, so the
      device may renumber (`/dev/nbd0` → `/dev/nbd1`). The tool re-derives the device from the
      live `/run/nbd-vram-dev` afterwards, binds the new unit/client/socket, and verifies the
      new row's priority and the sysctls. It never swapons the old name and never guesses.

    **The unit start has a non-bypassable free-VRAM gate.** It uses the captured NVIDIA **UUID**
    (a missing UUID, a changed UUID, or unreadable `nvidia-smi` refuses) and requires
    `free >= reservation + margin` on that exact card. The reservation number is the value the
    installed unit file itself declares - `Environment=VRAM_SETUP_SIZE_MB=7800` read from
    `systemctl show vram-swap-nbd.service` - treated as **decimal MB** and converted upward to
    MiB: `ceil(7800 * 1000000 / 1048576) = 7438 MiB`, plus `--vram-margin-mib` (default 512 MiB).
    That is the unit's declared number, not a measurement of what the daemon allocates: no run in
    this lane measured the daemon's allocation, so the harness never infers precision from the
    variable's name and never treats the sum as exact - it only ever *requires at least* that
    much free, refuses when the GPU read is unknown, and refuses on an 8192 MiB card when a
    helper holds 4000 MiB. `--allow-compute-pid` names expected clients; **it never relaxes
    capacity** - it cannot create free VRAM, and it is not accepted for the unit-start gate at
    all. There is no capacity override flag and no `0 MiB` literal expectation anywhere: display
    and driver allocations stay, and a drained baseline is judged by the drop against the
    captured per-UUID usage, not by an invented target.

    `restore` exits 5 (`restore-swap-unproven`, `restore-sysctl-failed`, `restore-unproven-final`)
    when a command succeeded but the swap entry, priority, client, marker or sysctls are not
    proved: the record is **kept** as the recovery record and re-running `restore` resumes from
    the live state. On success the record is kept too (`phase=restored`, with the device it
    proved) - it is a receipt, not a marker to delete.

    `verify --expect restored` PASSes only when every predicate below is proved, and prints the
    list of what is missing when it FAILs:

    * the device to check comes from the **validated, snapshot-bound operation record**
      (`schema`, `snapshot_sha256`, unit, device, `phase=restored`, `restored_device`,
      `restored_priority`); an unreadable, malformed, non-object, foreign or not-restored record
      is a **refusal (exit 2)**, never a raw-JSON fallback and never a guessed old target;
    * the live `/run/nbd-vram-dev` names **that** device (a marker naming the old name fails);
    * `/proc/swaps` holds it at the recorded priority; the unit is proven `active`; the sysfs
      client record is present; `/run/nbd-vram.sock` is a socket object owned by uid 0 matching
      the capture; the sysctls equal the captured 150 / 1048576 KiB (not the stop defaults
      60 / 65536 KiB);
    * the live daemon is bound to this restore (the captured daemon, or a daemon started after
      this record's restore intent in the same boot) **and** the causal backing proof succeeds.

    `verify --expect drained` PASSes only when: the device is absent from `/proc/swaps`, the unit
    is proven inactive, `/run/nbd-vram-dev` is proven absent (ENOENT, not "unreadable"), the sysfs
    client record is absent, the sysctls match the capture, the recorded prod processes are gone
    (PID + start time, PID reuse refused), the endpoint port is proven closed, **and** the NVIDIA
    backing release is proved against the captured per-UUID usage. A PASS is the proof of all of
    them; printing facts that are not checked is not a PASS. The tool never `swapon`s a device
    with no verified backing.
10. Restart **the exact launcher the snapshot records** (full sha256 in the snapshot's `files`
    entry labelled `launcher`), not a variant, not a path remembered from an older baseline. If
    the current snapshot records `launcher=missing`, the recovery entry point is **not** known:
    the window does not proceed - the primary identifies the launcher, captures again, and the
    independent gate re-checks.

    Historical example only (the lane's first baseline, **not** a recovery permission and not an
    executable default): `launch-prod-swift15-dflash-w4-q4_0-v023.sh` (sha256 `f74792cf…e0a3`),
    the v023 launcher, which the current live setup does **not** use:

    ```sh
    # HISTORICAL EXAMPLE - do not run this as a restore step
    bash /mnt/user/Strata/worktrees/v023-merge/launch-prod-swift15-dflash-w4-q4_0-v023.sh
    ```

    It refuses if `:8009` is occupied and strips `STRATA_DF_*/DFLASH_*/DUMP_*/WINDOW_*`. Send one
    request and one tool call before declaring the window closed.

    **If the snapshot records the launcher as missing, the tool refuses (exit 2) and the primary
    must identify the launcher first.** This is live now: production was restored externally on
    2026-10-25 to `/mnt/user/dflash2/Strata-integ/build-dfsample-1/strata` with
    `/mnt/user/Strata/prod-config-swift15-dflash-w4-q4_0.json`
    (sha256 `9aa2ad2d…3656`), and its wrapper's parent is `systemd --user`, so no launcher script
    is provable from `/proc`. The r2 snapshot records `launcher=missing` on purpose; it is a
    baseline, **not** a recovery authorization.
11. Rollback path, kept for instant fallback (not a default restore): the incumbent
    `/mnt/user/Strata/launch-prod-swift15-dflash-w4-q4_0.sh` with
    `/mnt/user/Strata/prod-config-swift15.json` (launcher sha256 `3f8653c7…0cfb`).
12. Restore prod first, then delegate the post-review. Delegation never runs during an outage.

## Safe commands for this lane (no GPU, no mutation)

```sh
python3 -m unittest discover -s tools/mixed_gpu -p 'test_*.py'      # fake backend only
python3 tools/mixed_gpu/maintenance.py --help
python3 tools/mixed_gpu/maintenance.py capture --help               # and drain/restore/verify
python3 tools/mixed_gpu/maintenance.py capture \
    --out build-mixed-gpu/baseline-20261025-r2.json \
    --live-root /mnt/user/dflash2/Strata-integ \
    --binary /mnt/user/dflash2/Strata-integ/build-dfsample-1/strata \
    --config /mnt/user/Strata/prod-config-swift15-dflash-w4-q4_0.json \
    --build-dir /mnt/user/dflash2/Strata-integ/build-dfsample-1 \
    --file /mnt/user/Strata/launch-prod-swift15-dflash-w4-q4_0.sh \
    --file /mnt/user/Strata/worktrees/v023-merge/launch-prod-swift15-dflash-w4-q4_0-v023.sh \
    --unit vram-swap-nbd.service --port 8009
python3 tools/mixed_gpu/maintenance.py drain \
    --snapshot build-mixed-gpu/baseline-20261025-r2.json \
    --mem-budget-kib 8000000 --opstate build-mixed-gpu/opstate-r2.json   # plan only: refuses
```

Plan-only `drain` on this host currently refuses (exit 2): the causal backing proof is root-only
(`unsupported in V1`), the r2 snapshot records `launcher=missing`, and the RAM budget guard
refuses at the measured `MemAvailable`. All three refusals are the intended state: they are the
prerequisites, not bugs to route around, and r2 is a baseline capture, not recovery permission.

`capture` writes only `--out` (mode 0600) and refuses to overwrite an existing file. Keep
snapshots under `build-mixed-gpu/` (ignored by `build*/`). A snapshot is evidence: full
unabbreviated sha256 of binary/config/launcher, CMake keys, process identities
(PID + start ticks + exe + cwd, whitelisted args), `/proc/meminfo`, `/proc/swaps`,
`/run/nbd-vram-dev`, `/run/nbd-vram.sock`, the sysfs NBD client record, unit properties
(MainPID + start ticks + Environment), sysctls, bounded NVIDIA/AMD reads (UUID, total, used,
compute apps). Secrets are not serialized: process environments are never read; flag values are
kept only for a whitelist of plain engine settings. The config is referenced by path + hash, not
copied.

## Stop conditions

Stop and report instead of improvising when any of these appear:

  * `exit 2` (refused) by any command: a prerequisite is unresolved - prod process live or a
    reused prod PID, port listening (the port is bound to the snapshot's recorded port),
    stale/mismatched device, unprovable launcher/config/binary hash, unbounded sysctl, missing
    budget, negative staging/margin/timeout, zero budget, low `MemAvailable`, unknown snapshot
    field, missing NVIDIA UUID, insufficient free VRAM, unexpected compute client, missing,
    foreign, unreadable, malformed or non-object operation record, a snapshot from another boot,
    a daemon whose pid/start epoch/binding does not match, a socket that is not a uid-0 socket,
    or an unprovable causal backing link (`unsupported in V1`). Refusal is the **pre-action**
    exit: it happens before the first mutating command, so nothing was mutated. A failure found
    after a mutation never exits 2 - it exits 5 with the record kept.
  * `exit 3` (drain): swapoff failed, the entry stayed, or `/proc/swaps` became unreadable
    after the swapoff. Backing is running, the marker is kept, nothing else ran. RAM first,
    then retry the same command.
  * `exit 4` (verify): the requested state is not proved, or a required read is unknown.
    `verify` never PASSes on unknown, and `--expect drained` includes the NVIDIA backing
    release: an unverified release is a FAIL, because that check is the runtime readiness gate
    for the CUDA window. `verify --expect restored` refuses (exit 2) when the record it would
    check is not a validated snapshot-bound receipt.
  * `exit 5` (partial): a step is proved incomplete, **including a late read failure after a
    mutation** (an unreadable swap table, marker or sysctl at that point). The record names the
    step. Re-run the same command; do not delete the record, do not hand-run the step, and do
    not clean up afterwards.
  * `exit 6` (internal): the harness met a value it did not expect (a programming-level
    surprise, reported with the exception type). No cleanup is attempted, the operation record
    on disk stays the truth, and the run is reported - not a traceback and not a "refused,
    nothing mutated".
  * Any command the harness reports as `unknown`: treat as unknown, not as safety.
  * Do not add `swapoff -a`, do not target a device other than the reported one, do not edit the
    installed scripts/unit, do not format disks, do not `swapon` other devices.

## Known evidence limits (stated, not papered over)

  * `/sys/block/<dev>/pid` is a **kernel NBD client record**, not a userspace process: measured
    on this host as pid 4569 with no readable `/proc/4569` entry, and the connect script's
    `nbd-client` exits after connecting. So the client is bound by **presence** plus the unit's
    socket `/run/nbd-vram.sock` (exists, uid 0) plus the daemon identity (unit MainPID,
    argv[0]=`nbd-vram`, start ticks). Process-level proof of the client is not obtainable for a
    non-root operator, and the harness says so instead of inventing a guarantee.
  * `/proc/<daemonpid>/exe` is permission-denied for a non-root operator (measured); the daemon
    is identified by `/proc/<pid>/cmdline` argv[0] and start ticks, and the snapshot records
    `main_exe_status: unknown` in that case.
  * **The causal device→socket→daemon link is not provable for a non-root operator on this
    host** (measured 2026-10-25): `ls /proc/<daemonpid>/fd` → `Permission denied`; `ss -x`
    lists `/run/nbd-vram.sock` with no owning pid and `ss -x -p` shows users only for the
    operator's own processes; `/sys/block/nbd0/pid` reported 4780 with no `/proc/4780` entry.
    The harness therefore treats causality as a **capability probe**: with root access the
    proofs succeed and the actions proceed; as this operator the action paths refuse with
    `unsupported in V1`. Presence-level facts (socket exists, client record present, unit
    MainPID is a `nbd-vram` process) are recorded as proximate evidence and are never claimed
    as causality.
  * `btime` is used as the boot identity; measured on this host as `1792947786` at the r2
    capture, i.e. the host has rebooted since the earlier baselines, which is exactly why
    snapshot start times are compared with boot identity and absolute epoch, not tick offsets.
  * `build-mixed-gpu/baseline-20261025.json` and `baseline-20261025-r2.json` are historical
    captures (r2 records `launcher=missing` deliberately). Neither is a recovery authorization,
    and neither is comparable to a host that has rebooted since: capture in the window's boot.
  * `nvidia-smi -pm 1` **is** classified as a mutation (it changes persistence mode). This tool
    never runs it; the classification exists so the tests and report describe reality.
  * `is_mutating()` is a **classifier** for tests and reports, not an enforcement layer: the
    guarantee is that this module builds only its own argv shapes, with no `shell=True`, no
    shell strings, no sudo and no user hooks.

## Not validated (explicit)

  * No hardware coexistence, no real-CUDA/HIP parity run, no benefit measurement, no benchmark.
  * No drain/restore was executed on the real host in this lane; the mutation paths are
    exercised only by fake-backend tests. Real-host runs performed here were `capture` (read-only)
    and plan-only `drain`/`verify`, which refused correctly while prod and the swap were up.
  * SIGTERM semantics of the running daemon during a stop after a verified drain are unproven;
    the harness avoids depending on them by disconnecting before the stop.
