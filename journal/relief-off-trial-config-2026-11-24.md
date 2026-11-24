# The relief-OFF trial config — and why it cannot test the re-rank path

**Date:** 2026-11-24 · **Branch:** `recovery-startup-watchdog` · **Lane:** fixer, CPU-only
**Prod:** DOWN throughout (a failed boot, per the caller). Nothing booted, no engine started, no GPU
touched, no `:8009` traffic, no process control, no push.
**Writes:** `prod-config-port0218-norelief.json`, `tools/derive_norelief_config.py`,
`tools/check_config_argv.py`, this journal.
**Source of truth:** the prod binary `run/port0218-nohuge-bin/rpcw-hip/strata` is built from the
`port-0218` worktree, so `/mnt/srcds/Strata-wt-0218/engine/src` is the tree whose line numbers this
record cites (the same file exists in this checkout with different numbering; the validation text is
identical in both). The failure the caller hit is in `logs/engine-prod-port0218.log:3433`.

---

## 1. Verdict before the detail

| Item | Result |
|---|---|
| Config written and statically accepted | **yes** — `prod-config-port0218-norelief.json`, 69 args (was 75), 0 unrecognised flags, all 6 relief validators PASS |
| Minimal consistent "relief off" set | **remove 4 flags, add nothing** |
| Does relief-off remove `release_pair`/`refill_pair`? | **It was never reached in this tuple, before or after.** Relief-off changes nothing here |
| Is the experiment worth a soak? | **No.** It cannot distinguish the two arms — both are already the "no unregister" arm |

---

## 2. The exact dependency rule

Six named refusals, `generate.cpp` in `Strata-wt-0218/engine`. Each returns **2** (argv refusal).

| # | Line | Rule | Message subject |
|---|---|---|---|
| 1 | 5873 | `relief` **requires** `--expert-helper-socket` | "the out-of-process expert helper is not configured" |
| 2 | 5882 | `packs` **requires** `relief` | "the packs lever extends the relief's drop set" |
| 3 | 5889 | `hot-mib != 7000` **requires** `packs` | "the hot/keep split tunes the packs lever's stage drop" |
| 4 | 5897 | `arena-cap-mib > 0` **requires** `relief` | "the cap drops the arena's coldest host copies" |
| 5 | 5903 | `arena-cap-decode-mib > 0` **requires** `arena-cap-mib > 0` | "it decides which pairs the cap may not drop" |
| 6 | 5910 | `arena-cap-rerank` **requires** `arena-cap-mib > 0` | "there is nothing to re-rank" |

Dependency chain, as a graph:

```
--expert-helper-ram-relief  ──requires──>  --expert-helper-socket
        │
        ├──> --expert-helper-ram-relief-packs
        │            │
        │            └──> --expert-helper-relief-hot-mib N   (only if N != 7000)
        │
        ├──> --expert-helper-relief-arena-cap-mib N          (only if N > 0)
        │            ├──> --expert-helper-relief-arena-cap-decode-mib N   (only if N > 0)
        │            └──> --expert-helper-relief-arena-cap-rerank
        └── (gate #3 does NOT depend on rerank)
```

The caller's failure was **rule 3**: removing `relief` and `packs` left
`--expert-helper-relief-hot-mib 2048` in place, and `2048 != 7000` with no packs flag is a refusal.
Note the asymmetry that makes this easy to get wrong: `hot-mib` is a **defaulted** value (7000), so
leaving it at its default is legal, while setting it is not — the validator tests the *value*, not the
*flag's presence*.

**Minimal consistent set for "relief completely off"** = remove these four, add nothing:

| Flag | Value | Why it must go |
|---|---|---|
| `--expert-helper-ram-relief` | (bare) | the switch itself |
| `--expert-helper-ram-relief-packs` | (bare) | rule 2 |
| `--expert-helper-relief-hot-mib` | `2048` | rule 3 (non-default without packs) |
| `--expert-helper-relief-arena-cap-mib` | `4096` | rule 4 (positive without relief) |

