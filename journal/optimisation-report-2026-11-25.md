# How to optimise serving on this box — 2026-11-25

**Status:** draft. Two measurements are pending and are marked PENDING where they matter:
W4 cells 3–4 (2x2, engine-vs-server attribution) and the W3s span pair (names the losing
prefill stage). Every number below that exists is cited to a log or record; nothing is
estimated except where the word "expected" appears.

Window these numbers come from: W2, `journal/w2-window-2026-11-24.md`, 12 boots, 6,428 s wall.
Arms: **A** = fork engine 0.1.57 (`4b8251e9`, `run/fsclamp-bin`) + helper `9d16742d` + server
tree `v0215`. **B** = upstream port 0.2.18 engine 0.1.64 (`15d330ea`, `run/port0218-nohuge-bin`)
+ helper `58c3ae02` + server `run/port0218-server`.

---

## 1. Bottom line

1. **The biggest measured loss is not prefill and not decode — it is the disk-tier snapshot
   restore.** Both arms park and both drop one conversation to disk; A then re-reads 99 tokens
   in **6,865 ms (14.4 tok/s)**, B in **3,382 ms (29.3 tok/s)**. That ~3.5 s is, to within
   rounding, the entire inter-turn gap (6.904 s vs 3.577 s = 3.33 s). The cache configuration
   is byte-identical between the arms, so this is the engine's restore path, not a setting —
   but raising the RAM ceiling above the working set would mean nothing ever drops, which is a
   config change that helps both arms.
2. **The 144k prefill gap (−4.7 %) is where not to start.** At n=3 on a longer shape the two
   engines were indistinguishable (W1f, 239k: A 275.2 vs B 254.5 tok/s, p=1.00). W2's −4.7 %
   is a direction from two boots per arm, and it may be real — but it is small and unproven.
3. **The largest engine-side signal is short-shape prefill in B's favour (+13.6…15.7 %), and
   it is n=1.** The cheapest next move there is boots, not code.
