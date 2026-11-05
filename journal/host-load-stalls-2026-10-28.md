# Strata stalls under hostapp load despite nice 0 — GUAM, 2026-10-28

User: hostapp running (a Windows hostapp), Strata stalling/crashing again. Read-only investigation;
no settings changed, no restarts forced. Nice fix verified live: engine PID 4186355 at NI=0 all day.

## Event map (engine log / prod log / journalctl)

- 08:42 server restarted (PID 2118909 → 2177557, new log prod-...20261028-084246). Engine exit-code-1
  restarts at 08:50:02 and 08:53:15 with no stall/cudaMalloc line near them — pre-load startup retries,
  self-healed; stable 08:53 → 11:03 (2 h 10 m).
- 11:03:34 #29 abort (exit -6): "no progress for 60 s during a request (request -1)" — the
  conversation-parking pseudo-request. Stall report: **host idle 452 220 ms** (7.5 min!), 24/24 expert
  jobs done, 7/7 workers sleeping, 0/36 threads in D, VRAM 561 MiB free at request start, 9.6 GiB RAM
  available, 3.5 GiB of engine in swap, 84 529 major faults. Immediately before:
  "conversation cache: skip parking (snapshot 2273 MiB exceeds available budget)".
  On stall handling: "released the verify window's GPU waits (#267): the GPU finished in 268 ms"
  (and 15 ms) — **the GPU was never hung; the host's waits were starved**.
- 11:04:35 restart attempt: cudaMalloc(1.54 GB) weight arena failed, 556 MiB of 16.4 GiB free —
  dying engine still held VRAM → exit code 1. 11:04:52 second restart succeeded (MemAvailable 51 GiB).
- 11:06–11:07: prompt read stuck at 20 189 tokens for 30+ s, then resumed (26 867/26 868 in 27 s);
  decode 19–29 tok/s (normal ≈ 29–31); request completed 24.8 tok/s. Degraded, not dead, while background-load.

## Kernel proof of mechanism

- 11:06:44 `strata: page allocation failure: order:4 GFP_KERNEL|__GFP_ZERO|__GFP_COMP` with call trace
  through **kfd_wait_on_events** (amdgpu) — "11 callbacks suppressed" ⇒ 12 occurrences. The ROCm
  event-wait ioctl path itself could not get a compound page. This is the mechanism that starves the
  host callback thread → engine watchdog sees 60 s no-progress → abort.
- No amdgpu ring/timeout/reset events today; no OOM kills. GPU errors are excluded per user (self-induced).

## Contention measurements under load (11:07, hostapp running)

- Hostapp: a Windows hostapp via Proton, `CrBrowserMain` 275–288 % CPU, ~2 GB RSS.
  DRM map: card1=04:00.0 RVII#1 (16.6 GB, strata), card3=09:00.0 RVII#2 (16.4 GB, strata, 100 %),
  **card4=0a:00.0 Cezanne iGPU 97 % busy for the hostapp: 2.78 GiB GTT + 0.51 GiB UMA = ~3.3 GiB system
  RAM stolen**, sharing DDR bandwidth and the single amdgpu driver with the compute cards.
  card2=01:00.0 RTX 3080 Mobile idle/unused.
- Memory: MemFree ~450–540 MiB, MemAvailable ~9.7 GiB, engine RSS 43.6 GiB, kswapd active.
  **Major faults ~1 202/s** (6 032 in 5 s); vmstat: 400–500 MB/s block reads — the 40 GiB expert arena
  (file-cache mmap) is thrashing between RAM and NVMe. Interrupt rate 450 k–1 M/s, ctx switches
  110–155 k/s. CPU not saturated (45–47 % idle): **not a CPU-priority problem** — nice was never this
  failure class.

## Root cause chain

iGPU hostapp (RAM theft + DDR bandwidth + amdgpu-driver-wide contention) + 40 GiB arena + desktop ⇒
sustained reclaim ⇒ (a) arena page thrash (slow prefill/decode), (b) fragmentation → order-4 alloc
failures inside KFD wait path → host event starvation → #29 aborts, (c) post-abort restart races the
dying engine for VRAM → cudaMalloc exit-1 → double restart. Vorta evenings stack a third RAM/IO consumer
(same mechanism, seen 2026-10-21..27).

## Candidate remediations (NOT applied; need approval)

1. `vm.min_free_kbytes=524288` + `vm.watermark_scale_factor=100` — keep reclaim ahead of demand so
   order-4 compound allocations stop failing; directly targets the crash mechanism. Reversible sysctl.
   (The old vram-swap service used to set min_free_kbytes=1 048 576; it is offline now.)
