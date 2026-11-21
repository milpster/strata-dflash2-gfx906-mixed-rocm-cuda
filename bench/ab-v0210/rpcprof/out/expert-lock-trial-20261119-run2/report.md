# Expert-page lock trial — runs 1 & 2 reconciliation (2026-11-19)

Live prod `:8009` throughout; no downtime, no engine/config/binary change. Engine PID 3538773,
helper 3537319, config `95ef3693…`, engine `b0ec0109…`. Probes: `prod_judge.sh` (same prompt,
500 max tokens, decode tok/s from the engine log). Pressure: `ram_pressure.py`. Lock:
`expert_shard_locker.py` (external page-cache `mlock` of the *resident* extents of the CPU-side
expert source shards 00002/00003; PLE, dflash and the unmapped shard 00004 never touched).

## Run 1 (out/expert-lock-trial-20261119/) — mechanics + fit, pressure legs invalid

- Legs A (17.9 mean) / B locked (17.2) / C locked (22.7) / D unlocked (21.7) — but both pressure
  legs ran with the holder already exited (hold-file bug: `ram_pressure.py` expects the caller to
  create it) and 24 GiB cap was below what the box absorbs. C/D are *no-pressure* numbers.
- Proven: lock mechanics (8 781.6 MiB locked, `Mlocked` 161→8 942 MiB, clean release to 161 MiB);
  MemAvailable falls ~1:1 with locked bytes; floor guard (4 096 MiB) stops partial; no TG penalty
  from the lock itself.
- Discovered: the engine's RAM relief (`--expert-helper-ram-relief`) releases engine memory nearly
  1:1 against external allocation — synthetic 4.5 GiB target unreachable; realistic floor ~7 GiB.

## Run 2 (out/expert-lock-trial-20261119-run2/) — corrected: holder stays alive through probes

| Leg | State | avail | tok/s (sequence) | Engine RSS |
|---|---|---|---|---|
| E0 anchor | unlocked | 16.9 GiB | 22.6 / 23.7 / 23.2 | 46.6 GiB |
| E1 locked | 12 957 MiB locked | 4.2 GiB | 22.9 / 12.8* / 23.0 | 47.3 GiB |
| **E2 locked + pressure held** | lock held | 5.0 GiB | 22.9 / 23.5 / 16.4 / 23.0 / 23.4 / 20.5 | **47.6 GiB stable** |
| **E3 unlocked + pressure held** | holder ~10 GiB | 7.0 GiB | 11.4† / 16.1 / 18.8 / 19.5 / 19.9 / 23.1 | **39.4 GiB (relief dumped ~8 GiB)** |

\* 365-token early stop. † 105-token early stop (excluded from trend reading, kept in the record).

### Verdict

**The expert-page lock protected generation speed under real, held memory pressure.**

- E2 (locked, 5.0 GiB avail): steady ~23 tok/s, engine hot set intact (RSS flat 47.6 GiB, swap
  untouched), two dips to 16.4/20.5.
- E3 (unlocked, 7.0 GiB avail — *more* available than E2): relief dumped ~8 GiB of engine memory,
  expert pages evicted, TG fell to 11–19 tok/s with the classic refault recovery ramp
  (11→16→19→20→23) and only reached parity on the last request. The eviction/churn — not the
  absolute avail level — is what cost speed.
- Lock cost: ~1:1 MemAvailable (12 957 MiB locked → avail 16.9→4.1 GiB), floor-guarded; the lock
  itself measurably cost nothing at steady state (E1 ≈ E0).

### Honest limits

- Single run per condition, small identical prompts, decode-only metric; no quality measurement.
- E2's low avail came from the lock itself (holder allocated nothing: already below target); E3's
  came from the holder after a relief dump — pressure *mechanisms* differ even though both legs
  ran at sustained low avail. E3's higher avail yet worse speed makes the eviction explanation
  stronger, not weaker.
- Coverage: extent locking covers the resident subset of shards 00002/00003; tensor-level
  expert-only coverage is inferred from the serving path, not proven. Fit bound measured: max lock
  ≈ 8.8–13 GiB depending on starting avail, at a 4 GiB floor; with the game running (3–5 GiB) the
  lockable maximum shrinks accordingly.
- Not a same-baseline candidate comparison: prod binary unchanged; this is evidence for the mlock
  lane, not a promotion input by itself.

Evidence: `trial.log`, `memory.log`, `probe.log`, `locker.log`, `pressure*.log` in both out dirs;
probe rows also appended to `out/gpu-rounds-20261116/spd-prod-judge.csv`. Independent run-1
monitoring analysis: `../expert-lock-trial-20261119/analysis-nemotron.md` (Nemotron monitor).
