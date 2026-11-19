# RAM parking/loading — fixtures, manifests, assertions (source-backed contract, rev 2)

Status: CPU/read-only specialist deliverable. NOT permission to run the GPU window, alter production, or promote. Parent reconciles and owns all GPU execution and the exact calibration constants. Complements `ram-parking-gpu-test-plan.md`; does not replace it. Contract, not evidence — preparation is never a pass.

This rev supersedes the earlier draft's points 1-8 after parent's source reconciliation. It deliberately separates **accepted reusable primitives**, **missing code (real gaps)**, and **genuinely GPU-dependent calibration**. It is NOT "everything runs deterministic after one check"; the still-open bridge and restart expectations are flagged in §6.

Candidate `af1e64a5…` = built SHA `8f19baaf150237e5d8085c12c842a4b5728d10bd00ce315ab0ed52ff9bd819ed` (`/mnt/srcds/Strata-wt-integ/build-mixed-gpu/rpcw-hip/strata`, already built; no rebuild, no re-run of the six approved CPU checks — parent holds: 5446 checks pass). Baseline `42cb41b314` / `b0ec0109ebc50c982512c371d81b2c15a8f72c70edf8a461a4ff10337da52f02`. C++ line numbers are HEAD `af1e64a5`; verify any *reason-string* claim against baseline source before calling it comparable (see §3).

Canonical serving tuple to preserve: `prod-config-v0238.json`, `--spec 5`, `--spec-min-p .5`, DFlash block 7, `--max-context 262144`, `--conversation-cache-mib 4096`, `--conversation-cache-disk-mib 24576`, `--conversation-cache-ram-mib 512`, `--conversation-cache-min-free-mib 2560`, `--kv-resident 262144 --kv-resident-mib 4096`, wrapper-added helper socket fields + `layer_split 30`.

## Parent reconciliation — research notes accepted, runnable gate not accepted

Parent read this revision and independently checked the disk `best()` implementation: it requires a full matching prefix strictly shorter than the request, with the deepest generation winning and equal-depth ties following use order. The reusable vocabulary and the earlier corrections are useful. They do not resolve these acceptance blockers:

- No relaxation of exact token-ID/state checks is approved. The transport **choice** is NOT open: exact raw token-ID/state equality is **required** and HTTP text (even with log state-hash/tokens) is **not** equivalent. The faithful raw-transport **lifecycle adapter** (`gpu-rounds/ram_raw.py`, single settings + ownership authority; the outer-owner launcher `ram_raw_launch.sh` is a THIN launcher that only normalizes env and `exec`s the Python outer owner) is WIRED and CLI-integration-tested (`tests/test_ram_raw_cpu.py`, 53 tests, real `main` preflight/run/run --worker branches through fakes): exactly-one owned pair (helper→ready→scoped engine worker→raw `GEN`/`T`/`DONE`→engine.close in the worker `finally`→outer owner terminate/wait), read-only production-absence preflight (stop proof `exit 0` AND `ALL STOPPED` from the `<stop_proof>.code` sidecar + live-identity probes, strict pgrep rc semantics) before any lazy `serve.server` import, an EXACT owned-helper positive proof (realpath exe + start tick + both live sockets under out_dir; basename substring rejected) excluding only the owned pid + self/ancestors, an exact pre-construction this-arm argv check (cache-dir included; drift aborts with `ctor calls==0`), a `RealEngineHandle` whose observed argv/exe come from `/proc` (not the resolver list), and a bounded per-request engine stderr window, with the real factory using `engine_args`/`child_env`/`StrataEngine` as the source authority (wired but UNINVOKED in offline tests). The unscoped-outer-owner + `systemd-run --user --scope -p MemoryMin=48G` worker makes helper-outside-engine-scope a launch-order fact, so the earlier cgroup concern is NOT a blocker and no attach API is used. It is DEFAULT INACTIVE: import/prepare/preflight/help/refusals import no serving engine and spawn nothing. What remains for the parent's production-down window is only to EXECUTE one bounded pair with a fresh `--out` and verify live cgroup split + observed-argv reconciliation. The measured finite cap with physical bounds and the GPU restart/checkpoint fixtures remain open. No new endpoint and no second engine is authorized.
- Refusal/fallback pressure turns and restore-success controls need separate exact predicates. The proposed §1c list is not a valid blanket pass rule for every measured turn or for both binaries.
- The cap formula is a calibration mechanism, not a frozen finite ladder: remove the ellipsis, choose exact measured units/rungs and physical safety bounds, and account for a fresh boot per attempt before execution.
- The two divergent generations in §4c are not two simultaneously compatible alternatives for `DiskCache::best`. Inside-manifest checkpoints and generation selection are different checks. The approximate rewind expectation in step 5 is not an acceptance assertion; exact compatible published/checkpoint depths and a shallower-result rejection still need a concrete fixture.

