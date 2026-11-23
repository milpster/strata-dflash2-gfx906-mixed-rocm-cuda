# Fault-track differential (window `faultdiff-r1`)

Seven frozen configs, each the promoted prod tuple with **one** thing changed, for the fault
track of the approved contract (`/home/srcds/.opencode/plan/measurement-contract-speed-attribution-faults.md`,
Phase 4). The fault is a kernel-side page permission fault on the engine's host pages, logged on
the XDMA client (vmid 8) on card 1 (`0000:09:00.0`), which ends the generation after 5-15 minutes
under serving load (`bench/gpu-fault-repro/README.md`).

- Base: `prod-config-port0218.json` (the promoted port tuple - the tuple that serves and therefore
  the tuple the fault hits), sha256 recorded in `manifest.json` (`base_sha256`).
- Generator: `../faultdiff_configs.py` (beside `port_ab_configs.py`). It is the only writer here:
  every config is derived from the base, every change is declared data, and `--check` fails closed
  when a file, a delta or the base drifts.
- Runner: `../faultdiff_run.py` boots, watches, loads and stops one leg through the prod launcher's
  own sequence on the trial port 8019. Nothing in this directory was hand-edited or booted while it
  was written (fixer lane: configs, guard code and static checks only).

Do not edit a config by hand. Change `faultdiff_configs.py`, run it, and review the diff.

## Order of the window - the app-exoneration control runs FIRST

1. **Build and run the minimal HIP repro** (`bench/gpu-fault-repro/README.md`, unmodified):
   positive control first (expect exit 3 or a signal kill with `FATAL signal`), then the long
   `--mode cycle` leg. This is the only control that can exonerate the application without
   instrumentation: the same eviction/refault/re-registration sequence with no Strata code.
   - repro faults -> the platform space is implicated; the engine's buffer handling is exonerated
     as the *origin*, and the legs below can then only find a mitigation, not a cause.
   - repro clean -> this path is stable on this box. It does **not** exonerate the engine's
     remaining deltas (pool growth, SVM range churn, concurrent queues, the 40 GiB working set),
     and it does not clear the hardware.
2. **Then the config legs**, one boot each. They run with prod stopped (exclusivity), so they are
   separate boots in their own window, never on a speed leg.
3. **Hardware stays OPEN** until the software/platform space is exhausted: slot swap and card swap
   come only after these legs and a stock-build test (`docs/AMD_HIP.md` territory), never in place
   of them.

## The differential

| leg | the one variable | narrows (if it stops the fault) | CANNOT exclude |
|---|---|---|---|
| `parking-off` | `--conversation-cache-mib 0`, no `--conversation-cache-dir`, no `--conversation-cache-disk-idle-save` | the whole park/restore path (host snapshot, disk park stream, fadvise of the parked file, restore re-registration) as a precondition | parking as a *trigger*: with the path off no conversation is ever restored either, so a clean leg only says "needs the park path". Read-only serving, the helper's DMA and the platform stay open. |
| `park-on-no-evict` | base minus **only** `--conversation-cache-evict-page-cache` | the page-cache release (fadvise DONTNEED on park writes, restore reads, ring sidecars) as a precondition, with parking and the disk tier intact - the cleanest single-variable leg | page-cache pressure as a general factor: without the release the parked file's pages stay cached, so the memory shape differs (less free RAM, more Cached) in both outcomes. |
| `dflash-off` | `--dflash` and `--dflash-block` removed (drafter not loaded) | the DFlash drafter's own path: its artifact, its draft passes, the cross-GPU tap collection over the layer split, the drafter's DMA reads | the drafter as a *rate amplifier*: decode is slower, so equal wall time is a smaller exposure (record requests and tokens). The verify window (spec 5) and its kernels stay. |
| `single-card-0` | `--gpu 0` (and the `gpu` key) | the split and every cross-card transfer as a precondition; with `single-card-1` it separates "the tuple" from "this card" | the slot/board: card 0 is another physical card in another slot. A fault here moves the question, it exonerates nothing. |
| `single-card-1` | `--gpu 1` (the faulting card alone) | if the fault continues with no second card and no split, the candidate set is this card's own path | one card holds every layer, so expert cache, KV residency and page shape all differ from the base (recorded from the boot lines, never assumed equal). |
| `xnack-1` | `HSA_XNACK=1` in the env block | the runtime's xnack setting as the difference between "the kernel kills the process" and "the fault is retried/handled" | anything about the access pattern: xnack changes what the platform does *after* a bad access, never which access is bad. |
| `xnack-0` | `HSA_XNACK=0` (the base leaves it unset) | the same split from the other side: with xnack explicitly off a permission fault has no retry path | whether unset already meant off on this runtime - the unset base state is the third data point, and it is only a data point if it is recorded, which the boot record does. |

