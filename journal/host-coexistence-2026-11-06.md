# Background-load coexistence trial — 2026-11-06

## Scope and approval

- User asked to speed inference up while the hostapp remains running, then
  required advance notice of changes. No runtime tuning had been applied at that point.
- User explicitly approved one live test: move only CPU-0 `strata-stage` threads
  to logical CPUs `3,5,7,9,11,13,15`, saving the original affinities for rollback.
- Do not restart either workload, change the model/configuration, enable compression,
  tune KSM, or change hostapp scheduling without presenting that separate change first.
- Work branch: `host-coexistence`, created from `ksm-launch`. Pre-existing modified
  supervisor state and untracked runtime files are not part of this work.

## Read-only observations before the trial

- Live engine PID 2533394, helper 2532770, server 2533023, launcher 2532650.
  The live model is **swift-1.5-iq3_xxs-f16kv**, not the IQ3_S instance mentioned
  in an earlier journal. It uses the frozen `v0210` HIP engine and CUDA helper,
  262144 maximum context, FP16 KV, prefill 16384, and two Radeon VIIs plus the
  RTX 3080 Mobile helper. The hostapp PID is 3718120; it renders on the Cezanne iGPU.
- Ryzen 9 5900HX, 8 physical / 16 logical CPUs, kernel 7.2.9-x64v3-xanmod1.
  SMT siblings are adjacent logical CPUs. Strata host=0, expert workers=2,4,...,14.
- `vmstat -w 1 6` interval samples at about 15:27 showed 113312–136816 KiB/s
  swap-in, with 41–47% CPU idle. Large swap occupancy alone is not the claim;
  active paging was observed. Strata held roughly 45 GiB RSS and 1.5 GiB swap.
- A later system-wide `pidstat -d -h 1 3` attributed 66527 and 208412 KiB/s
  reads to a `hindsight-api` worker, while hostapp/engine reads were small. This
  identifies a competing background workload, not proof it caused the delay.
- Memory safeguards remain active: min_free_kbytes=524288 and
  watermark_scale_factor=100; swappiness=100. No VM settings were changed.
- KSM is running (100 pages per 200 ms); at inspection it had completed 22
  full scans. Strata reported 0 merging pages and process profit -169087168 B;
  global profit was -188058752 B. This is an observation of the current pilot,
  not an authorization to change the operator-selected KSM setting.
- Host-thread `pidstat` samples showed about 84% scheduling wait; dozens of
  `strata-stage` threads inherited its CPU-0 mask. A later two-second sample
  showed 66% host wait and all visible staging threads still on CPU 0.
- At 15:30 `/metrics` reported prompt 76902, read=73804, elapsed=1683.6 s,
  with both compute GPUs idle. The live log also reported a draft-ring rewind
  requiring a 73804-token reread and a closed prompt-helper channel, falling
  claimed rows back to the CPU. These are distinct costs; affinity is not
  asserted to fix either condition.
- Before any tuning, that request recovered by itself: its completed metrics
  report prompt_ms=1762703.6 and decode=27.2 tok/s. The next request was
  generating at about 23 tok/s. Do not attribute that recovery to this trial.

## Evidence plan

- Use the same live engine and hostapp. Snapshot and verify per-thread affinity;
  preserve the host and expert-worker placements.
- Compare short live telemetry windows before/after, then restore and verify the
  saved placements. No synthetic requests: they could evict the active long
  conversation and trigger a costly full prefill.
- Live traffic/hostapp state are uncontrolled, so token-rate movement alone does
  not establish a speedup. A controlled same-input/output-equivalence A/B remains
  necessary before making a performance claim or changing startup defaults.
- New tool: `tools/mixed_gpu/staging_affinity.py`, Linux-only, explicit opt-in,
  snapshot-before-mutation, apply/readback/restore. No engine source edits/builds.
- Sudo requires a password; PSI is unavailable on this kernel. Serena and the
  memory recall service were unavailable; the on-disk journals were used.

## Trial result

- Engine SHA256: `66446e2181bd52430fdb8ccec26cf2c22faace8de100925a5afb8b7d4d72243b`;
  helper SHA256: `5a949542c04607537df18f2baabd6c66cf9808ed85b0c5fb860bac232f2d459e`.
  Neither binary changed.