2. zswap `max_pool_percent` 3 → 10 (~6.2 GiB pool; measured 2.21:1 ⇒ ~7 GiB effective extra RAM),
   cutting arena eviction. Live-tunable, reversible; earlier "don't enlarge" advice predates the
   measured under-load ratio.
3. Run the hostapp on the idle RTX 3080 instead of the Cezanne iGPU (user decision) — removes the 3.3 GiB
   theft, the DDR-bandwidth drain, and iGPU↔compute driver contention at once.
4. Avoid hostapp + Vorta simultaneously with long-context sessions.

## Handoff 11:20 — commands given to user (not run; sudo unavailable in this session)

- Fix 1 live: `sudo sysctl -w vm.min_free_kbytes=524288 vm.watermark_scale_factor=100`; persist via
  `/etc/sysctl.d/99-strata-memory.conf`. Expect a brief initial reclaim burst while the reserve fills.
- Fix 2 live: `echo 10 | sudo tee /sys/module/zswap/parameters/max_pool_percent`; persist via
  `zswap.max_pool_percent=10` on the kernel command line (`/etc/default/grub` + `update-grub`).
  Caveat repeated to user: running boot has `zswap.enabled=0` while grub default says `=1` — verify
  `/proc/cmdline` after next reboot; runtime-enabled zswap does not survive reboot without it.

## Post-fix crashes 11:24–11:45 — revised diagnosis

Fixes verified live (min_free 524288, wsf 100, zswap 10% holding 3.1 GiB at 2.37:1; **0 page-allocation
failures since 11:10**; MemFree reserve held ≥2.2 GiB). Yet #29 aborts continued: 11:03 (parking),
11:24, 11:36, plus a restart ~11:39:53. New evidence:

- 11:24 stall: host idle 406 s, epoch 0, **"the GPU rang 0"** — zero KFD events since engine start;
  GPU finished in 0–1 ms after forced release. 0/38 threads in D. Deadlock-class event blackout.
- 11:36 stall: host idle 87 s, GPU rang 32, **self-recovered 2 s into the report** (aborted anyway).
- 11:42 probe: IRQ80/card1 fires 6,234/s under load (earlier "dead IRQ" was an idle-window artifact,
  refuted); card1 100 % busy on a 51 291-token prefill while card3 0 %; a 5-token chat request timed
  out at 120 s (serialized behind the big prefill).
- 11:45: prompt read frozen at **exactly token 18 823 of 51 291 for 235+ s — the same offset as the
  11:24 stall**; both compute GPUs 0 % busy; IRQ85/card3 0/s; engine 166 % CPU; new PID 482565
  (restart ~11:39:53). The engine log shows the user's session carries a ~51k-token prompt; 11:36's
  "done up to token 51241" was the same prompt further along.
- Interrupt load while background-load: CAL/IPI ~25K/s, TLB shootdowns 0.5K/s (2.43 G lifetime). Hostapp
  (renderD131, iGPU 92–97 %, ~3.3 GiB GTT+UMA) holds all four render nodes' fds; engine also holds
  renderD131 (ROCm enumeration).

### Revised root-cause statement

The crash loop = **51k-prompt re-prefill under hostapp load**. Every #29 abort wipes KV; the server
replays the whole 51k prompt at ~128 tok/s (CPU expert path, hostapp stealing 3 cores + DDR), with
60-s-class event blackouts ("GPU rang 0", host idle minutes, GPUs idle 0 % — lost-wakeup signature,
not GPU hang, not RAM exhaustion anymore, not nice). Whether the blackout is caused by amdgpu-wide
serialization with the 97 % iGPU, KFD event-page latency under IPI storm, or a card3-specific
pathology (card3 = the historically wedged 09:00.0, anomalous 750/s idle IRQs) — passive observation
cannot separate; needs the A/B below.

### Decisive next tests (user-executed)

1. Pause/quit the hostapp 2 min, retry the same session → instant vs stuck discriminates iGPU-load
   coupling vs persistent driver/engine state.
2. The planned reboot (swap-order fix) also resets amdgpu/timer state — retest before restarting
   the hostapp.
3. Trim or restart the 51k-token session regardless: it is the loop's fuel. Each abort costs a full
   replay.
4. If it still hangs on a fresh boot with no hostapp: upstream issue #29 with these stall reports.

## Watchdog extension 11:55 (applied at user request)

