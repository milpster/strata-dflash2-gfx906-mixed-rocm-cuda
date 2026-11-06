#!/usr/bin/env python3
"""GREEDY-GATE controller - PARENT-only (agents never run --execute).

BYTE-EXACT greedy equality across four modes, ONE server start each, isolated port 8019:

  1. old_target_only   OLD prod binary, pure AR  (config proto-server-swift15-old_target_only.json)
  2. target_only       NEW binary,   pure AR      (proto-server-swift15-target_only.json)
  3. suffix_lookup     NEW binary,   prod args verbatim (proto-server-swift15-suffix_lookup.json)
  4. dflash_w3         NEW binary + Inco drafter  (proto-server-swift15-dflash_w3.json, width 3)

Gates (per prompt, byte-exact string compare; texts recorded VERBATIM):
  FAIL-CLOSING  old_target_only == target_only   (old-vs-new pure-AR engine gate)
  FAIL-CLOSING  target_only      == dflash_w3    (speculative-decoding equality gate)
  INFORMATIONAL target_only      == suffix_lookup

MEASURED FACTS (windows 175220 + 180344, this file's history - see L17-RESIDENCY-GATE-REPORT.md):
old-vs-new pure-AR passed 8/8 BYTE-EXACT in both windows.  AR-vs-dflash_w3 FAILED in both
(diffs at prompts 1,5,6,7 production-shape; 1,3,4,5 with the pin).  The L16 --correctness-pin 2000
control is MEASURED INVALID: every arm pinned --expert-cache 2000 equalized ONLY the CUDA0 cache
(2904 slots / 4.34 GiB in all arms) while the CUDA1 STAGE cache stayed room-sized - 5340 slots
(10.00 GiB) on the AR/suffix arms vs 4602 (8.61 GiB) with the drafter - and the drafter arm also
trimmed the prompt chunk 8192 -> 4096.  Same HOST cache value did NOT equalize per-stage
residency; the byte-equality claim on AR-vs-dflash is UNVALIDATED, not proven, not dismissed.

SOURCE FINDINGS (this lane, c938100b + the old tree read-only at 497e4be4):
  - CUDA0's cache is byte-budget-pinned by --expert-cache N (budget = N x round-up(max_blob),
    capped by free room, filled as the profile's ranked PREFIX: generate.cpp:6244-6263; measured
    equal 2904 across all arms).
  - A split's LATER-stage cache is room-sized only (generate.cpp:6521-6545: room = live free -
    reserve - window allowances, stage_room 4492-4505, kDrafterMib 4441; the drafter has already
    eaten the free figure, 6524-6525).  NO config knob pins a stage's slots:
    --expert-cache-device1..3 is the REMOTE third-GPU tier and is REFUSED with a layer split
    ("needs a GPU that runs no stage", generate.cpp:3074-3079; help 1005); --peer-* is the peer-
    device tier, not with --layer-split (help 1013).
  - The all-CPU expert fallback is NOT implementable on a split: with a profile present
    --expert-cache 0 MEANS AUTO (converted at generate.cpp:4356, issue #46 comment - old tree
    4172 identical), and with NO profile the stage cache loop dies the engine ("no room"
    return 1, 6563).  No existing knob disables per-stage GPU caches while keeping the split.
  - Membership equality has credible runtime evidence: cache fill is the profile's ranked prefix
    (CUDA0 fill loop 6448-6466, stage fill 6568-6583, stage profiles = the global ranking
    filtered per stage, 5079-5090), and STRATA_VERIFY_COMPLEMENT (env-presence test hook,
    present in BOTH binaries - new 6513, old 6111) prints a read-back membership CHECKSUM:
    "expert cache checksum %016llx (%zu slots)" over (slot, blob bytes, D2H content hash) in
    slot order.
  - Prompt chunk: --prefill N (parse 2550-2558); the runtime trim only LOWERS it ("prompt chunk
    8192 -> 4096 ... so its buffers fit in every expert cache", 8655-8657).

CORRECTNESS-RESIDENCY MODE (--correctness-residency [N], N = CUDA0 pin, default 2000;
--correctness-pin N kept as a backward-compatible alias): every mode launches a DERIVED config =
the CURRENT mode clone + ONLY the allowlisted overrides --expert-cache N and --prefill 4096
(documented chunk equality), under ops/<window>-residencyN/ at 0600 with a UNIQUE per-window
engine log, plus child env STRATA_IQ_MT_MIN=1, STRATA_ROUTE_TAIL_SKIP=0,
STRATA_VERIFY_COMPLEMENT=1.  The production configs are never written; the production-shape smoke
(`--execute` with no flag) is unchanged.

RUNTIME RESIDENCY GATE (fail-closed, BEFORE any of the 8 prompts runs): after each mode's engine
verifies ready, its CURRENT-session startup log is fingerprinted - CUDA0 slots/GiB/prefilled/
checksum, every stage's (layers, slots, GiB, filled, profiled-pairs), the per-card room-GiB lines,
the final prompt chunk - and compared with the FIRST mode's fingerprint.  Equal => the
byte-equality gate is RESIDENCY-ESTABLISHED and the 8 prompts run.  UNEQUAL (measured today: CUDA1
5340 vs 4602, chunk 8192 vs 8192->4096) => the mode FAILS with the exact numbers AND the room gap,
NO prompts run, the window stops, production restores: "residency gate NOT ESTABLISHED".
THE ACTIONABLE EQUALIZER (source-proven, both binaries): --vram-reserve-later-mib IS stage_room's
later-card reserve (generate.cpp:4499-4501; parse 2548; old tree 497e4be4 identical parse 2427, the
engine itself recommends adding it - old 10443).  Run 1 (measure): --correctness-residency with no
equalize - the gate fails at dflash_w3 and RECORDS the CUDA1 room gap.  Run 2 (equalize): add
--residency-equalize-mib <gap MiB> (measured gap at 180344: 10.00-8.61 GiB ~ 1424): the flag is
raised by that amount on the NON-dflash arms ONLY, shrinking their CUDA1 room onto the drafter
arm's; the stage cache fills as the profile's ranked prefix of the room budget (fill 6568-6583,
stage profile = global ranking filtered per stage 5079-5090), so equal room bytes => same slot
count => IDENTICAL resident membership (CUDA0 proven byte-wise by the checksum, CUDA1 by the
prefix rule).  Live-free quantization makes first-try equality likely, NOT guaranteed: a residual
gap is re-measured from the recorded room lines and re-run; the gate asserts exact equality every
time, so a false pass is impossible.  If a residency-ESTABLISHED window STILL shows byte
differences, escalate to design (oracle) - no speculative engine change.

Evidence per mode (unchanged from L16 rev): usage/timings/ttft verbatim, current-session-only
engine-log copy with byte-range + inode provenance, dflash_w3 with zero drafts offered = failure
(vacuous), AR arms offering drafts = failure, suffix diffs informational verbatim.  Prompt-size
truth = engine-OBSERVED prompt_tokens per request.  Currency/identity/family-stop/signal/restore
mechanics are imported from model_window (structural cloner diffs, exact per-mode exe/log,
parameterized identity, WindowSignal cleanup path, survivor-guarded restore).

DIVERGENCE HYPOTHESES (hypotheses ONLY - cause UNKNOWN; NO engine change without root cause):
  H1 residency-gated numeric paths: cached experts compute on the GPU, uncached on the CPU, and
     the engine itself says the roundings differ ("the GPU computes the experts in the cache; it
     rounds differently from the CPU, so a reply can differ slightly", engine log at 180344
     target_only lines 28-31); 5340 vs 4602 slots changes WHICH experts take the GPU path
     (measured hit rates 76.2% vs 73.6% in the same window).
  H2 verify-window shape: suffix arms with EQUAL residency (5340 = 5340) STILL diverged from the
     AR arm (1,3,5,7,8 / 4,8) - so residency is not the whole story; accepted-draft windows
     change per-token expert batch shapes and row counts (IQ_MT_MIN=1 pins the single-token rule
     to the multi-token kernel, native_expert.cpp:159-168, but grouped-list composition can still
     differ), and the prompt path borrows differ with cache size (2139 of 5340 vs 1392 of 4602),
     changing prompt chunking and the int8-KV accumulation grid.
  H3 the drafter arm's multi-row verify commits change the token-graph/resident-set path; not
     proven.
If a residency-established run still diverges, the parent escalates to the oracle - this lane
does not fix the engine speculatively.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import window as W                                   # shared verified snapshot/stop/restore
import model_window as mw                           # shared currency/identity/stop/evidence helpers

GATE_MODES = [("old_target_only", 1), ("target_only", 1), ("suffix_lookup", 1), ("dflash_w3", 3)]
MAXT = (128, 96, 64)                                # short / medium / long
PROTO_PORT = mw.PROTO_PORT
STARTUP_S, COLD_S, REQ_TIMEOUT = mw.STARTUP_S, mw.COLD_S, mw.REQ_TIMEOUT
LOGS, OPS = W.LOGS, W.OPS
PIN_DEFAULT = 2000                                  # CUDA0 byte-pin proven equal across arms
PIN_CHUNK = 4096                                    # measured drafter-arm chunk; pinned on ALL arms
PIN_ENV = {"STRATA_IQ_MT_MIN": "1",                  # native_expert.cpp:159-168, generate.cpp:14422
           "STRATA_ROUTE_TAIL_SKIP": "0",            # generate.cpp:10046-10052
           "STRATA_VERIFY_COMPLEMENT": "1"}          # membership checksum hook, new 6513 / old 6111
PIN_ALLOW = {"--expert-cache", "--prefill", "--vram-reserve-later-mib"}
# --vram-reserve-later-mib is the ONLY source-proven way to shrink a split's LATER-stage room:
# stage_room's base reserve for later cards IS this flag (generate.cpp:4499-4501; parse 2548;
# default -1 = same as the first card, 614; the old tree at 497e4be4 parses it identically at
# 2427 and the engine itself recommends adding it - old generate.cpp:10443).  Raising it on the
# NON-dflash arms by the measured CUDA1 room gap (AR 10.00 GiB vs dflash 8.61 GiB -> ~1424 MiB)
# makes every arm's CUDA1 room the same byte budget; the stage cache fills as the profile's
# ranked prefix of that budget (fill loop 6568-6583, profile filter 5079-5090), so equal room
# bytes => equal slot count => IDENTICAL resident membership.  Live-free quantization means
# first-try equality is likely, NOT guaranteed: the runtime gate asserts exact equality and
# fails closed, so a false pass is impossible.

# ------------------------------------------------------------------ the fixed prompt set (constants)
_MED_TECH = """A layer split runs the model's layer loop as an ordered chain of GPU stages: stage 0 owns
the first block of layers on the first card, and each later stage owns the next block on its own card.
The stages hand the hidden rows of one chunk from one card to the next over a bounded host hand-off,
and every stage keeps its own key/value pools for exactly its own layers, which is why the pools are
described as per-stage. The expert cache is sized per stage from that stage's own profile, so the
resident set on the second card differs from the resident set on the first, and the prompt path of a
stage borrows only the tail of its own cache. The verify window walks the chain once per window: each
stage reads the rows the previous stage wrote, computes its layers, and passes the rows on, so the
last stage returns rows that are the full model's answer for the window. Under speculative decoding
the window carries several candidate tokens per step, and only the rows whose chain walk was
accepted reach the commit. When a drafter is active on the last stage's card, the target taps that
the drafter reads are captured at their boundary layers, and the taps captured on other cards are
transported to the drafter's card through pinned host staging, one chunk at a time."""
_MED_CODE = """Task: write a function merge_sorted_runs(runs, k) that merges k sorted runs of int64
rows into one sorted stream, streaming, with a memory cap. Requirements: (1) use a heap of
(run_index, row_index, value) triples, never concatenate the runs; (2) when a run is exhausted, drop
its heap entry and continue; (3) respect the cap by spilling the smallest heap tail to a scratch file
and re-reading it lazily; (4) the function must be deterministic: ties break by (run_index,
row_index); (5) write three unit tests, one with empty runs, one with a single run, one with ten
runs of random ints under the cap; (6) state the complexity in terms of total rows N and k, and
explain the spill cost in big-O form and in bytes per spill. Add a docstring that states the
invariants: every emitted row appears exactly once, the output is non-decreasing, and the peak
memory stays at or below the cap for every input. Return the code with type hints and the tests;
do not use a framework, do not import anything outside the standard library, and keep the whole
module under one hundred and fifty lines so a reviewer can read it in one sitting."""
_MED_LIST = """A batched prompt reader on a two-stage layer split must choose one chunk size that fits
every stage's borrowed buffers. Work through the eight cases in order and give, for each, what the
reader must do first: (1) a fresh 4096-token prompt and every stage has room for a 2048-token chunk;
(2) a 1200-token prompt that is smaller than one chunk; (3) a read that continues a session at token
9000 on the same chunk grid; (4) a read interrupted by a yield at a chunk boundary whose part must
be parked in a slot; (5) a checkpoint restore that lands mid-segment off the grid; (6) a stage whose
cache cannot lend a full chunk, so its buffers must be allocated; (7) a second request that reuses
the whole prefix from the live session; (8) a request that starts a new conversation sharing only
the system-prompt prefix through the conversation cache. For each case state, in one short paragraph,
the chunk grid the reader must land on, which stage caches lend rows and which must allocate their own
buffers, and what the pipeline state of the stages is when the read finishes its last chunk."""
_MED_REPEAT = "\n\n".join(
    f"{name.upper()} configuration. The {name} arrangement exists to keep the key/value rows of a "
    f"stage resident where its layers are computed. The {name} arrangement holds the rows of the "
    f"stage's own layers and nothing else. The {name} arrangement evicts rows only when a newer "
    f"window cannot be admitted, never while a committed window still reads them. The {name} "
    f"arrangement is observed through the engine's reused-token count on a repeated conversation."
    for name in ("pinned", "evicted", "staged"))
