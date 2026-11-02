# Mixed-backend work checkpoint

## Git policy

This work uses the `work/mixed-cuda-finish` branch, based on `26631e4b`.
The user requested a checkpoint of the existing work and commits for each completed
change on 2026-11-02. This replaces the earlier instruction not to commit.
Keep source, tests and documentation committed. Build products, raw local evidence
and agent caches stay outside Git under the repository's ignore rules.
Do not push, publish or adopt production changes without separate authorization.

## Checkpoint limits

This is an in-progress snapshot, not a release or numerical signoff.

- The retained L1 remote-drafting comparison was 5.86% slower. It was not adopted.
- The retained M5 relative-error p99.9 was 0.00582918448879, above the unchanged
  0.005 limit. Its maximum, 0.014444641279, was below the 0.02 limit.
- M5 covered eight fixtures, layers 32–35 and three of ten format pairs. It did
  not establish complete layer/format coverage or tensor NMSE.
- Legacy recovery is removed; the auditor requires full schema-v2 JSON. The real
  CMake CPU test passed, followed by 50 Python regressions against that binary.
  Full CUDA and gfx906-HIP builds passed, with five helper tests in each tree and
  the HIP CPU-report test. CPU tests do not establish GPU accuracy.
- The earlier full-backend build was cancelled during HIP configuration; its
  partial tree and log are retained. All four protected binary hashes still match.
- The fresh build's final source guard caught two added Python imports. The failed
  guard was retained. Compiled inputs matched; the current auditor passed all 50
  regressions against the HIP CMake binary. No backend rebuild was needed for imports.
- Actual 80k-context plus 5k-output acceptance and a practical larger-model or
  constrained-host-RAM comparison remain open.

Canonical production recovery remains
`/mnt/user/Strata/launch-prod-swift15-dflash-w4-q4_0-mixed.sh --recover`.
Recovery on 2026-11-02 was verified with `/v1/models` HTTP 200, a separate
content smoke returning `READY`, 4093 helper slots and the second channel attached.
The initial 16-token smoke returned reasoning only; that result was retained.
Use the custom `~/rocm-gfx906-xnack` rocBLAS before `/opt/rocm-6.1.0`.
Do not overwrite protected build trees, relax numerical limits, install/download
dependencies, reload the GPU driver or automatically adopt candidates.
