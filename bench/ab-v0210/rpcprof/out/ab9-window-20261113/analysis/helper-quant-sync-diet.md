# Helper window (kBegin/serve_begin) quant + sync diet — design

Scope: design only. Source anchors from `/mnt/srcds/Strata-wt-helper-ipc` @ `read-once-prefill`
(c89d626480, base lineage v0232b-hotfix c7be950984). Measured anchors: prompt-tier span
decomposition (docs/work/MIXED_P2_REFERENCE.md:499-507, this tree's copy identical), window-tier
`rpcprofile: serve tier=w` lines (logs/prod-swift15-f16kv-dflash-w4-q4_0-mixed-20261110-193526-993230-helper.log,
211 frames), and the sibling `tg-design.md` (per-window RPC structure). Composes with branch
`helper-window-diet` (worktree `/mnt/srcds/Strata-wt-helper-diet`, 3 commits: narrowed scratch
clear, grow-only kRows reply buffer, device lock released before the reply write) — nothing here
re-touches those spans.

Line anchors are the c7be950984 numbering used in the task (the IPC worktree's `read-once-prefill`
head matches serve_begin byte-for-byte; only the diet branch moves them).

---

## 0. The premise correction the whole design hangs on

The task's frame — "quant 44.9% + ksync 39.1% = 84% of the frame, same structure in the window
tier" — is **half right**. The 84% split was measured on the **prompt tier** (kBeginP,
`serve_prompt_core`), where `quant` is the helper's own fp32→q8_1 twin quantize run **inside its
device lock** (helper_main.cpp:121-147, span names at :739-751). The **window tier has no quant
span at all**: `serve_begin` receives the image **already quantized on the wire** and uploads it
verbatim (helper_main.cpp:653-657, "no requantization"). The window tier's quantization tax lives
**on the engine**, in `RemoteExperts::proxy_begin` (src/core/remote_experts.cpp:590-593), inside
the engine span profile's `claim_us` — on the decode doorbell critical path, but outside the
helper lock. And there is **no weight quant anywhere** in the serve path: slots hold native weight
pairs, consumed natively by the grouped kernel (helper_main.cpp:629, :666, :680-682).

Consequences for the designs below: "quant-at-fill / per-slot quant memoization" has no referent
(weights are never converted; the image is per-token activations and cannot be slot-cached), and
the window-tier analog of the 84% is: engine quant (~72-144 µs/frame) + helper ksync
(~90-150 µs/frame of the ~199 µs p50 lock) out of a ~1.5-2.4 ms per-frame RPC exposure — the rest
is transport (two socket hops, four thread wakeups, validate/scatter).

### Measured window-tier frame anatomy (211 tier=w frames)

| metric | value | source |
|---|---|---|
| rows/frame | avg 7.9, max 9 | log, `rows=` field |
| lock_us | **p50 199, mean 323, p90 453, p99 2152, max 2402** | log, `lock_us=` field |
| queue_us | ≈ 0 (no cross-channel contention in the decode regime) | log, `queue_us=` field |
| reply_us | ≈ 20 | log, `reply_us=` field |
| frames/verify window | ~33 (7 of 48 layers skip claims) | tg-design.md:45 |
| helper-dependent share | 50-80 ms/window ⇒ ~1.5-2.4 ms per frame RPC | tg-design.md:186-187 |
| helper lock total/window | ~10.7 ms (33 × 323 µs mean) | derived |

Geometry (include/strata/kernels/cpu/expert.hpp:36-37, helper_main.cpp:82, remote_experts.cpp:526):
H=2560, FF=640, kK=10, kMaxTok=8 ⇒ CAP=80 entries/frame. Image = n_tok×2880 B (q8_1) or
n_tok×2920 B (Q8_K) ≤ 23.4 KB. Staging: `d_q8` device 233.6 KB (:1711-1712, :1729); `d_out`
**pinned+mapped host** 800 KB (:1733-1734); `d_scratch` device, `native_expert_scratch_bytes(80,640)`
= 672 KB (:1706-1708, formula src/kernels/cuda/iq_kernels.cu:5553-5556: 3 aligned fp32 planes
cap×FF×4 + aligned q8_1 plane); GroupMeta 1608 B pinned+mapped (:1722-1728).

---

## 1. Answers

### Q1 — where the q8 image comes from in serve_begin