_L1_HEAD = ("A two-card layer split of this model places the first 32 layers on the first card and "
            "the last 16 on the second; the following numbered lines record, in order, what the "
            "engine does from the first request onward, one line per step, so that a reader can "
            "check a live log against the design. ")
_L1_LINES = [
    "the proxy starts the engine child and the engine loads the pack, the weights at the streaming "
    "rate, and the per-stage expert arenas from their profiles",
    "the engine prints the split plan with the layer ranges and each card's cache slot count",
    "the drafter's geometry, taps, collector boundaries and pinned staging are printed once at start",
    "the first request reads its prompt in chunks, each stage processing the same chunk one behind "
    "the previous stage in the pipeline",
    "the prompt read ends with the last stage reporting the chunk and the taps of the foreign "
    "stages arriving at the drafter's card through pinned staging",
    "the first verify window is captured as a graph on each stage before the decode round runs",
    "the drafter proposes a block, the window carries it, and the accepted rows commit in order",
    "the rejected tail of the window never reaches the session, so the session stays a plain prefix",
    "each accepted token appends rows to the per-stage key/value pools of exactly its own layers",
    "the conversation cache pins the system-prompt prefix so a new chat mounts it without a re-read",
    "a repeat of the same prompt reuses the prefix rows and reports the reused token count",
    "a new unrelated prompt resets the slot's live session and reads from the mounted prefix only",
    "a sampled request on a drafter config decodes target-only with an explicit log line",
    "the checkpoint of a long read stores the session state at the segment's planned chunk grid",
    "a cancel right after a parked conversation is restored must not lose that conversation",
    "the tap collector keeps exactly two staging slots, so an unconsumed chunk is never destroyed",
    "the draft context is appended only with rows the verify window accepted as real tokens",
    "the drafter's pools stay bounded by the draft window, never by the full context length",
    "the verify watchdog ends a window that cannot complete and reports it as a timed-out window",
    "the final answer streams token by token while the next window is already in flight",
]
_L2_HEAD = ("A runbook for a two-card layer-split server, numbered line by line so an operator can "
            "follow a live session from the launcher to a restored production state. ")
