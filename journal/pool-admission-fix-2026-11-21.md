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

## What does reproduce it: concurrency

`bench/ab-v0210/rpcprof/concurrent-stress.py` — one long request (78k target, 1,500 tokens
wanted) with six short requests arriving 40 s later, both arms at margin 512, same boot
identity, same libs:

| Build | Long request | Six parallel | Engine |
|---|---|---|---|
| `d534e72a` (old) | `refilling a lent slot failed: No buffer space available` after 247.6 s | 1 × `the engine stopped unexpectedly (exit code 1)`, 5 × connection dropped | **exited, code 1** |
| `96a04be9` (fix) | ok, 1,500 tokens, prompt 53,047, 268.6 s | all six answered (16-19 tokens each) | alive |

So the ingredient the earlier shapes lacked is overlap: a second request in flight while the
first is decoding. Both earlier single-request results (one cold 65k/2,200 and eight turns
with parks) survived on the old build; only with concurrent traffic does the card go.

**The repeat killed the arm, not the claim.** The fixed build under the same load, different
seed:

| Build | Long request | Six parallel | Engine |
|---|---|---|---|
| `96a04be9` (fix), seed 29 | `refilling a lent slot failed` after 273.4 s | 1 × exit-code-1 error, 5 × dropped | **exited, code 1** |
| `d534e72a` (old), seed 23 | same | same | exited, code 1 |

So concurrency reproduces the death on **both** builds, and the hit-path fix does not change
it. The 96a04be9 pass above was luck, not margin. What is established: the failure is VRAM
headroom under overlap (`refilling a lent slot`), not the host pool; the engine exits with
code 1 instead of failing the request, which is why every concurrent request dies with it;
and margin 512 is not enough for two requests at once. The lever is the margin ladder
(512 → 576 → …), applied and re-tested, which is a different fix from the one this window
started with.

## Two build traps that cost a cycle each

- A fresh build directory needs `-DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF`; without
  them the engine refuses to start and the pair supervisor loops (`dflash2: rebuild with ...`).
- It also needs `-isystem /opt/rocm-6.1.0/include` in `CMAKE_HIP_FLAGS`, or the kernels pick up
  `/usr/include/hip` and fail on `__builtin_amdgcn_wavefrontsize`, and a gcc pin because ROCm
  6.1.0's clang otherwise selects gcc-16 and cannot parse its libstdc++.

Both are now in the tuning skill, along with the `ldd` soname check that catches a mislink.

## The line that names the defect

At margin 640, under one long request (4,000 tokens) and six concurrent helpers, the long
request **completed** and then a helper died:

```
strata: managed pool: borrow refused layer 30 expert 201: borrowers=19053 completed=0 pending=0 resident_pairs=0 evictions=86460
```

- `borrowers=19053` — the pool's whole capacity in live leases (~1.95 MiB each);
- `completed=0 pending=0` — **not one of them was ever handed to `note_async_read`**, so
  `pending_event` was never set and the sweep had nothing to free;
- `resident_pairs=0` after 86,460 evictions — the cache had already given up everything it
  was allowed to evict.

So the pool refusal is a lease-accounting defect with a named shape: the engine borrows
`blob_stable` and never tells the adapter the copy finished. The biggest such site is the
swap planner's availability probe (`generate.cpp:2626`), which borrows a lease for every
pair it tests, on every planning pass, only to ask whether the pair is available.

That also retires two earlier hypotheses honestly: the hit-path fix (`dca0e3cc93`) removed a
real defect but was not the cause; and the retry cannot help, because it only sweeps
borrowers that completed.

## Margin ladder under overlap (all on `96a04be9`, retry armed from 576 on)

| Margin | Leg | Long request | Outcome |
|---|---|---|---|
| 512 | 78k target, 1,500 wanted, 6 helpers | died `refilling a lent slot` | 7/7 failed, engine exited |
| 576 | same | died at 273 s | 7/7 failed |
| 576 | lighter (2,500 / 400 ×6) | ok in 102 s | passed, but with a 3x speed difference vs the failing leg, so not comparable |
| 640 | 4,000 / 900 ×6, 289.7 s overlap | **4,000 tokens, completed** | VRAM held; failure moved to the **pool** class |

So the VRAM ladder bought the overlap its headroom (640 holds a 4,000-token decode with six
concurrent helpers), and what remains is the lease accounting above. The next change is code,
not another 64 MiB.

## The 6,000-token leg at 640 (lease-accounting build, retry unarmed)

| Request | Result |
|---|---|
| long, 6,000 wanted at a 53k prompt | `refilling a lent slot failed: No buffer space available` at 279.9 s |
| six helpers, 900 each | one `engine stopped unexpectedly (exit code 1)`, five dropped |
| overlap | 254.9 s (a real one) |

So 640 holds a 4,000-token decode under six helpers and does not hold a 6,000-token one:
the VRAM class is a function of how long the overlap lasts, not a fixed step. The ladder
therefore continues on the failed card only after the next code build proves the pool side on
a shape whose VRAM the current margin already covers - otherwise a failure cannot be
attributed to either axis.

Review of the same build (oracle, static, `5435391e3d`): the `generate.cpp` sites are
balanced, but `prefill.cpp:6190`/`:7881`/`:5306` borrow from a source that reports both
`pinned()` and `transient()` true - one borrow per non-resident pair per layer with no note
and no release. That is the leak the guard should have caught had it counted idle entries
instead of the whole table.

## Correction: `refilling a lent slot failed: No buffer space available` is the host pool, not the card

Every margin step from 384 to 640 was taken on the belief that this line was VRAM exhaustion.
It is not, and the code says so plainly:

- `"No buffer space available"` is errno **ENOBUFS**. The only producers of
  `std::errc::no_buffer_space` in this tree are `src/core/ram_pool.cpp:306` (pool_alloc, no
  fitting range) and `pool_expert_cache.cpp` (its LRU gave up). CUDA reports a full card as
  `"out of memory"`, never as ENOBUFS.
- The refill loop takes the store **directly**: `generate.cpp` `refill_issue` does
  `g_pool_expert_store->acquire(layer, expert, lease)` and, on failure, `e = ec.message()`.
  The caller prints `ERR refilling a lent slot failed: %s`. So the failing prompt read at
  53,170 of 53,175 tokens (280 s) was the **RAM pool refusing a refill**, not the card.

That also explains why failing boots carried no `borrow refused` evidence line: refills go
through the store, not through the adapter, so the adapter's one-per-second refusal line never
fires for them.

What this costs the record: the ladder's premise was wrong, and the direction was wrong too -
a larger CUDA0 margin shrinks the GPU expert cache, which sends *more* pairs to the host pool
on every refill, i.e. it increases the pressure that was actually failing. The 640 rung stays
as the current state only because it is what is running; no further step is taken on this axis
until the pool is fixed and a genuine card failure (CUDA's own out-of-memory text) has been
seen.

The classifier is corrected (`host-pool-refill`) and the ledger re-derived with
`bench/ab-v0210/rpcprof/reclassify-ledger.py`: 9 entries reclassified, and the ledger now holds
no `vram-exhaustion` entry at all - 5 pool admissions, 7 pool refills, the rest empty or
SIGABRT.
