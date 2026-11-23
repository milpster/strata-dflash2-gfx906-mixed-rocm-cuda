#!/usr/bin/env python3
"""port_ab_contract.py - the frozen numbers of the Phase-2 A/B contract, in one place.

Everything the windows are gated on lives here: the byte targets of the frozen corpus, the
per-window leg plan (legs per boot), the pre-registered deltas, the claim rule and the
prerequisite-0 thresholds. port_ab_prepare.py writes this into the manifest; the leg runner
and the pre-window checker read the same values, so a number cannot drift between them.

Sources (plan/measurement-contract-speed-attribution-faults.md, approved 2026-11-23):
  * byte target 323,200 B -> 239,663 tokens (the one same-prompt pair; observed ratio
    323,200 / 239,663 = 1.3486 B per token, ctx-threshold.sh documents ~0.742 tok/B);
  * prefill 287.1-348.2 tok/s and decode 21.6-22.1 tok/s measured on the 239,663 prompt;
  * boot 3-5 min (full-residency init);
  * pre-registered deltas: prefill 5 %, decode 8 %; claim level 90 % (n=3/arm, exact/HL);
  * prerequisite 0: >=15 GiB MemAvailable pre-boot, non-engine RSS <=8 GiB, no non-pinned
    process above ~50 % of a core on the stage cores, >=40 GiB free for the cache dir.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------- corpus shapes
# bytes are what champions_prepare.build_paired_prompt takes (exact byte length); tokens is
# the count the engine reported for that byte target on the 239,663 pair.
BYTES_PER_TOKEN = 323200 / 239663


def bytes_for(tokens: int) -> int:
    return int(round(tokens * BYTES_PER_TOKEN))


SHAPES = {
    "pp239k": {"tokens_expected": 239663, "bytes": 323200, "head_tag": "champs work=pp239k",
               "use": "W1 prefill rate, W3 prefill half of the 240k decode leg"},
    "dec144k": {"tokens_expected": 144000, "bytes": bytes_for(144000),
                "use": "W2 144k decode (one prefill + 1024 generated), W4 2x2 decode shape"},
    "pp2k": {"tokens_expected": 2000, "bytes": bytes_for(2000),
             "use": "W2 short-PP 2k and the TTFT leg's turn-1 prompt"},
    "pp8k": {"tokens_expected": 8000, "bytes": bytes_for(8000), "use": "W2 short-PP 8k"},
}

# Generation caps and request kinds per leg. kind=prefill is one request: a fresh prefill
# plus the generation cap; the judge line reports both rates.
LEGS = {
    # kind=tg issues the SAME prompt twice: request 1 is the fresh prefill, request 2 the
    # cached re-run. The decode rate is request 2's gen rate (the contract's "request 2 of the
    # boot, 1024-token cap"); request 1's read rate is the prefill, already owned by W1.
    "pp239k": {"kind": "prefill", "kind_by_window": {"W1": "prefill", "W3": "tg"},
               "max_tokens": {"W1": 8, "W3": 1024}},
    "dec144k": {"kind": "prefill", "kind_by_window": {"W2": "tg", "W4": "tg"},
                "max_tokens": {"W2": 1024, "W4": 1024}},
    "pp2k": {"kind": "prefill", "max_tokens": {"W2": 32}},
    "pp8k": {"kind": "prefill", "max_tokens": {"W2": 32}},
    "ttft3": {"kind": "ttft", "turns": 3, "max_tokens": 64},
}


def kind_for(shape, window):
    return LEGS[shape].get("kind_by_window", {}).get(window) or LEGS[shape]["kind"]

# The request body every leg sends (pins sampling + reasoning; the driver adds messages and
# max_tokens only). Matched across arms by construction; the driver hashes it per leg.
REQUEST_TEMPLATE = {
    "temperature": 0,
    "enable_thinking": False,
    "reasoning_effort": "low",
    "stream": False,
}
TTFT_TEMPLATE = {**REQUEST_TEMPLATE, "stream": True}

# ------------------------------------------------------------------------------- windows
# reps is n per arm (operator directive 2026-11-23: 3, not 5). Interleave is the boot order
# inside every window: A B B A A B (balanced, no arm boots twice in a row at the ends).
REPS = 3
# (arm, rep) in boot order: A B B A A B, with each arm's three replicates numbered 1..3
INTERLEAVE = ("A", "B", "B", "A", "A", "B")
INTERLEAVE_PAIRS = (("A", 1), ("B", 1), ("B", 2), ("A", 2), ("A", 3), ("B", 3))

# legs_per_boot names how many measured legs one boot carries. W2 has two boot shapes:
# a decode boot (144k prefill + 1024 generated) and a short boot (2k PP, 8k PP, 3-turn
# TTFT). Every boot gets its own fresh cache dir; every leg's prompt carries a head tag, so
# its judge line must read "0 reused" even inside a shared boot.
WINDOWS = {
    "W1": {
        "name": "prefill rate, 239,663-token frozen corpus",
        "legs": {"pp239k": {"max_tokens": 8}},
        "legs_per_boot": {"pp239k": 1},
        "boots_per_arm": REPS,
        "warmup_boots": 2,          # one discarded first-touch boot per arm (not in the count)
        "budget_s": [5400, 9000],   # contract: 1.5-2.5 h
        "gate_metric": "read_tps",
        "delta": 0.05,
    },
    "W2": {
        "name": "144k decode + short-PP 2k/8k + 3-turn TTFT",
        "legs": {"dec144k": {"max_tokens": 1024}, "pp2k": {"max_tokens": 32},
                 "pp8k": {"max_tokens": 32}, "ttft3": {"turns": 3, "max_tokens": 64}},
        "legs_per_boot": {"dec144k": 1, "pp2k": 1, "pp8k": 1, "ttft3": 1,
                          "_boot_shapes": {"decode": ["dec144k"],
                                           "short": ["pp2k", "pp8k", "ttft3"]}},
        "boots_per_arm": REPS * 2,  # 3 decode boots + 3 short boots per arm
        "warmup_boots": 2,          # the pilot boots double as the discarded first-touch boots
        "pilot": {"boots_per_arm": 1, "shape": "short",
                  "note": "short-PP/TTFT pilot legs run first; their deltas are frozen into the "
                          "manifest before the claim legs and are excluded from the claim sample"},
        "gate_metric": "gen_tps / ttft_s",
        "delta": {"gen_tps": 0.08, "ttft_s": None},
        "delta_note": "decode 8 % pre-registered; short-PP and TTFT deltas come from the pilot "
                      "section of the manifest, frozen before the claim legs",
        "budget_s": None,           # derived below (W2 had none until legs-per-boot was frozen)
    },
    "W3": {
        "name": "240k decode (each boot carries its own 239,663 prefill)",
        "legs": {"pp239k": {"max_tokens": 1024}},
        "legs_per_boot": {"pp239k": 1},
        "boots_per_arm": REPS,
        "warmup_boots": 2,
        "budget_s": [4320, 7200],   # contract: 1.2-2 h
        "gate_metric": "gen_tps",
        "delta": 0.08,
    },
    "W4": {
        "name": "2x2 engine x server cell (veto-only)",
        "legs": {"dec144k": {"max_tokens": 1024}},
        "legs_per_boot": {"dec144k": 1},
        "cells": ["A-engine+B-server", "B-engine+A-server"],
        "controls": ["A-engine+A-server", "B-engine+B-server"],
        "controls_note": "the two same-arm cells are already measured by W1-W3; W4 spends its "
                         "budget on the two cross cells so an engine claim can be vetoed",
        "boots_per_cell": 2,        # optional 3/cell (min p 0.10) if the budget allows
        "warmup_boots": 0,
        "budget_s": [2700, 3900],   # contract: ~1 h
        "gate_metric": "gen_tps",
        "delta": None,
        "claim": "VETO ONLY: with 2 boots/cell min exact p = 0.33; the cell can reject an engine "
                 "claim (the effect follows the server, or the cells disagree) but can never "
                 "establish one. Pair attribution stands until a cell vetoes it.",
    },
}

# --------------------------------------------------------------------- measured anchors
PREFILL_TPS = (287.1, 348.2)   # slowest, fastest (239,663-token pair)
DECODE_TPS = (21.6, 22.1)
BOOT_S = (180, 300)
W1_LEG_S = (690, 1150)         # contract's own per-leg range for the 239,663 leg


def _leg_s(tokens: int, cap: int, *, slow: bool) -> float:
    pf = tokens / (PREFILL_TPS[0] if slow else PREFILL_TPS[1])
    gen = cap / (DECODE_TPS[0] if slow else DECODE_TPS[1])
    return pf + gen


def budget_derivation() -> dict:
    """The arithmetic behind every window budget, with each term named.

    W2's budget had no number until legs-per-boot was frozen (oracle N4 follow-up); this is
    where it gets one: 6 decode boots + 6 short boots per the claim plan, plus the two pilot
    boots that double as the discarded first-touch boots.
    """
    short_legs_fast = (_leg_s(2000, 32, slow=False) + _leg_s(8000, 32, slow=False)
                       + 3 * _leg_s(2000, 64, slow=False))
    short_legs_slow = (_leg_s(2000, 32, slow=True) + _leg_s(8000, 32, slow=True)
                       + 3 * _leg_s(2000, 64, slow=True))
    # a decode boot issues the same 144k prompt twice (fresh, then cached): one prefill plus
    # two 1024-token generations
    dec_fast = _leg_s(144000, 1024, slow=False) + 1024 / DECODE_TPS[1]
    dec_slow = _leg_s(144000, 1024, slow=True) + 1024 / DECODE_TPS[0]
    return {
        "anchors": {"prefill_tps": PREFILL_TPS, "decode_tps": DECODE_TPS, "boot_s": BOOT_S,
                    "w1_leg_s": W1_LEG_S, "source": "239,663-token pair + contract text"},
        "W1": {
            "terms": "6 legs x per-leg 690-1,150 s + 6 boots x 3-5 min (warmup boots excluded)",
            "range_s": [6 * W1_LEG_S[0] + 6 * BOOT_S[0], 6 * W1_LEG_S[1] + 6 * BOOT_S[1]],
        },
        "W2": {
            "terms": ("6 decode boots x (144k prefill + 1024 gen + boot) "
                      "+ 6 short boots x (2k + 8k + 3-turn TTFT + boot) "
                      "+ 2 pilot boots (also the discarded first-touch boots)"),
            "decode_boot_s": [dec_fast + BOOT_S[0], dec_slow + BOOT_S[1]],
            "short_boot_s": [short_legs_fast + BOOT_S[0], short_legs_slow + BOOT_S[1]],
            "range_s": [6 * (dec_fast + BOOT_S[0]) + 8 * (short_legs_fast + BOOT_S[0]),
                        6 * (dec_slow + BOOT_S[1]) + 8 * (short_legs_slow + BOOT_S[1])],
        },
        "W3": {
            "terms": "6 boots x (239,663 prefill + 2 x 1024 gen (fresh then cached) + boot)",
            "range_s": [6 * (_leg_s(239663, 1024, slow=False) + 1024 / DECODE_TPS[1] + BOOT_S[0]),
                        6 * (_leg_s(239663, 1024, slow=True) + 1024 / DECODE_TPS[0] + BOOT_S[1])],
            "note": "the contract states 1.2-2 h; its own leg range (690-1,150 s) plus boots "
                    "derives the wider range above - the derived one governs planning",
        },
        "W4": {
            "terms": "4 boots x (144k prefill + 1024 gen + boot); 6 boots if 3/cell",
            "range_s": [4 * (_leg_s(144000, 1024, slow=False) + BOOT_S[0]),
                        4 * (_leg_s(144000, 1024, slow=True) + BOOT_S[1])],
        },
    }


# -------------------------------------------------------------------------- claim rule
CLAIM = {
    "level": 0.90,
    "method": "exact / Hodges-Lehmann two-sided interval at n=3 per arm "
              "(3v3 gives C(6,3)=20 allocations, minimum exact p = 0.10)",
    "outputs": ["confirm (90%)", "indeterminate"],
    "confirm_when": "the 90% interval excludes 0 AND exceeds the pre-registered delta",
    "within_delta_clause": "'within +/-delta' is licensed ONLY when the entire 90% interval "
                           "lies inside +/-delta; a wide interval straddling the delta boundary "
                           "is INDETERMINATE, not 'within 5%'",
    "never": ["a 95 % claim at this n", "'no difference'", "'engine faster' before W4 decides"],
    "report": "median + full range, speed beside memory, boot-state confounds named",
    "delta_pre_registered": {"prefill": 0.05, "decode": 0.08,
                             "short_pp_ttft": "from the W2 pilot, frozen before the claim legs"},
    "n_per_arm": REPS,
    "interleave": list(INTERLEAVE),
}

# --------------------------------------------------------------- prerequisite 0 (pre-boot)
PREREQ0 = {
    "mem_available_gib_min": 15,
    "nonengine_rss_gib_max": 8,
    "single_process_rss_gib_warn": 2,
    "stage_cores": [3, 5, 7, 9, 11, 13, 15],
    "core_hot_fraction": 0.5,
    "cache_free_gib_min": 40,
    "in_window_min_avail_gib": 12,   # SPEC-ab9 guard, recorded not gated during a leg
    "note": "pre-boot only: during a leg min_avail_mib is RECORDED against the expected "
            "9-12 GiB serving band, never gated",
}

# ------------------------------------------------------------- lines a leg/boot must show
EXPECTED_LINES = {
    "judge": r"strata serve: prompt (\d+) tokens = (\d+) reused \+ (\d+)(?: of (\d+))? read in "
             r"(\d+) ms \(([\d.]+) tok/s\), (\d+) generated in (\d+) ms \(([\d.]+) tok/s\)",
    "judge_tail": r"drafts accepted (\d+) of (\d+), (\d+) checkpoints helper=reqs=(\d+) "
                  r"rows=(\d+) bytes=(\d+) timeouts=(\d+) fallbacks=(\d+)",
    "boot_tuple": r"engine started: .*--spec 5 .*--dflash-block 7 .*--vram-reserve-mib 256 "
                  r".*--vram-reserve-later-mib 192",
    "boot_helper": r"expert-helper: (\d+) rows on .* \(mirrored \d+/\d+ slots, ([\d.]+) GiB",
    "boot_helper_proxy": r"expert-helper proxy: (\d+) experts on .* \(fill report\)",
    "boot_cache": r"strata generate: expert cache (\d+) slots",
    "boot_split": r"layer split: .* runs layers 30-47",
    "boot_fs": r"foresight swap: .*granted (\d+) slots.*leaving (\d+) MiB",
    "dflash": r"dflash: .* trained block (\d+)",
    "low_warning": r" - LOW:",
    "non_finite": r"non-finite logits",
    "gpu_fault": r"Memory access fault by GPU node-",
    "restart": r"pair supervisor:.*(restart|exited)",
}

# Gates per window: (name, predicate in words). The leg runner implements them.
GATES = {
    "prefill": ["judge line present with reused == 0 (cold prompt, no cache reuse)",
                "served prompt-N equal across arms for the same shape (truncation guard)",
                "no non-finite logits; no GPU memory-access fault; no supervisor restart in the boot",
                "boot line carries the frozen tuple (spec 5, dflash-block 7, reserves 256/192, "
                "layer split 30) and the helper fill is inside the recorded band"],
    "decode": ["judge line present, gen_tps parsed, drafts accepted/total recorded",
               "helper reqs/rows/bytes and timeouts=0 fallbacks=0 recorded",
               "same discard gate as prefill"],
    "ttft": ["per-turn first-token time from the server's own event stream (first_token - queued) "
             "and from the client's first streamed delta; both recorded",
             "inter-turn wall = next turn's first token - previous turn's finish",
             "same discard gate; turns must share one conversation (no fresh prefill after turn 1)"],
}

# The one line the contract takes from the shell, kept next to the numbers it belongs to.
PREFLIGHT_NOTE = ("A-engine+B-server boots only with arm A's config (no --conversation-spill-mib); "
                  "the port tuple fed verbatim to the fork engine exits 2 at argv. See the "
                  "preflight section of the manifest.")


def window_budget_s(window: str) -> list[int]:
    """The budget a window is gated on: the derived range for W2, the contract's otherwise."""
    if WINDOWS[window]["budget_s"] is not None:
        return list(WINDOWS[window]["budget_s"])
    return [int(x) for x in budget_derivation()[window]["range_s"]]