- Engine watchdog knobs found in `src/program/generate.cpp:11520–11525`: `STRATA_WATCHDOG_S`
  (default 60 s, 0 = disabled) and `STRATA_WATCHDOG_IO_S` (default 10 × limit, slow-storage grace,
  only applies while major faults/reads advance — our blackouts had neither, so it never fired).
- Added `"STRATA_WATCHDOG_S": "300"` to all 7 prod configs next to `STRATA_ENGINE_NICE`; JSON
  re-validated, `git diff --check` clean. Takes effect at the next server restart (config env is read
  once at server start, `server.py:8266/3497`); the planned reboot covers it.
- Trade-off stated to user: 300 s survives the self-recovering blackout class (the 11:36 one recovered
  at ~90 s — restart and full 51k replay avoided) but a deep hang (11:24: 6.8+ min) now takes 300 s
  instead of 60 s before auto-recovery. STRATA_WATCHDOG_S=0 (full disable) deliberately not set:
  an unattended hang would then never self-recover.

## 2026-10-29 zswap verdict (user suspected it was not helping — confirmed)

Status first: server restarted 03:34:22 today; live engine 4029468 carries `STRATA_ENGINE_NICE=0`
**and `STRATA_WATCHDOG_S=300`** (verified via /proc environ). Zero #29 stalls since yesterday
13:41:45 (11 on Oct 28). No reboot yet (still the Oct-27 boot): cmdline `zswap.enabled=0`, runtime
zswap on (lz4, 10 %), min_free 512 MiB + wsf 100 live, NVMe swaps (prio 2) carry 61 of 62 GiB swap used.

Measurements (10:34–10:37, model serving, 247 % CPU, RSS 45.7 GiB):

- Pool ratio collapsed to **0.91–0.99 : 1** across three samples (e.g. 3 046 768 kB pool for
  3 337 332 kB original). Below 1:1 = the pool costs MORE RAM than the pages it holds
  (quantized weights compress ~0.9, zsmalloc overhead eats the rest). Yesterday's 2.2–2.4:1 was the
  compressible-desktop phase; under model load the store stream is model pages.
- Churn: zswpin +339 768, zswpout +289 066 in 10 s (~30 k pages/s each way), zswpwb 0. The pool is a
  seconds-scale transit cache cycling the same ~3 GiB window: LZ4 compress+depress on ~60 k pages/s
  for zero net RAM. Lifetime zswpin/zswpout = 91 % re-reference; 47.8 M pages (≈187 GiB) written back.
- Remaining "stalls": pgmajfault ~578/s under load, allocstall/compact counters flat since the
  min_free/wsf fix (kernel page-allocation failures: 1 since Oct 28 12:00). That is the arena
  working set > RAM (AnonPages ≈ 50 GiB), not zswap.

Verdict + advice: disable zswap (`echo N | sudo tee /sys/module/zswap/parameters/enabled`; one-time
pool write-back burst). Edit `/etc/default/grub` zswap.enabled=1 → 0 (it currently contradicts the
running cmdline; next reboot would silently re-enable it otherwise) and drop any zswap.* params added
there. Keep min_free/wsf persistence and NVMe-first swap ordering. Not applied by me: no sudo.

## 2026-10-29 zswap disabled by user; rcu_nocbs/nohz_full question

- User disabled zswap at runtime (accepted the advice). grub-default contradiction (=1) not yet
  confirmed fixed — recheck at next reboot via `/proc/cmdline`.
- User asked whether `rcu_nocbs=all nohz_full=all` would help. Answered: no for this failure class
  (GPU event delivery + memory thrash, not tick jitter). Reasons recorded: multi-task shared cores keep
  ticking; CPU 0 (engine host thread) excluded as boot CPU; nohz_full's context-tracking tax hits the
  ioctl-heavy KFD path; grub's commented-out line shows rcu_nocbs was tried before and did not stay.
  Offered the targeted alternative: IRQ 90 (iGPU, CPU 12 — a pool-worker core) and friends moved to
  the aux CPU 1. nohz_full pair not applied.

## 2026-10-29 11:42 — zswap re-enabled at 3 %, verdict "mildly positive, workload-dependent"

- User re-enabled zswap with max_pool_percent=3 (old value), lz4, accept 90, shrinker on.
- Snapshot (engine 383 % CPU, NO hostapp): ratio **2.29:1** (341 MB pool / 782 MB stored, 441 MB saved),
  quiet churn (zswpout/zswpwb 0 over 10 s, zswpin 345/s ≈ 1.4 MB/s NVMe reads avoided).
