# The pool-admission death: a hit-path pin leak — 2026-11-21

This window found why the managed pool refused an admission in the middle of a long
decode, why the first fix attempt would not have helped, and what actually fixed it. The
series is one boot per leg; nothing here is a speed claim, and the sizes that matter are
token counts, not rates.

> **Read the correction at the end first.** Everything below that calls `refilling a lent slot
> failed: No buffer space available` a *VRAM* death is wrong: that text is ENOBUFS from the RAM
> pool, and the refill loop takes the store directly. The class column in the first table and
> the sentence about "+64 rule" are kept as written on the night, with the correction named at
> the foot of the file. The margin steps they justify are not to be continued.

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

## The leg that passed: 6,000 tokens with six helpers, on `716c3abee5`

Same shape that had just killed 7 of 7 on the previous build, same margin (640), same seed,
retry unarmed:

| Request | Engine's own line |
|---|---|
| long, 53,175-token prompt, 6,000 asked | `done: 6000 tokens in 347 s (39.6 tok/s) (length), expert cache 96.6% hit (+12.0% of the routed experts over PCIe)` |
| six helpers, 900 asked each | 424-707 tokens in 14-29 s, 27.0-32.4 tok/s, 86.0-98.4% hit |
| failures | none - 0 of 7; genuine overlap 321.5 s (needed 150 s) |
| refusal-class lines this boot | 0 (`borrow refused` / `refilling a lent slot` / `managed pool at the refusal`) |

Memory beside the speed, from the same run: `resident_pairs=19143`, `evictions=62373`, and the
borrower table peaked at **7,959**.

## What the guard says is left

```
strata: managed pool: 6368 live borrowers, 6368 of them with no copy in flight and none completed
(the borrow that crossed the line: layer 46 expert 408) - a caller is borrowing without noting or
releasing; resident_pairs=19143 evictions=62373
```

- Before the prefill fix the same measurement was **19,053** borrowers with the cache at zero.
- Now it is **7,959** at the peak, the cache holding 19,143 pairs, and every borrower idle.
- So the prefill gate closed roughly 11,000 of them; about 8,000 remain - ~15 GiB of a 38 GiB
  pool pinned by callers that still neither note nor release.

It did not cost this leg. It is the next cause of death, and the reason the cache cannot reach
the pack's 24,576 pairs. The fixer lane is naming the caller with an opt-in borrow trace rather
than guessing: the guard reports the pair that crossed the line, not the code that borrowed it,
and a backtrace at that moment is the shortest honest route to the site.

### Repeat (n=2), same boot, seed 43

| Request | Result |
|---|---|
| long, 53,137-token prompt, 6,000 asked | ok, alive 446.3 s |
| six helpers, 900 each | 461-710 tokens, alive 475.0-583.3 s |
| failures | 0 of 7; genuine overlap **421.3 s** |
| refusal-class lines | 0 |
| borrower peak | **9,053** (was 7,959 on the first leg) |

So the shape passes twice on this build with the overlap doubled over what was required, and the
one number that moves the wrong way is the borrower table: 7,959 after one leg, 9,053 after the
second on the same boot. It does not fall back between requests - the leak is per request, which
is why a longer agent session still ends in the refusal. Naming the caller is the next thing.

## The staged binary that did not contain the fix (process note)

`9358921670` reported "build of the six touched TUs ... warnings only" and the tests green. The
engine binary did **not** change: `build-906-fixadmit/strata` still hashed `618689a5`, the
`716c3abee5` build. A translation unit compiling is not the engine linking, and the lane's own
validation line was honest about it ("built the six touched TUs") without me reading it that way.

Caught by hashing the binary before staging it, not by distrust of the report as a whole. From
here on a lane's build claim counts only with **two** hashes: the test binary that ran and the
`strata` binary that will be booted. I ran `ninja -C build-906-fixadmit strata` myself; it
relinked in 8 steps (`a506e620`), and that is what is staged.

## The leak closes: `9358921670` (`a506e620`), the first boot with a silent guard

A lane that compiles a TU is not an engine link, so I relinked `strata` myself (`a506e620`) and
staged that. The 6,000-token leg then ran on it, margin 640 unchanged, trace off:

| | Engine's own line |
|---|---|
| long request, 53,175-token prompt, 6,000 asked | `done: 6000 tokens in 200 s (41.8 tok/s) (length), expert cache 98.5% hit (+11.5% of the routed experts over PCIe)` |
| six helpers | 465-644 tokens in 16-33 s, 20.2-30.7 tok/s, 89.7-98.0% hit |
| failures | 0 of 7; genuine overlap 174.8 s |
| **guard lines this boot** | **0** |
| refusal lines this boot (borrow / refill / at-the-refusal) | **0** |

That is the measurement the previous three boots could not produce: the guard fires whenever more
than 4,000 borrowers sit with no copy in flight and none completed, and this boot has eight request
lines and not one guard line. The table that reached 7,959, then 9,053, then 11,465 on earlier
builds no longer crosses the line at all. The fix was the lend/return contract: a CPU-thread lease
in `expert_pool_dispatch`, `expert_pool_dispatch_multi`, `expert_helper_recompute_claimed`,
`PeerExperts::adapt`, the blocking refills and the affinity probe is now given back through
`BlobLoanBatch` when the call that read the bytes returns.

## Evidence hygiene: an offset taken inside a backgrounded chain is not an offset

