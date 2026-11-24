#!/usr/bin/env python3
"""faultdiff_configs.py - derive and freeze the fault-track differential's configs.

Why this exists: the fault track (plan/measurement-contract-speed-attribution-faults.md,
Phase 4) needs controlled config tests, ONE variable per boot, on the promoted port tuple.
default-margins is the one declared exception (operator directive 2026-11-23): it moves both
reserves and, through the headroom they leave, the Foresight swap grant - see its entry and
README.md.
This file is the only writer of gen/faultdiff/: every config is derived from the one base
(prod-config-port0218.json, the tuple that is serving and therefore the tuple the fault hits),
each declared change is data below, and --check fails closed when a config on disk or the base
itself drifts. Nothing here is hand-edited; nothing here boots, touches a GPU or a process.

Conventions copied from port_ab_configs.py (the Phase-2 harness):
  * ONE frozen base per arm; the generator writes the base through, never a re-typed copy.
  * Every change is a recorded delta, split into "harness" (paths the rig owns, identical for
    every leg) and "lever" (the one variable this leg tests).
  * Provenance notes that are not settings leave the config; the leg's own note replaces
    trial_note, so a config on disk says which leg it is.
  * Run dirs, sockets and the engine log are per leg; the cache dir is fresh per boot.

What it does NOT do: it does not add, remove or retune any serving flag. A leg that cannot
boot as the base plus one lever is a finding, not a config to be tuned into booting (see the
dflash-off note below and README.md).

Static facts this file relies on (port engine source = src/, the binary the base runs,
sha256 936efcc7..., verified identical strings):
  * --conversation-cache-mib 0 turns the RAM tier off (src/program/generate.cpp:1025);
    park_current_body returns at once when neither the RAM tier nor the disk tier is on
    (generate.cpp:15147), and the disk tier opens only from --conversation-cache-dir
    (generate.cpp:13520). So "mib 0 + no dir + no idle-save" is parking truly OFF.
  * --conversation-cache-park-gain 0 is the INVERTED trap: park_worth is true when
    park_gain <= 0, i.e. 0 parks every turn (generate.cpp:19494). It is not used here.
  * --spec 0 is parsed (plain decode, generate.cpp:929) but REFUSED at startup on this rig:
    --dflash requires 2 <= spec (generate.cpp:7970) and --serve requires spec >= 2
    (generate.cpp:11754). The dflash-off leg therefore removes the drafter, not the window;
    see README.md ("why --spec 0 is not the dflash-off leg").
  * --gpu N sets HIP_VISIBLE_DEVICES before any HIP call (generate.cpp:3806-3826), so
    single-card legs see exactly one Radeon VII; with one GPU the port server does not append
    --layer-split at all (serve/server_parts/engines.py:783 layer_split_of), and layer_split 30
    stays in the file as the base's (inert) key.

CPU-only: reads JSON, writes JSON under gen/faultdiff/, hashes files. No engine, no helper,
no sockets, no process control.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys

ROOT = pathlib.Path("/mnt/srcds/Strata")
HERE = pathlib.Path(__file__).resolve().parent
BASE = ROOT / "prod-config-port0218.json"
GEN = HERE / "gen" / "faultdiff"

# The fault window is its own window with its own directory (never a Phase-2 window's dir:
# the speed windows' artifacts are sha-pinned and a fault boot may not touch them).
WINDOW = "faultdiff-r1"
OUT_DEFAULT = HERE / "out" / WINDOW
PORT = 8009          # the prod port (operator rule 2026-11-23: while prod is down, trials run on 8009 too)

# The rig the base runs, pinned. faultdiff_run.py re-hashes both before a boot and refuses.
PINS = {
    "engine": "run/port0218-bin/rpcw-hip/strata",
    "engine_sha256": "936efcc798aab3952551281f2f328c1b8b19311fee9d0298142e702809a58394",
    "helper": "run/port0218-bin/rpcw-cuda/strata-expert-helper",
    "helper_sha256": "58c3ae020a7479a13c622627544507f5b43f284f9be4b5222ac90c3421b88bb2",
    "server_tree": "run/port0218-server",
}

SOCKETS = ("--expert-helper-socket", "--expert-helper-socket2")

# Provenance keys that point at another config or name a value with no consumer.
DROP_KEYS = ("derived_from", "derived_note", "managed_budget_mib")

LEG_ORDER = ("default-margins", "parking-off", "park-on-no-evict", "dflash-off",
             "single-card-0", "single-card-1", "xnack-1", "xnack-0", "arena-mlock",
             "pageable-h2d", "pool-nohuge", "pool-huge")

# The legs whose one lever is an engine environment key: leg -> (key, value). The two new
# knobs parse differently on purpose: STRATA_ARENA_MLOCK is on for ANY value (a presence test,
# include/strata/core/pin_budget.hpp:147), while STRATA_TEST_PAGEABLE is on only for '1'
# (e[0] == '1': include/strata/core/pinned_ring.hpp:173, src/prefill/prefill.cpp:244). "1" is
# the value both accept, so no leg here can be inert through its parser.
ENV_LEVER = {
    "xnack-1": ("HSA_XNACK", "1"),
    "xnack-0": ("HSA_XNACK", "0"),
    "arena-mlock": ("STRATA_ARENA_MLOCK", "1"),
    "pageable-h2d": ("STRATA_TEST_PAGEABLE", "1"),
    "pool-nohuge": ("STRATA_POOL_NOHUGEPAGE", "1"),
    # pool-huge is the CONTROL twin of pool-nohuge: the identical config with the knob
    # absent, so a PP+TG pair differs by exactly one env line. Its "value" is only a
    # marker - derive() drops the key for it instead of setting it.
    "pool-huge": ("STRATA_POOL_NOHUGEPAGE", "<absent>"),
}


def sha256_path(path):
    h = hashlib.sha256()
    with pathlib.Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


ENGINE_ENV = "STRATA_FAULTDIFF_ENGINE"
ENGINE_SHA_ENV = "STRATA_FAULTDIFF_ENGINE_SHA256"


class EngineOverrideError(RuntimeError):
    """The operator's engine override is incomplete or wrong: the harness refuses to boot."""


