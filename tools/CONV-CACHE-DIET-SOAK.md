# Round-6 diet lane: bound the conversation-cache side (park once, restore beats re-read)

The round-4 arena cap (`--expert-helper-relief-arena-cap-mib 22000`) bounds the expert-arena bytes;
it does not touch the **conversation-cache side**. Round 5 (v0224) added the page release, the RAM
ceiling and the `--kv-resident-mib` budget, and its soak FAILED both gates. Its own log
(`/mnt/srcds/Strata/logs/engine-soak-v0224.log`) says why:

1. **The RAM tier accumulated** — `parked 4866 tokens ... bytes=406167416` → `9597 ... 935972512` →
   `14301 ... 1588683704` → `19010 ... 2482969624`: one whole snapshot per park of the SAME
   conversation (`superseded=0`, `evictions=0`). #342's rule matches the outgoing chain's deepest
   checkpoint; under the checkpoint-save RAM gate (69f37322d) chains go sparse, so the rule never
   fires and the tier grows ~47 KB/token per turn.
2. **The ceiling never bit** — the soak set `--conversation-cache-ram-mib 4096`, the same as
   `--conversation-cache-mib`, so 3.7 GiB of tier sat under it at 28k tokens while engine RSS
   ratcheted 40.8 → 54.0 GiB and avail fell to 7.5.
3. **Restore lost to re-read** — `the draft ring no longer retains a rewind to 16384 (draft miss:
   no pair at this checkpoint); reading the prompt again (16384 tokens)` → PP fell to
   28.5–149.7 tok/s (`slow read context`) and parks cost 15 s, 28 s, 69 s.

## The three fixes