Every leg also carries these, identical on purpose (not a variable): engine `936efcc7`, helper
`58c3ae02`, `--vram-reserve-mib 256 --vram-reserve-later-mib 192`, `STRATA_RAM_BUDGET_MIB 36864`,
`--max-context 262144 --kv int8 --kv-resident 262144 --kv-resident-mib 4096`,
`--expert-helper-slots 4600` (helper `--fill-mib 0`, its own budget governs - the fill report is
recorded every boot), `--conversation-spill-mib 0`, `--spec 5 --spec-min-p 0.5`, `--layer-split 30`
(key; the server appends it only with more than one GPU), `--vision`, the sampling block, and every
env key except the leg's own. The only differences are the harness paths (log, port 8019, the two
helper sockets, the per-leg cache dir) and the base's provenance notes.

## Why `--spec 0` is not the dflash-off leg

The review prescribed `--spec 0` (plain decode) or the equivalent. Checked against the port
engine's parsing and startup validation (`src/`, the same strings as the pinned binary):

- `--spec 0` is parsed and means plain decode (`generate.cpp:929`, `:4089`).
- But `--dflash` refuses a window below 2: `strata generate: --dflash requires --spec T (2 <= T <= 8;
  the window is 1 anchor + K candidates)` - exit 2 (`generate.cpp:7970`).
- And serve refuses it too: `strata serve: needs --spec T and --prefill CHUNK ...` - exit 2
  (`generate.cpp:11754`).

So `--spec 0` cannot boot this rig at all; the bootable equivalent is "no drafter": remove
`--dflash` and `--dflash-block`, keep `--spec 5`. With no drafter the serve path falls back to the
prompt-lookup drafter or plain one-token-per-round decoding (still verified against the model),
which is why the leg records `drafts accepted` - the leg never assumes what the fallback did.

The inverted trap is **not** used anywhere: `--conversation-cache-park-gain 0` makes
`park_worth` true on every turn (`generate.cpp:19494`), i.e. maximum parking, the opposite of
parking off. Every leg keeps park-gain at the base's 4096.

## Inert leftovers in `parking-off` (and why they stay)

`--conversation-cache-evict-page-cache`, `--conversation-cache-disk-mib 24576`,
`--conversation-cache-disk-ram-mib 64`, `--conversation-cache-ram-mib 512`,
`--conversation-cache-min-tokens 2048` and `--conversation-cache-park-gain 4096` remain in the
config. Each is read only inside the park/disk path, and that path never opens: the disk tier
opens *only* from `--conversation-cache-dir` (`generate.cpp:13520`), and `park_current_body`
returns at once when neither the RAM tier nor the disk tier is on (`generate.cpp:15147`). Removing
them would enlarge the diff without changing a byte of behaviour.

## Running one leg

All commands from `/mnt/srcds/Strata`. The legs are frozen for the window directory
`bench/ab-v0210/rpcprof/out/faultdiff-r1/` (log, sockets, cache dir are inside the configs).

