# The CUDA expert helper (`strata-expert-helper`)

**Status, measured 2026-10-28 (Linux, AMD primary + NVIDIA helper).** The helper computed real rows
on a real NVIDIA card: 1,972 validated returned rows over the six measured samples, 0 timeouts,
0 fallbacks. When the helper was killed mid-decode, the engine recomputed its claimed rows on the CPU
and finished the request. No speedup was measured — the same pinned request decoded at 34.68 tok/s without
the helper and 33.40 tok/s with it. The helper serves decode and speculative verification only, in
windows of up to eight tokens. Prompt prefill is not offloaded and no prefill offload is claimed.

**Opt-in only.** No shipped setup, preset, preset save, launch plan or Terminal/app default starts a
helper. A config without `"expert_helper_socket"`, and an engine run without `--expert-helper-socket`,
do not enable the proxy. Tests check that omitted helper settings add no engine arguments. A
release-versus-candidate output-equivalence run has not been done. The helper is an experimental
prototype, and the helper process is NVIDIA-only.

## What it is

The helper serves decode and speculative-verification windows of up to eight tokens. Batched prompt
prefill uses a separate engine path and does not call it. NVIDIA memory held during prefill is not
evidence that the card is computing. The measured hardware run found no end-to-end speedup.

One extra process, `strata-expert-helper`, on the NVIDIA card. The engine keeps its own device (a HIP
engine keeps AMD only — it runs the helper's client with no CUDA runtime mapped at all) and talks to
the helper over a local socket. The helper owns ALL of that card's expert VRAM: it fills the cache
slots, keeps them filled, computes one grouped launch per decode window, and sends the window's rows
back. The engine keeps the routing, the top-k, the blend, the logits, the context, and the CPU path.

The helper is a client of the engine's window, never of its memory: nothing is shared between the two
processes. It computes with the engine's own grouped-kernel code (`native_expert_grouped`), the same
kernels and the same formulas as the in-process tier, so a helper row and an in-process row are the
same arithmetic.

The engine's side is a proxy tier inside its normal expert dispatch. It registers FIRST in the
tier list, it makes no device call, and it claims only entries the plan left to it: entries the
primary's own caches hold never reach the helper.

## Start it by hand, stop it by hand

The engine never spawns the helper. Start the helper before the engine, stop it after.

```sh
# 1. the helper, on the NVIDIA card, BEFORE the engine (it binds its own socket, 0600)
./build-cuda/strata-expert-helper \
    --pack /models/qwen3.8-flash-next-q2_0 \
    --socket /run/$USER/strata-expert.sock \
    --reserve-mib 512
# (the fill's cap comes from the ENGINE's request at the handshake: `--expert-helper-slots` as a
#  count, `--expert-helper-mib` as bytes, and the card's own budget - the helper's `--slots` is a
#  standalone-run knob and the engine's request is what bounds a normal fill)
# on stderr, two lines:
#   expert-helper: listening on /run/.../strata-expert.sock (device 1, pack ..., 48 layers x 512 experts)
#   expert-helper: filled N slots (X.XX GiB), scratch N B, buffers N B

# 2. the engine, with the helper option (serve mode, native pack, profile + cache)
./build-cuda/strata --serve --model /models/.../model.gguf \
    --expert-profile /models/.../profile.txt --expert-cache 256 \
    --expert-helper-socket /run/$USER/strata-expert.sock
```

Stop: `Ctrl-C`/`SIGTERM` to the helper, then to the engine. The helper releases its card and unlinks
ONLY the socket it bound; a socket file it did not bind is left alone (it refuses an existing path at
startup, so a stale file is an error, never a takeover). The engine's own cleanup never unlinks a
helper path. A helper killed by a signal cannot unlink anything: after a crash the socket file stays
and the next helper refuses to start. Check that no helper is running, then remove the file by hand.

The engine flags: `--expert-helper-socket PATH`, `--expert-helper-layers N` (top layer, inclusive;
default every layer no split stage owns), `--expert-helper-slots N` (an EXPLICIT upper bound on the
slot count; there is no implicit count), `--expert-helper-mib N` (an upper bound on the helper's
aligned resident slot BYTES, MiB; 0/absent = AUTO, the card's budget decides),
`--expert-helper-reserve-mib N` (VRAM the helper leaves free on its card, default 512),
`--expert-helper-timeout-ms N` (the per-window begin/finish bound, default 20). The helper's own
flags: `--pack`, `--socket`, `--experts` (the experts GGUF shard; a relative path is joined to
`--pack`, so `--experts ../model.gguf` names the pack's neighbour), `--slots` (0: the budget decides),
`--reserve-mib`, `--layers`, `--device`, `--timeout` (idle exit before an engine connects, default
120000), `--begin-timeout` (default 60000, see *Limits*), `--once`, `--help`.

Scope, checked at start and said, not guessed: serve mode with a native (IQ) expert pack and
`--expert-profile` + `--expert-cache`; not with `--batch`, `--pipeline-windows>=2`, `--peer-device`,
`--expert-cache-device1..3`. Every layer is ELIGIBLE: the candidates are the profile's global rank
order over all layers, minus every pair a cache owns (the primary's and each split stage's — the
engine sends that owner mask with the handshake and the helper fills none of it), and Hadamard-folded
layers are skipped with a count (the rotation path stays the in-process tier's). The start line
prints the counts it skipped and why.

`serve/server.py` passes it through from the config: `"expert_helper_socket": "<path>"` (plus the
optional `"expert_helper_layers"`, `"expert_helper_slots"`, `"expert_helper_reserve_mib"`,
`"expert_helper_timeout"`). No key, no engine flag — the default path.

## What the channel carries

One AF_UNIX `SOCK_STREAM`, one framed message at a time, one outstanding request. A 24-byte header
(magic `STHH`, version 2, type, seq, request id, payload length) then the payload. Types: `kHello` /
`kHelloAck`, `kFill` / `kFillReport`, `kBegin` / `kRows`, `kBye`, `kError`. Writes use
`MSG_NOSIGNAL`; the descriptor is non-blocking, and every read and write is one poll with an absolute
`CLOCK_MONOTONIC` deadline, so a stalled peer costs a named timeout — never a hung engine thread,
never an unbounded read. The socket is 0600 and the peer must run as the same uid (`SO_PEERCRED`):
the helper answers `kError` and closes a peer of another uid.

`kBegin` carries the window's routed ids, the primary-residency bitmap and the window's `q8_1`
activations; `kRows` carries one dense row per claimed entry with the original entry index beside it.
The engine checks the reply as a whole before using any of it (a bad index, a duplicate, a row that
was not claimed, a non-finite value, a wrong count: the whole reply is refused, no row is applied).

A deadline given to the client API (`hello`, `fill_report`, `begin`, `finish`) is RELATIVE
milliseconds; the client turns it into an absolute stamp (`deadline_after`). Every call site reads
that way, and a bare relative number passed straight through expires at once — that is how a stall
looks, and the tests check it.

## Two channels (opt-in)

The window tier (the decode and verification rows, plus the drafter lane's frames) and the prompt
tier (the bulk prompt rows) are independent of each other, and on ONE socket they wait for each
other: each tier's frames queue behind the other tier's one outstanding request. Two measurements
this week named the cost: a speculative draft lane riding this socket cost −19.1% decode throughput,
its per-round round trip serialized behind the window traffic; and during a 78k-token prefill a
draft lane's bounded wait timed out twice with prompt-row frames and window frames queued together.

The split is two sockets, and the helper serves each on its own thread:

```sh
./build-cuda/strata-expert-helper --pack /models/... --socket /run/$USER/strata-expert.sock \
    --socket2 /run/$USER/strata-expert-prompt.sock --reserve-mib 512
./build-cuda/strata --serve ... --expert-helper-socket /run/$USER/strata-expert.sock \
    --expert-helper-socket2 /run/$USER/strata-expert-prompt.sock
```

Nothing about the protocol changes: the same frames, the same `kHello`/`kHelloAck` handshake on each
socket, `kFill` on ONE channel only, and both channels serve rows out of the one filled mirror. The
engine's window tier keeps the first socket, its prompt tier uses the second. The helper's expert
mirror, its staged activations and its drafter ring are shared, so one served frame takes a lock; the
lock is never held across a channel's idle read, which is what lets one channel's socket I/O overlap
the other channel's GPU work.

Each channel fails on its own. A dead prompt channel refuses the prompt tier by name and the prompt
rows are recomputed locally (at the next request the tier re-arms, on its own channel if it is alive,
else back on the primary one); the window tier never notices. A dead window channel is today's rule
for the window tier — claims zero rows, residency released — and the prompt tier keeps serving on its
own socket. The helper process exits when the primary channel ends, as it always did.

`--socket2` absent (the default, every shipped config) is one socket, one accept, one serve thread:
exactly the behaviour before this option. The end-to-end gain on hardware is NOT measured yet — the
two-channel A/B is the next window, and its numbers go here when they exist.


The helper reads the card's free memory at startup, keeps `--reserve-mib` free, keeps its own scratch
and window buffers out of the cache, and fills slots in the engine's profile rank order, bounded by
the engine's request — `--expert-helper-mib` (aligned slot bytes, summed exactly in rank order, not
`bytes / max_blob`) and `--expert-helper-slots` (a count) — and by its own budget: with neither cap
given, it fills as many of the ranked pairs as its free VRAM allows. The engine asks for slots, never
for a magic count. Its `kFillReport` is AUTHORITATIVE: the engine mirrors it and claims only what the
helper reported. An engine that asked for 3,400 slots and gets a report of 40 claims 40 — the rest of
the window stays on the CPU, which is the normal path. Nothing is re-asked per window, nothing is
evicted: the resident set is fixed for the run. A byte budget too small for even one aligned slot is a
named refusal at engine start, and the CPU path serves the whole run.

The whole context capacity stays the engine's. The helper takes no KV, no activation buffer for the
graph, no part of the context — only expert blobs on its own card.

## When the helper is late, absent, or gone

This is the shape the design asked for: a late helper is a CPU window, never a stall, and the
context, the top-k and the blend are the engine's own.

* Absent at start (no socket, a refused handshake, a geometry refusal): the engine says so
  (`expert-helper: ...`) and serves the whole run on the CPU path. No fatal error, no retry.
* Begin fails (the helper is mid-compute, the channel closed): the tier claims ZERO rows and
  SUCCEEDS. The dispatch's plan keeps every entry CPU-served, so this window is computed on the CPU
  exactly as it was without the helper, and the tier is out for the rest of the request.
* Finish fails (no reply, a late reply, a refused reply): the engine recomputes EXACTLY the rows the
  helper claimed, on the same CPU kernels the dispatch uses for CPU-served rows
  (`expert_helper_recompute_claimed`), disables the tier for the rest of the request, and counts
  those entries as CPU-served (`multi_entries`, `cache_refused`). A row is never written twice and
  never left unwritten.
* Reconnect: only at the next engine start. There is no in-run reconnect, no watchdog, no restart of
  a helper the person started.

The measured size of a CPU window (what a late helper costs) is in the P0 record: 6.75 ms per window
at 128k for the CPU dispatch, 1.24 ms of exposed stage-0 wait, on the geometry below. The per-window
bound is 20 ms by default.

Both fault shapes were run on live chains (see *What has been measured*). A helper killed mid-decode
closed the reply channel, fired exactly one claimed-work CPU recompute, and the request finished with
its full text; a later request on the same engine succeeded with no reconnect. A helper killed before
the prompt pass ended left the request intact and the helper with 0 rows, which is the correct shape
for an idle helper.

## Build it

```sh
cmake -S . -B build-cuda -DCMAKE_BUILD_TYPE=Release -DSTRATA_ENABLE_CUDA=ON \
      -DSTRATA_NATIVE_EXPERTS=ON -DSTRATA_BUILD_TESTS=ON -DSTRATA_BUILD_EXPERT_HELPER=ON
cmake --build build-cuda --target strata-expert-helper strata -j2
```

`STRATA_BUILD_EXPERT_HELPER` is OFF by default; the helper target compiles `helper_main.cpp` as CUDA
and links `strata_engine` + `strata_kernels_cpu` + the CUDA runtime (measured: `libcudart.so.13` is
the only GPU library its `NEEDED` list has). It is REFUSED under `STRATA_HIP_GFX906` (the sidecar is
CUDA-only; the HIP engine builds and runs the proxy client unchanged) and REFUSED when
`STRATA_NATIVE_EXPERTS=OFF` (the helper computes native IQ blobs). The host-side files the proxy uses
(`expert_helper_socket.cpp`, `helper_frame.cpp`) carry no device call: proved by `nm` on the CUDA, HIP
and CPU test objects (0 device symbols), not by a flag. The latest full HIP `strata`, CUDA `strata`
and CUDA helper builds completed with `-j12` (parent build logs).

The CPU-only tests run under CTest on Linux: `expert_helper_socket_test` (the framed transport's
faults and the q8_1 quantizer's golden pins), `expert_helper_fallback_test` (the CPU recompute,
bitwise against an independent all-CPU reference), `helper_microbench` (the channel's own cost). Each
built test binary is proven GPU-free with `readelf -d` and `nm` before it is run. The socket test
links `ggml-base` alone (ggml's fp16 conversion for the quantizer pin): no `ggml-cpu`, no
`ggml-cuda`, no `ggml-hip`.

## What has been measured, on what

**Hardware run, 2026-10-28.** Linux host, two Radeon VII cards under ROCm 6.1 (the HIP engine,
layers split at layer 32, context 262144) with an NVIDIA RTX 3080 Laptop as the CUDA helper, the
Swift 1.5 IQ3_XXS native expert pack. Parent records:
`.slim/deepwork/mixed-cuda-finish/r3-runtime-results.md` (chains, rows, faults),
`r3-production-dflash.md` (production recovery), `r3-regression-pins.md` (CPU pins),
`r1-remediation-p0.md`/`r2-gate.md` (numerical parity, integration gate).

### Helper compute on the card

* Fill on real VRAM: 3,400 slots, 6.37 GiB, `--reserve-mib 512`. That run used
  `--expert-helper-timeout-ms 100` and the helper with `--timeout 7200000 --begin-timeout 1800000` so
  a long prompt pass is not read as a dead engine. The 20 ms default was not swept and was not
  changed.
* Four fresh chains (OFF1/ON1/ON2/OFF2), two warmups plus three samples each. Across the six ON
  samples: 1,972 validated returned rows and 20,193,280 bytes, 0 timeouts, 0 fallbacks. The row and
  byte counters increment only after the reply passes validation
  (`ExpertHelperClient::finish`, `src/core/expert_helper_socket.cpp:327-335`), so they count returned
  rows, not residency, not begin requests. ON1 served 1,884 windows including warmups; ON2 is a
  separate fresh chain. The helper log binds the real peer, the native GGUF slot bytes and the CUDA
  grouped kernel.
* Every measured sample: HTTP 200, prompt 193 tokens, 186 cached, 256 generated, seed 271828,
  temperature 0. Both arms use the same candidate, the same layer split, context, reserves, drafter
  and timing instrumentation; the ON arm adds only the helper keys and stage paths.
* Decode rate, six samples per arm: OFF mean 34.683333 tok/s, wall 7.570039 s; ON mean 33.400000
  tok/s, wall 7.850988 s. The ON wall time is 3.7113% longer, not shorter. The arms' outputs are not
  byte-equal and the drafter acceptances differ (OFF 143/141/139, ON 135/135/137), so this is a
  description of the workload, not an output-equivalent compute ratio and not an accuracy result. No
  acceleration was observed on this pinned workload.
* Per-window GPU timing on the card has not been measured; the numbers above are end-to-end serve
  rates.

### Faults on live chains

* Helper killed mid-decode (`pidfd` `SIGKILL`, exit confirmed, output already flowing before the
  signal): the engine's reply channel closed, the claimed-work CPU recompute fired once
  (`src/core/expert_source.cpp:5237-5244` increments the counter only after a nonempty claimed set is
  recomputed locally), the request finished HTTP 200 with 692 tokens — the complete integers 1..200 —
  and a later 256-token request on the same engine succeeded with no reconnect. The engine does not
  log how many rows it recomputed.
* Helper killed before the prompt pass ended: the request survived (HTTP 200, 25 tokens) and the
  helper ended with 0 requests, 0 rows, 0 fallbacks, which is legitimate for an idle-helper death.
  The strict active-prefill gate is INCONCLUSIVE: numbered partial prompt progress appeared only
  AFTER the helper died. This is not an active-prefill PASS, not prefill offload, and not a claimed-
  row recompute proof.
* Capacity, from the parent's capacity records for this host: 66,574 prompt tokens cold; 132,922
  prompt tokens with 65,536 cached, coherent repeated-text responses. No cold 128k accuracy or
  performance claim. The raised `--begin-timeout` was used, not swept; it is not a measured optimum.
* Restoration after the six chains: clean, with no capture failures and unchanged config and launcher.
  Both full production recovery batches failed on the unchanged incumbent drafter
  (`dflash: non-finite candidate scores`, engine exit 1); its two successful samples decoded 35.2 and
  34.7 tok/s, inside the 33.3–35.5 baseline, so no clean recovery PASS. The service came back on a
  normal request (the server's lazy engine restart). Its last check returned a full 256-token response
  and health reported loaded. No production config, source, or binary change
  was made. The numeric cause and any link to this run are unproven, and the drafter path never
  routes through the helper.

### Numerical parity of the kernels the helper runs (CUDA)

* Real CUDA fixtures, all 48 layers, 10 format classes: finite outputs, max raw relative L1 1.82e-2
  against the ggml-cpu oracle (tolerance 3e-2), combine exact.
* Grouped-window parity (`native_grouped_parity`): 63 format/geometry cells × 84 calls, bitwise equal
  to the v1 reference, 0 failures, not skipped.

### CPU-only checks, this tree (Linux)

* `expert_helper_socket_test`: 107 printed check lines, 0 fails. It covers the framed transport's
  whole-or-nothing row reply, magic/version/size/seq wire faults, EOF, the 0600 and same-uid peer
  rules, the bind/unbind rules, the deadline cap, the 12 golden pins of the proxy's q8_1 quantizer,
  and four regression pins (the poll cap not being the deadline, the socket-path accept/refuse rule,
  the `kHello` claimed-bitmap unpack at 48×512, and `MSG_NOSIGNAL` under `SIG_DFL`). Each pin fails
  against the pre-fix code. The R2 count for this suite was 78 lines; 107 is the same suite with the
  pins added.
* `expert_helper_fallback_test`: 17 checks on this CPU. The CPU recompute is BITWISE the all-CPU row
  for the published native fixture format (`tests/data/native_experts/iq2_xs.txt` layer 0: gate/up
  IQ2_S + down Q2_0, blob 1,510,400 B, which `native_fmt` reproduces exactly): 9 claimed rows of an
  8-token window, rows the helper did not claim untouched, `d.job_of` reset, the counters counting
  each recomputed entry once, a missing blob latching the dispatch's failure path, the window
  contract refused, zero claims doing no work. Its canonical Q2_0 scenarios need the engine's AVX-512
  gate (`cpu_require_expert_support`) and are skipped on the AMD Ryzen 9 5900HX this tree is tested
  on; the engine serves native packs here.
* Acceptance tooling, CPU-blocked (no GPU, no service): 104 capture-suite cases
  (`tests/terminal/test_mixed_gpu_acceptance.py` plus the 18 helper/default-argv cases of
  `tests/terminal/test_expert_helper.py`) and 90 driver cases
  (`tests/terminal/test_mixed_gpu_acceptance_driver.py`). They capture chains and drive chains; they
  are not phase, speed, or acceptance evidence by themselves. The live ON chains carried the real
  engine argv through the config passthrough; the default path (no helper keys) adds no engine
  argument, which the argv-level tests pin.
* The channel's own cost, CPU-only socketpair, same `Socket` code both ends (`helper_microbench`,
  200 rounds): 0.019 ms per 10-row window, 0.056 ms per 40-row window, 0.106 ms per 80-row window
  (23.9 KB out, 819,552 B back); 5.5–7.9 GB/s both ways.
* The quantizer's golden pins are a REGRESSION pin of the CPU function: the exact bytes it writes for
  an all-zero block, a constant block (whose q = 127 and sum half = 16.0 are hand-derivable) and a
  mixed block (the +-127 saturation of q, the #606 clamp of the scale, a 2-block vector, the tail of
  whole floats). They pin THIS code against change. They do NOT claim the CUDA kernel's bits: amax, d
  and q are provably device-identical (max is associative; d is the same clamp; q depends on d alone),
  the fp16 SUM field is ULP-close only — the device reduces with the `shfl_xor` butterfly tree
  (o = 16,8,4,2,1), this loop accumulates in index order, and IEEE-754 addition is not associative.
  Device parity is measured on hardware by the helper's ROWS against the same seed, where the blend's
  documented tolerance is the expectation, not bit equality.
* Historical R2 note: that gate used builds and CPU tests, not NVIDIA hardware. R3 added real-card
  fill, handshake, returned expert rows and end-to-end HTTP. Per-window GPU timing remains unmeasured.

## Words the person sees

| What happens | The line |
| --- | --- |
| helper absent at start | `strata generate: the expert helper is absent; the CPU path serves its rows for the whole run (start strata-expert-helper BEFORE the engine: docs/MIXED_GPU_HELPER.md)` |
| proxy up | `strata generate: expert-helper proxy: N experts on /path, X GiB (fill report)` |
| Hadamard layers skipped | `strata generate: expert-helper: N ranked pairs skipped (Hadamard-folded layers, out of the helper's scope)` |
| what is eligible, counted | `strata generate: expert-helper: N of M ranked pairs eligible in layers 0..L (N held by a cache, N outside the window, N outside the geometry, N repeated, N Hadamard-folded)` — the pairs a cache owns (the primary's and each split stage's) are the ones removed, and the counts say so |
| what is asked for | `strata generate: expert-helper: asking N slots = B B aligned of M eligible (no byte cap: AUTO / the --expert-helper-mib N MiB cap[, bounded by --expert-helper-slots]); the helper's own free-VRAM budget decides the fill and its report is authoritative` |
| a byte budget buys nothing | `strata generate: expert-helper: the byte budget of B B buys no slot: the smallest aligned slot is S B; the helper is not started and the CPU path keeps serving its rows` |
| scope refused | `strata generate: --expert-helper-socket: <the rule it broke>` (the engine refuses the OPTION combination at start, before any model load) |
| per-call counters | the DONE line ends ` helper=reqs=N rows=N bytes=N timeouts=N fallbacks=N` — the numbers are that engine call's deltas, not lifetime totals |

## The split's cache spill (`STRATA_CACHE_SPILL=1`, opt-in)

A layer split gives every stage its own cache on its own GPU, sized from that card's free VRAM and
filled only from **that stage's own layers'** pairs (`src/program/generate.cpp:9061-9069` sizing,
`:9102-9115` fill).  Nothing looks at the sum of the cards' headroom, so the card holding the fewest
layers leaves its VRAM unclaimed: at `--layer-split 44` on two Radeon VIIs the first stage fills CUDA0
to 404 MiB free (3695 slots, 5.94 GiB) while CUDA1, owning 4 layers, sits with ~2.8 GiB nothing uses.

With the gate on, the engine registers **tier 0 as an in-process remote tier on the last stage's
device** (`cache_spill.hpp`, and the split's device assignment in generate.cpp) filled, with the card's
own auto byte rule (free VRAM less the tier's 512 MiB), from the pairs no cache holds.  The rows of
those pairs are then computed on that card by the same kernels the primary's hit path uses and return
through the pinned host rows - the shipped in-process tier's contract, measured on AMD in
SECOND_GPU.md.  Tiers are disjoint by construction: `RemoteExperts::open` skips pairs a stage claimed,
and the helper's `howned` bitmap lists the spill tier's pairs too, so the out-of-process helper fills
nothing the spill holds.  A pair lives in exactly one place, so a layer's claim order is unchanged;
what differs is where the bytes are and how the row comes back (device-local hit vs a row over PCIe),
which is a latency difference, not a numeric one.

Expected on this rig: ~1432 slots on CUDA1 (2.8 GiB minus 512 MiB at the rig's 1 724 416 B slot), so
3695 + ~1432 = ~5127 slots ≈ 8.2 GiB - at or above what split-32 had.  The gate also accepts the
helper beside an in-process tier (the scope refusal keeps naming every tier the caller names by hand),
and the per-request re-arm seam in `src/core/expert_source.cpp` re-arms every proxy, not only index 0.

**Costs, stated plainly:** the tier's fill reads ~2.3 GiB of blobs into CUDA1 at startup (a few
seconds of boot); `--vram-elastic` and `--kv-grow` are off with tiers (they are off for a split
anyway); `--pipeline-windows` is excluded with tiers (it is off by default for a 2-stage split);
concurrency stays what it is today - the helper's scope excludes `--batch` and pipeline windows, so
one window is in flight and the tier's per-window state needs no lock.  Unset, every start is
byte-for-byte as before: the gate is read at one place, and nothing else runs.

## Limits and known gaps

### Optional whole-pair recovery (local Linux launchers)

The local mixed production launchers accept `--recover`. This runs
`tools/mixed_gpu/pair_supervisor.py`. If a helper process or channel ends, it stops
the owned server and engine, starts a fresh pair, and fills the helper again.
Without `--recover`, launcher behavior is unchanged. There is no live-cache
reconnect. A killed helper has lost its GPU allocations.

Recovery interrupts the active request and clears the live conversation cache.
The supervisor uses PID/start-time checks, an exclusive lock, bounded cleanup,
socket-inode checks and retry backoff. Its `attached` state means the helper has
filled and connected; it is not an HTTP readiness or performance measurement.
The supervisor itself must stay running. Persistent hardware faults may prevent
recovery. This does not repair an AMD driver hang.

On 2026-10-31, a local test on two Radeon VII cards and an RTX 3080 killed the
helper between requests. A fresh pair restored both channels and 4000 slots
(6.07 GiB) in 89.3 seconds. Six matched long-prompt probes succeeded without helper
timeouts or fallbacks. Median generated rate was 26.0 tok/s before and 25.8 after
(99.2%). These were short, 17–18-token generations with Swift 1.5 IQ3_XXS and a
Q4_0 DFlash drafter, not a sustained-throughput or active-request fault test.
Evidence: `build-mixed-gpu/cap1-validation/rec2-recovery-status.json` and the
associated request, reply, engine and helper logs. The CPU supervisor, private
window and A/B suite passed 105 tests.

### Engine-level limits (without launcher recovery)

One helper, one card, one request at a time; a fill that takes tens of GB takes tens of seconds at
startup and the engine waits for it (it is the person's own hand-started step); no in-run reconnect;
no per-window re-fill (the resident set is fixed); no Hadamard-folded layers; no canonical (non-IQ)
pack — the helper computes native IQ blobs; no Windows path (AF_UNIX: Linux only, the client says so
on other platforms).

**The 20 ms default bound is a starting point, not a measured optimum.** The measured hardware run
used 100 ms. The sweep is on the hardware list; the number for 20 ms is not measured.

**One timeout ends the helper's part of the run (by design).** The same `--expert-helper-timeout-ms`
bounds BOTH the window's begin write and its rows read, and every timeout closes the channel: the
tier is out for the rest of that request and, because the engine opens the channel once at startup and
never reconnects, for the rest of the RUN — a single hiccup on a busy card leaves the CPU serving the
helper's rows from then on, counted in the DONE line
(`helper=reqs=… rows=… bytes=… timeouts=… fallbacks=…`). Re-arming needs an engine restart. The six
measured ON samples recorded zero timeouts; that is not a sweep of the bound.

**The sidecar's `--begin-timeout` must exceed the longest prompt chunk.** The prompt pass never
touches the channel (prefill is excluded from the helper), so a prompt pass longer than the sidecar's
`--begin-timeout` (default 60000 ms of channel silence) is read as a dead engine: the helper exits
mid-request, the next window's begin send fails, and the proxy is off for the rest of the run. For
64k/128k runs, start the helper with `--begin-timeout` well above the longest prompt chunk you expect.
The long-context run used `--begin-timeout 1800000` and survived the prompt pass; that shows the
raised value works on that geometry, it is not a sweep and not an optimum.

**Linux only, and the Windows CUDA engine build is unverified (likely broken).** The three CPU-only
sources the proxy needs (`src/core/expert_helper_socket.cpp`, `tools/mixed_gpu/sidecar/helper_frame.cpp`)
are compiled into `strata_engine` for every CUDA/HIP tree, unguarded, and they include POSIX headers
(`<unistd.h>`, `sys/socket.h`, `sys/un.h`, `sys/uio.h`, `fcntl.h`); only `open_socket` refuses
non-Linux. No Windows or macOS build of that path has been run, so that build is UNVERIFIED static
reading and expected to fail to compile until those sources are guarded
(`if(CMAKE_SYSTEM_NAME STREQUAL "Linux")`) or given a Windows stub. Treat the helper as Linux-only.

**Not measured, not claimed.** A release-versus-candidate byte-identical output run; per-window GPU
timing; bitwise equality of helper rows against the in-process tier on hardware; `--once` and
idle-exit behaviour on a busy card; the 20 ms timeout sweep; the strict active-prefill gate (see
*Faults on live chains*: INCONCLUSIVE); SYCL (not configured in these buildtrees, not built); any
acceleration, and any production DFlash fix.
