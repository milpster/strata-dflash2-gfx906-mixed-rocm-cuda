# Mixed backend: STRATA_SPLIT_OVERLAP A/B (window ovw1)

Question: the two Radeon VII cards alternate at 99-100% during batch-1 decode instead of
overlapping. `docs/MULTI_GPU.md` pins the cause - "By default each verify window runs the
stages one after another: the host syncs stage 1, then ... launches stage 2's graph" - and
ships an opt-in lever, `STRATA_SPLIT_OVERLAP=1`, measured upstream only on 2x RX 7900 XTX
(gfx1100): decode 53 -> 70.1 tok/s (code) and 45 -> 55.9 (prose). This window is the first
gfx906 (Radeon VII) datapoint, on the mixed backend (2x Radeon VII + RTX 3080 expert helper).

The companion lever `--pipeline-windows 2` is architecturally excluded beside the RPC expert
helper (`generate.cpp` helper scope: "--pipeline-windows>=2 is excluded (two decode stages,
one channel)"), so it cannot be measured on this setup without engine+helper source work.

## Rig and method

- 2x Radeon VII (gfx906, ROCm 6.1.0 pins) layer split 0-31 (CUDA0) / 32-47 (CUDA1), RTX 3080
  running the RPC expert helper (both sockets), canonical identity slots 4093 / fill 6.25 GiB.
- Window `ovw1`, 2026-11-02 20:28-20:39, arms a/b/c = OFF/ON/OFF, 3 matched long probes per
  arm (10,471-token prompt, ~17 generated tokens, private port 18080).
- Production binaries untouched - the lever is engine-env only, delivered by the arm-b
  launcher export (`export STRATA_SPLIT_OVERLAP=1` after the scrub, before the server start;
  the server's `child_env` passes `dict(os.environ)` plus config env to the engine). OFF arms
  export nothing and are byte-identical to production.
- Gates: engagement proof in the arm-b engine log, request-SHA parity, zero helper
  timeouts/fallbacks, control drift 0.9-1.1, ON/OFF decode median ratio >= +5%.

## Engagement proof

Family engine logs (raw engine stderr; the `/mnt/user/Strata/logs/window-*` copies are the
server narrator view and do not carry these lines):

- arm a: `strata serve: layer split: layers 0-31 (CUDA0), 32-47 (CUDA1), one hand-off per window`
- arm b: `... one hand-off per window (overlapped: STRATA_SPLIT_OVERLAP=1)`
- arm c: same as a (no suffix)

## Measured

| arm | split_overlap | decode gen_rate median (tok/s) | prefill read_ms median |
|---|---|---|---|
| a | off | 32.2 | 44,453 ms |
| b | **on** | 32.7 | 44,596 ms |
| c | off | 30.7 | 44,468 ms |

- Pooled OFF (a+c) 30.7 vs ON (b) 32.7 -> ratio **1.065** -> OVERLAP_CANDIDATE by the
  pre-registered >= +5% gate.
- Control drift a -> c = 0.953 (inside the 0.9-1.1 band, but a downward trend); b vs the
  adjacent control a is only +1.6%. The honest effect estimate is between +1.6% and +6.5%.
- Prefill unchanged (44.5 s both arms) - expected, the lever only changes the decode
  hand-off.
- Helper traffic identical across arms per probe (~250 window requests, ~1,100 rows, ~11 MB;
  prompt tier 192 requests / 45,424 rows per arm), 0 timeouts, 0 fallbacks, request-SHA
  parity on every rep.
- Restore: canonical supervised production restored (completion smoke 200).

## Limits of this measurement

Each probe decodes only ~17 tokens in ~0.5-0.7 s after a ~44 s prefill, so the decode sample
per arm is three short bursts; upstream's +24-32% was measured on sustained decode. The +6.5%
pooled ratio crosses the gate but sits inside the window's own drift band. A confirmation
window with decode-heavy probes (more generated tokens per probe, more reps) is required
before any adoption consideration.

## Verdict

OVERLAP_CANDIDATE (weak): engagement proven, gates passed, no adoption. The env stays unset
in production; nothing default-on changed in the tree. Evidence:
`build-mixed-gpu/cap1-validation/ovw1-*` (status, per-arm engine logs, manifests, replies).