4. The sign flips with context length (B faster at 2k/8k, A faster at 144k), and the static
   diff found why: the two engines use **different expert kernel classes** (FP16 dequant + GEMM
   against B's own int8 dot kernels) and B additionally gains residency by trimming stage
   weights. B pays a kernel handicap and collects a residency bonus in the same number. The
   mmq `built=` difference is a red herring: both arms run mmq **off**.

---

## 2. What is measured, and how strong it is

| metric | A | B | Δ | n (A/B) | usable as |
|---|---|---|---|---|---|
| 144k prefill (`read_tps`) | 320.65 tok/s | 305.60 | −4.7 % (B slower) | 2/2 | **direction only**; 2v2 min exact p = 0.33 |
| 144k decode, request 2 (cached) | 28.55 tok/s | 31.70 | +11.0 % (B faster) | 2/2 | **decode metric, not TG** — 513/545 and 512/533 tokens generated, all below the 1,000-token floor |
| pp2k prefill | 76.05 tok/s | 88.0 | +15.7 % | 2/**1** | lead only |
| pp8k prefill | 181.45 tok/s | 206.2 | +13.6 % | 2/**1** | lead only |
| ttft3 turn-1 TTFT | 20.849 s | 14.601 s | −30.0 % | 2/**1** | lead only |
| ttft3 inter-turn (turns 2–3) | 6.904 s | 3.577 s | −48.2 % | 2/**1** | lead only |
| 239k prefill (W1f) | 275.2 tok/s | 254.5 | — | 3/3 | **indistinguishable, p = 1.00** |

Two rules that govern the table, both from the contract (`port_ab_contract.py`): 2v2 gives
`C(4,2)=6` allocations and a minimum exact two-sided p of **0.33**, so no comparison here is
a confirm/deny; and no figure may be called a TG rate with fewer than 1,000 generated tokens.

---

## 3. Action 1 — stop the parked conversation reaching the disk tier

**Corrected mechanism (2026-11-25).** The W2 journal's shorthand — "A parked, B is not
parked" — is wrong in one respect: **both arms park, both drop one conversation to the disk
tier, both re-read it.** B's "not parked" line (`W2-B-sht-r2.engine.log:121`) is about a
*superseded reply*, not the conversation state. What differs is the price of the re-read.

| step | A (`W2-A-sht-r1`) | B (`W2-B-sht-r2`) |
|---|---|---|
| RAM ceiling | 512 MiB | 512 MiB |
| parked, in order | 2,088 → 8,063 → 2,120 tokens | 2,088 → 8,063 → 2,120 tokens |
| drop | `dropped 1 parked conversation … (RAM ceiling 512 MiB, 294 MiB resident, 1 parked)` `:121` | same line, 409 MiB resident `:128` |
| re-read | `prompt 2151 tokens = 2052 reused + 99 read in 6865 ms (14.4 tok/s)` `:124` | `… + 99 read in 3382 ms (29.3 tok/s)` `:124` |
| next turn | `+ 118 read in 6714 ms (17.6 tok/s)` `:128` | `+ 118 read in 3685 ms (32.0 tok/s)` `:131` |

**The cache configuration is identical between the arms** — `--conversation-cache-mib 4096`,
`--conversation-cache-min-tokens 2048`, `--conversation-cache-park-gain 4096`,
`--conversation-cache-ram-mib 512`, `--conversation-cache-disk-mib 24576`,
`--conversation-cache-disk-ram-mib 64`, `--conversation-cache-evict-page-cache`. B adds only
`--conversation-spill-mib 0`. So the 2.1× is **the engine's restore path** (0.1.57 against
0.1.64), not a knob we can tune apart.

Two levers follow, and they are not the same lever:

| lever | what it does | expected | cost |
|---|---|---|---|
| **A. raise `--conversation-cache-ram-mib` above the working set** | two live snapshots (294 + 375 MiB) exceed the 512 MiB ceiling, so one is *always* evicted. At ~1,024 MiB nothing drops and no restore is needed — this helps **both** arms | removes the restore: ~3.4 s/turn for B, ~6.8 s for A | +512 MiB RSS on a box where B's r1 already swapped 8 GiB |
| **B. fix the restore path** (diff the conversation-cache restore between 0.1.57 and 0.1.64) | makes A's restore as cheap as B's | ~3.4 s/turn for A, zero memory cost | engine work + build |

Lever A is a config change and testable in one 3-turn pair. Lever B is where the actual 2.1×
lives, and it is a *named function to diff*, which is much stronger than "the server is
slower".

**How to verify A.** One A boot at `--conversation-cache-ram-mib 1024`, three turns.
Prediction: the `dropped 1 parked conversation` line disappears and inter-turn falls toward
the prefill-only cost. If the drop disappears but the turns do not speed up, the restore was
never the cost and the hypothesis dies.

**How to verify B.** Diff the restore path across the two engine versions on an identical
drop. If 0.1.64 reads a snapshot in ~3.4 s where 0.1.57 needs ~6.8 s, the static lane should
name the function and the knob is a backport, not a setting.

---

## 4. Action 2 — finish B's sample before touching code

B has **n=1** on pp2k, pp8k and ttft3, because `W2-B-sht-r1` died on a kernel ring timeout and
GPU reset (`launcher: the engine stopped unexpectedly (exit code -7)`, `W2-B-sht-r1-boot.log:27`).
Operator ruling: hardware hiccup, not an arm property; the gate discarded the boot anyway.

Three boots turn the +14 % short-shape lead into a decision. Nothing else in this report can
promote a code change on n=1 evidence, and 3 boots are much cheaper than a build.

---

## 5. Action 3 — the prefill gap is a kernel-class swap plus a residency default (static diff done)

**The mmq lead is dead.** Both arms run with mmq off, and their own boot lines say so:

| arm | line |
|---|---|
| A | `prefill mmq: off (env=0) [built=1]` (`W2-A-dec-r1.engine.log`) |
| B | `prefill mmq: off (env=0) [built=0]` (`W2-B-dec-r1.engine.log`) |

`built=` is a compile-time link property, not a runtime choice: A links llama.cpp's MMQ kernels;
B's lineage deleted them and `engine/src/prefill/mmq_absent.cpp:19` is
`bool built() { return false; }` (verified in `/mnt/srcds/Strata-wt-0218`, the 0.1.64 lineage,
tip `db5bc3ab10`). The guard is `built() && env != 0` and `env` is 0 on both sides, so the
flag cannot move any timing, and a "size threshold" on it would change nothing.

**What actually differs, verified in source:**

| difference | A (0.1.57 fork) | B (0.1.64 port) | evidence |
|---|---|---|---|
| expert kernel class with mmq off | FP16 dequant + GEMM | its own int8 dot kernels on **48 of 48 layers** | A `prefill.cpp:1565,7986`; B's own boot line: `prompt kernels: every chunk runs 48 of 48 layers on the int8 kernels` |
| `trim_stage_weights` default | **false** (`src/program/generate.cpp:943`) | **true** (`engine/src/program/generate.cpp:1291`) | source, both trees |
| dense matrices resident | 301 (2,331.54 MiB) | 188 (1,474.17 MiB) | W2 records |
| cache slots / resident experts | 4,951 / 9,125 | 5,739 / 11,149 | W2 records |

The residency rows are not independent of the trim row: trimming stage weights frees dense
weights per layer, and B's extra ~850 MiB of device memory is the kind of room that buys 788
more cache slots and 2,024 more resident experts. **So B pays a kernel handicap and collects a
residency bonus in the same measurement**, which is the most direct reading of the sign flip: at
2k/8k the per-chunk kernel cost dominates and B still wins, while at 144k the whole-prompt rate
and resident coverage dominate.

**Two knob legs decide it without a build:**

| leg | what it isolates | prediction if this account is right |
|---|---|---|
| B with `--no-trim-stage-weights` | residency | B's slots and resident experts fall toward A's, and its 2k/8k lead shrinks |
| B with `STRATA_EXPERT_I8=0` | kernel class | B's prompt kernels fall back to FP16 and its 2k/8k lead disappears |

One boot each, no build, and run them at 2k/8k where the effect is largest rather than at 144k.

**Ruled out in source** (`journal/prefill-static-diff-ab-2026-11-25.md`): chunk geometry is
identical (`--prefill 16384` explicitly, so 8×16,384 + 12,821 in both), 46 files including
`moe_mmq.cu` are byte-identical across the lineages, and helper volume is near-identical
(270 / 68,775 against 270 / 69,116).

**Honest limit:** these are engine-internal differences, so W4 cannot separate them — its
server side carries identical argv. The knob legs and W3s spans are the separators.

---

## 6. Ranked, with cost

| # | Action | Evidence | Expected | Cost | Needs GPU |
|---|---|---|---|---|---|
| 1 | Raise `--conversation-cache-ram-mib` to ~1024 so nothing drops | two live snapshots 294 + 375 MiB against a 512 MiB ceiling; the drop line is `W2-A-sht-r1.engine.log:121` | removes the restore for **both** arms: ~3.4 s/turn (B), ~6.8 s (A) | config + ~512 MiB RSS | 1 pair of boots |
| 2 | 3 more B short boots | B n=1 | turns a +14 % lead into a decision | 3 boots | yes |
| 3 | B with `--no-trim-stage-weights` (residency leg) | trim default true vs false; 188 vs 301 dense matrices | decides the residency half of B's +14 % | 1 boot, no build | yes |
| 4 | B with `STRATA_EXPERT_I8=0` (kernel leg) | FP16 vs own int8, 48/48 layers | decides the kernel half | 1 boot, no build | yes |
| 5 | Backport the 0.1.64 restore path to the fork engine | same 99-token re-read: 6,865 ms vs 3,382 ms, identical cache config | −3.4 s/turn at zero memory cost | engine work + build | yes |
| 6 | Finish W4 cells 3–4 + W3s spans | attribution of the parking/restore path | decides engine-vs-server (veto-only) | ~50 min | yes |

---

## 7. What not to do, and why

| Don't | Why |
|---|---|
| Start with the 144k prefill −4.7 % | At n=3 on 239k the engines were indistinguishable (p=1.00); at 2 boots/arm the −4.7 % cannot be confirmed or denied |
| Report any decode number as TG | 512/513 generated < 1,000 floor; the cap is the answer-room rule at `max_tokens 1024` (thinking limited to 512). Use ≥2048, or lower the reserve |
| Re-open THP / mlock / relief re-ranking | All three retracted with evidence, `journal/fault-investigation-corrections-2026-11-24.md` |
| Cut resident experts to save memory | B's swap blowout (8,011 MiB used, 9.19 GiB avail) shows the cost side; the memory saving is real, the speed claim is not measured |
| Keep hunting the permission fault | Fixed and promoted: `HSA_USERPTR_FOR_PAGED_MEM=0`, 10/10 clean cold 58k boots vs 4-in-9 baseline (P=0.003), commit `b7d46f9f80` |

---

## 8. PENDING slots

| # | Measurement | Decides | Blocker |
|---|---|---|---|
| P1 | W4 cells 3–4 (B-engine+A-server r2, A-engine+B-server r2) | engine vs server, veto-only | prereq 0 (backup + MCP RSS) |
| P2 | W3s span pair (`dec144k-spans`, A and B) | which stage loses the 144k prefill ms | same |
| P3 | **done** — mmq is compile-time and off in both; the axes are kernel class and the trim default | — | `journal/prefill-static-diff-ab-2026-11-25.md` |
| P4 | `--conversation-cache-ram-mib 1024` pair (action 1, lever A) | whether the drop/restore is the inter-turn cost, and whether it helps both arms | needs a window |
| P5 | Restore-path diff 0.1.57 vs 0.1.64 (action 1, lever B) | names the function behind the 2.1× restore cost | not started |
| P6 | B `--no-trim-stage-weights` at 2k/8k (action 3) | the residency half of B's lead | needs a window |
| P7 | B `STRATA_EXPERT_I8=0` at 2k/8k (action 3) | the kernel half of B's lead | needs a window |

---

## Sources

| what | path |
|---|---|
| W2 numbers, arm differences, oracle corrections | `journal/w2-window-2026-11-24.md` |
| W1f 239k prefill (p=1.00) | W1f records, `out/port-ab-20261123/records/W1f-*` |
| park/re-read log lines | `out/port-ab-20261123/logs/W2-{A,B}-sht-r*.engine.log` |
| contract constants, claim rule, TG floor | `bench/ab-v0210/rpcprof/port_ab_contract.py` |
| prefill static diff (UNCONFIRMED STATIC ANALYSIS) | `journal/prefill-static-diff-ab-2026-11-25.md` |
| fault fix evidence | `journal/userptr0-cold58k-2026-11-25.md` |
| retractions | `journal/fault-investigation-corrections-2026-11-24.md` |
| arm pins | `bench/ab-v0210/rpcprof/port_ab_configs.py` |
