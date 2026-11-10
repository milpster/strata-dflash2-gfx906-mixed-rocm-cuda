# Split-40 production restore and allocator investigation — 2026-11-10

## User-directed restore

The user requested split 40, the latest built engine and helper, and the production
conversation disk cache. This is an explicit operational choice, not a claim that
split 40 has passed a conclusive same-build short/long-context TG comparison.

- Branch: `allocator-placement`; pre-existing benchmark outputs and unrelated journal edits are untouched.
- Candidate source worktree: `/mnt/srcds/Strata-wt-helper-ipc`, clean at `894669db7`.
- Build evidence: `build-mixed-gpu/cache-spill-build.log` ends with both L3 builds green.
- Engine build mtime: 2026-11-10 18:58:17 +01:00.
- Helper build mtime: 2026-11-10 18:50:28 +01:00.
- Fixed production copies: `/mnt/srcds/Strata/v0216/build-mixed-gpu/{rpcw-hip/strata,rpcw-cuda/strata-expert-helper-patient}`.
- Engine SHA-256: `00691ac86aabb021b11bac784d10a1c6be6e064c12a8ec126876db82dde24efa`.
- Helper SHA-256: `0a340b99d550cfb7666203824929a90ca4951e8db2c0d7e2cdcad2ee5ab66ad6`.
- `cmp` verified both production copies against the source build artifacts.
- Engine runtime links resolve to ROCm 6.1 (`libhipblas.so.2`, `libamdhip64.so.6`) and the custom gfx906 `librocblas.so.4`.
- The existing v0215 Python serving wrapper remains in use. Only engine/helper binary paths and the layer split change.
- `STRATA_CACHE_SPILL=0` is explicit. No experimental cache-spill path is promoted.
- KV remains fp16. VII reserves remain 256/256 MiB. Helper launch arguments remain 4600 slots, fill 6796 MiB, reserve 300 MiB; actual effective fill/reserve must be read from the helper boot log.
- Production disk cache remains `/mnt/srcds/Strata/data/conversation-cache`, budget 24576 MiB. No scratch cache is substituted and no existing cache data is deleted.
- Previous config saved as `prod-config-swift15-f16kv-dflash-w4-q4_0-mixed.json.bak-20261110-before-split40-latest`; previous v0215 binaries remain available.

## Evidence reset for the investigation

Earlier conversation and journal entries contain contradictory GPU mappings and
unsupported performance explanations. Treat those explanations as hypotheses,
not established causes. In particular, the DFlash cross-GPU tap log is not proof
of an expert-spill bottleneck; expert-slot counts must distinguish primary,
later-stage, and helper residency; request contamination must be ruled out rather
than attributed to automatic disk replay without tracing request admission.

First audit the allocator, residency/dispatch paths, reserve precedence, and
actual physical GPU mapping from source. A read-only specialist may run only
after production serves successfully. GPU builds/tests are parent-owned and
start only after that specialist has reached a verified terminal state.

## Startup verified at 20:03–20:04 +01:00

- Supervisor PID 1374568 is attached to engine PID 1375327 and helper PID 1374628.
- `/proc/1375327/exe` and `/proc/1374628/exe` resolve to the v0216 release copies; both live-file SHA-256 hashes match the recorded build hashes above.
- The engine command line contains `--layer-split 40`, `--kv fp16`, and the original production disk-cache directory/budget/idle-save flags.
- `/health` reports `status: ok` and `loaded: true`.
- A bounded temperature-zero request completed at 20:03:33; response saved in `run/split40-latest-smoke.json`. It generated four reasoning tokens and hit its output limit, establishing serving readiness, not answer quality or comparative speed.
- Serving wrapper and pair supervision remain alive. The production launcher is detached from the transient tool shell.
- Runtime launch log: `logs/prod-split40-latest-20261110-launch.log`; current wrapper/helper logs are recorded in the supervisor JSON.

## Independent read-only audit dispatched

Oracle session `ses_e39265907ffeBOcur6PrWcbEKK` owns the static allocator/reserve/dispatch review.
It may read source and logs only: no file edits, compilation, test requests,
benchmarks, or production lifecycle operations. The parent owns verifying its
cited artifacts and any subsequent GPU experiment. No benchmark runs concurrently
with this audit, and production must remain up for its model calls.

No comparative performance conclusion is claimed by this restore.
