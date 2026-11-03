# The helper is what changes the answers

Question: the mixed product (two Radeon VII for the engine, an RTX 3080 running the CUDA
expert helper) returns different text from the pre-helper product on the same requests
(window `sab3`: 0 of 3 requests match, first-window KL ≈ 0.0037 nats). Three things
differ between the products: the engine build, the helper, and the server. Which one
causes it?

Answer, measured (window `iso3`, 2026-11-03): **the helper**. The mixed engine and the
mixed server, with the helper client removed, reproduce the pre-helper product exactly.

## What ran

Same frozen `ppf1` request bytes as `sab3` (three prompts of 10,465 tokens, 512 new
tokens each, temperature 0, thinking off), first-logits dump on every arm. The middle
arm was new: the production-mixed engine (`rpcw-hip`, the binary production runs) and
the worktree server, but with the helper DISABLED - the validated helper-off
derivation removes both helper socket keys, so the engine never creates the helper
client and no helper process runs at all (the launcher records helper pid 0). Startup
proofs confirmed: mixed binary, no helper seam in config or log, same layer-split
geometry, FP16 prompt GEMMs on, ready line 262144.

## Results

| arm | engine | helper | messages vs stock |
|---|---|---|---|
| a stock | pre-helper `build-dfsample-1` | none | — (baseline) |
| b mixed-helper-off | production `rpcw-hip` | **none** | **3 of 3 byte-identical** |
| c stock | pre-helper `build-dfsample-1` | none | died before probing (see below) |

Arm b's first-window logit rows are bit-identical to arm a's: every one of the 248,320
floats equal in all three requests (KL 0.0, largest single-logit difference 0.0). The
identical messages also match `sab3`'s stock arms byte-for-byte.

Contrast with `sab3`'s helper-full arm (same engine, same server, helper ON): 0 of 3
messages match, KL ≈ 0.0037 nats, largest single-logit difference 2.34. So:

- engine build: exonerated (mixed engine, helper off → stock-identical)
- server: exonerated (the worktree server served the matching answers)
- helper traffic: the cause

The KL comparison in this window was computed after the fact by the parent from the
retained dump files, with the harness's own math (the harness's in-window comparison
never ran - see the incident below); it is recorded evidence only, never a gate.

## The closing-arm incident

Arm c, the second stock load, answered its health checks and started its first probe,
then the engine hit a GPU verify timeout: "verify: timed out at layer 10; its GPU waits
were released but the GPU did not finish within 5 s (#267)", after a startup note that
only 790 MiB of VRAM was free with the model loaded (the third consecutive engine load
in the window). The server returned 503 and the window ended without the harness's own
bracket comparison. This does not touch the decision evidence: arms a and b completed
all three probes each with full startup proofs, and stock determinism is established
independently - `sab3`'s in-window stock bracket was byte-identical 3 of 3 with
bit-identical dumps, and the stock arms of `sab3`, `iso2` and `iso3` (five independent
loads in total) all returned the same three messages.

Production was restored by the runner and re-audited: health 200, context 262144
loaded, canonical engine environment (`STRATA_HIP_PROMPT_F16=1`,
`STRATA_PREFILL_HELPER=1`), supervisor attached, no leftover ports or processes.

## What this settles, and what it does not

Settled (measured, this window):

- The divergence between the mixed product and the pre-helper product comes from the
  helper's participation - some experts being computed on the RTX 3080 over the CUDA
  channel instead of on the Radeon VIIs. Remove the helper and every measured byte
  matches stock.

Not settled:

- WHICH part of the helper's participation (transport rounding, the bf16 taps, cache
  residency split, timing-dependent adaptive placement, or something else). That needs
  helper-internal levers, a separate design.
- Whether the helper's answers are "worse" - only that they differ. No quality claim
  is made or needed here.
- The pooled numerical relative-error gate remains FAIL (it compares helper-full
  against helper-less numerics); nothing in this window changes it.
- Speed: no claim. The window was instrumented by the logit dump (timings are not gain
  evidence), and the helper-off arm is not the production configuration anyway.

Three prompts, one machine, one day. Everything is retained under
`build-mixed-gpu/cap1-validation/iso3-*` (and `iso1-*`/`iso2-*` for the earlier
attempts: `iso1` was cancelled by a reboot with no arms, `iso2` was ended by a harness
proof bug - fixed in `63396fd` - whose stock arm nonetheless re-confirmed stock
determinism). No settings were adopted.
