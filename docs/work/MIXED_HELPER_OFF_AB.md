# Mixed backend: helper-less vs canonical A/B (windows how1/how2)

Question (user): "is the helper really doing anything?" - the RTX 3080 shows ~4% util in
nvidia-smi while the VIIs do the work. This is the first direct helper-vs-no-helper A/B on
this rig: same production binaries, same day, only the helper's presence differs.

Note: the adopted +13.6% (slots4200/fill6400, 2026-11-01) was a helper-configuration delta
(with-helper vs with-helper), never a helper-vs-no-helper number. This window is the direct
measurement the earlier work never ran.

## Rig and method

- 2x Radeon VII (gfx906) layer split 0-31/32-47, RTX 3080 RPC expert helper (both sockets)
  on the canonical arm; helper-less arms reuse `helper_ab`'s validated OFF derivation (both
  socket keys absent - "the shipped byte-identical default path: the engine never creates
  the helper client").
- Window `how2` (how1 crashed on a probe-gate TypeError; fix 72235489; evidence retained),
  2026-11-02 20:55-21:06, arms a/c = helper-less (no helper process at all, supervisor
  helper_pid 0, attach on the server's own "ready: http://" line), arm b = canonical
  supervised pair, 3 matched long probes/arm (10,471-token prompt, ~17 generated tokens).
- Gates: helper-less arms prove zero helper requests AND zero prompt-tier closes; the
  canonical arm proves positive helper traffic; request-SHA parity across all arms (greedy
  tokens must not change); control drift 0.9-1.1.

## Measured

| arm | helper | decode gen_rate median (tok/s) | infill read_rate median (tok/s) |
|---|---|---|---|
| a | **off** | 30.7 | 236.9 |
| b | on (canonical) | 31.8 | 235.2 |
| c | **off** | 31.0 | 237.1 |

- ON/OFF decode ratio **1.031** (+3.1%) -> HELPER_GAIN_SMALL (below the +5% "confirmed" bar).
- Infill ratio 0.993 (prefill neutral, within noise).
- Control drift 1.010 (a vs c - the most stable window of the session).
- Engagement proofs: helper-less arms 0 helper requests every probe, 0 prompt-tier closes;
  canonical arm 725 window requests / 3,345 rows / ~33.8 MB total, 0 timeouts, 0 fallbacks.
- Request-SHA parity held on every rep: the helper changes WHERE expert rows are computed,
  never the tokens.
- Restore: canonical supervised production restored, completion smoke 200.

## What it means

The helper IS doing real work on every request (roughly 13% of the routed expert rows over
PCIe, ~250 window requests per probe) and it costs nothing measurable in prefill. But on
THIS probe shape the measured decode value is ~3%, inside the per-rep noise band
(individual reps ranged 26.2-40.2 tok/s within single arms; each probe decodes only ~17
tokens in ~0.5 s after a ~44 s prefill). The honest statement: batch-1 decode bursts this
short cannot resolve the helper's contribution sharply; sustained decode (real chat
generations of hundreds of tokens) is where its 13% row share would compound, and that
shape was not measured here.

## Limits

Measurement A/B only - no adoption decision, no config change (production is already
canonical with the helper). Probe decode samples are tiny by design (matched-window
discipline); a decode-heavy probe variant would sharpen both this number and the
split-overlap candidate. Evidence: `build-mixed-gpu/cap1-validation/how1-*` (crash) and
`how2-*` (status, per-arm engine logs, manifests, replies).