`--expert-helper-relief-arena-cap-decode-mib` and `--expert-helper-relief-arena-cap-rerank` are
**absent from the promoted tuple already** (verified in both configs), so nothing to remove.

---

## 3. What else moves with relief — the answer is "nothing you set"

| Candidate | Finding | Cite |
|---|---|---|
| Budget / slot count | no coupling; `--expert-helper-slots 4600` and `expert_helper_slots: 4600` are the helper's fill cap, independent of the relief | `expert_helper_slots` in both configs; `generate.cpp:11050-11053` (plan_fill) |
| Helper argv | unchanged; the launcher's helper line carries no relief flag | `launch-prod-...-mixed.sh:120-123` |
| Env keys | none relief-specific. `STRATA_WINDOW_POPULATE` (the only other consumer of `refill_recorded_pairs`) is **unset** in both | verified in both configs; `generate.cpp:26756` |
| `STRATA_PROMPT_STAGE_MEMO` | stays `1`; relief only *forces* it on (`stage_memo.force`), it is already opted in | `prefill.cpp:4625` |
| Arena cap | goes with the relief — but the cap is only reachable with a layer split and the packs fds live (gate at `generate.cpp:11200`) |
| `STRATA_RAM_BUDGET_MIB` | unchanged, and this is the key: it is what makes relief inert (§6) | `generate.cpp:9437` |

---

## 4. The delta proof

Recursive diff of the two configs, canonicalised with `json.tool --sort-keys`. Everything outside the
delta is byte-identical: `env` (30 keys), `exe`, `port`, `cwd`, `lib_dirs`, `layer_split`,
`expert_helper_socket(2)`, `expert_helper_timeout`, `sampling`, `vision`, `managed_budget_mib`,
`trial_note`, `model_name`, `tokenizer`, `pack`, `log`.

```diff
@@ args @@
         "--expert-helper-slots",
         "4600",
-        "--expert-helper-ram-relief",
         "--conversation-cache-dir",
@@
         "--conversation-cache-evict-page-cache",
-        "--expert-helper-ram-relief-packs",
-        "--expert-helper-relief-hot-mib",
-        "2048",
-        "--expert-helper-relief-arena-cap-mib",
-        "4096",
         "--conversation-spill-mib",
         "0"
@@ metadata @@
-    "derived_from": "prod-config-port0218.json",
-    "derived_note": "PROMOTED 2026-11-24 (commit c511059c5c): ...
+    "derived_from": "prod-config-port0218-nohuge.json",
+    "derived_note": "Trial 2026-11-24. NOT PROMOTED. ...
```

**Arg count: 75 → 69** (6 tokens removed: 2 bare flags + 2 flag/value pairs). The only non-`args`
changes are the two provenance fields, which `derived_from`/`derived_note` are for.

Reproduce: `python3 tools/derive_norelief_config.py --check` (asserts the file equals the derivation)
and the diff above.

---

## 5. Static acceptance

**There is no parse-only mode.** No `--dry-run`, `--print`, `--check`, `--validate` or
`STRATA_PARSE_ONLY` exists in the binary (`strings` grep for `^--(dry|print|check|validate|parse|describe)`
returns only `--check-logits`, an unrelated sampling flag). The only early exits are `--help`/`-h`
(`generate.cpp:4257`), `--version`/`-V`, `--families`, `--geometry`, `--expert-support`
(`generate.cpp:4121-4145`). None of them parses the rest of argv, so **none can validate this config**.

Proven statically instead, by two independent checks:

**(a) Every flag is recognised.** `tools/check_config_argv.py` greps the binary's string table for each
of the 34 distinct `--` tokens. Result: **0 unrecognised**. This matters because the engine treats an
unknown flag as fatal — `"unknown argument: %s"` then `usage(); return 2;` (`generate.cpp:4730-4732`).