def engine_expected(env=None):
    """The engine this harness may run, as {"path", "sha256", "pinned_path", "pinned_sha256",
    "override", "allowed"}.

    Neither variable set: the rig pin, and nothing else (exactly the behaviour before the
    override existed). Both set: the operator's binary, verified (absolute, present, its
    sha256 equal to the one given) and recorded as an override next to the pin it replaces.
    One alone, a path that is not there, or a sha256 that does not match: EngineOverrideError.
    A fault leg that silently ran another binary would not be the rig the fault classes and
    the gates were frozen against, so the substitution is always named, never implied.
    """
    env = os.environ if env is None else env
    path = str(env.get(ENGINE_ENV) or "").strip()
    want = str(env.get(ENGINE_SHA_ENV) or "").strip().lower()
    pinned_path, pinned_sha = str(ROOT / PINS["engine"]), PINS["engine_sha256"]
    if not path and not want:
        return {"path": pinned_path, "sha256": pinned_sha, "override": False,
                "pinned_path": pinned_path, "pinned_sha256": pinned_sha,
                "allowed": (pinned_path,)}
    if not path:
        raise EngineOverrideError(ENGINE_ENV + " is not set but " + ENGINE_SHA_ENV + " is: an "
                                  "engine override needs both (the path and its sha256), or neither")
    if not want:
        raise EngineOverrideError(ENGINE_SHA_ENV + " is not set but " + ENGINE_ENV + " is: an "
                                  "engine override needs both (the path and its sha256), or neither")
    target = pathlib.Path(path)
    if not target.is_absolute():
        raise EngineOverrideError(ENGINE_ENV + " must be an absolute path, not " + path)
    if not target.is_file():
        raise EngineOverrideError(ENGINE_ENV + " names no file: " + path)
    got = sha256_path(target)
    if got != want:
        raise EngineOverrideError("engine override " + path + " is sha256 " + got + ", but "
                                  + ENGINE_SHA_ENV + " says " + want
                                  + ": the binary is not the one the operator named")
    return {"path": str(target), "sha256": want, "override": True,
            "pinned_path": pinned_path, "pinned_sha256": pinned_sha,
            "allowed": (pinned_path, str(target))}


def arg_pairs(args):
    """[(flag, value|None)] - the engine's argv as flags, keeping repeated flags in order.

    A token that does not start with '--' where a flag was expected is a generator bug:
    the base's argv is well formed and this file never writes a bare value.
    """
    out, i = [], 0
    while i < len(args):
        tok = str(args[i])
        if not tok.startswith("--"):
            raise SystemExit("orphan argv value " + repr(tok) + " in " + " ".join(map(str, args)))
        nxt = args[i + 1] if i + 1 < len(args) else None
        if nxt is not None and not str(nxt).startswith("--"):
            out.append((tok, str(nxt)))
            i += 2
        else:
            out.append((tok, None))
            i += 1
    return out


def render_args(pairs):
    flat = []
    for flag, value in pairs:
        flat.append(flag)
        if value is not None:
            flat.append(value)
    return flat


def arg_values(pairs):
    """flag -> list of values ('<flag>' for a valueless flag), in first-seen order."""
    out = {}
    for flag, value in pairs:
        out.setdefault(flag, []).append("<flag>" if value is None else value)
    return out


def get_one(pairs, flag):
    values = arg_values(pairs).get(flag)
    if values is None:
        raise SystemExit("the base config has no " + flag + ": this generator is written for "
                         "prod-config-port0218.json")
    return values[0]


def set_arg(pairs, flag, value):
    """Set a flag's value in place (first occurrence); an absent flag is appended with one."""
    for i, (f, _v) in enumerate(pairs):
        if f == flag:
            pairs[i] = (f, str(value))
            return pairs
    pairs.append((flag, str(value)))
    return pairs


def drop_flag(pairs, flag):
    return [(f, v) for f, v in pairs if f != flag]


def drop_key(cfg, key):
    cfg.pop(key, None)
    return cfg


# --------------------------------------------------------------------------- the legs
# Each leg: purpose (one line), narrows / cannot_exclude (for README.md's table), and the
# DECLARED deltas from the base. The delivered config is diffed against the base and must
# equal the declared set exactly - a forgotten step or an extra edit fails --check.
HARNESS_NOTE = ("harness: the rig's own paths, identical for every leg (log, port, the two "
                "helper sockets, the per-leg cache dir) plus the base's provenance keys")
