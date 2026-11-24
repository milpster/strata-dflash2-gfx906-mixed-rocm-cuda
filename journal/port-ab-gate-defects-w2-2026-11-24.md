# Three gate defects that let bad data pass (W2 oracle review) — 2026-11-24

CPU only: no boot, no leg, no GPU work, no `:8009` traffic, no process control, no push. Prod was
serving throughout. Branch `recovery-startup-watchdog`.

Scope: three defects an oracle review of the completed W2 window found, each one a way for data
that cannot support a statement to be printed as though it could. No leg shape, cap, delta,
budget, pin or window definition was changed, and no window was re-run.

## The defects and what changed

| # | Defect | Evidence it was real | Change |
|---|---|---|---|
| 1 | `port_ab_ttft.py` wrote no engine identity | all five `W2-*-ttft3` records carried no `engine_sha256`/`engine_pid`; `--check` printed them as "pre-identity record … not attributable to a build" (INFO) and their MemSampler was unattributed | the record now carries `engine_exe`, `engine_sha256`, `engine_identity_reason` and `mem.engine_pid`, written by the **existing** `ab9_driver.engine_identity` (imported, not duplicated) after the requests and the sampler, exactly as `ab9_driver.py` does; `--engine-pid` already existed and is now resolved once so the sampler and the identity describe the same process. A ttft leg with no attributable engine is now a **FAIL** in `--check` |
| 2 | `--check` could not see an unbalanced claim sample | arm B lost one short boot (`W2-B-sht-r1`, a GPU reset), so every short shape held A=2 / B=1 claim legs — and the gate printed every row and **passed**, while the report stated n=2/arm | `--check` now classifies every boot x shape as `claim`/`pilot`/`warmup`/`discarded`, prints a per-window **claim-n table** (shape x arm, claim legs only) and makes an unbalanced sample a **problem line** carrying both arms' counts |
| 3 | `LOW` was a log-format fingerprint, not a condition | `contract.EXPECTED_LINES["low_warning"]` matched only `" - LOW:"`, which only the fork server prints; the port server prints `N MiB of the card's memory are free at the start of this request … Below 512 MiB …`. "LOW 1 vs 1" in the W2 table was two spellings, not equal conditions (`logs/W2-A-dec-r0-boot.log` vs `logs/W2-B-dec-r0-boot.log`) | the regex counts all three known phrasings; the count is taken over the boot's own log segment (where the per-request warnings are) instead of the pre-request prefix alone; the column is renamed `lowVRAM` with a footnote saying what it counts |

### Why imbalance is a problem line, not a hard refusal (defect 2)

The contract's `CLAIM` block makes an n=2 window's output **descriptive**: `confirm_when`,
`within_delta_clause` and `never` all say no claim is supported at this n. An unbalanced
*descriptive* sample can still be printed honestly — it simply can never be described as
n=2/arm. So the gate reports the counts and lets the verdict carry them, rather than refusing
to describe a window that did run. What it will no longer do is print every row and pass while
other text calls the window balanced.

The table is driven by the **window's schedule**, not by the records that happen to exist: a
scheduled claim boot that wrote no record for a shape is a discarded leg, and it is that missing
leg which shortens the arm. Counting only records would have hidden exactly the defect.

Classification comes from `legs.schedule(window)` and never from the rep number: W2's pilot and
warmup are both rep 0, W1d has only a warmup at rep 0, and W1f's claims start at r1.

## The new W2 output (expected, and correct)

```
claim-n table: CLAIM legs only per shape and arm (pilot/warm/disc are shown and are in no n)
shape      arm  claim  pilot   warm   disc      n
dec144k     A      2      0      1      0      2
dec144k     B      2      0      1      0      2
pp2k        A      2      1      0      0      2
pp2k        B      1      1      0      1      1
pp8k        A      2      1      0      0      2
pp8k        B      1      1      0      1      1
ttft3       A      2      1      0      0      2
ttft3       B      1      1      0      1      1

NOTE: pp2k: W2-B-sht-r1 is out of the claim sample: the boot was discarded (restart + kernel
      fault fingerprint)
NOTE: pp8k: W2-B-sht-r1 is out of the claim sample: the boot was discarded (restart + kernel
      fault fingerprint) and it has no pp8k record
NOTE: ttft3: W2-B-sht-r1 is out of the claim sample: the boot was discarded (restart + kernel
      fault fingerprint) and it has no ttft3 record
FAIL: pp2k: claim n A=2 B=1 - the arms are not balanced; the window cannot be read as n=2/arm
      on this shape (it holds n=1 on the short arm)
FAIL: pp8k: claim n A=2 B=1 - …
FAIL: ttft3: claim n A=2 B=1 - …
```

The `lowVRAM` column now reads **3 / 3** on W2 where it used to read 1 / 1: each arm's boot
prints three low-VRAM warnings (one `- LOW:` at load, two per-request in that arm's own
wording). The two arms were always in the same condition; only the count was blind to it.

```
W2-B-sht-r1: kernel journal fingerprint is not clean: amdgpu 0000:09:00.0: ring page1 timeout
```
is what makes r1 a discarded boot in the first place — not a new finding, and not something the
gate invented.

## Validation

| Command | Result |
|---|---|
| `py_compile` `port_ab_ttft.py` `port_ab_legs.py` `port_ab_contract.py` | OK |
| `port_ab_ttft.py --selftest` | OK (parser, event pairing, inter-turn definition) |
| `port_ab_legs.py --selftest` | **32/32 ok** — 24 before, 8 new: unbalanced-n reported, balanced-n not reported, reworded LOW counted, fork/port counted equally, ttft attribution passes/fails/swaps |
| identity path exercised without a server | `engine_identity` on a live pid: exe, 64-hex sha, cached reuse, and the two reason strings (no pid / unreadable link) |
| `port_ab_ttft.main()` with the HTTP calls stubbed | record carries `engine_exe`, `engine_sha256` (64), `engine_identity_reason`, `mem.engine_pid` = the pid given |
| `port_ab_prepare.py --verify` | `verify: OK` |
| `out/port-ab-20261123` before vs after | **byte-identical** (489 files, sha256 per file) — the re-pin wrote no data |

The manifest was re-pinned (`--prepare`), as its `harness_script_shas` require when a harness
script changes. The manifest diff is exactly: the three script shas, `prepared_at`,
`git_head_at_prepare`, and the new `low_warning` text in each leg's `expected_engine_lines`.
No leg shape, cap, delta, budget, pin, window definition or interleave moved.

## Not done, deliberately

- No window re-run: the W2 records are the baseline and stay untouched.
- No boot, no leg, no GPU work, no `:8009` request; prod was serving and was not touched.
- The historical (pre-identity) ttft3 records are now FAILED, not retro-fixed: a record written
  by the old script cannot be given an engine it never recorded, and re-running the window is an
  operator decision.