- The original engine exited **before application**. Its inspection failed on
  the missing `/proc/2533394/comm`, without changing any affinities. The wrapper
  log reports verify timeout at layer 7, waits released but GPU did not finish
  within 5 s (#267), followed by automatic server-driven engine restart.
- The helper also exited. Its log reports a second-channel helper timeout,
  followed by primary-channel closure and release of the card. The replacement
  engine PID 233080 logs `helper connect failed` and serves those experts on
  the CPU for the whole run. It cannot be compared with the earlier helper-on
  regime. No restart or helper recovery was performed by this session.
- At 14:37:29 UTC the explicitly approved trial moved **8** `strata-stage`
  threads of engine 233080 from CPU 0 to `3,5,7,9,11,13,15`. Snapshot:
  `logs/host-affinity-20261106-233080.json`. Readback confirmed only the
  selected threads moved; host=0 and the seven pool-worker masks were unchanged.
- A five-second control sample reported host scheduling wait 99.8%; the
  subsequent ten-second trial sample reported 1.4%. Hostapp CPU changed from
  108.2% to 292.4%, so this is not a controlled speed comparison.
- Both control/trial `/metrics` snapshots still reported the same 80144-token
  prompt with no first-chunk count, at elapsed 117.3 and 160.7 s respectively.
  No token-throughput improvement was established in that window.
- Restored all **8** saved affinities, and inspection confirmed the staging
  threads were back on CPU 0. No runtime affinity change was retained.
- The user subsequently observed PP rising. A later readback confirmed
  prompt_read=15176/80144, reported mean PP=94.2 tok/s, both GPUs at 100%.
  This was after restoration; attribution to affinity remains unproven.
- Checks passed: Python compile; live inspect/apply/readback/restore/readback;
  `git diff --check`. No controlled same-input parity or inference speed gate
  was run. No startup/default changes were made.
- Receipts: `logs/host-affinity-20261106-{before,control,trial}-metrics.json`
  and the affinity snapshot above; interval samples are in the session output.

## Follow-up: favor the hostapp slightly

- User requested a small loadend-inference priority advantage, while keeping
  the requirement to present changes first.
- The hostapp and engine share the same terminal cgroup. Changing that scope's
  CPUWeight would affect both, not prioritize one over the other.
- Live hostapp-thread nice distribution: 4 at -10, 1 at -2, 12 at -1, 129 at 0,
  1 at +2, 18 at +3, 19 at +19. Preserve these role-specific priorities.
- All 37 engine threads sampled were nice 0. Proposed live-only adjustment:
  engine threads 0 -> +1; leave the hostapp, kernel workers, GPU configuration,
  affinity, and startup configuration unchanged. This changes CPU contention
  weights, not a CPU quota or guaranteed frame rate.
- Engine RLIMIT_NICE=30. A disposable `nice -n 1 nice -n -1 nice` process
  returned 0 without a privilege error: a +1 -> 0 rollback is permitted here.
- At proposal time no live nice-priority change had been applied. Helper recovery
  requires separate authorization; it is not included in this priority proposal.

### Approved live application

- User selected **Apply live +1**. Saved the engine's original per-thread nice
  values in `logs/host-priority-20261106-before.txt`, and hostapp values in
  `logs/host-priority-20261106-hostapp-before.txt`.
- Verified PID 233080 still named `strata-host` (started 15:33:51); applied nice
  +1 only to its snapshotted nice-0 threads. Two short-lived threads ended before
  their `renice` operation, producing `No such process`; every remaining thread
  was then checked. **All 36 live engine threads reported nice +1.**
- Saved after snapshots in the corresponding `-after.txt` files. Comparing
  surviving hostapp TIDs reported **zero changed priorities**.
- Affinity inspection confirmed host=0, workers=2,4,...,14, all 8 staging
  threads=0; the staging-affinity trial remains rolled back.
- `/health` returned loaded/ok. Later live telemetry reported the same prompt
  at 80139/80144 tokens, mean PP 107.6 tok/s. This is progress evidence, not
  an attributable nice-priority speedup or a measured hostapp-frame-time result.
- Retained runtime change: **engine nice +1 only**. No hostapp/engine restart,
  launcher/configuration edit, KSM/VM tuning, or helper recovery was initiated.
  This live adjustment resets on engine restart (`STRATA_ENGINE_NICE` stays 0).
- Rollback is permitted by the verified nice limit. Before restoring saved
  per-thread priorities, verify this engine's identity (PID 233080, start tick
  1846487); do not reuse stale TIDs after a restart. All saved original values
  were 0; inspect newly created engine threads as well if rolling back later.
- Pre-existing modified supervisor files and untracked runtime files were left
  untouched. Final documentation is committed separately from the trial tool.

## Wired into pair startup (2026-11-06 evening)

The trial above proved the engine re-pins staging threads to CPU 0 at every
start, so a runtime move dies with each supervisor recovery generation. The
move is now startup work: `pair_supervisor.py run --stage-cpus 3,5,7,9,11,13,15`
(what all six prod launchers pass) applies `staging_affinity.py apply` for the
discovered engine right after `attached`, every generation, snapshot-first,
results in the state JSON under `stage_affinity`. An affinity miss never
restarts the pair. `--single-run` runs without it. Details and limits:
`docs/PAIR_SELF_HEALING.md`, section "Stage-thread affinity at attach".

Night follow-up: the first live pair hit a startup race - the engine renames
itself `strata-host` only AFTER early serve startup, so the attach-time attempt
met comm `strata` and failed as permanent. Fixed: that error is retryable,
30 attempts; applied by hand to the live pair (64 threads moved); the fixture
now delays its rename to regression-test the window.
