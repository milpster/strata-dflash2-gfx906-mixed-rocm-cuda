# Operator directives for the serving windows on this box

Standing, applies to every lane until the operator says otherwise.

## VRAM margins when a card actually runs out (2026-11-21)

If a GPU genuinely exhausts its VRAM during a window (driver paging warning plus a failed or
stalled request, `no_buffer_space`, or an engine/helper abort blamed on VRAM), raise THAT
card's margin by 64 MiB and re-run the leg. One step per exhaustion, not a blanket raise:

- CUDA0 (first Radeon VII): `--vram-reserve-mib` 192 -> 256 -> 320 ...
- CUDA1 (second Radeon VII): `--vram-reserve-later-mib` 192 -> 256 -> 320 ...
- RTX 3080 (expert helper): `--reserve-mib` is clamped 64..4096; the current floor of 64 is
  the operator's chosen lowest margin, so it moves only on the operator's word.

Thin-but-working is NOT exhaustion and does not change the margin: the 40 GiB window sat at
2 MiB free on CUDA0 and 251 MiB on the 3080 and still decoded at 20.1 tok/s. The engine's own
"add --vram-reserve-mib 484" hint is advice, not the rule; the rule is +64 per real failure.

Margins are edited in the window's config `args` (they are recorded there), never by editing
the running binary or a booted process.

## Which model a lane runs on (operator, 2026-11-21)

- **One active subagent at a time.** Never two, never a second spawned to "speed things up".
- **Work that can run while prod serves goes to swift** (`llamacpp/swift-1.5-iq3_xxs`, served by
  this box's own engine on 8009). Code, tests, builds, docs: those lanes ride prod.
- **Work that needs prod down goes to deepseek** (`tokenharbor/deepseek-v4.1-flash-1017`), never to
  swift: a swift lane dies with the engine it is thinking on. Anything that stops, swaps or boots
  the serving pair is a deepseek lane.
- The parent decides the model per lane and states it when dispatching. A lane never restarts
  prod itself; if a step inside it would take prod down, the lane stops and reports instead.