The independently confirmed restore-checker defect is now repaired and parent-checked with 53 offline CPU tests; no live restore was used to test it. Future authorized restores must use a fresh `--out` directory. The raw-transport lifecycle adapter, pressure calibration and restart-fixture items above remain gated (the transport choice itself is settled: exact raw IDs/state required); accepting these research notes does not authorize a GPU run or promotion.

---

## 1. ≥20-turn pressure fixture — reuse the LOGIC, not soak's CLI/verify()

### 1a. What is NOT reusable as-is (corrected)
`conversation_cache_soak.py` is a **private-engine, cache-off-vs-cache-on A/B/A** gate, not a drop-in for the baseline-vs-fix RAM arms and **not** usable with its `verify()` unchanged:
- `verify()` hard-requires `requested_cycles >= 30` (`:27`), `max(prompt_tokens) > candidate['kv_resident']` (`:36`, streamed-KV coverage), and `max(prompt_tokens) >= .9 * candidate['context']` (`:37`, near-context-limit). Under canonical `--max-context 262144` (`.9` ≈ 236k) the proposed mid-length donors (≈2048/8000/16000) satisfy **none** of `:36`/`:37`, and `:36` compares against the *actual* resident length, not the `--kv-resident 262144` token flag (effective residency must be **evidenced** from the engine, never assumed from the flag). Do NOT lower context/residency to satisfy the old soak gate.
- Near-limit / streamed-KV coverage is **not required** for this bounded RAM gate; do NOT silently add that unrelated expensive demand. It also forces a third long context each run.
- `verify()`'s arms are `conversation_cache_mib == 0` vs `> 0` (`:33-34`) — a within-binary miss/reference, NOT the baseline-vs-fix comparison. Do not relabel it.

### 1b. Accepted reusable primitives (copy the logic into one RAM script; do not fork a framework)
- `answer_text(text)` (`conversation_cache_soak.py:20`) — known-answer normalization; its text-equality predicate (`:42,:47` shape).
- The deterministic secret-bearing prompt builder currently **nested inside `main()`** (`soak:89-99`, `long_prompt`). It is **not importable**. Glue = lift it to a module-level pure function (deterministic, per-conversation-unique so parks are distinct), or re-implement that deterministic builder in the RAM script. No other tool exports it.
- Occupancy regex `parked=\d+ bytes=(\d+)` (`soak:125`).
- `state_hashes`/`STATE_KEYS` (`conversation_cache_parity.py:19,35`) — but ONLY the log-parsing of the `STATE_HASH` line (§1d); do not import the whole parity tool (see §2 for its flag pollution).
- Scenario **shapes** (not their runner/lifecycle): `conversation_disk_cache_check.py:s4_rewind` (`:260-282`) change-of-tail checkpoint-selection; `:s5_restart` (`:285-301`) park→stop→start→continue. Drive their lifecycle via the champions verified boot/stop/restore, never their `run/helper.json` self-managed lifecycle (`:115-138`).

