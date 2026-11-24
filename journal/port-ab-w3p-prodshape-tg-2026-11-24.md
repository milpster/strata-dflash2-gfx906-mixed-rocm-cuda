# prod-shape TG leg (W3p): thinking ON, so generation runs past EOS to the cap — 2026-11-24

CPU only: no boot, no leg, no GPU, no `:8009` traffic, no process control, no push. Prod is down
for a measurement window and untouched by this. Every number below is from `port_ab_prepare`/
`port_ab_legs` output, from a file hash, or from arithmetic over the contract's measured anchors.

## The problem

The W3 `tg` leg asks for 1,024 tokens, but the thinking-off shape stops at EOS after ~518: the
harness tuple pins `reasoning_budget_tokens: 0` and the request template pins
`enable_thinking: false`. `gen_tok` therefore never clears `TG_CLAIM_MIN_GENERATED` (1,000), so
TG never became a reportable rate. The engine has no EOS suppression (`--min-tokens` is a
conversation-cache threshold, not a generation floor) and a rebuild is explicitly not wanted.

The fix is config only, and it reproduces what prod actually serves: the promoted
`prod-config-port0218-nohuge.json` carries `reasoning_budget_tokens: 2000` and
`reasoning_effort: "high"`. With thinking on the model works through a budget, so generation
runs to the cap instead of ending at EOS.

## What was added

| Site | Change |
|---|---|
| `contract.LEGS["pp239k-prodshape"]` | new leg: `kind: tg`, `corpus: pp239k`, `max_tokens: 1024` |
| `contract.WINDOWS["W3p"]` | new window: `pp` boot group, arm B only, 2 claim boots + 1 warmup, `config_variant`/`request_template` `prodshape`, `poolable_with_claim_legs: false` |
| `contract.PRODSHAPE_TEMPLATE` + `REQUEST_TEMPLATES` | the thinking-on template and the name -> file map (claim, prodshape) |
| `contract.window_arms` / `config_variant` / `request_template` | the window owns its arms, config variant and template |
| `contract.claim_pairs` | filters the frozen interleave to the window's arms; reps never renumber |
| `configs.VARIANTS["prodshape"]` | `{reasoning_budget_tokens: 2000, reasoning_effort: "high"}` |
| `configs.derive(variant=)` | applies the overlay as `variant` deltas; the window resolves its own variant |
| `configs.check_tuple(variant=)` | a variant config must prove the variant keys; a claim config must still prove the tuple's (a leaked 2,000 fails) |
| `configs.expected_identity` | one reader for both the writer and the checker |
| `legs.template_path(out, window)` | per-window template; default stays the claim one |
| `legs.check` | `prodshape` column, `TG=tg*` label, NOT-poolable NOTE, and a hard FAIL when a prod-shape leg falls under the floor |
| `prepare.prepare_templates` | writes and pins both templates, each at its own path and sha |
| `prepare.verify` | fails on a missing/mismatched variant template, a variant config with a claim budget, a claim config with variant deltas, or the two templates sharing a sha |
| `tests/test_port_ab_prodshape_cpu.py` | 16 CPU tests over the window, the config delta, the template and the command |
| `legs --selftest` | 2 new cases: prod shape clears the floor -> `tg*` + NOT-poolable; 518 generated -> FAIL |

`out/request-template-prodshape.json` is a frozen artifact, `sha256
579df15686219567cb05f44012eeab769376925e39ccd72751567173b5907680`. The claim template keeps
`1367e0c041b3e7e6abceef27587f9ffbb8ff1d8b2ea4cc7d0141d9e945a18abd`, byte-identical.

## The config delta vs the current arm B claim config

`configs/W3p-B-pp-r1.json` against `configs/W3-B-pp-r1.json`, with the harness-owned paths
blanked (`port_ab_configs.settings_only` + `blank_declared`) and `trial_note` dropped (it
carries the leg key): **24 non-harness keys compared, 1 differs.**

| Key | W3-B-pp-r1 (claim) | W3p-B-pp-r1 (prod shape) |
|---|---|---|
| `reasoning_budget_tokens` | `0` | `2000` |

Proven identical: engine `args` (75 flag/value tokens, harness paths blanked), `env` (27 vars,
including `STRATA_POOL_NOHUGEPAGE=1`), `exe`, `cwd`, `port`, `model_name`, `reasoning_effort`
(`"high"` in both — the variant restates the tuple value rather than changing it),
`layer_split`, `expert_helper_slots`. `reasoning_effort` correctly produces no delta because
nothing moved; `check_tuple` still proves the value.

## Boot + run for one prod-shape TG leg

```sh
bench/ab-v0210/rpcprof/port_ab_boot.sh W3p B pp 1
python3 bench/ab-v0210/rpcprof/port_ab_legs.py --window W3p --arm B --boot pp --rep 1 \
  --shape pp239k-prodshape --run
bench/ab-v0210/rpcprof/port_ab_stop.sh W3p B pp 1
```

`--runbook W3p` prints the whole 3-boot window (1 discarded warmup + 2 claim boots). The leg
command it generates pins everything before the wire:

