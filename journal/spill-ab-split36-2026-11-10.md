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

Pending: lane running against the restored spill-OFF boot (supervisor PID
3287852, engine 3289144, split 36 verified, `STRATA_CACHE_SPILL=0` verified,
same binary hashes). Verdict against the adoption bar (≥5% PP gain, no TG
regression, byte-identical temperature-zero SHA) recorded after both lanes.
