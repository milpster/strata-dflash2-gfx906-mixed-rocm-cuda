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

## The merge lands, armed and unproven at runtime

`helper-ipc-diet` is merged into `main` on branch `merge-helper-ipc-diet`, commit `159e3097ef`
(parents `bd4172ed00` + `9358921670`), and pushed to `fork`. Seven conflicts, not ten; the lane
worked in its own worktree `/mnt/srcds/Strata-wt-merge-ipc` because the main checkout sits on
`tg-stall-cause` with live supervisor state and a branch switch there would have clobbered it.

The resolutions worth knowing, because they are the places a merge silently loses a feature:

| File | Resolution |
|---|---|
| `src/core/expert_source.cpp` | `main`'s refactor to a shared `gates_pass_locked(..., count_miss)` kept, with the branch's `transient()` rejection re-added inside it - both survive |
| `include/strata/core/pin_budget.hpp` | `main`'s `STRATA_ARENA_MLOCK` and the branch's `STRATA_NATIVE_MLOCK` both kept, spliced at the same insertion point |
| `CMakeLists.txt` | `main`'s `tail_skip_guard_test` / `read_once_test` registrations kept; the branch's six pool test registrations verified present |
| `bench/.../ab9_pcie_ctx.sh` | `main`'s per-arm `$hbin` and the branch's UD pack paths, independent axes |
| the swift15 prod config + launcher | the branch's UD repoint, plus `main`'s validated promotion values, not the branch's stale base |
| champion `prod-config-ud-*` / `launch-prod-ud-*` | did not conflict; the current values were ported onto the merge output and the result is semantically identical to what serves (checked by parsing both files, not by eye) |

Verified by me, not taken from the lane: `pool_expert_source_adapter_test` **237 checks, 0
failures**, `ctest -R "ram_pool|pool_expert"` **6/6**, and `build-906-merge/strata` hashes
`5a699be1`. The lane needed `-DSTRATA_BUILD_RAM_POOL_TEST=ON` for those targets to exist at all -
without it the mandated tests are not built, which is worth knowing for the next fresh directory.

**What is armed and what is not.** `run/merge-bin/` holds the merged engine (`5a699be1`) with the
helper unchanged (`553f3bf4`), and `run/pool-admit-a506/` holds a byte-identical copy of the engine
that serves right now (`a506e620`), each with `SHA256SUMS` and a `README.md` naming the swap and the
revert. Prod has **not** been switched: the merge brings `main`'s tail-skip, `STRATA_NATIVE_MLOCK`
and helper-IPC work together with the pool work for the first time, and a build that passes the
pool tests is not the same claim as an engine that has carried an agent leg. Promotion is the
operator's call; the directory is ready for one command.

## The real agent lane: 138 requests, a 100k-token prompt, no refusal

The merge lane above ran on **swift**, i.e. on prod's engine, for its whole session: a real
code-reading agent whose context grows turn by turn. That accidentally delivered the strongest
evidence of the window, because it is the shape the operator actually runs - not a stress script.

One boot (`a506e620`), everything below from the engine's own lines:

| | |
|---|---|
| Requests served on this boot | **138** |
| Largest prompt | `prompt 100291 tokens = 100032 reused + 259 read in 12624 ms ... 1860 generated in 77069 ms (24.1 tok/s)` |
| Next largest | 99,386 / 99,193 / 98,197 / 97,859 tokens, 20.8-24.8 tok/s |
| Expert-cache hit rate | lowest 52.2%, highest 99.1% |
| Helper health, every request | `timeouts=0 fallbacks=0` - all 138 of them |
| Borrower-guard lines | **0** |
| Refusal-class lines (borrow / refill / at-the-refusal) | **0** |
| Engine restarts | **0** (one `engine started:` in the slice) |
| Crash-ledger entries since | none |

The merge lane's build also pinned every CPU core for the better part of an hour *while* prod
served those requests - the host-load shape that killed earlier boots. The guard stayed silent
through it, and the pool never refused.

Context to read it against: the deaths on 2026-11-21 were at 53,717 and 64,507 tokens, and the
largest prompt here is 100,291. That is not a proof for every future session, but it is the first
time this configuration has carried a real agent past the sizes that used to end it.

## The helper card's last spare MiB (2026-11-21, after the 240k infill)

The operator judged the 239,677-token prefill (`259.5 tok/s`) abysmal and ordered the pool down
4 GiB (`dfd0600117`: budget 40,960 → 36,864, pool 36 → 32 GiB), then asked why the 3080 still had
spare memory. The answer was in the helper, not the config.

**The double count.** `serve_fill` read `cudaMemGetInfo` (already net of every startup
`cudaMalloc`) and then subtracted `scratch_bytes + buffers_bytes` again — 172.4 MiB, part of it
pinned host memory that was never VRAM. The tell was arithmetic: after the fill the card was left
with exactly `reserve 64 + buffers 172 + scratch 0.7 + headroom 16`. Fixed in `3d1e031f37`
(`fill_budget_bytes(free, reserve_mib, late_bytes)`), ordering verified first: no tier, graph, or
relief buffer allocates device memory after the probe.

**Then the reserve floor.** The launcher already asks `--reserve-mib 0`; the helper silently raised
that to a 64 MiB floor introduced in `c7be950984` while fixing a dead-argument bug — a round number,
never a survived failure. Floor set to 0 (`7d3230c316`) and the fill cap removed (`--fill-mib 0`,
`c98273b1bf`).

| Boot | Budget | Fill stop | Slots | 3080 free after |
|---|---|---|---|---|
| `553f3bf4`, reserve 64, cap 7400 | 7,264.2 MiB | `budget` | 3,785 | 250 MiB |
| `0c87a16d`, reserve 64, cap 7400 | 7,436.4 MiB | `fill-mib` | 3,861 | 114 MiB |
| `9d16742d`, reserve 0, cap 0 | **7,500.4 MiB** | `budget` | **3,913** | **14 MiB** |