- Framed as not apples-to-apples: yesterday's 0.91–0.99:1 was under hostapp load cycling engine anon
  (int8 KV / conversation snapshots, entropy-heavy). Also recorded: the 40 GiB expert arena is
  file-backed mmap — it never enters zswap; only anon does. Same-instant pgmajfault 4 582/s
  (arena thrash) remains untouchable by zswap in either direction.
- Persistence warning given: grub default has only zswap.enabled=1 → next boot = kernel defaults
  (LZO, 20 % cap), not lz4/3 %. To persist: add `zswap.compressor=lz4 zswap.max_pool_percent=3`
  to GRUB_CMDLINE_LINUX_DEFAULT + update-grub.

## 2026-10-29 17:31 — post-reboot kernel-param review (new kernel 7.3.0-x64v3-xanmod1, boot 17:22)

Good: swap ordering FIXED at boot (3 NVMe files prio 2 incl. new /mnt/swap5; /home prio 1, unused);
Strata auto-started with STRATA_ENGINE_NICE=0 + STRATA_WATCHDOG_S=300 verified in engine env;
preempt=full, swappiness 100, numa_balancing=0; full amdgpu/PCIe stability stack carried over.

Regressions found and reported to user (not applied — no sudo):
1. **P0** vm.min_free_kbytes back to 67584 and wsf to 10 (persistence file never created) — the
   allocation-failure fix is gone; re-apply + create /etc/sysctl.d/99-strata-memory.conf.
2. **P1** zswap re-enabled by kernel default (DEFAULT_ON, param removed from grub): **lzo + 20 %**.
   Measured 2.34:1 at light load (2.4 GB pool / 5.7 GB stored) but flagged as the riskiest config
   under hostapp load (RAM-negative mode measured 2026-10-28, now scaled 2× with slower compressor).
   Options given: cmdline lz4+10 % (recommended) or zswap.enabled=0.
3. **P2** THP defrag=madvise → defer (1.29 M lifetime sync compaction stalls; keep THP=always for
   the arena). Runtime + tmpfiles.d persistence.
4. **P3** IRQ 90 (iGPU, hostapp) on CPU 12 = engine pool-worker core; nvme IRQs on 2/3/6. Shift to
   odd/housekeeping cores offered.
New-kernel note: 7.3.0 amdgpu behavior under hostapp load unverified; first background-load session is the test.

## 2026-10-29 17:35 — tuning script created