LEGS = {
    "default-margins": {
        "purpose": "Does the fault need the explicit 256/192 margins? (both --vram-reserve* flags absent: the engine's own 700 MiB default on every card; operator directive 2026-11-23)",
        "narrows": "the explicit reserve pair as a precondition: the engine sizes the expert cache against a "
                   "larger reserve (700 MiB on card 0 and, because --vram-reserve-later-mib defaults to -1, "
                   "700 MiB on the later card too), so free VRAM and cache slots both change",
        "cannot_exclude": "the reserve as a rate confound: a larger reserve leaves a smaller expert cache, so a "
                          "clean leg is a stability data point at a different occupancy and its rates must not "
                          "be compared with the base's legs; it also cannot separate 'more free VRAM at fault "
                          "time' from 'fewer slots, less churn'. It is not single-variable either: the leg moves "
                          "BOTH reserves (256/192 -> the engine's 700/700) and, through the larger post-load "
                          "headroom, the Foresight swap grant - the base's 256/192 boots granted 0 slots, the "
                          "2026-11-23 22:58 boot of this leg granted card0:2,card1:8 - so a clean or faulting "
                          "result cannot be attributed to the reserve alone",
        "declared": [
            ("args/--vram-reserve-mib", "256", "<absent>"),
            ("args/--vram-reserve-later-mib", "192", "<absent>"),
        ],
        "present": [r"engine started: ", r"expert cache auto: .* MiB reserved"],
        "absent": [r"engine started: .*--vram-reserve-mib",
                   r"engine started: .*--vram-reserve-later-mib"],
        "observe": ["the boot's 'expert cache auto: X GiB free, 700 MiB reserved ... -> N slots' line: the "
                    "engine's own default took effect (record the slots against the base's)",
                    "the engine-started argv echo carries no --vram-reserve-mib and no "
                    "--vram-reserve-later-mib",
                    "the boot's 'strata serve: foresight swap: slots per layer per card: ...' line: record the "
                    "granted slots per card every boot. The base's 256/192 boots granted 0 slots; this leg's "
                    "2026-11-23 22:58 boot granted card0:2,card1:8, so the swap grant, not only the reserve, "
                    "differs from the base",
                    "the 'strata serve: N MiB of VRAM free with everything loaded' line (the headroom the swap "
                    "grant is sized from)",
                    "the kernel slice's fault class and the leg's survivability vs the base's exposure"],
    },
    "parking-off": {
        "purpose": "Is the park path necessary for the fault? (parking truly OFF: mib 0, no dir, no idle-save)",
        "narrows": "the whole park/restore path (host snapshot, disk park stream, the fadvise of a "
                   "parked file, restore re-registration) as a precondition",
        "cannot_exclude": "parking as a trigger: with the path off the engine also stops switching "
                          "conversations (a parked conversation is never restored, and a switch "
                          "re-prefills the whole prompt), so the leg's load stays on ONE "
                          "conversation and its prefill/decode shares differ from the base; a clean "
                          "leg means 'needs the park path', and a faulting leg still leaves "
                          "read-only serving, the helper's DMA and the platform open",
        "declared": [
            ("args/--conversation-cache-mib", "4096", "0"),
            ("args/--conversation-cache-dir", "<base>", "<absent>"),
            ("args/--conversation-cache-disk-idle-save", "<flag>", "<absent>"),
        ],
        "present": [r"engine started: .*--conversation-cache-mib 0 --"],
        "absent": [r"conversation cache: file page-cache release ON",
                   r"conversation cache: disk tier ON",
                   r"conversation cache: parked",
                   r"disk park streamed",
                   r"--conversation-cache-dir"],
        "observe": ["no 'parked' and no 'disk park streamed' line over the whole leg: the park "
                    "path never ran (park_current returns at once when neither tier is on)",
                    "the engine-started argv echo carries --conversation-cache-mib 0 and no "
                    "--conversation-cache-dir (the tier is off, not merely unused)"],
    },
    "park-on-no-evict": {
        "purpose": "Does the fadvise of the disk tier's own file pages matter? (base minus ONLY --conversation-cache-evict-page-cache)",
        "narrows": "the page-cache release (posix_fadvise DONTNEED on park writes, restore reads and "
                   "ring sidecars) as a precondition, with parking and the disk tier otherwise intact",
        "cannot_exclude": "page-cache pressure as a general factor: without the release the parked "
                          "file's pages stay cached (a different memory shape - less free RAM, more "
                          "Cached), so 'no fault' and 'fault' both carry that confound",
        "declared": [("args/--conversation-cache-evict-page-cache", "<flag>", "<absent>")],
        "present": [r"conversation cache: disk tier ON at ", r"conversation cache: RAM ceiling 512 MiB"],
        "absent": [r"conversation cache: file page-cache release ON"],
        "observe": ["'pages-released=0 (0 MiB released)' on every disk park line (base reads >0)",
                    "Cached/MemAvailable shape while parked (the pages are not handed back)"],
    },
    "dflash-off": {
        "purpose": "Is the DFlash drafter (and its cross-GPU tap collector) necessary? (drafter removed; --spec 0 is refused, see README)",
        "narrows": "the DFlash drafter's own path: its artifact, its draft passes, the target-layer "
                   "tap collection across the layer split, and the drafter's DMA reads",
        "cannot_exclude": "the drafter as a rate amplifier: without it decode is slower, so the "
                          "same wall time is a smaller exposure (record requests/tokens); and the "
                          "verify window still runs (spec 5), so the window's own kernel path stays",
        "declared": [
            ("args/--dflash", "/mnt/srcds/dflash2/artifacts/inco-dflash2-q4_0.gguf", "<absent>"),
            ("args/--dflash-block", "7", "<absent>"),
        ],
        "present": [r"engine started: .*--spec 5 "],
        "absent": [r"^dflash: /mnt/srcds/dflash2/artifacts/", r"--dflash\s+/mnt/srcds/dflash2"],
        "observe": ["no 'dflash:' artifact line: no drafter was loaded and no tap collector ran",
                    "the engine-started argv echo carries --spec 5 and no --dflash/--dflash-block",
                    "the judge line's 'drafts accepted' reads 0 of 0 over the leg (or the "
                    "prompt-lookup count, which the leg records - never assumed)"],
    },
    "single-card-0": {
        "purpose": "Card/slot split, control: the whole engine on GPU 0 (0000:04:00.0), the card the fault is NOT logged on",
        "narrows": "the split itself (two cards, cross-card traffic) as a precondition; with "
                   "single-card-1 it separates 'the tuple' from 'this card'",
        "cannot_exclude": "the slot/board: card 0 is a different physical card and a different PCIe "
                          "slot, so a fault here moves the question to that card, it does not "
                          "exonerate the hardware class",
        "declared": [("gpu", "[0, 1]", "[0]"), ("args/--gpu", "0,1", "0")],
        "present": [r"engine started: .*--gpu 0 ", r"foresight swap: .* on 1 card\(s\)",
                    r"expert-helper proxy: \d+ experts on "],
        "absent": [r"layer split across GPUs", r"layer split: CUDA1"],
        "observe": ["no layer-split line: one card, one stage",
                    "foresight swap reports 1 card and card0:<slots> (the 2,8 list truncates to "
                    "its first entry: STRATA_FS_SLOTS is per card, env kept identical on purpose)"],
    },
    "single-card-1": {
        "purpose": "Card/slot split, the suspect: the whole engine on GPU 1 (0000:09:00.0), the card the fault is logged on",
        "narrows": "card 1 alone: if the fault continues here with no second card and no split, the "
                   "remaining candidate set is this card's own path",
        "cannot_exclude": "VRAM/occupancy differences: one card holds every layer, so the expert "
                          "cache, the KV residency and the page shape all differ from the base (the "
                          "boot lines are recorded for exactly this)",
        "declared": [("gpu", "[0, 1]", "[1]"), ("args/--gpu", "0,1", "1")],
        "present": [r"engine started: .*--gpu 1 ", r"foresight swap: .* on 1 card\(s\)",
                    r"expert-helper proxy: \d+ experts on "],
        "absent": [r"layer split across GPUs", r"layer split: CUDA1"],
        "observe": ["the fault's card/vmid in the kernel slice is still card 1 (0000:09:00.0)",
                    "boot lines: expert cache slots, VRAM free, foresight slots (one card's numbers)"],
    },
    "xnack-1": {
        "purpose": "Survivability split: HSA_XNACK=1 (the runtime asks the kernel for xnack retry)",
        "narrows": "the runtime's xnack setting as the difference between 'the kernel kills the "
                   "process' and 'the fault is retried/handled'",
        "cannot_exclude": "anything about the access pattern: xnack changes what the platform does "
                          "AFTER a bad access, never which access is bad",
        "declared": [("env/HSA_XNACK", "<absent>", "1")],
        "present": [r"engine started: ", r"expert-helper proxy: \d+ experts on "],
        "absent": [],
        "observe": ["the leg's survivability vs xnack-0 at matched exposure: fault raises, process "
                    "dies, or the fault is retried and the request completes",
                    "the kernel slice's xnack/permission-fault lines for the same minutes"],
    },
    "xnack-0": {
        "purpose": "Survivability split: HSA_XNACK=0 (explicitly off; the base leaves it unset)",
        "narrows": "the same split from the other side: with xnack explicitly off, a permission "
                   "fault has no retry path",
        "cannot_exclude": "whether unset already meant off on this runtime (record both legs; the "
                          "base's unset state is the third data point)",
        "declared": [("env/HSA_XNACK", "<absent>", "0")],
        "present": [r"engine started: ", r"expert-helper proxy: \d+ experts on "],
        "absent": [],
        "observe": ["exit code and signal of the engine on a fault (killed vs error return)",
                    "the kernel slice's fault class, compared with xnack-1 at matched exposure"],
    },
    "arena-mlock": {
        "purpose": "Is a MOVABLE registered host span needed for the fault? (STRATA_ARENA_MLOCK=1: mlock() every "
                   "span cudaHostRegister pins, so reclaim, compaction and THP collapse cannot move or unmap those "
                   "pages under a queued H2D read; the base leaves the knob unset and it is on by PRESENCE, "
                   "pin_budget.hpp:147. The promoted tuple registers no expert arena - it boots 'experts via mmap' - "
                   "so the leg's own banner is its first evidence: see observe)",
        "narrows": "kernel-side relocation of a registered host span as a precondition, on any boot that registers "
                   "one: a cudaHostRegister'd range on gfx906 is a KFD userptr mapping, and amdgpu invalidates the "
                   "queues' view of it whenever the CPU page tables under it change - reclaim, swap and compaction "
                   "all do (pin_budget.hpp:129-133), and the arena is THP-requested (pinned.cu:514). mlock() is the "
                   "one knob that forbids that move. If the banner reports a nonzero locked size and the fault "
                   "stops, the precondition is a page-table change under a live registration, not the copy code",
        "cannot_exclude": "WHICH relocation: THP collapse, compaction, ordinary reclaim and swap are not separated "
                          "(mlock forbids all four for the span), and the knob changes memory pressure itself - the "
                          "locked span stops being reclaimable, so the box's memory shape (MemAvailable, Cached, "
                          "swap) differs from the base for the whole leg, a second variable the leg cannot remove. "
                          "A clean leg is 'no fault with the span pinned', not 'no fault from reclaim', and a span "
                          "mlock could not take keeps today's behavior with only a log line to say so. On the "
                          "promoted tuple the lever may have NOTHING to lock: that tuple's own boots print "
                          "'experts via mmap' (15 of them in logs/engine-prod-port0218.log) with zero 'strata pin:' "
                          "lines, so no expert arena is registered and a locked size that covers only the native "
                          "preset's mapped host tables is not a test of the expert pages",
        "declared": [("env/STRATA_ARENA_MLOCK", "<absent>", "1")],
        "present": [r"strata pin: arena mlock: RLIMIT_MEMLOCK soft limit",
                    r"strata pin: arena mlock: locked d+.d+ GiB over d+ spans in d+ ms"],
        "absent": [],
        "observe": ["the engine's own banner, 'strata pin: arena mlock: locked X.XX GiB over N spans in M ms', "
                    "with its '(complement covers N MiB)' tail when there is one (arena_mlock_report, "
                    "pin_budget.hpp:189-200). READ THE SIZE FIRST: the base tuple registers no expert arena (it "
                    "boots 'experts via mmap' and prints no 'strata pin:' line at all), so a small count means the "
                    "knob locked the native preset's mapped host tables and the expert pages were never in scope; "
                    "a count against a real arena needs a boot whose 'strata generate: expert arena:' line exists",
                    "the boot's expert-source line, 'strata generate: experts via mmap (--mmap-experts; the A/B arm "
                    "of R2.1)' (generate.cpp:8400) or 'strata generate: expert arena: ...' (generate.cpp:8656): "
                    "which one the leg booted is the difference between testing a registered arena and testing "
                    "nothing of the kind",
                    "the once-per-process 'strata pin: arena mlock: RLIMIT_MEMLOCK soft limit: unlimited' line - "
                    "or its WARNING form naming a MiB limit, which predicts a partial mlock",
                    "every 'strata pin: arena mlock: mlock of [.., +N MiB) failed (errno N, ...)' line: a failed "
                    "span kept the base's behavior, so the leg is a partial mlock and says which spans were left",
                    "no banner at all means the knob never fired (no registration reached arena_mlock_span) - the "
                    "leg is then not a test of the arena, whatever its fault outcome",
                    "whether the fault stops within the same ~5-minute window as the base, at the leg's recorded "
                    "exposure (requests, tokens, parks); a fault later than the base's is exposure, not proof",
                    "the kernel slice's fault class and vmid: the fault must not simply move to another client"],
    },
    "pageable-h2d": {
        "purpose": "Is the PINNED source of the H2D copies necessary for the fault? (STRATA_TEST_PAGEABLE=1: every "
                   "pinned host allocation with a pageable fallback takes the pageable path, so the copies come "
                   "from ordinary pageable memory)",
        "narrows": "the pageable-source copy path of the PROMPT path: the prefill ring buffers and the PLE host "
                   "buffers fall back to pageable memory (prefill.cpp:241-244, the force_pageable() gate at :763 and "
                   ":2033) and the pinned upload ring hands out no slot, so every such upload goes through direct() "
                   "- cudaMemcpyAsync from the caller's pages plus a stream wait (pinned_ring.hpp:49, :96-102, :121), "
                   "the path the code's own comment assumes is safe because the driver copied before returning "
                   "(pinned_ring.hpp:10-15). If the fault stops, that path's assumption is implicated",
        "cannot_exclude": "it is a STRESS VARIANT, not the production path: the base serves its prompt path from "
                          "pinned slots, so a fault here proves the class is reachable from a pageable source, not "
                          "that the promoted tuple takes it. The scope is narrow on this tuple in particular: its "
                          "expert pages are ALREADY a pageable file mapping ('experts via mmap', 15 boots in "
                          "logs/engine-prod-port0218.log), so the knob adds the prompt-path rings on top of an "
                          "expert path that was pageable before the leg started, and the two cannot be separated "
                          "here. It also changes the workload's timing - each staged upload waits for its stream - "
                          "so exposure and rates differ from the base's legs and decode rate is not comparable",
        "declared": [("env/STRATA_TEST_PAGEABLE", "<absent>", "1")],
        "present": [r"engine started: .*--serve ",
                    r"strata generate: expert-helper proxy: d+ experts on "],
        "absent": [],
        "observe": ["whether faults become more frequent or at-will: the first fault's wall time since ready "
                    "against the base's window, and whether a request dies before its token budget",
                    "the boot's expert-source line: 'strata generate: experts via mmap (--mmap-experts; the A/B arm "
                    "of R2.1)' (generate.cpp:8400) means the expert pages were already pageable before the knob, so "
                    "the leg's difference is the prompt-path rings only. A boot that says 'strata generate: expert "
                    "arena:' (generate.cpp:8656) instead is the other tuple, and then read its pin-mode lines too: "
                    "'FileExpertSource: allocating X.XX GiB (page-locked|pageable resident) cache complement' "
                    "(expert_source.cpp:3937), '... cache complement ready: resident X GiB, pinned Y GiB' "
                    "(:4348) and 'strata generate: resident RAM mode: ... (page-locked|locked|pageable)' "
                    "(generate.cpp:11663) - a pinned GiB that falls there would mean the leg moved a second thing",
                    "the helper's fill report and the 'expert-helper proxy:' line, unchanged from the base: the "
                    "leg must not change the expert tier's size",
                    "the engine prints NO banner of its own for the forced pageable rings - the knob only skips "
                    "the cudaHostAlloc - so the leg is asserted by its config, its env echo in the boot record and "
                    "the absence of any 'strata: pinned upload of N bytes failed' / 'upload of N bytes failed' line",
                    "exposure (requests, tokens, parks) and the decode rate, recorded as NOT comparable with the "
                    "base's: every staged upload waits for its stream"],
    },
    "pool-nohuge": {
        "purpose": "Does keeping TRANSPARENT HUGE PAGES out of the 32 GiB RAM pool mapping stop the fault? "
                   "(STRATA_POOL_NOHUGEPAGE=1: MADV_NOHUGEPAGE on the pool's own mapping)",
        "narrows": "THP collapse/split of the pool as the trigger: with the advice in place no 2 MiB page of the "
                   "pool exists, so khugepaged cannot rewrite the CPU page tables under the registered range while "
                   "the copy engine reads it. Measured basis (2026-11-24): the faulting VA of the 00:49:26 fault sat "
                   "inside this pool's 32.0 GiB anonymous VMA while that VMA was still mapped "
                   "(bench/fault-va/maps_sampler.sh), and with THP/compaction switched off system-wide, four fresh "
                   "boots completed the same 58,423-token prefill with zero faults and AnonHugePages 0 kB",
        "cannot_exclude": "the probe changed four kernel knobs at once (THP enabled, THP defrag, khugepaged defrag, "
                          "compaction proactiveness, swappiness), so the experiment implicates THP/compaction "
                          "without separating collapse from split from compaction migration; and this leg's knob "
                          "covers the POOL only - other registered ranges (prefill ring slots, PLE host buffers, "
                          "helper staging) keep the kernel's THP policy, so a fault that moves to those buffers "
                          "would look like 'still faulting'",
        "declared": [("env/STRATA_POOL_NOHUGEPAGE", "<absent>", "1")],
        "present": [r"engine started: .*--serve ",
                    r"strata: RAM pool: .* mapping: transparent huge pages off"],
        "absent": [r"strata: RAM pool: WARNING: MADV_NOHUGEPAGE"],
        "observe": ["the boot's own line 'strata: RAM pool: N MiB (N bytes) mapping: transparent huge pages off "
                    "(MADV_NOHUGEPAGE; STRATA_POOL_NOHUGEPAGE)' - that line IS the assertion that the knob fired; "
                    "a boot without it has not applied the leg",
                    "the engine's per-process AnonHugePages over the leg (grep AnonHugePages "
                    "/proc/<engine>/smaps_rollup): expected 0 kB for the pool; a non-zero value on a fresh boot "
                    "means some other mapping, and the maps sampler is what localizes it",
                    "the kernel slice's fault class and the boot's survivability against the base's ~4.5-minute "
                    "window at matched exposure (one cold 58,423-token prefill)",
                    "the memory shape beside the result: MemAvailable, swap used, per-card VRAM - the knob costs no "
                    "memory, so a delta there is a confound to name"],
    },
    "pool-huge": {
        "purpose": "CONTROL twin of pool-nohuge: the identical config with STRATA_POOL_NOHUGEPAGE absent, so a "
                   "pool-huge/pool-nohuge pair differs by exactly one env line (used for PP+TG A/B on one binary)",
        "narrows": "nothing on its own - it is the baseline the pool-nohuge arm is differenced against",
        "cannot_exclude": "everything the knob could matter for: this leg is the uncontrolled condition, so a "
                          "fault here is the expected baseline behaviour and a clean run says only that this boot "
                          "was lucky. Never report it as evidence against the fix",
        "declared": [],   # no lever: this leg IS the base with the harness paths, nothing else
        "present": [r"engine started: .*--serve "],
        "absent": [r"strata: RAM pool: .* mapping: transparent huge pages off"],
        "observe": ["the absence of the 'RAM pool: ... transparent huge pages off' line IS the assertion that the "
                    "knob is not applied; the boot must look like the promoted-tuple boots before 2026-11-24",
                    "on a PP+TG pair: prefill rate from request 1 and decode rate from request 2 of the same judge "
                    "line, so the two arms are compared on identical prompt bytes",
                    "the kernel slice's fault class - a fault here is not a defect of the control, it is the "
                    "baseline risk that the fix is meant to remove"],
    },
}


