# W4 gate metric: gen_tps -> read_tps — 2026-11-25

**Branch** `recovery-startup-watchdog` · **Lane** fixer, CPU-only · **Prod:** serving on :8009 throughout;
no boot, no GPU work, no process control, no :8009 traffic, nothing written under
`bench/ab-v0210/rpcprof/out/port-ab-20261123/records` or `logs`. **No push.**

## 1. The blocker this closes

`port_ab_contract.WINDOWS["W4"]["gate_metric"]` was `gen_tps` on `dec144k` (`kind: tg`, cap 1,024), so
`--check W4` demanded **>= 1,000 tokens actually generated** (`TG_CLAIM_MIN_GENERATED`). This model cannot
produce them at that shape here. All four W2 decode boots generated **512-513** tokens
(W2-A-dec-r1 513, r2 513; W2-B-dec-r1 512, r2 512), and every one of those boots' logs carries the server's own
line `thinking limited to 512 of max_tokens 1024 so the answer has room`.

**The guard, exactly.** With thinking on and no budget of the request's own, the reply is closed at
`max_tokens - min(max(ANSWER_RESERVE_MIN 512, max_tokens // 4), max_tokens // 2)`.
At cap 1,024 the binding term is the **512 floor**, not `max_tokens // 4` (which is 256 here):
`1024 - min(max(512, 256), 512) = 512`. The reply writes its answer and ends at EOS, so the judge line
counts 512-518 generated, never the cap. Code: `serve/server.py:5501-5513` (`run/port0218-server`) and
`:6399-6411` (`v0215`); `ANSWER_ROOM_MIN_CAP 1024` / `ANSWER_RESERVE_MIN 512` at `:222-225`.

It bites because **thinking is on**: the claim request template's top-level `"enable_thinking": false` is not
read by the chat path — only `chat_template_kwargs.enable_thinking`, or a `reasoning_effort` of `"none"`,
turns it off (`serve/frontend.py:669-680`, `:173-187`), and the template's `reasoning_effort "low"` leaves it on.
Not fixed here (out of scope for a metric re-scope); it is why the cap, not `max_tokens`, decides the count.

Raising the cap does not help: W3p generated 1,536 under a 2,048 cap, and W3p only clears the floor because it
runs thinking ON with a 2,000-token budget — a different serving shape and not poolable with W4.

## 2. The change

| item | before | after |
|---|---|---|
| `WINDOWS["W4"]["gate_metric"]` | `gen_tps` | `read_tps` |
| `WINDOWS["W4"]["delta"]` | `None` | `0.05` (the CLAIM block's `prefill_read_tps`, copied) |
| `WINDOWS["W4"]["delta_note"]` | — | the decode 8 % kept as history, named as `decode_gen_tps` |
| `WINDOWS["W4"]["metric_note"]` | — | what read_tps is measured on (request 1) |
| `LEGS["dec144k"]["tg_claim_by_window"]` | — | `{"W4": False}` |
| `claims_tg()` | kind + cap | reads `tg_claim_by_window` first |
| `port_ab_prepare.DELTA_KEY["dec144k"]["W4"]` | `None` | `prefill_read_tps` |
| `port_ab_prepare.verify()` | did not re-derive gate decisions | pins gate_metric/delta/delta_note/metric_note |

Nothing the boots RUN moved: same boot groups, same 4 boots, same cells, same interleave, same `dec144k`
two-request tg shape, same 1,024 cap, same corpus (796,618 B), same budgets (`[2744, 3585]` s), same 51 config
pins (no sha moved).

**Why the leg keeps its tg shape.** Only the *judgement* was wrong, not the leg. Keeping the two requests keeps
W4's boots directly comparable to W2's and still records a decode number as context; `tg_claim_by_window` is
the single switch that stops that number from being read as a TG claim.

## 3. Validation (CPU-only)

| check | result |
|---|---|
| re-keyed `--check W4` **before** | `VERDICT: 4 problem(s)` — 4 × `TG leg generated 512/513 ... below the 1000-token TG floor` |
| re-keyed `--check W4` **after** | `VERDICT: PASS`, TG column `ctx` on all four, read_tps 322.4/318.9 (A) and 298.7/312.5 (B) |
| `port_ab_prepare.py --prepare` | 8 windows, 51 configs, all identities re-hashed |
| `port_ab_prepare.py --verify` | `verify: OK` |
| tree content hashes before/after | **2 of 2,730 files** changed (manifest + its sha); 0 config/corpus/template/record/log byte moved |
| manifest semantic diff | 11 moved paths: 2 prep timestamps, 3 harness-script shas, **6 all inside `/windows/W4/`** |
| `port_ab_legs.py --runbook W4` | byte-identical to before; 4 boots, 4 legs, 4 stops |
| `port_ab_legs.py --selftest` | OK, incl. new `w4-short-generation-passes-on-read_tps` |
| `pytest tests/test_port_ab_{w4_read_metric,prodshape,prune}_cpu.py` | 37 passed |

The floor itself is unchanged and still tested: the selftest's 999/1,000 cases moved from W4 to **W3**, the
window whose metric really is `gen_tps`, so the floor keeps its coverage instead of losing it with W4.

## 4. What W4 can and cannot conclude with read_tps

**Can (veto only).** With 2 boots per cell, min exact two-sided p = 0.33, so the cell can **VETO** an engine
claim: if the read-rate difference follows the **server** (A-engine+B-server lands on B's rate, B-engine+A-server
on A's), or the two cross cells disagree with each other, the pair-level engine attribution is refuted for the
prefill shape. That is all it is for.

**Cannot.** It can never **establish** an engine claim — no interval at n=2/cell reaches 90 %; the 5 % delta is a
reference, not a threshold this window can clear into a claim. It says nothing about **decode** (that metric is
unavailable here, which is why the metric moved). And it is a **prefill** statement only: at 143,893 tokens it
does not transfer to short contexts, to the prod-shape thinking-on serving path, or to any rate W1/W2/W3 own.

Context from the W2 records (not a W4 result): A 320.65 / B 305.60 median tok/s at 144k, i.e. B slower by
**4.69 %**, inside the pre-registered 5 % band. W4's job is to see which cell reproduces that — as a veto test,
descriptive at n=2/cell.

## 5. Notes left standing

- 12 tracked `boots/W2-*.evidence.json` files were already dirty when this task started (mtime 00:51:13,
  from an earlier `--check W2` — `check()` writes evidence as a side effect, the preflight journal's §7 hazard).
  They are **not** part of this commit and were not reverted by me.
- `--check` writes `boots/<key>.evidence.json`; every check in this task ran against
  `bench/fault-va/out/w4-rekey-20261125/` (since removed), never against the real records tree.
- The preflight's other findings (server-tree gate, `stop.sh` nohuge path, `:8009` hard-coded URL, helper sha
  gate) are untouched and still open.
