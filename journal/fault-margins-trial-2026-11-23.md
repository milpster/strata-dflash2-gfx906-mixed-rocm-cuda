# Engine-default VRAM margins: first evidence — 2026-11-23

Operator directive of 2026-11-23: stop testing the historical margins (256/192, and 144/144 before
it) and boot the engine's own default — both `--vram-reserve-mib` and `--vram-reserve-later-mib`
absent, i.e. 700 MiB per card (`out/faultdiff-r1/configs/default-margins.json:137`) — because the
recurring XDMA permission fault kept ending boots. Paths are relative to
`bench/ab-v0210/rpcprof/`; `elog` is an engine log read with `tr -d '\000' < file | cat -n`. The
default-margins elog is still growing (220
lines at 2026-11-23T23:14:12+01:00), so its line numbers are that snapshot. Every number is
read from the file named beside it; nothing was measured by me.

## The fault class

`journalctl -k -b` (this boot started 22:04:03) holds 20 records of one class on
`amdgpu 0000:09:00.0` (CUDA1): 10 at 22:32:16, 10 at 22:48:21. One record, whole:

| Field | Value |
|---|---|
| message | `[mmhub0] no-retry page fault (src_id:0 ring:24 vmid:8 pasid:353)` |
| process | `Process strata pid 784900 thread strata pid 784900` |
| page | `in page starting at address 0x00007f9b33bf1000 from IH client 0x12 (VMC)` |
| status / client | `VM_L2_PROTECTION_FAULT_STATUS:0x00800031`, `Faulty UTCL2 client ID: XDMA (0x0)` |
| permissions / walk / rw | `PERMISSION_FAULTS: 0x3`, `MORE_FAULTS: 0x1`; `WALKER_ERROR: 0x0`, `MAPPING_ERROR: 0x0`, `RW: 0x0` |

Other 22:48:21 pages: `0x7f9b33be2000`, `0x7f9b33bd2000`, `0x7f9b33bf2000`, `0x7f9b33be3000`,
`0x7f9b33bd3000` — one 4 KiB host-VA neighbourhood. The engine sees `Memory access fault by GPU
node-2 ... on address 0x7f9b33bfd000. Reason: Page not present or supervisor privilege.` (W1-B-pp-r1
elog 88); the 22:32:16 group is pid 422387, address `0x7f11181e9000` (W1-A-pp-r1 elog 86), and card
09:00.0 carries the 2026-10-22 wedge history (`journal/pool-admission-fix-2026-11-21.md:1215-1216`).

## Today at 256/192

| Boot | Engine start | Outcome | Source |
|---|---|---|---|
| W1-B-pp-r0 gen 1, 2 | 21:43:12, 21:58:16 | elogs stop at 6 lines (1-6, 7-12); 880 s deadline, then Ctrl+C at 240 s | B-r0 elog 6, 12; boot log 50-51, 94-100 |
| W1-B-pp-r0 gen 3 | 22:22:03 | leg done, 301.6 tok/s in 193,713 ms; clean stop | B-r0 elog 18, 117; boot log 144-145 |
| W1-A-pp-r1 gen 1 | 22:27:40 | XDMA fault 22:32:16 (4 m 36 s in), pid 422387 | A-r1 elog 6; kernel log |
| W1-A-pp-r1 gen 2 | 22:32:22 | leg done, 290.8 tok/s in 200,883 ms | A-r1 elog 92, 189 |
| W1-B-pp-r1 gen 1 | 22:43:50 | XDMA fault 22:48:21 (4 m 31 s in), pid 784900 | B-r1 elog 6, 88; kernel log |
| W1-B-pp-r1 gens 2-8 | 22:48:23 | 7 recoveries, all `helper channel ended`, no leg; stop 22:51:10 | `run/W1-B-pp-r1.supervisor-events.log` 1-9 |

The D-state is only half confirmed: two consecutive boots whose elogs never passed 6 lines, a server
that waited to `still starting (880 s)` before `start_engine` timed out (boot log 50-51), a second
attempt interrupted at 240 s (boot log 94-100), and in the previous boot `GPU reset end with ret = -19`
every 2 s from 21:43:15 (`journalctl -k -b -1`). The name `drm_sched_entity_flush` is in no file I
read today; the same wedge was journaled on 2026-11-11
(`journal/serving-tg-reeval-split-retest-2026-11-11.md:3923`). The 22:04:03 reboot fell between
W1-B-pp-r0's gen 2 and gen 3.

## The prompt is 58,423 tokens, not 239k

| Fact | Value | Source |
|---|---|---|
| corpus file | 323,200 bytes, sha256 `210b16b32b8658e68b863442d3c2d9ebde7eb3c9d50b0cea8c28e7d045c0ea68` | `out/port-ab-20261123/corpus/pp239k.ctx323200.bin` (stat, sha256sum); same sha in `records/W1-A-pp-r1/A-pp239k.json:14` |
| tokens actually read | 58,423 | A-r1 elog 189; default-margins elog 103, 211 |
| harness expectation | 239,663 ±10 % | `port_ab_contract.py:42`; gate `port_ab_legs.py:639-641` |