`/home/user/scripts/apply-strata-tuning.sh` — one idempotent script applying all recommendations:
memory floor (live + /etc/sysctl.d/99-strata-memory.conf), zswap lz4/**3 %** (live + grub cmdline
rewrite stripping old zswap.* tokens + update-grub; pool cap now the ZSWAP_POOL_PCT variable at the
top, 10 → 3 at user request 17:40), THP defrag=defer (live + tmpfiles.d), and
amdgpu+nvme IRQ affinity onto odd cores 3–15 (engine keeps 0/2,4../1; installs
strata-irq-affinity.service boot hook unless --no-boot-hook). Flags: --dry-run (no root needed,
validated: 21 IRQs identified incl. two on CPU 0), --irqs-only (boot-hook mode). Dry-run
verified 0 failures; real run pending user execution (sudo needed). irqbalance inactive on this
box, so affinities will stick.

## 2026-10-29 17:38 — first real run (user-executed): all recommendations live

17:38 run result: sysctl floor, zswap lz4/3 %, grub cmdline + update-grub, tmpfiles, boot hook —
all applied and verified live afterwards. "17 failures" were two script defects, both benign:
(a) THP defrag read-back compared against sysfs's bracketed `[defer]` — the write had SUCCEEDED
(verified `[defer]` live); fixed by stripping brackets. (b) IRQs 60–75 are nvme0q1–q16, kernel-
managed per-CPU queue IRQs whose affinity userspace cannot change — script now SKIPs them.
Effective IRQ state after run: amdgpu irq 90 (iGPU/hostapp) core 12 → 3, irq 82 → 15, irq 89 → 11,
nvme0q0 59 → 3. Both fixes patched into the script; re-run shows honest PASS/SKIP/exit 0.

## 2026-10-30 16:51 — first background-load day on the full fix stack

- Since 2026-10-29 17:22 boot: exactly **1 engine abort in 22.5 h** (06:43:47, hit the full 300 s
  watchdog, same "waiting for the GPU (attention, router)" prefill blackout class) vs 11 in one
  morning pre-fix. ~90 % reduction.
- Live with hostapp on: a prompt read froze ~60 s at token 20 127 (103→163 s) then **resumed on its
  own** — the 300 s watchdog absorbed the blackout without abort, keeping KV (the design goal).
  Engine serving at 29–39 tok/s decode, 93–94 % expert-cache hit. Memory healthy: MemAvailable
  8.7 GiB, swap-in 144 kB/s. zswap 0.99:1 (incompressible hostapp/buffer mix — expected).
- Note: my earlier "engine DOWN" was a pgrep artifact (pattern matched my own shell); server and
  engine were up throughout (port 8009 listening).
- Residual class unchanged: GPU event-delelivery blackouts under hostapp load. Options remain:
  hostapp → RTX 3080, full CPU isolation, or upstream #29 report with the 06:43 stall report.
- 2026-10-30 ~17:00: at user request the tuning script now DISABLES zswap (live `enabled=N` +
  cmdline `zswap.enabled=0`, required because the kernel re-enables it by default). Dry-run 0
  failures; real run pending. Rationale: measured ~1:1 under load, NVMe swap fast and ordered.

## 2026-10-30 17:07 — correction + second abort: user switched to a -mixed build

- Monitoring correction: the server now runs `prod-config-swift15-dflash-w4-q4_0-mixed.json` with a
  different binary (`worktrees/cuda-helper/build-mixed-gpu/r4-dflash-after/bin/strata-hip`,
  `--expert-helper-socket/-slots 3400/-timeout-ms 100`). Its engine log is
  `engine-...-mixed.log` — my earlier counts watched only `-sampled.log`, so "1 stall in 22 h"
  undercounted. True total across both logs: **2 aborts in ~23 h** (06:43 -sampled, 16:41 -mixed),
  both under hostapp load, both deep ≥300 s blackouts.
- 16:41 stall report (-mixed, parking the previous conversation): host idle 330 967 ms, all workers
  asleep, GPU rang 32, 0/36 in D, 7.8 GiB RAM available, released → **GPU finished in 7 177 ms**
  (real queued work drained slowly, unlike the earlier 0–268 ms class). Restart hit the usual
  cudaMalloc VRAM race (612 MiB free) → double start. zswap was already disabled; memory healthy.
- Verdict unchanged and now cross-build: memory/nice/zswap fixes hold (all healthy inside the
  reports); the residual is the amdgpu event-delivery blackout under iGPU hostapp load — the ≥300 s
  class the watchdog cannot absorb. Remaining options: hostapp → RTX 3080 (decisive, removes shared
  amdgpu contention), full CPU/IRQ isolation, or upstream #29 with reports from both builds.

## 2026-10-30 ~17:30 — "it used to work better": regression timeline assembled

- Kernels in /boot: 6.14 stock (Nov 2025), 7.2.9 (Oct 3), 7.2.11 (Oct 15), 7.2.12 (Oct 21),
  7.3.0 (Oct 23 build, first booted Oct 29). The crash-loop era began Oct 21 — the day 7.2.12
  landed. 7.2.11 is the last kernel from the reportedly stable background-load era and is still bootable.
- Engine changed twice since: build-dfsample-1 (Oct 24) → cuda-helper worktree r4-dflash-after
  (-mixed, Oct 29-30). Stalls persisted across both builds.
- VM-policy era shift (journal/igpu-nvidia-swap-2026-10-26.md): the NBD-VRAM-swap era ran
  swappiness 150–200, min_free_kbytes 1 GiB, NBD swap prio 1500. Today: swappiness 100,
  min_free 512 MiB, NBD offline. Oct 26 stall signatures (mmap_read_lock/mprotect D-state,
  GPU released after 0.6–25.6 s) differ from today's pure event blackouts — the mechanism evolved.
- Workload grew: sessions now 51k–104k tokens with 2+ GiB parking snapshots; every abort forces a
  full replay. Part of "less inference than before" is session growth, not only system regression.
- User's GPU-settings testing Oct 25–26 (self-acknowledged) muddies that window.
- Decisive regression test recommended: boot 7.2.11 (GRUB advanced menu), same -mixed config,
  one background-load session. Vanishing stalls ⇒ kernel-side amdgpu/KFD regression in 7.2.12/7.3.0
  xanmod builds; persisting stalls ⇒ engine/workload/hardware era. Optional cheap trial in the
  current era: min_free_kbytes back to the old-era 1 048 576.
