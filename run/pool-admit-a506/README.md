# Rollback copy: the engine that serves today (`a506e620`)

An exact copy of `run/pool-admit-bin/` as it stands while prod runs it, kept so a rollback needs no
build directory.

| | |
|---|---|
| Engine | `rpcw-hip/strata`, sha256 `a506e62008ba4bc4c767f2368c5ca6ace757567b1b186dee579a796c36ecf382` |
| Helper | `rpcw-cuda/strata-expert-helper-patient`, sha256 `553f3bf48219c99298e4620547507eac7eefa29d660ee32e806bb9780a9ab361` |
| Worktree commit | `9358921670` on `helper-ipc-diet`, pushed to `fork` |
| Provenance | `run/pool-admit-bin/README.md` |

What this binary has behind it, measured 2026-11-21 (journal `pool-admission-fix-2026-11-21.md`):

- two concurrent-stress legs (6,000-token long request + six helpers): 0 of 7 failed both times,
  genuine overlap 174.8 s and 347.5 s, borrower guard silent, no refusals;
- one eight-turn parked conversation, 45,929 → 70,486 tokens: all 8 turns answered, 26.8–29.0 tok/s
  by the engine's own lines, guard silent.

Restore with:

```sh
./stop-all.sh
rsync -a run/pool-admit-a506/ run/pool-admit-bin/ && ./launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh
```

Separate commands on purpose: a boot command's text contains the launcher path, and any
pattern-kill in the same invocation matches it.