### 1c. The NEW RAM-acceptance-specific predicate (this is new code, stated explicitly)
For each declared **measured** turn (≥20; this count is the RAM fixture's own declared turns, not soak's 30-cycle gate), assert:
1. known-answer **text** equality via `answer_text` (HTTP path — see §1d);
2. **STATE_HASH equality** to the warm reference, read from the engine log (`state_hashes`), `STRATA_STATE_HASH=1`;
3. **reused-from-log** ≥ the specific published boundary the turn is expected to hit (NOT soak's blanket `reused >= prompt_tokens`);
4. parked bytes ≤ the run's declared byte budget;
5. the **engagement** lines for that request window (§3) are present, so engagement is *asserted*, never inferred from turn count.

It explicitly does **NOT** require: 30 cycles, 3 context lengths, streamed/near-limit KV, byte-equal token-ID sequences, or a cache-off baseline side.

### 1d. Transport — REAL gap, smallest source-supported bridge
- Soak/parity/restore_speed drive the **raw** engine `serve.server.StrataEngine` (`server.py:1193`): `.generate(ids, max_new, sampling, cancel)`→token IDs, `.last` (reused/read/generated/prompt_tokens), `.info`, `.max_context`, `.proc.pid` (e.g. `restore_speed:142`). That is the embedded engine, NOT the HTTP serving loop.
- The RAM acceptance boots the **HTTP wrapper** via the champions lifecycle (server.py main + supervisor). Over `/v1/chat/completions` you get **text only** — no `.last`, no `.info`, no token IDs. So **exact next-token-ID-sequence parity and raw `.info`/`.state` are NOT available on the HTTP path.** Do NOT claim a thin HTTP wrapper exposes them, do NOT spawn a second engine beside the booted wrapper, and do NOT imply any existing CLI takes `--server`/`--attach` (no source support for that).
- Bridge actually supported by source, on the HTTP path: `STATE_HASH` is written to the **engine log** under `STRATA_STATE_HASH=1` (parse via `state_hashes`); reused/read are in the log line `prompt N tokens = R reused + K read` (`champions_log.py:parse_window`); known-answer via response text. So HTTP gives text + state-hash (log) + reused/read (log) — enough for predicates 1-3 without raw IDs.
- **The remaining implementation gap (not an open choice):** exact token-ID/state parity needs the raw `StrataEngine` path, and a bare `StrataEngine(cfg['args'])` (`restore_speed:142`) omits the wrapper's top-level helper socket fields + `layer_split 30`, so it is NOT the mixed-GPU production tuple (that wiring lives only in the champions wrapper). The resolution is to add the raw-path production-tuple wiring as explicit, named glue (a faithful lifecycle adapter); HTTP text+state-hash+log-token is NOT an equivalent substitute and is not an accepted fallback. This is gated until the adapter exists — do not paper over it, and do not spawn a second engine.

---

## 2. Matched baseline/fix manifest + a defensible cap ladder

### 2a. One canonical base; only `exe` differs
Base = live `prod-config-v0238.json` serving tuple (§0), identical on both arms except `exe`: baseline `b0ec0109…`, fix `8f19baaf…` (rehash per plan). Per-arm fresh `--conversation-cache-dir`, distinct port/sock/log under `out/ram-parking-<ts>/arms/<arm>/`; corpora generated by the deterministic builder (§1b) so both arms see byte-identical text. Reuse `champions_prepare.py` primitives (`sha256_path`, `retarget_config`, `retarget_launcher`, `write_slot`) but NOT its frozen `prepare/verify` (they pin the original champion knobs).

### 2b. Do NOT import helper flag builders (they change the serving tuple)
- `conversation_cache_parity.py:engine_args:45-50` appends `--adapt-swaps 0`, `--spec max(2,spec)`, `--mtp-max-t spec`, `--spec-min-p 0`, plus prompt-cache/slots/suffix; `conversation_restore_speed.py:135` forces `--adapt-swaps 0` + `--prompt-cache 6`. These are NOT unchanged-quality / unchanged-execution builders (canonical is `--spec 5`, `--spec-min-p .5`, DFlash 7). Private `cfg['args']` also omits the wrapper's helper socket + `layer_split 30`.
- Keep canonical model/precision/context/spec/actual serving env intact. Only explicitly declared matched TEST knobs differ (cache-mib / disk-mib / ram-mib / min-free / per-arm paths). Any `--prompt-cache` / cache-count delta must be **named and equal across arms**; no hidden helper defaults.
- Assert the floor explicitly: `conversation_cache_min_free_mib == 2560` read back from engine info (pattern `conversation_cache_parity.py:145-146`) — `champions_prepare.py:quality_keys` does NOT watch it.
- No concessions: precision/`--kv`, quantization, `--max-context`/`--kv-resident`, `--spec`/numerical tolerance identical. Do not raise the 2560 floor.

### 2c. Cap ladder — relative to observed usage (absolute caps were wrong)
Header `conversation_memory.hpp:18-24`: the operator cap term is **`cap_mib − usage_now`**, where `usage_now` = top-visible cgroup `memory.current` (or `MemTotal − MemAvailable` when cgroup unreadable), **less** a credit for that group's clean inactive file cache. It is NOT a budget solely for snapshot copies. Arbitrary 12000/9000/7000/6000 MiB caps on a tens-of-GiB loaded model/host are not a route to the narrow positive band — they can just zero every reading and stop donors forming. Replace with a formula-relative ladder (MiB):
- `U` = the **observed** cap-accounted usage the engine itself reports (from its boot/refusal `ram_note`).
  Correction after parent reconciliation: the boot guard (`generate.cpp` boot guard) already prints
  limit/current/credit/available/MemAvailable at **0.1 GiB** precision plus the floor at integer MiB,
  but those fields are **quantized**; `ram_note` exposes only `available`/source, and a rounded
  `current − credit` / `available` is **NOT** an exact `U`. `U` must come from a finite calibration
  boot, not from rounded logs. Any earlier ellipsised rung ladder or a later floor-raising proposal is
  **not accepted / not current execution**; the config floor stays **2560 MiB** throughout.
- `floor` = 2560. `band` = the reported restore-headroom band (boot band line). `ask` = measured `snapshot_bytes` of the spender that must be admitted.
- Rungs (fixed, ≤5): `cap = U + floor + ask + {band, band/2, band/4, 0, ...}` and a below-band edge `cap = U + floor + band/2` to push `available` under `floor+band` so the guard engages. The rung whose log shows the first engagement line (§3) is the frozen calibration cap. A rung reporting `available == 0` (no donors form) is a failed rung; if no rung engages within the fixed set → **UNRESOLVED**, stop. No open-ended tuning.
- `set_memory_limit_bytes` is **startup-only** (called once at arg parse, `generate.cpp:4668`; `set_restore_headroom_bytes` also startup, `:14623`; no runtime setter). So **each rung needs a NEW BOOT**. Do NOT claim one short boot sweeps multiple startup caps — there is no source-supported runtime control to do so.
- The exact Δ set, the host `MemAvailable` safety floor, and the swap-used bound are **parent-owned** (depend on the live footprint). This section is the mechanism, not a pre-proven cap.
- **Byte-budget pressure ≠ restore-headroom engagement.** `--scenario pressure` / `pressure_budget_hint` force byte-tier eviction with `reused == 0` (`conversation_cache_parity.py:82-84`, an explicit required **cache miss**) and `evictions > 0` (`:103`). Passing it proves byte eviction + fallback, NOT the restore guard. Keep the byte-pressure fixture and the 100%-cached restore fixture on **separate records**; never demand both on the same turns.

---

## 3. Refusals, reread tokens, engagement (keep totals comparable)

### 3a. Comparable by OPERATION, reason-independent
A fix spender's refusal can be classified `restore headroom` where baseline says `physical RAM admission`. Comparing only the literal `physical RAM` reason would **undercount candidate refusals**. Therefore:
- Comparable totals = counts by **operation** independent of reason: park refusals (any reason), checkpoint skips (any reason), restore refusals (any reason).
- Reason-specific counts are **diagnostics** only.
- Disk bookkeeping comparable: `disk park streamed N tokens ... generations= evicted= superseded= sidecars= anchor= pair= tail-draft= pages-released=` (`generate.cpp:14947`).
- Reread tokens comparable: `prompt N tokens = R reused + K read` via `champions_log.py:parse_window` (`EnginePass.reused/read`).
- **Verify on baseline source before labeling any category comparable.** Fix-only reason strings (`restore headroom`, `admitted after`, the boot band line) may be new; do not assert a hardcoded candidate line exists unchanged on baseline without checking `42cb41b314`. A fix-only line is engagement, never a comparable category.

### 3b. Fix-ONLY engagement (never a baseline-comparable skip count)
Present on `af1e64a5`, absent on baseline; a lower candidate count is NOT a comparison:
- boot band `restore headroom <B> MiB (<limiter> is the limiter; ...)` (band computed/set at `:14623`; terms `conversation_memory.hpp:85-98`);
- park skip reason `restore headroom` (`:15759-15760`, the `headroom_only` branch);
- checkpoint `checkpoint at N: its B MiB host copy admitted after <note>` (`:16190`) and skip reason `restore headroom` (`:16193-16194`);
- `disk tier RAM guard admitted|refused: <note>` (`:14322`).

### 3c. Don't double-count; aggregate ≠ donor
Scope every counter to one request's **byte window** (ab9 per-request `elog_start/elog_end`; disk_cache_check `log0 = len(engine_log())` then `new = engine_log()[log0:]`). Each refusal/park/evict line counts once, in the window of the request that caused it.
Aggregate reclaim `MemoryGuarantee.reclaimed` is **MEASURED** (sum of positive re-sample deltas, `conversation_memory.hpp:158-163`). Donor bookkeeping = `spent.entries`/`entry_bytes`; `advised_bytes` = FILE bytes advised (kernel may ignore). `ram_reclaim_note` (`:14282-14291`) already separates advised vs entries vs measured — reuse verbatim, never re-derive a per-round number from the aggregate.

### 3d. NO live no-progress inference (removed)
GPU aggregate telemetry establishes **only engagement + overall measured movement**. It CANNOT show "an advice round that moved zero followed by a donor round that moved" — the aggregate does not attribute movement per round. So on the live engine, report `admitted after`/`evicted … to admit` as **engagement only**; the per-round "zero-progress advice then donor progress" stays **NOT DIRECTLY OBSERVED**, regardless of positive `advised`/`measured`/`entries`. That invariant is established **only** by the CPU tests `conversation_memory_test.cpp:85-86` and `conversation_reclaim_test.cpp`. Add NO new runtime telemetry to "fix" this.

---

## 4. Non-vacuous deepest compatible PUBLISHED checkpoint restart (corrected)

### 4a. Authoritative selection + fields (disk tier, not the RAM analogy)
- Selection: `DiskCache::best(prompt, images, cvec)` at `include/strata/core/conversation_disk_cache.hpp:157-172` — deepest **published** generation whose `tokens` is a **full prefix** of the prompt (`std::equal(g.tokens…, prompt…)`), ties to the longer/later entry. (The earlier draft's `conversation_cache.hpp:329` RAM-cache analogy was wrong; cite the disk one.)
- Fields (`conversation_disk_cache.hpp:41-54`): `tokens` = generation's full token depth (what `best` matches); `anchor` = deepest checkpoint prefix; `pair_prefix` = deepest checkpoint prefix with a legal pair (0 = none); `tail_draft` = the generation's own ring horizon retains a resume at its end.
- `restored T = manifest.tokens.size()` (`generate.cpp:15025`) == the `disk park streamed N tokens` count (`src.ids.size()`, `:14950`). So **T = mounted generation depth, NOT the anchor**; `anchor ≤ T`. **Do NOT assert `T == anchor`.**
- Resume boundary at T (`:15110-15127`): engine prefers a valid **tail ring at T** (`tail_draft` horizon `.context_retained(T)`), else a **pair checkpoint at EXACTLY T** (`cm.prefix == T`, matching ring geometry), else **draft rebuild**. Classification (`:15521-15532`): `complete target+draft hit (tail ring)` / `(pair: checkpoint at T)` / `complete target hit (no drafter)` / `target-only prefix match, draft miss`. Compatibility gate = full-prefix match + `session_file_manifest_v2`/`session_file_read_v2` identity+geometry (`:15017`); mismatch/late-fail → `disk restore skipped (<reason>); rereading the prompt` (`:20523`).
- Keep these DISTINCT, never equate: mounted T (=manifest.tokens.size(), restored target tokens, == streamed N), publication anchor (`anchor=`), selected checkpoint prefix (`pair=` only when the tail ring is invalid), effective reused/read tokens (log `prompt N = R + K`), draft classification.

### 4b. Why "more `--prompt-cache`" does not create two generations
`retire_superseded` (`:14923`, header:210+) retires, at publish, the same-conversation generations a new one **outgrew** (their tokens a prefix). So a monotonically-advancing conversation keeps only its deepest generation; `--prompt-cache 6` grows the checkpoint chain **inside** one manifest, it does not leave older disk generations alive. A **changed-tail rewind** publishes a **divergent** generation (not a prefix-superset) → it survives alongside the pre-rewind one, but `best()` matches only the one that is a full prefix of the current prompt.

### 4c. Concrete source-supported two-boundary fixture
Boundaries exist (i) **inside one published manifest** — `anchor`, `pair_prefix`, shallower checkpoints, and the tail-ring/pair-at-T resume — and (ii) as a second **divergent** generation from a change-of-tail rewind.
1. Conversation C advances to depth N1, publishes G1; capture `disk park streamed N1 … anchor=a1 pair=p1 tail-draft=?` (with `--prompt-cache 6`, several checkpoints below anchor).
2. **Change-of-tail rewind** C, advance to divergent N2, publish G2; capture `disk park streamed N2 … anchor=a2 pair=p2 tail-draft=?`.
3. **Restart the same arm + cache dir** (s5 shape via champions lifecycle; `fresh_run_dir` must NOT archive the cache — keep pre/post artifact refs).
4. Continuation of the **rewound** C: assert `best` == G2 — `restored N2 tokens from disk`, log reused target == N2, classification equals the published one (`tail ring`@N2 or `pair: checkpoint at N2`), NOT `disk restore skipped`, and NOT G1 (G1's tokens are not a prefix of the post-rewind prompt → validation fail → reread, so reused would collapse far below N2). A shallow/wrong selection is thus **detectable** (reused != N2, or draft-miss when a tail-ring/pair was published).
5. The **rewind-resume** turn: assert reread covers only past the deepest compatible checkpoint (reused ≈ a compatible prefix, read ≈ the divergent tail), i.e., pair/anchor selection at the rewind prefix — not a full reread and not a wrong deeper generation. This exercises checkpoint selection (s4), not just the live image.
6. Pin EXPECTED depth from the captured `disk park streamed N` / manifest tokens, **never** from a hoped-for T. If `tail_draft`/gen-anchor did not fire, `pair`-at-T may legitimately be absent and a rebuild is correct — classify as observed; do not assume a pair.

### 4d. Payload-free inspection (no invented parser)
The engine's own disk index (`DiskGeneration`, tokens/anchor/pair_prefix/tail_draft, no payload) and `session_file_manifest_v2` (`:15017` — header/identities/manifest/region-dir/table-checksum precheck; payload verified only by the read sink's own pass) are the payload-free readers. There is **no standalone CLI or ready-made Python parser** for them; the Python-side surface is the engine log (`disk park streamed … anchor= pair= tail-draft=`) plus `/slots`,`/metrics`. Assert against those log lines; do NOT invent a Python on-disk parser.

---

## 5. Smallest CPU/offline validation contract + the stale-checker fix

- Offline CPU-only foundations are now implemented in `gpu-rounds/ram_acceptance.py` (matched test-config build/verify, compact diff/hash provenance, reason-independent operation totals, caller-declared turn classes, exact frozen restore expectations, and a guard that reuses `watch_gpu_residency.read_fdinfo_clients`) with isolated fixture tests in `tests/test_ram_acceptance_cpu.py`. They validate declared data only and are **NOT** a runnable GPU gate; the §6 runtime bridges and calibration remain open.
- **Parser/extractor/restart-predicate unit tests on canned log fixtures only**, in `tests/test_champions_cpu.py` isolation (`fresh_scratch:40`, `_NoNetwork:172`, outputs under `out/tests-tmp/`, never real evidence). Feed the exact strings: park skip both reasons (`:15759/:15760`), park evict (`:15769`), checkpoint evict/admitted-after/skip (`:16187/:16190/:16193`), disk park streamed-N/anchor/pair/tail-draft (`:14947`), restore classification tail-ring/pair/target-only (`:15521-15532`), restore-skipped (`:20523`), boot band. Assert the parser (i) totals refusals **per operation** independent of reason, (ii) keeps reason diagnostics, (iii) tags fix-only engagement strings separately, (iv) extracts streamed-N/anchor/pair/tail-draft, and the restart predicate **accepts** tail-ring@T or pair@T with reused==N and **rejects** `disk restore skipped`, a shallower reused, or target-only when a tail-ring/pair was published.
- **Reclaim/no-progress invariant**: cite the existing CPU tests (`conversation_memory_test.cpp:85-86`, `conversation_reclaim_test.cpp`); do NOT re-run the six approved CPU checks (parent holds the evidence).
- **Generation-aware restore checker (SPECIFY ONLY — do not edit/execute here).** The earlier "re-resolve AFTER the ready gate" was wrong: while bound to the **dead** generation's log the gate never passes (that is the 17:02:23 false timeout, evidence `out/game-exit-debug-20261118-163135-1526550/`). Correct, bounded requirement: (a) the **ready-wait LOOP itself** re-reads the launcher announce each poll to re-bind the supervisor's **current** engine log; (b) confirm `/proc/<pid>/exe` == expected SHA (as `champions_panel.py:engine_identity:229`, `boot:199-206`); (c) **re-verify identity/readiness AROUND the `/v1/models` + one real completion** so a concurrent generation change is not silently accepted. Not a HA framework. Also require an **output-root override + expected-SHA argument** so RAM runs write to their own `out/ram-parking-<ts>/` (never old champion measurements) and pin the SHA via the acceptance preflight, not a print.
- **Preserve evidence byte-for-byte**: no writes to `out/champions-20261118/`, `out/game-exit-debug-*`, `journal/*`, or frozen manifests.
- **Recorded limitation**: isolated synthetic cap-pressure exercises the real GPU-path spend/refusal/donor/restore code under a declared condition; it does **NOT** prove survival of OS direct-reclaim/swap collapse. A pass under a frozen cap is not host-collapse resilience.

---

## 6. Open items for parent (do not run an invalid gate)

| # | Item | Why it is open | Bounded next step (parent-owned) |
|---|---|---|---|
| 1 | **Transport lifecycle adapter** | Exact raw token-ID/`.info`/`.state` equality is REQUIRED; HTTP gives text+state-hash+log-token only, which is NOT equivalent. The CHOICE is settled (raw required). The faithful raw-path lifecycle ADAPTER is WIRED and fake-tested (`gpu-rounds/ram_raw.py` + `ram_raw_launch.sh`): single owned pair, `GEN`/`T`/`DONE` raw collection, read-only production-absence preflight (stop proof `exit 0` AND `ALL STOPPED` + live-identity probes), exact this-arm `observed==[exe,--serve,resolved]` argv check (cache-dir included), bounded per-request engine stderr window, engine.close in the worker `finally` before the outer owner terminate/wait. The unscoped-outer-owner + `systemd-run` scoped worker makes helper-outside-engine-scope a launch-order fact (NOT a cgroup blocker and NOT an attach API). DEFAULT INACTIVE: import/prepare/preflight/help/refusals import no server and spawn nothing; the real factory (`engine_args`/`child_env`/`StrataEngine`) is wired but UNINVOKED. | Parent, after all agents end: run one bounded pair in the production-down window (fresh `--out`), verifying live helper/engine cgroup split, the exact observed-argv reconciliation against the live resolver, and the per-request state/token capture. Do not relabel HTTP text as equivalent or spawn a second engine. |
| 2 | **Cap calibration** | The rung landing `available` inside `floor+band+ask` (with `usage_now` credit) is measurement-dependent; startup-only setter → one boot per rung. | ONE arm, ONE boot per rung; capture engine-reported effective `kv_resident`, `snapshot_bytes`, boot band, one `disk park streamed N anchor= pair= tail-draft=`; feed §2c formula; freeze the first engaging rung. Parent sets Δ set + MemAvailable/swap bounds. No multi-cap claim from one boot. |
| 3 | **Byte vs headroom fixtures** | `--scenario pressure` requires `reused==0` (forced miss); it does not engage the restore guard. | Keep byte-pressure (eviction) and 100%-cached restore on separate fixtures/records. |
| 4 | **Comparable-string audit** | Fix-only reason strings must not be counted as baseline-comparable categories. | Diff the reason strings in §3a/3b against `42cb41b314` source before freezing comparable totals. |
| 5 | **Restart expectation** | `T`/anchor/pair/tail-ring are distinct; a rewind can make the deepest stored generation incompatible as a whole. | Confirm §4c boundaries exist on the actual candidate for the chosen prompt lengths (which generations survive, whether `tail_draft` fires) in one boot before asserting the deep-restore predicate. |

Nothing here is a proven-pass gate. §6 #1 and #2 are genuine bridge/calibration gaps that need the parent's source decision and GPU calibration respectively; §4's exact surviving-generation behavior on the chosen fixture must be confirmed in a boot, not assumed.
