# SPEC-prereq — launcher env pass-through for helper profiling (no GPU, no prod restart)

## Root cause (verified by reading the code)
`launch-prod-swift15-f16kv-dflash-w4-q4_0-mixed.sh:75` boots the helper with a **clean environment**:
`env -i STRATA_HELPER_REPLY_TIMEOUT_MS=600000 PATH="$PATH" HOME="$HOME" TERM="${TERM:-dumb}" "$HELPER" ...`
so `STRATA_RPC_PROFILE` (and any future helper-side env) exported by a window shell never reaches the helper.
The engine at `:99` uses `env -u CUDA_VISIBLE_DEVICES -u STRATA_ARENA_MMAP -u GPU_PINNED_MIN_XFER_SIZE
-u HSA_USERPTR_FOR_PAGED_MEM -u LD_PRELOAD ...` and inherits the rest, so engine-side
`rpcprofile: claim ...` lines work. This is why AB7 collected engine spans but zero
`rpcprofile: serve ... queue_us=... lock_us=...` lines.

## Change (mechanical, all six launchers)
Replace the literal `env -i <vars> "$HELPER"` with an array built from the same four vars, appending any
helper-profile vars present in the parent:

```
helper_env=(STRATA_HELPER_REPLY_TIMEOUT_MS=600000 PATH="$PATH" HOME="$HOME" TERM="${TERM:-dumb}")
for v in STRATA_RPC_PROFILE STRATA_VERIFY_PROFILE STRATA_HELPER_REPLY_TIMEOUT_MS; do
  [[ -n ${!v:-} ]] && helper_env+=("$v=${!v}")
done
env -i "${helper_env[@]}" "$HELPER" ...
```
(The explicit `STRATA_HELPER_REPLY_TIMEOUT_MS=600000` stays the default; a parent value overrides it by
appearing later in the array — verify `env -i` last-wins semantics, and if it is not last-wins, drop the
literal and keep only the parent value with the literal as the fallback default computed in bash.)

Files: `launch-prod-swift15-f16kv-dflash-w4-q4_0-mixed.sh`, `launch-prod-swift15-f16kv-dflash-w4-q4_0-mixed-radeonvision.sh`,
`launch-prod-swift15-dflash-w4-q4_0-mixed.sh`, `launch-prod-swift-iq3_s-dflash-w4-q4_0-mixed.sh`,
`launch-prod-swift-iq3s-f16kv-dflash-w4-q4_0-mixed.sh`, `launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh`.

## Validation (all CPU-only, prod untouched, no GPU)
1. `bash -n` on all six launchers; `git diff --check`.
2. Env-chain proof: run the helper with a non-existent device (`--device 99`) under the new `env -i` array with
   `STRATA_RPC_PROFILE=1` exported, and assert (a) it prints its normal startup lines, (b) `/proc/<pid>/environ`
   (captured before exit, or via a wrapper `env -i ... sh -c 'cat /proc/self/environ'`) contains
   `STRATA_RPC_PROFILE`. This is the same GPU-free pattern used before (`--device 99`).
3. Guard-only launcher test (the KSM precedent): export an `ss` function that always reports port 8009
   occupied; launcher must refuse at the port guard **before** starting helper/server/supervisor state writes.
   Run with and without `STRATA_RPC_PROFILE` exported; outputs byte-identical to the pre-change launcher when
   the var is unset (opt-in invariant).
4. No prod process is signaled, restarted, or reconfigured. Receipts → `logs/rpcprofile-env-<ts>-*.log`.
5. Commit on a fresh branch off `main` (e.g. `rpc-profile-env`), conventional message:
   `fix(prod): pass profile env to the mixed helper`.
