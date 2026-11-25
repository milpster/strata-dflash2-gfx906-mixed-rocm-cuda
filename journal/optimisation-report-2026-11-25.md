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
4. The sign flips with context length (B faster at 2k/8k, A faster at 144k). That is the
   signature of a size-dependent path selection, and the boot lines name one: A reports
   `prefill mmq built=1`, B `built=0`.

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

## 5. Action 3 — the mmq / size-threshold lead (PENDING static diff)

The boot lines differ by one letter that matters: A `prefill mmq built=1`, B `built=0`. A also
holds more on-device state: 301 dense/native matrices (2,331.54 MiB) vs B's 188 (1,474.17 MiB);
4,951 vs 5,739 cache slots; 9,125 vs 11,149 resident experts.

The sign flip is the hypothesis: B wins at 2k and 8k, A wins at 144k. A size-dependent path —
one that has per-chunk or per-launch overhead at small sizes and a throughput payoff at long
context — produces exactly that. mmq selection is the obvious candidate.

**PENDING:** `bench/fault-va/out/static-diff-ab-prefill.md` (static analysis lane, in progress)
establishes whether `built=` is a compile-time option, a capability probe or a size heuristic,
and what the engine does when it is 0.

**If true, the fix is a threshold, not a choice:** keep mmq above the size where it pays and
fall back below it. That would capture both shapes instead of trading one for the other.

**How to verify, once the static diff lands:** W3s span pair at 2k/8k and at 144k. Prediction
if the hypothesis holds: A's per-chunk cost is higher in one named stage at 2k, and lower in
the same stage at 144k. Any other pattern kills it.

---

## 6. Ranked, with cost

| # | Action | Evidence | Expected | Cost | Needs GPU |
|---|---|---|---|---|---|
| 1 | Raise `--conversation-cache-ram-mib` to ~1024 so nothing drops | two live snapshots 294 + 375 MiB against a 512 MiB ceiling; the drop line is `W2-A-sht-r1.engine.log:121` | removes the restore for **both** arms: ~3.4 s/turn (B), ~6.8 s (A) | config + ~512 MiB RSS | 1 pair of boots |
| 2 | 3 more B short boots | B n=1 | turns a +14 % lead into a decision | 3 boots | yes |
| 3 | mmq size threshold | `built=1` vs `built=0`; sign flip | both shapes, size unknown | code + build | yes |
| 4 | Backport the 0.1.64 restore path to the fork engine | same 99-token re-read: 6,865 ms vs 3,382 ms, identical cache config | −3.4 s/turn at zero memory cost | engine work + build | yes |
| 5 | Finish W4 cells 3–4 + W3s spans | attribution | decides engine-vs-server (veto-only) | ~50 min | yes |

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
| P3 | Static diff A-vs-B prefill path | whether mmq is compile-time or heuristic | running now |
| P4 | `--conversation-cache-ram-mib 1024` pair (action 1, lever A) | whether the drop/restore is the inter-turn cost, and whether it helps both arms | needs a window |
| P5 | Restore-path diff 0.1.57 vs 0.1.64 (action 1, lever B) | names the function behind the 2.1× restore cost | static lane, secondary task |

---

## Sources

| what | path |
|---|---|
| W2 numbers, arm differences, oracle corrections | `journal/w2-window-2026-11-24.md` |
| W1f 239k prefill (p=1.00) | W1f records, `out/port-ab-20261123/records/W1f-*` |
| park/re-read log lines | `out/port-ab-20261123/logs/W2-{A,B}-sht-r*.engine.log` |
| contract constants, claim rule, TG floor | `bench/ab-v0210/rpcprof/port_ab_contract.py` |
| fault fix evidence | `journal/userptr0-cold58k-2026-11-25.md` |
| retractions | `journal/fault-investigation-corrections-2026-11-24.md` |
| arm pins | `bench/ab-v0210/rpcprof/port_ab_configs.py` |
