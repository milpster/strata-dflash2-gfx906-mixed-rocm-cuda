# Lanes review — strata mixed-GPU serving campaign, 8 lanes off v0232b-hotfix (c7be950984)

Reviewer gate before the build window. Code-reading only (no builds, no GPU, no processes run).
Every lane reviewed as `git -C <worktree> diff c7be950984..HEAD`. Design docs:
`tg-design.md` (C1/C3/C4/C5), `helper-quant-sync-diet.md` (W1–W4), journal
`serving-tg-reeval-split-retest-2026-11-11.md` rounds 16–18e.

---

## A. Verdict table

| # | lane / worktree | commits reviewed | verdict | one-line basis |
|---|---|---|---|---|
| 1 | tailskip-helper-guard @ Strata-wt-tailskip | f40be15c80 + a900344979 | **PASS-with-notes** | Gate is exactly the legacy rule knob-off (exhaustively test-walked); per-entry `helper_holds` guard matches C5-conservative; notes: banner text changes on default boots (intended), knob named `STRATA_TAIL_SKIP_HELPER` not the doc's `STRATA_ROUTE_TAIL_HELPER` |
| 2 | graph-family-trim @ Strata-wt-graphtrim | 532ba8cd2b | **PASS-with-notes** | Default-off inert (only dead member writes); evict_window_graph's sync-before-destroy rule followed; notes: hysteresis counts `refresh_ar` calls (≥1 per window, ar_live() adds more) not strictly windows; a guard-skip (capture_all/pipelined/release) marks `ar_trimmed_` permanently until the next flip — conservative, may miss a trim window |
| 3 | foresight-shadow @ Strata-wt-foresight | b4506679a4, 8dbf925022, d8af6a98bd, d4d81db959 | **PASS-with-notes** | All four C1 fixes present and default-off clean (reorder is a no-op when shadow off; mask loop finds no candidates without FS_SLOTS); notes: **no host test** (the doc's plan promised claim-suppression + slot-accounting tests), **no journal landing entry**, stage_blob pack reads run synchronously on the dispatch thread (bounded by ring occupancy), a slot dropped by a #2134 exchange keeps its ring entry until reassign (ring starvation → `ring full` counter, no correctness issue) |
| 4 | claim-stripe @ Strata-wt-claimstripe | da334704ee + 3b964e33c2 | **PASS-with-notes** | Stage-1 stripe and stage-2 ring+W4 match the journal 18e record; negotiation matrix, one-fd contract, stale-seal, per-slot staging all verified; notes: `claimed_out` duplicates the B-half (`claims` already contains it — benign double CPU-recompute on the failure path only), a single B failure latches B (and its prompt tier) off for the process (pre-existing latch class), several same-anchor cherry-pick conflicts (mapped below) plus one **semantic merge must-do** (claim_res forwarding) |
| 5 | helper-window-diet @ Strata-wt-helper-diet | 51b3899a11 + a2c30a5e55 + d596de441c | **PASS-with-notes** | Wire bytes identical, grow-only reply fully overwritten, lock release sound (stream drained at sync, reply reads channel-local bytes only); notes: the narrowed memset is the **scratch** clear (d_out was already claim-sized at base — task/journal wording says "d_out"; journal row is loose), the scratch-tail-stale-bytes assumption is the same proof class as W3.1 and is watched by the per-frame H14 finite scan |
| 6 | read-once-prefill @ Strata-wt-helper-ipc | a2cddd32b9 + c89d626480 | **PASS** | Faithful to round-16 design A: request-scoped LRU at the copy_blobs/copy_blob hook, stepwise RAM-guarded admission (PIN_RESERVE default 2 GiB), wholesale disarm via RAII on any exit, host test, `read-once reuse:` meter; knob-off is bit-for-bit (every hook behind `armed()`) |
| 7 | window-wire-diet @ Strata-wt-wirediet | d678fb08ba + b774ee868a + 1d9a980708 | **PASS-with-notes** | kBeginF32 negotiation is the exact kBeginQ8K pattern (never sent to a bit-less peer); W3.1 store-proof recorded at the call site with per-variant line refs; W3.2's deviation from the doc (stage through pinned h_img instead of registering the realloc-ing RxBuffer) is documented and strictly safer; notes: the golden byte-identity pin is GPU-execution-deferred to the build window (dump + compare harness landed), F32+stripe interaction must be resolved as mapped below |

**No FAIL. Nothing blocks the build window.** All seven lanes default to today's bytes/behavior on a
no-env run (details in B). The blocking-adjacent items are integration obligations (C), not lane bugs.

---

## B. Default-off equivalence (per lane, with the hunt results)

Hunted paths: unconditional env reads, banner/log output, counter wiring, lazily-allocated buffers,
negotiation bits offered unasked, changed function signatures, thread-local reuse buffers.

1. **tailskip** — `tail_skip_gate(rank, peer, remote_count, env) == legacy_gate` for every
   rank/peer/remote combination when the knob is absent/empty/`0`; the test walks all 240 combos.
   `skip_d`/`kind` loops untouched knob-off. **Only default-run delta: stderr banner text** — a
   helper rig now prints "…is suspended…" where v0232b printed "…is ON…" (the lane's stated purpose;
   log-only, no behavior). Knob read once via static.
2. **graphtrim** — `trim_windows == 0` short-circuits before `++ar_stable_`; the new early `return`
   inside the flip block matches the base function's tail (nothing followed it). Members
   `ar_stable_/ar_trimmed_` are write-only inert. `cudaMemGetInfo` etc. only inside
   `trim_stale_family`, unreachable knob-off.
3. **foresight** — with `helper_shadow=false`, the dispatch branch
   `(helper_shadow || !helper_holds(e)) && take()` evaluates `helper_holds` first and `take` only
   after — the base's exact else-if order and call pattern. The proxy_begin mask loop requires
   `kind[i]==0 && primary_res<0`, which without `STRATA_FS_SLOTS` (no `d.fs`) cannot occur →
   `claim_res == primary_res`, no `res_copy`, byte-identical frames. Ring allocation only under
   shadow; `note_miss` restructure preserves request_locked's exact gate order and effects. The
   stage_blob lambda is only installed when shadow on.
   *Hunt find (benign)*: with FS_SLOTS on and shadow **off**, a slot-served non-helper-held entry
   also qualifies for the mask, so the frame's bitmap gains bits vs base — claims/replies unchanged
   (the helper doesn't hold those pairs either way); opt-in-config-only wire-byte delta.
4. **claimstripe** — `claim_stripe_` false without env+socket2; `proxy_claims_b_` stays empty;
   `owns()`'s extra binary_search over an empty vector is a no-op; the new stripe counters read 0
   and are added to the DONE-line helpers as +0; `begin_stripe` never called; the ring engine half
   asks only when `STRATA_HELPER_REPLY_RING` is set, and `map_ring(not offered, not wanted)` is a
   no-op. Helper: ring_create only under env; ack offers the bit only to asking hellos; `ring_slot
   = -1` otherwise; `read_frame_anc` is used only in the engine's hello (same bytes when no fd
   arrives — recvmsg vs recv is wire-identical). The "ignored" notice fires only with env set.
5. **helper-diet (PASSIVE — no knob)**: behavior-preservation rests on three claims, all checked:
   (a) *reply bytes*: the grow-only per-channel buffer is fully overwritten on `[0, need)` (header +
   records) before `write_reply` — identical wire bytes, no zero-fill dependency;
   (b) *lock release before reply*: the frame's stream drained at the sync above it, H14 scan and
   pack read only d_out/channel-local bytes, fill's fencing requirement is the stream (drained) —
   scheduling change only, not observable in output;
   (c) *scratch clear narrowing*: `scratch_dirty = max(native_expert_scratch_bytes(dst.size()),
   moe_hit_grouped_scratch_bytes(dst.size()))` — both formulas linear in the entry count, max covers
   both launch variants, and the claim "the chain writes every byte of the dst.size()-entry prefix
   before reading it" is the same proof class as W3.1, precedented by the prompt tier's narrowed
   clear. Residual risk = a variant reading scratch beyond the formula → stale bytes → the H14
   finite scan (still on every frame) converts it to a loud refusal, not silent corruption.
   The d_out clear line is untouched by this lane (already claim-sized at base).
6. **read-once** — every consult behind `armed()`; knob-off arms nothing; the meter prints nothing
   when hits+reads == 0; `copy_blob`/`copy_blobs` bodies identical when disarmed. RAII end on any
   exit; refcount nesting uses acq_rel fetch_add/sub.
7. **wirediet** — engine: `helper_f32_window_enabled() && helper_->f32_window_supported()` — both
   halves gate kBeginF32; Q8K branch returns first (precedence kept); W3.3 gated (default fresh
   vector; `resize` on a fresh vector zero-fills → today's bytes). Helper: the F32 bit is
   advertised unconditionally (an old engine ignores unknown bits — safe direction), but
   `image_f32` requires the kBeginF32 frame type which an ungated engine never sends; h_img/
   d_img_f32 allocate lazily only when `image_f32 || pinned_rx()` → default-run `buffers_bytes`
   unchanged. W3.1's clear drop strictly behind `win_fast()`.

---

## C. Cross-lane integration (the build-window map)

### C.1 Protocol allocations — collision-free, only two lanes touch the wire

| constant | base | wirediet | claimstripe | others |
|---|---|---|---|---|
| HelloAck feature bits | 1,2,4,8,16,32 (bits 0–5) | **kFeatureF32Window=64** (bit 6) | **kFeatureReplyRing=128** (bit 7, deliberately renumbered off 64) | none touch protocol |
| frame types | 1–19 | **kBeginF32=20** | **kRowsRing=21** (comment records 20 taken) | none |
| HelloReq flags | reserved0 must be 0 | — | **kHelloWantReplyRing=1** in reserved0 (unknown bits refused by name; legacy helper kError → one flag-clear retry, host-pinned) | none |

Verified no other lane edits `expert_helper_protocol.hpp` (tailskip/graphtrim/foresight/helper-diet/
helper-ipc: 0 files). `HelloReq` layout still pinned by static_assert.

### C.2 Cherry-pick order tailskip → graphtrim → foresight → claim-stripe → window-diet → read-once → wirediet

File-collision matrix (✓ clean, △ textual conflict with trivial resolution, ▲ textual conflict +
semantic obligation):

| file | lanes | verdict |
|---|---|---|
| src/core/expert_source.cpp | 1+3 (dispatch ~5060–5085 vs ~5109+), +6 (copy_blobs ~3519+) | ✓ distinct hunks (>20 lines apart; 3-line contexts don't touch). Lane 1's skip loop and lane 3's else-if chain/begin-seam/ForesightSwap impl are disjoint. |
| src/program/generate.cpp | 1 (banner ~15635) + 3 (stage_blob ~15556) | ✓ different regions |
| src/core/remote_experts.cpp / .hpp | 3+4 (+7 in .cpp) | **▲** the one semantic obligation — see C.3 |
| expert_helper_socket.cpp/.hpp | 4+7 | △ both insert knob helpers after `claim_batch_env` (same anchor, keep both); both append protocol constants at the same anchor (keep both); `hello()`: lane 4's retry-loop rewrite + lane 7's one-line `f32_window_` latch (adjacent, auto-merges or one-line fix); hpp: distinct regions (lane 4 @@142/214/318-435, lane 7 @@14/67/111/133/329) |
| tools/mixed_gpu/sidecar/helper_main.cpp | 4+5+7 | **△ 3-way serve_begin signature**: lane 5 adds `std::vector<uint8_t>& reply` (pos 2), lane 4 adds `int ring_slot` (last), lane 7 adds `bool image_f32` (last) — merge to `serve_begin(hp, body, reply, req_id, seq, err, out, image_q8k, image_f32, ring_slot)`; **same-line conflict at the serve-loop call site** (all three edit `else rc = serve_begin(hp, body, head.req_id, head.seq, err, out, head.type == kBeginQ8K);`). Lane 7's W3.1 clear-gate wraps the d_out memset line (untouched by lane 5); lane 5's scratch hunk sits above the launch (untouched by 4/7); lane 4's ring block is inserted between launch-check and the legacy sync (lane 5's reply/lock hunks live in the legacy tail beyond it) — **content-order composes**: ring path returns early, so lane 5's unlock-before-reply applies only to the legacy (non-ring) path; no double-unlock possible. |
| tools/mixed_gpu/sidecar/helper_frame.cpp | 4 only (7 and 5 don't touch) | ✓ |
| CMakeLists.txt | 1+6+7 | △ all three add test blocks at the swapin_depth_test anchor — adjacent insertions, trivial |
| tests/ | disjoint files | ✓ |

### C.3 The semantic merge obligations (the integrator MUST do these, git will not)

1. **claim_res forwarding (foresight × claimstripe × wirediet)** — the only genuinely dangerous
   merge point. Lane 3 masks foresight-served (kind 0, res<0) experts into `claim_res` and passes
   it to `begin`/`begin_q8k`. Lane 4's stripe block (built off base) passes **`primary_res`** to
   `helper_->begin`, `helper2_->begin_stripe`; lane 7's F32 path passes **`primary_res`** to
   `begin_f32`. If merged as-written and the panel runs `FS_HELPER_SHADOW=1 FS_SLOTS=… +
   CLAIM_STRIPE=1` (or `F32_WINDOW=1`), the unmasked bitmap lets the helper claim slot-served rows
   that are absent from `proxy_claims_` → `validate_rows_payload` "no unclaimed index" → kErrShort →
   channel closed + fallback. Default-off runs are immune (mask is empty → `claim_res ==
   primary_res`). **Fix: pass `claim_res` in all four begin variants in the merged proxy_begin**
   (begin, begin_q8k, begin_stripe's bitmap, begin_f32). The claims list itself already derives
   from claim_res, so the halves stay consistent once the frames carry it.
2. **F32 × stripe ordering** — lane 7's F32 gate sits immediately before `quantize_q8_1_cpu`, lane
   4's stripe block immediately after. Keep the F32 gate first (F32 takes the layer; the stripe is
   inert under F32 because begin_stripe needs the q8 image). Any interleaved resolution that puts
   the stripe between the F32 check and its return would strand the B-half — don't.
3. **serve_begin merged body** — with the ring path present, lane 5's lock release must stay in the
   legacy tail only (it does, by the early return), and lane 7's `image_f32` staging/quant must run
   before the grouped launch in BOTH the ring and legacy paths (it's above the launch — composes).
4. **kBeginF32 on the ring lane**: reply stays kRows/kRowsRing — `serve_begin`'s ring epilogue is
   downstream of d_q8 production, so F32 quantize-then-epilogue composes unchanged.

### C.4 The composition question asked point-blank (W3.1 proof vs the ring epilogue vs I-diet vs WIN_FAST)

`ring_pack_records` (helper_main.cpp, lane 4) is a **pure down-pass epilogue**: it reads
`rows[i*h+c]` from d_out and `h_ring_claims[slot]`, and writes only the slot's records area + seq
word — it never writes a d_out row, so W3.1's store-semantics proof (which is about *who writes
d_out*) is unaffected by its existence. Range agreement across all three edits:
- the grouped kernel STORES every row in `[0, dst.size())` (W3.1's per-variant proof, incl. the
  two variants serve_begin can launch: `native_expert_grouped` / `_q8k`);
- lane 5's claim-sized scratch clear and the (base) claim-sized d_out clear cover exactly
  `dst.size()` entries — the same range;
- the epilogue packs `n_rows == compact_claim.size() == dst.size()` rows;
- the legacy scan/pack and the H14 finite scan read the same `[0, dst.size())` prefix.
So there is **no path where a reader consumes a row no kernel variant fully wrote**: with the clear
present (default / lane 5) the range is pre-zeroed anyway; with the clear dropped (WIN_FAST) every
row in the read range is stored by the kernel before the sync/event that orders the read. The only
buffer whose stale tail can survive is d_scratch beyond `scratch_dirty` (lane 5), which no reader
touches. Cross-frame d_out reuse under W4's early release is safe by single-stream FIFO (both
channels' window frames share `hp.stream`; the prompt tier's split staging never touches d_out).

---

## D. Safety-contract spot audit

| contract | where | result |
|---|---|---|
| **W4 d_out lifetime / event fencing / one-outstanding** | lane 4 serve_begin ring path | **Sound.** Lock released at launch (both modes); the frame's rows left d_out via the stream-ordered epilogue, so the next frame's kernel overwriting d_out after this frame's stream work is harmless; the serve thread waits only its own `ring_ev[slot]` (spin then sync) before the H14 scan + doorbell; one-outstanding-per-channel (io_mtx_ + begin_outstanding_) bounds live events/generations to one per slot and makes slot generations strictly sequential. Events are recorded **at birth** (creation), so serve_fill's fence and a first frame's wait never see an un-recorded event. |
| **fill fencing under early release** | lane 4 serve_fill | **Sound.** With the ring on, fill event-syncs BOTH slots' events after winning the lock — covering frames that released early; non-ring channels keep the full-span lock + stream sync (fenced by the lock itself); the prompt tier keeps its whole-frame lock (host pack needs it). |
| **ring stale-seal (kErrSeq)** | lane 4 apply_ring_rows | **Sound.** Doorbell seq vs the slot's seq word (memcpy read; ordering is guaranteed by seal-before-doorbell-send, not by the read's volatility — the header's "volatile load" comment overstates, harmlessly). Mismatch → kErrSeq → channel closed. Host test 22 pins generation sequencing + stale-seal refusal. Re-accepted peers continue the seq sequence — both sides read the same memory, so no false positive. |
| **SCM_RIGHTS one-fd contract** | lane 4 helper_frame.cpp | **Sound.** `read_all_anc` refuses a second fd (closes everything) and MSG_CTRUNC; `write_seq` attaches the cmsg to the FIRST sendmsg only (`fd_pending` cleared after first partial send — no double-dup; a failed-first-send retry keeps it). The engine closes its received fd right after mmap ("the mapping is the reference"). |
| **memfd teardown ordering** | lane 4 Helper dtor + ring_create failure path | **Sound.** Channels joined first (no waiters), then per-slot claims freed + events destroyed, then `cudaHostUnregister(ring_map)` **before** `munmap` (correct order — munmap of a registered region without unregister leaks the registration), `close(ring_fd)` last. Engine side: `release_ring()` munmaps on close/re-hello. Creation failure disables the lane only (socket replies continue). |
| **C1 claim-mask agreement (kRows row-for-row)** | lane 3 proxy_begin + serve_begin | **Sound within the lane.** Engine claims = `claim_res<0 && proxy_holds`; the frame's bitmap is built from the same `claim_res` (`res>=0` → bit set → helper skips); the helper derives its rows from ids+bitmap+holds → identical set; kRows rows land in claim order and validate at original indices. Holds-staleness mismatch (helper evicted since the fill report) is the pre-existing failure class (validation error → fallback), unchanged. Cross-lane: see C.3 obligation 1. |
| **C5 skip-set (helper-held rows never skipped)** | lane 1 dispatch | **Sound.** `if (helper_holds(e)) continue;` precedes `skip_d[q]=true`; `helper_holds` covers ALL remote tiers; skipped entries set kind −2 (never claimed — begin only claims what it holds, so a skipped row could never reach the helper anyway); peer tiers suspend the skip outright. |
| **C3 destroy-under-stream sync** | lane 2 trim_stale_family | **Sound.** `cudaStreamSynchronize(cs_)` before `cudaGraphExecDestroy` (evict_window_graph's rule); sync failure keeps the family and retries next window; `launched_` flags reset (cold recapture); guards skip trim while `no_window_eviction_` (capture_all), `pl_ple_rows_` non-empty (pipelined readers), or released. pl_launch null-checks exec_ and errors cleanly; ensure() recaptures lazily before prelaunch. |

---

## E. Journal presence

| item | status |
|---|---|
| Round 18c (graphtrim landing) | ✅ journal:2524–2538 |
| Round 18d (wirediet landing) | ✅ journal:2540–2606 |
| Round 18e (claim-stripe landing) | ✅ journal:2608–2708 |
| Lane 5 (helper-window-diet) entry | ⚠️ round-18b code-lane table row only (journal:2513, 3 commits named) — no dedicated mechanics/composition record like 18c/18d/18e |
| Lane 6 (read-once) entry | ⚠️ round-18b table row only (journal:2512); design in round-16 Item 3 |
| Lane 1 (tailskip) entry | ❌ none (design mentioned at journal:2506-2507; no landing record) |
| Lane 3 (foresight) entry | ❌ none (no landing record, no test note) |

Recommendation: add compact 18f entries for lanes 1+3 (and fatten 5/6) before the build window —
18c/18d/18e set the precedent (mechanics + composition map + panel arm), and lanes 1/3 are the two
with the thorniest merge notes.

---

## F. EYEMARK — ranked further-improvement opportunities

Gain arithmetic anchors: the campaign regression `ms/tok ≈ 9.9 + 0.29 × rows/tok` (rows/tok 85 → 50
≈ 41 tok/s), window anatomy ~33 frames/window, 1.5–2.4 ms exposed RPC/frame, helper-dependent share
50–80 ms/window, drafter fills 2.58 of spec-5 (+1 drafted/window ≈ +9 tok/s).

| # | opportunity | expected gain (arithmetic) | effort | risk | provenance |
|---|---|---|---|---|---|
| 1 | **Validate + panel the landed C1 helper-shadow** (build window arm c: `FS_HELPER_SHADOW=1 FS_SLOTS=3,2`) with the C.3 claim_res fix | 40–60 % row interception → rows/tok 85→50–62 → **36.6–41.0 tok/s** (+27–42 %); pessimistic 30 % → 34.2 | 0 new code (landed) + 1 merge obligation | medium-low (parity gate: 61-task suite; slot thrash visible in `busy/over_budget`) | lanes' own deferral — everything now waits on the build |
| 2 | **Drafter acceptance at long context** (84 % @64k vs 65 % @180k): the STRATA_SPEC_MIN_P=0.4 passthrough arm + drafter depth, target ≥4 drafted of 5 at 180k | 2.58→4+ drafted/window ⇒ ~+13–14 tok/s on killerL-class legs (the exact gap between 24 and 35+); the largest single non-residency lever left | 3–6 h tuning + panel (knob passthrough already landed, lane B round-18) | low-medium (draft quality = acceptance, watch divergence) | R3A lean panel + forensics |
| 3 | **C4a stripe + ring panel arm with STRATA_HELPER_STREAMS=1** (true concurrency needs the split streams; default mode is wire-overlap only) | halves serialized per-layer RTT on unintercepted rows: ~15–30 ms/window of the 50–80 ms helper share → **+15–30 % of the residual after C1** (~+3–8 tok/s at rows/tok ~55) | 0 code; 1 boot arm | low (correctness proven stream-mode-independent) | claimstripe journal 18e's own note |
| 4 | **W1 F32 window wire golden pin + arm** (the −72–144 µs/frame engine quant tax) | −60–130 µs/frame net ⇒ −2–4 ms/window off claim_us → ~+2–5 tok/s at current rows/tok, more under C1 (frames that remain) | golden compare in the build window (harness landed) + env arm | low after the golden pin passes (single failed compare = unset env) | wirediet's deferred GPU leg |
| 5 | **Move lane 3's stage_blob staging off the dispatch thread** (queue (layer,expert) for the filler thread to stage; dispatch only notes) | removes up to 16 × 1–2 ms synchronous pack reads per request warmup from the doorbell critical path (~10–30 ms on first windows; steady-state bounded by ring churn) | 4–8 h | low (same ring/admit machinery, different thread) | this review |
| 6 | **kSwap adaptive helper residency** (helper mirrors 3560 pairs; 12997 dropped to the CPU pool; re-rank the mirror set by observed decode demand) | cuts the 1.4–5.1 % CPU-computed refusal share and the dropped-pair re-reads; rows/tok −5–15 ⇒ +2–5 tok/s; also raises C1's interception ceiling | 1–2 days | medium (fill churn vs the 6796 MiB cap; interacts with #2065 headroom) | G audit's deferred note |
| 7 | **Read-once design B (layer-major / request-scoped pack ring)** — decide on A's meter | A's meter gives the hit-rate ceiling; if A < 700 PP and >900 wanted: GGUF reads collapse to ~40 GiB write + 40–80 GiB cache reads → PP ~850–950 (from 482–534) | 5–8 days | medium (write-vs-read contention, disk-tier budget, FADV interplay) | round-16 design B, sequenced after A |
| 8 | **Window-tier multi-frame batching (C4b kBeginPB-for-decode)** | same −15–30 % class as #3 but without needing socket2; stacks with it only if C1's residual is still large | 1.5–2 days | medium (protocol + both state machines) | tg-design C4b, deferred by stage-1's "no seam" note |
| 9 | **Q8_IMAGE prompt-tier arm** (`STRATA_HELPER_Q8_IMAGE=1`, measured −42 % helper lock on the prompt tier) | PP legs only (the −0.8 % wall lesson: the engine paces the prompt tier); ~0 TG; cheap insurance for prompt/window contention legs | 0 (env arm) | low | Q3's measured-but-untested arm |
| 10 | Small hygiene from this review: release ring_idx on #2134-exchange-dropped slots (lane 3 ring starvation); de-duplicate the stripe's `claimed_out` (lane 4); volatile-word comment vs memcpy read (lane 4) | correctness-neutral; removes a starvation mode and a confusing fallback double-compute | <2 h combined | trivial | this review |

---

## Bottom line for the build window

- **Green light**: all 7 lanes PASS / PASS-with-notes; no FAIL; every knob fully encloses its
  behavior; lane 5's passive changes are wire-identical.
- **Do during integration** (not optional): C.3 obligation 1 (claim_res into begin/begin_q8k/
  begin_stripe/begin_f32) — the only way the combined C1+stripe/F32 panel arm breaks;
  the 3-way serve_begin signature/call-site merge; F32-gate-before-stripe-block ordering.
- **Do in the build window**: helper rebuild is required twice over (lanes 4+5+7 all touch
  helper_main.cpp; 4 touches helper_frame.cpp) — the CUDA tree needs `STRATA_BUILD_EXPERT_HELPER=ON`
  per 18d/18e; run `expert_helper_socket_test` (section 22), `expert_helper_f32_window_test` +
  the F32 golden dump compare, `tail_skip_guard_test`, `read_once_test`, then the panel arms in
  EYEMARK order 1→4.
- **Journal debt**: 18f entries for lanes 1+3, fatten 5/6.