```sh
# 0. the configs still are what the generator produces, and the rig's shas are the pinned ones
.venv/bin/python bench/ab-v0210/rpcprof/faultdiff_configs.py --check
.venv/bin/python bench/ab-v0210/rpcprof/faultdiff_configs.py --diff --leg parking-off

# 1. STOP PROD (its own invocation, the operator's call), then check the boot plan
.venv/bin/python bench/ab-v0210/rpcprof/faultdiff_run.py --leg parking-off --boot --dry-run

# 2. boot: prerequisite 0, no live prod pair, no other /dev/kfd user, free port 8019,
#    no stale socket, empty cache dir, then helper -> sockets -> server -> 'ready: http://'
.venv/bin/python bench/ab-v0210/rpcprof/faultdiff_run.py --leg parking-off --boot
#    the non-engine RSS sum is the one waivable gate (the operator's desktop baseline):
#    add --waive-nonengine (the reading and the waiver are written into the boot record)

# 3. load: sequential requests, one in flight - the prod shape, NOT a concurrency test
#    (operator directive 2026-11-21). Two conversations alternating by default, so every
#    switch parks one and restores the other; parking-off defaults to ONE conversation
#    (--ctx-pair 80000,80000) because a switch without the conversation cache re-prefills
#    the whole prompt and would turn the leg prefill-bound instead of decode-bound.
.venv/bin/python bench/ab-v0210/rpcprof/faultdiff_run.py --leg parking-off --load --minutes 45

# 4. watch: liveness, engine fault lines, the kernel slice since the boot, parks/pages-released
.venv/bin/python bench/ab-v0210/rpcprof/faultdiff_run.py --leg parking-off --status

# 5. stop (separate invocation; TERMs recorded pids after checking /proc identity, then QUIET)
.venv/bin/python bench/ab-v0210/rpcprof/faultdiff_run.py --leg parking-off --stop
```

Boot takes minutes (the 256k start); the load runs until the fault or until the operator's chosen
exposure. Each load call is the same prompt twice (`--kind tg`: the second is the cached rerun) and the
calls alternate between the two sizes, so every transition parks one conversation and restores the
other; the first call of each size pays its own prefill (an 80k prompt is minutes at the measured
prefill rate), later calls are served from the cache - and from the parked snapshot after a switch.