```
python3 .../ab9_driver.py --arm B --leg pp239k-prodshape --kind tg --ctx 323200 \
  --max-tokens 1024 --url http://127.0.0.1:8009/v1/chat/completions \
  --prompt-file .../corpus/pp239k.ctx323200.bin \
  --prompt-sha256 210b16b32b8658e68b863442d3c2d9ebde7eb3c9d50b0cea8c28e7d045c0ea68 \
  --request-template .../request-template-prodshape.json \
  --request-template-sha256 579df15686219567cb05f44012eeab769376925e39ccd72751567173b5907680 \
  --timeout 5400 --champs-shape pp239k-prodshape --champs-pair 0 --champs-measured
```

`port_ab_boot_plan.py --plan` resolves the frozen config pin before boot and refuses a config
that left the manifest (`W3p-B-pp-r1` pin `9614f9f55eacb82b…`, engine `15d330ea…`, helper
`58c3ae02…`).

## What did NOT move

`--prepare` then `--verify` prints `verify: OK`. Against `HEAD`'s manifest:

| Check | Result |
|---|---|
| 42 existing config pins (path + sha256) | **none changed**, none missing |
| arm A/B engine, base-config and server-tree shas | **identical** |
| claim request template sha | **identical** (`1367e0c0…`) |
| corpus shas, `frozen_tuple`, `CLAIM` block | **identical** |
| W1/W1d/W1f/W2/W3/W4 `budget_s`, `contract_budget_s`, `interleave`, `boot_groups`, `legs_per_boot`, `warmup_boots`, `delta`, `gate_metric` | **identical** |
| `--runbook W1`/`W2`/`W3`/`W1f` | **byte-identical** to `HEAD` |
| existing leg sections | additive keys only (`config_variant`, `request_template`, `request_template_sha256`, `request_settings`) |
| new configs | the 3 `W3p-*` boots only |

## Validation

| Command | Result |
|---|---|
| `port_ab_prepare.py --prepare` | prepared; W3p `[0.8, 1.02] h`, boots `{claim: 2, warmup: 1, total: 3}` |
| `port_ab_prepare.py --verify` | `verify: OK` |
| `port_ab_legs.py --selftest` | **24/24 ok** (2 new prod-shape cases) |
| `port_ab_legs.py --runbook W3p` | 3 boots, arm B only, CONFIG VARIANT banner, "never pool with a claim leg" |
| `port_ab_boot_plan.py --plan --window W3p` | resolves; config sha matches the manifest pin |
| `port_ab_legs.py --window W3p --arm B --shape pp239k-prodshape` | generates the tg/1024/prod-template command |
| `unittest discover` (port_ab_prodshape_cpu) | **16/16 OK** |
| `unittest discover` (whole rpcprof tree) | 251 tests, **5 failures — all `test_reserve_panel_cpu`** |
| those 5, with my 4 files stashed | **the same 5 fail** → pre-existing, not mine |
| `py_compile` on the 5 harness modules; `bash -n` on boot/stop | OK |

The 5 reserve-panel failures are the frozen `prod-config-v0238.json` profile drifting when
production moved to `port0218-nohuge` (`restore pin config_sha256 is 316e6128…, not b221140f…`).
They touch neither the port-ab harness nor the W3p path.

## The measurement caveat, stated plainly

**This TG is measured with thinking ON.** It is comparable to prod's real serving shape and to
nothing else in this harness. It is **NOT poolable with the thinking-off prefill legs**
(W1/W1d/W1f/W3): those generate under `enable_thinking: false` and `reasoning_budget_tokens: 0`,
and this leg generates under `enable_thinking: true` with a 2,000-token budget. The tokens
generated are a different quantity, the model's behaviour is a different behaviour, and a
tok/s figure from one says nothing about the other.

What it does settle: the earlier defect. A leg that must clear the 1,000-generated floor can do
so on this shape, because the budget keeps generation running to the cap instead of ending at
EOS. The `--check` table marks those rows `tg*` (not `tg`) and prints the NOT-poolable NOTE, and
a prod-shape leg that still stops short is a hard FAIL — it is the failure the leg exists to fix,
never excused by the shape it ran on.

`delta_key` is `None`: there is no thinking-off counterpart to move against, so W3p supports no
A/B statement at all. Any comparison is prod-shape against prod-shape across separate boots.

## Not done, deliberately

- No boot, no leg, no measurement: prod is down for the operator's own window and this lane is
  CPU-only. The leg is prepared, pinned and runnable; firing it is the parent's call.
- No engine rebuild: the operator ruled it out and the config route needs none.
- No EOS suppression: the engine has no generation floor flag, and inventing one was out of scope.
- Arm A is not booted for W3p: it runs a different server tree, so an A leg would answer a
  different question and burn a boot on a shape nobody serves.
- No writes under `out/port-ab-20261123/` beyond `--prepare`'s own artifacts: the 3 new configs,
  the new template and the manifest are the only additions; the existing files are byte-identical.
