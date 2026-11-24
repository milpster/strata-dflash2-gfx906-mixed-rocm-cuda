# The pool fault: three beliefs that measurement killed — 2026-11-24/25

Correction record. Three claims written on 2026-11-24 were disproven by later measurement the same evening. Nothing is
deleted from the originals; each is marked where it stands, and this file gives the order. Fixer lane, branch
`recovery-startup-watchdog`, box GUAM. No boot, no GPU work, no process control, no `:8009` traffic, no push. Every
number is read from an artifact or from the read-only command printed beside it. Corrected:
`journal/pool-nohuge-verification-2026-11-24.md` (block at the top, marker at §6) and
`journal/promotion-pool-nohuge-2026-11-24.md` (block at the top, §6's mechanism sentence).

## 1. The story, in order

The belief was coherent. The engine's 32 GiB pool is a KFD userptr range, and `include/strata/core/pin_budget.hpp:129-134`
records that amdgpu invalidates a queue's view of such a range whenever the CPU page tables under it change. A 2 MiB
huge page collapsing or splitting is exactly such a change, so `MADV_NOHUGEPAGE` on the pool should stop it.

| Time (2026-11-24) | What was believed | Marked in |
|---|---|---|
| 01:26 | the knob works — 4 cold prefills, 0 faults | `pool-nohuge-verification…md` §3 |
| 04:46 | the 40-minute soak confirms it | `…verification…md` §8 |
| 10:21 | promoted to prod `:8009` | `promotion-pool-nohuge…md` §1 |
| ~21:00 | `STRATA_ARENA_MLOCK=1` is the real fix; mlock costs 7-14 % | `prod-config-port0218-mlock.json:154` |
| 20:30:37 / 20:44:13 | *measured later* — the promoted binary faults twice; beliefs 1 and 3 die | §3, §4 |
| 22:55 | *measured later* — per-VMA sampling arrives; belief 2 dies | §5 |

## 2. The retractions

| Claim | Disproven by | Date / time |
|---|---|---|
| `STRATA_ARENA_MLOCK=1` fixed the fault | `Pool::create` mlocks the pool **unconditionally**: `engine/src/core/ram_pool.cpp:260`, in the loop at `:256-267`, no `getenv` gate (the file's only `getenv` is `:243`, the THP knob; `grep -c getenv` = 1). The file has **0** `cudaHostRegister`/`hipHostRegister`. `STRATA_ARENA_MLOCK` locks only spans passed to `arena_mlock_span` (`include/strata/core/pin_budget.hpp:154-187`), and this tuple registers none | 2026-11-24, after 21:00 |
| mlock costs 7-14 % on prefill | The "mlock on" and "mlock off" runs differed in nothing that touches the pool — it is locked either way (§3). The figure is in **no artifact**: `grep -rn '7-14%\|7–14%'` over `journal/*2026-11-2*`, the mlock config and the soak logs returns nothing | 2026-11-24, 23:56 |
| the pool got huge pages anyway despite `MADV_NOHUGEPAGE` | per-VMA `smaps`: the 32 GiB pool VMA reads `anonhuge_kb=0` with `flags=rd wr mr mw me lo ac sd nh` (`bench/fault-va/out/sampler-selftest/huge-1795557323.txt:3`). The cited 2.9 GiB was `smaps_rollup` — process-wide, no mapping attribution | 2026-11-24, 22:55 |
| the relief/re-rank unregister path revokes the translation | `release_pair` has one caller (`engine/src/core/expert_source.cpp:8043`), `refill_pair` two (`:7691`, `:8050`); both `generate.cpp` sites gate on `srcp == &arena_src` (`:26756-26760`, `:26769-26773`) and the re-rank also needs `--expert-helper-relief-arena-cap-rerank`, which the tuple does not pass | 2026-11-24, 23:16 |

The last row rests on `STRATA_RAM_BUDGET_MIB=36864` (`prod-config-port0218-nohuge.json:99`): `generate.cpp:9550`
points `srcp` at the pool adapter, so `ArenaExpertSource::open` (`:9669`) never runs. The engine says so itself —
`nothing dropped: this run's expert source is not the arena` — 25 times in `logs/engine-prod-port0218.log`, once in
each faulting boot. (43 of 68 boots lack it: they ran the other binary and expert source, so not counter-evidence.)

## 3. The same configuration, twice

This kills belief 1. A live engine on the promoted tuple, from a config that does **not** set `STRATA_ARENA_MLOCK`
(`prod-config-port0218-nohuge.json` has `STRATA_POOL_NOHUGEPAGE` and `STRATA_RAM_BUDGET_MIB`, no `STRATA_ARENA_MLOCK`):

```
$ tr '\0' '\n' < /proc/75777/environ | grep -E '^STRATA_(ARENA_MLOCK|POOL_NOHUGEPAGE|RAM_BUDGET_MIB)='
STRATA_RAM_BUDGET_MIB=36864
STRATA_POOL_NOHUGEPAGE=1
$ grep -E '^VmLck:' /proc/75777/status
VmLck:	33718272 kB
```

`33718272 kB` = 32 GiB + 163,840 kB; the extra 160 MiB is the pinned reserve, not the pool. The pool's VMA — the only
34,359,738,368-byte mapping in the process (`awk` over `/proc/75777/maps` → `7f15fbc00000-7f1dfbc00000`), as the
upgraded sampler recorded it at 23:57:47: `7f15fbc00000-7f1dfbc00000 rw-p anonhuge_kb=0 flags=rd wr mr mw me lo ac sd nh`.
`lo` = locked (`mlock`'s own VmFlag), `nh` = `MADV_NOHUGEPAGE`. One VMA, one instant: locked **and** huge-page
excluded **and** 0 kB huge. There is no configuration in which this pool is unlocked.

## 4. The faults the promoted binary still takes

| # | When | Engine | Requests before death | Faulting VA |
|---|---|---|---|---|
| 1 | 20:30:37 | pid 2676741, `15d330ea…` | 10 | **unresolved**: no sampler ran for this pid (`maps-soak/` opens 20:36:56, after it). 2 records, status `0x00800030`, one address `0x7fdaedba2000` |
| 2 | 20:44:13 | pid 681814, `15d330ea…` | 6 | `0x7f788b3bc000` → `7f7520800000-7f7d20800000 rw-p`, `bytes=34359738368` = **32.00 GiB**. 10 records, status `0x00800031` |

Both carry `XDMA (0x0)`, `PERMISSION_FAULTS: 0x3`, `ring:24 vmid:8`, card `0000:09:00.0`. Event 1 is a smaller
variant (2 records, `MORE_FAULTS: 0x0`); event 2 has the shape of the original four fatal faults and resolves against
the snapshot taken one second before it — arithmetic, not inference:

```
# pid=681814 ts=1795549452 iso=2026-11-24T20:44:12+01:00     # maps-1795549452.txt, head -1
MATCH 7f7520800000-7f7d20800000 rw-p bytes=34359738368
```

Nothing else in this process is 32.00 GiB; the boot line names the pool at `34359738368` bytes (`logs/engine-prod-port0218.log:27`).

## 5. The sampler was upgraded mid-investigation, and that matters

The process-wide number misled us first. `bench/fault-va/maps_sampler.sh` at `HEAD` reads `smaps_rollup` only —
`git show HEAD:bench/fault-va/maps_sampler.sh` has `AnonHugePages` at the `smaps_rollup` line and nowhere else. That
is one aggregate for the whole process: it answers "does this process have huge pages", never "does **this mapping**".
The per-VMA output (`huge-<ts>.txt`, every mapping over 1 GiB) was added at **22:55** and is still uncommitted
(`git status`: `M bench/fault-va/maps_sampler.sh`; `maps_sampler.sh:22-32`).

So, plainly: **the earlier faulting runs have no per-VMA data.** `maps-soak/` and `maps-mlock/` hold 140 and 900
`maps-` snapshots with a `rollup-` beside each and **0** `huge-*` files. For the 20:44:13 and 00:49:26 faults, "the
pool had huge pages" was **never measured** — it was assumed from a process-wide total; for 20:30:37 no sampler ran at
all. Every per-VMA reading we do have (4 self-test snapshots; the live `maps-xfer/` window on pid 75777) puts the pool
at `anonhuge_kb=0`. **The instrument that could have supported the belief did not exist until after it was published.**

## 6. What survives

| Still true | Evidence |
|---|---|
| The fault recurs **inside the 32 GiB pool VMA** | measured twice — 20:44:13 by size and range (§4), 00:49:26 against the snapshot at 00:49:24 (`bench/fault-va/run/maps-1795477764.txt`). 20:30:37 is not in this count: no snapshot |
| That VMA has **0 kB huge pages** and is **locked**, yet it faults | §3's VMA line; no per-VMA reading exists for a faulting window (§5) |
| `MADV_NOHUGEPAGE` never addressed the cause | the knob was on in both faulting boots — one knob line inside each boot's range: 2436 (19:45:08 boot → pid 2676741) and 2641 (20:30:45 boot → pid 681814), each range taken to the next `engine started`. Config `:108` |
| It needs **sustained park/restore churn** | died at request 10 and request 6 of alternating 80k/16k soaks (`prod-soak-2020.log:30,33`; `prod-soak-maps.log:18,21`); 0 faults in 4 cold prefills and the thinking-off soak |
| The promotion happened; prod runs `15d330ea` | `promotion-pool-nohuge…md` §1-§2; that sha in every soak record's `engine_sha256` |

The last row is a fact about what was deployed, not a stability claim — the promotion record's new block says so.

**Limits.** No mechanism: these corrections remove three explanations and install none. The pool is locked, has no huge
pages, cannot be reclaimed or swapped, and its GPU translation is revoked anyway. No frequency: two faults, one
evening. No mlock cost figure — withdrawn, not replaced, because both arms of that comparison locked the pool. No link
to the `0000:04:00.0` reset class, untouched here as everywhere.

**Next.** A fault inside a window that writes `huge-*` would settle the huge-page question for a fault rather than one
boot away. With page mobility and huge pages both excluded, the remaining variable inside that VMA is what the engine
does to its contents — `Pool::pool_alloc` churn (`ram_pool.cpp:286`), the `PoolExpertStore` scratch window
(`pool_expert_store.hpp:48`), the DMA aliasing over it.
