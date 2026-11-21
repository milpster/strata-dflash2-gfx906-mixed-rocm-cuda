# The pool-admission death: a hit-path pin leak — 2026-11-21

This window found why the managed pool refused an admission in the middle of a long
decode, why the first fix attempt would not have helped, and what actually fixed it. The
series is one boot per leg; nothing here is a speed claim, and the sizes that matter are
token counts, not rates.

## What died, and how the ladder split the deaths into two classes

Four deaths on the pool champion in ninety minutes, all on agent-sized prompts:

| Time | Margin | Request | Error | Class |
|---|---|---|---|---|
| 03:42 | 256 | 13,946-token prefill | `refilling a lent slot failed: No buffer space available` | VRAM |
| 03:54 | 320 | 43,867-token prefill | same | VRAM |
| 04:09 | 384 | 53,717-token prompt, 1,694 generated | `the expert source could not produce a blob` | pool admission |
| 04:16 | 384 | 64,507-token prompt, 691 generated | same | pool admission |
| 04:41 | 384 | 13,695-token prefill | `refilling a lent slot` | VRAM |
| 04:57 | 384 | 21,233-token prefill | same | VRAM |

The VRAM class answered the +64 rule: 384 → 448 (`5232eba3ff`) stopped it, and at 448 a
44,556-token prompt with 1,224 generated tokens then ran to completion at 23.7 tok/s decode
with 95.9% expert-cache hits. The pool class did not answer any margin: it is host RAM.

## Why a retry was the wrong first answer

`PoolExpertSourceAdapter` turned a refused admission into `nullptr` and the request died.
An opt-in bounded retry looked obvious (`7c2e76e71b`), then a second round added a
self-sweep and a process-wide sleep budget (`f65396a46a`). Review killed it as the cure:

- the refusals are **self-pinned**: the pairs holding the pool belong to the blocked
  thread's own earlier borrowers, and the only code that marks a borrower complete runs
  *inside* `note_async_read`, which a blocked thread never reaches, so the sweep found
  nothing;
- arming it widened a corruption window, because a borrow did not clear `completed` and a
  foreign sweep could recycle bytes under an in-flight copy;
- `copy_blob` sits in a sweep over every pair, so the retry had no total cost bound.

It is landed default-off, with the re-arm safety and an evidence line, and it is **not
armed** on prod. It is not the fix.

## The root cause

`expert_pool_dispatch` (`src/core/expert_source.cpp`) asked for a blob for **every routed
expert before the hit test**. A hit and a remote-owned expert both `continue` without ever
noting completion, and the dispatch loop never releases. In the pool adapter a borrow is a
reference-counted lease, so every hit pinned one pair for the rest of the request. The
pinned set grew across the decode until the cache had nothing evictable left, admission
returned `no_buffer_space`, and the next miss surfaced it as the death line.

That explains the shape of the evidence: deaths after hundreds to ~1,700 generated tokens,
never at prefill (prefill copies through `copy_blob` and borrows nothing), and always on the
longest prompts.

`dca0e3cc93` moves the `blob()` call below the hit and remote-owned continues, so only an
expert that becomes a job borrows. Two things the naive version would have broken:
`ArenaExpertSource::blob` also records decode heat, so a `note_routed()` hook preserves it
on both skipped paths; and the adapter now re-arms `completed`/`pending_event` on every
borrow so a foreign sweep cannot free an in-flight lease.

## Measurement so far (new build `96a04be9`, prod, margin 448)

| Leg | Prompt | Generated | Result |
|---|---|---|---|
| probe | short | 22 | `up` |
| longctx-stress | 44,556 | 1,224 | completed, 23.7 tok/s decode, 95.9% hit, **no refusal line** |
| longctx-stress | 95,000 target | 1,800 max | running at the time of writing |

The A/B was run, and it came back **negative** — the honest result:

| Build | Prompt | Generated | Result |
|---|---|---|---|
| `96a04be9` (hit-pin fix) | 65,341 | 2,200 (`length`) | completed, 25.5 tok/s, 96.4% hit, 0 refusals |
| `d534e72a` (winner, old) | 65,341 | 2,200 (`length`) | **also completed**, 20.9 tok/s |

So a single cold request of the death shape does not reproduce the death on either build, and
the hit-path pin leak is therefore **not established as the cause of the 04:09/04:16 deaths**.
What is established: the old path borrowed a pool lease for every hit, which pins pairs the
cache could otherwise evict for the whole request; the new path does not. That is a real
defect and a real improvement, but it is not proven to be the thing that killed prod — the
deaths needed a shape a single request does not have: a live lane with a warm conversation
cache (the engine restored 42,299 tokens from disk in 13.9 s and parked every turn), several
requests in sequence, and a conversation cache that grows between them.

The fix stays (it removes a defect and cannot be worse than the old path); the claim that it
ends the prod deaths does not, until the multi-turn shape is reproduced on the old build.

## Two build traps that cost a cycle each

- A fresh build directory needs `-DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF`; without
  them the engine refuses to start and the pair supervisor loops (`dflash2: rebuild with ...`).
- It also needs `-isystem /opt/rocm-6.1.0/include` in `CMAKE_HIP_FLAGS`, or the kernels pick up
  `/usr/include/hip` and fail on `__builtin_amdgcn_wavefrontsize`, and a gcc pin because ROCm
  6.1.0's clang otherwise selects gcc-16 and cannot parse its libstdc++.

Both are now in the tuning skill, along with the `ldd` soname check that catches a mislink.
