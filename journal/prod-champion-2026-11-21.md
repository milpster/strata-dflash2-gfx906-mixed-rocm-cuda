# Prod on the managed pool champion — 2026-11-20/21

Window: promotion of the 38 GiB managed pool config to prod, then three prod deaths in
ninety minutes, each on an agent-sized prompt. Autopsied from the boot logs and the engine
elog; no measurement was taken in this window, so nothing here is a speed claim.

## What was promoted

`prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json` + its launcher, on the 38 GiB champion
identity: budget 43,008 MiB (`outside_owned=4 GiB`), `pool_bytes=40,802,189,312`, helper
reserve 0→64, fill 7,400, slots 4,600, relief hot 2,048 / arena 4,096, kv-resident 4,096,
max-context 262,144, UD pack with helper shard 00002. Commits `95b2f69d49` (config +
launcher), `ec9b722885` (exe → the pool build; the old v0233 path silently ran a pool-less
engine), `9a6a968103` (CUDA0 margin 256→320).

Winner binaries frozen in `run/winner-pin-38g/` (engine `d534e72a…`, helper `553f3bf4…`)
because `run/trial-managed-bin` resolves to the build tree a rebuild overwrites.

## Three deaths, three classes

| Boot | CUDA0 margin | Request that died | Error | Class |
|---|---|---|---|---|
| 03:42 | 256 | 13,946-token prefill | `refilling a lent slot failed: No buffer space available` | VRAM exhaustion |
| 03:54 | 320 | 1,353 tokens fine, then 43,867-token prefill at 22 s | same | VRAM exhaustion |
| 04:09 | 384 | 727 tokens fine, then 53,717-token prefill, died after 1,694 generated | `the expert source could not produce a blob` | **host pool admission** |

The margin ladder was applied per the operator's +64-per-real-exhaustion rule and did move
the failure: 13.9k died at 256, 43.9k died at 320, and at 384 a 46,235-token request
(`42,299 reused + 3,936 read`, 56.9 tok/s prefill, 21.6 tok/s decode) completed — the first
40k+ request to finish on this config. The 04:09 death is a different class and gets no
margin step.

## Why a config that passed a 230k prefill dies on 14k–54k prompts

The 230k window never tested prod's workload shape. It proved a prefill rate and a failure
mode, not stability.

| | 230k window (one boot, one request) | prod (live lane) |
|---|---|---|
| Conversation cache | empty, `0 reused` | restores 42,299 tokens from disk in 13.9 s at request start |
| Parks | one, after 228,966 tokens | every turn, e.g. `parked to disk only (46340 tokens, snapshot 1460 MiB ...)` |
| Free VRAM at request start | 284 MiB | 197 MiB (`WARNING: only 197 MiB ... below 512 MiB`) |
| Requests per boot | 1 | many, back to back |
| Outcome | prefill ok, died at 3,437 generated (host blob) | dies on the second or third request |

A 42k restore, a park, a rewind and the next prefill are each allocation bursts of tens to
thousands of MiB on a card holding 200–400 MiB free, because the expert cache absorbs every
MiB the margin hands back. That is why +64 keeps changing which request dies.

## Where the failure moved

At 384 the VRAM class stopped biting on 40k+ prefills and the pool class took over. The
`could not produce a blob` refusal is the admission bug the swift fixer lane is repairing
(opt-in bounded retry, transient-vs-permanent discrimination, default off). No margin step
substitutes for it: the pool is host RAM, not VRAM.

## Standing arrangements made this window

- `bench/ab-v0210/rpcprof/watch-prod-crashes.sh` — polls the pair supervisor's generation
  counter every 20 s and appends one attributed JSON line per restart to
  `run/prod-crash-ledger.jsonl` (dead boot log, error line, failing request, last VRAM and
  cache lines). It changes nothing; margin steps are applied by hand with their attribution.
- Margin steps are recorded with the class they answer. A pool-class death gets no VRAM step.
- Stop rule: past roughly 704 MiB the CUDA0 cache loses about 0.45 GiB; the ladder stops
  there and the attribution plus options go to the operator instead of trading speed away.
- Prod stays frozen while a subagent lane is mid-turn; only the parent stops or starts it,
  and only between turns. Prod-down work (swap/boot/probe) goes to the deepseek lane, never
  to the swift lane that rides prod.
