# Conversation parking/loading — bounded GPU acceptance window

Status: preparation only; GPU execution is paused for the operator-requested passive investigation of persistent slowdown after the iGPU game exits. Preserve that state until the investigation is reconciled. Operator separately requested this GPU test after the whole-plan oracle finished. No promotion is authorized. Reuse the existing runners and cache scenarios; do not build another window framework.

## Frozen candidates and authority

| Arm | Source | Engine SHA256 |
|---|---|---|
| Prior production behavior | `42cb41b314` | `b0ec0109ebc50c982512c371d81b2c15a8f72c70edf8a461a4ff10337da52f02` |
| Approved RAM source | `af1e64a5cb4b617d52c0f9b416af76d28c42bc4f` | `8f19baaf150237e5d8085c12c842a4b5728d10bd00ce315ab0ed52ff9bd819ed` |

Parent rehashed both binaries, checked candidate HEAD and its clean worktree, exit 0. The candidate is the already-built `/mnt/srcds/Strata-wt-integ/build-mixed-gpu/rpcw-hip/strata`; no rebuild or deployment is required to identify it. Canonical rollback remains `/mnt/srcds/Strata/launch-v0238.sh` with the original config and production binary.

## Claims, owners and minimum evidence

| Claim | Owner | Decisive evidence |
|---|---|---|
| Real GPU park/load remains correct over repeated pressure-relevant switches | Parent window | At least 20 declared test turns per arm with pinned synthetic histories, normal finite completions, raw per-request cache/reuse/read and memory evidence. Warm-up and calibration are separate, never counted toward the 20. |
| Reclamation reaches parked donors after file advice is insufficient | Parent window | Candidate admission/eviction lines naming file advice, donor count, rounds and measured availability separately. No inferred reclaimed bytes from advised sizes. If live logs expose only aggregate deltas, do not claim per-round zero telemetry was observed; reuse the existing CPU no-progress test for that narrower invariant. |
| Headroom protection is bounded and safe, not an OS reservation | Existing CPU/source evidence plus parent window | Named-limiter boot line; unchanged **2560 MiB** floor; real spend/refusal/restore engagement. More safe misses or shorter recovery depth are reported, not hidden. |
| Skip/recovery behavior is compared fairly | Parent window | Same effective configuration, pressure cap and persisted inputs outside the engine executable. Compare generic park/checkpoint/restore refusal counts, reread tokens and published depth on both arms. Fix-only `restore headroom`/`admitted after` strings prove engagement; baseline has no such category, so a lower fix-only count is not a valid comparison. |
| Restart selects the expected deepest compatible published state | Parent window | Record published-file identity and checkpoint metadata before stopping; restart the same isolated arm/cache; assert the actual restore boundary against the compatible published boundary, not merely `reused > 0` or an upper bound. Include a rewind/change-of-tail fixture when required to exercise checkpoint selection rather than only the live image. |
| Tested behavior is attributable and reversible | Parent window | Exact engine/helper/wrapper/config/input hashes, live executable/effective environment, per-boot log offsets, isolated cache paths and finite deadlines. Restore original v0238, verify fresh readiness, binary identity, models and a real completion at window exit. |

## Resource-safe pressure and limits

Use an existing isolated engine memory-limit control, not a host-wide memory hog or OS/GPU reset. `sample_host_memory()` combines host MemAvailable, cgroup room and the explicit cap; the cap can induce real admission/reclaim decisions without exhausting physical RAM (`conversation_memory.cpp:207-240,284-332`). Leave the 2560 MiB floor unchanged. Any calibration must be finite and separately logged; freeze one common final configuration before comparing arms. Name all test-only changes, including any RAM-cache ceiling needed to retain eligible donors.

This is **cap-adjusted pressure on the real GPU serving path**. It is not a reproduction of OS direct-reclaim/swap collapse. A pass can establish park/load/reclaim/restart behavior under that declared condition; it cannot close genuine host-collapse resilience by implication. Abort on unsafe real host memory/swap conditions, malformed state, abnormal process exits or request deadlines. A timeout is not server cancellation: stop the isolated arm before retrying.

## Reuse inventory and remaining preparation

- Reuse `gpu-rounds/champions_panel.py:Panel.boot`, `stop_all`, `_pump_all`/`_pump` and the stop/restore machinery for lifecycle, identity and NUL-stripped live logs. Reuse `ab9_driver.py:MemSampler` for memory and `champions_log.py:parse_window` for actual reused/read tokens. Do not inherit the champion fresh/cached pair loop, fixed shapes or speed-winner summary as RAM acceptance assertions.
- Parent inspected the implementation after the focused harness review. `Panel.boot` currently collects `changed_path_evidence` from the **wrapper** boot log (`champions_panel.py:214`), while the actual RAM-policy line is in the engine log. A RAM runner must explicitly read its own engine log; the existing empty collector is not a missing-feature verdict. `fresh_run_dir` archives prior per-arm/raw-log evidence, so a restart must keep unambiguous pre-/post-boot artifact references and preserve the same isolated cache.
- Reuse `champions_prepare.py` hashing/retarget primitives, but not its frozen champion manifest/`prepare`/`verify` verbatim for changed test knobs: they deliberately enforce the original champion configs. Acceptance needs a separate run-specific manifest, equal effective environments and explicit pressure/floor/cache pins. Its `quality_keys` list does not independently watch the 2560 MiB floor; preserve and assert that value explicitly.
- `champions_stop.sh` already requires the canonical supervisor-aware stop path; `champions_restore.sh` already gates fresh readiness, live executable path, models and optional real completion. Its output root is currently hardcoded to the old champion run, and it prints rather than independently enforces the expected binary SHA. Preserve its logic, but route future RAM-window evidence to the new run and enforce pinned hashes through the acceptance preflight/identity checks. Do not mutate the frozen comparison to accommodate a new run.
- Existing `tools/conversation_disk_cache_check.py:s5` already drives disposable-pair restart; its default lifecycle/helper handling is not safe to reuse blindly for this mixed-GPU deployment. Reuse scenario semantics under the verified lifecycle.
- Existing `tools/conversation_cache_soak.py` already drives at least 30 fixed known-answer cache cycles with exact state/output checks; `conversation_cache_parity.py` already has pressure and admission assertions. Their cache-budget pressure and cache-disabled reference are not automatically the RAM-guard/baseline-binary comparison required here.
- Add only missing turn orchestration, fix-specific log assertions and a non-vacuous published-depth assertion where existing facilities cannot provide them. Keep CPU tests isolated from real benchmark/cache artifacts; do not modify engine arithmetic or add a production diagnostic API for the test.
- Before execution, save an exact runnable manifest: all commands, identities, ports/sockets, isolated paths, pressure/calibration policy, fixture hashes, turn counts, bounds, expected assertions and canonical restore. Validate support code offline, finish all specialists, then let the parent run the single background GPU lane with live panel/engine output and per-arm reporting.

Preparation is not evidence of a pass. Final verdicts must distinguish passed, failed, unengaged and untested claims; a resource-safe partial result is preferable to an unsupported full-acceptance claim.
