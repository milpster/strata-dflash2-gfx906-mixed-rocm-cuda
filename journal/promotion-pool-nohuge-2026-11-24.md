# Promotion record: prod serves the pool-MADV_NOHUGEPAGE engine — 2026-11-24

> ## CORRECTION — 2026-11-25. The promotion happened; it was not a fault fix.
>
> This record is accurate about what was deployed and when. What it is **not** entitled to claim is stability. The
> evidence behind the promotion covered two workloads only — **four cold prefills** and a **40-minute soak with
> thinking off** (§9 below is right that it is one boot each, and the soak is `journal/pool-nohuge-verification-2026-11-24.md`
> §8). It did not cover sustained park/restore churn. **The promoted binary faults on that workload.**
>
> | Event | When | Evidence |
> |---|---|---|
> | engine pid 2676741, `15d330ea…`, `0.2.18` + knob, booted on `:8009` | 2026-11-24 20:19:24 | `bench/fault-va/out/logs/prod-soak-2020.log:1` |
> | **fault**, 2 records, `XDMA (0x0)`, `PERMISSION_FAULTS: 0x3`, status `0x00800030`; VA unresolved (no sampler for this pid) | 2026-11-24 20:30:37 | `journalctl -k`; engine gone at request 10 of an alternating 80,000/16,000 soak (`prod-soak-2020.log:33`) |
> | engine pid 681814, same binary, same config | 20:36:56 | `prod-soak-maps.log:1` |
> | **fault**, 10 records, same client and status family (`0x00800031`), faulting VA inside the 32 GiB pool VMA | 2026-11-24 20:44:13 | `journalctl -k`; VA `0x7f788b3bc000` resolves to `7f7520800000-7f7d20800000 rw-p` = 34,359,738,368 B (`bench/fault-va/out/maps-soak/maps-1795549452.txt`) |
>
> So the promotion stands as a **fact about what prod runs**. It is not evidence that the fault is fixed, and §6's
> "that is the mechanism the knob removes" is withdrawn below. §8(a)'s "no demonstrated cost" is now also stale in
> both directions: no cost was demonstrated, and no benefit was either.