The `pp239k` label is stale: a 323,200-byte text file (head tag `ctx=323200`) that tokenizes to
58,423 tokens, below 0.9 × 239,663, so a verify would report `served 58423 tokens, expected ~239663`.
No leg-gate report exists in either out directory, so I cannot say whether that check ran.

## The first default-margins boots

Engine `run/port0218-bin/rpcw-hip/strata`, sha `936efcc7…` — W1 arm B's binary
(`out/port-ab-20261123/boots/W1-B-pp-r1.json`, `engine_sha256`); neither boot has a reserve flag
in argv (`out/faultdiff-r1/configs/default-margins.json`, only the note at line 137).

| | Boot 1 | Boot 2 |
|---|---|---|
| engine started | 22:58:10 (elog 6) | 23:07:45 (elog 114) |
| port | 8019 (record 66) | 8009 (record 66) |
| expert cache auto | 11.14 GiB free, **700 MiB reserved** → 4116 slots (elog 40) | same (elog 148) |
| free VRAM, loaded | **728** card0 / **648** card1 MiB (elog 79-80) | 728 / 648 (elog 187-188) |
| free at request start | 465 MiB (elog 105) | 465 MiB (elog 213) |
| Foresight swap | `card0:2, card1:8` granted (elog 77) | same (elog 185) |
| prefill leg | 287.4 tok/s in 203,252 ms (elog 103); record wall 205.453 s (record 11) | 294.4 tok/s in 198,445 ms (elog 211); wall 200.610 s (record 11) |
| kernel fault records since 22:58:00, engine fault lines | 0, 0 | 0, 0 |

Same boot shape at 256/192, arm B: `expert cache auto: 11.14 GiB free, 256 MiB reserved … ->
4291 slots` (B-r0 elog 52; B-r1 elog 40), `398 MiB` card0 / `436 MiB` card1 (B-r0 elog 93-94;
B-r1 elog 81-82), swap `every card's slots clamped to zero` (B-r0 elog 91; B-r1 elog 79).

Memory during the legs: `min_avail_mib 12692`, `max_swap_used_mib 4369`, card3 16032 / card2 15978 /
nvidia0 7592 (`…/default-margins-b2/B-pp239k.json` 40-46); boot 1 11308 / 4372 / 16030 / 15977 /
7592 (default-margins record 40-46), against `max_swap_used_mib 0` on W1-B-pp-r0
(`records/W1-B-pp-r0/B-pp239k.json:41`). `max_rss_mib` reads 227 and 224 there (line 39) where
peers read 40924 and 41379 (A-pp239k.json:39, B-pp239k.json:39): those are the SERVER process, not
the engine — the driver was pointed at the server pid (boot 2's record `engine_pid 1343791` is
`server_pid` at `out/faultdiff-r1/run/default-margins.state.json:117`), so the engine's RSS is not
in these records. It stands in the load records instead, where the pid came from `find_engine` on
the leg socket (`faultdiff_run.py:314,638-639`): `engine_pid 1344547`, `max_rss_mib` 41,946-41,956
(`out/faultdiff-r1/records/default-margins/load/faultdiff-default-margins-n1..n3.json:46,49`).

Boot 1 was stopped to move port (`strata prefill: stopped in the chunk from token 0 before layer 34
(STOP)`, elog 108). The generator now uses 8009 (`faultdiff_configs.py:63`): while prod is down,
trials run on 8009, not port 8019.

## Two confounds

1. **Defaults are not single-variable.** At 700/700 the Foresight swap is granted (card0:2, card1:8,
   elog 77) where 256/192 granted 0 slots; the harness says so itself (`port_ab_contract.py:24-27`).
   On these legs it carried no traffic: `0 experts served from the swap space, 0 copies` (elog 101, 209).
2. **Swap was live:** 4,372 and 4,369 MiB during the legs against 0 on W1-B-r0 — named, not fixed.

## What this does NOT show

- **n = 2 boots.** Not a fix; W1-B-pp-r0 gen 3 also ran a leg to completion at 256/192.
- **No fault-time VRAM reading, no link to margins.** The `mem` block is per-leg, so 400/436 MiB
  are request-start readings, not 22:32:16; the fault is read-only, on a host VA, with
  `WALKER_ERROR`/`MAPPING_ERROR` zero — not a card-local allocation running out. Static reading.
- **Margin correlation has already failed once.** `journal/pool-admission-fix-2026-11-21.md:
  1213-1222` records 10 further XDMA faults at the reverted 36,864 MiB budget after a full reboot
  and calls the budget correlation DISPROVEN.