def leg_paths(out, leg):
    return {
        "engine_log": str(out / "logs" / (leg + ".engine.log")),
        "helper_log": str(out / "logs" / (leg + ".helper.log")),
        "boot_log": str(out / "logs" / (leg + "-boot.log")),
        "socket": str(out / "run" / ("fd-" + leg + ".sock")),
        "socket2": str(out / "run" / ("fd-" + leg + "-2.sock")),
        "cache_dir": str(out / "cache" / leg),
        # the booted copy: the runner writes it from the frozen file and refuses unless the
        # bytes hash to the manifest pin (the server writes its settings sidecar beside it, and
        # that sidecar must never land in gen/faultdiff/)
        "config": str(out / "configs" / (leg + ".json")),
        "frozen": str(GEN / (leg + ".json")),
        "state": str(out / "run" / (leg + ".state.json")),
        "records": str(out / "records" / leg),
    }


def derive(leg, out=OUT_DEFAULT):
    """One leg's config: the base through the harness paths, then exactly its declared lever."""
    if leg not in LEGS:
        raise SystemExit("unknown leg " + repr(leg) + ": choose " + ", ".join(LEG_ORDER))
    cfg = json.loads(BASE.read_text())
    paths = leg_paths(out, leg)
    pairs = arg_pairs(cfg["args"])

    # 1. provenance notes that are not settings (the leg writes its own trial_note)
    for key in DROP_KEYS:
        drop_key(cfg, key)

    # 2. the rig's paths - identical for every leg
    cfg["log"] = paths["engine_log"]
    cfg["port"] = PORT
    cfg["expert_helper_socket"] = paths["socket"]
    cfg["expert_helper_socket2"] = paths["socket2"]
    pairs = [p for p in pairs if p[0] not in SOCKETS]
    if "--conversation-cache-dir" not in arg_values(pairs):
        raise SystemExit("the base config lost --conversation-cache-dir: regenerate this file "
                         "deliberately")
    set_arg(pairs, "--conversation-cache-dir", paths["cache_dir"])

    # 3. the leg's own lever - one variable, never a retune
    if leg == "default-margins":
        pairs = drop_flag(pairs, "--vram-reserve-mib")
        pairs = drop_flag(pairs, "--vram-reserve-later-mib")
    elif leg == "parking-off":
        set_arg(pairs, "--conversation-cache-mib", "0")
        pairs = drop_flag(pairs, "--conversation-cache-dir")
        pairs = drop_flag(pairs, "--conversation-cache-disk-idle-save")
    elif leg == "park-on-no-evict":
        pairs = drop_flag(pairs, "--conversation-cache-evict-page-cache")
    elif leg == "dflash-off":
        pairs = drop_flag(pairs, "--dflash")
        pairs = drop_flag(pairs, "--dflash-block")
    elif leg in ("single-card-0", "single-card-1"):
        card = "0" if leg.endswith("-0") else "1"
        cfg["gpu"] = [int(card)]
        set_arg(pairs, "--gpu", card)
    elif leg in ENV_LEVER:
        env = dict(cfg.get("env") or {})
        key, value = ENV_LEVER[leg]
        if value == "<absent>":       # the control twin: the key must NOT be present
            env.pop(key, None)
        else:
            env[key] = value
        cfg["env"] = env
    else:                                        # pragma: no cover - guarded above
        raise SystemExit("no transform for " + leg)

    cfg["args"] = render_args(pairs)
    cfg["trial_note"] = ("faultdiff leg " + leg + " (window " + str(out.parent.name) + "): "
                         + LEGS[leg]["purpose"] + " Base prod-config-port0218.json sha256 "
                         + sha256_path(BASE)[:16] + "; generated by faultdiff_configs.py - do not "
                         "hand-edit. Boot and stop through faultdiff_run.py; the operator gates "
                         "the window.")
    return cfg, paths