The RAM-pool fault fix (`STRATA_POOL_NOHUGEPAGE`: `MADV_NOHUGEPAGE` on the engine's 32 GiB pool mapping) is **live on
prod `:8009`**, promoted by the parent session at 10:21 CET under the operator's directive. The writing lane ran no
boot, no GPU work and no process control: every number is read from an artifact or from the read-only command beside
it. **elog** = `tr -d '\000' < logs/engine-prod-port0218.log | cat -n` (2,240 lines after `tr`; the raw file is
NUL-padded, so line numbers are taken after `tr`, as in `journal/pool-nohuge-verification-2026-11-24.md`).

## 1. What is serving

| Item | Value | Source |
|---|---|---|
| engine | pid 2715463, `/proc/2715463/exe` → `run/port0218-nohuge-bin/rpcw-hip/strata`, sha256 `15d330ea4d0eec4c…` | `ps`, `readlink`, `sha256sum` |
| helper | pid 2714328, `run/port0218-nohuge-bin/rpcw-cuda/strata-expert-helper`, `58c3ae020a7479a1…` (unchanged, copied) | `ps`, `sha256sum` |
| config | `prod-config-port0218-nohuge.json` sha256 `14ffa129c33cef9b…` | `sha256sum` |
| API | `:8009`, model `swift-1.5-ud-iq3xxs-managed`, ctx 262,144; `/health` → `{"status":"ok", …"loaded":true}` | `curl -s -m 5 http://127.0.0.1:8009/health` |
| uptime | **26:28** at 10:47 CET, engine start 10:21:26 | `ps -o etime= -p 2715463` |
| kernel since 10:21 | **0** `no-retry page fault`, **0** `GPU reset begin` | `journalctl -k --since '2026-11-24 10:21:00' --no-pager \| grep -c …` |

## 2. The promotion commit and the two lines it changed

`c511059c5c` `[Other] promote: prod serves the pool-MADV_NOHUGEPAGE engine (fault fix)` — 1 file, **+2 −2**, in
`launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh` (`git show --stat c511059c5c`). Both defaults were the previous
tuple (`run/port0218-bin`, `prod-config-port0218.json`); nothing else in the launcher moved, and both stay overridable
(that is the rollback in §7):

```
36: BIN=${STRATA_PROD_BIN:-"$ROOT/run/port0218-nohuge-bin"}   # 2026-11-24: promoted 0.2.18 port + pool MADV_NOHUGEPAGE (15d330ea; helper 58c3ae02 unchanged)
39: CONFIG=${STRATA_PROD_CONFIG:-"$ROOT/prod-config-port0218-nohuge.json"}   # 2026-11-24: promoted tuple = 0.2.18 + STRATA_POOL_NOHUGEPAGE=1 (fault fix; numerics byte-identical to the fork REF, journal/pool-nohuge-quality-gate-2026-11-24.md)
```

## 3. The promoted tuple, field by field

| Field | Value | Source |
|---|---|---|
| product / engine build | 0.2.18 port, engine 0.1.64: the build that made `936efcc798aab395…` (`run/port0218-bin/rpcw-hip/strata`) plus `db5bc3ab10` | `journal/pool-nohuge-quality-gate-2026-11-24.md` §1; `sha256sum`; `faultdiff_configs.py:67-69` |
| patch | `MADV_NOHUGEPAGE` on the pool mapping before the pre-fault: `engine/src/core/ram_pool.cpp:243-252` (mmap at :224) | `git show db5bc3ab10` in `/mnt/srcds/Strata-wt-0218` (branch `port-0218`); same change as `473f715f62` on `helper-ipc-diet` |
| knob value | `"STRATA_POOL_NOHUGEPAGE": "1"` | `prod-config-port0218-nohuge.json:108` |
| config delta vs the old config | exactly three: `derived_from`/`derived_note`, the knob line, `exe` → nohuge | `diff` of the two parsed JSONs |
| numerics | teacher-forced 4,995 positions: F/N0/N1 share one md5 `9d9e64eb…`, top-1 100 %, KL 0.00000 | `journal/pool-nohuge-quality-gate-2026-11-24.md` §2 |

## 4. Boot evidence (the knob actually fired)

| elog | Line |
|---|---|
| 2132 | `[strata] 2026-11-24 10:21:26 engine started: /mnt/srcds/Strata/run/port0218-nohuge-bin/rpcw-hip/strata --serve --gpu 0,1 --pack /mnt/srcds/ai/iq3xxs-ud …` |
| 2152 | `strata: RAM pool: 32768.0 MiB (34359738368 bytes) mapping: transparent huge pages off (MADV_NOHUGEPAGE; STRATA_POOL_NOHUGEPAGE)` |
| 2167 | `expert cache auto: 11.14 GiB free, 256 MiB reserved (+0 MiB for the draft head, +256 MiB …) -> 4291 slots` |
| 2208 | `strata serve: 398 MiB of VRAM free with everything loaded` |
| 2209 | `strata serve: CUDA1: 436 MiB of VRAM free with everything loaded - LOW: its window graphs are captured on first use …` |

The 17 boots before it have **0** lines matching `transparent huge` (`sed -n '1,2131p' | grep -c`), so 2152 is the
first and only such line in this log: the mapping is reported, not assumed.

## 5. Live verification: one real request, then the request that used to kill it

**(a) A real `POST /v1/chat/completions`** ("Reply with exactly: promotion verified", `max_tokens 64`, temperature 0):
content `promotion verified`, `finish_reason: stop`, usage prompt 58 / completion 31 / total 89. The engine's judge
line is elog **2227**: `prompt 58 tokens = 0 reused + 58 read in 3124 ms (18.6 tok/s), 31 generated in 1985 ms
(15.6 tok/s), drafts accepted 22 of 26 … timeouts=0 fallbacks=0` — 5.1 s of engine time (the probe's client wall was ~6 s; not re-measured here). The two token counts are the gate on this check and come from this line.

**(b) The survival prefill** — the 58,423-token frozen corpus leg that faulted fatally on the previous tuple, on the
same prod boot. Record `bench/ab-v0210/rpcprof/out/promotion-check/B-pp239k.json` (arm B, `kind: prefill`,
`max_tokens` 8, prompt `210b16b3…`); judge line at elog **2239** (record `elog_start`/`elog_end` 342,266 → 343,836 bytes of `logs/engine-prod-port0218.log`):

```
prompt 58423 tokens = 0 reused + 58423 read in 226058 ms (258.4 tok/s), 4 generated in 447 ms (8.9 tok/s), drafts accepted 1 of 3, 4 checkpoints helper=reqs=78 timeouts=0 fallbacks=0
```

| Record field | Value |
|---|---|
| `engine_sha256` / `engine_exe` | `15d330ea4d0eec4c…` / `…/run/port0218-nohuge-bin/rpcw-hip/strata` |
| `wall_s` | 228.418 (226.058 read + 0.447 gen + 1.9 s overhead) |
| `max_rss_mib` / `min_avail_mib` / `max_swap_used_mib` | **41,945** / **9,971** / **8,663** |
| `max_vram_mib` | card3 16,271 / card4 453 / card2 16,312 / nvidia0 7,592 |
| kernel since 10:21 (covers the leg) | 0 `no-retry page fault`, 0 `GPU reset begin` |

The 8.9 tok/s in that line is a **4-token tail** (`max_tokens` 8) and is not a decode rate; nothing here is a tg claim.

## 6. The contrast that makes this promotion

| | previous tuple (`936efcc7`) | promoted tuple (`15d330ea`) |
|---|---|---|
| workload | 58,423-token cold prefill | 58,423-token cold prefill |
| outcome | **fatal**: `amdgpu 0000:09:00.0: [mmhub0] no-retry page fault`; 4 events at 22:32:16, 22:48:21, 23:29:49, 00:49:26; boot→fault **251–282 s** | **completed**: 226,058 ms read, 4 generated, both cards alive |
| source | `journal/amdgpu-userptr-fault-diagnosis-2026-11-24.md:13-41` | §5b above |

The faulted VA sat inside the pool's VMA while it was still mapped, and the pool carried AnonHugePages 1.27 GiB at
fault time (`db5bc3ab10` message; `bench/fault-va/maps_sampler.sh`). That is the mechanism the knob removes.

> **CORRECTED 2026-11-25 — both sentences are withdrawn.** (a) The 1.27 GiB figure is not a measurement: it exists
> only in a commit message, and at the time of that fault the sampler read `smaps_rollup` only — process-wide, no
> per-VMA output (`journal/amdgpu-userptr-fault-diagnosis-2026-11-24.md:128` says the same). The per-VMA sampler
> arrived the evening of 2026-11-24, after this promotion. (b) "That is the mechanism the knob removes" is false.
> The knob-on binary faulted twice on 2026-11-24 evening (20:30:37, 20:44:13), and the per-VMA reading taken on the
> same promoted configuration puts the pool VMA at `AnonHugePages: 0 kB` with `VmFlags: … lo … nh`
> (`bench/fault-va/out/sampler-selftest/huge-1795557323.txt:3`). The 0 kB reading is from 22:55, not from a fault
> second — no per-VMA reading exists for either fault — but it shows the fault needs no huge pages in that mapping,
> so removing them cannot be the mechanism. See `journal/fault-investigation-corrections-2026-11-24.md` §4.

## 7. Rollback (one invocation, no rebuild)

```
STRATA_PROD_BIN=/mnt/srcds/Strata/run/port0218-bin STRATA_PROD_CONFIG=/mnt/srcds/Strata/prod-config-port0218.json ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh
```

A pre-promotion copy of the launcher is at `/tmp/opencode/launcher-pre-promote-1018.sh` — **ephemeral**, `/tmp` is not
backed up, so `c511059c5c` (`git revert`) is the durable path.

## 8. Two caveats, stated where they cannot be missed

**(a) The knob's speed cost is unmeasured.** The promotion rests on three facts only: the fault is gone on the
triggering workload (§5b, §6), the numerics are byte-identical (§3), and no cost has been demonstrated. The A/B is
*prepared* but not run: `2c63683e7d` adds the `pool-huge` control twin, whose config differs from `pool-nohuge` by
exactly one env line (`diff` of `gen/faultdiff/{pool-nohuge,pool-huge}.json`), and the tg leg shape is one judge line
per arm — request 1 fresh prefill, request 2 the cached re-run at a 1,024-token cap (`port_ab_contract.py:73-78`,
`faultdiff_configs.py:495-499`). **As frozen, both legs boot the unpatched engine**: `manifest.json` pins
`engine = run/port0218-bin` / `936efcc7…`, and the pair only becomes a real test with `STRATA_FAULTDIFF_ENGINE` +
`STRATA_FAULTDIFF_ENGINE_SHA256` naming `15d330ea` (`faultdiff_configs.py:110-155`). Until that window runs,
"no demonstrated cost" is the honest phrasing — not "free".

**(b) `khugepaged/defrag` reads 1, not 0.** The diagnostic profile set it to `0` (`bench/fault-va/page_stability.sh`,
`apply_stable`), and `restore()` never puts it back — the state file records only `thp_enabled`, `thp_defrag`,
`vm_compaction_proactiveness`, `vm_swappiness`. It now reads `1` (the kernel default) with an mtime of
**2026-11-24 10:15:12**, six minutes before the promoted boot: `stat -c %y
/sys/kernel/mm/transparent_hugepage/khugepaged/defrag`. `enabled` is `[always]`, `defrag` `[defer]`,
`AnonHugePages: 2,076,672 kB` — the same THP state as the faulting configuration
(`journal/pool-nohuge-verification-2026-11-24.md` §2), so the promotion was not made on a THP-starved kernel. The
script gap is still real and is a fixer item: add `khugepaged/defrag` to `save()`/`restore()`.

## 9. What this does NOT show

| Not shown | Why it matters |
|---|---|
| **one boot, 26 minutes up** | the old tuple died at 251–282 s into this same workload, so 26 min past it is a real signal — but it is one boot, not a distribution; the 40-minute soak (`80bfed7250`) was another boot and another mix |
| the window legs | 3k oracle, 240k fill, park/restore and the port helper's debut were validated on the 0.2.18 port (`journal/pool-admission-fix-2026-11-21.md:1303-1316`) and have **not** been re-run on `15d330ea`; only the prefill that caused the fault was |
| the SMU/powerplay reset class | untouched and unexplained: 692 resets in one boot, SMU answering `0xffffffff` 59 s before the first reset (`journal/pool-nohuge-verification-2026-11-24.md` §7). A clean run here says nothing about it |
| the default-off path | the fix is **opt-in**: a fresh build without `STRATA_POOL_NOHUGEPAGE` has exactly the old behaviour and the old exposure (`db5bc3ab10`; 0 strings hits in `run/port0218-bin`) |
| the helper path | the helper binary is unchanged, so a helper-side fault stays possible; `helper=reqs=78 timeouts=0` is a counter, not a survival claim |

Recorded rather than fixed (cosmetic): `prod-config-port0218-nohuge.json:153`'s `derived_note` still ends
"NOT PROMOTED" — written before `c511059c5c`. It misleads a reader of the config, not the engine.