_L2_LINES = [
    "the launcher starts the proxy with the production config on port 8009 and the engine child "
    "inherits the device library paths from the launcher environment",
    "the proxy verifies the model pack, the tokenizer files, and the structured layer-split key "
    "before it starts the engine",
    "the engine reports the free VRAM on each card after the arenas are registered, and warns "
    "below the reserve floor",
    "the first request pays the prompt read, the graph captures, and the drafter's first proposal "
    "cycle, so its time is not decode speed",
    "the second request on the same conversation reuses the prefix, and the reused count is the "
    "engine's own number, not an estimate",
    "the greedy gate compares texts byte-exactly across modes, and a single differing byte is a "
    "gate failure, never a rounding note",
    "the parity fixture dumps the drafter's named stage tensors for one chosen cycle and a numpy "
    "harness recomputes them per row from the artifact",
    "the cancel-park regression parks a conversation, restores it, cancels mid-turn, and proves "
    "the answer rows are reused",
    "the live-continuation regression checks cached answers against fresh reads under fixed expert "
    "residency",
    "the long-context arms use a real long fixture, and the engine-observed prompt tokens are "
    "recorded beside the requested context label",
    "the VRAM of the drafter is measured by sampling the per-process device memory during a window, "
    "not by reading a single startup line",
    "the benchmark arms run in downtime windows, one mode per server start, grouped by config "
    "sha256 so runs never mix",
    "the results of every window are kept under a unique label, owner-only, with the engine log "
    "copied per window so nothing is overwritten",
    "the production restore is verified after every window: health, model path, config sha, engine "
    "exe and the command line flags",
    "a surviving verified process stops the restore from launching a duplicate, always",
]


def _numbered(head: str, lines: list[str], repeats: int) -> str:
    """Deterministic long fixture: the numbered lines, repeated in whole passes with a global
    step counter so every emitted line carries unique content (no identical rows)."""
    out, n = [head], 1
    for _ in range(repeats):
        for ln in lines:
            out.append(f"Step {n}: {ln}.")
            n += 1
    return " ".join(out)


GATE_PROMPTS = [
    ("short", "Explain, in two short sentences, why layer splitting moves K/V pools per stage."),
    ("short", "Name, in a few words, how many GPUs a layer split of two stages uses."),
    ("medium", _MED_TECH),
    ("medium", _MED_CODE),
    ("medium", _MED_LIST),
    ("medium", _MED_REPEAT),
    ("long", _numbered(_L1_HEAD, _L1_LINES, 5)),
    ("long", _numbered(_L2_HEAD, _L2_LINES, 5)),
]
GATE_PAIRS = [("old-vs-new pure AR", "old_target_only", "target_only", True),
              ("AR vs dflash_w3", "target_only", "dflash_w3", True),
              ("AR vs suffix_lookup (informational)", "target_only", "suffix_lookup", False)]


