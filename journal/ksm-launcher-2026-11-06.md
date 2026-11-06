# KSM launcher trial — 2026-11-06

## Scope and evidence already collected

- Production launcher: `launch-prod-swift15-f16kv-dflash-w4-q4_0-mixed.sh`.
- KSM is globally enabled but the inspected model processes were not mergeable;
  `pages_scanned`, `pages_sharing`, and `general_profit` were zero.
- The inspected main host had about 38.5 GiB of resident anonymous memory; the
  expert helper was predominantly file-backed. Duplicate anonymous content has
  not been established, so no memory savings are promised.
- A short-lived `systemd-run --user -p MemoryKSM=yes` probe reported
  `ksm_merge_any: yes` and `ksm_mergeable: yes` (1.9 MB peak). It did not touch
  production. This establishes kernel support, not model-memory savings.
- Earlier zram testing was abandoned by the user after allocation stalls;
  zram's measured peak physical allocation was about 6.8 GiB. Do not re-enable
  compression as part of this experiment.

## Initial opt-in preparation (94cb9214)

Add `STRATA_KSM=1` as an opt-in launch setting. A short-lived Python bootstrap
calls `PR_SET_MEMORY_MERGE`, then replaces itself with the original Bash launcher.
It preserves the working directory, exported environment, PID, and arguments,
including `--recover`. Clearing the opt-in environment variable prevents a
bootstrap loop; the kernel setting remains inherited by children. This avoids
systemd environment changes and the launcher's deliberate `LD_PRELOAD` removal.
Without the flag, the existing launch path remains unchanged. No engine,
helper, model configuration, or global VM setting is changed.

Validate the actual launcher with an exported `ss` test function that always
reports an occupied port. Both normal and KSM runs must refuse startup before
starting helpers or touching supervisor state. The function reports its child's
KSM status, and `strace` checks the opt-in syscall and re-exec. Do not pass
`--recover` during this guard-only check, since that delegates before the guard.

## Real-model test — pending manual restart

The operator must use the usual controlled stop/restart procedure, preserving
the normal arguments. Per the follow-up request below, KSM is now requested by
default; `STRATA_KSM=1` may also be set explicitly.
Do not start a second model stack while production occupies port 8009.

1. Record the model/configuration, KSM settings and counters, MemAvailable,
   paging/allocstall counters, and representative inference timing before restart.
2. Verify the restarted model PIDs actually report KSM opt-in before scanning.
3. Measure `general_profit` (approximate net bytes saved after KSM metadata),
   per-process `ksm_stat` and PSS, `ksmd` CPU use, paging, and allocation warnings.
   Keep the model settings and comparison workload unchanged.
4. Start with an idle-machine pilot. Pause scanning if a new allocation-stall
   warning/OOM occurs, available memory falls below the chosen 8 GiB pilot
   safeguard, or responsiveness deteriorates. The threshold is a test budget,
   not a kernel reserve or a universal recommendation.
5. Pause with `echo 0 | sudo tee /sys/kernel/mm/ksm/run`; this retains merged
   pages. Never use `run=2` or force live unmerging under memory pressure.
6. A low-savings result before repeated complete scans is inconclusive. Only
   keep the option if net savings justify scanning and copy-on-write overhead.
   Background-load impact still requires actual frame-time measurements.

To opt out, use `STRATA_KSM=0` on the next fresh launcher start from a non-KSM
parent. This skips enabling KSM; it does not unmerge a running process or clear
KSM already inherited from a parent.
No production restart or global scan-setting change is authorized by this
preparation alone. Actual model savings and performance remain unmeasured.

## Initial opt-in validation (94cb9214)

- User scope confirmed: add KSM to the launcher, do not touch production yet.
- Branch: `ksm-launch`; unrelated runtime files were left untouched.
- `bash -n` and `git diff --check`: passed.
- Guard-only runs of the HEAD reference, updated default, explicit
  `STRATA_KSM=0`, and `STRATA_KSM=1` all exited 1 at the forced occupied-port
  guard, before any helper/server startup or supervisor-state access.
- The HEAD reference, updated default, and explicit-zero output were
  byte-identical; their child reported KSM opt-in disabled.
- The opt-in child's `/proc/self/ksm_stat` reported both `ksm_merge_any: yes`
  and `ksm_mergeable: yes`, establishing inheritance through re-exec and fork.
- `strace` recorded exactly one successful
  `prctl(PR_SET_MEMORY_MERGE, 0x1, 0, 0, 0) = 0`, followed by re-exec of the
  original Bash interpreter with the test argument preserved. No bootstrap loop.
- Production model/vision/helper PID lists before and after were identical.
  No production restart, global KSM change, or inference benchmark was performed.
- Receipts: `logs/ksm-launcher-20261106-120249-{reference,default,zero,optin,trace,pids-before,pids-after}.log`.

## User follow-up: default on

The user explicitly requested KSM on by default in this launcher, without a
production restart. Unset `STRATA_KSM` now requests KSM, as does an explicit
`STRATA_KSM=1`; an explicit `STRATA_KSM=0` skips the bootstrap.

Use a separate inherited `STRATA_KSM_BOOTSTRAPPED=1` marker to prevent a re-exec
loop. Clearing `STRATA_KSM` no longer works as a recursion guard once its
default is on. The marker also avoids repeating the bootstrap on supervised
child launches, which inherit the kernel setting.

This is an operator-selected local default, not a measured performance
improvement. Actual model-memory savings and inference/background-load impact remain
untested. Running production and global KSM settings remain untouched.

Default-on guard-only validation passed:

- `bash -n` and `git diff --check` passed.
- Old default, new default, explicit zero, and explicit one all exited 1 at
  the forced occupied-port guard; no model stack was started.
- New default and explicit one produced identical output with
  `ksm_merge_any: yes` and `ksm_mergeable: yes` in the child.
- Explicit zero produced output identical to the prior default-off launcher,
  with both flags disabled.
- Each traced enabled run made exactly one successful
  `PR_SET_MEMORY_MERGE` call: the inherited marker prevents re-exec loops.
- Production PID lists and global KSM scan settings were identical before
  and after validation. No running production process was modified.
- Receipts: `logs/ksm-default-20261106-121718-*`.
