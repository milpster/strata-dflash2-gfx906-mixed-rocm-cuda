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