# ------------------------------------------------------------------ residency derivation
def residency_log(window: str, mode: str, pin: int) -> str:
    return f"{LOGS}/engine-residency{pin}-{window}-{mode}.log"


def pin_derive(window: str, mode: str, cfg: dict, pin: int,
               later_reserve_mib: int | None = None) -> tuple[pathlib.Path, dict, list[dict]]:
    """Derived correctness-residency config: the CURRENT mode clone + ONLY the allowlisted
    overrides --expert-cache N and --prefill PIN_CHUNK, plus, when later_reserve_mib is given,
    --vram-reserve-later-mib raised by that amount on the NON-dflash arms ONLY (the CUDA1 room
    equalizer - see the PIN_ALLOW note).  The dflash arm keeps the clone's reserve.  Production
    configs are never written; derived files live under ops/<window>-residencyN/ at 0600.
    --expert-cache 2000 is the proven CUDA0 byte pin (measured equal 2904 slots in all four
    arms); --prefill 4096 pins the requested chunk to the drafter arm's measured value (the
    runtime trim only lowers, generate.cpp:8655-8657)."""
    d = copy.deepcopy(cfg)
    args = list(d.get("args", []))
    overrides = []

    def apply(flag: str, to: str) -> None:
        if flag in args:
            i = args.index(flag)
            overrides.append({"flag": flag, "from": args[i + 1], "to": to})
            args[i + 1] = to
        else:
            # MUTATE the closure list.  `args += [...]` REBINDS `args`, which makes it local to
            # apply, so the very first `if flag in args` raised UnboundLocalError (window
            # greedy-gate-20261022-193027: failure before any model launch, modes[] empty,
            # restore TRUE).  list.extend is a pure mutation and keeps `args` the closure list.
            args.extend([flag, to])
            overrides.append({"flag": flag, "from": None, "to": to})

    apply("--expert-cache", str(pin))
    apply("--prefill", str(PIN_CHUNK))
    if later_reserve_mib:
        cur = dict(mw.flags(args)).get("--vram-reserve-later-mib")
        base = int(cur) if cur not in (None,) and str(cur).lstrip("-").isdigit() else 0
        if mode != "dflash_w3":       # shrink the BASELINE arms' later room onto the drafter arm's
            apply("--vram-reserve-later-mib", str(base + later_reserve_mib))
    d["args"] = args
    d["log"] = residency_log(window, mode, pin)
    path = OPS / f"{window}-residency{pin}" / f"proto-server-swift15-{mode}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(d, indent=2) + "\n")
    os.chmod(path, 0o600)
    return path, d, overrides


def validate_pin_derivation(src: dict, derived: dict, overrides: list[dict],
                            window: str, mode: str, pin: int) -> list[str]:
    """The derived config must equal the source clone EXACTLY except: the allowlisted arg
    overrides and the unique per-window residency log.  Anything else = refuse the mode."""
    problems = []
    expect = dict(mw.flags(src.get("args", [])))
    for o in overrides:
        if o["flag"] not in PIN_ALLOW:
            problems.append(f"override {o['flag']} is not on the allowlist {sorted(PIN_ALLOW)}")
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
    if derived.get("log") != residency_log(window, mode, pin):
        problems.append(f"derived log {derived.get('log')!r} is not the unique per-window residency "
                        f"log {residency_log(window, mode, pin)}")
    return problems


# ------------------------------------------------------------------ residency fingerprint
FP_CUDA0 = re.compile(r"expert cache (\d+) slots, ([0-9.]+) GiB of VRAM")
FP_CUDA0_FILL = re.compile(r"pre-filled (\d+) of (\d+) slots from the profile")
FP_CHECKSUM = re.compile(r"expert cache checksum ([0-9a-f]{16}) \((\d+) slots\)")
FP_STAGE = re.compile(r"CUDA(\d+) runs layers (\d+)-(\d+), expert cache (\d+) slots \(([0-9.]+) GiB\), "
                      r"(\d+) of its (\d+) profiled pairs")
FP_CHUNK = re.compile(r"prompt chunk (\d+)(?: -> (\d+))? tokens")
FP_BORROW = re.compile(r"CUDA(\d+) prompt path borrows (\d+) of its (\d+) slots")
FP_ROOM = re.compile(r"CUDA(\d+): [0-9.]+ GiB free of [0-9.]+, room for experts ([0-9.]+) GiB")


def residency_fingerprint(rec: dict, argv_prefill: str | None = None) -> dict | None:
    """The mode's startup residency fingerprint from its CURRENT-session log copy ONLY (the
    provenanced per-window file).  Membership basis: every cache fills as the profile's ranked
    PREFIX (generate.cpp:6448-6666/6568-6583; stage profiles = the global ranking filtered per
    stage, 5079-5090), so equal (slots, filled, profile) => identical resident MEMBERSHIP; the
    CUDA0 checksum (STRATA_VERIFY_COMPLEMENT, new 6513 / old 6111) proves the CUDA0 set
    byte-wise, read back from the device.

    PROOFS, NOT NONES (tightened after the 193027 review - a missing piece is a failure, never an
    equality success):
      - `checksum_proof`: the read-back checksum, or an ABSENT proof carrying a per-fingerprint
        nonce, so two arms with no checksum can never compare equal (no `None == None` pass) -
        CUDA0 byte-wise membership is then unproven and the gate fails closed.
      - `chunk_proof`: the FINAL chunk from the log plus whether a runtime trim was observed; when
        the log carries NO chunk line the chunk is accepted ONLY on a verified proof - argv
        `--prefill == PIN_CHUNK` (4096) AND no chunk/trim line anywhere in the log (the trim only
        lowers, generate.cpp:8655-8657) - otherwise an UNVERIFIED proof with a nonce that can
        never compare equal.
    Rounded GiB strings (from the log) are DIAGNOSTIC ONLY: they are recorded and printed, never
    compared.  The compared authority is the source-proven stage/cache COUNTS and profile prefix
    (slots, prefilled, filled, pairs, layers) plus the two proofs."""
    p = (rec.get("engine_log") or {}).get("per_window_copy")
    if not p:
        return None
    try:
        text = pathlib.Path(p).read_text(errors="replace")
    except OSError:
        return None
    m0 = FP_CUDA0.search(text)
    mf = FP_CUDA0_FILL.search(text)
    mc = FP_CHECKSUM.search(text)
    stages = {int(m.group(1)): {"layers": f"{m.group(2)}-{m.group(3)}", "slots": int(m.group(4)),
                                "gib": m.group(5), "filled": int(m.group(6)),
                                "pairs": int(m.group(7))}
              for m in FP_STAGE.finditer(text)}
    chunks = [(int(m.group(1)), int(m.group(2)) if m.group(2) else int(m.group(1)))
              for m in FP_CHUNK.finditer(text)]
    borrows = [{f"cuda{m.group(1)}_borrows": int(m.group(2)), f"of_its": int(m.group(3))}
               for m in FP_BORROW.finditer(text)]
    rooms = {int(m.group(1)): float(m.group(2)) for m in FP_ROOM.finditer(text)}
    if m0 is None or mf is None or not stages:
        return None      # startup residency lines not fully present - fail closed
    chk = mc.group(1) if mc else None
    # ABSENT checksum = fail closed: the nonce makes an absent proof unique per fingerprint, so it
    # can never match another arm's absent proof (no None == None equality success).
    checksum_proof = ["checksum", chk] if mc else ["checksum-absent-UNPROVEN", os.urandom(8).hex()]
    chunk_final = chunks[-1][1] if chunks else None
    trim = [c for c in chunks if c[1] != c[0]]
    if chunks:
        chunk_proof = ["log-chunk", chunk_final, "trim-observed" if trim else "no-trim"]
    elif str(argv_prefill) == str(PIN_CHUNK) and not FP_CHUNK.search(text):
        chunk_proof = ["argv-pinned-PIN_CHUNK-no-chunk-or-trim-line", PIN_CHUNK]
    else:
        chunk_proof = ["chunk-UNVERIFIED", os.urandom(8).hex()]
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
            "checksum_present": mc is not None}


