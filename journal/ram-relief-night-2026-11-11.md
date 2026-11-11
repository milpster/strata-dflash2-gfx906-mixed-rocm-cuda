# RAM-relief night — iq3xxs/iq3s occupancy push (2026-11-11, in progress)

Goal: highest safe occupancy (helper + engine reserves) for iq3xxs (split 38) and
iq3s (split 36), spill ON/OFF, each gated by 250k infill + QA. Winners → prod.

## Landed tonight (engine, branch helper-ipc-diet)

- `089b6efcf` allocator placement fix → v0217 (validated: gate no longer tanks PP)
- `d82068115` spill-tier pairs join `--expert-helper-ram-relief` → v0218
- `38d91a17c` stage-cache pairs join the relief → v0219 (rpcw-*-fix3)
  - engine f53a0b7804f62a7283da7420425df71b95599721947862f9f3717a510b089f35
  - helper 2accdd5dd3db77d6bf107f1a2ef7e63ad3c4a1d55673bbe20ea8d4b3f9ffa9a5

## Helper static border (iq3xxs, slots at free-mib 7496)

| reserve | slots | free after |
|---|---|---|
| 512 | 4130 | 442 MiB |
| 256 | 4335 | 442 MiB |
| 128 | 4405 | 312 MiB |
| 0   | 4484-4504 | 185-186 MiB |

Budget rule confirmed: budget = free − reserve − scratch(672000 B) − buffers(180098776 B);
the budget binds, not fill-mib. Border = reserve 0, fill 7400, slots 5000.

## Stage relief boot accounting (M1b: v0219, split-38, spill OFF, --adapt-swaps 0)

- `13832 pairs total (23533 MiB) dropped` = helper 4504 (7348 MiB) + stage 9328 (16185 MiB)
- `+9328 stage-cache pairs (16185 MiB) join the drop set` — lever-1 works at boot
- engine RSS 37.45 GiB (was ~48.7 pre-stage-relief), helper 7.84 GiB
- sys: available 14 GiB (was ~2-3)

## Discovered: adaptive tier gate

Stage-cache relief only fires with `--adapt-swaps 0` (adaptive caches keep host
copies by design: they swap slots at runtime and need the pages). With it off,
stage caches are pure pinned (boot fill). All matrix configs (m1-m4, i1-i3) now
carry `--adapt-swaps 0`. Trade-off: no heat adaptation over long sessions; the
profile-ranked fill encodes standing heat. Lane + QA to validate.

## REGRESSION (open): v0219 fresh-request refill failure

- Any FRESH /v1/chat/completions → HTTP 400 `refilling a lent slot failed:` (err EMPTY)
- Reuse-heavy path works: idle-save verification (106331 tok = 105640 reused + 691 read) succeeded post-drop
- Control: v0217 (and v0218) serve fresh requests fine
- Print sites generate.cpp:20532/20616/20623; fixer owns the debug (fix4 build incoming)
- Interim: A3 v0217 pair hosts agents on :8009 (fresh-request probe 200)

## Spill verdict so far (iq3xxs, lanes)

Split-40 ON: 8k +6.8% but 80k −4.2%, TG not clean. Split-42 ON dominated.
Spill is dead for serving on iq3xxs unless matrix says otherwise; tier rows cost
PCIe hops (hit rate up, 80k/TG pay).

## Matrix plan (paused on regression)

M1 spill-off 256/256 · M2 spill-on · M3 128/128 · M4 64/64 — all split-38,
helper border, v0219-fixed, D-lane + standing infill250k + ram_map + disk-tier
check per arm. Gates: no amdgpu swap fallback / #2065, no stalls, 0-reuse on
measurement legs (unique per-leg tags). QA: SHA re-runs same-boot + cross-restart
(reuse expected on SHA legs; timing legs stay fresh-tagged).

Prompt/conversation disk caching locked in everywhere: RAM tier 4096 / disk 24576 /
idle-save / park-gain 4096 / --prompt-cache-spread, identical in every arm;
per-boot + post-lane disk-tier line (sidecars/orphans/corrupt/budget) recorded.
