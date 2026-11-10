# Swift IQ3_S champion-config trial — 2026-11-10

## Setup transfer (the "old iq3s config" brought to champion standard)

The v0210-era `launch-prod-swift-iq3s-f16kv-dflash-w4-q4_0-mixed.sh` +
`prod-config-swift-iq3s-f16kv-dflash-w4-q4_0-mixed.json` were regenerated as exact
copies of the current swift15 (iq3xxs) champion files, changing only model identity:
pack `/mnt/srcds/ai/iq3s`, GGUF
`/mnt/srcds/ai/Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S.gguf`, sockets, log and
state names, `model_name swift-iq3_s-f16kv`. Everything else transferred 1:1:

- v0216 pinned binaries: engine `00691ac8…24efa`, helper `0a340b99…66ad6`
  (hashes re-verified against prod pins before boot)
- `layer_split 34` (split-sweep winner), `--vram-reserve-mib 256`,
  `--vram-reserve-later-mib 256`, `STRATA_CACHE_SPILL=0`
- conversation cache 4096 MiB + disk tier (24576 MiB, idle-save), vision
  max_tokens 2048, KSM opt-in block, `--begin-timeout 63115200000`
- helper: engine `--expert-helper-reserve-mib 256` + launcher `--fill-mib 7200`
  (the latest occupancy arm; h384 measured a wash on iq3xxs so the helper-config
  delta vs the champion lane is expected ≈ 0)
- `run_lane.sh` now takes `AB9_STATE` (default unchanged) so lanes can target
  either supervisor state file.

Note: the iq3xxs champion numbers (below) were measured with baseline helper
(fill-mib 6796, engine reserve 512); the h384 arm showed identical PP/TG at
4253 slots, so the 7200/256 helper settings are treated as a non-factor.

## Boot accounting (split-34, v0216, spill OFF)

Boot ~13 min (larger pack fill). Layer split 0-33 / 34-47 confirmed.

| | iq3xxs-15 (champion) | iq3_s |
| --- | ---: | ---: |
| blob size per slot (approx) | ~1.55 MiB | ~1.86 MiB |
| helper fill (budget stop, 6.89-6.92 GiB) | 4426 slots | 3712 slots |
| resident experts (token graph) | 9415 (4971 CUDA0 + 4444 CUDA1) | 7858 (CUDA1: 3918) |
| prompt tier borrow | — | 3681 CUDA0 + 3369 CUDA1 |
| VII free loaded (M0/M1) | 404/504 MiB | 398/498 MiB |
| helper eligible pairs | 15124 | (engine-ranked, iq3_s routing) |

iq3_s's larger blobs buy precision but cost ~17% resident pairs and ~16%
helper slots at identical byte budgets.

## Lane + longtg results (out/ab9-iq3s-split34-20261110)

Tokenizer caveat: swift-iq3_s tokenizes the same prompt bytes to ~9% fewer
tokens (8000 B -> 1497 tok vs 1648; 160000 B -> 28912 vs 29063; warm 512 B ->
142 vs 293), so per-token tok/s under-reports iq3_s; per-request walls compare
identical bytes.

| leg (same bytes) | iq3xxs s34 wall | iq3_s s34 wall | delta |
| --- | ---: | ---: | ---: |
| 8000 ×3 | 7.54/7.54/8.22 s | 7.32/7.20/7.03 s | ~5% faster |
| 80000 ×3 | 47.02/46.87/46.71 s | 70.21/99.82/66.11 s | +42..113% slower |
| tg1024 | 13.83/10.41 s | 64.64/18.61 s | prefill blowup |
| longtg fresh | 77.04 s | 219.35 s | +184% slower |
| longtg cached rerun | 10.95 s | 23.20 s | +112% slower |

Tok/s: PP 8k 208.8/212.8/216.7; PP 80k 209.3/146.9/222.4 (erratic);
longtg fresh PP 139.2 vs 436.2; cached-run prefill 5 tok in 4566 ms (1.1
tok/s) vs 204 ms on iq3xxs — misses dominate even the 5-token continuation.
Decode: tg1024 23.6/35.9 (vs 44.3/46.1); longtg 37.0/36.8 (vs 41.2/43.2);
warm first request 22.7 tok/s (cold page cache, 32.8 s wall).

Attribution: iq3_s blobs ~1.86 MiB/slot (vs ~1.55) → resident pairs 9415 →
7858 and helper slots 4426 → 3712 at the same byte budgets; elevated expert
misses route to the helper/CPU paths and dominate long prompts and decode.
80k repeats stayed slow (98.8 s worst), so a warmed page cache does not close
the gap. The shared expert-profile ranking was derived from 1.5-model traffic
and may also mis-rank 1.0-model routing (untested hypothesis).

