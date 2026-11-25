# How to optimise serving on this box — 2026-11-25

**Status:** draft, corrected after an adversarial review and a re-read of all eight cold 144k
legs. Every claim is either cited to a log/record or labelled as unconfirmed static analysis.
Two measurements are still PENDING and marked so.

Arms. **A** = fork engine 0.1.57 (binary `4b8251e9`, tree tip `564e1dbbea`, `run/fsclamp-bin`)
+ helper `9d16742d` + server tree `v0215`. **B** = upstream 0.2.18 port engine 0.1.64
(`15d330ea`, tip `db5bc3ab10`, `run/port0218-nohuge-bin`) + helper `58c3ae02` + server
`run/port0218-server`. The arm pins are **binary** sha256s, not commits.

---

## 1. Bottom line

1. **The largest explanatory variable in the 144k data is not the engine — it is box memory
   state.** Across all eight cold 144k prefill legs, swap used correlates with prefill rate at
   **r = −0.90**: the six legs under 9 GiB of swap averaged **311.3 tok/s**, the two legs at or
   over 9 GiB averaged **282.4 tok/s**. The arms overlap completely inside the low-swap band.
   **Caveat, and it is a real one:** both high-swap legs are the W4 legs, booted in a later
   session, so "swap" and "which session" are collinear here and this correlation cannot
   separate them. What it establishes is negative and still decisive: **the 144k spread is
   explained by something other than the engine**, and no W2 engine claim survives that.
2. **The −4.7 % "A faster at 144k" does not reproduce as an engine effect.** A's two claim
   legs span **1.1 %**; B's span **4.5 %**, and B's slow leg is the one that swapped 8 GiB
   (`W2-B-dec-r1`). Drop that leg and B is **−2.5 %** against A. The between-arm delta is
   smaller than one arm's own spread.
3. **The one genuine cross cell points the other way.** `W4-A-dec-r1` (A engine + B server,
   complete, cold, `0 reused`) ran at **279.2 tok/s** while `W4-B-dec-r1` (B engine + **B**
   server, same high-swap window) ran at **285.7** — same server, different engine, and the
   **B engine is 2.3 % faster** there. n=1 each; a direction, not a result.
4. **My earlier W4 conclusion was wrong and is retracted.** I reported "B-engine+A-server
   285.7" as a cross cell. Its boot record says `arm B, server_arm B`: it was a **native B
   control** booted under a cross-cell key, because `--server-arm` was never passed
   (`w4_finish.sh`). The claim "both cross cells are slower than both native cells" is
   therefore unsupported — only one cross cell exists.
5. **The inter-turn gap is real but its mechanism is not in the logs.** A spends 6,865 ms
   re-reading 99 tokens where B spends 3,382 ms. No W2 engine log contains a conversation-cache
   **restore** line at all, so "the snapshot came back from disk" is a hypothesis with no
   evidence, and I withdraw it.

---

## 2. All eight cold 144k prefill legs

Cold means `0 reused` on the judge line. Rate is the engine's own
`prompt 143893 tokens = 0 reused + 143893 read in N ms` figure.

| leg | engine | server | prefill tok/s | max swap MiB | min avail MiB |
|---|---|---|---|---|---|
| W2-A-dec-r0 | A | A | 303.2 | 2,908 | 13,248 |
| W2-A-dec-r1 | A | A | **322.4** | 2,968 | 11,193 |
| W2-A-dec-r2 | A | A | **318.9** | 3,372 | 13,033 |
| W2-B-dec-r0 | B | B | 311.9 | 3,514 | 12,736 |
| W2-B-dec-r1 | B | B | 298.7 | **8,011** | 9,415 |
| W2-B-dec-r2 | B | B | **312.5** | 3,599 | 12,077 |
| W4-A-dec-r1 | A | **B** | 279.2 | **11,724** | 9,066 |
| W4-B-dec-r1 | B | B | 285.7 | **13,808** | 10,158 |