| where | fix |
|---|---|
| `include/strata/core/conversation_cache.hpp` | `drop_superseded` gained the **live-prefix rule**: an entry whose whole live state is a prefix of the outgoing live, with every checkpoint the new chain holds, is superseded — park once per conversation, proportional bytes. Pinned prefixes and guarded donors are exempt. |
| `src/program/generate.cpp` (park path) | a park whose estimated snapshot is over `--conversation-cache-ram-mib` **goes to the disk tier only**: no host copy is built (the ceiling drops it a moment later anyway), so the tier's bytes stay bounded by the ceiling and a long conversation costs zero RAM. Line: `parked to disk only (N tokens, snapshot X MiB over the Y MiB RAM ceiling; ...)`. |
| `src/program/generate.cpp` + `DFlashDrafter::rewind_cold` | a draft-miss rewind mounts **target-only with a cold drafter** (the ring horizon is set to the resume point; the first rounds propose from the previous turn's rows, the verify rejects them, its appends refill the window). The full-prompt re-read stays only when the drafter itself refuses the cold horizon. |

Kept from round 5 (the log proves they work): `--conversation-cache-evict-page-cache`
(`disk park streamed ... pages-released=8 (2589 MiB released)`), the ceiling/LRU wiring, the boot
flag lines, and `--prompt-window-retry`'s merged-window extension.

## Geometry arithmetic (iq3xxs-class, fp16 KV, 48 layers, 12 QSA, page 4 cells)

Per-page KV pool = `4*2*256*2*2 = 8192 B` (+64 header). Per resident cell = `8192/4 * 12 = 24576 B`
(the engine prints it at boot). `--kv-resident-mib 4096` budgets `4096*1048576/24576 = 174762`
cells: a 130k-token ACTIVE conversation stays fully resident (reuse intact); only contexts past
174762 cells stream the tail. A parked **snapshot** costs ~45–47 KB/token measured (v0224:
894285920 B = 853 MiB at 19010 tokens, 1140411680 B = 1087 MiB at 28430) — a 120k conversation is
~5.3 GiB, which is exactly why it must be disk-only.

## Boot lines (exact, with numbers)

```
strata generate: --kv-resident-mib 4096: 24576 bytes per resident KV cell over 12 QSA layers -> resident 174762 of 262144 asked cells (4.0 GiB resident of the 262144-token context, the rest streams; --kv-resident 262144)
strata serve: conversation cache: RAM ceiling 512 MiB (parked snapshots plus the retained K/V); a snapshot over it parks to disk only, and over it the least recently active conversations the disk tier published drop to disk (the active conversation is never dropped)
strata serve: conversation cache: file page-cache release ON (park writes, restore reads and ring sidecars hand their cached pages back after their flush; the parked file's page-cache footprint stays ~0)
```

## Judge lines during the soak (expected shapes)

```
strata serve: conversation cache: parked to disk only (19010 tokens, snapshot 853 MiB over the 512 MiB RAM ceiling; the published generation serves the next restore; parked=0 bytes=0)
strata serve: conversation cache: parked 9597 tokens in 320 ms; parked=1 bytes=496 MiB evictions=0 snapshot_bytes=529805096 reused_kv_bytes=0
strata serve: conversation cache: dropped 1 superseded copy of this conversation; parked=0
strata serve: conversation cache: disk park streamed 120133 tokens idle to .../conv-N.sess in 6400 ms; ... pages-released=2 (5370 MiB released)
strata serve: conversation cache: restored 119200 tokens (checkpoint 119200) in 3100 ms; parked=0 bytes=0
strata serve: the draft ring no longer retains a rewind to 16384 (draft miss: no pair at this checkpoint); mounting target-only with a cold drafter (no drafts claimed for the first window, the refill costs acceptance, not the read)
strata serve: prompt 120133 tokens = 119200 reused + 933 of 933 read in 1.5 s (620 tok/s), 128 generated in 2900 ms (44.1 tok/s)
```

Forbidden in a passing run: `reading the prompt again`, `slow read context`,
`0 generated in 0 ms`, and a `parked ... bytes=` that climbs past the ceiling.

## Expected gate numbers

Baseline (prod v0219, S34 uncapped tuple, measured today): max_rss **40183 MiB** at boot-state,
avail **13372 MiB**, VIIs 16.2/16.1 GB, 3080 7.1 GB, PP 648.8 tok/s, TG 44.3 tok/s (small-pass
decode), 142k fresh read 648.8 tok/s, wall class ~234 s.

| gate | expectation with the round-6 soak config (`RAM_MIB=512`, `KV_RES_MIB=4096`, expert tuple untouched) |
|---|---|
| engine RSS at 130k active | **≤ 40.7 GiB** (40183 MiB baseline + ≤ 512 MiB tier + ~100 MiB retained KV) — inside the 40 GiB = 40960 MiB budget with ~265 MiB slack |
| tier bytes | **≤ 512 MiB** at every park line; the 120k conversation parks disk-only (its 5.3 GiB never touches RAM) |
| parked-file page cache | delta ≈ 0 (the park/restore releases report 1087–5370 MiB each) |
| PP / TG | fresh 142k read 470–650 tok/s, cycle reads 600+ tok/s (restore + tail), tg probe ≥ 34 tps at full ctx, 44 tps small-pass — the tier costs no tokens: it builds no host copy over the ceiling, and a rewind never re-reads |
| host avail at 130k | **~13 GiB** with the expert tuple untouched — the 15 GiB floor is 2 GiB out of reach at that RSS on a 62 GiB host with prod live. It is reachable with `--expert-helper-relief-hot-mib 13000` + `--kv-resident-mib 3072` (~2.0 + ~1.0 GiB of residency given back → RSS ~37.7 GiB, avail ~16 GiB), at the cost of decode swap-ins the tuple was measured to avoid: **measure the tg probe on that arm before accepting it**. |

Soak `--rss-budget-gib 40 --avail-floor-gib 15 --tg-floor 34 --ram-ceiling-mib 512`.

## Soak procedure (operator; boots its own engine, never :8009)

1. `bash tools/conv_cache_diet_boot.sh` — writes the soak config (prod config + the three diet
   flags + its own cache dir/port 8019, `RAM_MIB=512`) and prints the serve command and the boot
   lines to grep.
2. Boot the soak engine on port 8019 against the pinned round-6 pair, serve stderr to `--elog`.
3. `python3 tools/conv_cache_diet_soak.py --url http://127.0.0.1:8019/v1/chat/completions \
   --elog <serve stderr log> --cache-dir <soak cache dir> --ram-ceiling-mib 512`

Phases: (a) grow one conversation past 120k tokens in ~12×~9k-token deltas; (b) 10 idle→request
cycles at full size (park/restore hammering); (c) LRU pass; (d) tg probe at full ctx. Each phase is
a memory leg in the ab9 driver's shape — `max_rss_mib` (VmHWM, reset with `clear_refs` at the leg's
start, lower-better), `min_avail_mib` (higher-better), `max_swap_used_mib` — and the assertions are:
RSS ≤ 40 GiB per leg, avail floor ≥ 15 GiB, tg ≥ 34 tps, zero-gen == 0, parked page-cache ≤ 0.6 GiB,
**tier bytes ≤ the ceiling** (park-once), **zero `reading the prompt again`**, **every cycle after a
park shows `reused > 0`** (restore beats re-read), zero `slow read context`.
