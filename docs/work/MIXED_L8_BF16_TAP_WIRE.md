# L8: bf16 draft taps on the wire — measured, no gain

The drafter's tap rows cross the PCIe wire as fp32: up to 100 MiB per context frame
and about 400 KB per verify round. L8 halved that by sending them as bf16 instead
(commit `b92fb705`): two new frame kinds (`kDraftCtxBf16`/`kDraftReqBf16`), a new
`HelloAck` feature bit (`kFeatureDraftBf16`), round-to-nearest-even packing on the
engine and scalar expansion on the helper. Everything is opt-in and default-off:
with `STRATA_DRAFT_TAP_BF16` unset, or a peer without the bit, both ends send and
accept exactly today's fp32 bytes on today's frame kinds.

## What was measured

Window `l8w1` on 2026-11-02, 19:51–20:10 (`tools/mixed_gpu/slots_window.py
--draft-probe --draft-l8`): arms local / remote-fp32 / remote-bf16 / local at the
canonical 4200 slots / fill 6400 MiB, both sockets, on the freshly built l8
binaries (CUDA helper `ace6bf8f`, HIP engine `8b618cda`, `ctest -R helper` 5/5 in
each tree). The two remote arms draft remote-only with identical footprints
(3325 slots / 5.08 GiB — the drafter's weights displace experts by the same
amount), so only the wire encoding differs between them; the local arms (4093
slots / 6.25 GiB) bracket the session. Three long probes plus one 78k-token XL
probe per arm; identical request bytes on every arm (SHA parity, long and XL).

The remote drafter was verifiably engaged on both remote arms: 109,944 context
rows and 238/242 draft rounds served over the wire. Zero helper fallbacks and
zero timeouts on every arm; control drift 1.057.

| measure | local (a, d) | remote fp32 (b) | remote bf16 (c) |
|---|---:|---:|---:|
| decode median (long) | 30.05 tok/s | 29.10 tok/s | 27.50 tok/s |
| c-vs-b decode ratio | — | — | 0.945 |
| XL decode rate | 33.2 / 32.5 tok/s | 31.6 tok/s | 31.4 tok/s |
| XL whole-request wall | 182.2 / 171.5 s | 175.9 s | 240.6 s |
| draft acceptance | 0.43 | 0.474 | 0.467 |

**Verdict: no gain (L8_NO_GAIN).** The gate was ≥+5% decode for bf16 over fp32 on
the remote arms; bf16 came out 5.5% *slower*, and its XL request took 37% longer
end to end (240.6 s vs 175.9 s). Precision was not the problem — acceptance
moved only 0.7 points (0.467 vs 0.474, inside the 2-point guard). The cost is
time: packing ~1.4 billion taps per XL prompt through the scalar
round-to-nearest-even loop on the engine, and expanding them back one by one on
the helper, costs more CPU than the halved PCIe bytes ever saved. The tap
transfer was never the bottleneck — the same conclusion as the L1 remote-drafting
bracket (5.86% slower remote) and the L3 profile (no lock contention).

## Status

- Not adopted; `STRATA_DRAFT_TAP_BF16` stays unset in production and in every
  harness default. The frame kinds, feature bit and conversion code stay in the
  tree behind the env gate, default-off.
- Build lesson recorded (`e00a92ec`): deriving an HIP tree needs `build_l3.sh`'s
  ROCm pins (`CMAKE_PREFIX_PATH`/`ROCM_PATH`/`HIP_PATH`/`HIP_ROOT_DIR` on
  `/home/user/rocm-gfx906-xnack` + `/opt/rocm-6.1.0`); without them the
  configure wanders into the newer `/opt/rocm` headers and dies in
  `amd_device_functions.h`. Failed-build evidence kept in `l8-build.log`, the
  green retry in `l8-hip-rebuild.log`.
- Evidence: `build-mixed-gpu/cap1-validation/l8w1-status.json`,
  `l8w1-progress.log`, per-arm configs/launchers/manifests alongside. Production
  was restored after the window (supervisor 1634932, smoke generation HTTP 200).