def flatten(cfg):
    """The config as comparable items: top-level keys, env keys, args flags."""
    items = {}
    for key, value in cfg.items():
        if key in ("args", "env"):
            continue
        items[key] = value
    for key, value in (cfg.get("env") or {}).items():
        items["env/" + key] = str(value)
    for flag, values in arg_values(arg_pairs(cfg["args"])).items():
        items["args/" + flag] = values[0] if len(values) == 1 else list(values)
    return items


def normalize(value):
    """A comparable string: '<absent>' for a missing key, JSON for a list."""
    if value is None:
        return "<absent>"
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def declared_for(leg, out=OUT_DEFAULT):
    """The full declared delta set of one leg: the harness deltas plus its lever.

    Markers resolve here, once: "<cache>" is the leg's own cache dir, "<base>" the base's.
    """
    base = json.loads(BASE.read_text())
    paths = leg_paths(out, leg)
    base_cache = arg_values(arg_pairs(base["args"]))["--conversation-cache-dir"][0]
    declared = {
        "trial_note": ["<base note>", "<leg note>"],
        "derived_from": [base.get("derived_from"), "<absent>"],
        "derived_note": [base.get("derived_note"), "<absent>"],
        "managed_budget_mib": [base.get("managed_budget_mib"), "<absent>"],
        "log": [base.get("log"), paths["engine_log"]],
        "port": [base.get("port"), PORT],
        "expert_helper_socket": [base.get("expert_helper_socket"), paths["socket"]],
        "expert_helper_socket2": [base.get("expert_helper_socket2"), paths["socket2"]],
        "args/--conversation-cache-dir": [base_cache, paths["cache_dir"]],
    }
    # A harness delta that no longer changes anything (the trial port is now the prod port)
    # is not a delta: emitting it would fail the declared-vs-actual diff.
    declared = {k: v for k, v in declared.items() if str(v[0]) != str(v[1])}
    for field, old, new in LEGS[leg]["declared"]:
        if old == "<cache>":
            old = paths["cache_dir"]
        if old == "<base>":
            old = base_cache
        declared[field] = [old, new]
    return declared