**The "late module load" claim, checked rather than repeated.** The lane's prose said the 16 MiB
headroom protects a late CUDA module load, without a size. Measured: the helper's device code is
14 sm_86 cubins totalling **1.08 MiB** (largest 0.21 MiB), and the prod fill's post-arena work —
warmup launch included — cost **2 MiB** of the 116 MiB its arithmetic left. Zero module-load
failures exist in any log in this tree. The prose was corrected to the measurement
(`a2303f4021`); the headroom stays at 16 MiB, which is ~15x the process's entire kernel code.

Staged in `run/pool-admit-bin` (helper `9d16742d`, `e3b14c0515`) with a byte-identical rollback copy
of the previous helper in `run/helper-553f3bf4/`. Engine `5a699be1`, margins 256/192 and the 32 GiB
pool unchanged throughout.

## The 240k leg on the final helper: two prefills, no output (2026-11-21 14:44-15:21)

Boot: engine `5a699be1`, helper `9d16742d` (reserve 0, no fill cap), `fill preflight: budget 7500.4 MiB
(reserve 0 MiB ...)`, `fill stopped: budget; 3913 slots, 7.32 GiB`, 14 MiB free; pool 32 GiB; margins
256/192. Leg: `longctx-stress.py --prompt-tokens 323200 --max-tokens 10000 --seed 7`.

| Pass | Result | Notes |
|---|---|---|
| 1 | `prompt 239677 tokens = 0 reused + 239672 of 239677 read in 800129 ms (299.5 tok/s), 0 generated` | best prefill this box has recorded |
| then | `WARNING: only 86 MiB of VRAM free at the start of this request (below 512 MiB) ... Add --vram-reserve-mib 704` | the engine's own #2065 warning fired before the failure |
| then | `after non-finite logits (#879): dropped the live session, 6 checkpoints ... reading this prompt from token 0`; `(#3465) this request reads its whole prompt through the batched path` | the first decode step produced NaN/Inf |
| 2 | `prompt 239677 tokens = 0 reused + 239677 read in 1408769 ms (170.1 tok/s), 0 generated in 134 ms, 6 checkpoints helper=reqs=33 rows=69 bytes=706560 timeouts=0` | clean read, same outcome |
| end | `HTTP 503 ... the model's output became non-finite (NaN) twice in a row (#879); the engine has dropped its cached state` | leg exit 1, no tokens produced |

Stability held otherwise: 0 refusals, 0 borrower-guard lines, no engine restart (the engine stayed up and
dropped the session), 6 `engine started:` in the whole file but 1 in this boot's slice.

**Where pass 2's 170.1 tok/s came from (measured, not inferred):** the counter froze on 16,384-token
boundaries for 50-90 s and jumped 16,384 tokens in 4-9 s (2,000-3,000 tok/s instantaneous). During the
freezes the engine read **329 MiB/s** from block devices (`/proc/<pid>/io` read_bytes delta), helper reads 0,
swap in/out 0, GPUs 49/0/14% busy and the 3080 3%. So the replay is IO-bound on the expert pack; the warm pass
(299.5) is the comparable prefill figure, the replay (170.1) is not.

**Open question, honestly framed:** both passes failed at the same context, but both also ran under the same
thin-margin state (86 MiB free, helper 14 MiB free). The two causes are not separated yet. The discriminating
test is a boot with `--vram-reserve-mib 704` (the engine's own suggestion, at the cost of ~166 CUDA0 cache
slots) running the same leg; if it still ends non-finite, the batched long-context path itself is the defect.

## Separating the 240k non-finite logits: VRAM is innocent, the prompt path is innocent (2026-11-21 15:26-16:10)

Test A/B: CUDA0 margin `--vram-reserve-mib` 256 -> **704** (the number the engine's own #2065 warning
prints), everything else identical. Engine `5a699be1`, helper `9d16742d`, pool 32 GiB.

`strata generate: expert cache auto: 9.67 GiB free, 704 MiB reserved (+0 MiB for the draft head,
+256 MiB for what is allocated after the cache) -> 3523 slots` — the margin costs 176 CUDA0 slots
(3,699 -> 3,523, ~0.34 GiB of expert cache).

