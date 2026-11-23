#!/usr/bin/env python3
"""port_ab_contract.py - the frozen numbers of the Phase-2 A/B contract, in one place.

Everything the windows are gated on lives here: the byte targets of the frozen corpus, the
per-window boot groups (legs per boot), the pre-registered deltas, the claim rule, the
helper-fill band and the prerequisite-0 thresholds. port_ab_prepare.py writes this into the
manifest; the leg runner and the pre-window checker read the same values, so a number cannot
drift between them.

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
             "use": "W2 short-PP 2k"},
    "pp8k": {"tokens_expected": 8000, "bytes": bytes_for(8000), "use": "W2 short-PP 8k"},
    # The TTFT leg has its OWN 2k prompt. Sharing pp2k's bytes would make its first turn a
    # conversation-cache hit inside a boot that already ran pp2k, so turn 1 would not be the
    # cold prefill the turn-latency shape means to measure. A different head tag diverges at
    # token zero (champions_prepare.build_paired_prompt).
    "ttft2k": {"tokens_expected": 2000, "bytes": bytes_for(2000),
               "head_tag": "champs work=ttft2k",
               "use": "W2 3-turn TTFT leg turn-1 prompt (distinct from pp2k: cold on purpose)"},
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
    "ttft3": {"kind": "ttft", "turns": 3, "max_tokens": 64, "corpus": "ttft2k"},
}


def kind_for(shape, window):
    return LEGS[shape].get("kind_by_window", {}).get(window) or LEGS[shape]["kind"]


def corpus_shape(shape):
    """The frozen corpus a leg reads (the TTFT leg reads its own 2k shape)."""
    return LEGS[shape].get("corpus") or shape


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
# n per arm (operator directive 2026-11-23: 3, not 5). Each window's claim boots are
# interleaved A B B A A B with the replicates numbered 1..3 per arm.
REPS = 3
INTERLEAVE = ("A", "B", "B", "A", "A", "B")
INTERLEAVE_PAIRS = (("A", 1), ("B", 1), ("B", 2), ("A", 2), ("A", 3), ("B", 3))

# A window's boots are grouped by the SHAPE they carry, and the group name is part of the
# boot key (W2-A-dec-r1 vs W2-A-sht-r1): two boot shapes of one window must never share a
# key, or the decode boot's cache dir and sockets collide with the short boot's and the
# second boot refuses (cache guard) or silently reuses the first one's state.
#   legs_per_boot = measured legs one boot carries (W2's short boot carries three)
#   warmup_boots  = discarded first-touch boots, one per arm per boot group; they count in
#                   the window's wall clock, so the budget derivation includes them.
WINDOWS = {
    "W1": {
        "name": "prefill rate, 239,663-token frozen corpus",
        "boot_groups": {"pp": ["pp239k"]},
        "legs_per_boot": {"pp": 1},
        "boots_per_arm": REPS,
        "warmup_boots": 2,
        "budget_s": None,             # derived below, INCLUDING the two warmup boots
        "contract_budget_s": [5400, 9000],   # the contract's 1.5-2.5 h (claim boots only)
        "gate_metric": "read_tps",
        "delta": 0.05,
    },
    "W2": {
        "name": "144k decode + short-PP 2k/8k + 3-turn TTFT",
        "boot_groups": {"dec": ["dec144k"], "sht": ["pp2k", "pp8k", "ttft3"]},
        "legs_per_boot": {"dec": 1, "sht": 3},
        "boots_per_arm": REPS * 2,    # 3 decode boots + 3 short boots per arm
        # one discarded first-touch boot per arm per boot group: the short one is the pilot
        # (its legs are recorded and excluded from the claim sample), the decode one is a
        # plain warmup. Without the decode warmup the first claim decode boot pays the rig's
        # first-touch cost that W1/W3 explicitly discard.
        "warmup_boots": 4,
        "pilot": {"boots_per_arm": 1, "group": "sht",
                  "note": "short-PP/TTFT pilot legs run first; their deltas are frozen into the "
                          "manifest before the claim legs and are excluded from the claim sample"},
        "decode_warmup": {"boots_per_arm": 1, "group": "dec",
                          "note": "discarded first-touch decode boot, as W1/W3 have"},
        "gate_metric": "gen_tps / ttft_s",
        "delta": {"gen_tps": 0.08, "ttft_s": None},
        "delta_note": "decode 8 % pre-registered; short-PP and TTFT deltas come from the W2 "
                      "pilot and are frozen in this manifest before the claim legs",
        "budget_s": None,             # derived below (the contract demanded a number here)
        "contract_budget_s": None,    # the contract carried no W2 budget until this froze
    },
    "W3": {
        "name": "240k decode (each boot carries its own 239,663 prefill)",
        "boot_groups": {"pp": ["pp239k"]},
        "legs_per_boot": {"pp": 1},
        "boots_per_arm": REPS,
        "warmup_boots": 2,
        "budget_s": None,
        "contract_budget_s": [4320, 7200],   # 1.2-2 h, claim boots only
        "gate_metric": "gen_tps",
        "delta": 0.08,
    },
    "W4": {
        "name": "2x2 engine x server cell (veto-only)",
        "boot_groups": {"dec": ["dec144k"]},
        "legs_per_boot": {"dec": 1},
        "cells": ["A-engine+B-server", "B-engine+A-server"],
        "controls": ["A-engine+A-server", "B-engine+B-server"],
        "controls_note": "the two same-arm cells are already measured by W1-W3; W4 spends its "
                         "budget on the two cross cells so an engine claim can be vetoed",
        "boots_per_cell": 2,
        "warmup_boots": 0,
        "budget_s": None,
        "contract_budget_s": [2700, 3900],   # ~1 h
        "gate_metric": "gen_tps",
        "delta": None,
        "claim": "VETO ONLY: with 2 boots/cell min exact p = 0.33; the cell can reject an engine "
                 "claim (the effect follows the server, or the cells disagree) but can never "
                 "establish one. Pair attribution stands until a cell vetoes it.",
    },
}

# --------------------------------------------------------------- helper fill (frozen band)
# The helper's own boot line is authoritative: "<rows> rows on <sock> (mirrored N/M slots,
# X GiB, device free at hello Y MiB)". --fill-mib 7000 caps both arms at 7000 MiB shell
# (6.84 GiB reported), which is BELOW the Phase-1 fills of 7.09 GiB (port, 3,816 rows) and
# 7.34 GiB (fork, 3,921 rows) - that is the point of the cap: it narrows the residual fill
# difference. The band is derived from the cap and the Phase-1 row size (1.87-1.92 MiB/row):
# 7000 / 1.92 = 3,646 .. 7000 / 1.87 = 3,743 rows; 6 % either side leaves room for the
# helper's own startup accounting without hiding an uncapped fill.
HELPER_FILL_BAND = {
    "rows": [3430, 3970],
    "gib": [6.55, 7.00],
    "arm_drift_max": 0.05,
    "basis": "fill-mib 7000 binds at ~6.84 GiB; Phase-1 row size 1.87-1.92 MiB/row -> "
             "3,646-3,743 rows; the band is 6 % either side and is checked per boot, never widened",
    "checked_from": "the helper's own boot line (EXPECTED_LINES.boot_helper)",
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
    """The arithmetic behind every window budget, with each term named and counted.

    The wall clock of a window includes its discarded first-touch boots, so every range here
    counts them. The contract's own numbers (1.5-2.5 h for W1, 1.2-2 h for W3) did not; they
    are kept beside the derivation as contract_budget_s.
    """
    short_fast = (_leg_s(2000, 32, slow=False) + _leg_s(8000, 32, slow=False)
                  + 3 * _leg_s(2000, 64, slow=False))
    short_slow = (_leg_s(2000, 32, slow=True) + _leg_s(8000, 32, slow=True)
                  + 3 * _leg_s(2000, 64, slow=True))
    # a decode boot issues the same 144k prompt twice (fresh, then cached): one prefill plus
    # two 1024-token generations
    dec_fast = _leg_s(144000, 1024, slow=False) + 1024 / DECODE_TPS[1]
    dec_slow = _leg_s(144000, 1024, slow=True) + 1024 / DECODE_TPS[0]
    # the 240k decode boot: 239,663 prefill + the same two generations
    w3_fast = _leg_s(239663, 1024, slow=False) + 1024 / DECODE_TPS[1]
    w3_slow = _leg_s(239663, 1024, slow=True) + 1024 / DECODE_TPS[0]
    w1_total = 6 + WINDOWS["W1"]["warmup_boots"]                  # 6 claim + 2 warmup = 8
    # per-arm counts become per-window counts: one discarded boot per arm per boot group
    w2_dec_warmup = 2 * WINDOWS["W2"]["decode_warmup"]["boots_per_arm"]   # 2 arms x 1
    w2_sht_pilot = 2 * WINDOWS["W2"]["pilot"]["boots_per_arm"]            # 2 arms x 1
    w2_dec = 6 + w2_dec_warmup                                    # 6 claim + 2 warmup = 8
    w2_sht = 6 + w2_sht_pilot                                     # 6 claim + 2 pilot  = 8
    w3_total = 6 + WINDOWS["W3"]["warmup_boots"]                  # 8
    w4_total = 4                                                  # 2 cells x 2 boots
    return {
        "anchors": {"prefill_tps": PREFILL_TPS, "decode_tps": DECODE_TPS, "boot_s": BOOT_S,
                    "w1_leg_s": W1_LEG_S, "source": "239,663-token pair + contract text"},
        "W1": {
            "boots": {"claim": 6, "warmup": WINDOWS["W1"]["warmup_boots"], "total": w1_total},
            "terms": str(w1_total) + " boots x (239,663 prefill leg + boot): 690-1,150 s per "
                     "leg, 180-300 s per boot",
            "range_s": [w1_total * W1_LEG_S[0] + w1_total * BOOT_S[0],
                        w1_total * W1_LEG_S[1] + w1_total * BOOT_S[1]],
        },
        "W2": {
            "boots": {"decode_claim": 6, "decode_warmup": w2_dec_warmup,
                      "short_claim": 6, "short_pilot": w2_sht_pilot,
                      "total": w2_dec + w2_sht},
            "terms": str(w2_dec) + " decode boots x (144k prefill + 2 x 1024 gen + boot) + "
                     + str(w2_sht) + " short boots x (2k + 8k + 3-turn TTFT + boot)",
            "decode_boot_s": [round(dec_fast + BOOT_S[0], 1), round(dec_slow + BOOT_S[1], 1)],
            "short_boot_s": [round(short_fast + BOOT_S[0], 1), round(short_slow + BOOT_S[1], 1)],
            "range_s": [w2_dec * (dec_fast + BOOT_S[0]) + w2_sht * (short_fast + BOOT_S[0]),
                        w2_dec * (dec_slow + BOOT_S[1]) + w2_sht * (short_slow + BOOT_S[1])],
        },
        "W3": {
            "boots": {"claim": 6, "warmup": WINDOWS["W3"]["warmup_boots"], "total": w3_total},
            "terms": str(w3_total) + " boots x (239,663 prefill + 2 x 1024 gen (fresh then "
                     "cached) + boot)",
            "range_s": [w3_total * (w3_fast + BOOT_S[0]), w3_total * (w3_slow + BOOT_S[1])],
            "note": "the contract states 1.2-2 h for the claim boots; adding the two warmup "
                    "boots and the cached second generation is why the planning range is wider",
        },
        "W4": {
            "boots": {"total": w4_total},
            "terms": str(w4_total) + " boots x (144k prefill + 2 x 1024 gen + boot)",
            "range_s": [w4_total * (dec_fast + BOOT_S[0]), w4_total * (dec_slow + BOOT_S[1])],
        },
    }


def shapes_of(window):
    """Every measured leg shape of a window, in boot-group order."""
    return [shape for shapes in WINDOWS[window]["boot_groups"].values() for shape in shapes]


def window_budget_s(window: str) -> list[int]:
    """The planning budget: ALWAYS the derived range (it includes the discarded boots)."""
    return [int(x) for x in budget_derivation()[window]["range_s"]]


def window_contract_budget_s(window: str):
    return WINDOWS[window]["contract_budget_s"]


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
    "delta_pre_registered": {
        "prefill_read_tps": 0.05,
        "decode_gen_tps": 0.08,
        "short_pp_read_tps": "from the W2 pilot (per shape, frozen before the claim legs)",
        "short_pp_gen_tps": "from the W2 pilot",
        "ttft_turn1_s": "from the W2 pilot",
        "ttft_inter_turn_s": "from the W2 pilot",
    },
    "n_per_arm": REPS,
    "interleave": list(INTERLEAVE),
}

# The W2 pilot's delta slots. Null until port_ab_legs.py --freeze-pilot W2 fills them from the
# pilot records; the claim legs fail closed while any is null (a claim leg must never run on
# an unfrozen delta).
PILOT_DELTA_KEYS = {
    "pp2k": {"read_tps": None, "gen_tps": None},
    "pp8k": {"read_tps": None, "gen_tps": None},
    "ttft3": {"ttft_turn1_s": None, "inter_turn_s": None},
}


def pilot_deltas_template():
    return {"frozen": False, "frozen_at": None, "window": "W2",
            "source": "records of the W2 pilot boots (group sht, rep 0), 1 boot per arm",
            "deltas": {k: dict(v) for k, v in PILOT_DELTA_KEYS.items()},
            "samples": {}}


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
# boot_tuple is PARSED as flag/value pairs, never matched in order: the engine argv's order
# is spec, reserve, reserve-later, dflash-block, and an order-sensitive regex silently matches
# nothing when it changes.
EXPECTED_LINES = {
    "judge": r"strata serve: prompt (\d+) tokens = (\d+) reused \+ (\d+)(?: of (\d+))? read in "
             r"(\d+) ms \(([\d.]+) tok/s\), (\d+) generated in (\d+) ms \(([\d.]+) tok/s\)",
    "judge_tail": r"drafts accepted (\d+) of (\d+), (\d+) checkpoints helper=reqs=(\d+) "
                  r"rows=(\d+) bytes=(\d+) timeouts=(\d+) fallbacks=(\d+)",
    "boot_started": r"engine started: ",
    "boot_helper": r"expert-helper: (\d+) rows on .* \(mirrored \d+/\d+ slots, ([\d.]+) GiB",
    "boot_helper_proxy": r"expert-helper proxy: (\d+) experts on .* \(fill report\)",
    "boot_cache": r"strata generate: expert cache (\d+) slots",
    "boot_split": r"layer split: .* runs layers 30-47",
    "boot_fs": r"foresight swap: .*granted (\d+) slots.*leaving (\d+) MiB",
    "dflash": r"dflash: .* trained block (\d+)",
    "low_warning": r" - LOW:",
    "non_finite": r"non-finite logits",
    "gpu_fault": r"Memory access fault by GPU node-",
    "restart": r"pair supervisor:.*(restart|recovered|recovering|exited)",
}

# Gates per window: (name, predicate in words). The leg runner implements them.
GATES = {
    "prefill": ["judge line present with reused == 0 (cold prompt, no cache reuse)",
                "served prompt-N equal across arms for the same shape (truncation guard)",
                "no non-finite logits; no GPU memory-access fault; no supervisor restart in the boot",
                "the engine's own 'engine started:' line carries the frozen tuple (parsed "
                "flag/value pairs: spec 5, reserves 256/192, dflash-block 7, helper slots 4,600)",
                "the helper's boot line reports a fill inside the frozen band, and the arms' "
                "fills differ by no more than the band's drift limit"],
    "decode": ["judge line present, gen_tps parsed from request 2, drafts accepted/total recorded",
               "helper reqs/rows/bytes and timeouts=0 fallbacks=0 recorded",
               "same discard gate as prefill (tuple, fill band, restart, fault fingerprint)"],
    "ttft": ["per-turn first-token time from the server's own event stream (first_token - queued) "
             "and from the client's first streamed delta; both recorded",
             "inter-turn wall = the NEXT turn's first token - the PREVIOUS turn's finish "
             "(not the client's idle gap)",
             "same discard gate; turn 1 must be a cold prefill of the TTFT leg's own corpus"],
}

# The one line the contract takes from the shell, kept next to the numbers it belongs to.
PREFLIGHT_NOTE = ("A-engine+B-server boots only with arm A's config (no --conversation-spill-mib); "
                  "the port tuple fed verbatim to the fork engine exits 2 at argv. See the "
                  "preflight section of the manifest.")
