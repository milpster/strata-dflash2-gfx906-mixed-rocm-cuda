#!/usr/bin/env python3
"""port_ab_contract.py - the frozen numbers of the Phase-2 A/B contract, in one place.

Everything the windows are gated on lives here: the byte targets of the frozen corpus, the
per-window boot groups (legs per boot), the pre-registered deltas, the claim rule, the
helper-fill band and the prerequisite-0 thresholds. port_ab_prepare.py writes this into the
manifest; the leg runner and the pre-window checker read the same values, so a number cannot
drift between them.

Sources (plan/measurement-contract-speed-attribution-faults.md, approved 2026-11-23):
  * byte target 323,200 B, frozen as corpus/pp239k.ctx323200.bin (sha256 210b16b3...): the
    engine served 58,423 prompt tokens on it in every W1 leg, both arms. The 239,663-token
    figure is the generator's old estimate (323,200 / 239,663 = 1.3486 B per token,
    ctx-threshold.sh documents ~0.742 tok/B); it still scales the other shapes' byte targets,
    but the frozen bytes, not the token estimate, are the contract;
  * prefill 287.1-348.2 tok/s and decode 21.6-22.1 tok/s measured on the 239,663 prompt;
  * boot 3-5 min (full-residency init);
  * pre-registered deltas: prefill 5 %, decode 8 %; n=2/arm (operator directive 2026-11-23
    evening, reduced from 3): 2v2 gives C(4,2)=6 allocations and a minimum exact two-sided
    p = 0.33, so this window is DESCRIPTIVE-ONLY; the 90 % exact/HL claim rule applies to a
    FOLLOW-UP window at n>=3 (3v3: C(6,3)=20, min p = 0.10), never to this one;
  * prerequisite 0: >=15 GiB MemAvailable pre-boot, non-engine RSS <=8 GiB, no non-pinned
    process above ~50 % of a core on the stage cores, >=40 GiB free for the cache dir.
  * W1f (operator directive 2026-11-24) is the claim-capable FOLLOW-UP to W1 after arm B was
    re-pinned to the promoted pool-fix engine 15d330ea: 3 boots per arm, so CLAIM's 90 %
    exact/HL rule applies to it. W1's four old-pin claim records stay untouched as the
    pre-amendment baseline. See the W1f entry.
  * W1d (operator directive 2026-11-23 23:00) is W1's window re-run with the ENGINE'S OWN
    DEFAULT VRAM margins: both reserve flags are absent instead of 256/192. It is a SEPARATE
    window with its own boot keys, so W1's evidence and config pins stay untouched. On this
    rig a default-margins boot measured today reserves 700 MiB per card (not 256/192), brings
    card0's expert cache up at 4,116 slots (not 5,739), leaves 728/648 MiB free VRAM after
    load, and flips the Foresight swap grant from 0 slots to card0:2/card1:8 - so a W1d
    result is NOT attributable to the reserves alone. See the W1d entry for the full note.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------- corpus shapes
# bytes are what champions_prepare.build_paired_prompt takes (exact byte length); tokens is
# the count the engine reports for that byte target. The generator freezes the BYTE target;
# every engine leg measured 58,423 prompt tokens on the 323,200-byte corpus (5.53 B/token),
# not the 239,663 the ratio below was estimated from. BYTES_PER_TOKEN still scales the other
# shapes' byte targets (their measured token counts are not in yet), so it is left unchanged.
BYTES_PER_TOKEN = 323200 / 239663


def bytes_for(tokens: int) -> int:
    return int(round(tokens * BYTES_PER_TOKEN))


SHAPES = {
    # tokens_expected is the MEASURED count, not the estimate the byte target was scaled from.
    # The frozen corpus is corpus/pp239k.ctx323200.bin: 323,200 bytes, sha256
    # 210b16b32b8658e68b863442d3c2d9ebde7eb3c9d50b0cea8c28e7d045c0ea68. All four W1 claim
    # boots, both engines, served exactly 58,423 prompt tokens on it. The old 239,663 was the
    # generator's 1.3486 B/token estimate and no longer describes the frozen file; the BYTE
    # target (323,200), not the token estimate, is what the corpus generator freezes.
    "pp239k": {"tokens_expected": 58423, "bytes": 323200, "head_tag": "champs work=pp239k",
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
    "pp239k": {"kind": "prefill",
               # W1f is W1's follow-up (same prefill leg, same 8-token tail); it is listed
               # here because gen_tokens/kind_for fall back through entry["W2"] and pp239k has
               # no W2 slot - an unlisted window would raise, not silently default.
               "kind_by_window": {"W1": "prefill", "W1d": "prefill", "W1f": "prefill",
                                  "W3": "tg"},
               "max_tokens": {"W1": 8, "W1d": 8, "W1f": 8, "W3": 1024}},
    "dec144k": {"kind": "prefill", "kind_by_window": {"W2": "tg", "W4": "tg"},
                "max_tokens": {"W2": 1024, "W4": 1024}},
    "pp2k": {"kind": "prefill", "max_tokens": {"W2": 32}},
    "pp8k": {"kind": "prefill", "max_tokens": {"W2": 32}},
    "ttft3": {"kind": "ttft", "turns": 3, "max_tokens": 64, "corpus": "ttft2k"},
}


# TG claim floor (operator directive 2026-11-24, contract line "Decode/TG measurement
# floor"): a gen_tps figure supports a TG statement only when that leg actually GENERATED at
# least this many tokens - the count the judge line reports, never the max_tokens cap. A
# shorter tail is context only: W1's 8-token tail is a prefill leg and its gen rate is not a
# TG measurement. A decode leg below the floor cannot enter a TG claim sample.
TG_CLAIM_MIN_GENERATED = 1000


def kind_for(shape, window):
    return LEGS[shape].get("kind_by_window", {}).get(window) or LEGS[shape]["kind"]


def gen_tokens(shape, window):
    """How many tokens this leg's kind is meant to generate in this window (its own cap).

    The single place the TG gate reads a leg's intended generation count from; it is the same
    number port_ab_legs.commands() passes as --max-tokens.
    """
    entry = LEGS[shape]["max_tokens"]
    return int(entry) if isinstance(entry, int) else int(entry.get(window) or entry["W2"])


def claims_tg(shape, window):
    """True when this leg's kind produces a TG (decode) claim in this window."""
    return LEGS[shape]["kind"] != "ttft" and gen_tokens(shape, window) >= TG_CLAIM_MIN_GENERATED


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
# n per arm. Operator directive 2026-11-23 (evening) reduced it to 2 (it was 3, then 5).
# At n=2 the window cannot support a confidence claim (2v2: C(4,2)=6 allocations, minimum
# exact two-sided p = 0.33), so its outputs are descriptive-only; see CLAIM below. Each
# window's claim boots are interleaved A B B A with the replicates numbered 1..2 per arm.
REPS = 2
INTERLEAVE = ("A", "B", "B", "A")
INTERLEAVE_PAIRS = (("A", 1), ("B", 1), ("B", 2), ("A", 2))