| Pass | Free VRAM at request start | Result |
|---|---|---|
| margin 256, pass 1 | 284 MiB | read 239,672 in 800,129 ms (299.5 tok/s), 0 generated, then non-finite at position 239,672 |
| margin 256, pass 2 (batched path) | 86 MiB | read all in 1,408,769 ms (170.1 tok/s), 0 generated in 134 ms, non-finite again |
| margin 704, pass 1 | **730 MiB**, no #2065 warning | read 239,658 of 239,663 in 1,716,709 ms (139.6 tok/s), 0 generated, then `non-finite logits (#879) in a prompt window at position 239658 (row 0)` |
| margin 704, pass 2 (#3465: whole prompt through the batched path) | — | read all 239,663 in 917,580 ms (261.2 tok/s), 0 generated in 110 ms, non-finite again -> `non-finite (NaN) twice in a row` |

Conclusion: the margin does not change the outcome (284 -> 730 MiB free, same failure), and the path does
not either (the second pass reads the prompt from token 0 through the batched path, `0 reused`, and fails
the same way). The failure is deterministic at ~239,660 tokens, at the first decode step, `row 0` of the
window that ends there. The 6 non-finite lines in the 704 slice and the 4 in the 256 slice are all on the
same two requests, not on independent ones.

Both margin 704 passes were slower than the 256 warm pass (139.6 and 261.2 tok/s vs 299.5) because the
page cache was cold after the reboot: the engine read the pack itself at up to 329 MiB/s during the
window gaps (measured via /proc/<pid>/io). Speed is not the variable under test here.

Next instrument: `STRATA_DBG_NAN=1` in the engine's environment (config commit `b0f21dd600`), which makes
`src/prefill/prefill.cpp` report, per chunk, `layer L (mmq N, types G/D, K experts): non-finite GU ... H ...
Dm ... bo ... of T` for the first chunk that produces them, and at prompt end the non-finite count and
max |x| of the last residual row, the PLE history and the GDN state.

## The 230k-working tuple does not have the problem (2026-11-21 16:23-16:44)

Booted the last tuple that produced tokens at 230k (journal `prod-champion-2026-11-21.md`): engine
**`d534e72a7369fe5d`** (`run/trial-managed-bin/rpcw-hip/strata`), helper **`553f3bf48219c992`**, budget
**40,960 MiB** (`pool_bytes=38,654,705,664`), CUDA0 **256** / CUDA1 192, helper reserve 64, fill 7,400, slot
cap 4,600, kv-resident-mib 4,096, prefill 16,384, max-context 262,144. Both hashes verified from `/proc/<pid>/exe`
before the leg. Config: `bench/ab-v0210/rpcprof/champion38g-prod-test.json` on `:8009`, champion files untouched.

Same leg as the failing runs (`ctx-threshold.sh 323200 8`, 239,663-token prompt):

| | champion tuple `d534e72a` + `553f3bf4` | merged tuple `5a699be1` + `9d16742d` |
|---|---|---|
| prompt read | 239,663, ~1,102 s (≈218 tok/s) | 239,658 / 239,663, 1,716 s and 918 s (139.6 and 261.2 tok/s) |
| first decode step | **finite** | non-finite logits (#879) 4 times in 4 passes |
| engine's own line | `done: 4 tokens in 1222 s (7.5 tok/s) (length, cancel=False), expert cache 95.7% hit (+7.4% of the routed experts over PCIe)` | `non-finite logits (#879) in a prompt window at position 239658 (row 0)` |
| non-finite lines in the boot | **0** | 6 and 4 per slice |
| answer text | empty, and for a benign reason: `the reply reached max tokens while still thinking, so it has no answer` (max_tokens 8 sat inside the thinking prelude) | none produced |

So the failure is a **regression** relative to `d534e72a`, not a property of the model, the prompt, the pack,
the KV settings, the margins or the pool size (40,960 vs 36,864 also differ, and the pool was larger here).

The helper is not the suspect: the first failing pass on the merged tuple reported
`helper=reqs=0 rows=0 bytes=0` — it produced the non-finite logits with the helper serving nothing. The
remaining suspects are the engine changes between `d534e72a` and `5a699be1`: the merge `159e3097ef` and the
prefill gates/pool-lease commits (`716c3abee5`, `5435391e3d`, `dca0e3cc93`, `9358921670`).

Trade-off this leaves: `d534e72a` is the engine that died three times in ninety minutes on agent-sized prompts
(pool admission), and `5a699be1` is the one that survived a 138-request agent session but cannot finish a
240k prompt. Neither tuple is good for both workloads yet.

Harness notes: `ctx-threshold.sh` now honours `STRATA_ELOG` (`dcc0f61973`) because this boot wrote its progress
lines to the console log, not the `engine-prod-ud-iq3xxs-v0233.log` slice it assumed. Editing a shell script
while a copy of it is running makes bash resume at a stale byte offset — the leg's tail then reports a bogus
syntax error (seen here); the leg's own result lines were already complete and are what the table above uses.

## The merge is the cause, and the failure reproduces at 3k (evening window, 21 Nov 20:20 → )

### What the bisect settled

| binary | built | tree | 239,663-token leg |
|---|---|---|---|
| `d534e72a` | 20 Nov 23:47 | worktree, before the pool suite | **passes** |
| `a506e620` | 21 Nov 08:18 | worktree, tip of the pool suite | **passes**: prompt read in 852 s (281 tok/s), `done: 4 tokens`, 0 non-finite |
| `5a699be1` | 21 Nov 10:14 | merge `159e3097ef` | **fails**, 4 of 4 |

The seven pool-admission commits are exonerated. The regression is in the merge.

Correction to an earlier claim of this window: `run/bisect-db6d/` was populated from
`/mnt/srcds/Strata-wt-merge-ipc/bin/strata`, which is the `#!/bin/sh` Strata Terminal launcher, not an engine —
it exits with `cannot find strata.py next to …`. There is no fourth engine on disk. The bisect points are
`d534e72a` and `a506e620` (pass) against `5a699be1` (fail).

### The failure is not about 240k

It reproduces **deterministically at a 3,058-token prompt** (3 of 3, 17 s of prefill), on a clean conversation
cache. Every failure lands in the same place: the last window — `position N-5` in the prompt window and
`position N-1` in the first decode window — at 3,058, 6,029, 60,153 and 239,663 tokens alike.

### The engine's own instrumentation (`STRATA_DBG_NAN=1` at 3k, merge engine)

```
strata dbg: layer 30 (mmq 0, types 18/20, 75 experts): non-finite GU 6790726 H -1 Dm 16529553 bo 7815680 of T 3053
```

`bo` is `T × 2560` floats, so **every token's layer-30 MoE output is NaN**. Layer 30 is the first layer of the
second GPU's stage (`layer split: CUDA0 runs 0-29, CUDA1 runs 30-47`). The branch engine at the same 3,058
tokens reports 0 non-finite. So the data dies at the stage-2 entry, not in the experts and not in the helper.

### Moving the split (decisive)

| `--layer-split` | stage 2 | 3k leg |
|---|---|---|
| 29 | layers 29-47, 19 layers, 4165 slots | NaN at **layer 29** |
| 30 (the auto choice) | layers 30-47, 18 layers, 4174 slots | NaN at **layer 30** |
| 31 | layers 31-47, 17 layers, 4180 slots | **clean** |
| 34 | layers 34-47, 14 layers, 4322 slots | **clean** |

The NaN always sits at the first stage-2 layer, and it disappears once CUDA1 holds 17 layers or fewer.

### Candidates tested and excluded

All still NaN at 3k with split 30: `STRATA_GRAPH_FAMILY_TRIM=0`, `STRATA_GR_FAST=0`, `STRATA_QFUSE=0`,
`STRATA_DFLASH_SAMPLED=0`. Excluded by reading: `STRATA_ROUTE_TAIL_SKIP` is suspended on this rig (a helper
tier is registered) and would zero rows rather than produce non-finites; `STRATA_REGISTER_LAST` is inert under
`--mmap-experts` (both boots print identical memory lines); `STRATA_GFX906_TOPK_FIT17/REG` are read by neither
tree (dead config keys); the `expert_source.cpp` resolution keeps every guard (`where[key]`, admission, and the
per-card budget — `++card_used` moved into `assign_locked`); `hc_bf16_view_pair` is inert on gfx906
(`STRATA_HC_Q8` is off outside gfx12/gfx1151); `batch_rows_error` refuses hard instead of handing on garbage.

### Two measurement traps found

1. **The conversation cache outlives the boot.** `--conversation-cache-dir …/run/diet-prod-38g` persists, so a
   leg can restore 3,053 tokens and take the 5-row incremental path, passing where a full read fails. The
   harness now clears that directory before each boot.
2. **`ctx-threshold.sh` classifies from `$STRATA_ELOG`.** Pointed at the console log it counts 0 non-finite lines
   and prints "finite output produced" for a request that returned HTTP 503 (#879). Point it at the engine log
   the config names.

### Harness added

`bench/ab-v0210/rpcprof/knob-leg.sh LABEL TARGET [MAXTOK] [KEY=VALUE …]`: one stop → boot → leg cycle; the
config is generated into `bench/ab-v0210/rpcprof/gen/<LABEL>.json`, the running engine's sha is verified against
the config before the leg fires, the conversation cache is cleared, and teardown goes through `stop-all.sh`.
`EXE=` points at another `run/` directory, `ARGS=` appends engine arguments.

### Mitigation and what is still open

`--layer-split 31` instead of the auto 30 makes the 3k repro clean; a 239,663-token leg at split 31 is the open
test. **Not yet explained:** why the first stage-2 layer's input is garbage only when CUDA1 holds 18 or more
layers. Unexcluded candidates are the per-card expert slot/layer tables (`card_slots`/`layer_card`), the stage-2
input buffer, and pool/helper row addressing for stage-2 layers. A build bisect of the merge is the remaining
instrument: no intermediate merge builds exist because the conflict resolution was never committed separately.


### The 240k leg at `--layer-split 31`: no NaN, but a hard stall at 180,224 tokens

Measured 21:57 → 22:13 on the merge engine (`5a699be1`) with a cleared conversation cache:

| signal | value |
|---|---|
| non-finite lines in the engine log | **0** (the layer-30 NaN is gone at split 31) |
| server's progress ticks | `reading the prompt: 180,224 of 239,663 tokens` repeated from 612 s to 902 s |
| watchdog | `the engine said nothing for 300 s during the request; the server ended the engine` |
| outcome | `done: 0 tokens in 912 s (no speed) (error, cancel=False)` |
| VRAM at request start | `CUDA1: 156 MiB of VRAM free` against `the layer split planned to keep 288 MiB free here`, i.e. **132 MiB went to something the plan did not count** (a merge-added warning) |

So the split move trades one failure for another: at 240k the engine stops advancing in the 11th 16,384-token
chunk (180,224 = 11 × 16,384) and never resumes. It is a stall, not slowness: chunk 1-11 ran at ~300 tok/s, then
zero progress for 300 s.

Contrast with the passing branch engine at 240k: its server log carries 13 `said nothing for 90 s` warnings, but
each was followed by new progress and the request finished (`prompt 239,663 tokens ... in 847,394 ms`). The 90 s
warning is non-fatal by design; only 300 s of *no progress* ends the engine.

Open: why the merge engine stalls in the tail chunks when CUDA1 has 156 MiB free, and why 132 MiB more than
planned is missing. The single largest merge-added VRAM consumer is the per-stage reserve/guard path in
`generate.cpp` plus `vram_guard` (default off) and the prompt-path `fused_gr` scratch.

#### Identified: the stall is VRAM starvation, and the engine says so (issue #2065)

Reading the boot lines in full (earlier I had cut them at 135 characters):

```
strata serve: CUDA1: 156 MiB of VRAM free with everything loaded - LOW: its window graphs are captured on
              first use and need room too, and a full card can stall a request; add --vram-reserve-later-mib 804
              to the config's args (or lower --max-context)
strata serve: CUDA1: the layer split planned to keep 288 MiB free here after loading; 156 MiB are free, so
              132 MiB went to something the plan did not count
strata serve: WARNING: only 278 MiB of VRAM free at the start of this request (below 512 MiB): when the card
              is this full the driver can page GPU memory to system RAM, decode falls to a few tok/s and a
              window may never finish (#2065). Add --vram-reserve-mib 512 to the config's args
```

`planned_vs_free_text` (new in the merge, `include/strata/program/gpu_report.hpp:106`) is a *report*, not a
cause: 132 MiB is how far the split's own estimate was off, not a leak. The stall at 180,224 tokens is the
documented full-card stall (#2065), and the engine recommends `--vram-reserve-later-mib 804` and
`--vram-reserve-mib 512` for it.

Why split 31 is starved and split 30 is not: with 17 layers on CUDA1 the cache is grown to 8.30 GiB (4180 slots)
and leaves 156 MiB, where split 30 leaves 360 MiB and the passing branch engine ran the same 239,663 tokens at
360 MiB. So the split move trades the layer-30 NaN for a starved card at long context.

Consequence for the plan: the split is not a fix. The NaN has to be fixed at split 30, or the split moved with
the reserves raised per the engine's own recommendation - and margins are the operator's call, not mine.

## Root cause found and confirmed by measurement: the per-card `STRATA_FS_SLOTS` parse starves card1

### The source difference (proved by reading both trees)

| tree | parse of `STRATA_FS_SLOTS="2,8"` | card1 Foresight slots |
|---|---|---|
| branch `9358921670` (`expert_source.cpp:7665`) | `const int slots = v ? std::atoi(v) : 0;` → **2** | ~55 MiB (the merge's own comment) |
| merge `159e3097ef` (`expert_source.cpp:8018-8032`) | comma list, in card order → **card0:2, card1:8** | ~220 MiB |

The merge's comment states the intent plainly: *"the split rigs' card1 sits on a thin VRAM margin, so it takes
fewer; card0 at 2 slots is ~133 MiB, card1 at 2 is ~55 MiB"*. Our config carries `STRATA_FS_SLOTS=2,8`, a value
that was harmless before the merge and now gives the thin-margin card **four times** card0's slots. The swap then
serves nothing in these legs (`0 served / 0 copies`), so it buys no speed and costs the margin.

### The measurement (merge engine `5a699be1`, 3,058-token oracle, split 30 unless stated)

| leg | card1 FS slots | `--vram-reserve-later-mib` | CUDA1 free, loaded | outcome |
|---|---|---|---|---|
| `dbgnan` (baseline) | 8 | 192 | **140 MiB** | **FAIL** — layer-30 MoE all NaN |
| `split29` | 8 | 192 | 124 MiB | FAIL |
| `split31` | 8 | 192 | 156 MiB | PASS |
| `split34` | 8 | 192 | 204 MiB | PASS |
| **`fsslots2`** | **2** | 192 | **360 MiB** | **PASS**, no dbg line at all |
| **`fs28-reserve`** | **8 (unchanged)** | **400** | **348 MiB** | **PASS** |
| `ctl-a506` (branch engine) | 2 (atoi) | 192 | 360 MiB | PASS |

`fsslots2` lands on exactly the branch's 360 MiB, and `fs28-reserve` keeps the merge's 8 slot1 slots yet passes by
freeing the same margin from the cache. So the variable is **free VRAM on card1**, not the slot count and not the
layer count — "18 or more layers on CUDA1" was only a proxy, because each extra layer also brings FS slots.

### The mechanism (from the delegate's instrumentation argument)

The non-finite counts are **not multiples of the GEMM's N**: `22268 = 69×320 + 188` bad outputs on layer 30 half-0
(`T=3053 N=320 K=10240`), and 11149 on half-1. A non-finite *input* element would make whole output rows NaN and
give counts that are exact multiples of N. Scattered counts mean the first stage-2 GEMM's own outputs go bad in
place: below roughly 150 MiB free, card1's first-use allocations (rocBLAS workspace/kernel selection, window-graph
capture) fail or page, which is exactly the regime the engine's own warning names (`#2065`, `kVramSafeMib`).
CUDA0, with 284 MiB free, never corrupts.

### Fixes, cheapest first

1. **Config, no rebuild:** set `STRATA_FS_SLOTS=2` — restores the pre-merge behaviour exactly (atoi semantics) and
   the 360 MiB margin. Measured: passes at 3k.
2. **Code, durable:** in `ForesightSwap::init` (`expert_source.cpp:7609-7622`) clamp `card_slots[c]` with
   `cudaMemGetInfo` so at least `vram_guard::kLowMib` stays free, and/or allocate the FS `cudaMalloc` before the
   expert-cache fill so the cache absorbs it instead of the safety margin.
3. Independent of both: the same starvation explains the 240k stall at split 31 (156 MiB free, `#2065` full-card
   stall). One cause, two symptoms.

Validation in flight: the 239,663-token leg at split 30 with `STRATA_FS_SLOTS=2`.

### Validation: 239,663 tokens pass at split 30 with `STRATA_FS_SLOTS=2`

Leg `fs2-240k`, 23:06 → 23:20, merge engine `5a699be1`, clean conversation cache:

| | value |
|---|---|
| prompt | **239,663 tokens = 0 reused + 239,663 read in 814,622 ms (294.2 tok/s)** |
| decode | 8 generated in 373 ms (21.4 tok/s), 5 of 8 drafts accepted |
| non-finite lines | **0** |
| watchdog / stall | none; the request finished in 820 s |
| answer text | empty for the benign reason: `max_tokens 8` ran out inside thinking |

Against the passing branch engine's own leg (239,663 tokens in 847-852 s, 281 tok/s, 4 tokens), the restored margin
gives the same throughput and the same completion. One config value, two symptoms gone: the 3k NaN and the 240k
no-output.

Armed file for the operator's decision (not applied to prod): `prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed-fs2.json`
(byte copy of the tested generated config). No margin was changed: `--vram-reserve-mib 256` and
`--vram-reserve-later-mib 192` stay exactly as championed; only `STRATA_FS_SLOTS` returns to the pre-merge
behaviour.

### The durable fix, built and measured: `564e1dbbea` clamps FS slots per card

`ForesightSwap::init` (`src/core/expert_source.cpp:7655-7725`, branch `merge-helper-ipc-diet`) now sizes one
slot (`per_slot` = the card's layers' blob bytes), queries `cudaMemGetInfo` under `OnDevice(devices[c])`, and
clamps `card_slots[c]` so the `cudaMalloc` leaves at least `vram_guard::kLowMib` free. Card 0 slots are skipped,
a card that can afford nothing gets no `cudaMalloc`, and if every card clamps to zero the swap returns off. One
log line per clamped card; nothing printed when nothing clamps.

Built `4b8251e9e06334a8…` (ldd: `libhipblas.so.2`/`libamdhip64.so.6` → `/opt/rocm-6.1.0/lib`,
`librocblas.so.4` → `/home/srcds/rocm-gfx906-xnack/lib`, the expected set), staged in `run/fsclamp-bin/`.

3,058-token leg at split 30 with the merge's own `STRATA_FS_SLOTS=2,8`, 23:40 → 23:43:

```
strata serve: foresight swap: card0 asked 2 slots (114 MiB) with 400 MiB free -> granted 0 slots (0 MiB), leaving 400 MiB
strata serve: foresight swap: card1 asked 8 slots (293 MiB) with 434 MiB free -> granted 0 slots (0 MiB), leaving 434 MiB
strata serve: foresight swap: every card's slots clamped to zero against the free-VRAM low line - swap space off
strata serve: CUDA1: 434 MiB of VRAM free with everything loaded
```

**Result: pass, 0 non-finite, 8 tokens, 5 of 8 drafts accepted.**

The honest consequence: on this rig the engine loads every card below the 512 MiB low line by design (reserve
192-256 MiB), so `kLowMib` as an allocation floor means the swap is never affordable here — `STRATA_FS_SLOTS=2`
clamps to zero just as `2,8` does. The clamp is therefore a safety valve that removes the failure, not a way to
keep the swap. Keeping the swap *and* the margin needs the planner to count the FS allocation (reduce the expert
cache by it) — a larger change, not made, and not needed for correctness.

Also worth recording: the config-side fix stays valid on the *old* engine (`run/pool-admit-bin`), and is
redundant on a clamped engine.

### 239,663 tokens on the clamped engine (the durable fix, merge's own config value)

Leg `clamp28-240k`, 23:44 → 23:59, engine `4b8251e9`, `STRATA_FS_SLOTS=2,8`, split 30, clean conversation cache:

| | value |
|---|---|
| prompt | **239,663 tokens = 0 reused + 239,663 read in 834,764 ms (287.1 tok/s)** |
| decode | 8 generated in 359 ms, 5 of 8 drafts accepted, 6 checkpoints |
| non-finite lines | **0** |
| stall | none; finished in 841 s |
| card1 margin | 434 MiB free, swap clamped off at boot |

So the code fix closes the bug without touching the config, and the config fix closes it without a rebuild. Both
land at the branch engine's own numbers (847-852 s, 281 tok/s). CHAMPION.md and CHAMPION.json carry the resolution.

## The RAM areas and the disk park (champion tuple B, for the 1k-decode window)

Read from the live config and the engine's own `--help`, not estimated:

| Area | Setting | Size |
|---|---|---|
| Expert pool, host RAM | `STRATA_RAM_BUDGET_MIB` | 36,864 MiB (36 GiB) |
| Conversation cache, total RAM budget | `--conversation-cache-mib` | 4,096 MiB |
| Conversation cache, hard host-RAM ceiling | `--conversation-cache-ram-mib` | **512 MiB** (a park over it goes to disk only, no host copy) |
| Disk park tier | `--conversation-cache-dir` + `--conversation-cache-disk-mib` | 24,576 MiB at `Strata-wt-helper-ipc/run/diet-prod-38g` |
| Disk tier host RAM (manifest, dirs, read-ahead) | `--conversation-cache-disk-ram-mib` | 64 MiB, flat, does not scale with the conversation |
| Park floor / gates | `--conversation-cache-min-tokens`, `--conversation-cache-park-gain` | 2,048 tokens / 4,096 tokens of growth |
| Prompt checkpoints | `--prompt-cache` (default 6) | ~118 MiB each |

So a 240k conversation is not kept in RAM: with the 512 MiB ceiling its snapshot streams to the 24 GiB disk
tier and a matching request is restored straight into the live pools. The host side costs 64 MiB of buffers
plus whatever the active conversation's K/V needs. Box: 62 GiB total, ~50 GiB available before the window;
`/mnt` has 96 GiB free against the 24 GiB tier (90% used, worth watching).

## The park-and-restore window on the clamped champion (fsclamp-champion, 00:15–00:33)

Boot: engine `4b8251e9` from `run/fsclamp-bin`, helper `9d16742d` from the same dir, champion config unchanged
(`STRATA_FS_SLOTS=2,8` → clamp granted 0 on both cards, swap off; CUDA0 400 / CUDA1 434 MiB free; pool 36,864 MiB).
Cache cleared before boot. One request at a time. 0 non-finite lines, no stall, 6 checkpoints per request.

| Request | Prompt | Decode | Answer |
|---|---|---|---|
| 1 — cold infill | 239,646 = 0 reused + 239,646 read in 842,742 ms (**284.4 tok/s**) | 513 generated in 25,384 ms (**20.2 tok/s**), drafts 314/540 (58%) | 678 chars, coherent |
| 2 — same boot, same prompt | 240,158 reused + 18 read in **393 ms**; then a rewind pass 239,641 reused + 5 read in 275 ms | 174 at 25.1 tok/s, then 406 at **30.7 tok/s** | 573 chars, coherent |

Client side: request 1 took 880.5 s end-to-end (704 completion tokens incl. reasoning); request 2 took **18.3 s
end-to-end** (406 tokens, 22.1 tok/s overall including the restore mount). The draft ring for the rewind to 239,641
restored from its pair.

**The park itself is real.** At idle the disk tier streamed 240,158 tokens to `conv-1.sess` in 4,884 ms — the file is
**3,854.8 MiB** (~810 MB/s, direct I/O); a second generation `conv-2.sess` (3,853.5 MiB) plus `ring-1.dring`
(40.2 MiB) make **7,748 MiB of the 24,576 MiB disk budget**; the log's `bytes=` counter is MiB. The 4,097 MiB
snapshot is over the 512 MiB RAM ceiling, so no host copy was built; park-gain correctly refused the re-publish
(advanced 191 of 4,096 tokens) and page-cache pages were handed back after each flush.

What this window does NOT prove: request 2 ran on the same boot, where the active conversation is never dropped.
A cross-boot restore (fresh engine, cache kept on disk) is the remaining leg — running next as
`fsclamp-champion-xboot` (`NOCLEAR=1 ONE=1`). Memory gap named: no per-process RSS sampler ran beside this window;
VRAM and pool budgets are from the engine's own boot lines.

One pre-existing, non-fatal warning seen once: `--ple-io direct measures slow here (12,863 rows/s, ~53 MB/s of
random 4 KiB reads)`.

### Cross-boot restore from the disk tier (fsclamp-champion-xboot, 00:36–00:38)

Fresh boot, engine `4b8251e9`, cache kept (conv-1/conv-2/ring-1 on disk), same 239,646-token prompt:

| | value |
|---|---|
| restore | **restored 239,641 tokens from disk (checkpoint) in 32,681 ms** — checkpoint mount at 239,641 of 240,051 (conv-2's generation) |
| prompt line | 239,646 = 239,641 reused + 5 read in 33,252 ms |
| decode | 512 generated in 21,168 ms (**24.2 tok/s**), drafts 318/496; second pass 145 at 30.2 tok/s after a 475 ms re-mount |
| client | 675 tokens, **64.7 s end-to-end** (10.4 tok/s overall), coherent answer |
| park after | conv-3.sess, 240,157 tokens in 4,825 ms; generations=3, **11,603 MiB** of the 24,576 MiB tier |
| non-finite | **0** |

So both restore paths are measured: same-boot reuse in 393 ms, cross-boot from disk in 32.7 s (≈118 MiB/s direct-I/O
checkpoint restore, against ~810 MiB/s park writes) — versus 842.7 s to read the prompt cold. The 240k no-output bug
and the park/restore story are both closed on the clamped engine.

Next, per operator order "turn the expert trail back on and measure again": clean A/B at reserves 600/600
(cache size held constant), arm `fson-rsv600` with `STRATA_FS_SLOTS=2,8` (the clamp grants what fits) against arm
`fsoff-rsv600` with the swap off, same window script.

### Expert trail back ON under the clamp (fson-rsv600, 00:41–00:59)

Reserves raised 256/192 → 600/600 so both cards clear the 512 MiB low line; `STRATA_FS_SLOTS=2,8`. The clamp
granted the full ask (no clamp line): `slots per layer per card: card0:2, card1:8`, CUDA1 548 MiB free with
everything loaded (after the 293 MiB allocation). Control arm `fsoff-rsv600` (swap off, same reserves) follows.

| Leg | Prompt | Decode | Drafts |
|---|---|---|---|
| 1 — cold infill | 239,646 read in 824,150 ms (**290.8 tok/s**) | 699 in 31,513 ms (**22.2 tok/s**) | 427/709 (60%) |
| 2 — same boot | 239,641 reused + 5 read in 250 ms; rewind pass re-read 535 at 51.8 tok/s | 514 at **30.2**, 136 at **32.1 tok/s** | 325/506, 84/134 |

Client: request 1 860.7 s end-to-end; request 2 41.6 s (16.0 tok/s overall). 0 non-finite, no stall, park/restore
all clean (conv-1/2/3, 11,608 MiB tier, park-gain skip, disk-only snapshot).

**The swap is inert in this workload**: every pass reports `0 experts served from the swap space, 0 copies
(0 from the router look-ahead), 0 not landed yet, 0 no free slot, 0 over budget`. The 240k prefill and short
draft-verified decodes never trip the admit path (admit after 2 misses per window; the expert cache plus the
helper cover the misses). Awaiting the same-reserve control before any speed verdict.

### The A/B verdict: expert trail on vs off, reserves 600/600 both arms (01:00–01:18)

`fson-rsv600` (`STRATA_FS_SLOTS=2,8`, clamp granted the full ask, CUDA1 548 MiB free after the 293 MiB allocation)
against `fsoff-rsv600` (swap off, CUDA1 842 MiB free). Same engine `4b8251e9`, same window script, same 239,646-token
prompt, caches cleared per arm. Both arms 0 non-finite, no stall, park/restore clean.

| Metric | FS ON | FS OFF | Delta |
|---|---|---|---|
| Cold prefill | 824,150 ms = 290.8 tok/s | 829,759 ms = 288.8 tok/s | +0.7%, inside noise |
| Cold decode (first pass) | 22.2 tok/s | 21.4 tok/s | +3.7%, small-N |
| Restored turn, decode | 30.2 / 32.1 tok/s | 29.0 / 29.8 tok/s | +2–4%, small-N |
| Second turn, client end-to-end | 41.6 s | 30.3 s | different completion lengths, not comparable |
| Swap service | **0 experts, 0 copies, 0 look-ahead** | n/a | — |
| Card1 free VRAM | 548 MiB | 842 MiB | the swap costs 293 MiB |

**Verdict: the Foresight expert trail is a wash in this workload.** Speed differences are inside run-to-run noise
(boot-to-boot fill-wall bimodality alone is larger, journal 2026-11-11 round 18ag), and the swap served zero experts
in every pass of every arm — the expert cache plus the helper cover all misses before the admit path (2 misses per
window) triggers. Its 293 MiB on card1 is pure cost. On this rig the swap has never been observed doing work:
`0 served / 0 copies` in the 3k oracle legs, the 240k legs, and now both A/B arms.

Recommendation (operator to decide): ship the clamp `564e1dbbea` and leave `STRATA_FS_SLOTS` at the champion's
`2,8` — the clamp self-disables it at 256/192 where it cannot fit, and grants it where it can, so no config change
is needed either way. A planner change that counts the FS allocation is only worth a window if a workload that
actually trips the swap (multi-conversation churn with cold experts) is ever in scope; none is measured today.

## Operator order 2026-11-22: the Foresight swap stays always on (quality first)

After the A/B wash the operator chose to keep the expert trail on as a quality safeguard rather than treat its
293 MiB as waste. The armed tuple is the measured `fson-rsv600` leg, promoted to the prod defaults:

| Part | Value |
|---|---|
| Engine | `run/fsclamp-bin/rpcw-hip/strata` = `4b8251e9e06334a8` (clamp `564e1dbbea`) |
| Helper | `run/fsclamp-bin/rpcw-cuda/strata-expert-helper-patient` = `9d16742d` (unchanged) |
| Config | `prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed-fson.json` — reserves **600/600**, `STRATA_FS_SLOTS=2,8` |
| Launcher | default `STRATA_PROD_BIN` → `run/fsclamp-bin`, default `STRATA_PROD_CONFIG` → the `-fson` config |

Why the reserves moved: at 256/192 the clamp correctly refuses the swap (434 MiB free < 512 low line + 293 MiB);
600/600 leaves CUDA1 at 548 MiB free after the full allocation, so the swap is *always granted*, and the clamp
remains the safety valve for any future tighter load. Cost measured: none outside noise (prefill 290.8 vs 288.8
tok/s, decode in noise, 240k + park/restore + 1k-decode all clean, 0 non-finite).

### Prod boot on the always-on tuple (01:26–01:28)

Booted through the launcher with the new defaults. Engine log tail: `foresight swap: slots per layer per card:
card0:2, card1:8` — full grant, no clamp line — and `CUDA1: 548 MiB of VRAM free with everything loaded`
(the 140 MiB LOW lines earlier in the same file are older boots). Health `ok`, `loaded: true`. One real
completion: HTTP 200 in 0.5 s, 8 tokens, 8/8 drafts accepted, 28.2 tok/s; the probe's first `content: null`
was `finish_reason: length` inside reasoning, the known benign class. Prod serves on `127.0.0.1:8009`.

### Operator order 2026-11-22: margins down 256 MiB (600/600 → 344/344)

3,058-token oracle leg first (fson-rsv344, 01:33): **pass**, 0 non-finite, 8 tokens, 5/8 drafts. The clamp's own
words at boot:

```
foresight swap: card0 asked 2 slots (114 MiB) with 488 MiB free -> granted 0 slots, leaving 488 MiB
foresight swap: card1 asked 8 slots (293 MiB) with 586 MiB free -> granted 2 slots (73 MiB), leaving 512 MiB
strata serve: CUDA1: 512 MiB of VRAM free with everything loaded
```

So −256 MiB of margin buys bigger expert caches, and costs most of the swap: `card0:0, card1:2` instead of
`card0:2, card1:8`. The two operator orders now pull against each other — "always on for quality" needs ~600/600,
"reduce margins" lands the cards just under the low line. The 240k window (fson-rsv344-240k) is running to confirm
long-context safety before any prod default moves.

### 240k + 1k decode at 344/344 (fson-rsv344-240k, 01:34–01:52) — clean

| | value |
|---|---|
| cold prefill | 239,646 read in 841,420 ms (**284.8 tok/s**) |
| cold decode | 651 in 28,586 ms (**22.8 tok/s**), drafts 399/670 (60%) |
| restored turn | 239,641 reused + 5 read in 244 ms; 405 at **32.2 tok/s**; client 17.8 s |
| non-finite | **0**; park/restore clean (conv-1/conv-2, 7,750 MiB tier) |
| clamp | card0:0, card1:2; CUDA1 512 MiB free; swap served 0 experts even with its 2 slots |

Speed sits inside the noise band of every measured tuple (prefill 284.4–290.8 tok/s, decode 20.2–32.2). Applied to
the prod `-fson` config (reserves 344/344) per the operator order; prod rebooted on it.

### Prod boot on 344/344 (01:53–01:54)

Health `ok`, `loaded: true`; one real completion: HTTP 200 in 6.6 s (first request pays window capture),
`finish=stop`, content `ready`. Clamp lines identical to the measured leg: card0 0, card1 2 slots, CUDA1
512 MiB free. Prod serves on `127.0.0.1:8009` with reserves 344/344, engine `4b8251e9`, helper `9d16742d`.

### Operator order 2026-11-22 (~01:58): ROCm card margins down another ~200 MiB (344/344 → 144/144)

Ordered with an engine restart, and with the upstream-plan subagent paused for the window (operator: "pause agent,
restart engine, resume agent" — session ses_dff35027cffemi359djp64GuRW cancelled, to be resumed on the same session
after prod is back). The 3k oracle leg runs before the prod boot, per the measure-first discipline.

Expected landing: free-before-FS ≈ 386 MiB on card1, so the clamp grants 0 slots everywhere (swap off) and the card
loads at ~386 MiB free — the same free-VRAM class as the measured 360 MiB tuple (`fs2-240k`: 239,663 tokens clean),
and above the 156 MiB full-card-stall line.

### Restart on 144/144 (02:19–02:20)

3k oracle first: **pass**, 0 non-finite, 6 tokens, 4/4 drafts, 82.6 tok/s prefill. Boot landed as predicted:
card0 288 / card1 386 MiB free at the FS point → clamp granted 0 everywhere, swap off; `CUDA1: 386 MiB of VRAM
free with everything loaded` — inside the measured-pass band (360 MiB passed 239,663 tokens; the stall line is 156).
Health `ok`, completion probe `finish=stop`, `ready` in 6.4 s. Prod serves on `127.0.0.1:8009` at reserves 144/144,
engine `4b8251e9`, helper `9d16742d`. The margin ladder for the record today: 256/192 → 600/600 (always-on swap)
→ 344/344 (−256) → **144/144 (−200)**, every step measured before the prod boot.

## The upstream 0.2.18 port starts (2026-11-22, operator order)

Phase 0 done by the parent: `git worktree add /mnt/srcds/Strata-wt-0218 -b port-0218 v0.2.18` — new branch `port-0218`
at tag `v0.2.18` (tip `6e38f588a6`, engine 0.1.64), both `engine/src/core/expert_source.cpp` and
`terminal/serve/server.py` present. Phases 1–2 delegated to a fixer riding the engine via the
`llamacpp/swift-1.5-iq3_xxs` entry (prod :8009): 140-file port manifest, the two hand-merges, CMake wiring, fork
dflash2 flag rename, then the gfx906 build — commits local and incremental, no boots, no GPU runs, nothing on :8009.
Plan of record: docs/PLAN-UPSTREAM-0218.md (approved, f10c877634).