## Split sweep for iq3_s (32 / 34 / 36), all v0216, spill OFF, helper 256/7200

The first split-34 lane ran cold (first iq3s boot since Nov 6): warm leg 32.8 s,
80k 70-100 s. Subsequent boots share a warm page cache, so 34 was re-run warm
for a fair three-way. Stalls = the known erratic disk/userptr class (legs
marked below).

| | split-32 | split-34 (warm) | split-36 |
| --- | ---: | ---: | ---: |
| resident pairs | 7893 | 7858 | 7811 |
| helper slots | 3721 | 3712 | 3705 |
| PP 8k tok/s (median) | 197.3 | 215.9 | **235.3** |
| PP 8k walls s | 7.49-7.87 | 7.01-7.21 | **6.38-6.52** |
| PP 80k tok/s (clean legs) | 287.0/288.6 (+141.9 stall) | 295.0/297.1 (+114.0 stall) | **318.3/318.6/310.0** |
| PP 80k walls s | 51.2-51.5 (+103 stall) | 49.8-50.0 (+128 stall) | **46.4-48.6** |
| tg1024 decode | 33.8 / 40.9 | 39.0 / 34.1 | **38.1 / 38.8** |
| longtg walls s (fresh/cached) | 114.0 / 52.2 | 86.4 / 54.0 | **114.9 / 13.7** |
| longtg fresh PP tok/s | 282.2 | 391.0 | 352.7 |
| longtg decode (fresh/cached) | 36.3 / 17.6 | 36.3 / 19.8 | **33.5 / 34.7** |
| stall legs in lane | 2 | 2 | **0** |

(34's longtg cached decode 19.8 and 32's 17.6 are stall-contaminated: the
5-token cached prefill took 38.2 s / 32.6 s respectively, vs 213 ms on 36.)

**iq3_s best split = 36**: fastest or tied on every clean metric, the only
stall-free lane. Unlike iq3xxs (TG peak at 34), iq3_s favors more primary
layers — its bigger blobs shift the CUDA0/CUDA1 room tradeoff.

## iq3_s-36 vs iq3xxs-34 (the promotion decision, operator's call)

| | iq3xxs-34 (champion) | iq3_s-36 |
| --- | ---: | ---: |
| PP 8k wall s | 7.54-8.22 | **6.38-6.52** |
| PP 80k tok/s / walls | 318.7 / 46.7-47.0 s | 318.3 / 46.4-48.6 s (tie) |
| tg1024 decode | **44.3 / 46.1** | 38.1 / 38.8 |
| longtg decode | **41.2 / 43.2** | 33.5 / 34.7 |
| resident pairs | 9415 | 7811 |

iq3_s-36 matches 80k PP, wins small-prompt PP, and gives back ~13-19% TG.
Unmeasured: IQ3_S quality advantage and cross-model answer quality.

## Split-38 probe (does higher help iq3_s further?)

| | split-36 | split-38 |
| --- | ---: | ---: |
| resident pairs | 7811 | 7676 |
| PP 8k tok/s (median) | 235.3 | 239.4 |
| PP 80k tok/s (clean legs) | 318.3 | **326.0/326.3** (+212.4 stall leg, 68 s) |
| longtg fresh PP / wall | 352.7 / 114.9 s | **374.3 / 90.0 s** |
| tg1024 decode | **38.1 / 38.8** | 33.8 / 34.2 |
| longtg decode | **33.5 / 34.7** | 32.8 / 32.7 |

Answer: PP keeps inching up (80k +2.4%, 8k +1.7% over 36 — gains flattening:
+9.4% at 34→36 vs +2.4% at 36→38) while TG drops ~10% at small context.
Same PP/TG tradeoff iq3xxs showed at 36→38. Split-40 not probed: gains are
flattening, residency keeps falling, and split-40 is exactly where the
allocator pair-cap bug strands later-stage VRAM (fix still pending) — a
boot there belongs after the allocator fix, if ever needed.

**iq3_s ranking: 36 best balanced (PP≈38 within ~2%, TG 10-15% better than
38); 38 only if small-context PP outranks TG.**

State after this probe: iq3s serving at split-38 (this boot); promotion
decision pending.




## Comparison targets (iq3xxs split-34, spill OFF, baseline helper)

PP 8k 221.9 (222.5/221.9/204.1), PP 80k 318.7 (317.6/318.7/319.7),
tg1024 44.3/46.1, longtg 41.2/43.2 @29k ctx, longtg PP 436.2.
