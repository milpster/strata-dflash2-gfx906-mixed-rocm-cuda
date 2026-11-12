# Round-5 diet lane: bound the conversation-cache side (v0224)

The round-4 arena cap (`--expert-helper-relief-arena-cap-mib 22000`) bounds the expert-arena
bytes. It does not touch the **conversation-cache side**, which degraded prod on Nov 12 morning:
one conversation grew to 126k tokens / 72 requests; its parked 4.18 GiB file's page cache +
restored KV + pack-page rebound grew engine RSS 34.2 → 51.7 GiB, avail 19.5 → 9.7, swap 5 → 11.4,
decode fell from the 37 band to 25–27, with six `slow read context` events and one
`0 generated in 0 ms` the `--prompt-window-retry` did not catch.

Three new opt-in flags (`src/program/generate.cpp`), off by default, all built green in the
canonical L3 pair:

| flag | bound | default for the soak |
|---|---|---|
| `--conversation-cache-evict-page-cache` | release parked/restore/ring-sidecar file pages after their flush (`posix_fadvise DONTNEED`), so a GiB-scale park leaves ~0 resident | ON |
| `--conversation-cache-ram-mib N` | host-RAM ceiling on the parked snapshots + retained KV; over it the least-recently-active conversations the disk tier **published** drop to disk (LRU). Active conversation, unpublished/guarded/pinned entries never drop | 4096 |
| `--kv-resident-mib N` | size `--kv-resident` from a budget (only lowers the asked residency, keeps its 4096-cell floor); the rest streams from the host | 4096 |

`--prompt-window-retry` now also covers a **merged** first window (the class that aborted at the
prompt tail with the whole prompt re-read next: `#879`, 246937 of 246942 read, 0 generated in 0 ms).

## Geometry arithmetic (iq3xxs-class, fp16 KV, 48 layers, 12 QSA, page 4 cells)

Per-page KV pool = `page_size * n_head_kv * head_dim * 2(K,V) * 2(fp16) = 4*2*256*2*2 = 8192 B`
(+64 header). Per resident cell = `8192/4 * 12 = 24576 B ≈ 24 KiB` (the engine prints the exact
number at boot). `--kv-resident-mib 4096` budgets `4096*1048576/24576 = 174762` cells, so a
130k-token ACTIVE conversation stays fully resident (reuse intact); only contexts past 174762
cells stream the tail. Full residency (262144) would hold 6.44 GiB of KV, the 174762 cap 4.22 GiB.

## Boot lines (exact, with expected numbers)

```
strata generate: --kv-resident-mib 4096: 24576 bytes per resident KV cell over 12 QSA layers -> resident 174762 of 262144 asked cells (4.2 GiB resident of the 262144-token context, the rest streams; --kv-resident 262144)
strata serve: conversation cache: RAM ceiling 4096 MiB (parked snapshots plus the retained K/V); over it the least recently active conversations the disk tier published drop to disk (the active conversation is never dropped)
strata serve: conversation cache: file page-cache release ON (park writes, restore reads and ring sidecars hand their cached pages back after their flush; the parked file's page-cache footprint stays ~0)
```

Judge lines during the soak (counts are cumulative on the disk tier's stats):

```
strata serve: conversation cache: parked 120133 tokens in 2900 ms; parked=1 bytes=... pages-released=3 (8100 MiB released)
strata serve: conversation cache: dropped 2 parked conversations to the disk tier (RAM ceiling 4096 MiB, 4095 MiB resident, 3 parked, evictions=2)
strata serve: prompt 120133 tokens = 119200 reused + 933 of 933 read in ... , N generated in ... ms (>= 34.0 tok/s)
```

Expected at steady state, 130k-token active conversation (vs the unbounded Nov 12 numbers):

| metric | unbounded (Nov 12) | bound target (v0224) |
|---|---|---|
| engine RSS | 51.7 GiB (now 47) | ≤ ~40 GiB |
| MemAvailable | 9.7 GiB | never < 15 GiB |
| decode at full ctx | 25–27 tps | holds the 35 band (probe ≥ 34) |
| parked-file page cache | +4.18 GiB resident | delta ≈ 0 |
| `0 generated in 0 ms` | 1+ (retry missed it) | 0 |

RSS arithmetic for the target: baseline fresh boot 34.2 GiB; the +17.5 GiB growth was the parked
file's page cache (4.18, **removed** by the release), the resident-KV host copies for the full
262k residency (**cut ~2.2 GiB** by the 174762 cap), the parked RAM copy (already capped by
`--conversation-cache-mib 4096`, now also by the RAM ceiling's LRU drop), and the pack-page
rebound. That leaves ~38–40 GiB at 130k active. **If RSS overruns 40, drop `RAM_MIB` and
`KV_RES_MIB` to 3072** (`--conversation-cache-ram-mib 3072` keeps one ~2.5 GiB parked snapshot +
retained KV; `--kv-resident-mib 3072` budgets ~131k resident cells, still ≥ the 130k active).
Avail floor 15 GiB is met by the same ~11 GiB of freed residency against the 62 GiB host.

## Soak procedure (operator, after pinning v0224 — boots its own engine, never :8009)

1. `bash tools/conv_cache_diet_boot.sh` — writes the soak config (prod config + the three flags +
   its own cache dir/port 8019) and prints the serve command.
2. Boot the soak engine on port 8019 against the pinned v0224 pair, serve stderr to `--elog`.
3. `python3 tools/conv_cache_diet_soak.py --url http://127.0.0.1:8019/v1/chat/completions \
   --elog <serve stderr log> --cache-dir <soak cache dir>`

Phases: (a) grow one conversation past 120k tokens in ~12×~9k-token deltas (auto-extends to
`--grow-target-tokens 120000`, max 24 deltas) with `max_tokens` 1–8; (b) 10 idle→request cycles at
full size (park/rewrite hammering); (c) LRU pass — two small conversations interleaved then touched
out of order; (d) tg probe at full ctx (`max_tokens 128`, cached run) and assertions. The driver
samples `MemAvailable`, summed engine/helper VmRSS, and the newest parked `.sess` file's resident
bytes (`mincore`) while parked; it greps the boot/park/drop/slow-read/zero-gen lines from its own
log slice and asserts: avail floor ≥ 15 GiB, RSS ≤ 40 GiB, tg ≥ 34 tps, zero-gen == 0, parked
page-cache resident ≤ 0.6 GiB, zero `slow read context`.
