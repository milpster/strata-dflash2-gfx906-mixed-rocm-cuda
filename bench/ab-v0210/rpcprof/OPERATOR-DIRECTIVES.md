# Operator directives for the serving windows on this box

Standing, applies to every lane until the operator says otherwise.

## VRAM margins when a card actually runs out (2026-11-21)

If a GPU genuinely exhausts its VRAM during a window (driver paging warning plus a failed or
stalled request, or an engine/helper abort blamed on VRAM), raise THAT
card's margin by 64 MiB and re-run the leg. One step per exhaustion, not a blanket raise:

- CUDA0 (first Radeon VII): `--vram-reserve-mib` 192 -> 256 -> 320 ...
- CUDA1 (second Radeon VII): `--vram-reserve-later-mib` 192 -> 256 -> 320 ...
- RTX 3080 (expert helper): `--reserve-mib` is clamped 64..4096; the current floor of 64 is
  the operator's chosen lowest margin, so it moves only on the operator's word.

**`no_buffer_space` / `No buffer space available` is NOT a VRAM signal** (corrected 2026-11-21).
It is errno ENOBUFS, produced only by the RAM pool (`ram_pool.cpp` pool_alloc, the pool cache's
LRU); CUDA calls a full card `out of memory`. It appears as
`refilling a lent slot failed: No buffer space available` because the refill loop takes
`g_pool_expert_store->acquire()` directly. A margin step cannot answer it, and a larger CUDA0
margin makes it worse (smaller GPU expert cache -> more pairs sent to the pool). The
2026-11-21 ladder 384->640 was taken on this misreading; treat it as provisional.

Thin-but-working is NOT exhaustion and does not change the margin: the 40 GiB window sat at
2 MiB free on CUDA0 and 251 MiB on the 3080 and still decoded at 20.1 tok/s. The engine's own
"add --vram-reserve-mib 484" hint is advice, not the rule; the rule is +64 per real failure.

Margins are edited in the window's config `args` (they are recorded there), never by editing
the running binary or a booted process.

## No concurrency testing (operator, 2026-11-21, "once and for all")

Do not run, rebuild or cite parallel-request load legs. `concurrent-stress.py` is retired
(`RETIRED-concurrent-stress.py`, banner in the file). A pass or fail under manufactured
concurrency says nothing about the product and costs a prod window to learn nothing.

The shapes that judge this box, one lane at a time: one long prompt (infill then
generation), one parked multi-turn conversation, one real agent session. Real agent traffic
may of course overlap by itself - that is the workload, not a test we construct.

## Which model a lane runs on (operator, 2026-11-21)

- **One active subagent at a time.** Never two, never a second spawned to "speed things up".
- **Work that can run while prod serves goes to swift** (`llamacpp/swift-1.5-iq3_xxs`, served by
  this box's own engine on 8009). Code, tests, builds, docs: those lanes ride prod.
- **Work that needs prod down goes to deepseek** (`tokenharbor/deepseek-v4.1-flash-1017`), never to
  swift: a swift lane dies with the engine it is thinking on. Anything that stops, swaps or boots
  the serving pair is a deepseek lane.
- The parent decides the model per lane and states it when dispatching. A lane never restarts
  prod itself; if a step inside it would take prod down, the lane stops and reports instead.

## Queued for the operator (2026-11-21, awaiting a word)

Each of these is a decision or a window the agent does not take alone.

1. **Promote the engine.** Prod runs `run/pool-admit-bin/rpcw-hip/strata`, sha256 `a506e620`
   (worktree `helper-ipc-diet` @ `9358921670`). Two concurrent-stress legs passed on it with the
   borrower guard silent; the rollback pin `d534e72a` in `run/winner-pin-38g` is untouched.
1b. **The merged engine is armed and unproven at runtime.** `run/merge-bin/` (engine `5a699be1`,
   helper `553f3bf4`) is `main` + `helper-ipc-diet` (`159e3097ef`, pushed to `fork`), built and
   tested but never booted: the merge brings `main`'s tail-skip, `STRATA_NATIVE_MLOCK` and helper
   IPC work together with the pool work for the first time. Activate with
   `rsync -a run/merge-bin/ run/pool-admit-bin/`, revert with `rsync -a run/pool-admit-a506/ ...`
   (both READMEs name the swap); either is a stop then a boot, separate commands.

2. **Measure the CUDA0 margin back down.** 640 was raised on the misread ENOBUFS class. The
   question is what the card actually needs now that the pool leak is fixed: 512 (the value
   before the ladder) then 448, each on the same 6,000-token / six-helper leg, one boot per
   step, keeping whichever passes twice. Not taken unilaterally because it lowers a prod margin.
3. **The engine-exit amplifier.** A request-level pool failure still ends the engine process
   (`refill_issue` prints and returns 1), so one refused request takes every concurrent request
   with it (`exit code 1`, five dropped connections). Failing the request and keeping the
   process is a separate, riskier change than the lease fix, and it is the next outage shape
   even with the pool clean.