One run's "this boot" numbers were meaningless because the command read
`START=$(stat -c %s ...) && ... setsid nohup ./launch ... &` - the `&` backgrounds the whole
`START=... && ...` list, so the parent's `START` was empty and `tail -c +1` printed six days of
log. The arithmetic looked plausible (a 19,053-borrower line, guards at 6.7k) and was from
earlier boots.

Corrected practice, used since: slice by marker, not by offset -
`grep -n "engine started:" | tail -1` gives the line where this boot begins, and every number is
taken after it. The refusal line that appeared in the bad slice was the old 07:30 boot's.

## The agent shape: eight parked turns, 46k to 71k, on the same boot

`multiturn-stress.py --turns 8 --seed-tokens 55000 --max-tokens 900 --idle 25`: one conversation,
each turn appends its answer and a new question, 25 s idle so the engine parks and restores it.

| Turn | Prompt the engine reported | Engine's own decode | Expert cache |
|---|---|---|---|
| 5 | `59983 tokens = 56398 reused + 3585 read in 52607 ms (68.1 tok/s), 453 generated in 16751 ms (27.0 tok/s)` | 27.0 tok/s | 97.5% |
| 6 | `63532 = 59978 reused + 3554 read in 46150 ms (77.0 tok/s), 304 in 11342 ms (26.8 tok/s)` | 26.8 | 98.4% |
| 7 | `67038 = 63527 reused + 3511 read in 32729 ms (107.3 tok/s), 271 in 9409 ms (28.8 tok/s)` | 28.8 | 98.5% |
| 8 | `70486 = 67033 reused + 3453 read in 32084 ms (107.6 tok/s), 170 in 5856 ms (29.0 tok/s)` | 29.0 | 99.1% |

All 8 turns answered. 19 park/restore lines, 0 borrower-guard lines, 0 refusal lines.

Two things the run shows beside the stability:

- **The script's own `decode=` column is not a speed** - it divides by elapsed time, which includes
  the 25 s idle and the restore. The engine's line is the number: 26.8-29.0 tok/s. The script
  should either subtract the idle or stop printing a rate; noted rather than claimed.
- Every park went to disk: `parked to disk only (63835 tokens, snapshot 1698 MiB over the 512 MiB
  RAM ceiling; the published generation serves the next restore)`, then
  `disk park streamed 67308 tokens idle ... in 2207.6 ms`. The snapshot is ~1.7 GiB per 70k-token
  conversation against a 512 MiB ceiling, so the RAM park cannot hold one. That is the
  conversation-cache policy, not this fix, and it belongs to the parked backlog (park misreport).

The stability claim this run supports is narrow and exact: the borrower table stays quiet and no
request is refused on a parked multi-turn conversation that grows past the sizes that killed prod
(53,717 and 64,507 tokens on 2026-11-21).

## The park path, read properly: the 32 s per turn is prefill, not the disk restore

Recon of the conversation cache (call it what it is: the engine's park/restore code, not a
mystery) answered most of it, and the per-turn log settles the part it inferred wrongly.

What the recon established in code, and I checked against the config:

- The "512 MiB RAM ceiling" is not hidden or clamped: the prod config passes
  `--conversation-cache-ram-mib 512` itself (a second knob next to
  `--conversation-cache-mib 4096`, which is the cache's byte budget). Nothing ties it to the
  38 GiB pool.
- `parked to disk only` is the intended branch for a snapshot above that ceiling
  (`generate.cpp:15328-15341`), taken before any allocation, and the disk tier streams first.
- `park-gain: advanced 0 of 4096 past the published N` is benign: it is a re-park of a
  conversation sitting exactly at its published depth. Every occurrence this boot says 0.
- No pool lease is taken anywhere in park or restore, and the conversation cache is plain heap
  outside both the pool and the budget arithmetic (`outside_owned = kv_cap_bytes`, headroom 0).
  The 512 ceiling is currently the only bound on that heap.

What the log corrects:

| Turn | Engine's own line | Reused | Read | prompt_ms |
|---|---|---|---|---|
| 2 | `49340 = 45924 reused + 3416 read in 49600 ms (68.9 tok/s)` | 45,924 | 3,416 | 49.6 s |
| 5 | `59983 = 56398 reused + 3585 read in 52607 ms (68.1 tok/s)` | 56,398 | 3,585 | 52.6 s |
| 8 | `70486 = 67033 reused + 3453 read in 32084 ms (107.6 tok/s)` | 67,033 | 3,453 | 32.1 s |

The per-turn mechanism is `the draft ring for the rewind to <M> restored from its pair`, with M
exactly the previous turn's prompt depth - **not** a disk read. The only `restored ... from disk
(checkpoint) in 11562.7 ms` in this boot belongs to the 6,000-token concurrency request, and the
checkpoints at 16,384 / 32,768 / 49,152 are `target-only (no legal pair boundary ...)`, so the
pair-based reuse only exists at the last boundary. So the recon's fit (~15 s restore inside the
32 s at 68k, 47%) does not apply to these turns: here the 32 s is the **prefill of ~3.5k fresh
tokens at 68-108 tok/s**, and a turn pays it because the new question is that large, not because
the park failed.

The real cost the log does show: the restore depth lags the published generation by exactly the
previous answer's tokens (published 46,095 vs reused 45,924 = 171; 70,655 vs 67,033 = 275 among
them), because the pair is captured at the prompt boundary and the answer sits after it. That is
one answer re-read per turn, small next to the question, and the design intent of the pair mount.

So: park/restore behaves as designed on the fixed build; the per-turn latency is prefill
throughput; the 512 MiB ceiling is a deliberate setting that forces every 70k park to disk, and
it is undocumented outside `--help` (a docs gap, queued).
