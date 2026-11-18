# Three champion stacks — TG/PP results, 2026-11-18

## Outcome

All three arms completed 9/9 planned pairs: 54 benchmark HTTP requests in total. No engine or config was promoted. Canonical production is back on the original v0238 launcher, config and `b0ec0109` binary; the final restore returned `OK` from a real completion and exited 0 at 06:59.

Long-context PP is effectively unchanged. Latest and Nov-16 have identical long-context TG medians. Short-context TG has substantial variation, and the latest build's lower median must not be hidden, but this panel does not establish its cause. There is no defensible overall speed winner or quality-equivalence claim from this run.

## Frozen identities and method

| Arm | Source | Engine SHA256 prefix | Config snapshot |
|---|---|---|---|
| Latest (`current`) | `af1e64a5cb4b` | `8f19baaf150237e5` | `27728dc1ff:prod-config-v0238.json` |
| Nov-16 | `42cb41b314` | `b0ec0109ebc50c98` | `d2e94b9b08:prod-config-v0238.json` |
| Nov-15 | `81e023bfa0` lineage | `36ecbe983837828d` | `fcd1b55325:prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json` |

The request was made Nov-17; the approved latest build finished Nov-18. The two historical champions remain request-relative Nov-16 and Nov-15. Full binary/helper/server identities, config blobs, launcher snapshots and corpus hashes are in `../out/champions-20261118/manifest.json`, SHA256 `1f876a4dbc6e20f2cc0942262af8c3487eb7b978a201834a2b2ca912edad0595`.

One pair at a time, arm order latest → Nov-16 → Nov-15. Each arm used a tiny warm-up pair, then short/long shapes with one excluded warm-up pair and three measured pairs each. Exact prompt files are shared across arms; each fresh pair has a distinct leading identity. Every pair sends the same prompt twice: fresh input for PP, then cache reuse for TG. Actual contexts were **16008** and **79986** tokens; fresh reuse was always 0, cached reuse **16003/79981**.

PP uses the actual newly read tokens and engine prompt time. TG uses the main engine call's generated-token time, excluding PP. Internal server continuation calls are retained in request windows but excluded from this main-call metric. The driver requested `temperature: 0`, `enable_thinking: false`, `reasoning_effort: low`; actual server logs still show thinking and continuation activity. These are not thinking-disabled or whole-response decode measurements. Generated lengths vary, and output hashes differ even between some fresh/cached responses; neither deterministic outputs nor bit identity were established.

## Throughput

Median tok/s, with measured minimum–maximum in parentheses. Three measured pairs per cell; no measured pair was dropped.

| Arm | PP 16k | Cached TG 16k | PP 80k | Cached TG 80k |
|---|---|---|---|---|
| Latest | 266.3 (257.3–272.6) | 19.7 (16.7–41.8) | 465.3 (418.0–466.0) | 32.6 (32.4–33.7) |
| Nov-16 | 253.8 (251.2–272.9) | 33.1 (32.9–33.6) | 466.2 (448.7–466.9) | 32.6 (32.5–33.6) |
| Nov-15 | 270.5 (266.6–272.9) | 34.0 (13.4–36.3) | 466.1 (465.5–467.1) | 34.4 (27.1–35.5) |

Arithmetic changes versus Nov-16's median, **not causal speedup estimates**:

| Arm | PP 16k | TG 16k | PP 80k | TG 80k |
|---|---|---|---|---|
| Latest | +4.93% | −40.48% | −0.19% | 0.00% |
| Nov-15 | +6.58% | +2.72% | −0.02% | +5.52% |

Fresh-call TG medians (not the cached headline metric): latest **27.2/31.5**, Nov-16 **35.8/34.8**, Nov-15 **32.5/31.8** tok/s, short/long respectively. Client walls and all underlying samples remain in the per-arm JSON records; they must not be substituted for generation-only time.

## Memory and effective stack differences

MiB, sampled during measured pairs. RSS is the engine's high-water mark, not unique physical RAM. Host MemAvailable is not the memory guard's cap-adjusted room.

