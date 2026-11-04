# --expert-helper-ram-relief: TG fix (W1a+W1b) — results, 2026-11-04 evening

Box: 2x Radeon VII (gfx906, ROCm 6.1.0) + RTX 3080 helper; 62 GiB RAM. Model
Swift-1.5-Qwen3.8-Flash-Next IQ3_XXS 40 GiB packs. Build: branch tap-overlap-f1f2,
W1a 1167bd6d (lock-free residency pre-check) + W1b f4e0bc3d (dropped-only forwards),
binaries 19:53. Same evening, same binary for V5/P0; stock C separate build.
Gates: TG >= 33.1 tok/s (report.py filter read<400, generated>400), 80k sha
1c7d4a6d9be118d8 byte-identical, engine anon reduced vs P0, drop line non-zero.

## Results (TG = per-segment median over all kept boots)

| arm (boots)            | TG median | 88k tok/s | 80k decode | probe/80k sha | engine anon | drive disk reads |
|------------------------|-----------|-----------|------------|---------------|-------------|------------------|
| V5 relief+fix (r1,r2)  | 37.65     | 552.9     | 38.6       | ok / ok       | 42.1 GiB    | 2.31 GiB (r1)    |
| P0 winning (r7)        | 38.05     | 576.5     | 38.5       | ok / ok       | 47.4 GiB    | -                |
| C stock (r3)           | 27.6      | 550.0     | 23.4       | ok / ok       | 46.2 GiB    | -                |
| V4 relief unfixed (r7) | 16.0      | 503.3     | 5.4        | ok / ok       | 41.2 GiB    | ~40 GB           |
| V4 relief unfixed (r8) | 20.1      | 438.8     | 18.3       | ok / ok       | 41.2 GiB    | ~40 GB           |

TG segments V5: r1 34.5/35.9/36.5/37.9/39.9/40.8, r2 36.7/36.7/37.6/37.7/38.1/39.5
(tight, no bimodality; unfixed V4 swung 6.3-31.7). Drop line both V5 boots:
4116/4116 pairs (6.24 GiB), complement 3033 ranges / 33.73 GiB. Zero helper
timeouts/fallbacks, no #267 wedges, no drive retries in any V5/P0/C run.

## Verdict

- W1a+W1b remove the relief TG loss: V5 reaches 98.9% of the winning-config control
  (37.65 vs 38.05) and beats stock by 36%. The unfixed arm was 2.3-2.4x slower.
- The RAM goal holds: engine anonymous 42.1 vs 47.4 GiB (-5.3 GiB, -11%); the drop
  line is unchanged. 4.3 GiB of reclaimable file cache (pack pages) rides along.
- Quality byte-identical on every gate (probe sha 709bd8fb, 80k sha 1c7d4a6d...).
- Root cause fixed (measured): the relief file machinery was prefetching pack ranges
  of RESIDENT experts (~40 GB/drive of storage reads at 100-500 MB/s during TG legs);
  after the fix the whole drive reads 2.31 GiB from disk (peak window 40 MiB/s).
- Remaining deltas vs control: 88k -4.1% (552.9 vs 576.5); probe leg slightly slower.
  Fixes B (staged LRU) / C (helper piggyback) / D (boot fadvise) held in reserve.

Adoption is a user decision. Evidence: logs/{V5-r1,V5-r2,P0-r7,C-r3,V4-r7,V4-r8}-*.log,
out/V5-r1-io.csv, out/V4-r8-io.csv, JOURNAL.md 2026-11-04.