def diff_from_base(leg, out=OUT_DEFAULT):
    """Every setting that is not identical to the base, as {field: [from, to]}.

    A field the base does not carry diffs from '<absent>'; a flag the leg drops diffs to
    '<absent>' ('<flag>' means the flag is present without a value). The leg's own note is
    rendered by its marker, never by its text: a note is not a measurement.
    """
    base = flatten(json.loads(BASE.read_text()))
    cur = flatten(derive(leg, out)[0])
    out_map = {}
    for field in sorted(set(base) | set(cur)):
        if field == "trial_note":
            out_map[field] = ["<base note>", "<leg note>"]
            continue
        before, after = normalize(base.get(field)), normalize(cur.get(field))
        if before != after:
            out_map[field] = [before, after]
    return out_map


def expected(leg, out=OUT_DEFAULT):
    """The declared delta set, rendered the way diff_from_base renders it."""
    return {field: [normalize(old), normalize(new)]
            for field, (old, new) in declared_for(leg, out).items()}


def exe_problems(leg, exe):
    """Is this config's "exe" a binary the harness may run for THIS leg?

    The rig's pinned engine, always. When the operator's override is complete and valid
    (STRATA_FAULTDIFF_ENGINE + STRATA_FAULTDIFF_ENGINE_SHA256, both, file present, sha256
    equal), its absolute path is the only other value accepted. A half-set override is a
    problem here exactly as it is at boot time: one variable alone never widens the set.
    """
    try:
        allowed = engine_expected()["allowed"]
    except EngineOverrideError as error:
        return [leg + ": exe is " + str(exe) + " (" + str(error) + ")"]
    if str(exe) not in allowed:
        return [leg + ": exe is " + str(exe)]
    return []