**(b) Every relief validator, re-evaluated against the new values:**

| # | Check | New value | Verdict |
|---|---|---|---|
| 1 | relief needs a socket | `relief=false` | PASS (antecedent false) |
| 2 | packs needs relief | `packs=false` | PASS |
| 3 | hot-mib≠7000 needs packs | `hot=7000` (flag absent → default) | PASS |
| 4 | cap>0 needs relief | `cap=0` | PASS |
| 5 | decode>0 needs cap>0 | `decode=0` | PASS |
| 6 | rerank needs cap>0 | `rerank=false` | PASS |

All six antecedents are false, so all six pass vacuously with no removal left dangling. The remaining
argv is a strict subset of an argv that already booted successfully (the promoted tuple, per
`logs/engine-prod-port0218.log` boots at 19:45, 20:30, 20:44, 21:02, 22:56) and `--help`/`next` value
pairing is preserved (only whole flag/value pairs were removed).

**Where the validators sit:** all six are in `main()` (`generate.cpp:4089` on, block at
5873-5915), *after* argv parsing and *after* the memory-guard line (`generate.cpp:5155`) and the layer-split
line (`generate.cpp:5399`). The failed boot in the log shows exactly that order — `memory guard` →
`layer split across 2 GPUs` → the refusal at `logs/engine-prod-port0218.log:3433` — so a rejection is
decided before any model load. It is *not* free of GPU contact: `cudaMalloc`/device calls appear earlier in
`main()` (e.g. `generate.cpp:1044, 2409, 3313, 3603`), which is why this was proven statically rather than
by running it. Not booted here, per instruction.

---

## 6. The decisive finding: relief-off does not remove the unregister path — it was never on

This is the part the caller needs before a soak. Two independent structural facts, either sufficient.

### 6a. The call graph

`release_pair` and `refill_pair` have exactly **one** caller each, both inside
`reorder_arena_by_heat` (`expert_source.cpp:8043` and `:8050`):

```
release_pair()  <- expert_source.cpp:8043  (only caller)
refill_pair()   <- expert_source.cpp:8050  (only caller)  [and :7691 in refill_recorded_pairs]
                       ^
reorder_arena_by_heat()  <- generate.cpp:26773  (only caller)
                       ^
   gated by: o.expert_helper_relief_arena_cap_rerank && ... && srcp == &arena_src
```

`--expert-helper-relief-arena-cap-rerank` **is not in the promoted tuple** and not in the new one.
Its arming call `set_arena_reorder` (`generate.cpp:11258`) is behind the same flag plus a non-empty
`relief_cap_tier`. So `reorder_arena_by_heat` is never invoked, so `release_pair` is never invoked —
**in the currently-served config**. The journal that diagnosed this fault already counted it:
`grep -c 'cap re-rank'` = **0** in the faulting elogs
(`journal/amdgpu-userptr-fault-diagnosis-2026-11-24.md`, "competing candidates" table).

`refill_pair` additionally self-refuses at `expert_source.cpp:7717` — `if (base_ == nullptr ||
!ram_relief_ || !ranges_ || arena_ == nullptr) return false;` — so it too is dead without the relief.

### 6b. The arena is never constructed in this tuple at all

`STRATA_RAM_BUDGET_MIB=36864` is set, so `ram_budget_managed` becomes true (`generate.cpp:9303`) and
the expert-source branch is taken at `generate.cpp:9437`:

```
9437:  if (o.mmap_experts || ram_budget_managed) {   <-- opens, closes at 9552
9550:      srcp = ram_budget_managed ? &*managed_adapter : &src;
9552:  } else {
9553:      arena_src.set_gguf(...)                     <-- the arena path
9666:      if (o.expert_helper_ram_relief) arena_src.set_ram_relief(true);
9669:      const bool arena_ok = arena_src.open(...)
9770:      srcp = &arena_src;
```

