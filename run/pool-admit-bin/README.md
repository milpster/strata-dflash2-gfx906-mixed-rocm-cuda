# pool-admit-bin — the build prod runs after the 2026-11-21 lease-accounting fix

Engine: `rpcw-hip/strata` = worktree `helper-ipc-diet` @ `9358921670` (borrow only for a copy:
`available()` probe, and note-or-release at every swap-in site;
CPU-thread loans are given back by a scope guard, and `STRATA_POOL_BORROW_TRACE=1` prints the
borrowing caller's frames when the guard fires; prefill asks `pinned && !transient`,
so a pair it believes pinned is never borrowed from the pool), built in
`build-906-fixadmit` with the four flags a fresh gfx906 dir needs (see the
strata-tuning skill: DFlash on, HIP include, gcc-14, tests). sha256 in SHA256SUMS.

Helper: symlink to `v0233/build-mixed-gpu/rpcw-cuda/strata-expert-helper-patient`
(the frozen `553f3bf4…` winner helper).

Rollback: `run/winner-pin-38g` (`d534e72a…`) in the worktree, plus the config
and launcher one commit back (they name this directory only).