# W1f's follow-up n (operator directive 2026-11-24): CLAIM needs n>=3 per arm before any claim
# can be made, so W1f is 3 boots per arm - the only prepared window where INTERLEAVE_PAIRS
# (n=2) does not apply.
W1F_REPS = 3

# W1's approved contract range (5,400-9,000 s for its 4 claim boots = 1,350-2,250 s per boot).
# W1f's contract budget is DERIVED from it, never invented: W1f spends 8 boots - 6 claim
# (3 per arm, i.e. 1.5x W1's claim count) + the same 2 discarded warmups - so its range is
# W1's per-boot rate x 8.
W1_CONTRACT_BUDGET_S = [5400, 9000]
W1F_CLAIM_BOOTS = 2 * W1F_REPS                       # 6
W1F_BOOTS = W1F_CLAIM_BOOTS + 2                      # 6 claim + 2 warmup = 8
W1F_CONTRACT_BUDGET_S = [int(round(x * W1F_BOOTS / (2 * REPS))) for x in W1_CONTRACT_BUDGET_S]


def _abba_pairs(n):
    """Claim boots in mirrored interleave order: n=2 -> A B B A, n=3 -> A B B A A B.

    The ABBA block is continued, not restarted, so every arm's boots stay as far apart as the
    order allows and the first and last boot of a window belong to different arms. Reps are
    numbered 1..n per arm in their order of appearance, so n=2 reproduces INTERLEAVE_PAIRS
    exactly and W1's runbook cannot change.
    """
    block = ("A", "B", "B", "A")
    seen = {"A": 0, "B": 0}
    pairs = []
    i = 0
    while len(pairs) < 2 * n:
        arm = block[i % 4]
        seen[arm] += 1
        pairs.append((arm, seen[arm]))
        i += 1
    return tuple(pairs)


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
        "margins": "explicit",         # both reserve flags written from TUPLE (256/192)
        "boot_groups": {"pp": ["pp239k"]},
        "legs_per_boot": {"pp": 1},
        "boots_per_arm": REPS,
        "warmup_boots": 2,
        "budget_s": None,             # derived below, INCLUDING the two warmup boots
        "contract_budget_s": [5400, 9000],   # the contract's 1.5-2.5 h (claim boots only)
        "gate_metric": "read_tps",
        "delta": 0.05,
    },
    # Operator directive 2026-11-23 23:00: re-run W1's speed window with the ENGINE'S OWN
    # DEFAULT VRAM margins. Every W1d config has BOTH --vram-reserve-mib and
    # --vram-reserve-later-mib REMOVED (port_ab_configs.derive calls ensure_absent and records
    # each removal as an "operator-directive" delta); nothing else about the leg differs from
    # W1, which keeps writing 256/192. Purpose: see whether the XDMA permission fault that
    # recurs during the W1 claim sequence stops recurring under default margins.
    #
    # What a default-margins boot is KNOWN to change on this rig (measured today, 2026-11-23,
    # on the live faultdiff leg): the engine reserved 700 MiB on each card instead of 256/192;
    # card0's expert cache came up with 4,116 slots instead of 5,739; post-load free VRAM was
    # 728/648 MiB; and the Foresight swap grant FLIPPED from 0 slots to card0:2/card1:8.
    # ATTRIBUTION LIMIT: because the Foresight grant flips with the reserves, a W1d result is
    # NOT attributable to the reserves alone - it is the default-margins BOOT (reserves +
    # cache size + swap grant together) against W1's pinned tuple. W1d is descriptive-only for
    # that reason and for the n=2/arm reason CLAIM states; it must never be read as "the
    # reserves cost X".
    #
    # W1d has its own window id and therefore its own boot keys (W1d-*) and config paths, so
    # W1's records, evidence and manifest config pins are never overwritten.
    "W1d": {
        "name": "prefill rate, 239,663-token frozen corpus, engine-default VRAM margins "
                "(operator-directed rerun of W1)",
        "margins": "engine-default",   # both reserve flags REMOVED, not set
        "boot_groups": {"pp": ["pp239k"]},
        "legs_per_boot": {"pp": 1},
        "boots_per_arm": REPS,
        "warmup_boots": 2,
        "budget_s": None,             # derived below, identical boot shape to W1
        "contract_budget_s": [5400, 9000],   # inherited from W1 (same boot shape); the
                                             # contract itself says nothing about a
                                             # default-margins rerun
        "gate_metric": "read_tps",
        "delta": 0.05,
        "delta_note": "W1's pre-registered 5 % prefill delta is kept for reference only: W1d "
                      "is a separate, descriptive-only window (n=2/arm, and the boot differs in "
                      "more than the reserves)",
        "boot_identity_note": "engine-default margins: --vram-reserve-mib and "
                              "--vram-reserve-later-mib absent from the engine argv, so the "
                              "leg runner's tuple gate expects BOTH absent for a W1d boot",
    },
    # Operator directive 2026-11-24: arm B was re-pinned from 936efcc7 to the promoted
    # pool-fix engine 15d330ea (commit e39905a674), so W1's four claim records - measured on
    # the OLD pin - are the PRE-AMENDMENT BASELINE and stay exactly as they are (records,
    # evidence and manifest pins are never re-derived or overwritten). W1f is the
    # CLAIM-CAPABLE FOLLOW-UP window the contract demands before any claim can be made:
    # 3 boots per arm (3v3 gives C(6,3) = 20 allocations and a minimum exact two-sided
    # p = 0.10), so CLAIM["level"] (90 %, exact/Hodges-Lehmann) applies to THIS window and to
    # no other prepared one. It has its own window id and therefore its own boot keys
    # (W1f-*) and config paths, so W1's evidence cannot be disturbed.
    #
    # Boot shape is W1's unchanged: one 239,663-token prefill leg per boot, explicit margins
    # (256/192 from TUPLE), the same 5 % pre-registered prefill delta. Only n differs (3, not
    # 2), which is why the budget below is derived for 8 boots: 6 claim + 2 discarded
    # first-touch warmups (one per arm, as W1 spends).
    "W1f": {
        "name": "prefill rate, 239,663-token frozen corpus, follow-up at n=3/arm "
                "(claim-capable; arm B re-pinned to 15d330ea)",
        "margins": "explicit",         # both reserve flags written from TUPLE (256/192), as W1
        "boot_groups": {"pp": ["pp239k"]},
        "legs_per_boot": {"pp": 1},
        "boots_per_arm": W1F_REPS,     # the follow-up n: CLAIM["level"] applies at n>=3
        "warmup_boots": 2,             # discarded first-touch boot per arm, exactly as W1
        "budget_s": None,              # derived below, INCLUDING the two warmup boots (8)
        # W1's 5,400-9,000 s per-boot rate applied to W1f's 8 boots (see W1F_BOOTS): the claim
        # share is 6 boots = 1.5x W1's 4 claim boots, and the other two are the same discarded
        # first-touch warmups W1 spends. Nothing new is claimed about a per-boot cost.
        "contract_budget_s": W1F_CONTRACT_BUDGET_S,
        "contract_budget_note": "8 boots (6 claim + 2 warmup)",
        # 3 boots per arm in the ABBA order continued to n=3: A1 B1 B2 A2 A3 B3 (see
        # _abba_pairs). Every other prepared window keeps the n=2 INTERLEAVE_PAIRS.
        "interleave_pairs": _abba_pairs(W1F_REPS),
        "gate_metric": "read_tps",
        "delta": 0.05,
        "delta_note": "the same pre-registered 5 % prefill delta as W1, and the only prepared "
                      "window whose 90 % exact/HL interval is a claim; W1/W1d/W2/W3 stay "
                      "descriptive-only",
        "claim": "CLAIM-CAPABLE: 3v3 interleaved (C(6,3)=20 allocations, minimum exact "
                 "two-sided p = 0.10), so the contract's CLAIM level/method applies here - the "
                 "90 % interval must exclude 0 AND exceed the pre-registered 5 % delta. W1's "
                 "four records (old arm-B pin) are the baseline this window is read against; "
                 "they are never pooled into it.",
    },
    "W2": {
        "name": "144k decode + short-PP 2k/8k + 3-turn TTFT",
        "margins": "explicit",
        "boot_groups": {"dec": ["dec144k"], "sht": ["pp2k", "pp8k", "ttft3"]},
        "legs_per_boot": {"dec": 1, "sht": 3},
        "boots_per_arm": REPS * 2,    # 2 decode boots + 2 short boots per arm
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
        # the TTFT delta is not pre-registered here: the W2 pilot's ttft_turn1_s /
        # ttft_inter_turn_s slots (pilot_deltas) are the only TTFT deltas a claim may use
        "delta": {"gen_tps": 0.08},
        "delta_note": "decode 8 % pre-registered; short-PP and TTFT deltas come from the W2 "
                      "pilot and are frozen in this manifest before the claim legs",
        "budget_s": None,             # derived below (the contract demanded a number here)
        "contract_budget_s": None,    # the contract carried no W2 budget until this froze
    },
    "W3": {
        "name": "240k decode (each boot carries its own 239,663 prefill)",
        "margins": "explicit",
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
        "margins": "explicit",
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
# 7000 / 1.92 = 3,646 .. 7000 / 1.87 = 3,743 rows; 6 % either side of the rows leaves room
# for the helper's own startup accounting. The GiB upper bound is 6.90 GiB (7065 MiB): 66 MiB
# over the cap for that accounting, and still far below the Phase-1 fills the cap must catch.
HELPER_FILL_BAND = {
    "rows": [3430, 3970],
    "gib": [6.55, 6.90],
    "arm_drift_max": 0.05,
    "basis": "fill-mib 7000 binds at ~6.84 GiB; Phase-1 row size 1.87-1.92 MiB/row -> "
             "3,646-3,743 rows; rows allow 6 % either side, GiB is 6.55-6.90 (the upper bound "
             "is 66 MiB over the 7,000 MiB cap, for the helper's startup accounting); checked "
             "per boot, never widened",
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
    # 2 arms x REPS claim boots, plus the discarded boots each window spends
    claim = 2 * REPS                                              # n=2/arm -> 4 claim boots
    w1_total = claim + WINDOWS["W1"]["warmup_boots"]              # 4 claim + 2 warmup = 6
    # W1f is the follow-up: the same boot shape as W1, 3 claim boots per arm plus the same two
    # discarded warmups. Its anchor rates are W1's (same leg, same boot), only the count differs.
    w1f_claim = 2 * WINDOWS["W1f"]["boots_per_arm"]                # 3/arm -> 6 claim boots
    w1f_total = w1f_claim + WINDOWS["W1f"]["warmup_boots"]         # 6 claim + 2 warmup = 8
    # W1d carries the identical boot shape (one 239,663 prefill leg per boot, 2 warmups), so
    # its range is W1's arithmetic and nothing new is claimed about default-margins speed.
    w1d_total = claim + WINDOWS["W1d"]["warmup_boots"]            # 4 claim + 2 warmup = 6
    # per-arm counts become per-window counts: one discarded boot per arm per boot group
    w2_dec_warmup = 2 * WINDOWS["W2"]["decode_warmup"]["boots_per_arm"]   # 2 arms x 1
    w2_sht_pilot = 2 * WINDOWS["W2"]["pilot"]["boots_per_arm"]            # 2 arms x 1
    w2_dec = claim + w2_dec_warmup                                # 4 claim + 2 warmup = 6
    w2_sht = claim + w2_sht_pilot                                 # 4 claim + 2 pilot  = 6
    w3_total = claim + WINDOWS["W3"]["warmup_boots"]              # 4 claim + 2 warmup = 6
    w4_total = 4                                                  # 2 cells x 2 boots
    return {
        "anchors": {"prefill_tps": PREFILL_TPS, "decode_tps": DECODE_TPS, "boot_s": BOOT_S,
                    "w1_leg_s": W1_LEG_S, "source": "239,663-token pair + contract text"},
        "W1": {
            "boots": {"claim": claim, "warmup": WINDOWS["W1"]["warmup_boots"],
                      "total": w1_total},
            "terms": str(w1_total) + " boots x (239,663 prefill leg + boot) = 4 claim "
                     "(2 per arm) + 2 warmup; 690-1,150 s per leg, 180-300 s per boot",
            "range_s": [w1_total * W1_LEG_S[0] + w1_total * BOOT_S[0],
                        w1_total * W1_LEG_S[1] + w1_total * BOOT_S[1]],
        },
        "W1f": {
            "boots": {"claim": w1f_claim, "warmup": WINDOWS["W1f"]["warmup_boots"],
                      "total": w1f_total},
            "terms": str(w1f_total) + " boots x (239,663 prefill leg + boot) = 6 claim "
                     "(3 per arm, 1.5x W1's claim count) + 2 warmup; 690-1,150 s per leg, "
                     "180-300 s per boot - W1's anchors, W1's leg, only n differs",
            "range_s": [w1f_total * W1_LEG_S[0] + w1f_total * BOOT_S[0],
                        w1f_total * W1_LEG_S[1] + w1f_total * BOOT_S[1]],
            "note": "the follow-up window: derived from W1's measured anchors, never from a new "
                    "per-boot estimate",
        },
        "W1d": {
            "boots": {"claim": claim, "warmup": WINDOWS["W1d"]["warmup_boots"],
                      "total": w1d_total},
            "terms": str(w1d_total) + " boots x (239,663 prefill leg + boot) = 4 claim "
                     "(2 per arm) + 2 warmup; same shape as W1 (engine-default margins change "
                     "the boot, not the leg)",
            "range_s": [w1d_total * W1_LEG_S[0] + w1d_total * BOOT_S[0],
                        w1d_total * W1_LEG_S[1] + w1d_total * BOOT_S[1]],
            "note": "inherits W1's anchors; a default-margins boot has no separate speed "
                    "anchor and must not be given one before it is measured",
        },
        "W2": {
            "boots": {"decode_claim": claim, "decode_warmup": w2_dec_warmup,
                      "short_claim": claim, "short_pilot": w2_sht_pilot,
                      "total": w2_dec + w2_sht},
            "terms": str(w2_dec) + " decode boots x (144k prefill + 2 x 1024 gen + boot) "
                     "[4 claim + 2 warmup] + " + str(w2_sht) + " short boots x (2k + 8k + "
                     "3-turn TTFT + boot) [4 claim + 2 pilot]",
            "decode_boot_s": [round(dec_fast + BOOT_S[0], 1), round(dec_slow + BOOT_S[1], 1)],
            "short_boot_s": [round(short_fast + BOOT_S[0], 1), round(short_slow + BOOT_S[1], 1)],
            "range_s": [w2_dec * (dec_fast + BOOT_S[0]) + w2_sht * (short_fast + BOOT_S[0]),
                        w2_dec * (dec_slow + BOOT_S[1]) + w2_sht * (short_slow + BOOT_S[1])],
        },
        "W3": {
            "boots": {"claim": claim, "warmup": WINDOWS["W3"]["warmup_boots"],
                      "total": w3_total},
            "terms": str(w3_total) + " boots x (239,663 prefill + 2 x 1024 gen (fresh then "
                     "cached) + boot) = 4 claim + 2 warmup",
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


def margins_mode(window):
    """How this window's configs carry the VRAM reserves.

    "explicit"       - derive() writes --vram-reserve-mib / --vram-reserve-later-mib from
                       TUPLE (256/192), the frozen Phase-2 tuple.
    "engine-default" - derive() REMOVES both flags (ensure_absent), so the engine's own
                       defaults apply. See the W1d entry for what that changes on this rig and
                       why such a result is not attributable to the reserves alone.
    """
    return WINDOWS[window].get("margins", "explicit")


def claim_pairs(window):
    """(arm, rep) in boot order for the window's CLAIM boots.

    W1f is the one prepared window at n=3/arm (its own ABBA-continued order, A B B A A B);
    every n=2 window keeps INTERLEAVE_PAIRS, so its boot order cannot move.
    """
    return tuple(WINDOWS[window].get("interleave_pairs") or INTERLEAVE_PAIRS)


def shapes_of(window):
    """Every measured leg shape of a window, in boot-group order."""
    return [shape for shapes in WINDOWS[window]["boot_groups"].values() for shape in shapes]


def window_budget_s(window: str) -> list[int]:
    """The planning budget: ALWAYS the derived range (it includes the discarded boots)."""
    return [int(x) for x in budget_derivation()[window]["range_s"]]


def window_contract_budget_s(window: str):
    return WINDOWS[window]["contract_budget_s"]


# -------------------------------------------------------------------------- claim rule
# n=2/arm (operator directive 2026-11-23 evening). 2v2 has C(4,2) = 6 allocations and a
# minimum exact two-sided p = 0.33 (1/6 + 1/6): NO interval at this n reaches 90 % (or 95 %),
# so NOTHING this window produces can be a claim. Its honest outputs are the per-leg rates,
# full ranges and memory; a direction may be noted and must carry UNCONFIRMED. "confirm",
# "no difference" and "within delta" are all unsupported here. A follow-up window at n>=3
# (3v3: C(6,3)=20, min p = 0.10) restores the 90 % exact/HL rule stated in level/method.
CLAIM = {
    "level": 0.90,
    "level_note": "the level applies to a FOLLOW-UP window at n>=3 only; at n=2/arm the "
                  "minimum exact two-sided p is 0.33, so no 90 % (or 95 %) interval exists",
    "method": "exact / Hodges-Lehmann two-sided interval at n>=3 per arm for a follow-up "
              "window (3v3 gives C(6,3)=20 allocations, minimum exact p = 0.10). This window "
              "is n=2/arm: 2v2 gives C(4,2)=6 allocations, minimum exact two-sided p = 0.33, "
              "and the outputs are DESCRIPTIVE-ONLY",
    "outputs": ["descriptive: per-leg rates, full ranges, memory beside speed",
                "directional observation, explicitly marked UNCONFIRMED"],
    "confirm_when": "NOT SUPPORTED at n=2/arm (no interval reaches 90 %); a follow-up window "
                    "at n>=3 confirms only when its 90 % interval excludes 0 AND exceeds the "
                    "pre-registered delta",
    "within_delta_clause": "NOT SUPPORTED at n=2/arm. In a FOLLOW-UP window at n>=3 'within "
                           "+/-delta' is licensed ONLY when the entire 90% interval lies "
                           "inside +/-delta; a wide interval straddling the delta boundary is "
                           "INDETERMINATE, not 'within 5%'",
    "never": ["'confirm' at n=2/arm", "'no difference'", "'within delta' at n=2/arm",
              "'engine faster' before W4 decides"],
    "follow_up": "any claim (confirm, no difference, within delta) requires a FOLLOW-UP window "
                 "at n>=3 per arm; this window's records are that window's baseline and are "
                 "descriptive only",
    "report": "median + full range, speed beside memory, boot-state confounds named, every "
              "comparison labelled descriptive-only",
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
    "claim_window": "W1f",
    "claim_window_note": "W1f is the FOLLOW-UP at n=3/arm (arm B re-pinned to 15d330ea on "
                         "2026-11-24): 3v3 gives C(6,3)=20 allocations and a minimum exact "
                         "two-sided p = 0.10, so level/method/confirm_when above apply to W1f "
                         "and to no n=2 window. W1's four records are the pre-amendment "
                         "baseline it is read against and are never pooled into it.",
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
