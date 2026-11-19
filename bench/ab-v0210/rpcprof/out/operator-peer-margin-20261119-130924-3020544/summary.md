# Production reserve update — 2026-11-19

User requested primary-card +64 MiB, then peer-card -128 MiB. Final persisted and
live argv values: `--vram-reserve-mib 256 --vram-reserve-later-mib 448`.

- Config: `prod-config-v0238.json`, SHA256 `95ef3693b3f598e6bdbe534ef7a9b195e13b9e1705acc0cad2d414db09f21852`.
- Engine unchanged: `v0238p0/rpcw-hip/strata`, SHA256 `b0ec0109ebc50c982512c371d81b2c15a8f72c70edf8a461a4ff10337da52f02`.
- Canonical launcher selects those paths; canonical restoration pins that config.
- All agents stopped before process control; no agent ran during either restart.

| Command/check | Actual result |
| --- | --- |
| `python3 -m json.tool prod-config-v0238.json` | Exit 0 |
| `bash -n bench/ab-v0210/rpcprof/gpu-rounds/champions_restore.sh` | Exit 0 |
| `bash bench/ab-v0210/rpcprof/gpu-rounds/champions_restore.sh --health --timeout 1800 --poll 5 --engine-sha b0ec0109ebc50c982512c371d81b2c15a8f72c70edf8a461a4ff10337da52f02 --out <this-dir>/restart` | Exit 0; ALL STOPPED; restored 13:12:01 CET |
| Recorded health response | HTTP accepted by verifier; OK, stop, 28 completion tokens; decode 17.7 tok/s |
| Actual `/proc/3034680/cmdline` reserve assertions | 256 / 448 MiB, each flag present once; exit 0 |
| `sha256sum --check` live `/proc/3034680/exe` and config | Both OK; exit 0 |
| `python3 -u .../watch_gpu_residency.py --out <this-dir>/residency-after --duration 0 --interval 30` | Exit 0; one valid snapshot at 13:12:50 CET |
| Owned config/restore diff whitespace check | Exit 0 |

Selected identity: supervisor 3033941, generation 1, engine 3034680, start ticks
6903176. Both target Radeon clients reported zero evicted VRAM and 2116 KiB GTT.
Device-wide free VRAM: PCI 0000:04:00.0/card2 112111616 B (106 MiB truncated),
PCI 0000:09:00.0/card3 206057472 B (196 MiB truncated).

These are post-restart observations, not a controlled speed result, proof of
sustained recovery, or a guarantee against eviction. RAM-pressure acceptance and
the recurrence investigation are parked; the RAM candidate was not deployed.
Quality, precision, context and KV capacity are unchanged. The active supervisor's
`restart/logs/restore-*.out` is mutable and intentionally excluded from any seal.
