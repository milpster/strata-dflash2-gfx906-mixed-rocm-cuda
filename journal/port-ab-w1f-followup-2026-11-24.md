# W1f: the claim-capable follow-up window for the re-pinned arm B — 2026-11-24

CPU only: no boot, no leg, no GPU, no `/dev/kfd`, no process control, no `:8009` traffic, no push.
The parent session holds the GPU; every number below is from `port_ab_prepare.py`/`port_ab_legs.py`
output or a file hash.

## Why a new window and not a W1 re-run

Arm B was re-pinned to the promoted pool-MADV_NOHUGEPAGE engine `15d330ea` (`e39905a674`). W1's four
claim records were measured on the OLD pin and are the pre-amendment baseline, so they must not be
re-derived or overwritten. The contract's claim rule also needs a FOLLOW-UP at n>=3 per arm
(`port_ab_contract.CLAIM`): 2v2 gives C(4,2)=6 allocations and a minimum exact two-sided p = 0.33
(descriptive only); 3v3 gives C(6,3)=20 allocations and a minimum exact p = 0.10. `W1f` is that
n=3 follow-up and the only prepared window `CLAIM["level"]` applies to.

## What W1f is

| Field | Value |
|---|---|
| window id | `W1f` (own boot keys `W1f-*`, own config paths, own cache dirs/sockets/logs) |
| margins | `explicit` — both reserves from TUPLE (256/192), as W1 |
| boot group / legs | `{"pp": ["pp239k"]}`, 1 leg per boot, max_tokens 8 (prefill, TG=ctx) |
| n | `boots_per_arm: 3`, `warmup_boots: 2` (one discarded first-touch boot per arm) |
| order | A1 B1 B2 A2 A3 B3 (ABBA continued; `contract.claim_pairs`) |
| gate / delta | `read_tps`, 0.05 (W1's pre-registered prefill delta) |
| planning budget | 1.93-3.22 h derived from W1's anchors (8 boots, warmups included) |
| contract budget | 3.0-5.0 h = W1's 5,400-9,000 s per-boot rate x 8 boots (6 claim = 1.5x W1's claim count + 2 warmup) |

## Consumers that had to learn about it

| Site | Before | Now |
|---|---|---|
| `port_ab_legs.schedule` | `C.INTERLEAVE_PAIRS` for every window | `C.claim_pairs(window)`: n=2 windows byte-identical, W1f gets A B B A A B |
| `contract.LEGS["pp239k"].{kind_by_window,max_tokens}` | W1/W1d/W3 only; `gen_tokens` fell through to `entry["W2"]` | `W1f: prefill, 8` added (unlisted window raised KeyError, it did not silently default) |
| `port_ab_prepare.DELTA_KEY` | `pp239k: {W1, W3}` | `W1f: prefill_read_tps` added |
| `port_ab_prepare` windows block | `spec["claim"]` default = "descriptive only at n=2/arm" | W1f carries its own claim-capable text |
| runbook contract line | hardcoded "claim boots only" | window's `contract_budget_note` (W1f: "8 boots (6 claim + 2 warmup)") |
| `port_ab_legs --window` / `port_ab_configs --window` | `sorted(C.WINDOWS)` | automatic |
| `port_ab_legs --runbook/--check` window validation | `value not in C.WINDOWS` | automatic |
| `port_ab_legs.parse_boot_key` | `^(W\d[a-z]?)-` | already matches `W1f` (no edit) |
| `port_ab_boot.sh` / `port_ab_stop.sh` | no window whitelist (boot key is built from argv) | comments name W1f; boot proceeds (no whitelist to update) |
| `port_ab_boot_plan.py` margins gate | comment named W1d only | comment says explicit windows carry both reserves; code is window-driven |

Silent failure that would have happened: at HEAD, `schedule()` used the module-wide
`INTERLEAVE_PAIRS`, so a W1f window with `boots_per_arm: 3` would have run **4** claim boots
(A1 B1 B2 A2) while the manifest says 3 per arm — an n=2 window dressed as the n=3 follow-up, with
no error. `C.gen_tokens("pp239k","W1f")` raised `KeyError: 'W2'` on the way (loud, not silent).

## Validation (CPU only)

| Check | Command | Result |
|---|---|---|
| syntax | `py_compile` (4 modules), `bash -n` (boot/stop) | 0 |
| prepare | `port_ab_prepare.py --prepare` | 0; W1f 1.93-3.22 h, 8 boots (6 claim + 2 warmup) |
| verify | `port_ab_prepare.py --verify` | `verify: OK` |
| runbook W1f | `port_ab_legs.py --runbook W1f` | 8 boots: 2 warmups r0 + A1 B1 B2 A2 A3 B3 |
| runbook W1..W4 unchanged | `--runbook` before (HEAD copy) vs after | all five byte-identical; W1 still 6 boots |
| W1 pins unmoved | manifest `configs` shas for `W1-*`, HEAD manifest vs new | identical (6/6); W1d/W2/W3/W4 and corpus shas also identical |
| prepare is additive | sha256 of every file under `out/port-ab-20261123` (cache by size+mtime) before/after | only the 8 new `configs/W1f-*.json`; no existing file changed |
| W1f boot plan | `port_ab_boot_plan.build` for A-r1, B-r1, A-r3 | config pin, engine/helper sha and tuple all pass |

Not run, on purpose: no `--run` leg, no boot, no prereq-0 window gate (needs the box quiet), no
`--check W1f` (there are no W1f records yet: it prints "no records ... for W1f", exit 2).
The 8 new config files are left untracked, as W1d's are; `port_ab_boot_plan.py` recreates any
config a boot needs from the pinned sha.
