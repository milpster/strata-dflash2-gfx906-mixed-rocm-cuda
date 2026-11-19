# Production startup after reboot — 2026-11-19

No configuration or binary change was needed: the saved config and launcher
retained primary/later-card reserves **256 / 448 MiB**.

- Config SHA256: `95ef3693b3f598e6bdbe534ef7a9b195e13b9e1705acc0cad2d414db09f21852`.
- Live canonical engine SHA256: `b0ec0109ebc50c982512c371d81b2c15a8f72c70edf8a461a4ff10337da52f02`.
- Startup: canonical `champions_restore.sh --health --timeout 1800 --poll 5 --engine-sha <canonical-sha> --out <this-dir>/restart`, exit **0**, accepted **13:39:25 CET**.
- Stop gate: exit 0 and **ALL STOPPED**. Real response: **OK**, finish **stop**, **28 completion tokens**.
- Identity: supervisor **47924**, generation **1**, engine **48666**, start ticks **58279**.
- Live argv reserve assertions and image/config hash checks: exit **0**.
- Passive snapshot **13:40:18 CET**: valid identity, both Radeon clients **zero evicted VRAM / 2116 KiB GTT**; collector exit **0**.
- JSON parse and launcher/restore shell syntax: exit **0** before startup.

All agents were stopped before production control. No benchmark, candidate
promotion, driver policy or quality/capacity change accompanied startup. This
proves current availability, not sustained speed or an eviction fix. Active
`restart/logs/restore-*.out` supervisor stdout is mutable and excluded from sealing.