def verify(leg, out=OUT_DEFAULT):
    """Problems with this leg: the delta must be exactly the declared one, and it must parse."""
    problems = []
    cfg, paths = derive(leg, out)
    got, want = diff_from_base(leg, out), expected(leg, out)
    for field in sorted(set(got) | set(want)):
        if field not in want:
            problems.append(leg + ": undeclared change " + field + " = " + str(got[field]))
        elif field not in got:
            problems.append(leg + ": declared change missing: " + field)
        elif got[field] != want[field]:
            problems.append(leg + ": " + field + " is " + str(got[field]) + ", declared "
                            + str(want[field]))
    # the leg's own invariants, restated where a wrong base would be invisible in the diff
    pairs = arg_pairs(cfg["args"])
    vals = arg_values(pairs)
    if leg == "parking-off":
        for flag in ("--conversation-cache-dir", "--conversation-cache-disk-idle-save"):
            if flag in vals:
                problems.append(leg + ": " + flag + " is still present")
        if get_one(pairs, "--conversation-cache-mib") != "0":
            problems.append(leg + ": --conversation-cache-mib is not 0")
        if get_one(pairs, "--conversation-cache-park-gain") != "4096":
            problems.append(leg + ": park-gain moved - the inverted trap is park-gain 0")
    if leg == "park-on-no-evict" and "--conversation-cache-evict-page-cache" in vals:
        problems.append(leg + ": the evict flag is still present")
    if leg == "dflash-off":
        for flag in ("--dflash", "--dflash-block"):
            if flag in vals:
                problems.append(leg + ": " + flag + " is still present")
        if get_one(pairs, "--spec") != "5":
            problems.append(leg + ": --spec moved; this leg removes the drafter only")
    if leg in ("single-card-0", "single-card-1"):
        if len(cfg.get("gpu") or []) != 1:
            problems.append(leg + ": gpu key is " + str(cfg.get("gpu")))
        if cfg.get("layer_split") != 30:
            problems.append(leg + ": layer_split key moved (inert with one GPU; keep it as the base)")
        if get_one(pairs, "--expert-helper-slots") != "4600":
            problems.append(leg + ": helper slots moved")
    if leg in ENV_LEVER:
        env_key, env_value = ENV_LEVER[leg]
        # "<absent>" marks a control twin: the key must not be present at all.
        if env_value == "<absent>":
            if env_key in (cfg.get("env") or {}):
                problems.append(leg + ": " + env_key + " is present but this leg is its control")
        elif (cfg.get("env") or {}).get(env_key) != env_value:
            problems.append(leg + ": " + env_key + " is not " + env_value)
        if (cfg.get("env") or {}).get("HSA_OVERRIDE_GFX_VERSION") != "9.0.6":
            problems.append(leg + ": HSA_OVERRIDE_GFX_VERSION left the env block")
        # the other env legs' keys must not have crept in: one variable means one variable
        for other in ENV_LEVER.values():
            if other[0] != env_key and other[0] in (cfg.get("env") or {}):
                problems.append(leg + ": " + other[0] + " is in the env block and is not this "
                                "leg's lever")
    problems += exe_problems(leg, cfg.get("exe"))
    for value in (cfg["log"], cfg["expert_helper_socket"], cfg["expert_helper_socket2"]):
        if "prod" in value or "port0218-cache" in value:
            problems.append(leg + ": a harness path still points at prod: " + value)
    try:
        json.loads(json.dumps(cfg))
    except (TypeError, ValueError) as error:
        problems.append(leg + ": config does not serialize: " + str(error))
    return problems