I verified this with a brace-depth walk, not by eye: branch 9437 closes at 9552, and lines 9666/9669/9770
are **outside** it (in the `else`). So `ArenaExpertSource::open` — and with it `set_unfilled` and
`register_complement`, the boot-time drop and registration — never runs. `srcp` is the
`PoolExpertSourceAdapter`.

The engine says so itself, 21 times in the prod log:

> `strata generate: --expert-helper-ram-relief: nothing dropped: this run's expert source is not the
> arena (--mmap-experts / --resident-experts read the files and keep no arena copy)`
> (`generate.cpp:11089-11091`; `logs/engine-prod-port0218.log:2383` and 20 more)

So the relief has been a **no-op on every prod boot since it was set**. It does not drop a pair, does
not build a complement, does not unregister anything.

### 6c. The pool VMA carries no registration at all

`Pool::create` is `mmap(MAP_PRIVATE|MAP_ANONYMOUS)` → optional `MADV_NOHUGEPAGE` → `mlock` in chunks
(`ram_pool.cpp:189-268`). `grep -n 'cudaHostRegister\|cudaHostUnregister' ` over
`pool_expert_store.cpp`, `pool_expert_cache.cpp`, `pool_expert_source_adapter.cpp`, `ram_pool.cpp`
returns **nothing**. The 32 GiB VMA that faulted has never had a GPU translation to revoke from this
path.

**Conclusion: the relief/re-rank unregister+re-register path cannot be the cause of the fault, and
turning relief off cannot test it.** Both arms of the proposed experiment are the same arm.

---

## 7. What the alternative mechanism is (for whoever picks this up)

The pool's pages still reach the GPU: `cudaMemcpyAsync(..., cudaMemcpyHostToDevice)` from pool leases
(`generate.cpp:12785`, `:18368`, `:28637`, `:23648`). On AMD a pageable source ≥ 1 MiB is copied by
ROCclr **locking its pages in place as a GPU userptr and keeping them** — the exact mechanism
`generate.cpp:4160-4167` documents and works around by setting `GPU_PINNED_MIN_XFER_SIZE=1000000`.
Two facts make this the live candidate:

| Fact | Value | Source |
|---|---|---|
| blob size vs the threshold | `blob_max=2662400` B (2.54 MiB) — **above** 1,000,000 B | prod log, pool store line |
| is the workaround in force? | the launcher **unsets** it; `STRATA_ARENA_MMAP` is unset so the `setenv` at `:4167` does not fire; `xfer_value` only sets it for integrated gfx11, not gfx906 | `launch-prod-...-mixed.sh:135`; `kfd_quiet.hpp:254-257` |

So every pool→device adaptive swap of a 2.54 MiB blob can take the pin-in-place path, and the resulting
userptr is unmovable in a way the fault signature (`PERMISSION_FAULTS: 0x3`, `WALKER_ERROR: 0`,
`MAPPING_ERROR: 0`) is consistent with — the same class the earlier diagnosis could not separate.
**This is a hypothesis, not a measurement.** I ran nothing.

A cheaper next experiment than the relief A/B: set `GPU_PINNED_MIN_XFER_SIZE=1000000` in the trial's
`env` and re-run the cold prefill. That is one env key, zero args changed, and it targets the only
mechanism actually present in this tuple.

---

## 8. Files

| File | What |
|---|---|
| `prod-config-port0218-norelief.json` | the trial config, 69 args, `derived_from` = `prod-config-port0218-nohuge.json` |
| `tools/derive_norelief_config.py` | derives it from the promoted tuple; `--check` asserts the on-disk file matches |
| `tools/check_config_argv.py` | static argv validation: flag recognition against the binary + all 6 relief validators |

**Not done, deliberately:** no boot, no engine start, no GPU, no process control, no `:8009` traffic,
no push. The static acceptance in §5 is the strongest claim available without one, and §6 is why I
recommend against the soak rather than a caveat to it.
