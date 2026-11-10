# Cache-spill A/B on split-36 — 2026-11-10

User-directed: boot spill-ON once ("run the lane, its up"), drive the ab9 D-lane
against the live server, then restore spill-OFF and rerun the identical lane.
Same pinned binaries throughout: engine `00691ac8…24efa`, helper
`0a340b99…66ad6` (v0216 copies, byte-identical to the clean worktree builds).

## Boot accounting (the narrow measurement)

| | spill-OFF boot | spill-ON boot |
| --- | ---: | ---: |
| Primary cache (CUDA0) | 4523 slots | 4523 slots |
| Later-stage cache (CUDA1) | 4842 slots | 4842 slots |
| Spill tier on CUDA1 | — | **56 pairs, 0.09 GiB** |
| Pairs held by a cache | 9365 | 9421 |
| Helper eligible pairs | 15211 | 15155 |
| Helper fill | 4194 (fill-mib stop) | 4199 (fill-mib stop) |
| CUDA1 free, everything loaded | 504 MiB | 406 MiB |

- Placement line confirmed (`generate.cpp:4518-4520`): tier 0 on CUDA1, last
  stage of 2, auto "as many as fit".
- **The split-36 later-stage card has essentially no headroom for a spill tier**:
  its own stage cache already spends the whole room (8.79 GiB, 4842 of 6144
  profiled pairs), leaving ~0.4 GiB, and the tier's fixed 512 MiB guard plus
  blob rounding admitted only 56 pairs. The spill feature's premise (residual
  VRAM on the last card) does not exist at split-36; it did at split-44
  (~2.8 GiB residual, 2048-pair cap).
- Helper candidate/eligibility accounting moved exactly by the tier's 56 pairs
  (15211→15155, held 9365→9421), matching the disjointness design.
- Helper preflight unchanged: reserve 512 MiB (engine hello wins over CLI 300),
  budget 6796.5 MiB, slots cap 4600, fill-mib cap 6796.

## Lane method

- `run_lane.sh ARM OUT` (new, reusable) drives the ab9 D-lane against the live
  server: warm(512) / 8000-1..3 / 80000-1..3 (prefill, max 8) / tg1024
  (ctx 2048, max 1024, two runs). Deterministic temp-0 prompts, thinking off.
- Both arms use arm label `S1` so prompts are byte-identical; the OUT dir
  distinguishes configuration (`out/ab9-spill1-split36-20261110`,
  `out/ab9-spill0-split36-20261110`).
- Judge = engine log slice lines (bytes → tokens: 8000 B → 1648 tok,
  80000 B → 14642 tok, all legs `0 reused` as required).
- Caveat: with max_tokens 8 the prefill-leg SHA hashes an invariant 8-token
  reasoning prefix, so prefill SHAs are trivially equal; parity evidence comes
  from the warm/tg1024 legs' full completions.
- Caveat: legs ran against the live server, so bench sessions may park into the
  production conversation cache (normal evictions; no deletion, budget intact).

## Spill-ON (S1) lane results — fresh boot, zero prior traffic

| Leg | Wall s | Judge: read tokens / ms / tok/s |
| --- | ---: | --- |
| warm | 7.95 | 293 / 5454 / 53.7 |
| 8000-1 | 7.28 | 1648 / 7159 / 230.2 |
| 8000-2 | 7.27 | 1648 / 7111 / 231.7 |
| 8000-3 | 7.15 | 1648 / 7039 / 234.1 |
| 80000-1 | 48.75 | 14642 / 47777 / 306.5 |
| 80000-2 | 48.57 | 14642 / 47626 / 307.4 |
| 80000-3 | 48.49 | 14642 / 47532 / 308.0 |
| tg1024 | 21.71 / 16.59 (cached) | first run 579 tok / 4652 / 124.5 |

- Prefill SHA (all six prefill legs): `01db7a9d154bb188430b234246589661629c627462db9ec632e33137f49bb45e`
- warm SHA: `acc211e7bf3ad6b837d09b380cb4822b9fe60451a6a11291cd414b09fea75bb5`
- tg1024 SHA: `a5311bd76566ef2b729093850bce42100f4c0e58b6e0445b1dab5ef357cc2ea4`

## Spill-OFF baseline and verdict

Baseline lane (restored boot: supervisor 3287852, engine 3289144, split 36,
`STRATA_CACHE_SPILL=0`, same hashes; fresh boot, zero prior traffic):

| Leg | Wall s | Judge: read tokens / ms / tok/s |
| --- | ---: | --- |
| warm | 7.64 | 293 / 5473 / 53.5 |
| 8000-1 | 7.13 | 1648 / 7007 / 235.2 |
| 8000-2 | 7.12 | 1648 / 6961 / 236.7 |
| 8000-3 | 8.18 | 1648 / 8058 / 204.5 (outlier) |
| 80000-1 | 44.04 | 14642 / 43886 / 333.6 |
| 80000-2 | 43.72 | 14642 / 43594 / 335.9 |
| 80000-3 | 43.69 | 14642 / 43556 / 336.2 |
| tg1024 | 26.91 / 28.77 | decode 513 tok / 12059 ms / 42.5; 514 / 12599 / 40.8 |

### Verdict against the adoption bar: REJECTED

- **PP ≥5% gain: FAIL.** Median 80k PP: spill-ON 307.4 tok/s vs spill-OFF
  335.9 tok/s — spill-ON is **~8.5% slower**. 8k median: 231.7 vs 235.2
  (~1.5% slower, near noise). Zero residency upside (56 pairs) cannot pay for
  the prefill-path changes the gate flips.
- **TG: no gain.** Spill-ON 40.7/40.8 tok/s vs spill-OFF 42.5/40.8 tok/s
  (per-request judge lines; different completion lengths, see parity).
- **Identical temp-0 SHA: FAIL.** warm `acc211e7…` vs `a317187e…`; tg1024
  `a5311bd7…` vs `3b43a501…`. The six prefill-leg SHAs match only because the
  8-token reasoning prefix is invariant — that is not parity evidence.
  Consistent with the audit: tier rows take GPU kernels where spill-OFF takes
  CPU-computed rows; even 56 pairs change logits and completions.
- Conclusion: **spill stays OFF.** At split-36 the feature has no headroom to
  use and measurably hurts long-context prefill.

### Confounds recorded honestly

- The spill-OFF tg1024 slice contains two interleaved ~1109-token requests
  (1091/535 reused) from non-bench traffic on the shared server — likely
  OpenCode background calls against the same prod. They inflate that leg's
  walls; per-request decode judge lines stay valid. Controlled reruns should
  use the window protocol's exclusive boot, or pause other clients.
- spill-OFF 8000-3 read 8058 ms is an outlier vs 6961/7007 (median used).
- Warm legs each contain a second judge pair (288 reused + 55 read): the
  wrapper's post-save verification request, present in both arms equally.