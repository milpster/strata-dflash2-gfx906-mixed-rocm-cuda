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

## Lane + longtg

Running into `bench/ab-v0210/rpcprof/out/ab9-iq3s-split34-20261110/`
(warm / 8000×3 / 80000×3 / tg1024 / longtg 160k×384). Judge lines and verdict
appended below when complete.

## Comparison targets (iq3xxs split-34, spill OFF, baseline helper)

PP 8k 221.9 (222.5/221.9/204.1), PP 80k 318.7 (317.6/318.7/319.7),
tg1024 44.3/46.1, longtg 41.2/43.2 @29k ctx, longtg PP 436.2.