def residency_key(fp: dict) -> tuple:
    """The compared residency identity: CUDA0 (slots, prefilled, CHECKSUM PROOF), every stage
    (dev, layers, slots, filled, pairs) and the FINAL-chunk PROOF.  With equal keys AND the same
    profile file (config-identical by derivation), the resident MEMBERSHIP is identical: fill =
    profile prefix of min(slots, pairs), source-proven deterministic order.  GiB/room numbers are
    NOT in the key - they are rounded log diagnostics for the gap report; the counts, the prefix
    rule and the two proofs are the authority, and an unproven checksum or chunk carries a nonce
    that can never compare equal.  Stage device ids are normalized with str() so a fingerprint
    reloaded from the results file (JSON object keys are strings) keys identically."""
    stages = tuple(sorted((str(dev), s["layers"], s["slots"], s["filled"], s["pairs"])
                          for dev, s in fp["stages"].items()))
    return (fp["cuda0"]["slots"], fp["cuda0"]["prefilled"], tuple(fp["cuda0"]["checksum_proof"]),
            stages, tuple(fp["chunk_proof"]))


# ------------------------------------------------------------------ preflight (read-only)
def preflight(prod: dict) -> tuple[list[str], list[str]]:
    checks, problems = mw.preflight(prod)
    for mode, width in GATE_MODES:
        path = mw.CONFIG_DIR / f"proto-server-swift15-{mode}.json"
        ok = path.is_file()
        checks.append(f"gate config {mode}: {'ok' if ok else 'MISSING'} {path}")
        if not ok:
            problems.append(f"gate config missing: {path}")
    old_ok = pathlib.Path(W.PROD_EXE).exists()
    checks.append(f"old prod binary (old_target_only exe): {'ok' if old_ok else 'MISSING'} {W.PROD_EXE}")
    if not old_ok:
        problems.append(f"old prod binary missing: {W.PROD_EXE}")
    sys.path.insert(0, f"{mw.CLONE}/tools")
    try:
        import strata_bench                                    # noqa: F401 - the real chat client
        checks.append("bench client strata_bench: ok")
    except ImportError as e:
        checks.append(f"bench client strata_bench: MISSING ({e})")
        problems.append("bench client strata_bench missing")
    return checks, problems


# ------------------------------------------------------------------ the gate requests
def run_gate_requests(mode: str, rec: dict, note) -> bool:
    sys.path.insert(0, f"{mw.CLONE}/tools")
    from strata_bench import chat, digest
    base, ok = f"http://127.0.0.1:{PROTO_PORT}", True
    for n, (cls, prompt) in enumerate(GATE_PROMPTS, 1):
        max_tokens = MAXT[("short", "medium", "long").index(cls)]
        r = chat(base, "", [{"role": "user", "content": prompt}], max_tokens, think=False, timeout=REQ_TIMEOUT)
        d = digest(r)
        usage = r.get("usage") or {}
        rec["requests"].append({"n": n, "class": cls, "prompt_words": len(prompt.split()),
                                "prompt_chars": len(prompt), "max_tokens": max_tokens,
                                "observed_prompt_tokens": usage.get("prompt_tokens"),
                                "greedy": True, "reasoning": "off (reasoning_effort=none, enable_thinking=false)",
                                "ok": d.get("ok"), "err": r.get("err"), "ttft_s": d.get("ttft_s"),
                                "total_s": round(r.get("total") or 0, 3), "pieces": r.get("pieces"),
                                "usage": usage, "timings": r.get("timings"),
                                "text": r.get("text")})
        note(f"{mode}: prompt {n} ({cls}) ok={d.get('ok')} err={str(r.get('err'))[:80]} "
             f"observed_prompt_tokens={usage.get('prompt_tokens')} "
             f"completion={usage.get('completion_tokens')}")
        if not d.get("ok"):
            ok = False
    return ok


# ------------------------------------------------------------------ comparisons
def compare_texts(modes: dict) -> tuple[dict, list[str]]:
    texts = {m: [r.get("text") for r in (modes.get(m) or {}).get("requests", [])] for m in modes}
    comparison, failures = {}, []
    for label, a, b, closing in GATE_PAIRS:
        rows = []
        for i in range(len(GATE_PROMPTS)):
            la, lb = texts.get(a, []), texts.get(b, [])
            ta = la[i] if i < len(la) else None
            tb = lb[i] if i < len(lb) else None
            if ta is None or tb is None:
                eq, at = False, -1
            else:
                eq = ta == tb
                at = -1 if eq else next((k for k in range(min(len(ta), len(tb))) if ta[k] != tb[k]),
                                        min(len(ta), len(tb)))
            rows.append({"prompt": i + 1, "equal": eq, "first_diff_index": at,
                         "excerpt_a": None if eq else (ta or "")[max(0, at - 40):at + 40],
                         "excerpt_b": None if eq else (tb or "")[max(0, at - 40):at + 40]})
            if not eq and closing:
                failures.append(f"GATE {label}: prompt {i + 1} differs at index {at}")
        comparison[label] = {"pair": [a, b], "fail_closing": closing, "prompts": rows,
                             "all_equal": all(r["equal"] for r in rows)}
    return comparison, failures