**Engine-side, per-frame CPU quant of the window's ACTIVATIONS; uploaded from host per frame;
never cached, never VRAM-resident, never per-slot.** Chain:

1. The decode doorbell service calls `RemoteExperts::proxy_begin(layer, x, ids, n_tok, k=10, …)`
   (src/core/remote_experts.cpp:521). `x` is the window's fp32 activations, n_tok≤8 rows × 2560.
2. **The quantize**: `quantize_q8_1_cpu(x, n_tok*2560, q8.data())` at remote_experts.cpp:590-593
   (quantizer defined src/core/expert_helper_socket.cpp:105-155, "R2 proxy: the engine-side q8_1
   quantizer of the window's activations (the kBegin payload)"). Output = canonical q8_1 blocks,
   **n_tok×(H/32)×36 = n_tok×2880 B**. Nothing else is converted — **no IQ3_S→q8_1 weight
   conversion exists**; weights stay native in the slots forever (filled at kFill, never mutated;
   claims resolve via `cache.slot_of` → `device_slot` pointer arithmetic, helper_main.cpp:621-629).
   The Q8_K variant (env `STRATA_EXPERT_HELPER_Q8K`, remote_experts.cpp:559-587) swaps in
   `native_quant_act` at 2920 B/token.
3. The blocks are memcpy'd into the kBegin payload (expert_helper_socket.cpp:457-464), sent on the
   socket, land in the helper's per-channel pageable RxBuffer, and `serve_begin` uploads them H2D
   per frame with `cudaMemcpyAsync(d_q8, q8, n_q8, H2D)` (helper_main.cpp:657) — from **pageable**
   memory (the socket receive buffer), staged by the driver.

Per-frame byte cost: n_tok×2880 B quantized on the engine CPU (~18 µs/row at the prompt tier's
measured 6036 µs / 335 rows ⇒ **~72-144 µs/frame**, ~2.4-4.8 ms per window), then the same bytes
pay wire + H2D again on the helper side.

### Q2 — what cudaStreamSynchronize at :685 waits for

It drains **this frame's whole enqueued chain on hp.stream**: the q8 H2D (:657, 9-46 KB pageable),
the GroupMeta H2D (:664, 1.6 KB pinned), the claim-sized d_out clear (:669, 72-90 KB written
**through the PCIe mapping** — d_out is cudaHostAllocMapped, :1733), the scratch clear (:673, 672 KB
device at CAP=80), and the grouped launch (:675-682), which is internally ~3 kernels (gate/up GEMM,
SwiGLU+q8_1 requant of the intermediate, down GEMM — e.g. the v2k path at iq_kernels.cu:6336-6341;
scratch layout at :6281-6285).

At the ~7-8 row claim scale **the GEMM itself is microseconds, not milliseconds** — the kernel
file's own benches: RTX 2080 Ti "10 experts x 1 entry 47.3 → 41.4 µs" (iq_kernels.cu:6201-6203),
V100 "1 group of 2 29.6 vs 21.3 µs" (:6360-6362), P40 "18-group call at 2 tokens: IQ3_S 390 µs"
(:6193-6195). On the RTX 3080 helper with ~7-8 groups × 1-2 entries expect **~30-80 µs device
work**; the sync span is therefore dominated by (a) seven enqueued ops' launch/dispatch gaps
(~2-5 µs each), (b) the mapped-host memset and pageable H2D engine time, and (c) the sync API's
own spin/wakeup (~10-20 µs) — all bounded by the measured lock p50 of 199 µs. Call it
**~90-150 µs ≈ 45-60% of the window lock: pure overhead, not compute.** The prompt tier's 8.95 ms
ksync median (2.292 s / 256 claims) is a different regime: ~335 rows, the 8192-row prompt scratch
clear (~21 MB) and the 413 MiB-per-window image drain dominate there.

### Q3 — STRATA_HELPER_Q8_IMAGE (format 0)

Read **per call** in `build_beginp_payload` (src/core/expert_helper_socket.cpp:694), default OFF.
It affects **only the prompt tier (kBeginP)**: with the env set and an fp32-advertising peer, the
engine quantizes each selected row with `quantize_q8_1_cpu` (:736) and ships `image_format 0`
(the tier's original wire); the helper uploads the blocks verbatim
(helper_main.cpp:888-889, "upload verbatim, no requantization") and its `quant` span — the
fp32→q8_1 twin (:121-147) that image_format 1 forced inside its device lock — collapses to ~0.
Q8_K keeps precedence; a peer without the fp32 bit was already format 0 (env inert); unset keeps
today's bytes.

**Window tier: inert by construction.** `BeginReq` has no image-format field
(include/strata/core/expert_helper_protocol.hpp:220-227) — kBegin always carries engine-quantized
q8_1 and kBeginQ8K carries Q8_K (frame type *is* the format). The env never reaches the window
path. In effect, **the window tier has been on the "format 0" arrangement since R2** — the
engine quantizes, the helper uploads verbatim — which is exactly why Q1's engine-side quant is the
window tier's quant tax.

**Why −42% on the prompt tier**: `quant` was 44.9% of the 5.856 s lock; deleting the span measured
lock 5.856 → 3.376 s = **−42.4%** (docs/work/MIXED_P2_REFERENCE.md:545-547). It landed short of
44.9% because second-order effects cut both ways (the 3.56× smaller image also shrank `upload_us`,
while the engine's `begin_us` grew); and the **8k wall moved only −0.8%** — on the prompt tier the
engine paces the layer cycle, so helper-lock savings did not reach the wall. That caveat does NOT
transfer to decode: there the doorbell graph spin-waits flagCPU (verify.cpp:2418-2459 per
tg-design.md:22-24) and the RPC RTT is directly exposed, so engine-side claim-span savings are
real wall savings.

---

## 2. Designs, ranked by (span reduction × safety) / effort

Expected-reduction column: fraction of the **~1.5-2.4 ms per-frame exposed RPC** (the quantity the
decode wall feels), with the helper-lock portion broken out. All are env-gated default-OFF; revert
= env change, no rebuild.

### W3 — cheap bundle first (safe microseconds, composes with helper-window-diet)

1. **Drop the d_out pre-clear** (:669) once store-semantics are proven. The H14 comment is
   conditional by its own words — "a stale value … must never survive **if the kernel accumulates
   rather than stores**" (:667-668) — and the diet branch's own scratch reasoning ("the
   gu/SwiGLU/down chain writes every byte of the dst.size()-entry prefix") says the down pass
   STORES. Evidence to pin: `launch_down` store instructions (iq_kernels.cu, the `out[dst] = …`
   epilogue) + one host test with a poisoned d_out. Saves the 72-90 KB mapped-PCIe memset +
   one enqueue: **~10-15 µs/frame**. Keep the clear under the env gate for one release.
2. **Pin the RxBuffer**: `cudaHostRegister` the per-channel grow-only receive buffer once
   (helper_main.cpp:1418-1420 area) so the :657 H2D runs from pinned memory (no driver staging).
   **~5-10 µs/frame**, zero wire change. VRAM: none (host pages).
3. **Engine payload diet**: `q8.assign(n, 0)` zero-fills n_tok×2880 B every frame before the
   quantizer overwrites it (remote_experts.cpp:591-592) — resize-once reuse instead; same for the
   per-frame `std::vector payload` rebuild in `begin()` (expert_helper_socket.cpp:457). Thread-local
   grow-only, exactly the helper-side RxBuffer pattern. **~10-20 µs/frame** off claim_us.

Totals: **~25-45 µs/frame (~2-3% of the RPC)**, zero risk, no wire bytes change.
VRAM: none. Effort: **3-5 h** incl. host tests. Env-gate: STRATA_HELPER_WIN_FAST=1 bundles 1
(2 and 3 are unconditionally safe).

### W1 — fp32 window wire + helper-GPU quant micro-kernel (kills the real quant tax) ★ PRIMARY

The window tier's quant cost is the engine's CPU quantize (Q1). Delete it the way M4c (b) did for
the prompt tier, but land the quantize on the **helper's GPU stream** instead of either CPU:

- **Wire**: new frame type `kBeginF32` negotiated by a feature bit (the exact kBeginQ8K pattern,
  expert_helper_socket.cpp:486-516 + protocol kFeatureQ8K precedent :87). Payload carries the fp32
  rows (n_tok×H×4 ≤ 82 KB). At n_tok≤8 the 3.56× wire growth is ~+59 KB/frame ≈ 10-15 µs on an
  AF_UNIX socket — negligible, unlike the prompt tier's GiB-scale images.
- **Engine**: `proxy_begin` skips `quantize_q8_1_cpu` (remote_experts.cpp:590-593) and sends `x`
  verbatim → **−72-144 µs/frame off claim_us** (~2.4-4.8 ms/window). If `x` lives in the engine's
  pinned doorbell staging, this also makes the frame a zero-copy candidate for the claim-stripe
  memfd lane (below).
- **Helper**: upload fp32 into a 82 KB pinned staging slot; launch a ~40-line GPU twin of
  `quantize_q8_1_cpu` (one thread per 32-block: same amax/sum order, same fp16 scales
  (`__float2half_rn`), same roundf/clamps — the bit-identity proof the CPU twin already carries,
  helper_main.cpp:116-120, redone for the GPU) writing `d_q8`; then the existing grouped launch,
  unchanged. Lock grows +~15 µs (bigger upload + one µs-scale launch) for −72-144 µs engine-side:
  **net −60-130 µs/frame (~4-8% of the RPC, ~2-4 ms/window)**.
- **Parity**: golden-byte pin against `quantize_q8_1_cpu` on the socket-test pattern (section 21
  precedent, tests/core/expert_helper_socket_test.cpp:2283-2337). The H14 finite scan already
  guards the output side.
- VRAM: +82 KB pinned host + reuse of d_q8. Effort: **6-10 h**. Risk: medium-low (the GPU twin's
  bit-identity is the only new arithmetic on the path; a single failed golden test reverts the env).
- Fused-in-kernel variant (the task's "GPU-inline/fused quant in the grouped kernel") is **rejected
  against this**: the grouped kernel reads the image once per GROUP (7-8×), so inline quant
  recomputes it per group, and it entangles the new arithmetic with the verified GEMM. The
  standalone micro-kernel keeps the grouped launch byte-identical.

### W2 — kernel writes the reply payload into a pinned ring (kills pack; feeds claim-stripe)

Make the down pass (or a tiny epilogue kernel) write reply records `[int32 claim_idx | H floats]`
directly into a **pinned host ring** instead of the bare `d_out` rows, so `serve_begin`'s pack loop
(:708-712, ~15-25 µs of mapped-memory memcpy) disappears and `write_reply` sends the ring slot
verbatim. The finite scan (:688-698) then reads the packed records.

- **Buffer/ownership contract for the memfd claim-stripe follow-up** (journal-approved follow-up,
  serving-tg-reeval-split-retest-2026-11-11.md:2506): the ring is a fixed-size table of N slots
  (N = max concurrent frames = 2 sockets × 1 outstanding frame each, the protocol's own
  one-outstanding rule, expert_helper_socket.cpp:465-483), allocated once as pinned+mapped memory
  and **also exposed as a memfd** the engine mmaps read-only. Slot k belongs to frame k mod N from
  enqueue (after the helper parses kBegin) until the engine's finish consumed it (reply ack or
  kRows read completes); the helper never writes slot k again before the engine's consumption flag
  for the previous generation flips (a per-slot sequence number the engine validates — the same
  seq discipline the socket frames already carry). The kRows wire reply can then shrink to a
  header pointing at the slot (rows/seq/n_rows), with the socket reply kept as the fallback path
  when the memfd is not mapped — byte-identical default, zero-copy when negotiated.
- Gain: −15-25 µs pack + the D2H reply bytes (72-90 KB/frame) leave the socket on the memfd lane;
  combined with W1 that is most of the per-frame byte traffic off the transport. **~5-8% of the
  RPC, more once claim-stripe pipelines frames.** Effort: **10-14 h** (kernel epilogue + ring +
  tests + negotiation bit). Risk: medium (new kernel output layout — pinned by extending the H14
  dump tests). VRAM: N×(8 + CAP/2 avg×10244) ≈ 2×~400 KB pinned host.

### W4 — event-driven completion, lock released at launch (spell the contract, land it late)

Replace the blocking `cudaStreamSynchronize` (:685) with `cudaEventRecord(ev_frame)` at launch;
release the device lock; scan/pack/reply run after `cudaEventQuery` reports done (or a
`cudaLaunchHostFunc` doorbell). **Honest value on decode: small** — measured queue_us ≈ 0 (no
contender for the lock in the decode regime; the prompt tier is idle), and one frame is in flight
per channel by protocol, so early release mostly helps the p99 tail (~2.2 ms frames) and the
prompt/window contention legs, not the median. **~20-60 µs/frame typical, ~0 on uncontended legs.**

Correctness contract, since the task asks for it spelled:
- **d_out lifetime**: the frame's output buffer is owned from enqueue until its event completes;
  the serve thread is the only reader (scan/pack), and it event-waits first. Reuse by the next
  frame is safe only because (a) same-channel frames are strictly sequential — the engine cannot
  send kBegin N+1 before kRows N was written (io_mtx_ + begin_outstanding_, expert_helper_socket.cpp:465-483),
  and (b) cross-tier staging is already separate (_p buffers, helper_main.cpp:1722-1758) or
  FIFO-ordered on the single shared stream in default mode — stream order makes d_q8/d_out/d_scratch
  reuse correct without the mutex, which is exactly why the mutex can go.
- **One-outstanding-frame rule**: unchanged and now load-bearing — it is what bounds the number of
  live events/buffer epochs to one per channel. The claim-stripe follow-up must weaken it
  explicitly (per-stripe outstanding set) before pipelining more than one window frame.
- **Fill fencing**: kFill takes gpu_gate as sole writer (helper_main.cpp:362-370) — with early
  release it must additionally wait every channel's last frame EVENT (cudaEventSynchronize), not
  just its reply; the diet branch already established the reply is channel-local after the sync.
- Effort: 8-12 h, risk medium (fill/event interplay), yield low standalone. **Land it as part of
  the claim-stripe lane, not alone.**

### W5 — quant-at-fill / per-slot lazy quant memoization: REJECTED (premise false)

No weight quant exists to memoize (§0, Q1). The charitable reading — convert slots to q8_1 weights
at fill — is strictly bad here: q8_1 is 9 bpw vs the pack's ~3.06 bpw (IQ3_S), so a slot grows
~1.48 MiB → ~4.4 MiB and the fill budget's slot count collapses ~3× (4600 → ~1500; the tg-design
rig's 3560 → ~1200): residency, the thing the window tier exists for, is destroyed. It would speed
the GEMM (Q8_1×Q8_1 MMQ) — but the window kernels are latency-bound at 7-8 rows (Q2), so there is
nothing to speed. (As a PROMPT-tier ksync lever — 8.95 ms median at 335 rows — a q8_1 weight arm
is a legitimate separate question, quality- and VRAM-gated; out of scope here.)

---

## 3. Summary table

| # | design | span cut / frame (of ~1.5-2.4 ms RPC) | per window | VRAM | risk | effort | env-gated |
|---|---|---|---|---|---|---|---|
| W3 | cheap bundle (clear drop + pinned Rx + engine payload reuse) | ~25-45 µs | ~1-1.5 ms | 0 | low | 3-5 h | 1 gate; 2-3 free |
| W1 | fp32 wire + helper-GPU quant ★ | ~60-130 µs (net) | ~2-4 ms | +82 KB pinned | med-low | 6-10 h | yes (frame-type bit) |
| W2 | kernel-packed reply ring (+memfd lane) | ~20-35 µs + wire offload | ~1-2 ms + stripe enablement | ~0.8 MB pinned | medium | 10-14 h | yes (feature bit) |
| W4 | event completion / early lock release | ~20-60 µs (contended/tail) | ~0 uncontended | one event | medium | 8-12 h | yes |
| W5 | quant-at-fill / slot memo | — (premise false; −3× slots) | — | +6-9 GiB | — | — | rejected |

## 4. Recommended landing order

1. **W3** — this week; three independent µs-wins, composes with helper-window-diet (its narrowed
   scratch clear and lock release stay; W3.1 deletes the *d_out* clear the diet branch left).
2. **W1** — the actual "quantization tax" kill for the window tier: engine claim-span −72-144 µs
   per frame, the only quant that exists there, and unlike the prompt tier's −42% the saving is
   wall-real because decode's doorbell exposes the whole RTT.
3. **W2** — reply ring + memfd contract: deletes pack, and its ring/ownership contract is the
   on-ramp the approved claim-stripe follow-up needs (stripe without it would multiply the
   per-frame socket bytes it exists to remove).
4. **W4** — only inside the claim-stripe change, where multiple outstanding frames make early
   release matter; its correctness contract is written above to be lifted verbatim.

Failure containment: every piece default-off by env/feature bit; unset = today's bytes; no
worktree touched by this design.
