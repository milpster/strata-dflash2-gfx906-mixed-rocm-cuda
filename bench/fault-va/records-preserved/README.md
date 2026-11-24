# Preserved cycle records

Recovered copies, kept here because the path they were written to no longer holds them.
They are **not live artifacts**: no script writes to this directory, and the engine logs
they point at are the ones from the runs of 2026-11-24 01:18 and 01:25.

## cb-2.default-margins.B-pp239k.json

| | |
|---|---|
| what | cycle 2 of a `LEG=default-margins` `cycle_boots.sh` run: one cold prefill of the 58,423-token corpus, arm B |
| fired | prefill start 01:21:05, record written 2026-11-24 01:25:30 (the record's `ts` minus its `wall_s`, and its `ts`) |
| wall | `wall_s` 265.76 s, from the file |
| judge | `strata serve: prompt 58423 tokens = 0 reused + 58423 read in 263508 ms (221.7 tok/s), 4 generated in 318 ms (12.6 tok/s), drafts accepted 1 of 3, 4 checkpoints helper=reqs=79 rows=239 bytes=2447360 timeouts=0 fallbacks=0` — the engine log line at the record's own `elog_start`/`elog_end` (106395..109041) in `out/faultdiff-r1/logs/default-margins.engine.log` |
| engine | `936efcc798aab3952551281f2f328c1b8b19311fee9d0298142e702809a58394` (the rig pin) |
| memory | `max_rss_mib` 41,912, `min_avail_mib` 8,156, `max_swap_used_mib` 4,309 |
| sha256 | `146495b6bb273b7a6fc28acfcb9b5e82f85934c9cae0589a323e04e7ac6fadfa` |

Why it is here: until this commit `cycle_boots.sh` wrote every cycle's record to
`bench/ab-v0210/rpcprof/out/faultdiff-r1/records/cb-<N>/`, a name that depends on the
cycle number only. On 2026-11-24 at 01:45 a `LEG=pool-nohuge` run wrote its own cycle 2
over it, so `records/cb-2/` now holds that pool-nohuge record (ts 01:49:53, engine
`15d330ea…`) and this file is the only copy of the default-margins one. The copy was
recovered from an opencode snapshot of the file, at the sha256 above; the file the driver
wrote is gone, so there is no original left to compare it against. Treat it as recovered
evidence, not as a measurement taken today.

## cb-1.default-margins.B-pp239k.json

The same run's cycle 1, kept so both cycles of the preserved run sit together: `wall_s`
244.565, record `ts` 2026-11-24 01:18:58, judge line `… read in 242301 ms (241.1 tok/s),
4 generated in 322 ms (12.4 tok/s) …` (elog offsets 92048..94694), `max_rss_mib` 41,946,
`min_avail_mib` 8,506, `max_swap_used_mib` 6,601, sha256
`c6bdfa1941f1913eb4235fdeaf8aabfde4b6d633f2042281932970d1ab2f1942`. That digest is
identical to the live `records/cb-1/B-pp239k.json`, which was not overwritten.

## The live evidence this leaves alone

The live directories are left exactly as they are: `records/cb-1/` (default-margins,
01:18:58), `records/cb-2/` (pool-nohuge, 01:49:53 — the one that overwrote the record
above), `records/cb-3/` (pool-nohuge, 02:10:16) and `records/cb-4/` (pool-nohuge,
02:16:04). Since this commit a run writes to `records/<leg>-<stamp>/cb-<N>/`, so two legs
- or two runs of one leg - cannot land on the same path.