def draft_counters(mode: str, rec: dict, failures: list[str]) -> None:
    offered = sum(a["offered"] for a in (rec.get("evidence") or {}).get("acceptance", []))
    rec["drafts_offered_total"] = offered
    if mode == "dflash_w3" and offered == 0:
        failures.append("dflash_w3 offered NO drafts: the AR-vs-dflash equality is vacuous without an "
                        "active drafter (evidence is the current session's log bytes only)")
    if mode in ("target_only", "old_target_only") and offered:
        failures.append(f"{mode} offered {offered} drafts - not pure autoregressive, the AR baseline is invalid")


# ------------------------------------------------------------------ the window
def run_gate(snap: dict, record: dict, note, pin: int | None = None,
             later_reserve_mib: int | None = None) -> None:
    if not W.stop_prod(snap, record, note):   # the SAME verified stop path as every window
        return
    env = os.environ.copy()
    env.update(snap["_env_full"])
    modes: dict = {}
    record["gate_failures"] = []
    for mode, width in GATE_MODES:
        summary, problems = mw.currency(mode, width, json.loads(pathlib.Path(W.PROD_CONFIG).read_text()))
        if problems:
            rec = {"mode": mode, "mode_ok": False, "mode_failures": problems, "requests": []}
            record["modes"].append(rec)
            record["break_reason"] = f"{mode}: config currency failed mid-window"
            note(record["break_reason"])
            return
        cfg = json.loads(pathlib.Path(summary["config"]).read_text())
        rec = {"mode": mode, "width": width, "config": summary["config"], "requests": [],
               "exe_want": mw.EXE_WANT.get(mode, mw.PROTO_BINARY)}
        mode_env = env
        if pin:
            dpath, dcfg, ovr = pin_derive(record["window"], mode, cfg, pin, later_reserve_mib)
            dproblems = validate_pin_derivation(cfg, dcfg, ovr, record["window"], mode, pin)
            rec.update({"source_config": summary["config"], "derived_config": str(dpath),
                        "pin_overrides": ovr, "pin_env": sorted(PIN_ENV),
                        "later_reserve_added_mib": later_reserve_mib if (later_reserve_mib and mode != "dflash_w3") else 0,
                        "config": str(dpath)})
            if dproblems:
                rec["mode_ok"], rec["mode_failures"] = False, dproblems
                record["modes"].append(rec)
                modes[mode] = rec
                record["break_reason"] = f"{mode}: residency derivation failed: {dproblems}"
                note(record["break_reason"])
                return
            cfg = dcfg
            mode_env = dict(env, **PIN_ENV)       # allowlisted comparison controls, child env only
            note(f"{mode}: residency run - derived config {dpath.name} + env {sorted(PIN_ENV)}")
        record["modes"].append(rec)
        modes[mode] = rec
        try:
            if mw.start_mode(record["window"], mode, cfg, width, mode_env, rec, note):
                bad: list[str] = []
                gate_ok = True
                if pin:
                    # RUNTIME RESIDENCY GATE before ANY prompt: fingerprint the startup log
                    # (current session bytes only) and compare with the first mode's baseline.
                    if not mw.draft_evidence(record["window"], cfg, mode, rec):
                        bad.append(f"residency fingerprint: startup log evidence refused "
                                   f"({json.dumps(rec.get('engine_log'))[:200]})")
                        gate_ok = False
                    else:
                        fp = residency_fingerprint(rec, dict(mw.flags(cfg.get("args", []))).get("--prefill"))
                        rec["residency"] = fp
                        if fp is None:
                            bad.append("residency fingerprint incomplete (startup cache/chunk lines "
                                       "missing) - fail closed")
                            gate_ok = False
                        else:
                            key = residency_key(fp)
                            base = record.get("residency_baseline")
                            if base is None:
                                record["residency_baseline"] = {"mode": mode, "key": list(key),
                                                                "fingerprint": fp}
                                note(f"{mode}: residency baseline set (cuda0 {fp['cuda0']['slots']} slots "
                                     f"checksum_proof={fp['cuda0']['checksum_proof']}, "
                                     f"stages {fp['stages']}, chunk_proof={fp['chunk_proof']} "
                                     "(giB/room recorded as diagnostics, never compared)")
                            elif list(key) != base["key"]:
                                bfp = base["fingerprint"]
                                bad.append(f"residency gate NOT established: {mode} residency "
                                           f"(cuda0 {fp['cuda0']['slots']}/{fp['cuda0']['gib']} GiB "
                                           f"checksum_proof={fp['cuda0']['checksum_proof']}, "
                                           f"stages={fp['stages']}, chunk_proof={fp['chunk_proof']}, "
                                           f"rooms={fp['room_gib']} GiB (giB/rooms are diagnostics) != "
                                           f"baseline mode {base['mode']} (cuda0 {bfp['cuda0']['slots']}/"
                                           f"{bfp['cuda0']['gib']} GiB "
                                           f"checksum_proof={bfp['cuda0']['checksum_proof']}, "
                                           f"stages={bfp['stages']}, chunk_proof={bfp['chunk_proof']}, "
                                           f"rooms={bfp['room_gib']} GiB) - equal-residency byte gate "
                                           "UNVALIDATED; NO prompts run as a false gate. ACTIONABLE: "
                                           "CUDA1 room gap (GiB) x1024 = the --residency-equalize-mib "
                                           "value for the rerun (raises --vram-reserve-later-mib on the "
                                           "non-dflash arms only); then the stage caches are the same "
                                           "profile prefix")
                                gate_ok = False
                            fp_ok = fp.get("checksum_present")
                            rec["residency_checksum"] = "present" if fp_ok else "ABSENT - fail closed"
                            if not fp_ok:
                                bad.append("residency membership UNPROVEN: no CUDA0 read-back checksum in the "
                                           "current-session log (STRATA_VERIFY_COMPLEMENT not in force) - an "
                                           "absent checksum is NEVER an equality success, fail closed")
                                gate_ok = False
                            if fp["chunk_proof"][0] == "chunk-UNVERIFIED":
                                bad.append(f"final prompt chunk UNVERIFIED: argv --prefill={fp['argv_prefill']!r} "
                                           f"is not the pinned {PIN_CHUNK}, or the log has no usable final chunk; "
                                           f"proof={fp['chunk_proof']} - fail closed. Accepted proofs: a log "
                                           f"chunk line, or argv --prefill {PIN_CHUNK} with NO chunk/trim line "
                                           "anywhere in the log (the runtime trim only lowers)")
                                gate_ok = False
                if gate_ok:
                    requests_ok = run_gate_requests(mode, rec, note)
                    ev_ok = mw.draft_evidence(record["window"], cfg, mode, rec)   # final copy incl. request lines
                    if not ev_ok:
                        bad.append("engine log evidence unreadable (provenance failed)")
                    if not requests_ok:
                        bad.append("a request failed (see the recorded error) - text equality gates the byte-compare")
                    draft_counters(mode, rec, bad)
                rec["mode_ok"], rec["mode_failures"] = not bad, bad
            else:
                rec["mode_ok"], rec["mode_failures"] = False, [rec.get("startup", "startup failed")]
        except BaseException as e:            # WindowSignal lands HERE: stop this mode, stop the window
            rec["mode_error"], rec["mode_ok"] = repr(e)[:300], False
            rec["mode_failures"] = [f"mode interrupted: {rec['mode_error']} (no further requests/modes run)"]
        note(f"{mode}: mode_ok={rec['mode_ok']} failures={rec.get('mode_failures')}")
        gone = mw.stop_mode(mode, rec, note)  # our proxy only; rec['family'] + rec['engine'] tracked
        rec["family_gone"] = gone
        if not rec["mode_ok"] or not gone:
            record["break_reason"] = f"{mode}: mode_ok={rec['mode_ok']} family_gone={gone} - restoring now"
            note(record["break_reason"])
            return
    if pin:
        established = ("residency_baseline" in record and
                       len(record["modes"]) == len(GATE_MODES) and all(m.get("mode_ok") for m in record["modes"]))
        record["residency_gate_established"] = established
        if established:
            comparison, gate_failures = compare_texts(modes)
            record["comparison"] = comparison
            record["gate_failures"] += gate_failures
        else:
            record["comparison"] = "NOT RUN - residency gate not established (see per-mode residency records)"
            record["gate_failures"].append("residency gate NOT ESTABLISHED - the byte-equality gates are "
                                           "UNVALIDATED: read the per-mode residency fingerprints and "
                                           "escalate to design; texts are NOT a pass/fail")
        gates_ok = established and record["gate_failures"] == []
        record["gate_mode"] = (f"correctness-residency pin{pin} (chunk {PIN_CHUNK}, "
                               f"later-reserve +{later_reserve_mib} MiB on non-dflash arms)"
                               if later_reserve_mib else
                               f"correctness-residency pin{pin} (chunk {PIN_CHUNK}, "
                               "measure-run: no room equalizer)")
    else:
        comparison, gate_failures = compare_texts(modes)
        record["comparison"] = comparison
        record["gate_failures"] += gate_failures
        record["gate_mode"] = ("production-shape smoke - residency NOT equalized (auto caches, chunk "
                               "trim allowed); byte comparisons here are smoke evidence, the validated "
                               "gate is the --correctness-residency run")
        gates_ok = record["gate_failures"] == []
    for label, _a, _b, closing in GATE_PAIRS:
        v = record["comparison"].get(label) if isinstance(record.get("comparison"), dict) else None
        note(f"GATE {label}: all_equal={v['all_equal'] if v else 'NOT RUN'} fail_closing={closing}")
    record["gates_ok"] = gates_ok and not record.get("break_reason")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="PARENT-only: real greedy gate (default DRY RUN)")
    ap.add_argument("--correctness-residency", type=int, nargs="?", const=PIN_DEFAULT, default=None, metavar="N",
                    help="correctness-comparison runs: derive per-mode configs = CURRENT clone + ONLY "
                         f"--expert-cache N (default {PIN_DEFAULT}) and --prefill {PIN_CHUNK} (allowlist "
                         f"{sorted(PIN_ALLOW)}), child env {sorted(PIN_ENV)}, unique per-window residency "
                         "log, and a RUNTIME RESIDENCY GATE (startup fingerprint equality across all four "
                         "modes, asserted BEFORE the 8 prompts; unequal = fail closed, no prompts, restore) "
                         "validated against the source findings in this file's docstring")
    ap.add_argument("--correctness-pin", type=int, metavar="N", default=None,
                    help="DEPRECATED backward-compatible alias for --correctness-residency (same mode)")
    ap.add_argument("--residency-equalize-mib", type=int, metavar="DELTA", default=0,
                    help="with --correctness-residency: raise --vram-reserve-later-mib by DELTA on the "
                         "NON-dflash arms ONLY (the source-proven CUDA1 room equalizer: stage_room's "
                         "later-card reserve IS this flag, generate.cpp:4499-4501, old 497e4be4:2427; "
                         "1424 = the measured 10.00-8.61 GiB gap at window 180344); the runtime gate "
                         "still asserts exact slot/membership equality - drift can never fake a pass")
    a = ap.parse_args()
    if a.correctness_residency is not None and a.correctness_pin is not None and \
            a.correctness_residency != a.correctness_pin:
        print("REFUSE: --correctness-pin and --correctness-residency disagree - give one value")
        return 2
    pin = a.correctness_residency if a.correctness_residency is not None else a.correctness_pin

    window = f"greedy-gate-{time.strftime('%Y%m%d-%H%M%S')}"
    LOGS.mkdir(parents=True, exist_ok=True)
    OPS.mkdir(parents=True, exist_ok=True)
    results, wlog = OPS / f"{window}-results.json", LOGS / f"{window}.log"
    note = lambda msg: W.note_to(wlog, msg)   # noqa: E731

    snap = W.snapshot()
    prod = json.loads(pathlib.Path(W.PROD_CONFIG).read_text())
    summaries, problems = [], []
    for mode, width in GATE_MODES:
        s, p = mw.currency(mode, width, prod)
        summaries.append(s)
        problems += p
    checks, pre = preflight(prod)
    problems += pre
    print(json.dumps({"window": window, "production_snapshot": W.display(snap),
                      "prod_config_sha256": W.config_sha(), "configs": summaries,
                      "config_currency_problems": problems, "preflight": checks,
                      "port_8019_listener_now": W.listener_pid(PROTO_PORT),
                      "modes_in_order": [m for m, _ in GATE_MODES],
                      "max_tokens_by_class": {"short": MAXT[0], "medium": MAXT[1], "long": MAXT[2]},
                      "prompt_sizes": [{"n": i + 1, "class": c, "words": len(p.split()), "chars": len(p),
                                        "token_truth": "engine observed prompt_tokens per request (usage)"}
                                       for i, (c, p) in enumerate(GATE_PROMPTS)],
                      "gates": [{"label": l, "pair": [x, y], "fail_closing": c} for l, x, y, c in GATE_PAIRS],
                      "mode_plan": (
                          {"kind": f"correctness-residency pin{pin}",
                           "derived_dir": f"{OPS}/{window}-residency{pin}",
                           "overrides_allowlist": sorted(PIN_ALLOW),
                           "overrides": {"--expert-cache": str(pin), "--prefill": str(PIN_CHUNK),
                                         "--vram-reserve-later-mib": (
                                             f"+{a.residency_equalize_mib} on non-dflash arms (CUDA1 room "
                                             "equalizer, stage_room later reserve)"
                                             if a.residency_equalize_mib else "not used (measure-run mode)")},
                           "child_env": sorted(PIN_ENV),
                           "residency_gate": "startup fingerprint per mode from the current-session log copy: "
                                             "CUDA0 slots/prefilled + the read-back CHECKSUM PROOF, every "
                                             "stage's layers/slots/filled/pairs, the FINAL-CHUNK PROOF, the "
                                             "profile path; GiB and room numbers are recorded DIAGNOSTICS and "
                                             "are never compared. Keys equal across all four modes BEFORE any "
                                             "prompt, else fail closed with no prompts and restore. An absent "
                                             "checksum is never an equality success (the arm fails closed, and "
                                             "its proof carries a per-fingerprint nonce so two absent proofs "
                                             "can never match); a log with no chunk line is accepted only on "
                                             "argv --prefill 4096 plus the absence of any chunk/trim line, "
                                             "otherwise the chunk is unverified and the arm fails closed. "
                                             "CUDA0 membership is proven by the read-back checksum, stage "
                                             "membership by the source-proven profile-prefix fill",
                           "source_findings": "CUDA0 = --expert-cache byte pin (generate.cpp:6244-6263, "
                                              "measured 2904 equal); stage caches room-sized with NO "
                                              "config knob (6521-6545, stage_room 4492-4505, kDrafterMib "
                                              "4441); --expert-cache-device1..3 = 3rd-GPU remote tier, "
                                              "refused with split (3074-3079); --expert-cache 0 + profile "
                                              "= auto (4356, old tree 4172) and profile-empty split dies "
                                              "(6563) - no CPU-fallback knob; measured invalidity of the "
                                              "L16 pin: CUDA1 5340 (AR/suffix) vs 4602 (dflash), chunk "
                                              "8192 vs 8192->4096 (window 180344)",
                           "chunk_note": f"--prefill {PIN_CHUNK} pins the REQUESTED chunk equal on all "
                                         "arms; the runtime trim only lowers (8655-8657), and the gate "
                                         "compares the FINAL chunk from the log, not the argv"}
                          if pin else
                          {"kind": "production-shape smoke",
                           "note": "configs verbatim; residency NOT equalized (measured: 2904/5340 "
                                   "caches, chunk trim 8192->4096 on the drafter arm); byte comparisons "
                                   "are smoke evidence only - the validated gate is "
                                   "--correctness-residency"}),
                      "startup_deadline_s": STARTUP_S, "cold_load_max_s": COLD_S,
                      "request_timeout_s": REQ_TIMEOUT}, indent=2))
    if not snap.get("verified"):
        print("REFUSE: " + snap.get("why", "snapshot failed"))
        return 2
    if problems:
        print("REFUSE: config currency / preflight problems above - regenerate configs with the plan "
              "tool (parent), never hand-edit them")
        return 2
    if not a.execute:
        print("DRY RUN only - zero signals, zero GPU, zero HTTP inference. Parent: rerun with --execute.")
        return 0

    record: dict = {"window": window, "started": time.strftime("%FT%T%z"), "snapshot": W.display(snap),
                    "prod_config_sha256": W.config_sha(), "configs": summaries,
                    "correctness_pin": pin, "pin_env": sorted(PIN_ENV) if pin else [],
                    "pin_chunk": PIN_CHUNK if pin else None,
                    "residency_equalize_mib": (a.residency_equalize_mib if pin else None),
                    "stop_sent": False, "modes": []}
    mw.install_signal_handlers()             # SIGTERM/SIGINT -> WindowSignal -> cleanup path, not a silent death
    try:
        run_gate(snap, record, note, pin, a.residency_equalize_mib if pin else None)
    except BaseException as e:
        record["window_error"] = repr(e)[:300]
    finally:
        surv = mw.proto_survivors(record)    # never restore with a prototype survivor alive
        if surv["exe_matches"] or surv["recorded_family_alive"] or surv["port_8019_listener"]:
            record["restore"] = {"restored": False,
                                 "refused": f"prototype family survivors {json.dumps(surv)} - restore "
                                            "REFUSED (a duplicate full model load is never attempted); "
                                            "parent: confirm the recorded pids, clean them by their "
                                            "verified pids, re-check, then restore"}
        else:
            record["restore"] = W.restore_and_verify(snap, record, time.time() + 360)
        record["status"] = ("ALL OK" if record.get("gates_ok") and record["restore"].get("restored")
                            else "RESTORED; gate failures - see logs" if record["restore"].get("restored")
                            else "RESTORE FAILED - PARENT INVESTIGATE NOW")
        W.note_to(wlog, f"WINDOW_FINAL {record['status']} restore={json.dumps(record['restore'])}")
        results.write_text(json.dumps(record, indent=2) + "\n")
        os.chmod(results, 0o600)
        print(json.dumps(record, indent=2))
    return 0 if record.get("gates_ok") and record["restore"].get("restored") else 1


if __name__ == "__main__":
    sys.exit(main())