| Arm | Peak RSS 16k / 80k | Minimum host MemAvailable 16k / 80k | Peak VRAM card2 / card3 / nvidia0 |
|---|---|---|---|
| Latest | 51121 / 53117 | 16214 / 15250 | 16361 / 16186 / 7407 |
| Nov-16 | 52086 / 53794 | 15622 / 15117 | 16361 / 16186 / 7407 |
| Nov-15 | 51493 / 52995 | 16043 / 14527 | 16361 / 16187 / 7407 |

The additional sampled `card4` stayed at 374 MiB. All arms emitted low-free-VRAM warnings; no capacity, quantization, precision or GPU setting was changed for this panel. Swap residue was present and not reset between arms. Full per-leg swap/VRAM readings are retained.

**Correction to the preparation claim:** the retargeted config files match outside the five harness fields, but the complete runtime environments do not. Latest and Nov-16 launchers export `STRATA_MEMORY_LIMIT_MIB=51200` and `STRATA_WATCHDOG_S=120`; the adopted Nov-15 launcher does not. Actual guard boot lines show **50.0 GiB, source flag** for latest/Nov-16 versus **62.2 GiB, source meminfo** for Nov-15. `config_identical_across_arms: true` in the frozen manifest describes config files only, not effective launch environments. Preserve the manifest as run; this observed correction belongs alongside it. This is a **champion-stack comparison**, not a three-way engine-only A/B.

Fixed arm order, few samples, different generated traces/lengths, existing low-VRAM warnings, swap residue and runtime-limit differences limit attribution. Timing variation is observed; a specific page-cache, scheduling, thermal or RAM-policy cause was not isolated. Do not promote a build based on the largest single sample or erase a slow sample as an outlier.

## Validation, restore and RAM-fix limits

| Check | Result |
|---|---|
| Latest measurement | Complete 9/9; process exit 1 solely from the restore checker's trailing-semicolon path bug |
| Latest actual restore | Independently verified canonical `/proc/<pid>/exe` + SHA, fresh ready line, `/v1/models`, real `OK` completion; no additional restart needed |
| Restore parser regression | Fixed in `cea984827c`; test executes the real shell assignment with the launcher's delimiter |
| Nov-16 / Nov-15 panels | Both complete 9/9, process exit 0, automatic canonical restore and `OK` health completion passed |
| Focused harness checks | Parent ran 34 CPU tests, compile, individual shell syntax and pin verification: all exit 0; final post-panel pin verification also exit 0 |
| Selected engine timing counters | Finite for all accepted pairs; helper timeouts/fallbacks 0 in every selected main call |
| Quality equivalence | Not established: this was a throughput panel, not a teacher-forced/logit comparison |

The approved RAM policy **was enabled** in latest's disk tier: the actual engine boot line chose **677 MiB**, limited by restore host ask (file cap 4219, cache quarter 1024, chain 677, ceiling 1024 MiB). `boot.json.changed_path_evidence` is empty because that collector searched the wrapper log, not the engine log containing this line. The raw engine log and panel output retain it. This panel did **not** close pressure-induced reclaim, ≥20-turn reproduction, or restart-at-published-depth acceptance. No unconditional RAM guarantee is claimed.

## Evidence

Root: `/mnt/srcds/Strata/bench/ab-v0210/rpcprof/out/champions-20261118/`.

- `summary/{current,nov16,nov15}.summary.{csv,json}`: all samples, medians/ranges, counts, memory and exclusions.
- `arms/<arm>/{boot.json,legs.jsonl,records/,driver/,supervisor.out}`: exact request hashes, byte windows and execution artifacts.
- `logs/{current,nov16,nov15}.engine.log` and `logs/panel-*.log`: raw engine evidence and live terminal transcripts.
- `final-evidence.sha256`: hashes of all six summaries and three engine logs.
- `../champions-cpu-validation-20261118.log`: final 34-test parent run. Tests now use isolated project-local scratch; the real measurement files passed before/after stat+SHA guards.

Next: fresh read-only TG analysis using old journals, these results and relevant upstream PRs/commits; then independent iterative oracle review. No optimization implementation or additional GPU sweep is authorized by that research step. Zero quality sacrifice remains the acceptance requirement.