- **No speed claim.** 287.4 and 294.4 against one 256/192 leg at 301.6 tok/s is n = 2 vs n = 1, with
  the swap grant and ~4.4 GiB of swap differing; W1d is descriptive-only (`port_ab_contract.py:156-158`).
  Replies differ too (`b3482dfb…` at 256/192, `…/W1-B-pp-r0/B-pp239k.json:28`; `01db7a9d…` on both
  default legs, record 28).
- **Nothing about arm A**'s margins; A-r1's record is the replacement generation's. Gen 1 (fork
  engine) took the XDMA fault at 22:32:16 and its in-flight prefill died with the engine (`exit code
  -6`, boot log 39-40); the pair supervisor replaced the generation
  (`out/port-ab-20261123/run/W1-A-pp-r1.supervisor.json`: `generation 2`, `engine_pid 527427`) and the
  leg was re-fired at it, writing the record at ts 1795469857.584 (22:37:37, after the fault) with
  that pid (`records/W1-A-pp-r1/A-pp239k.json:36`). The 203.3 s is the replacement's, not gen 1's —
  hence the failed (boot log 1-41) and completed (78-81) request in one file.

## Next

- The W1d default-margins A/B window exists in the harness (`port_ab_contract.py:142-161`; 2 claim +
  2 warmup boots per arm, W1's pins untouched, 140-141) but has no artifacts: no `W1d` entry in the
  manifest, and three harness scripts differ from the shas at manifest 1926-1928 — the drift
  `port_ab_prepare.py:419` calls `harness script changed since prepare`, so
  `port_ab_prepare.py --prepare` must run before its first boot.
- The fault track's differential legs are unrun: only `default-margins` and `default-margins-b2`
  have records; the other frozen configs under `gen/faultdiff/` have none.
- Decide the 239k mismatch before more W1/W1d legs are read as 240k.

## Closed 2026-11-23 23:33: margins reverted; the fault is margin-independent in this window

The operator returned the serving tuple to 256/192 (`prod-config-port0218.json`, unchanged) at 23:33 and
closed this experiment; `prod-config-port0218-defaultmargins.json` (armed in `db53fa43d2`) is deleted in
the same commit so it cannot be booted by accident, and the base config and its launcher stay untouched.

- **The fault returns at 700/700.** W1d-B-pp-r0 (port engine `run/port0218-bin/rpcw-hip/strata`, both
  reserve flags absent — `700 MiB reserved`, elog 40 — start 23:25:38, elog 6) took the XDMA fault at
  23:29:49, engine pid 1756818 (`journalctl -k -b` 2010-2014; elog 85, `0x7f9161ff2000`) — **251 s** in,
  against **276 s** (W1-A-pp-r1 gen 1, 22:27:40 → 22:32:16, pid 422387, `-k -b` 1759) and **271 s**
  (W1-B-pp-r1 gen 1, 22:43:50 → 22:48:21, pid 784900, `-k -b` 1901) at 256/192. Same 10 records,
  `0x00800031`, XDMA, `RW: 0x0`, pages inside one 128 KiB host-VA window.
- **Faults per prefill attempt: 2 of 4 at 256/192, 1 of 4 at 700/700**, Fisher exact two-sided p = 1.0.
  256/192 completed W1-B-pp-r0 gen 3 at 301.6 tok/s (elog 117) and W1-A-pp-r1 gen 2 at 290.8 (elog 189);
  700/700 completed default-margins b1 at 287.4, b2 at 294.4 (legs above) and W1d-A-pp-r0 at 262.8
  (elog 101). Nothing in this data separates the settings, and the Foresight swap co-varies with them in
  every boot read (0 slots at 256/192: W1-B-r0 elog 89-91, W1-A-r0 elog 74-76; `card0:2, card1:8` at
  700/700: W1d-B elog 77, W1d-A elog 74), so margins, headroom and swap cannot be told apart either; the
  arms also differ in engine binary (`fsclamp-bin` A, `port0218-bin` B), a third co-variable.
- **Nothing survived.** Every non-faulted boot in the window was ended by `port_ab_stop.sh` (`boots/*.json`,
  `ended_by`), and the longest fault-free boot that served a prefill is **626 s** (default-margins b2:
  23:07:45 elog 114 → 23:18:11, prefill leg plus 6 decode requests at 27.3-38.4 tok/s, boot log 94-178).
  The longer fault-free walls served nothing: W1-A-pp-r0, 886 s, no request.
- **One fault is per attempt, not per boot.** W1-A-pp-r1 gen 1 faulted at 22:32:16; its successor started
  6 s later (22:32:22, elog 92) and completed the identical 58,423-token prefill at 290.8 tok/s (elog 189).
- 256/192 stays the serving tuple. The W1d window stays in the harness with 2 of its 6 boots used (one per
  arm, no claim pair) — superseding the "no artifacts" note under *Next* — and will not be continued.