`parking-off` is the exception, and it has to be: with the conversation cache off, a switch to the
other conversation throws the first one's state away, so the next turn re-reads its whole prompt
(prefill-bound, and the park path it is testing is never reached either way). Its load therefore
stays on ONE conversation (`--ctx-pair 80000,80000`, the runner's default for that leg): every turn
resumes the live session, decode stays the bulk of the exposure, and no park can happen - which is
exactly the claim the leg is testing. The other legs alternate, because parking needs a switch. A fault ends the engine; the server restarts it on the next request (the product's own
behaviour), so stop the leg at the first fault and keep the log and the kernel slice.

### The gates (all of them refuse; only two are waivable, both recorded)

| gate | source | waivable |
|---|---|---|
| MemAvailable >= 15 GiB, non-engine RSS sum <= 8 GiB, no process >= 2 GiB, no hot thread on the stage cores, >= 40 GiB free disk | `port_ab_prereq0.py` (Phase 2's own gate) | non-engine RSS sum only, per invocation (`--waive-nonengine`), as the operator's desktop baseline |
| no live prod pair (`run/prod*-supervisor.json`) | `faultdiff_run.py --boot` | no |
| no other `/dev/kfd` user (`fuser /dev/kfd`, `rocm-smi --showpids`) | the repro README's exclusivity step | `--waive-exclusivity` only, and it is written into the boot record |
| port 8019 free, no stale socket, cache dir empty | `faultdiff_run.py --boot` | no |

## Expected engine boot lines (per leg: present / absent)

Written by the engine to its stderr, which the server appends to the leg's engine log
(`boot record: engine_log`); the server's own narration goes to `<leg>-boot.log`. `--status`
re-evaluates each pattern against the logs and prints OK / MISSING / PRESENT, so a leg states its
own boot shape instead of assuming it.

| line (regex) | source | base legs | parking-off | park-on-no-evict | dflash-off | single-card-* |
|---|---|---|---|---|---|---|
| `engine started: .*<argv>` | `serve/server.py:894` | yes | `--conversation-cache-mib 0`, no `--conversation-cache-dir` | yes | no `--dflash`/`--dflash-block`, `--spec 5` | `--gpu 0` / `--gpu 1` |
| `strata serve: conversation cache: RAM ceiling 512 MiB` | `generate.cpp:13374` | yes | yes | yes | yes | yes |
| `strata serve: conversation cache: file page-cache release ON` | `generate.cpp:13380` | yes | **absent** | **absent** | yes | yes |
| `strata serve: conversation cache: disk tier ON at <dir> (direct I/O, budget 24576 MiB)` | `generate.cpp:13933` | yes | **absent** | yes | yes | yes |
| `dflash: <artifact>: ... trained block 7, pass K=7, mask ...` | `generate.cpp:8035` | yes | yes | yes | **absent** | yes |
| `strata generate: expert cache N slots, X GiB of VRAM` | `generate.cpp:9271` | yes | yes | yes | yes | yes (one card's numbers) |
| `strata generate: layer split: CUDA1 runs layers 30-47, expert cache ...` | `generate.cpp:9560` | yes | yes | yes | yes | **absent** (no split) |
| `strata generate: expert-helper proxy: N experts on <sock>, X GiB (fill report)` | `generate.cpp:9869` | yes | yes | yes | yes | yes |
| `strata serve: foresight swap: ... on N card(s)` | `core/expert_source.cpp:8019` | 2 cards, `card0:2,card1:8` | same | same | same | 1 card: `STRATA_FS_SLOTS` 2,8 truncates to card0:2 (env kept identical on purpose) |
| at request time: `strata serve: conversation cache: parked ...` / `disk park streamed ... pages-released=N` | `generate.cpp:15131`, `:14254` | yes / N>0 | **never** | yes / **N=0** | yes / N>0 | yes / N>0 |

The engine's `INFO ...` line goes to its **stdout** (the server's ready pump consumes it into the
Monitor's info), so it is not in the logs; the argv echo plus these stderr lines are the boot-line
evidence, and `--status` prints whichever of them exist.

## What to record (every leg, fault or not)

1. Outcome: no fault / fault continues (card 1, vmid 8, XDMA, page permission) / fault shifted
   (another card, another client, another class) - with the wall time since ready, the request in
   flight, and the engine's exit code or signal.
2. The passive fingerprint (`--status`, and written into the boot record by `--stop`):
   `journalctl -k` slice for the boot, filtered by the harness's fault class
   (`port_ab_legs.FAULT_CLASS`) and the engine's own `Memory access fault by GPU node-` lines.
3. Exposure: requests served, generated tokens, park/restore count, `pages-released` - so a clean
   leg can be read against the base's time-to-fault instead of being called "no fault".
4. The boot lines above (present/absent), the helper's fill report, and the memory shape
   (MemAvailable, swap, per-card VRAM from `--status` or the engine log's VRAM lines).

## Limits of this differential

- **n = 1 per leg.** The output is binary (stops / continues) plus a fingerprint, not a rate. A
  clean leg is only evidence against its hypothesis if its exposure is at least the base's
  time-to-fault; state the exposure, do not infer a mean time.
- **Parking-off and dflash-off change the workload's shape** (no conversation switching, no
  speculative decode). Both are recorded, and both make "clean" weaker evidence than a fault.
- **Single-card legs change the whole memory plan** (every layer, the expert cache, the KV
  residency on one card). A difference there is the card *or* the plan; the boot lines say which.
- **No leg exonerates hardware.** Nothing here touches the xnack build, the kernel, the PCIe link
  or the card's own firmware. Slot swap and card swap stay open until the software and platform
  space is exhausted, and the stock-build test is separate again.
- **A fault during boot or before steady load** invalidates the leg (it is not the serving fault);
  the record still keeps it, and the leg is repeated.