Claim legs (r1, r2 per arm, the contract's sample): A **320.65**, B **305.60**, delta **−4.7 %**.

| slice | n | mean tok/s | reads as |
|---|---|---|---|
| all legs under 9 GiB swap | 6 | **311.3** | the arms' normal band (all W2 legs) |
| all legs at/over 9 GiB swap | 2 | **282.4** | **−9.3 %**, but these are also the two later-session W4 legs, so this is "session", not necessarily "swap" |
| B claim legs, excluding the 8 GiB-swap leg | 1 | 312.5 | −2.5 % vs A, not −4.7 % |
| A engine vs B engine, **same server, same window** | 1 / 1 | 279.2 vs 285.7 | **B +2.3 %** |

`max_rss` is ~41 GiB in every leg, so the swap differences are **box state** (what else ran,
page cache, prior boots) rather than an arm property. That is the confound to control, and it
was not controlled in W2.

**What this means for every number below:** a prefill comparison is only interpretable inside
one swap band. Any future leg must report `max_swap_used_mib` beside its rate, and a leg over
~9 GiB is not comparable with the W2 baseline.

---

## 3. The inter-turn gap: measured cost, unexplained mechanism

Same three-turn shape, same conversation, both arms. **Both arms park and both evict at the
same 512 MiB ceiling** (`dropped 1 parked conversation to the disk tier`, A `:121`, B `:128`);
each leg evicts twice (`evictions=2`).

| turn | A (`W2-A-sht-r1`) | B (`W2-B-sht-r2`) |
|---|---|---|
| re-read after the drop | `2052 reused + 99 read in 6865 ms (14.4 tok/s)` `:124` | `2052 reused + 99 read in 3382 ms (29.3 tok/s)` `:124` |
| next turn | `2052 reused + 118 read in 6714 ms (17.6 tok/s)` `:128` | `2052 reused + 118 read in 3685 ms (32.0 tok/s)` `:131` |

Inter-turn mean over turns 2–3: **A 6.904 s, B 3.577 s** (`journal/w2-window-2026-11-24.md`).

**What the logs do not say.** `grep -c 'conversation cache: restored'` is **0 in all twelve
W2 engine logs**; the only "restored" lines concern a draft ring, not a snapshot. The prefix
was served from cache (`2052 reused`), which is why the cost is a 99-token continuation rather
than a snapshot read. So the 3.5 s gap is real and unexplained by anything currently logged.

**The instrument already exists and needs no code.** `STRATA_TRACE` makes the engine print, per
admission, exactly the decomposition this question needs (`src/program/generate.cpp:23493-23506`):

```
strata trace: admission of slot N: drain <ms>, prompt <ms> (read <ms>, windows <ms>,
  K checkpoint(s) <ms> = sync <ms> + save <ms> + evict <ms>, rest <ms>), first window <ms>,
  slot copy <ms>, slot freed <ms>, checkpoint copy <ms>
```

None of the twelve W2 boots had it set (`grep -c 'strata trace:'` = 0 in every log), which is
why the gap is unattributed. **One ttft3 pair with `STRATA_TRACE=1` turns the 6.9 s vs 3.6 s
question into a named term** — evict, sync, save, windows, or "rest" — at the cost of two boots
and no build. That is rank 1 precisely because it is the cheapest decisive measurement left.

**Do not** raise the ceiling on faith. The drop message prints resident bytes *after* eviction
(`src/program/generate.cpp:13497-13516`), so "294 + 375 MiB against 512" is not a working set;
the leg's own counters show **784 MiB for two parked snapshots** (`822624624` bytes, B `:127`),
and five conversations are parked per leg. A ceiling that removes the drop would need
**≥ 2 GiB**, and those snapshots are anonymous host memory (`generate.cpp:1499-1503`) — exactly
what Linux swaps on a box whose worst leg already used 8 GiB. Any such trial must report
`max_swap_used_mib` and must not run on a leg that is already swapping.

---

## 4. Sample size: what any future boot can and cannot buy

| design | minimum exact two-sided p | so it can |
|---|---|---|
| 2 v 2 (W2, today) | 0.33 | describe a direction |
| 3 v 3 | 0.10 | describe more tightly |
| 5 v 5 | 0.008 | **only here** can a claim be confirmatory |

Three boots per arm **cannot** produce a confirmatory result. What they can do is narrow the
range, which is what the short shapes need: B is n=1 on pp2k, pp8k and ttft3 because
`W2-B-sht-r1` died on a kernel ring timeout and GPU reset (`exit code -7`).

---

## 5. Prefill: what differs, and what is still unexplained

**The mmq lead is dead — verified, not inferred.** Both arms run it off and say so:
`prefill mmq: off (env=0) [built=1]` (A) and `[built=0]` (B). `built()` is a compile-time link
property; B's lineage deleted the kernels and
`/mnt/srcds/Strata-wt-0218/engine/src/prefill/mmq_absent.cpp:19` is `bool built() { return
false; }`. With `env=0` on both sides the guard cannot affect timing, and a "size threshold"
on it would change nothing.

**What does differ** (`journal/prefill-static-diff-ab-2026-11-25.md`, UNCONFIRMED STATIC
ANALYSIS):

| axis | A | B | separable by |
|---|---|---|---|
| expert kernel class (mmq off) | FP16 dequant + GEMM (source only — **no boot line**) | own int8 dot kernels, `prompt kernels: … 48 of 48 layers on the int8 kernels` (boot line) | `STRATA_EXPERT_I8=0` on B, proved by that line changing |
| `trim_stage_weights` default | `false` (`src/program/generate.cpp:943`) | `true` (`engine/src/program/generate.cpp:1291`) | `--no-trim-stage-weights` on B |
| dense matrices resident | 301 (2,331.54 MiB) | 188 (1,474.17 MiB) | same leg, re-read |
| cache slots / resident experts | 4,951 / 9,125 | 5,739 / 11,149 | same leg, re-read |
| helper binary | `9d16742d` | `58c3ae02` | **not separable by any existing cell** |
| pool hugepage opt-in | absent | `STRATA_POOL_NOHUGEPAGE=1` | B-only; static diff dismisses it because `resident_pairs=0` in that window |
| spill tier | absent (flag not implemented) | `--conversation-spill-mib 0` | structural |

**The kernel direction is unknown, not a B handicap.** The static diff says so explicitly, and
B's own header claims int8 beats MMQ by 1.10–1.35×. Combined with the legs above, the honest
statement is: **the engine's prefill contribution at 144k is not measurable from W2**, because
memory state moves the rate by ~9 % and the engine by ≤2.3 % in the one clean pairing.

**And the two axes predict the wrong sign anyway.** B has better coverage (52.0 % → 60.4 %) and
more resident experts, so both axes predict B ≥ A at long context — yet B does not win there.
Neither axis explains a long-context deficit. The 144k-specific candidates that remain are the
own-int8 per-layer host work and group planning, the helper binary, and memory state.

---

## 6. Ranked actions

| # | Action | Why | Cost | Decides |
|---|---|---|---|---|
| 1 | **Run one ttft3 pair with `STRATA_TRACE=1`** | 6.9 s vs 3.6 s per turn is the biggest measured cost; the engine already prints drain / prompt(read, windows, checkpoints = sync+save+evict, rest) / slot copy / checkpoint copy, and **no W2 boot had the flag set** | 2 boots, no build | which term the 3.5 s sits in |
| 2 | **A `max_tokens 2048` leg (or `answer_room: false`)** | 6 of 12 W2 legs produced **no TG number at all**: thinking is capped at 512 by the answer-room rule, so every decode figure is below the 1,000-token floor. Nothing about decode can be claimed without this | 1 boot | a legitimate TG rate for the first time |
| 3 | **Establish the 144k noise floor**: A and B interleaved, 4–5 legs each, swap recorded per leg, **all in one session** | the current between-arm delta (4.7 %) is **smaller than B's own spread (4.5 %)**; the later-session legs run ~9 % slower, and only an interleaved design can tell session from swap | ~8 boots | whether any engine delta exists at all |
| 4 | **3 more B short boots** (pp2k/pp8k/ttft3) | B is n=1 there; the +13.6…15.7 % is a lead from one boot | 3 boots | a 3 v 3 descriptive range |
| 5 | **`STRATA_EXPERT_I8=0` on B, at 2k/8k and 144k** | isolates the kernel axis; pass criterion is the `48 of 48 layers` line changing | 2 boots, no build | the kernel half |
| 6 | **`--no-trim-stage-weights` on B, at 144k** | isolates the residency axis, which matters at long context — not at 2k where it cannot show | 1 boot, no build | the residency half |
| 7 | **Helper swap leg** (A engine with helper `58c3ae02`) | the helper binary differs and no cell has ever separated it | 1 boot + helper build | whether the helper explains part of the pair delta |
| 8 | Finish the W4 2×2 **with `--server-arm`** | only one genuine cross cell exists today; the other was mis-keyed | ~50 min | engine-vs-server, veto-only |

---

## 7. What not to do, and why

| Don't | Why |
|---|---|
| Read the 144k −4.7 % as an engine effect | A spans 1.1 %, B spans 4.5 %, the swap confound is 9 %, and the one clean same-server pairing favours **B** |
| Cite the 58k W1f sample as evidence about 144k | it is the `pp239k` **byte** name for a corpus that serves **58,423 tokens** — a *shorter* context, not longer; its within-arm spread (31 %) dwarfs every effect here |
| Treat `p = 1.00` as "no difference" | at 3 v 3 the minimum attainable is 0.10, and the suppressed median there was **−8.1 %**, in the same direction as the delta being dismissed |
| Report any W2 decode number as TG | 512–545 generated tokens against a 1,000 floor |
| Raise `--conversation-cache-ram-mib` to 1 GiB | the working set is ≥784 MiB for two snapshots and five park per leg; and the snapshots are swappable host memory |
| Chase mmq `built=` | both arms run mmq **off** (`env=0`) |
| Re-open THP, mlock or relief re-ranking | all three retracted with evidence (`journal/fault-investigation-corrections-2026-11-24.md`) |
| Keep hunting the permission fault | fixed and promoted: `HSA_USERPTR_FOR_PAGED_MEM=0`, 10/10 clean cold 58k boots against a 4-in-9 baseline (P=0.003), `b7d46f9f80` |

---

## 8. PENDING and blocked

| # | Item | Blocker |
|---|---|---|
| P1 | W4 cells 3–4 **with `--server-arm`** | prerequisite 0: a non-engine process ≥ 2 GiB is **unwaivable** (`port_ab_prereq0.py`); on this box that is the agent host's own MCP python at 2.03 GiB. Stopping it is an **operator decision** |
| P2 | W3s span pair (`dec144k-spans`, `pp239k-spans`, 2 warmups per arm) | same gate |
| P3 | Restore/continuation-path instrumentation | not started; needs engine work |
| P4 | 144k noise floor (action 3) | same gate |
| P5 | `max_tokens 2048` TG leg (action 2) | same gate |

---

## Sources

| what | path |
|---|---|
| W2 numbers, arm inventory, oracle corrections | `journal/w2-window-2026-11-24.md` |
| prefill static diff, two lineages (UNCONFIRMED) | `journal/prefill-static-diff-ab-2026-11-25.md` |
| all cold 144k legs with memory | `out/port-ab-20261123/records/{W2,W4}-*/[AB]-dec144k.json` |
| cell identity per boot | `out/port-ab-20261123/boots/W{2,4}-*.json` (`arm`, `server_arm`, `server_tree`, `helper_sha256`) |
| judge lines | `out/port-ab-20261123/logs/*.engine.log` |
| contract constants, claim rule, TG floor | `bench/ab-v0210/rpcprof/port_ab_contract.py` |
| fault fix evidence | `journal/userptr0-cold58k-2026-11-25.md` |
| retractions | `journal/fault-investigation-corrections-2026-11-24.md` |

## What this report cannot support

1. **No engine attribution at 144k.** The between-arm delta is inside the noise and the swap
   confound, and the one clean same-server pairing (n=1 each) points the other way.
2. **No decoded-rate claim of any kind** until a leg clears 1,000 generated tokens.
3. **No confirmation.** Nothing here has the sample size for p < 0.05; every comparison is a
   direction.
4. **No mechanism for the inter-turn gap.** The cost is measured; the cause is not.
