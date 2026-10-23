#!/usr/bin/env python3
"""DIVERGENCE-WINDOW controller - PARENT-only diagnostic (agents never run --execute).

DEFAULT IS A DRY RUN: fresh production snapshot + config currency + derivation plan + preflight.
ZERO signals, ZERO GPU, ZERO HTTP inference.  `--execute` (parent) runs three FRESH, SEQUENTIAL
isolated server sessions on port 8019 ONLY, in this order:

    1. target_only      pure AR            (proto-server-swift15-target_only.json)
    2. dflash_w1        Inco width 1, T=2  (proto-server-swift15-dflash_w1.json)
    3. dflash_w3        Inco width 3, T=4  (proto-server-swift15-dflash_w3.json)

Each session is ONE server start, ONE fresh request, the SAME prompt text
(greedy_gate.GATE_PROMPTS[0]) greedy (strata_bench.chat: temperature 0, reasoning_effort "none",
enable_thinking false), max 32 new tokens.  The texts, usage, timings, ttft, pieces and errors are
recorded VERBATIM.  Plausible text is never correctness evidence.  THIS IS AN EXPLICIT
CORRECTNESS-DIAGNOSTIC VARIANT: its timings are NEVER production performance numbers, and the
window prints that label in every record.

OPT-IN SUBSET (--requests, valid ONLY with --prompt-set and NEVER with --reenable): run ONLY the given
original ONE-BASED gate-set positions (1..8) of greedy_gate.GATE_PROMPTS, in the ORDER GIVEN, one
fresh request per prompt on each arm's own server, with the SAME three arms and the SAME runtime
controls.  It is a SUBSET DIAGNOSTIC ONLY: the FULL eight-prompt gate is NEVER established by it, the
standard gates_ok stays False, and the separate subset_diagnostic_ok covers the strict comparisons of
the SELECTED responses plus the runtime gates.  Every plan row and every response records the
original full-set index beside the sequential index the comparisons use, and the status line says
SUBSET DIAGNOSTIC PASS/FAIL, never 'ALL OK'.  An empty selection, a duplicate, 0, an out-of-range
index, --requests without --prompt-set and --requests with --reenable are refused BEFORE any
snapshot, signal, config read, port probe or HTTP action.  The default (no --requests) is
byte-identical to the existing single-prompt and full-eight-prompt behavior, same controls, same gates.

WHAT DERIVATION DOES (the clone is the source; production configs are NEVER written):
Every mode launches a DERIVED config = the CURRENT clone for that mode + ONLY these allowlisted
overrides, under a unique per-window folder at 0600 with a unique per-window engine log:

    --expert-cache 2000            the proven CUDA0 byte pin (measured equal 2904 slots in all
                                   four arms, windows 180344/205434; generate.cpp:6244-6263)
    --prefill 512                  the engine's OWN room remedy for a split ("... a smaller
                                   --max-context, --prefill 512, a smaller --vram-reserve-mib ...",
                                   generate.cpp:6646-6647).  4096 -> 512 is INTENTIONAL,
                                   CORRECTNESS-ONLY (smaller own buffers so a no-borrow split
                                   fits); it is documented, not a performance choice
    --no-prefill-borrow            parse 2579; pf_borrow = !no_prefill_borrow && !profile (4421)
                                   -> the serve loan block (8477-8705) is skipped entirely, so the
                                   prompt loan is 0 on EVERY arm (the measured 1345-vs-1398 control
                                   gap of window 205434 is designed OUT, not explained away)
    --adapt-swaps 0                parse 2712; with adapt_swaps <= 0 the usage table is never
                                   assigned (10094-10095) so the adaptive tier never runs
                                   (16898 `if (!drive.d.usage.empty() && ...)`) - the usage/timing
                                   coupling of L18 F8 is designed OUT
    --prompt-cache 0 --conversation-cache-mib 0     parse 2622 / 2624-2648; the park/restore state
                                   moves of F9 are designed OUT.  Both flags together, always:
                                   serve warns "conversation caching is disabled by --prompt-cache 0"
                                   only while --conversation-cache-mib > 0 (2896-2899) - with mib 0
                                   there is no warning (the source's rule is a WARNING, not a
                                   refusal; this lane reports it as measured from source)
    dflash arms only: --spec-min-p 0 --lookup-chain 0
                                   --spec-min-p 0 marks the flag SET (2612), so the dflash default
                                   0.5 (5497) does not apply, and the window's min-p truncation
                                   (14549, 14777, 14810 `if (req_spec_min_p > 0.0)`) never fires:
                                   the FULL proposal width is exercised.  --lookup-chain 0 (2656)
                                   keeps sfx_on false (14338) so only the drafter's rows exist.
                                   Widths match spec exactly (the clone's own diffs): width 1 =
                                   --spec 2 --dflash-block 1, width 3 = --spec 4 --dflash-block 3,
                                   dflash_k = min(block, spec-1) (5500) -> T = k+1 = 2 and 4
    non-dflash arms only, when --equalize-mib DELTA is given:
    --vram-reserve-later-mib raised by DELTA   the source-proven CUDA1 room equalizer (the later
                                   stage reserve IS this flag, 4499-4501); the AR arms' room
                                   shrinks onto the drafter arm's.  Expected DELTA at chunk 512
                                   with no borrowing is ~1000 = kDrafterMib (4441) - the ONLY
                                   remaining asymmetry - and the run's own room lines re-derive it.

CHILD ENV on every arm: STRATA_IQ_MT_MIN=1 (CPU IQ grouping off, native_expert.cpp:152-170),
STRATA_ROUTE_TAIL_SKIP=0 (tail-skip off, generate.cpp:10044-10052), STRATA_VERIFY_COMPLEMENT=1
(CUDA0 read-back membership checksum, new 6513 / old 6111).

ABLATION CHILD ENV (one variable per variant, CHILD processes ONLY - DW_ABLATION_ENV carries the
citations): 'adaptation' adds STRATA_DECODE_TIMING=1, the ONLY gate on the serve path's per-request
activity line "strata decode timing: N windows, ... , X.Y swaps/window" (generate.cpp:14392 gates
15487/15048 prints; the rate is (adapt_nswaps - dt_sw0)/windows at 15499, counters reset per request
14402-14406; nothing but printing depends on the variable).  The CLI trace variable
STRATA_TRACE_ADAPT (16571/16607) is INERT on serve - the serve tier lambda 10368-10411 prints no
trace - so this variant does NOT request it.  'borrowing' adds
STRATA_SPLIT_OWN_BUFFERS=0, the source-verified force-borrow control: at chunk 512 the split's
own-buffers rule takes ownership and plans NO loan (8672-8686, the 8760 line) unless own_ok
(8144-8147, 8668-8671) is switched off; STRATA_SPLIT_OWN is NOT the knob (3037 requires
!o.no_prefill_borrow).  The default mode and 'conversation-cache' keep the pin trio EXACTLY.  Each
variant's variable is recorded per arm (env, child_env_extra) and PROVEN from the child's own
/proc/<engine>/environ, and is never exported in the parent's os.environ.

RUNTIME GATES, fail-closed, BEFORE any prompt of the offending mode:
  A. RESIDENCY: the mode's CURRENT-session startup log is fingerprinted (CUDA0 slots/prefilled +
     the read-back CHECKSUM proof, every stage's layers/slots/filled/pairs, the FINAL-CHUNK proof,
     the NO-BORROW proof, the ADAPT-OFF proof) and compared with the FIRST mode's key.  Unequal,
     or any proof missing => NO prompts run for that mode, the window stops, production restores,
     and the CUDA1 room gap in MiB is recorded as the --equalize-mib value for the rerun.  An
     absent proof carries a per-fingerprint nonce, so two unproven arms can NEVER compare equal.
  B. NO-LOAN PROOF (the L18 F3 gate defect, fixed for this window): the key requires the POSITIVE
     serve line "the prompt path allocates its own buffers (too few cache slots to borrow)"
     (generate.cpp:8760, the `any_loan == false` branch) AND the absence of every loan line
     ("CUDA<N> prompt path borrows X of its Y slots", 8736-8738; "the prompt path borrows N CUDA0
     cache slots", 8732-8733).  The ABSENCE of the loan line alone is NEVER the proof: the loan
     lines come from the borrowing path only (the block is entered only `if (pf_borrow && d_res)`,
     8477), so their silence can also mean a path that never reached them.  The CLI path has its
     own positive line ("strata generate: prompt path allocates its own buffers (no cache slots to
     borrow)", 16060) - the window runs the SERVE path, so it demands the serve line; the flag's
     parse (2579) and its `pf_borrow` meaning (4421) are the same on both paths.
  C. ADAPT OFF: the verified engine cmdline must carry --adapt-swaps 0 (engine_identity checks
     every config flag by inclusion); the adaptive-tier log lines are recorded as diagnostics.
  D. DRAFTERS MUST PROPOSE: a dflash arm with ZERO drafts offered is a MODE FAILURE (vacuous
     equality), and a silent target-only fallback line in the log is a MODE FAILURE; target_only
     with any draft offered is a MODE FAILURE (the AR baseline is invalid).
  E. EVIDENCE PROVENANCE: every engine-log copy is the CURRENT session's bytes only (byte offset +
     inode recorded immediately before that mode's launch, model_window.draft_evidence).

SAFETY (inherited from the reviewed helpers, unchanged): the ORIGINAL 8009 production proxy is
stopped only through window.stop_prod's verified identity path, and no GPU work runs until the
whole verified family is gone; every mode's OWNED prototype family (proxy pid + every recorded
child, pinned by pid and start ticks, the 8019 listener, any process whose exe is the launched
binary) must be gone before the next mode starts and before any restore; the restore is REFUSED
while any prototype survivor lives - production is NEVER loaded alongside a prototype; no global
kill anywhere; SIGTERM/SIGINT become WindowSignal and take the same cleanup path; every capture
file is 0600 under a unique window label; nothing OpenCode-related is ever matched; secret-looking
env values are masked.

WHAT THIS LANE DOES NOT CLAIM: no independent target tap/head truth exists, so the window
compares OUTPUTS ONLY.  serve has NO target-state probe (--window-hashes / STRATA_DUMP_FIRST_LOGITS
are CLI-only, generate.cpp:16717-16721/16886), so a first-token difference is NEVER attributed to
the head or the prompt state from text alone - the record says "text-only" and the parent decides.
No CLI probe, no engine change, no benchmarks in this window.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import window as W                                       # shared verified snapshot/stop/restore
import model_window as mw                                # shared currency/identity/start/stop/evidence
import greedy_gate as G                                   # shared fingerprint regexes + residency key

DW_MODES = [("target_only", 1), ("dflash_w1", 1), ("dflash_w3", 3)]      # this exact order
DW_PIN = 2000                                             # CUDA0 byte pin (measured 2904 slots)
DW_CHUNK = 512                                            # the engine's own room remedy (6646-6647)
DW_MAX_TOKENS = 32                                        # per the mission: at most 32 new tokens
DW_ENV = dict(G.PIN_ENV)                                   # the proven pin trio, child env only
DW_PROMPT = G.GATE_PROMPTS[0][1]                            # ONE fresh prompt, identical in all arms
DW_PAIRS = [("AR vs dflash_w1", "target_only", "dflash_w1", True),
            ("AR vs dflash_w3", "target_only", "dflash_w3", True)]
DW_ALLOW = {"--expert-cache", "--prefill", "--no-prefill-borrow", "--adapt-swaps",
            "--prompt-cache", "--conversation-cache-mib", "--spec-min-p", "--lookup-chain",
            "--vram-reserve-later-mib"}
DW_VALUELESS = {"--no-prefill-borrow"}                    # arity from the source parse (2579)
LOGS, OPS = W.LOGS, W.OPS
NO_BORROW_LINE = "the prompt path allocates its own buffers"      # generate.cpp:8760 (serve)
FP_CUDA0_HEAD_LOAN = re.compile(r"the prompt path borrows (\d+) CUDA0 cache slots")
TIER_MARKS = ("adaptive tier", "adaptive swap copy failed", "swaps/window", "adaptive overlap")

# ------------------------------------------------------------------ the bounded ablation (the NEXT step)
# The controlled configuration reproduced byte-identical output (ops/divergence-20261023-004442 and
# the eight-prompt rerun).  The next step re-enables ONE control GROUP per window, on all three arms,
# derived from the CURRENT production clone for that group.  Nothing else changes: pin 2000, chunk
# 512, the target pack, the split, the child env, the drafter widths, the max tokens, the equalizer.
DW_REENABLE = ("adaptation", "borrowing", "conversation-cache")
DW_SOURCE_DEFAULTS = {                                     # verified in c938100b, generate.cpp
    "--adapt-swaps": ("96", "generate.cpp:725 'int adapt_swaps = 96' (the flag's own default; "
                               "--adapt-every default 4, 694; the tier needs both > 0, 10094)"),
    "--prompt-cache": ("6", "generate.cpp:729 'int prompt_cache = 6' (help 887 'default 6')"),
    "--conversation-cache-mib": ("0", "generate.cpp:730 'int64_t conversation_cache_mib = 0' "
                                       "(help 889 'default 0 = off')"),
}
# The POSITIVE runtime marks of each group (a group turned ON must show one of these; an absence is
# never the proof when a positive line exists in the source).
DW_LOAN_MARKS = ("the prompt path borrows", "prompt path borrows")          # 8732-8739, 8746-8750
# L20 (source check, no GPU run): the marks below exist ONLY on paths this window does not run, so a
# serve-split adaptation arm has NO mark to find and fails closed - by design, never as a silent pass.
#   "ADAPT round=" is the CLI generate loop's trace only (16609-16614, 16690-16692, trace_adapt 16607).
#   "adaptive tier asynchronous" / "swaps per round" need --adapt-async (10615-10616).
#   "the adaptive tier's evictions reach" needs evict_sync (10360-10362), set only with
#   STRATA_ADAPT_LAG>1 / STRATA_ADAPT_NOWAIT (10279-10280).
# The SERVE adapt lambda (10368-10411) has NO trace prints at all.  The serve path's own activity line
# is "%.1f swaps/window" inside "strata decode timing" (15490-15493; the blocking tier's adapt_nswaps
# counter, declared at 10366 and incremented at 10412), printed only under STRATA_DECODE_TIMING
# (14392).  The tier IS supported on a
# split: usage is assigned at 10094 when adapt_every>0 && adapt_swaps>0 && !all_experts_resident, the
# serve DFlash loop ticks it every --adapt-every windows (15325-15326), and the serve lambda resolves
# each swap to the owning stage's cache (home_of 10403-10410, resident_stage_swaps 10411).  The
# startup line "adaptive overlap: off: not on a layer split" gates STRATA_ADAPT_OVERLAP ONLY (10166,
# 10223; its scope is stated at 10158) and proves nothing about the tier.  Marks left UNCHANGED:
# retuning this group's proof is the parent's call, not a silent rewrite of the gate.
# L21: the marks are the SERVE ones, because this window runs the serve path.  Dropped from the
# demands (they exist only on paths the window does not run, cited above): "ADAPT round=" (the CLI
# generate loop's trace, 16609-16614/16690-16692), "adaptive tier asynchronous" and "swaps per
# round" (--adapt-async, 10615-10616), "the adaptive tier's evictions reach" (evict_sync,
# 10279-10280/10360-10362).  Requiring them on serve demanded lines that cannot exist.
DW_ADAPT_ON_MARKS = ("swaps/window",)      # 15490-15493, under STRATA_DECODE_TIMING (14392)
# The SERVE decode-timing line, per request, exactly as the source formats it (15490-15493):
#   "strata decode timing: %lld windows, ... adapt wait %.2f join %.2f, %.1f swaps/window"
# The last number is (adapt_nswaps - dt_sw0) / windows (15499, with `ajob ? a_swapped : adapt_nswaps`;
# adapt_nswaps declared 10366 and incremented at 10412) - a RATE over that request's windows.  The
# counters reset per request (14402-14406), so one line per request.  The engine prints NO swap
# total, so this tool computes NONE: rate and window count are recorded exactly as printed.
DW_DECODE_TIMING_RE = re.compile(r"strata decode timing: (\d+) windows,.*?([\d.]+) swaps/window")
DW_CACHE_ON_MARKS = ("conversation cache:", "checkpoint restore", "checkpoint save")  # 9691, 1806, 1798

# Each ablation variant needs ONE extra CHILD-environment variable, and the source says why.
# ADAPTATION: STRATA_DECODE_TIMING=1.  The serve tier lambda (10368-10411) prints NO trace lines, so
# the serve path's only activity evidence is the per-request decode-timing line's "swaps/window"
# (15490-15493, printed only under STRATA_DECODE_TIMING at 14392; print-only, no engine behavior
# depends on it - the variable gates prints at 15048 and 15487 and nothing else).  L20 §1: the tier
# IS live on a split (10094, 10403-10411, ticked at 15325-15326), it simply had no visible record.
# The CLI trace variable STRATA_TRACE_ADAPT is NOT requested here: it guards prints that exist only
# in the CLI generate loop (16571, 16607 -> 16609-16614, 16690-16692), so on this serve window it is
# INERT - it proved nothing in 040201 (zero ADAPT lines) and asking for it wasted a proof gate.  The
# honest child-env set for this variant is therefore the timing variable ALONE.
# BORROWING: at --prefill 512 dropping --no-prefill-borrow is NOT enough (the 040648 run proved it):
# the split's OWN-BUFFERS rule takes ownership first - a part whose card has free VRAM >= the chunk's
# buffers + 1.5 GiB lends nothing, its `first` goes to -1, and the serve line 8760 prints
# ("the prompt path allocates its own buffers"): generate.cpp:8672-8686, with the same test in the
# lend-region sizing at 8156-8162.  The VERIFIED force-borrow control is STRATA_SPLIT_OWN_BUFFERS=0:
# `own_ok = v == nullptr || v[0] != '0'` (8145-8147 and 8668-8671), so '0' switches that rule off,
# every part keeps its loan, and the positive loan lines become reachable (8732-8739, 8746-8750).
# NOT the knob: STRATA_SPLIT_OWN (3036-3068) only FORCES the flag (3037 requires !o.no_prefill_borrow,
# so it can never undo --no-prefill-borrow); unset/0 is the plain default, which at 512 lands in the
# own-buffers rule above anyway.
# CHILD ENVIRONMENT ONLY, set per child process by this controller. It is NEVER exported in the
# parent's os.environ: window.py:294 builds the restore environment from os.environ.copy(), so a
# shell prefix would follow the variable straight into the RESTORED production process.
DW_ABLATION_ENV = {"adaptation": {"STRATA_DECODE_TIMING": "1"},
                   "borrowing": {"STRATA_SPLIT_OWN_BUFFERS": "0"}}


def child_env_extra(reenable: str | None) -> dict:
    """The child-environment addition for ONE ablation variant (empty for the default controlled mode
    and for the conversation-cache variant - their child env stays exactly the proven pin trio).
    'adaptation' adds the variable its SERVE activity line needs (STRATA_DECODE_TIMING);
    'borrowing' adds the source-verified force-borrow control (see DW_ABLATION_ENV for the
    generate.cpp citations)."""
    return dict(DW_ABLATION_ENV.get(reenable or "", {}))


# ------------------------------------------------------------------ per-window paths
def dw_dir_for(window: str) -> pathlib.Path:
    """The unique per-window derived-config folder (reruns never mix, never overwrite)."""
    return OPS / f"{window}-dw"


def dw_log(window: str, mode: str) -> str:
    return str(LOGS / f"engine-divwindow-{window}-{mode}.log")


# ------------------------------------------------------------------ derivation (the clone is the source)
def derive(window: str, mode: str, cfg: dict, base_dir: pathlib.Path,
           equalize_mib: int = 0, reenable: str | None = None) -> tuple[pathlib.Path, dict, list[dict]]:
    """The derived config = the CURRENT clone + ONLY the allowlisted overrides, with the unique
    per-window log.  Overrides are RECORDED (flag/from/to, plus action/effective/citation for the
    ablation).  Valueless flags are added WITHOUT a value (the source parse consumes none,
    generate.cpp:2579).  `reenable` (None = the passing controlled mode, byte-identical to the
    measured runs) turns ONE control group back on, on every arm, from the CURRENT clone for that
    group - a flag the clone does not carry falls back to the SOURCE-VERIFIED default with its
    citation recorded, never a guess.  Every other group stays controlled."""
    d = copy.deepcopy(cfg)
    args = list(d.get("args", []))
    overrides: list[dict] = []
    src = dict(mw.flags(args))

    def apply(flag: str, to: str | None) -> None:
        if flag in args:
            i = args.index(flag)
            if flag in DW_VALUELESS:
                return                                        # already present, nothing to change
            if args[i + 1] == to:
                return                                        # already the intended value
            overrides.append({"flag": flag, "from": args[i + 1], "to": to})
            args[i + 1] = to
        else:
            # pure mutation (greedy_gate's pin_derive lesson, window 193027): `args +=` rebinds
            overrides.append({"flag": flag, "from": None, "to": None if flag in DW_VALUELESS else to})
            args.extend([flag] if flag in DW_VALUELESS else [flag, to])

    def record(flag: str, frm: str | None, to: str | None, action: str, effective: str,
               citation: str) -> None:
        overrides.append({"flag": flag, "from": frm, "to": to, "action": action,
                          "effective": effective, "citation": citation})

    def remove(flag: str, action: str = "removed-for-ablation") -> None:
        """Drop a diagnostic flag entirely (the ablation of a group is the CLONE's shape)."""
        if flag in args:
            i = args.index(flag)
            n = 1 if flag in DW_VALUELESS else 2
            overrides.append({"flag": flag, "from": (None if flag in DW_VALUELESS else args[i + 1]),
                              "to": None, "action": action})
            del args[i:i + n]
        else:
            overrides.append({"flag": flag, "from": None, "to": None, "action": action})

    def restore_from_clone(flag: str) -> None:
        """Restore ONE flag to the CURRENT clone's value; absent in the clone -> the source-verified
        default WITH its citation (never guessed)."""
        v = src.get(flag)
        if v is None:
            dv, cite = DW_SOURCE_DEFAULTS[flag]
            apply(flag, dv)
            if overrides:
                overrides[-1]["effective"] = dv
                overrides[-1]["citation"] = f"the clone carries no {flag}: {cite}"
        else:
            apply(flag, v)
            record(flag, v, v, "restored-from-the-current-clone", v,
                   "the CURRENT production clone for this group (config-currency verified)")

    apply("--expert-cache", str(DW_PIN))                    # controlled in EVERY variant
    apply("--prefill", str(DW_CHUNK))                       # controlled in EVERY variant
    # the ONLY group-specific changes:
    if reenable == "borrowing":
        remove("--no-prefill-borrow")                       # borrowing back on: the clone's shape
    else:
        apply("--no-prefill-borrow", None)
    if reenable == "adaptation":
        if src.get("--adapt-swaps") in (None,):
            remove("--adapt-swaps", "diagnostic-adapt-swaps-0-dropped")
            overrides[-1]["effective"] = DW_SOURCE_DEFAULTS["--adapt-swaps"][0]
            overrides[-1]["citation"] = f"the clone carries no --adapt-swaps: {DW_SOURCE_DEFAULTS['--adapt-swaps'][1]}"
        else:
            restore_from_clone("--adapt-swaps")
    else:
        apply("--adapt-swaps", "0")
    if reenable == "conversation-cache":
        restore_from_clone("--prompt-cache")
        restore_from_clone("--conversation-cache-mib")
    else:
        apply("--prompt-cache", "0")
        apply("--conversation-cache-mib", "0")
    if mode.startswith("dflash_w"):
        apply("--spec-min-p", "0")                          # controlled: full proposal width
        apply("--lookup-chain", "0")                        # controlled: sfx_on false (14338)
    elif equalize_mib:
        cur = dict(mw.flags(args)).get("--vram-reserve-later-mib")
        base = int(cur) if cur is not None and str(cur).lstrip("-").isdigit() else 0
        apply("--vram-reserve-later-mib", str(base + equalize_mib))
    d["args"] = args
    d["log"] = dw_log(window, mode)
    path = base_dir / f"proto-server-swift15-{mode}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(d, indent=2) + "\n")
    os.chmod(path, 0o600)
    return path, d, overrides


DW_REMOVAL_ACTIONS = {"removed", "removed-for-ablation", "diagnostic-adapt-swaps-0-dropped"}


def _num(v) -> int | None:
    s = str(v) if v is not None else ""
    return int(s) if s.lstrip("-").isdigit() else None


def validate_derivation(src: dict, derived: dict, overrides: list[dict], window: str, mode: str,
                        reenable: str | None = None) -> list[str]:
    """The derived config equals the source clone EXACTLY except the allowlisted overrides and the
    unique per-window log.  Anything else refuses the mode (fail closed).  For an ablation variant
    the SAME rule holds, and the variant's own shape is checked: ONLY the named control group may
    differ from the controlled configuration, the two untouched groups must stay controlled, and an
    ablation that would be a NO-OP (a group that ends up off anyway) refuses."""
    problems: list[str] = []
    expect = dict(mw.flags(src.get("args", [])))
    for o in overrides:
        if o["flag"] not in DW_ALLOW:
            problems.append(f"override {o['flag']} is not on the allowlist {sorted(DW_ALLOW)}")
        if o.get("to") is None and o.get("action") in DW_REMOVAL_ACTIONS:
            expect.pop(o["flag"], None)
        else:
            expect[o["flag"]] = o["to"]
    got = dict(mw.flags(derived.get("args", [])))
    if got != expect:
        problems.append(f"derived args {got} != source clone + allowlist {expect}")
    for key in src:
        if key in ("args", "log"):
            continue
        if derived.get(key) != src[key]:
            problems.append(f"derived key {key!r} differs from the source clone "
                            f"({derived.get(key)!r} != {src[key]!r}) - only the allowlist is permitted")
    for key in derived:
        if key not in src:
            problems.append(f"derived config carries an extra key {key!r}")
    if derived.get("log") != dw_log(window, mode):
        problems.append(f"derived log {derived.get('log')!r} is not the unique per-window log "
                        f"{dw_log(window, mode)}")
    flags_present = dict(mw.flags(derived.get("args", [])))
    if flags_present.get("--prompt-cache") == "0" and flags_present.get("--conversation-cache-mib") != "0":
        problems.append("--prompt-cache 0 without --conversation-cache-mib 0: serve disables "
                        "conversation caching with a warning in that mix (generate.cpp:2896-2899) - "
                        "the window's park-off control requires both at 0")
    if mode.startswith("dflash_w") and flags_present.get("--spec-min-p") != "0":
        problems.append(f"{mode}: --spec-min-p is {flags_present.get('--spec-min-p')!r}, not '0' - "
                        "the full proposal width (no min-p truncation, 14549/14777) is not proven")
    width = int(mode.removeprefix("dflash_w")) if mode.startswith("dflash_w") else None
    if width:
        want = dict(mw.flags(derived.get("args", [])))
        if want.get("--dflash-block") != str(width) or want.get("--spec") != str(width + 1):
            problems.append(f"{mode}: --dflash-block {want.get('--dflash-block')} / --spec "
                            f"{want.get('--spec')} do not match width {width} (T = min(block,spec-1)+1, "
                            "generate.cpp:5500)")
    # ---- the ablation variant's own shape: ONLY the named group changed, the rest stays controlled
    borrow_off = "--no-prefill-borrow" in flags_present
    adapt_off = flags_present.get("--adapt-swaps") == "0"
    cache_off = flags_present.get("--prompt-cache") == "0" and flags_present.get("--conversation-cache-mib") == "0"
    if reenable is None:
        if not (borrow_off and adapt_off and cache_off):
            problems.append("the controlled configuration requires --no-prefill-borrow, --adapt-swaps 0 "
                            f"and both cache flags at 0 (got borrow_off={borrow_off} adapt_off={adapt_off} "
                            f"cache_off={cache_off}) - this is the measured passing shape")
    elif reenable == "borrowing":
        if borrow_off:
            problems.append("ablation 'borrowing': --no-prefill-borrow is still present, so borrowing "
                            "was NOT re-enabled - a no-op ablation is refused")
        if not (adapt_off and cache_off):
            problems.append("ablation 'borrowing' must leave the OTHER groups controlled "
                            f"(adapt_off={adapt_off} cache_off={cache_off})")
    elif reenable == "adaptation":
        eff = _num(flags_present.get("--adapt-swaps"))
        if eff is None:
            eff = int(DW_SOURCE_DEFAULTS["--adapt-swaps"][0])     # flag absent = the source default 96
        if not borrow_off:
            problems.append("ablation 'adaptation' must leave borrowing OFF (--no-prefill-borrow present)")
        if not cache_off:
            problems.append("ablation 'adaptation' must leave the caches OFF (both flags at 0)")
        if flags_present.get("--adapt-swaps") == "0" or eff is None or eff <= 0:
            problems.append(f"ablation 'adaptation': the effective --adapt-swaps is {eff!r}, not > 0 - "
                            "adaptation was NOT re-enabled (the tier needs adapt_swaps > 0 and "
                            "adapt_every > 0, generate.cpp:10094; default 96 at 725, adapt_every 4 at 694)")
    elif reenable == "conversation-cache":
        pc, cc = _num(flags_present.get("--prompt-cache")), _num(flags_present.get("--conversation-cache-mib"))
        if not borrow_off:
            problems.append("ablation 'conversation-cache' must leave borrowing OFF")
        if not adapt_off:
            problems.append("ablation 'conversation-cache' must leave adaptation OFF (--adapt-swaps 0)")
        if not (pc and pc > 0 and cc and cc > 0):
            problems.append(f"ablation 'conversation-cache': --prompt-cache {pc!r} / "
                            f"--conversation-cache-mib {cc!r} are not both > 0 - parking/prompt caching "
                            "would stay OFF (the cache budget defaults to 0 = off, generate.cpp:730), "
                            "so this is a no-op ablation and is refused")
    else:
        problems.append(f"unknown ablation variant {reenable!r} (the choices are {list(DW_REENABLE)})")
    return problems


def ablation_effective_mode(overrides: list[dict], flag: str) -> str | None:
    """The value a flag EFFECTIVELY has in the derived config, as recorded (clone value, or the
    cited source default when the clone carries no flag)."""
    flags = {o["flag"]: o for o in overrides}
    o = flags.get(flag)
    if o is None:
        return None
    if o.get("effective"):
        return str(o["effective"])
    if o.get("to") is not None:
        return str(o["to"])
    return DW_SOURCE_DEFAULTS.get(flag, (None,))[0]


# ------------------------------------------------------------------ the runtime fingerprint
def dw_fingerprint(rec: dict, argv_prefill: str | None, argv: dict | None = None,
                   reenable: str | None = None) -> dict | None:
    """The mode's startup fingerprint from its CURRENT-session log copy only (mirrors
    greedy_gate.residency_fingerprint, with THIS window's chunk value and the two extra proofs:
    the positive NO-LOAN proof and the ADAPT-OFF proof).  Rounded GiB strings are DIAGNOSTICS:
    recorded, printed, never compared."""
    p = (rec.get("engine_log") or {}).get("per_window_copy")
    if not p:
        return None
    try:
        text = pathlib.Path(p).read_text(errors="replace")
    except OSError:
        return None
    m0 = G.FP_CUDA0.search(text)
    mf = G.FP_CUDA0_FILL.search(text)
    mc = G.FP_CHECKSUM.search(text)
    stages = {int(m.group(1)): {"layers": f"{m.group(2)}-{m.group(3)}", "slots": int(m.group(4)),
                                "gib": m.group(5), "filled": int(m.group(6)), "pairs": int(m.group(7))}
              for m in G.FP_STAGE.finditer(text)}
    chunks = [(int(m.group(1)), int(m.group(2)) if m.group(2) else int(m.group(1)))
              for m in G.FP_CHUNK.finditer(text)]
    borrows = [{f"cuda{m.group(1)}_borrows": int(m.group(2)), "of_its": int(m.group(3))}
               for m in G.FP_BORROW.finditer(text)]
    rooms = {int(m.group(1)): float(m.group(2)) for m in G.FP_ROOM.finditer(text)}
    if m0 is None or mf is None or not stages:
        return None                                       # startup residency lines incomplete - fail closed
    chk = mc.group(1) if mc else None
    checksum_proof = ["checksum", chk] if mc else ["checksum-absent-UNPROVEN", os.urandom(8).hex()]
    chunk_final = chunks[-1][1] if chunks else None
    trim = [c for c in chunks if c[1] != c[0]]
    if chunks:
        chunk_proof = ["log-chunk", chunk_final, "trim-observed" if trim else "no-trim"]
    elif str(argv_prefill) == str(DW_CHUNK) and not G.FP_CHUNK.search(text):
        chunk_proof = ["argv-pinned-DW_CHUNK-no-chunk-or-trim-line", DW_CHUNK]
    else:
        chunk_proof = ["chunk-UNVERIFIED", os.urandom(8).hex()]
    head_loan = FP_CUDA0_HEAD_LOAN.search(text)
    loan_lines = [ln[:220] for ln in text.splitlines()
                  if any(k in ln for k in DW_LOAN_MARKS) or "lend sizing" in ln][:12]
    if NO_BORROW_LINE in text and not borrows and head_loan is None:
        no_borrow_proof = ["no-loan-PROVEN-positive-line", "generate.cpp:8760"]
    else:
        no_borrow_proof = ["no-loan-UNPROVEN", os.urandom(8).hex()]
    loan_proof = (["loan-PROVEN-positive-lines",
                    "generate.cpp:8732-8739 (the head line and one per stage)"]
                  if (head_loan is not None or borrows) else ["loan-UNPROVEN", os.urandom(8).hex()])
    adapt_line = (argv or {}).get("--adapt-swaps")
    if reenable == "adaptation":
        eff = adapt_line if adapt_line is not None else DW_SOURCE_DEFAULTS["--adapt-swaps"][0]
        adapt_proof = ([f"adapt-active-PROVEN-argv-{eff}", str(eff), DW_SOURCE_DEFAULTS["--adapt-swaps"][1]]
                       if (_num(eff) or 0) > 0 and adapt_line != "0"
                       else ["adapt-active-UNPROVEN", os.urandom(8).hex()])
    else:
        adapt_proof = (["adapt-off-PROVEN-argv-adapt-swaps-0", "0"] if adapt_line == "0"
                       else ["adapt-off-UNPROVEN", os.urandom(8).hex()])
    pc, cc = (argv or {}).get("--prompt-cache"), (argv or {}).get("--conversation-cache-mib")
    cache_on = (_num(pc) or 0) > 0 and (_num(cc) or 0) > 0
    if reenable == "conversation-cache":
        # The variant CLAIMS the group is ON: the proof record says whether THAT claim is proven. A
        # 0/0 pair is not a success here - it is the ON claim with no evidence, so it carries a nonce
        # and the gate fails the arm closed.
        cache_proof = ([f"cache-active-PROVEN-argv-prompt-cache-{pc}-conversation-cache-mib-{cc}", pc, cc]
                       if cache_on else ["cache-UNPROVEN-for-ON-variant", os.urandom(8).hex()])
    elif pc == "0" and cc == "0":
        cache_proof = ["cache-off-PROVEN-argv-both-0", "0", "0"]
    else:
        cache_proof = ["cache-UNPROVEN", os.urandom(8).hex()]
    return {"cuda0": {"slots": int(m0.group(1)), "gib": m0.group(2), "prefilled": int(mf.group(1)),
                      "prefill_target": int(mf.group(2)), "checksum": chk,
                      "checksum_proof": checksum_proof},
            "stages": stages,
            "room_gib": rooms,
            "chunk_requested": chunks[0][0] if chunks else None,
            "chunk_final": chunk_final,
            "chunk_lines": chunks,
            "chunk_trim_observed": bool(trim),
            "chunk_proof": chunk_proof,
            "argv_prefill": str(argv_prefill) if argv_prefill is not None else None,
            "borrows": borrows,
            "head_loan_line": head_loan.group(0) if head_loan else None,
            "loan_lines": loan_lines,
            "loan_proof": loan_proof,
            "no_borrow_line_present": NO_BORROW_LINE in text,
            "no_borrow_proof": no_borrow_proof,
            "adapt_proof": adapt_proof,
            "cache_proof": cache_proof,
            "tier_lines": [ln[:200] for ln in text.splitlines() if any(k in ln for k in TIER_MARKS)][:20],
            "checksum_present": mc is not None}


def dw_key(fp: dict, reenable: str | None = None) -> tuple:
    """The compared identity: greedy_gate.residency_key (CUDA0 slots/prefilled/CHECKSUM proof, every
    stage's dev/layers/slots/filled/pairs, the FINAL-CHUNK proof) PLUS this window's NO-LOAN proof,
    ADAPT proof and CACHE proof.  Every proof is fail-closed: an absent one carries a per-fingerprint
    nonce, so two unproven arms can never compare equal.

    For an ABLATION variant (`reenable`), ONLY the deliberately-changed group's proof slot is
    replaced by a fixed label: the CONTROLLED components (cache slots, prefilled, checksum, every
    stage, the final chunk) are still compared exactly, and the ablated group is asserted by its own
    positive proof in the gate (and its geometry compared as the variable being studied).  This
    cannot manufacture a pass: an ablation variant is never allowed to report the standard
    correctness gate as established."""
    base = G.residency_key(fp)
    proofs = [tuple(fp["no_borrow_proof"]), tuple(fp["adapt_proof"]), tuple(fp["cache_proof"])]
    if reenable == "borrowing":
        proofs[0] = ("ablation-UNDER-STUDY-loan-group",)
    elif reenable == "adaptation":
        proofs[1] = ("ablation-UNDER-STUDY-adapt-group",)
    elif reenable == "conversation-cache":
        proofs[2] = ("ablation-UNDER-STUDY-cache-group",)
    return base + tuple(proofs)


# ------------------------------------------------------------------ the prompt plan + the requests
def select_prompts(prompt_set: bool, requests: list[int] | None = None) -> list[tuple[str, str]]:
    """ONE fresh prompt (the default, byte-identical to the L19 behavior) or the FULL eight-prompt
    gate set from greedy_gate.GATE_PROMPTS, in order, one request per prompt, sequentially.

    SUBSET DIAGNOSTIC (opt-in, validated by validate_requests BEFORE any action): `requests` are
    original ONE-BASED positions in greedy_gate.GATE_PROMPTS (1..8); exactly those prompts are
    selected, in the REQUESTED order.  With requests is None the selection is byte-identical to the
    existing default (one prompt) and --prompt-set (the full eight)."""
    if requests is None:
        return list(G.GATE_PROMPTS) if prompt_set else [G.GATE_PROMPTS[0]]
    return [G.GATE_PROMPTS[i - 1] for i in requests]


def validate_requests(values: list[int] | None, prompt_set: bool,
                      reenable: str | None) -> tuple[list[int] | None, str]:
    """PURE pre-action gate for --requests (CPU only: no snapshot, no signal handler, no config read,
    no port, no HTTP, no GPU, nothing written).  Returns (selected_indices, refusal); a non-empty
    refusal is printed and the window exits 2 BEFORE any window action.  Rules: valid ONLY with
    --prompt-set and NEVER with --reenable; indices are original one-based gate-set positions 1..8;
    the REQUESTED ORDER is preserved; an empty selection, duplicates, non-integers and out-of-range
    values are refused.  Returns (None, "") for the default (no --requests)."""
    if values is None:
        return None, ""
    sel = list(values)
    if not prompt_set:
        return None, "REFUSE: --requests is valid ONLY with --prompt-set (a subset of the gate set)"
    if reenable:
        return None, (f"REFUSE: --requests is not supported together with --reenable {reenable} - "
                      "unsupported combination (an ablation variant keeps its own selection)")
    n = len(G.GATE_PROMPTS)
    if not sel:
        return None, f"REFUSE: --requests needs at least one 1-based gate-set index (1..{n})"
    for v in sel:
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= n:
            return None, (f"REFUSE: --requests index {v!r} is not an original gate-set position "
                          f"(integers 1..{n} only)")
    dup = sorted({v for v in sel if sel.count(v) > 1})
    if dup:
        return None, f"REFUSE: --requests has duplicate indices {dup} - each prompt runs once per arm"
    return sel, ""


def prompt_indices(prompts: list[tuple[str, str]], requests: list[int] | None) -> list[int]:
    """The ORIGINAL full-set index of every selected prompt, aligned to `prompts` position by
    position: an explicit subset carries the requested indices in the requested order, and the
    defaults carry their own (the single default prompt IS gate-set position 1, the full set is 1..8)."""
    return list(requests) if requests else list(range(1, len(prompts) + 1))


def prompt_plan_rows(prompts: list[tuple[str, str]], indices: list[int], tokens: int | None) -> list[dict]:
    """THE prompt plan, built in ONE place so the dry-run plan and the executed window record are the
    SAME plan: the sequential index the comparison code uses, the ORIGINAL full-set index of that
    prompt, its class, its lengths, the bounded max tokens, and the SHA-256 of the prompt text."""
    return [{"n": i + 1, "class": c, "words": len(t.split()), "chars": len(t),
             "max_tokens": max_tokens_for(c, tokens),
             "max_tokens_requested": (tokens if tokens is not None else "class limit"),
             "sha256": hashlib.sha256(t.encode()).hexdigest(),
             "full_set_request": indices[i]}
            for i, (c, t) in enumerate(prompts)]


def subset_label(requests: list[int] | None, prompt_set: bool) -> str:
    """Honest scope words for the plan: an explicit --requests run is a SUBSET DIAGNOSTIC and is
    NEVER described as the full eight-prompt gate.  Without --requests the words are byte-identical
    to the existing wording ('1 fresh prompt' / '8-prompt gate set')."""
    if requests:
        return (f"SUBSET DIAGNOSTIC: requests {'+'.join(str(i) for i in requests)} of the 8-prompt "
                "gate set (selected responses ONLY)")
    return "8-prompt gate set" if prompt_set else "1 fresh prompt"


def max_tokens_for(cls: str, override: int | None) -> int:
    """The gate's own class limits (short 128 / medium 96 / long 64, greedy_gate.MAXT).  An override
    is BOUNDED: it can only LOWER a class limit (never above it, never below 1), and the requested
    value is recorded beside the value actually used."""
    base = G.MAXT[("short", "medium", "long").index(cls)]
    if override is None:
        return base
    return max(1, min(int(override), base))


def tokens_description(tokens: int | None, override: int | None = None) -> str:
    """What cap applies, worded HONESTLY: the gate's class limits, the DEFAULT single-prompt cap of
    32, or a user-supplied bounded override - the three cases are never conflated."""
    if tokens is None:
        return "class limits 128/96/64"
    if override is not None:
        return f"override {tokens} (bounded by the class limit)"
    return f"{tokens} (the default single-prompt cap)"


def run_dw_request(mode: str, rec: dict, note, prompts: list[tuple[str, str]],
                   max_tokens_override: int | None = None,
                   indices: list[int] | None = None) -> bool:
    """The greedy requests, thinking explicitly off, via the real bench client: one per prompt, in
    order, each a FRESH request on this arm's own server.  Records verbatim: the request text, the
    engine-observed prompt tokens, usage, timings, ttft, pieces, the full text and every error, and
    the per-request drafts the engine reported in the response timings (digest: draft_n /
    draft_n_accepted)."""
    sys.path.insert(0, f"{mw.CLONE}/tools")
    from strata_bench import chat, digest
    base = f"http://127.0.0.1:{mw.PROTO_PORT}"
    ok = True
    for n, (cls, text) in enumerate(prompts, 1):
        max_tokens = max_tokens_for(cls, max_tokens_override)
        full_idx = indices[n - 1] if indices else n          # ORIGINAL gate-set position of this row
        r = chat(base, "", [{"role": "user", "content": text}], max_tokens,
                 think=False, timeout=mw.REQ_TIMEOUT)
        d = digest(r)
        usage = r.get("usage") or {}
        rec["requests"].append({"n": n, "class": cls, "prompt": text, "prompt_words": len(text.split()),
                                "prompt_chars": len(text), "max_tokens": max_tokens,
                                "max_tokens_requested": (max_tokens_override if max_tokens_override
                                                         is not None else "class limit"),
                                "greedy": True,
                                "reasoning": "off (reasoning_effort=none, enable_thinking=false)",
                                "ok": d.get("ok"), "err": r.get("err"), "ttft_s": d.get("ttft_s"),
                                "total_s": round(r.get("total") or 0, 3), "pieces": r.get("pieces"),
                                "observed_prompt_tokens": usage.get("prompt_tokens"),
                                "observed_completion_tokens": usage.get("completion_tokens"),
                                "reused_tokens": d.get("reused_tokens"), "read_tokens": d.get("read_tokens"),
                                "drafts_offered": d.get("drafts_offered"),
                                "drafts_accepted": d.get("drafts_accepted"),
                                "usage": usage, "timings": r.get("timings"), "text": r.get("text"),
                                "full_set_request": full_idx})
        note(f"{mode}: request {n}/{len(prompts)} gate-set request {full_idx} ({cls}) "
             f"ok={d.get('ok')} err={str(r.get('err'))[:80]} "
             f"observed_prompt_tokens={usage.get('prompt_tokens')} "
             f"completion={usage.get('completion_tokens')} "
             f"drafts_offered={d.get('drafts_offered')} drafts_accepted={d.get('drafts_accepted')}")
        if not d.get("ok"):
            ok = False
    return ok


# ------------------------------------------------------------------ activation (drafter must propose)
def activation_gate(mode: str, rec: dict) -> list[str]:
    """DFlash arms: geometry + collector + VRAM lines, drafts offered > 0, no fallback line.
    AR arm: no drafter lines, no drafts offered.  (model_window.evaluate's dflash branch is
    width-3-labelled, so the width-parameterized arms are checked here.)"""
    ev = rec.get("evidence") or {}
    offered = sum(a["offered"] for a in ev.get("acceptance", []))
    startup = ev.get("startup_lines", [])
    fallback = [ln for ln in ev.get("fallback_lines", []) if "target-only decoding" in ln]
    watchdog = [ln for ln in ev.get("fallback_lines", [])
                if "timed out" in ln or "Unrecoverable native" in ln]
    rec["drafts_offered_total"] = offered
    bad: list[str] = []
    if mode.startswith("dflash_w"):
        if not [ln for ln in startup if "taps [" in ln]:
            bad.append(f"{mode}: no DFlash2 geometry line - the drafter never activated")
        if not [ln for ln in startup if "cross-GPU tap collection serial" in ln]:
            bad.append(f"{mode}: no cross-GPU tap-collection line - the collector is not enabled")
        if not [ln for ln in startup if "MiB of VRAM held" in ln]:
            bad.append(f"{mode}: no drafter VRAM line")
        if fallback:
            bad.append(f"{mode}: silent fallback to target-only in the engine log = MODE FAILURE")
        if offered == 0:
            bad.append(f"{mode}: NO draft was offered - the AR-vs-drafter equality is vacuous "
                       "(evidence is this session's log bytes only)")
    else:
        if [ln for ln in startup if "taps [" in ln] or [ln for ln in startup if "dflash: " in ln]:
            bad.append(f"{mode}: drafter lines in an AR arm - config/binary mismatch")
        if offered:
            bad.append(f"{mode}: offered {offered} drafts - not pure autoregressive, the AR baseline "
                       "is invalid")
    if watchdog:
        bad.append(f"{len(watchdog)} native-verification/watchdog line(s) in this session's log")
    return bad


# ------------------------------------------------------------------ comparison (text-only)
def compare_pair(label: str, a: list, b: list) -> dict:
    """Byte-exact comparison of CORRESPONDING responses, one row per prompt of the plan.  A missing
    response is never equal.  ZERO responses on either side is NEVER all_equal True - an arm that ran
    nothing has no equality evidence at all (the L19rev3 fail-closed rule)."""
    rows = []
    for i in range(max(len(a), len(b))):
        ta = a[i] if i < len(a) else None
        tb = b[i] if i < len(b) else None
        if ta is None or tb is None:
            eq, at = False, -1
        else:
            eq = ta == tb
            at = -1 if eq else next((k for k in range(min(len(ta), len(tb))) if ta[k] != tb[k]),
                                    min(len(ta), len(tb)))
        rows.append({"request": i + 1, "equal": eq, "first_diff_index": at,
                     "response_present": [ta is not None, tb is not None],
                     "excerpt_a": None if eq else (ta or "")[max(0, at - 40):at + 40],
                     "excerpt_b": None if eq else (tb or "")[max(0, at - 40):at + 40]})
    return {"label": label, "pair": None, "requests": rows, "n_requests": len(rows),
            "n_responses": [len(a), len(b)],
            "all_equal": bool(a) and bool(b) and bool(rows) and all(r["equal"] for r in rows),
            "zero_response_refusal": not a or not b}


def dw_ownership_gate(surv: dict) -> tuple[bool, str]:
    """Pure cleanup-ownership decision (no /proc, no signals): the restore may run ONLY when NO
    prototype survivor lives - no process whose exe is a launched mode's binary, no recorded family
    pid pinned by its start ticks, and no listener on the isolated port.  The /proc and `ss` scans
    live in model_window.proto_survivors; this function only decides from their result, so the
    decision is CPU-testable."""
    alive = bool(surv.get("exe_matches") or surv.get("recorded_family_alive")
                 or surv.get("port_8019_listener"))
    return (not alive, "restore allowed (no prototype survivor)" if not alive
            else "restore REFUSED: a prototype survivor lives - production is never loaded alongside it")


def dw_preflight(prod: dict) -> tuple[list[str], list[str]]:
    """Dry-run safe existence checks (files only; no HTTP, no process, no GPU)."""
    checks, problems = mw.preflight(prod)
    for mode, _w in DW_MODES:
        path = mw.CONFIG_DIR / f"proto-server-swift15-{mode}.json"
        ok = path.is_file()
        checks.append(f"divergence config {mode}: {'ok' if ok else 'MISSING'} {path}")
        if not ok:
            problems.append(f"divergence config missing: {path}")
    sys.path.insert(0, f"{mw.CLONE}/tools")
    try:
        import strata_bench                                        # noqa: F401 - the real chat client
        checks.append("bench client strata_bench: ok")
    except ImportError as e:
        checks.append(f"bench client strata_bench: MISSING ({e})")
        problems.append("bench client strata_bench missing")
    return checks, problems


# ------------------------------------------------------------------ the window
def arm_configured_on(reenable: str | None, rec: dict) -> dict:
    """What the SOURCE-VERIFIED configuration of the re-enabled group is, as recorded - kept apart
    from what the engine REPORTED DOING, so 'configured on' is never read as 'it swapped'."""
    flags = dict(mw.flags(json.loads(pathlib.Path(rec["config"]).read_text()).get("args", [])))
    return {"group": reenable,
            "adapt_swaps_argv": flags.get("--adapt-swaps"),
            "adapt_swaps_effective": (flags.get("--adapt-swaps")
                                      or DW_SOURCE_DEFAULTS["--adapt-swaps"][0]),
            "adapt_swaps_citation": DW_SOURCE_DEFAULTS["--adapt-swaps"][1],
            "child_env": child_env_extra(reenable)}


# ------------------------------------------------------------------ the ablation's runtime proof
def ablation_runtime_proof(mode: str, rec: dict, reenable: str, note) -> list[str]:
    """For a group deliberately turned ON, the POSITIVE runtime evidence is required from this
    session's FINAL log copy (the same provenanced copy the gate reads): the loan lines for
    borrowing, the adaptive-tier lines for adaptation, the conversation-cache/checkpoint lines for
    the cache.  Absence fails the arm closed - it is never read as 'the group is off, that is fine'.
    The group's own numbers are RECORDED (loan geometry, reuse counts, tier lines); the standard
    correctness gate stays UNESTABLISHED for the variant regardless of what is found."""
    marks = {"borrowing": DW_LOAN_MARKS, "adaptation": DW_ADAPT_ON_MARKS,
             "conversation-cache": DW_CACHE_ON_MARKS}[reenable]
    p = (rec.get("engine_log") or {}).get("per_window_copy")
    text = ""
    try:
        if p:
            text = pathlib.Path(p).read_text(errors="replace")
    except OSError:
        text = ""
    hits = [ln[:220] for ln in text.splitlines() if any(k in ln for k in marks)]
    rec["ablation_runtime"] = {"group": reenable, "marks_searched": list(marks),
                               "positive_lines": hits[:24], "positive_lines_total": len(hits)}
    if reenable == "conversation-cache":
        rows = rec.get("requests") or []
        rec["cache_reuse"] = {
            "reused_tokens_per_request": [r.get("reused_tokens") for r in rows],
            "read_tokens_per_request": [r.get("read_tokens") for r in rows],
            "reused_tokens_total": sum(r.get("reused_tokens") or 0 for r in rows),
            "fresh_requests": sum(1 for r in rows if not r.get("reused_tokens")),
            "cache_log_lines": hits[:24],
            "note": ("reuse counts and fresh/reset behavior are RECORDED from the response usage/timings "
                     "(strata_bench.digest: cached_tokens / cache_n -> reused_tokens, prompt_n -> "
                     "read_tokens) beside the engine's own cache lines; nothing is inferred")}
    if reenable == "adaptation":
        rec["adapt_membership_note"] = ("startup membership equality does NOT prove membership stays equal "
                                        "after requests - the adaptive tier swaps experts while decoding. "
                                        "The per-request decode-timing lines below are the ONLY "
                                        "membership-change evidence recorded by this window.")
        rec["post_request_tier_lines"] = hits[:24]
        # 'CONFIGURED ON' and 'OBSERVED ACTIVITY' are recorded SEPARATELY, and nothing is invented.
        # The serve tier lambda (10368-10411) prints no trace lines, so the activity record is the
        # engine's own per-request decode-timing line (15490-15493, gated by STRATA_DECODE_TIMING at
        # 14392).  The recorded number is the printed RATE (adapt_nswaps - dt_sw0)/windows (15499,
        # `ajob ? a_swapped : adapt_nswaps`; 10366/10412), and the counters reset per request
        # (14402-14406), so there is one line per request.  The engine prints NO swap total, so NO
        # total is computed from the rate here.  A rate of 0.0 on a request that ran decode windows
        # is a VALID observation (the decode path ran, no swap was observed), never a failure.
        tl = []
        for ln in text.splitlines():
            m = DW_DECODE_TIMING_RE.search(ln)
            if m is not None:
                tl.append({"windows": int(m.group(1)), "swaps_per_window": float(m.group(2)),
                           "line": ln[:220]})
        per_request = []
        for i, r in enumerate(rec.get("requests") or []):
            per_request.append({"request": r.get("n"), "class": r.get("class"),
                                "drafts_offered": r.get("drafts_offered"),
                                "drafts_accepted": r.get("drafts_accepted")})
        rec["adapt_activity"] = {
            "configured_on": arm_configured_on(reenable, rec),
            "child_env": child_env_extra(reenable),
            "serve_timing_lines": len(tl),
            "windows_per_timing_line": [t["windows"] for t in tl],
            "swaps_per_window_as_printed": [t["swaps_per_window"] for t in tl],
            "lines_with_observed_swaps": sum(1 for t in tl if t["swaps_per_window"] > 0.0),
            "lines_with_zero_observed_swaps": sum(1 for t in tl if t["swaps_per_window"] == 0.0),
            "timing_lines": tl[:24],
            "per_request": per_request,
            "activity_source": ("generate.cpp:15490-15493 '... adapt wait %.2f join %.2f, %.1f "
                                "swaps/window' - printed only under STRATA_DECODE_TIMING (14392, the "
                                "CHILD env of this variant); one line per request (14391, counters reset "
                                "per request at 14402-14406).  The value is the RATE "
                                "(adapt_nswaps - dt_sw0)/windows from 15499 (`ajob ? a_swapped : "
                                "adapt_nswaps`; adapt_nswaps declared 10366, incremented 10412).  The "
                                "engine prints no swap TOTAL, so no total is computed from the rate."),
            "cli_trace_note": ("the CLI trace variable STRATA_TRACE_ADAPT (16571/16607, lines "
                               "16609-16614/16690-16692) is INERT on this serve path and is NOT set by "
                               "this variant - the serve tier lambda 10368-10411 has no trace prints"),
            "zero_swaps_note": ("a tier that ran and swapped 0 slots in this short session is a VALID "
                                "observation: 'configured on' and 'observed swaps' are recorded "
                                "separately, the timing line proves the decode path ran, and no "
                                "activity is invented")}
        if not tl:
            return [f"ablation '{reenable}': the CHILD env requests STRATA_DECODE_TIMING=1 but this "
                    "session's final copy has NO 'strata decode timing: N windows ... swaps/window' line "
                    "(generate.cpp:15490-15493) - the serve activity evidence this variant depends on is "
                    "unproven, fail closed"]
    if not hits:
        return [f"ablation '{reenable}': NO positive runtime evidence for the re-enabled group in the "
                f"final current-session copy (searched {list(marks)}) - fail closed"]
    note(f"{mode}: ablation '{reenable}' runtime proof present ({len(hits)} line(s), first: {hits[0][:110]})")
    return []


def run_dw(snap: dict, record: dict, note, equalize_mib: int = 0,
           prompt_set: bool = False, max_tokens_override: int | None = None,
           reenable: str | None = None, requests: list[int] | None = None) -> None:
    prompts = select_prompts(prompt_set, requests)
    indices = prompt_indices(prompts, requests)
    # The DEFAULT stays exactly the L19 behavior: ONE prompt, 32 tokens.  The gate set uses the
    # gate's own class limits (128/96/64).  An explicit --max-tokens is BOUNDED: it can only lower
    # the limit that applies (max_tokens_for), never raise it.
    tokens = max_tokens_override if max_tokens_override is not None else (None if prompt_set
                                                                         else DW_MAX_TOKENS)
    # ONE plan builder (prompt_plan_rows) for the dry run AND the executed window, so the plan the
    # parent reads in the dry run IS the plan the window runs, and every row carries the ORIGINAL
    # full-set index beside the sequential index the comparisons use.
    record["prompt_plan"] = prompt_plan_rows(prompts, indices, tokens)
    record["requests_per_arm"] = len(prompts)
    record["prompt_set"] = prompt_set
    record["selected_requests"] = list(requests) if requests else None
    record["max_tokens_override"] = max_tokens_override
    record["reenable"] = reenable
    record["full_eight_prompt_gate_established"] = False
    record["subset_diagnostic_ok"] = False
    if requests:
        record["subset_diagnostic"] = {
            "scope": (f"SUBSET DIAGNOSTIC ONLY: the SELECTED original gate-set positions {indices} run "
                      "once on each arm's own fresh server, and every selected response is compared "
                      "byte-exactly with the corresponding response of the other arms. This is NOT a "
                      "full eight-prompt correctness pass and is NEVER labelled one."),
            "full_eight_prompt_gate_established": False,
            "selected_full_set_indices": list(indices),
            "sequential_to_full_set": {str(r["n"]): r["full_set_request"] for r in record["prompt_plan"]},
            "standard_gate": ("gates_ok stays False for an explicit --requests run BY DESIGN; the "
                              "separate subset_diagnostic_ok covers the strict selected-response "
                              "comparisons and the runtime gates ONLY"),
        }
    # Status fields exist from the first line of the window, with their fail-closed values. Every
    # path through run_dw ENDS by setting them (the completed-window block at the end), and bail()
    # sets them on every early exit - so a reader never has to guess what an absent key meant.
    record["residency_gate_established"] = False
    record["gates_ok"] = False
    record["ablation_ok"] = False
    if reenable:
        record["ablation"] = {
            "group": reenable,
            "scope": (f"ABLATION: ONLY the '{reenable}' control group is re-enabled, on all three arms, "
                      "derived from the CURRENT production clone for that group. Everything else stays "
                      f"controlled (pin {DW_PIN}, chunk {DW_CHUNK}, the target pack, the split, the child "
                      "env, the drafter widths, the max tokens, the room equalizer)"),
            "standard_correctness_gate": ("NOT ESTABLISHED - BY DESIGN: this variant deliberately breaks "
                                           "one of the controlled configuration's proofs, so the window "
                                           "NEVER reports the standard correctness gate as established"),
            "evidence_label": ("ABLATION EVIDENCE ONLY - a changed response identifies a CANDIDATE control "
                              "group; it proves NO specific kernel defect, NO speed, and NO safety of the "
                              "control"),
            "comparison": "EXPLORATORY text comparison under the ablation label (see exploratory_comparison)",
        }
    base_dir = pathlib.Path(record["dw_dir"])
    base_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(base_dir, 0o700)

    def bail(reason: str) -> None:
        """Every early break records the SAME fail-closed state a completed window records: the
        residency gate is explicitly NOT established, the byte comparisons are explicitly NOT RUN
        (the absence of a comparison is never a pass), and the reason is in the record and the log."""
        record["break_reason"] = reason
        record.setdefault("gate_failures", []).append(
            "residency gate NOT ESTABLISHED - the window broke before all three arms ran; the byte "
            "comparisons are NOT RUN and are never a pass")
        record["residency_gate_established"] = False
        record["comparison"] = f"NOT RUN - window broke: {reason}"
        # The status fields are stated EXPLICITLY on every early exit too: an absent key is not a
        # statement. A window that broke has NO correctness pass and NO clean ablation, in words.
        record["gates_ok"] = False
        record["ablation_ok"] = False
        record["full_eight_prompt_gate_established"] = False
        record["subset_diagnostic_ok"] = False
        note(reason)

    if not W.stop_prod(snap, record, note):        # the SAME verified stop path as every window
        bail(record.get("stop_fail", "production stop failed - no GPU work"))
        return
    env = os.environ.copy()
    env.update(snap["_env_full"])
    modes: dict = {}
    record["gate_failures"] = []
    for mode, width in DW_MODES:
        prod = json.loads(pathlib.Path(W.PROD_CONFIG).read_text())
        summary, problems = mw.currency(mode, width, prod)
        if problems:
            rec = {"mode": mode, "mode_ok": False, "mode_failures": problems, "requests": []}
            record["modes"].append(rec)
            bail(f"{mode}: config currency failed mid-window: {json.dumps(problems)[:400]}")
            return
        cfg = json.loads(pathlib.Path(summary["config"]).read_text())
        dpath, dcfg, ovr = derive(record["window"], mode, cfg, base_dir, equalize_mib, reenable)
        dproblems = validate_derivation(cfg, dcfg, ovr, record["window"], mode, reenable)
        extra_env = child_env_extra(reenable)
        rec = {"mode": mode, "width": width, "source_config": summary["config"],
               "derived_config": str(dpath), "overrides": ovr,
               "env": sorted(dict(DW_ENV, **extra_env)), "child_env_extra": extra_env,
               "config": str(dpath), "requests": [], "exe_want": mw.EXE_WANT.get(mode, mw.PROTO_BINARY)}
        record["modes"].append(rec)
        modes[mode] = rec
        if dproblems:
            bail(f"{mode}: derivation failed the allowlist: {dproblems}")
            return
        note(f"{mode}: derived {dpath.name} from {pathlib.Path(summary['config']).name}: "
             f"{json.dumps(ovr)} + env {sorted(dict(DW_ENV, **extra_env))}")
        mode_env = dict(env, **DW_ENV, **extra_env)
        try:
            if mw.start_mode(record["window"], mode, dcfg, width, mode_env, rec, note):
                argv = dict(mw.flags(dcfg.get("args", [])))
                bad: list[str] = []
                gate_ok = True
                # THE STARTUP EVIDENCE COMES FIRST (greedy_gate.run_gate's proven order): the
                # fingerprint reads the provenanced CURRENT-session copy that draft_evidence writes
                # (start_mode records only the pre-launch log_start offset+inode, never the copy),
                # so fingerprinting before it is guaranteed to fail.  A refused copy fails the mode
                # closed and NO prompt runs.
                # The ablation's child-environment addition is PROVEN from the child's own
                # /proc/<engine>/environ (W.env_captured keeps STRATA_* keys, window.py:62) - and the
                # parent's os.environ must NOT carry it, because window.py:294 restores production
                # from os.environ.copy(). Both sides are asserted; an unproven child env fails closed.
                if gate_ok and extra_env:
                    proof: dict = {"requested": extra_env, "child_environ": {},
                                   "parent_environ_has_it": {k: os.environ.get(k) is not None
                                                             for k in extra_env}}
                    eng = rec.get("engine")
                    if isinstance(eng, int):
                        proof["child_environ"] = {k: v for k, v in W.masked(W.env_captured(eng)).items()
                                                  if k in extra_env}
                    rec["ablation_env_proof"] = proof
                    missing = [k for k in extra_env if k not in proof["child_environ"]]
                    leaked = [k for k, v in proof["parent_environ_has_it"].items() if v]
                    if missing:
                        bad.append(f"ablation '{reenable}': the CHILD env {missing} is not in the "
                                   f"engine's own /proc environ (proof={json.dumps(proof)[:220]}) - "
                                   "the runtime proof this variant depends on is unproven, fail closed")
                        gate_ok = False
                    if leaked:
                        bad.append(f"ablation '{reenable}': {leaked} is set in the PARENT os.environ - "
                                   "the restore path inherits the parent environment (window.py:294), "
                                   "so the trace variable would follow into the RESTORED production "
                                   "process. Unset it in the parent shell before running this variant")
                        gate_ok = False
                if not mw.draft_evidence(record["window"], dcfg, mode, rec):
                    bad.append(f"startup residency evidence refused ({json.dumps(rec.get('engine_log'))[:220]}) "
                               "- fail closed, NO prompt runs")
                    gate_ok = False
                else:
                    rec["startup_evidence"] = (rec.get("engine_log") or {}).get("per_window_copy")
                fp = dw_fingerprint(rec, argv.get("--prefill"), argv, reenable) if gate_ok else None
                rec["residency"] = fp
                if gate_ok and fp is None:
                    bad.append("residency fingerprint incomplete (startup cache/stage lines missing) "
                               "- fail closed, NO prompt runs")
                    gate_ok = False
                if gate_ok:
                    key = dw_key(fp, reenable)
                    base = record.get("residency_baseline")
                    if base is None:
                        record["residency_baseline"] = {"mode": mode, "key": list(key), "fingerprint": fp}
                        note(f"{mode}: residency baseline set (cuda0 {fp['cuda0']['slots']} slots "
                             f"checksum_proof={fp['cuda0']['checksum_proof']}, stages {fp['stages']}, "
                             f"chunk_proof={fp['chunk_proof']}, no_borrow_proof={fp['no_borrow_proof']}, "
                             f"adapt_proof={fp['adapt_proof']}) - GiB/rooms recorded as diagnostics")
                    elif list(key) != base["key"]:
                        bfp = base["fingerprint"]
                        gap = ""
                        for dev, st in fp["stages"].items():
                            b = bfp["stages"].get(dev) or bfp["stages"].get(str(dev))
                            if b and st["slots"] != b["slots"]:
                                room_a = fp["room_gib"].get(dev, fp["room_gib"].get(str(dev)))
                                room_b = bfp["room_gib"].get(dev, bfp["room_gib"].get(str(dev)))
                                if room_a is not None and room_b is not None:
                                    gap += (f" CUDA{dev} room {room_b}->{room_a} GiB "
                                            f"delta_mib {round((room_b - room_a) * 1024)};")
                        bad.append(f"residency gate NOT established: {mode} "
                                   f"(cuda0 {fp['cuda0']['slots']}/{fp['cuda0']['gib']} GiB "
                                   f"checksum_proof={fp['cuda0']['checksum_proof']}, stages={fp['stages']}, "
                                   f"chunk_proof={fp['chunk_proof']}, no_borrow={fp['no_borrow_proof']}, "
                                   f"adapt={fp['adapt_proof']}, rooms={fp['room_gib']}) != baseline "
                                   f"{base['mode']} (cuda0 {bfp['cuda0']['slots']}/{bfp['cuda0']['gib']} GiB "
                                   f"checksum_proof={bfp['cuda0']['checksum_proof']}, stages={bfp['stages']}, "
                                   f"chunk_proof={bfp['chunk_proof']}, no_borrow={bfp['no_borrow_proof']}, "
                                   f"adapt={bfp['adapt_proof']}, rooms={bfp['room_gib']}) - NO prompt runs, "
                                   f"restore now. ACTIONABLE room gap:{gap} (measured room delta x1024 = the "
                                   "--equalize-mib value for the rerun; it raises --vram-reserve-later-mib on "
                                   "the non-dflash arms only, generate.cpp:4499-4501)")
                        gate_ok = False
                    if not fp["checksum_present"]:
                        bad.append("membership UNPROVEN: no CUDA0 read-back checksum in this session's log "
                                   "(STRATA_VERIFY_COMPLEMENT not in force) - an absent checksum is never "
                                   "an equality success")
                        gate_ok = False
                    if fp["chunk_proof"][0] == "chunk-UNVERIFIED":
                        bad.append(f"final prompt chunk UNVERIFIED: argv --prefill={fp['argv_prefill']!r} is "
                                   f"not {DW_CHUNK} and the log carries no usable chunk line "
                                   f"(proof={fp['chunk_proof']}) - fail closed")
                        gate_ok = False
                    # The proofs of the groups that stay OFF are asserted POSITIVELY.  The group this
                    # variant deliberately turns ON is asserted by ITS OWN positive proof (never by the
                    # absence of the OFF proof, and never by the absence of a log line).
                    if reenable != "borrowing":
                        if fp["no_borrow_proof"][0] != "no-loan-PROVEN-positive-line":
                            bad.append("NO-LOAN proof missing: the positive serve line "
                                       "\"the prompt path allocates its own buffers\" (generate.cpp:8760) is "
                                       f"absent or a loan line is present (borrows={fp['borrows']}, "
                                       f"head_loan={fp['head_loan_line']!r}, proof={fp['no_borrow_proof']}) - "
                                       "the absence of a loan line alone is NEVER the proof (the loan lines come "
                                       "from the borrowing path only, 8477)")
                            gate_ok = False
                    else:
                        if fp["loan_proof"][0] != "loan-PROVEN-positive-lines":
                            bad.append("ablation 'borrowing': borrowing is claimed ON but UNPROVEN - no "
                                       f"positive loan line in this session's copy (head_loan={fp['head_loan_line']!r}, "
                                       f"borrows={fp['borrows']}, proof={fp['loan_proof']}) - the loan lines are "
                                       "the source-backed proof (generate.cpp:8732-8739) - fail closed")
                            gate_ok = False
                        rec["loan_geometry"] = {"head": fp["head_loan_line"], "stages": fp["borrows"],
                                                "lines": fp["loan_lines"]}
                    if reenable != "adaptation":
                        if fp["adapt_proof"][0] != "adapt-off-PROVEN-argv-adapt-swaps-0":
                            bad.append(f"ADAPT-OFF proof missing: the verified engine cmdline does not carry "
                                       f"--adapt-swaps 0 (proof={fp['adapt_proof']})")
                            gate_ok = False
                    elif not fp["adapt_proof"][0].startswith("adapt-active-PROVEN"):
                        bad.append("ablation 'adaptation': adaptation is claimed ON but its POSITIVE proof is "
                                   f"missing (proof={fp['adapt_proof']}) - the effective --adapt-swaps must be "
                                   "an explicit value > 0 (the clone's value, or the source default 96 at "
                                   "generate.cpp:725). An adapt-OFF proof under an adaptation-ON variant is a "
                                   "CONTRADICTION and fails closed - fail closed")
                        gate_ok = False
                    if reenable != "conversation-cache":
                        if fp["cache_proof"][0] != "cache-off-PROVEN-argv-both-0":
                            bad.append("CACHE-OFF proof missing: the verified engine cmdline does not carry "
                                       f"both --prompt-cache 0 and --conversation-cache-mib 0 "
                                       f"(proof={fp['cache_proof']}) - fail closed")
                            gate_ok = False
                    elif not fp["cache_proof"][0].startswith("cache-active-PROVEN"):
                        bad.append("ablation 'conversation-cache': the cache group is claimed ON but its "
                                   f"POSITIVE proof is missing (proof={fp['cache_proof']}) - both flags must "
                                   "be an explicit positive pair (the clone's values, or the cited source "
                                   "default 6 at 729; a 0 budget is OFF at 730). A cache-OFF proof under a "
                                   "cache-ON variant is a CONTRADICTION and fails closed - fail closed")
                        gate_ok = False
                    rec["residency_checksum"] = "present" if fp["checksum_present"] else "ABSENT - fail closed"
                if gate_ok:
                    requests_ok = run_dw_request(mode, rec, note, prompts, tokens, indices)
                    ev_ok = mw.draft_evidence(record["window"], dcfg, mode, rec)
                    if not ev_ok:
                        bad.append("engine log evidence unreadable (provenance failed)")
                    else:
                        bad += activation_gate(mode, rec)
                        if reenable:
                            bad += ablation_runtime_proof(mode, rec, reenable, note)
                    if not requests_ok:
                        bad.append("the request failed (see the recorded error) - text equality gates the "
                                   "byte-compare")
                rec["mode_ok"], rec["mode_failures"] = not bad, bad
            else:
                rec["mode_ok"], rec["mode_failures"] = False, [rec.get("startup", "startup failed")]
        except BaseException as e:                          # WindowSignal lands HERE
            rec["mode_error"], rec["mode_ok"] = repr(e)[:300], False
            rec["mode_failures"] = [f"mode interrupted: {rec['mode_error']} (no further modes run)"]
        note(f"{mode}: mode_ok={rec['mode_ok']} failures={rec.get('mode_failures')}")
        gone = mw.stop_mode(mode, rec, note)                # OUR proxy only, verified gone
        rec["family_gone"] = gone
        if not rec["mode_ok"] or not gone:
            bail(f"{mode}: mode_ok={rec['mode_ok']} family_gone={gone} - restoring now "
                 f"(failures={json.dumps(rec.get('mode_failures'))[:300]})")
            return
    controlled_match = ("residency_baseline" in record and
                        len(record["modes"]) == len(DW_MODES) and all(m.get("mode_ok") for m in record["modes"]))
    record["controlled_components_match"] = controlled_match
    # THE DEFAULT GATE IS NOT LOOSENED: an ablation variant can NEVER report the standard correctness
    # gate as established, whatever its text comparison shows.
    established = controlled_match and reenable is None
    record["residency_gate_established"] = established
    texts = {m: [r.get("text") for r in (modes.get(m) or {}).get("requests", [])] for m in modes}
    if established:
        comparison, failures = {}, []
        for label, a, b, closing in DW_PAIRS:
            c = compare_pair(label, texts.get(a, []), texts.get(b, []))
            c["pair"] = [a, b]
            c["fail_closing"] = closing
            c["expected_responses"] = len(prompts)
            c["selected_full_set_indices"] = indices
            comparison[label] = c
            if c["zero_response_refusal"]:
                failures.append(f"GATE {label}: an arm produced NO responses "
                                f"({c['n_responses']}) - zero responses is never all_equal")
            elif c["n_responses"] != [len(prompts), len(prompts)]:
                failures.append(f"GATE {label}: response count {c['n_responses']} != one per prompt "
                                f"({len(prompts)}) - a missing response fails the gate")
            if closing and not c["all_equal"]:
                failures += [f"GATE {label}: request {r['request']} differs at index "
                             f"{r['first_diff_index']} (response_present={r['response_present']})"
                             for r in c["requests"] if not r["equal"]]
        record["comparison"] = comparison
        record["gate_failures"] += failures
    elif reenable and controlled_match:
        # EXPLORATORY comparison, under the ablation label, with the standard gate UNESTABLISHED.
        comparison = {}
        for label, a, b, closing in DW_PAIRS:
            c = compare_pair(label, texts.get(a, []), texts.get(b, []))
            c["pair"] = [a, b]
            c["expected_responses"] = len(prompts)
            c["ablation_group"] = reenable
            c["evidence_label"] = ("EXPLORATORY - ABLATION EVIDENCE ONLY, not a correctness pass: a "
                                   "difference names a CANDIDATE control group, it proves no kernel defect")
            comparison[label] = c
        record["exploratory_comparison"] = comparison
        record["comparison"] = (f"NOT RUN as a correctness gate - this variant deliberately ablates "
                                f"'{reenable}', so the standard gate is UNESTABLISHED; the byte "
                                f"comparisons of the responses are recorded as EXPLORATORY under "
                                f"exploratory_comparison")
        record["gate_failures"].append(f"standard correctness gate UNESTABLISHED by design (ablation "
                                       f"'{reenable}') - the exploratory comparison is evidence, never a pass")
        if reenable == "borrowing":
            arms = [m for m, _ in DW_MODES]
            geo = {m: (modes.get(m) or {}).get("loan_geometry") for m in arms}
            sigs = {json.dumps(geo.get(m), sort_keys=True) for m in arms}
            record["ablation"]["loan_geometry_by_arm"] = geo
            record["ablation"]["loan_geometry_equal_across_arms"] = len(sigs) == 1
            if len(sigs) != 1:
                record["ablation"]["loan_mismatch_is_the_variable"] = (
                    "AR and the drafter arms show DIFFERENT loan geometry. That difference IS the variable "
                    "being studied - it is reported as the variable, NEVER as an equal numeric "
                    "configuration and never as a pass")
                record["gate_failures"].append("ablation 'borrowing': the loan geometry differs across arms "
                                               "- recorded as THE VARIABLE UNDER STUDY, not as equality")
                note("ablation 'borrowing': loan geometry MISMATCH across arms = the variable under study: "
                     + json.dumps(geo)[:600])
            else:
                note("ablation 'borrowing': loan geometry equal across arms (recorded as a fact of this run)")
    elif reenable:
        record["comparison"] = ("NOT RUN - the ablation variant's CONTROLLED components differ "
                                "(cache slots / checksum / stages / chunk): the ablation is not interpretable")
        record["gate_failures"].append("ablation variant: the controlled components do not match across arms "
                                       "- stop, read the per-mode fingerprints and the room gap")
    else:
        record["comparison"] = "NOT RUN - residency gate not established (see the per-mode residency records)"
        record["gate_failures"].append("residency gate NOT ESTABLISHED - the byte comparisons are UNVALIDATED; "
                                       "read the per-mode fingerprints and the room gap, escalate to design")
    record["comparison_scope"] = ("Output texts ONLY: every row compares corresponding responses byte-exactly, "
                                  "with the engine-observed prompt tokens, usage, timings, full text and errors "
                                  "recorded beside it. This is comparison EVIDENCE; it attributes NO cause - no "
                                  "control, head, tap or prompt-state claim is made from text"
                                  + ("" if not reenable else
                                     f"; ABLATION '{reenable}': the comparison is EXPLORATORY and the standard "
                                     "correctness gate is UNESTABLISHED for this variant"
                                     + ("" if not requests else
                                        f"; SUBSET DIAGNOSTIC: ONLY the selected gate-set requests {indices} "
                                        "were run and compared - this is NOT the full eight-prompt gate")))
    record["gate_mode"] = ((f"divergence-window pin{DW_PIN} chunk{DW_CHUNK} "
                            + ("no-borrow " if reenable != "borrowing" else "BORROWING-ON(ablation) ")
                            + ("adapt-off " if reenable != "adaptation" else "ADAPTATION-ON(ablation) ")
                            + ("park-off " if reenable != "conversation-cache" else "CACHE-ON(ablation) ")
                            + f"minp0 ({subset_label(requests, prompt_set)}, "
                            f"max tokens {tokens_description(tokens, max_tokens_override)}, "
                            f"equalize +{equalize_mib} MiB on non-dflash arms)")
                           if equalize_mib else
                           (f"divergence-window pin{DW_PIN} chunk{DW_CHUNK} "
                            + ("no-borrow " if reenable != "borrowing" else "BORROWING-ON(ablation) ")
                            + ("adapt-off " if reenable != "adaptation" else "ADAPTATION-ON(ablation) ")
                            + ("park-off " if reenable != "conversation-cache" else "CACHE-ON(ablation) ")
                            + f"minp0 ({subset_label(requests, prompt_set)}, "
                            f"max tokens {tokens_description(tokens, max_tokens_override)}, "
                            "measure-run: no room equalizer)"))
    record["diagnostic_only"] = ("Explicit correctness-diagnostic variant: the timings here are NOT production "
                                 "performance numbers; comparisons are TEXT-ONLY (serve has no target-state "
                                 "probe, generate.cpp:16717-16721/16886 are CLI-only), so no cause is attributed "
                                 "to the head, the taps or the prompt state from text"
                                 + ("" if not reenable else
                                    f"; ABLATION '{reenable}': outputs are ablation evidence, NOT a new "
                                    "correctness pass, NOT a speed claim, NOT a safety verdict"))
    for label, _a, _b, closing in DW_PAIRS:
        v = (record["comparison"] if isinstance(record.get("comparison"), dict)
             else record.get("exploratory_comparison") or {})
        v = v.get(label)
        note(f"GATE {label}: all_equal={v['all_equal'] if v else 'NOT RUN'} fail_closing={closing}"
             + (" [EXPLORATORY ABLATION]" if reenable else "")
             + (" [SUBSET DIAGNOSTIC]" if requests else ""))
    record.update(dw_gate_flags(controlled_match, record["gate_failures"], reenable, prompt_set,
                                requests, record.get("break_reason")))


def dw_gate_flags(controlled_match: bool, gate_failures: list, reenable: str | None,
                  prompt_set: bool, requests: list[int] | None,
                  break_reason: str | None = None) -> dict:
    """PURE fail-closed decision of the window's status flags (no /proc, no HTTP, no restore), so the
    window record is filled from ONE CPU-testable decision and a reader never guesses an absent key.

      * an explicit --requests run is a SUBSET DIAGNOSTIC: the standard correctness gate is NEVER
        reported as established by it (gates_ok stays False) and the FULL eight-prompt gate is NEVER
        established by it; the separate subset_diagnostic_ok covers the strict selected-response
        comparisons plus the runtime gates ONLY;
      * without --requests the semantics are exactly the existing ones - the default single prompt and
        the full eight-prompt gate keep their own gates_ok, unchanged;
      * a break_reason (any early exit) or any gate failure clears everything: an unrun comparison is
        never a pass."""
    established = bool(controlled_match and reenable is None)
    standard_ok = bool(established and list(gate_failures) == [] and not break_reason)
    subset = bool(requests)
    return {"residency_gate_established": established,
            "gates_ok": bool(standard_ok and not subset),
            "ablation_ok": bool(reenable and controlled_match and not break_reason),
            "subset_diagnostic_ok": bool(subset and standard_ok),
            "full_eight_prompt_gate_established": bool(standard_ok and prompt_set and not subset)}


def dw_outcome(record: dict, restored: bool) -> tuple[str, int]:
    """PURE status words + exit code of a completed window (no restore, no /proc, no signals), so BOTH
    are CPU-testable and fail-closed: an absent key is never a pass, and a window whose production
    restore did not verify NEVER exits 0.  An explicit --requests run is labelled SUBSET DIAGNOSTIC
    PASS/FAIL - never 'ALL OK' - because only the SELECTED gate-set requests were compared."""
    if not restored:
        return "RESTORE FAILED - PARENT INVESTIGATE NOW", 1
    if record.get("selected_requests"):
        ok = bool(record.get("subset_diagnostic_ok"))
        return ("SUBSET DIAGNOSTIC PASS - selected gate-set requests ONLY, the full eight-prompt gate "
                "is NOT established by a subset run - see logs" if ok
                else "SUBSET DIAGNOSTIC FAIL - see logs", 0 if ok else 1)
    if record.get("gates_ok"):
        return "ALL OK", 0
    if record.get("ablation_ok"):
        return ("RESTORED; ABLATION EVIDENCE ONLY (the standard correctness gate is UNESTABLISHED by "
                "design for this variant) - see logs", 0)
    return "RESTORED; gate failures - see logs", 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true",
                    help="PARENT-only: the real divergence window (default DRY RUN, zero signals)")
    ap.add_argument("--equalize-mib", type=int, metavar="DELTA", default=0,
                    help="raise --vram-reserve-later-mib by DELTA on the NON-dflash arms only (the "
                         "source-proven CUDA1 room equalizer, generate.cpp:4499-4501; at chunk 512 with "
                         "--no-prefill-borrow the expected first value is kDrafterMib=1000, 4441; take the "
                         "delta the failed run's room lines recorded). The runtime gate still asserts exact "
                         "equality, so a false pass is impossible")
    ap.add_argument("--prompt-set", action="store_true",
                    help="run the FULL eight-prompt gate set (greedy_gate.GATE_PROMPTS, in order) instead "
                         "of the single default prompt: one fresh request per prompt, sequentially, on each "
                         "arm's own server, with the SAME three arms and the SAME runtime controls "
                         "(pin 2000, chunk 512, no borrowing, adaptation off, park/cache off, min-p 0). "
                         "Every corresponding response is compared byte-exactly; a missing or failed "
                         "request fails the gate, and zero responses is never all_equal")
    ap.add_argument("--requests", type=int, nargs="+", metavar="N", default=None,
                    help="SUBSET DIAGNOSTIC, OPT-IN: valid ONLY with --prompt-set and NEVER with "
                         f"--reenable. Run ONLY these original ONE-BASED gate-set positions "
                         f"(1..{len(G.GATE_PROMPTS)}) of greedy_gate.GATE_PROMPTS, in the ORDER GIVEN, "
                         "one fresh request per prompt on each arm's own server, with the SAME three "
                         "arms and the SAME runtime controls (pin 2000, chunk 512, no borrowing, "
                         "adaptation off, park/cache off, min-p 0). An empty selection, a duplicate, 0, "
                         "an out-of-range index, --requests without --prompt-set and --requests with "
                         "--reenable are REFUSED BEFORE any snapshot/signal/config/port/HTTP action. "
                         "This is a SUBSET DIAGNOSTIC ONLY: the full eight-prompt gate is NEVER "
                         "established by it, the standard gates_ok stays False, and the separate "
                         "subset_diagnostic_ok covers the SELECTED responses plus the runtime gates. "
                         "Every plan row and every response records the original full-set index")
    ap.add_argument("--max-tokens", type=int, metavar="N", default=None,
                    help="bounded override of the per-request new-token cap: it can only LOWER the limit "
                         "that applies (the class limits 128/96/64 for --prompt-set, 32 for the default "
                         "single prompt); 1..128, refused outside that range. The requested value is "
                         "recorded beside the value used")
    ap.add_argument("--reenable", choices=DW_REENABLE, default=None, metavar="GROUP",
                    help="ABLATION of ONE control group, on all three arms, derived from the CURRENT "
                         "production clone for that group: 'adaptation' (the diagnostic --adapt-swaps 0 is "
                         "dropped; the clone's value, or the source default 96 at generate.cpp:725, "
                         "recorded with its citation, PLUS the CHILD-env STRATA_DECODE_TIMING=1 that makes "
                         "the serve path print its per-request 'swaps/window' activity line - "
                         "generate.cpp:14392 gating 15490-15493; the CLI-only STRATA_TRACE_ADAPT is inert "
                         "on serve and is NOT requested), 'borrowing' (only --no-prefill-borrow is dropped, PLUS the CHILD-env force-borrow control STRATA_SPLIT_OWN_BUFFERS=0: at chunk 512 the split's own-buffers rule takes ownership and plans NO loan without it - generate.cpp:8672-8686, the same test at 8156-8162 and 8668-8671), "
                         "'conversation-cache' (--prompt-cache and --conversation-cache-mib restored from "
                         "the clone; an absent --prompt-cache uses the source default 6 at 729, cited - "
                         "never guessed). Everything else stays controlled (pin 2000, chunk 512, the pack, "
                         "the split, the env, the widths, the max tokens, the equalizer). The initial "
                         "cache membership/count fingerprints must STILL match before any prompt; the "
                         "re-enabled group must show its OWN positive runtime proof or the arm fails "
                         "closed. The standard correctness gate is NEVER reported as established for an "
                         "ablation variant - its outputs are ABLATION EVIDENCE ONLY")
    a = ap.parse_args()
    if a.max_tokens is not None and not 1 <= a.max_tokens <= 128:
        print(f"REFUSE: --max-tokens {a.max_tokens} is outside the bounded range 1..128")
        return 2
    # THE SELECTION IS VALIDATED HERE, at the same point as the other refusals: before the window
    # label, before any directory, before W.snapshot(), before any signal handler, config read, port
    # probe, derived file, GPU action or HTTP request.  An invalid --requests never reaches an action.
    requests, refuse = validate_requests(a.requests, a.prompt_set, a.reenable)
    if refuse:
        print(refuse)
        return 2

    window = f"divergence-{time.strftime('%Y%m%d-%H%M%S')}"
    LOGS.mkdir(parents=True, exist_ok=True)
    OPS.mkdir(parents=True, exist_ok=True)
    results, wlog = OPS / f"{window}-results.json", LOGS / f"{window}.log"
    note = lambda msg: W.note_to(wlog, msg)                                          # noqa: E731
    base_dir = dw_dir_for(window)

    # ONE selection and ONE plan builder for the dry-run plan AND the executed window, so the plan the
    # parent reads is the plan the window runs, and every occurrence uses the SAME selected list.
    plan_prompts = select_prompts(a.prompt_set, requests)
    plan_indices = prompt_indices(plan_prompts, requests)
    plan_tokens = (a.max_tokens if a.max_tokens is not None
                   else (None if a.prompt_set else DW_MAX_TOKENS))

    snap = W.snapshot()
    prod = json.loads(pathlib.Path(W.PROD_CONFIG).read_text())
    summaries, problems = [], []
    for mode, width in DW_MODES:
        s, p = mw.currency(mode, width, prod)
        summaries.append(s)
        problems += p
    checks, pre = dw_preflight(prod)
    problems += pre
    print(json.dumps({
        "window": window, "production_snapshot": W.display(snap),
        "prod_config_sha256": W.config_sha(), "configs": summaries,
        "config_currency_problems": problems, "preflight": checks,
        "port_8019_listener_now": W.listener_pid(mw.PROTO_PORT),
        "modes_in_order": [m for m, _ in DW_MODES],
        "derived_dir_would_be": str(base_dir), "derived_files_mode": "0600 (written at execute only)",
        "selected_requests": requests,
        "subset_diagnostic": (None if not requests else {
            "scope": (f"SUBSET DIAGNOSTIC ONLY: the selected original gate-set positions {requests} run "
                      "once on each arm's own fresh server and every selected response is compared "
                      "byte-exactly with the corresponding response of the other arms. NOT a full "
                      "eight-prompt correctness pass and never labelled one; gates_ok stays False and "
                      "subset_diagnostic_ok covers the selected responses ONLY"),
            "full_eight_prompt_gate_established": False,
            "selected_full_set_indices": list(requests)}),
        "prompt_plan": (
            prompt_plan_rows(plan_prompts, plan_indices, plan_tokens)
            + [{"note": f"{subset_label(requests, a.prompt_set)}; greedy; "
                        f"reasoning off; one fresh request per prompt per arm; max tokens "
                        f"{tokens_description(plan_tokens, a.max_tokens)}"}]),
        "child_env": sorted(DW_ENV),
        "ablation": (None if not a.reenable else {
            "group": a.reenable,
            "child_env_extra": child_env_extra(a.reenable),
            "child_env_never_in_parent": "the extra variables are set per CHILD process only "
                                         "(window.py:294 restores production from os.environ.copy()); "
                                         "the gate reads the child's own /proc environ and fails closed "
                                         "if the variable is not proven there",
            "only_this_group_changes": (f"the '{a.reenable}' control group, on all three arms, from the "
                                        "CURRENT production clone for that group (an absent flag uses the "
                                        "source-verified default WITH its citation, never a guess)"),
            "stays_controlled": {"pin": DW_PIN, "prefill_chunk": DW_CHUNK,
                                 "adapt_swaps": ("clone value / source default 96, cited"
                                                 if a.reenable == "adaptation" else "0"),
                                 "no_prefill_borrow": ("dropped (borrowing ON)" if a.reenable == "borrowing"
                                                       else "present (loan OFF)"),
                                 "prompt_cache": ("clone value / source default 6, cited"
                                                  if a.reenable == "conversation-cache" else "0"),
                                 "conversation_cache_mib": ("clone value, cited"
                                                            if a.reenable == "conversation-cache" else "0"),
                                 "spec_min_p": "0 on the drafter arms",
                                 "lookup_chain": "0 on the drafter arms"},
            "source_defaults": {k: list(v) for k, v in DW_SOURCE_DEFAULTS.items()},
            "initial_residency_gate": ("UNCHANGED and strict: CUDA0 slots/prefilled + CHECKSUM proof, every "
                                       "stage, the FINAL-CHUNK proof, and the POSITIVE proof of every group that "
                                       "stays OFF must match across arms BEFORE any prompt; the re-enabled group "
                                       "must show its OWN positive proof (loan lines / adapt argv + runtime / "
                                       "cache argv + runtime) or the arm fails closed"),
            "standard_correctness_gate": "NEVER reported as established for an ablation variant",
            "output_label": ("ABLATION EVIDENCE ONLY - exploratory text comparison, not a correctness pass, "
                             "not speed, not a safety verdict; the room delta may be retuned from the run's own "
                             "room lines (--equalize-mib)")}),
        "overrides_allowlist": sorted(DW_ALLOW),
        "overrides": {
            "all arms": {"--expert-cache": str(DW_PIN), "--prefill": str(DW_CHUNK),
                         "--no-prefill-borrow": "(flag, valueless)", "--adapt-swaps": "0",
                         "--prompt-cache": "0", "--conversation-cache-mib": "0"},
            "dflash arms": {"--spec-min-p": "0 (full proposal width, 14549/14777)",
                            "--lookup-chain": "0 (sfx_on false, 14338)"},
            "non-dflash arms with --equalize-mib": {"--vram-reserve-later-mib": "+DELTA (4499-4501)"}},
        "runtime_gates": ["residency key equality (CUDA0 slots/prefilled + CHECKSUM proof, every stage's "
                          "layers/slots/filled/pairs, FINAL-CHUNK proof, POSITIVE no-loan proof, ADAPT-OFF "
                          "argv proof) asserted BEFORE any prompt",
                          "no-loan proof = the positive serve line 8760 AND no loan line (absence alone is "
                          "never the proof: the loan lines come from the borrowing path only, 8477)",
                          "drafter arms must offer drafts (>0) with no fallback line; the AR arm must offer "
                          "none",
                          "engine-log evidence = the current session's bytes only (pre-launch offset + inode)"],
        "chunk_note": f"--prefill {DW_CHUNK} is a CORRECTNESS control (the engine's own room remedy, "
                      "generate.cpp:6646-6647); at 512 the split's own-buffer reserve is 160 + "
                      "512*680/1024 = 500 MiB with NO ring term, because the ring reserve needs chunk >= "
                      "stream_all_min() = 1024 (generate.cpp:4425-4430, prefill.cpp:182-188, ring STAGE=8 "
                      "below it, 138)",
        "diagnostic_only": "timings are NOT production benchmarks; comparisons are TEXT-ONLY, no cause is "
                           "attributed to the head/taps/prompt state from text",
        "startup_deadline_s": mw.STARTUP_S, "cold_load_max_s": mw.COLD_S,
        "request_timeout_s": mw.REQ_TIMEOUT}, indent=2))
    if not snap.get("verified"):
        print("REFUSE: " + snap.get("why", "snapshot failed"))
        return 2
    if problems:
        print("REFUSE: config currency / preflight problems above - regenerate the clones with the plan tool "
              "(parent), never hand-edit them")
        return 2
    if not a.execute:
        print("DRY RUN only - zero signals, zero GPU, zero HTTP inference, no derived files written. "
              "Parent: rerun with --execute (and --equalize-mib <the measured room gap x1024>).")
        return 0

    record: dict = {"window": window, "started": time.strftime("%FT%T%z"), "snapshot": W.display(snap),
                    "prod_config_sha256": W.config_sha(), "configs": summaries, "dw_dir": str(base_dir),
                    "prompt_sha256": hashlib.sha256(DW_PROMPT.encode()).hexdigest(),
                    "prompt_set": a.prompt_set, "max_tokens_override": a.max_tokens,
                    "selected_requests": requests,
                    "reenable": a.reenable,
                    "child_env": sorted(DW_ENV), "equalize_mib": a.equalize_mib,
                    "stop_sent": False, "modes": []}
    exit_code = 1                                          # fail-closed: an unfinished window is never 0
    mw.install_signal_handlers()                          # SIGTERM/SIGINT -> WindowSignal -> cleanup path
    try:
        run_dw(snap, record, note, a.equalize_mib, a.prompt_set, a.max_tokens, a.reenable, requests)
    except BaseException as e:
        record["window_error"] = repr(e)[:300]
    finally:
        surv = mw.proto_survivors(record)                 # never restore with a prototype survivor alive
        allowed, why = dw_ownership_gate(surv)
        if not allowed:
            record["restore"] = {"restored": False,
                                 "refused": f"prototype family survivors {json.dumps(surv)} - {why}; "
                                            "parent: confirm the recorded pids, clean them by their "
                                            "verified pids, re-check, then restore"}
        else:
            record["restore"] = W.restore_and_verify(snap, record, time.time() + 360)
        restored = bool(record["restore"].get("restored"))
        record["status"], exit_code = dw_outcome(record, restored)
        W.note_to(wlog, f"WINDOW_FINAL {record['status']} restore={json.dumps(record['restore'])}")
        results.write_text(json.dumps(record, indent=2) + "\n")
        os.chmod(results, 0o600)
        print(json.dumps(record, indent=2))
    if record.get("ablation_ok") and record["restore"].get("restored"):
        print(f"ABLATION EVIDENCE ONLY (group={record.get('reenable')}): the window ran clean, the "
              "controlled components matched, the re-enabled group showed its positive proof, and "
              "production was restored. THIS IS NOT a correctness pass, NOT a speed claim and NOT a "
              "safety verdict - the standard correctness gate is UNESTABLISHED for this variant by design.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