def body(cfg):
    return (json.dumps(cfg, indent=1, sort_keys=True) + "\n").encode()


def manifest(out=OUT_DEFAULT):
    legs = {}
    for leg in LEG_ORDER:
        cfg, _ = derive(leg, out)
        legs[leg] = {
            "file": leg + ".json",
            "sha256": hashlib.sha256(body(cfg)).hexdigest(),
            "purpose": LEGS[leg]["purpose"],
            "diff_from_base": diff_from_base(leg, out),
            "present": LEGS[leg]["present"],
            "absent": LEGS[leg]["absent"],
            "observe": LEGS[leg]["observe"],
            "narrows": LEGS[leg]["narrows"],
            "cannot_exclude": LEGS[leg]["cannot_exclude"],
        }
    return {
        "window": WINDOW,
        "out_dir": str(out),
        "port": PORT,
        "base": str(BASE.relative_to(ROOT)),
        "base_sha256": sha256_path(BASE),
        "pins": PINS,
        "leg_note": HARNESS_NOTE,
        "legs": legs,
    }


def write(out=OUT_DEFAULT):
    problems = []
    GEN.mkdir(parents=True, exist_ok=True)
    for leg in LEG_ORDER:
        problems += verify(leg, out)
        cfg, paths = derive(leg, out)
        (GEN / (leg + ".json")).write_bytes(body(cfg))
    (GEN / "manifest.json").write_bytes(body(manifest(out)))
    return problems


def check(out=OUT_DEFAULT):
    problems = []
    for leg in LEG_ORDER:
        problems += verify(leg, out)
        cfg, _ = derive(leg, out)
        target = GEN / (leg + ".json")
        if not target.is_file():
            problems.append(leg + ": missing " + str(target))
            continue
        on_disk = target.read_bytes()
        if on_disk != body(cfg):
            problems.append(leg + ": " + str(target) + " is not what the generator produces "
                                                          "(regenerate deliberately, never edit)")
        try:
            json.loads(on_disk.decode())
        except ValueError as error:
            problems.append(leg + ": does not parse: " + str(error))
    pinned = GEN / "manifest.json"
    if not pinned.is_file():
        problems.append("missing " + str(pinned))
    else:
        want = body(manifest(out))
        if pinned.read_bytes() != want:
            problems.append(str(pinned) + " is not what the generator produces")
    return problems


def main():
    ap = argparse.ArgumentParser(description="derive/freeze the fault-track differential")
    ap.add_argument("--leg", choices=list(LEG_ORDER), help="one leg: write it, or print it")
    ap.add_argument("--out-dir", type=pathlib.Path, default=OUT_DEFAULT,
                    help="the window directory the legs' paths point into (default: " + str(OUT_DEFAULT) + ")")
    ap.add_argument("--write", action="store_true", default=True, help="write gen/faultdiff/ (default)")
    ap.add_argument("--check", action="store_true", help="verify only; nonzero on drift")
    ap.add_argument("--print", dest="show", action="store_true", help="print a leg's config")
    ap.add_argument("--diff", action="store_true", help="print a leg's diff from the base")
    ap.add_argument("--list", action="store_true", help="list the legs and their purpose")
    a = ap.parse_args()
    if a.list:
        for leg in LEG_ORDER:
            print(leg.ljust(18) + LEGS[leg]["purpose"])
        return 0
    if a.show or a.diff:
        if not a.leg:
            ap.error("--print/--diff need --leg")
        cfg, _ = derive(a.leg, a.out_dir)
        if a.show:
            sys.stdout.write(json.dumps(cfg, indent=1, sort_keys=True) + "\n")
        for field, (before, after) in diff_from_base(a.leg, a.out_dir).items():
            print(field + ": " + before + " -> " + after)
        return 0
    if a.check:
        problems = check(a.out_dir)
        for p in problems:
            print("PROBLEM: " + p, file=sys.stderr)
        print(("faultdiff_configs: OK (" + str(len(LEG_ORDER)) + " legs, base "
               + sha256_path(BASE)[:16] + ")") if not problems else
              "faultdiff_configs: " + str(len(problems)) + " problem(s)", file=sys.stderr)
        return 0 if not problems else 1
    if a.leg:
        problems = verify(a.leg, a.out_dir)
        for p in problems:
            print("PROBLEM: " + p, file=sys.stderr)
        if problems:
            return 1
        cfg, _ = derive(a.leg, a.out_dir)
        (GEN / (a.leg + ".json")).write_bytes(body(cfg))
        print("wrote " + str(GEN / (a.leg + ".json")) + " (" + str(len(diff_from_base(a.leg, a.out_dir)))
              + " deltas from the base)")
        return 0
    problems = write(a.out_dir)
    for p in problems:
        print("PROBLEM: " + p, file=sys.stderr)
    print("faultdiff_configs: wrote " + str(len(LEG_ORDER)) + " legs under " + str(GEN)
          + (" (with " + str(len(problems)) + " problem(s))" if problems else ""))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
