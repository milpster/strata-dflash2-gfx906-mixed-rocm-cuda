#!/usr/bin/env python3
"""A/B helper ON/OFF window support: true-production config derivation, request-aware
serve-line analysis and the sample-validity gates the run-1 summary lacked.

WHY THIS FILE EXISTS (the two run-1 defects it replaces, .slim/deepwork/mixed-cuda-finish/
ab-lifetime-audit.md:7-15):
  * the arm configs came from an m4-derived base that silently DROPPED production settings
    (layer_split 32, the vision block, the conversation-cache arguments, --suffix-draft 0, the
    explicit --expert-helper-slots 4000, env STRATA_DFLASH_SAMPLED) - equal settings BETWEEN the
    arms never established parity WITH production;
  * the summary grepped the literal prompt token total 10443, the requests actually carry 10470,
    and the metric search did not separate the generated tok/s from the prompt tok/s.

This tool is file-only.  No mode starts, stops, signals, binds or listens: `derive`,
`helper-opts`, `request`, `sha`, `hashreply` and `analyze` read files and write artifacts.  The
GPU window belongs to the harness (build-mixed-gpu/cap1-validation/ab-window.sh) and to the parent.

Modes
  derive       derive BOTH arm configs from the FULL production config (read-only) and write the
               substitution manifest with the production config/launcher/binary fingerprints.
               The allowed delta is exactly: exe, log, port, model_name, cwd, and the helper
               socket (present on ON, absent on OFF).  Every other field must be identical, and
               the helper flags are read from the canonical launcher's own text, never copied by
               hand.
  helper-opts  print the canonical helper control-process flags (device 0, slots 4000, reserve
               300 MiB, --timeout 7200000, --begin-timeout 1800000, --socket <candidate>), one
               per line, verified against the launcher text.  Both arms get the SAME list.
  request      write one exact request body (prompt from a FILE, temperature 0, pinned
               max_tokens) - the bytes both arms must send.
  sha          print the sha256 (16 hex) of a file's bytes.
  hashreply    record one probe's request/content/message hashes: "label req=.. content=..
               message=.. finish=.. ct=..".
  analyze      associate serve lines to probes by BOUNDED ENGINE LOG RANGES from the probe
               records (never by a hardcoded token total), parse the generated rate apart from
               the prompt rate, apply the validity gates (completeness, duplicates, ambiguity,
               cache reuse, partial read, ON positive helper service on EVERY sample incl. XL,
               OFF no fill and no service, helper liveness before and after every ON probe,
               cross-arm identical request bytes) and print the medians.  NO accepted speed
               verdict is produced when any gate fails, even when a numeric median clears 5%.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------------------------
# The canonical helper control-process options, READ FROM THE LAUNCHER'S OWN TEXT
# (launch-prod-swift15-dflash-w4-q4_0-mixed.sh:36-40).  `derive`/`helper-opts` refuse to emit
# anything the launcher does not say: the harness never re-types these numbers.
# ---------------------------------------------------------------------------------------------

LAUNCHER_FLAG_RE = {
    "--device": r"--device\s+(\d+)",
    "--slots": r"--slots\s+(\d+)",
    "--reserve-mib": r"--reserve-mib\s+(\d+)",
    "--timeout": r"--timeout\s+(\d+)",
    "--begin-timeout": r"--begin-timeout\s+(\d+)",
    "--pack": r"--pack\s+(\S+)",
    "--experts": r"--experts\s+(\S+)",
}

# The A/B runs the helper on the same options production uses.  These literals are the GATE:
# launcher_text_of() compares them, so a launcher change makes derive FAIL rather than drift.
CANONICAL_HELPER_OPTS = {
    "--device": "0",
    "--slots": "4000",
    "--reserve-mib": "300",
    "--timeout": "7200000",
    "--begin-timeout": "1800000",
    "--pack": os.environ.get("STRATA_AI_DIR", "/mnt/user/ai") + "/iq3xxs-15",
    "--experts": "../Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf",
}

# ---------------------------------------------------------------------------------------------
# Config derivation
# ---------------------------------------------------------------------------------------------

SOCKET_FLAG = "--expert-helper-socket"

# The ONLY config fields an A/B candidate may differ in from production.  Everything else is a
# production execution setting and rides the candidates unchanged.
ALLOWED_SUBSTITUTIONS = {
    "exe": "the candidate engine binary (the live production binary is never a candidate)",
    "log": "the candidate engine log inside the evidence directory",
    "port": "the candidate loopback port (production keeps 8009)",
    "model_name": "the candidate model label (ab1-on / ab1-off)",
    "cwd": "the candidate working directory (the worktree the candidate binaries live in)",
    "expert_helper_socket": "the candidate socket path (ON arm only, never the production socket)",
}

REQUIRED_ARG_FLAGS = (
    "--spec", "--spec-min-p", "--max-context", "--kv", "--vram-reserve-mib", "--kv-resident",
    "--prefill", "--suffix-draft", "--expert-helper-slots", "--conversation-cache-mib",
    "--conversation-cache-min-tokens", "--conversation-cache-park-gain", "--prompt-cache-spread",
    "--expert-cache-cpu-order", "--dflash", "--dflash-block", "--native", "--native-dense-gguf",
    "--ple-gguf", "--expert-profile", "--gpu",
)
REQUIRED_TOP_KEYS = (
    "layer_split", "vision", "sampling", "env", "tokenizer", "lib_dirs", "gpu", "host",
    "backend", "reasoning_effort", "fit_max_tokens", "env_to_user",
)
REQUIRED_ENV_KEYS = ("STRATA_DFLASH_SAMPLED",)


def strip_arg_pair(args: list[str], flag: str) -> list[str]:
    """Drop every `flag <value>` pair from an argument list (the shipped default path carries
    none, so this is the defensive OFF-arm transform)."""
    out, i = [], 0
    while i < len(args):
        if args[i] == flag:
            i += 2
            continue
        out.append(args[i])
        i += 1
    return out


def launcher_opts(launcher_text: str) -> dict[str, str]:
    """The helper options the canonical launcher actually passes, parsed from its text
    (read-only: the launcher is never executed or sourced)."""
    found: dict[str, str] = {}
    for flag, pattern in LAUNCHER_FLAG_RE.items():
        m = re.search(pattern, launcher_text)
        if m:
            found[flag] = m.group(1)
    return found


def fingerprint(path: str | Path) -> dict[str, object]:
    """sha256 + size + mtime of one file: the manifest pins what the window measured against."""
    p = Path(path)
    data = p.read_bytes()
    st = p.stat()
    return {
        "path": str(p),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "mtime": int(time.time_ns() // 1000) if not p.exists() else int(st.st_mtime),
    }


def derive_arms(prod: dict, subs: dict) -> tuple[dict, dict]:
    """Build the ON and OFF arm configs from the FULL production config.

    ON  = production with the allowed substitutions, its "expert_helper_socket" pointed at the
          CANDIDATE socket (serve/server.py:expert_helper_args then injects the engine's
          --expert-helper-socket pair, exactly as production does).
    OFF = ON with the top-level socket key REMOVED (no injection) and any socket arg pair
          stripped (the shipped byte-identical default path: the engine never creates the helper
          client).  The unused helper slot/timeout keys stay identical on both arms.
    """
    on = copy.deepcopy(prod)
    on["exe"] = subs["exe"]
    on["log"] = subs["log"]
    on["port"] = subs["port"]
    on["model_name"] = subs["model_name"]
    on["cwd"] = subs["cwd"]
    on["expert_helper_socket"] = subs["socket"]
    # The socket rides the TOP-LEVEL KEY ONLY, exactly as production does: server.py's
    # expert_helper_args injects the --expert-helper-socket pair AND the --expert-helper-timeout-ms
    # pair TOGETHER - but ONLY while the flag is not already in args (its already-in-args guard,
    # serve/server.py:3151-3153).  run2 put the pair straight into args: the guard then swallowed
    # the timeout injection, the engine ran its 20 ms DEFAULT (generate.cpp:587), the first cold
    # window request missed it, the channel closed, and the helper died having served 0 windows.
    # NEVER append the socket pair to args.
    on["args"] = strip_arg_pair(list(on.get("args", [])), SOCKET_FLAG)

    # TUNED WINDOW (tuned-window-design.md): with an explicit second-channel path the ON arm
    # ALSO carries the top-level expert_helper_socket2 key (server.py injects the
    # --expert-helper-socket2 pair exactly the way it injects the primary), and BOTH arms turn
    # the prompt tier ON through the config env (server.py applies cfg["env"] to the engine,
    # serve/server.py:3534 - deliberately immune to the launcher wrapper's STRATA_* scrub).
    # Parity holds: the arms still differ ONLY by the helper connection(s), and
    # STRATA_PREFILL_HELPER is inert on OFF (no sockets -> no clients).
    if subs.get("socket2"):
        on["expert_helper_socket2"] = subs["socket2"]
        on.setdefault("env", {})["STRATA_PREFILL_HELPER"] = "1"

    off = copy.deepcopy(on)
    off.pop("expert_helper_socket", None)
    off.pop("expert_helper_socket2", None)
    off["args"] = strip_arg_pair(off["args"], SOCKET_FLAG)
    off["model_name"] = subs["model_name_off"]
    return on, off


def _env_matches_prod(prod: dict, arm: dict, tuned: bool) -> bool:
    """The config env may differ from production in EXACTLY one way: the tuned window's
    STRATA_PREFILL_HELPER='1' (required on BOTH arms, inert on OFF).  Canonical mode allows no
    such key at all; everything else must be production's env, byte for byte.  Production
    itself may carry the key (the tuned config was adopted 2026-10-31), so the comparison
    pops it from BOTH sides: every OTHER entry must match production, byte for byte."""
    e = dict(arm.get("env") or {})
    p = dict(prod.get("env") or {})
    if "STRATA_PREFILL_HELPER" in e:
        if not tuned or e.get("STRATA_PREFILL_HELPER") != "1":
            return False
        e.pop("STRATA_PREFILL_HELPER", None)
    p.pop("STRATA_PREFILL_HELPER", None)
    return e == p


def check_arm_pair(prod: dict, on: dict, off: dict, subs: dict) -> list[str]:
    """Every parity rule, as FAILURES (empty list = the arms are accepted)."""
    fails: list[str] = []
    tuned = bool(subs.get("socket2"))
    allowed_on = set(ALLOWED_SUBSTITUTIONS) | ({"expert_helper_socket2"} if tuned else set())

    # 0. An ADOPTED (tuned) production config is only derivable in tuned mode.  A canonical
    #    derive would deepcopy production's own second-channel key and prompt-tier env onto
    #    its ON arm: a hybrid arm nobody requested, refused by name.
    prod_tuned = ("expert_helper_socket2" in prod
                  or "STRATA_PREFILL_HELPER" in (prod.get("env") or {}))
    if prod_tuned and not tuned:
        fails.append("production config is tuned (expert_helper_socket2 / STRATA_PREFILL_HELPER "
                     "present); run the tuned window (AB1_FAMILY=ab2 AB1_SOCKET2=1) or de-tune "
                     "the production config first")

    # 1. Field preservation: no production field silently dropped, no field invented.
    prod_keys, on_keys, off_keys = set(prod), set(on), set(off)
    invented_ok = {"expert_helper_socket2"} if tuned else set()
    for k in sorted(prod_keys - on_keys):
        fails.append(f"ON dropped production field '{k}'")
    for k in sorted(on_keys - prod_keys - invented_ok):
        fails.append(f"ON invented field '{k}'")
    # OFF is the shipped default path: BOTH helper sockets are removed (an adopted
    # production config carries the second one too - dropping it is the point of OFF).
    expect_off = prod_keys - {"expert_helper_socket", "expert_helper_socket2"}
    for k in sorted(expect_off - off_keys):
        fails.append(f"OFF dropped production field '{k}'")
    for k in sorted(off_keys - expect_off):
        fails.append(f"OFF invented field '{k}'")

    # 2. The engine argument list is production's, BYTE FOR BYTE, on both arms.  The socket pair
    #    must NEVER appear in args: an args-borne pair trips server.py's already-in-args guard,
    #    silently swallows the --expert-helper-timeout-ms injection, and the engine then runs its
    #    20 ms default - the exact shape that killed run2's helper (0 windows served).
    prod_args = list(prod.get("args", []))
    if on["args"] != prod_args:
        fails.append("ON engine args differ from production (the socket must ride the top-level key, never args)")
    if off["args"] != prod_args:
        fails.append("OFF engine args differ from production")
    if prod_args.count(SOCKET_FLAG):
        fails.append("production args already carry the socket flag; derive assumes the top-level key")
    if SOCKET_FLAG in on["args"] or SOCKET_FLAG in off["args"]:
        fails.append("a derived arm carries --expert-helper-socket in args; server.py would skip the "
                     "timeout injection and the engine would run its 20 ms default (the run-2 death)")
    if SOCKET_FLAG in off["args"]:
        fails.append("OFF still carries the socket flag")

    # 3. Only the allowed fields differ from production, and by the exact substitution values.
    for key, want in (("exe", subs["exe"]), ("log", subs["log"]), ("port", subs["port"]),
                      ("cwd", subs["cwd"]), ("model_name", subs["model_name"])):
        if on.get(key) != want:
            fails.append(f"ON field '{key}' = {on.get(key)!r}, the substitution said {want!r}")
        off_want = subs["model_name_off"] if key == "model_name" else want
        if off.get(key) != off_want:
            fails.append(f"OFF field '{key}' = {off.get(key)!r}, the substitution said {off_want!r}")
    # ('args' is pinned exactly by rule 2 above: the socket pair is its only allowed delta.)
    for key in sorted((on_keys & prod_keys) - {"args"} - allowed_on):
        if key == "env":
            if not _env_matches_prod(prod, on, tuned):
                fails.append(f"field 'env' changed between production and the candidate: "
                             f"{prod.get('env')!r} -> {on.get('env')!r}")
        elif prod[key] != on[key]:
            fails.append(f"field '{key}' changed between production and the candidate: {prod[key]!r} -> {on[key]!r}")
    # 3b. The SAME pinning on OFF (ab1-parent-review finding 5): a mutated OFF env/sampling/vision
    # value must fail even when ON is clean.  OFF's only extra allowed diff is its model label,
    # which rule 3 pins to model_name_off; OFF has no socket key at all.
    off_allowed = set(ALLOWED_SUBSTITUTIONS) - {"expert_helper_socket"}
    for key in sorted((off_keys & prod_keys) - {"args"} - off_allowed):
        if key == "env":
            if not _env_matches_prod(prod, off, tuned):
                fails.append(f"OFF field 'env' changed between production and the candidate: "
                             f"{prod.get('env')!r} -> {off.get('env')!r}")
        elif prod[key] != off[key]:
            fails.append(f"OFF field '{key}' changed between production and the candidate: "
                         f"{prod[key]!r} -> {off[key]!r}")

    # 4. The settings run-1 dropped are named and pinned (a regression tripwire, not a summary note).
    for flag in REQUIRED_ARG_FLAGS:
        if flag not in on["args"]:
            fails.append(f"engine args missing production flag {flag} (run-1 dropped such flags)")
    for key in REQUIRED_TOP_KEYS:
        if key not in on:
            fails.append(f"config missing production key '{key}' (run-1 dropped such keys)")
    for key in REQUIRED_ENV_KEYS:
        if key not in on.get("env", {}):
            fails.append(f"config env missing production key '{key}' (run-1 dropped it)")

    # 5. The helper geometry/budget and the engine's per-request timeout ride BOTH arms.
    for key in ("expert_helper_slots", "expert_helper_timeout"):
        if prod.get(key) is None:
            continue
        if on.get(key) != prod[key] or off.get(key) != prod[key]:
            fails.append(f"'{key}' must stay identical to production on both arms")
    if prod.get("expert_helper_timeout") is not None and on.get("expert_helper_timeout") != 100:
        fails.append("the engine's 100 ms per-request helper timeout is not preserved")

    # 5b. run-2 refusal gate: a configured helper socket WITHOUT the 100 ms per-request timeout is
    #     the exact shape that killed run2 - server.py injects socket+timeout together, so a
    #     missing/foreign timeout key leaves the engine on its 20 ms default and the first cold
    #     window request closes the channel for the rest of the run.
    if on.get("expert_helper_socket"):
        if on.get("expert_helper_timeout") != 100:
            fails.append("ON configures a helper socket but expert_helper_timeout != 100 "
                         "(the engine would run its 20 ms default: the run-2 death)")

    # 6. Neither arm may refer to the production socket, and ON must use the candidate socket.
    if on.get("expert_helper_socket") != subs["socket"]:
        fails.append("ON's expert_helper_socket is not the candidate socket")
    if "expert_helper_socket" in off:
        fails.append("OFF still carries the top-level expert_helper_socket (injection not removed)")
    if subs.get("socket") == prod.get("expert_helper_socket"):
        fails.append("the candidate socket equals the production socket")
    # 6b. The tuned second channel rides the ON arm ONLY, never the production paths.
    if tuned:
        if on.get("expert_helper_socket2") != subs["socket2"]:
            fails.append("ON's expert_helper_socket2 is not the requested second-channel socket")
        if "expert_helper_socket2" in off:
            fails.append("OFF still carries expert_helper_socket2 (the tuned channel is ON-only)")
        if subs["socket2"] == subs["socket"]:
            fails.append("the second-channel socket equals the primary candidate socket")
        if subs.get("socket2") == prod.get("expert_helper_socket"):
            fails.append("the second-channel socket equals the production socket")
        if subs.get("socket2") == prod.get("expert_helper_socket2"):
            fails.append("the second-channel socket equals the production second channel "
                         "(an adopted production config carries one)")
    elif "expert_helper_socket2" in on or "expert_helper_socket2" in off:
        fails.append("expert_helper_socket2 present without the explicit tuned-window request "
                     "(derive must be called with --socket2 for it)")
    for label, cfg in (("ON", on), ("OFF", off)):
        blob = json.dumps(cfg)
        for prod_sock in (prod.get("expert_helper_socket"), prod.get("expert_helper_socket2")):
            if isinstance(prod_sock, str) and prod_sock and prod_sock in blob:
                fails.append(f"{label} config references the production socket {prod_sock}")
    return fails


# ---------------------------------------------------------------------------------------------
# Serve-line parsing and probe association
# ---------------------------------------------------------------------------------------------

SERVE_RE = re.compile(
    r"^strata serve: prompt (?P<prompt>\d+) tokens = (?P<reused>\d+) reused \+ "
    r"(?P<read>\d+)(?: of (?P<read_total>\d+))? read in (?P<read_ms>[\d.]+) ms "
    r"\((?P<read_rate>[\d.]+) tok/s\), (?P<generated>\d+) generated in (?P<gen_ms>[\d.]+) ms "
    r"\((?P<gen_rate>[\d.]+) tok/s\), drafts accepted (?P<accepted>\d+) of (?P<offered>\d+), "
    r"(?P<checkpoints>\d+) checkpoints(?P<tail>.*)$"
)
HELPER_TAIL_RE = re.compile(
    r"helper=reqs=(?P<reqs>\d+) rows=(?P<rows>\d+) bytes=(?P<bytes>\d+) "
    r"timeouts=(?P<timeouts>\d+) fallbacks=(?P<fallbacks>\d+)"
)
# The engine's prompt-tier channel-2 close line (remote_experts.cpp close(), printed at engine
# teardown): the ONLY per-pair proof the tuned arm actually USED the second channel.
PROMPT_TIER_CLOSE_RE = re.compile(
    r"the prompt tier's channel \((?P<path>\S+)\) closed after (?P<reqs>\d+) prompt requests / "
    r"(?P<rows>\d+) prompt rows"
)


def parse_serve_line(line: str) -> dict | None:
    """One 'strata serve: prompt ...' line -> its numbers, GENERATED rate kept apart from the
    PROMPT rate.  Returns None for any other line."""
    m = SERVE_RE.match(line.strip())
    if not m:
        return None
    s = {k: (int(v) if v is not None and re.fullmatch(r"\d+", v) else v)
         for k, v in m.groupdict().items() if k != "tail"}
    for k in ("read_ms", "read_rate", "gen_ms", "gen_rate"):
        s[k] = float(s[k])
    t = HELPER_TAIL_RE.search(m.group("tail") or "")
    if t:
        s.update({f"helper_{k}": int(v) for k, v in t.groupdict().items()})
    else:
        s["helper_reqs"] = None      # the shipped default path: no helper counters at all
    s["cancelled"] = s.get("read_total") is not None
    return s


FILL_RE = re.compile(r"expert-helper: filled (\d+) slots \(([\d.]+) GiB\)")
CONNECTED_RE = re.compile(r"expert-helper: engine connected")
LISTENING_RE = re.compile(r"expert-helper: listening on (\S+)")
TEARDOWN_RES = (
    re.compile(r"SIGTERM/SIGINT .*exiting"),
    re.compile(r"the engine said bye; exiting"),
    re.compile(r"--once: .*exiting"),
)
DEATH_RES = (
    re.compile(r"the engine idled past the timeout; exiting"),
    re.compile(r"channel: .*; exiting"),
    re.compile(r"releasing the card"),
)


def helper_events(lines: list[str], socket: str) -> dict:
    """Classify one helper log segment: did it listen on the candidate socket, connect, fill,
    and did it die UNEXPECTEDLY (the run-1 XL defect) or by intentional teardown?"""
    ev = {"listen": [], "connected": False, "filled_slots": 0, "filled_gib": 0.0,
          "windows_served": [], "teardown": [], "death": [], "other": []}
    for line in lines:
        m = LISTENING_RE.search(line)
        if m:
            ev["listen"].append(m.group(1))
            continue
        if CONNECTED_RE.search(line):
            ev["connected"] = True
            continue
        m = FILL_RE.search(line)
        if m:
            ev["filled_slots"] = int(m.group(1))
            ev["filled_gib"] = float(m.group(2))
            continue
        m = re.search(r"expert-helper: (\d+) windows served", line)
        if m:
            ev["windows_served"].append(int(m.group(1)))
            continue
        if any(r.search(line) for r in TEARDOWN_RES):
            ev["teardown"].append(line.strip())
            continue
        if any(r.search(line) for r in DEATH_RES):
            ev["death"].append(line.strip())
            continue
    ev["socket_is_candidate"] = socket in ev["listen"]
    return ev


# ---------------------------------------------------------------------------------------------
# Validity gates and the verdict
# ---------------------------------------------------------------------------------------------

EXPECTED_ARMS = ("on", "off")


def analyze(records: list[dict], engine_lines: list[str], helper_lines: list[str],
           shas: dict[str, dict[str, str]], sizes: list[str], reps: int,
           socket: str, helper_required: str = "on", generated_min: int = 8,
           socket2: str | None = None) -> dict:
    """Turn probe records + log ranges into samples, apply every gate, and compute the medians.

    `records` carries TWO shapes from the recorder: per-PROBE records (label/arm/size/rep,
    engine_start/engine_end, the probe's OWN helper_start/helper_end, helper_alive_pre/post,
    req_sha, http) and PAIR records ({"type":"pair", startup range written after the pair's
    listen/connect/fill; {"type":"pair_end", the range end after its teardown}).  Startup
    evidence is gated on the PAIR's ranges, lifetime evidence on each probe's own range.
    `shas` maps label -> {"req":.., "content":.., "finish":.., "ct":..}.
    Returns {"samples": [...], "failures": [...], "medians": {...}, "verdict": str}.
    """
    samples: list[dict] = []
    fails: list[str] = []

    def fail(label: str, why: str) -> None:
        fails.append(f"{label}: {why}")

    # pair provenance (ab1-parent-review finding 1): startup (listen/connect/fill) belongs to the
    # PAIR's startup range; service/death belongs to the probe ranges; OFF's WHOLE pair range
    # (startup..pair_end, including teardown) must show no fill and no service at all.
    pairs: dict[str, dict] = {}
    for rec in records:
        rtype = rec.get("type")
        if rtype == "pair":
            pairs[rec["pair"]] = {"arm": rec.get("arm"), "rep": rec.get("rep"),
                                  "helper_start": int(rec["helper_start"]),
                                  "helper_startup_end": int(rec["helper_startup_end"]),
                                  "helper_end": None,
                                  "engine_start": (int(rec["engine_start"])
                                                   if rec.get("engine_start") is not None else None),
                                  "engine_end": None}
        elif rtype == "pair_end":
            if rec["pair"] in pairs:
                pairs[rec["pair"]]["helper_end"] = int(rec["helper_end"])
                if rec.get("engine_end") is not None:
                    pairs[rec["pair"]]["engine_end"] = int(rec["engine_end"])
            else:
                fails.append(f"{rec['pair']}: pair_end record without a pair startup record")

    for rec in records:
        if rec.get("type") is not None:
            continue   # pair provenance records carry no probe fields; they are gated separately
        label, arm, size, rep = rec["label"], rec["arm"], rec["size"], int(rec["rep"])
        base = {"label": label, "arm": arm, "size": size, "rep": rep}
        seg = engine_lines[int(rec["engine_start"]):int(rec["engine_end"])]
        parsed = [(i + int(rec["engine_start"]) + 1, p) for i, l in enumerate(seg)
                  if (p := parse_serve_line(l)) is not None]
        if not parsed:
            fail(label, "no serve line in its log range (MISSING sample)")
            samples.append(base)
            continue
        if len(parsed) > 1:
            fail(label, f"{len(parsed)} serve lines in its log range (DUPLICATE/AMBIGUOUS sample)")
            samples.append(base)
            continue
        line_no, s = parsed[0]
        sample = {**base, "line": line_no, **s}

        # request-level health recorded by the harness
        pre, post = rec.get("helper_alive_pre"), rec.get("helper_alive_post")
        hseg = helper_lines[int(rec.get("helper_start", 0)):int(rec.get("helper_end", 0))]
        ev = helper_events(hseg, socket)
        sample["helper_listen_socket"] = ev["socket_is_candidate"]
        sample["helper_connected"] = ev["connected"]
        sample["helper_filled_slots"] = ev["filled_slots"]
        sample["helper_death"] = ev["death"]

        if str(rec.get("http")) != "200":
            fail(label, f"HTTP {rec.get('http')} (the probe did not complete)")
        if s["cancelled"]:
            fail(label, "partial/cancelled prompt read - not a sizing sample")
        if s["reused"]:
            fail(label, f"prompt cache reused {s['reused']} tokens - measures reuse, not the arms")

        # generated-work gate (ab1-parent-review finding 4, REWORKED after run4): the gate
        # exists to catch a TRUNCATED reply (engine stopped early, partial read), NOT a
        # legitimate early model finish - with the nonce prompts the model EOSes at 16-19 of
        # the 32 budget on every size, which run4's ==32 gate wrongly failed 46 times.  A
        # sample is valid when the reply FINISHED cleanly (stop|length; anything else is a
        # transport/API error), it carries at least the minimum work to be a timing sample,
        # and the reply's completion count AGREES with the engine's own serve line.
        if s["generated"] < generated_min:
            fail(label, f"generated {s['generated']} < {generated_min} tokens - truncated/empty sample")
        row = shas.get(label, {})
        if row.get("finish") not in ("stop", "length"):
            fail(label, f"finish_reason={row.get('finish')!r} - the reply did not finish cleanly")
        # completion_tokens is recorded and cross-checked ONE-SIDED.  This stack's reply counter
        # legitimately UNDER-counts the engine's serve-line count (tuned run ab2-r1: every reply
        # reported 16 while the engine served 16-19 with spec-decode; an equality gate refused
        # 13 clean samples), but a reply claiming MORE tokens than the engine served is a real
        # reply/engine disagreement - the run-1 truncation shape.
        try:
            ct_val = int(row.get("ct", "0"))
        except (TypeError, ValueError):
            ct_val = -1
        if ct_val <= 0:
            fail(label, f"completion_tokens={row.get('ct')!r} - malformed usage (transport error)")
        elif ct_val > s["generated"]:
            fail(label, f"completion_tokens={ct_val} > engine generated {s['generated']} "
                        f"(reply/engine disagreement)")

        if arm in EXPECTED_ARMS:
            if arm == helper_required:
                if pre is False:
                    fail(label, "helper process dead BEFORE the probe (lifetime loss)")
                if post is False:
                    fail(label, "helper process dead DURING/AFTER the probe (lifetime loss)")
                if ev["death"]:
                    fail(label, f"helper exited unexpectedly mid-probe: {ev['death'][0]}")
                if not s["helper_reqs"]:
                    fail(label, "helper=reqs=0 - no helper corroboration for this ON sample")
            else:
                if s["helper_reqs"]:
                    fail(label, f"OFF arm shows helper service (helper=reqs={s['helper_reqs']})")
        samples.append(sample)

    # pair-level gates: every (arm, rep) that ran probes must carry a pair startup record; ON
    # startup must show listen-on-candidate + engine connection + fill; OFF must show NO fill and
    # NO service anywhere in its whole pair range, teardown included.
    probe_pairs: set[str] = set()
    for rec in records:
        if rec.get("type") is None:
            probe_pairs.add(f"{rec['arm']}-r{int(rec['rep'])}")
    for pid_ in sorted(probe_pairs - set(pairs)):
        fails.append(f"{pid_}: probes recorded but NO pair startup record (startup evidence lost)")
    for pid_, p in sorted(pairs.items()):
        startup = helper_events(helper_lines[p["helper_start"]:p["helper_startup_end"]], socket)
        end = p["helper_end"] if p["helper_end"] is not None else p["helper_startup_end"]
        full = helper_events(helper_lines[p["helper_start"]:end], socket)
        if p["arm"] == helper_required:
            if not startup["socket_is_candidate"]:
                fails.append(f"{pid_}: pair startup never listened on the candidate socket {socket}")
            if not startup["connected"]:
                fails.append(f"{pid_}: pair startup shows no engine connection")
            if startup["filled_slots"] <= 0:
                fails.append(f"{pid_}: pair startup never filled (no slots served by this pair)")
        else:
            if full["filled_slots"] > 0:
                fails.append(f"{pid_}: OFF arm's helper filled {full['filled_slots']} slots "
                             f"(whole pair range, it must never fill)")
            served = [w for w in full["windows_served"] if w > 0]
            if served:
                fails.append(f"{pid_}: OFF pair's helper served windows {served} "
                             f"(whole pair range incl. teardown)")
        # channel-2 (tuned window only): every ON pair's ENGINE range must show the prompt-tier
        # close line on the requested path with requests > 0 (remote_experts.cpp prints it at
        # engine teardown, which is why the pair records carry engine ranges); an OFF pair must
        # show no prompt-tier line at all.
        if socket2:
            if p.get("engine_start") is None or p.get("engine_end") is None:
                fails.append(f"{pid_}: pair record lacks engine ranges - channel-2 "
                             f"participation cannot be proven")
            else:
                tier = [m for m in (PROMPT_TIER_CLOSE_RE.search(l)
                                    for l in engine_lines[p["engine_start"]:p["engine_end"]]) if m]
                if p["arm"] == helper_required:
                    if not any(m.group("path") == socket2 and int(m.group("reqs")) > 0 for m in tier):
                        fails.append(f"{pid_}: no channel-2 prompt-tier participation on {socket2} "
                                     f"- the tuned arm is invalid without it")
                elif tier:
                    fails.append(f"{pid_}: OFF pair shows a prompt-tier close line "
                                 f"({tier[0].group('path')})")
        if p["helper_end"] is None:
            fails.append(f"{pid_}: no pair_end record (teardown evidence missing)")

    # the full grid, exactly once
    seen: dict[tuple[str, str, int], int] = {}
    for rec in records:
        if rec.get("type") is not None:
            continue   # pair records are not probes; the grid counts probes only
        key = (rec["arm"], rec["size"], int(rec["rep"]))
        seen[key] = seen.get(key, 0) + 1
    for arm in EXPECTED_ARMS:
        for size in sizes:
            for r in range(1, reps + 1):
                n = seen.get((arm, size, r), 0)
                if n == 0:
                    fails.append(f"{arm}-{size}-r{r}: MISSING probe record")
                elif n > 1:
                    fails.append(f"{arm}-{size}-r{r}: DUPLICATE probe records ({n})")

    # identical prompt bytes: the same size must tokenize to the same count in every sample
    counts: dict[str, list[tuple[str, int]]] = {}
    for s in samples:
        if "prompt" in s:
            counts.setdefault(s["size"], []).append((s["label"], s["prompt"]))
    for size, got in sorted(counts.items()):
        distinct = sorted({c for _, c in got})
        if len(distinct) > 1:
            fails.append(f"{size}: prompt token counts differ across samples {distinct} - "
                         f"AMBIGUOUS pairing, the request bytes are not the same")

    # cross-arm request bytes must match per rep+size; content hashes are recorded, never gated
    for size in sizes:
        for r in range(1, reps + 1):
            on_sha = shas.get(f"on-{size}-r{r}", {}).get("req")
            off_sha = shas.get(f"off-{size}-r{r}", {}).get("req")
            if not on_sha or not off_sha:
                fails.append(f"{size}-r{r}: request SHA missing for one arm (no byte-identity proof)")
            elif on_sha != off_sha:
                fails.append(f"{size}-r{r}: cross-arm request bytes differ ({on_sha} vs {off_sha})")

    medians: dict[str, dict[str, float | None]] = {}
    for arm in EXPECTED_ARMS:
        medians[arm] = {}
        for size in sizes:
            vals = [s["gen_rate"] for s in samples
                    if s["arm"] == arm and s["size"] == size and "gen_rate" in s]
            medians[arm][size] = statistics.median(vals) if len(vals) == reps else None

    out = {"samples": samples, "failures": fails, "medians": medians,
           "verdict": "NO VERDICT", "rel": None}
    if fails:
        out["verdict"] = "NO VERDICT (invalid samples)"
        return out
    on_m, off_m = medians["on"].get("long"), medians["off"].get("long")
    if not on_m or not off_m:
        fails.append("long-arm medians unavailable")
        out["verdict"] = "NO VERDICT (invalid samples)"
        return out
    rel = on_m / off_m
    out["rel"] = rel
    out["verdict"] = "PASS" if rel >= 1.05 else "FAIL"
    return out


# ---------------------------------------------------------------------------------------------
# Request bodies and hashes
# ---------------------------------------------------------------------------------------------

def build_request(out_path: str, prompt_file: str, max_tokens: int, model: str) -> dict:
    """Write the EXACT request body (prompt by FILE: the ~330 KB xl prompt exceeds the 128 KiB
    per-argv limit) and return it."""
    prompt = Path(prompt_file).read_text(encoding="utf-8")
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": int(max_tokens), "temperature": 0}
    Path(out_path).write_text(json.dumps(body), encoding="utf-8")
    return body


def sha16(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def record_hashes(label: str, reply_path: str, req_path: str) -> dict[str, str]:
    reply = json.loads(Path(reply_path).read_text(encoding="utf-8"))
    choice = reply["choices"][0]
    msg = choice["message"]
    content = msg.get("content") or ""
    return {
        "label": label,
        "req": sha16(req_path),
        "content": hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
        "message": hashlib.sha256(json.dumps(msg, sort_keys=True).encode("utf-8")).hexdigest()[:16],
        "finish": str(choice.get("finish_reason")),
        "ct": str(reply.get("usage", {}).get("completion_tokens")),
    }


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="helper_ab.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="mode", required=True)

    d = sub.add_parser("derive", help="derive both arm configs + the substitution manifest")
    d.add_argument("--prod-config", required=True)
    d.add_argument("--launcher", required=True)
    d.add_argument("--out-dir", required=True)
    d.add_argument("--exe", required=True)
    d.add_argument("--helper", required=True)
    d.add_argument("--socket", required=True)
    d.add_argument("--socket2", default=None,
                   help="tuned window: the second channel path (ON arm only, recorded in the manifest)")
    d.add_argument("--fill-mib", type=int, default=0,
                   help="tuned window: pin the helper's uploaded expert bytes via --fill-mib (0: absent)")
    d.add_argument("--family", default="ab1",
                   help="artifact family prefix for the config/manifest filenames (ab1|ab2)")
    d.add_argument("--port", required=True, type=int)
    d.add_argument("--cwd", required=True)
    d.add_argument("--log", required=True)
    d.add_argument("--model-on", default="ab1-on")
    d.add_argument("--model-off", default="ab1-off")
    d.add_argument("--fingerprint", action="append", default=[])
    d.add_argument("--fail-on-invalid", action="store_true")

    h = sub.add_parser("helper-opts", help="print the canonical helper flags, verified vs the launcher")
    h.add_argument("--launcher", required=True)
    h.add_argument("--socket", required=True)
    h.add_argument("--helper", required=True)
    h.add_argument("--socket2", default=None,
                   help="tuned window: append --socket2 (never without an explicit request)")
    h.add_argument("--fill-mib", type=int, default=0,
                   help="tuned window: append --fill-mib N (0: absent)")

    r = sub.add_parser("request", help="write one exact request body")
    r.add_argument("out")
    r.add_argument("prompt_file")
    r.add_argument("max_tokens", type=int)
    r.add_argument("--model", default="ab1-candidate")

    s = sub.add_parser("sha", help="sha256(16) of a file")
    s.add_argument("path")

    hr = sub.add_parser("hashreply", help="record one probe's hashes")
    hr.add_argument("label")
    hr.add_argument("reply")
    hr.add_argument("req")

    a = sub.add_parser("analyze", help="request-aware analysis + validity gates")
    a.add_argument("--records", required=True)
    a.add_argument("--engine-log", required=True)
    a.add_argument("--helper-log", required=True)
    a.add_argument("--shas", required=True)
    a.add_argument("--sizes", default="short,med,long,xl")
    a.add_argument("--reps", type=int, default=3)
    a.add_argument("--socket", required=True)
    a.add_argument("--generated-min", type=int, default=8,
                   help="minimum generated tokens a valid sample must carry (run4: the model "
                        "EOSes at 16-19 of the 32 budget; the old ==32 gate failed 46 clean samples)")
    a.add_argument("--socket2", default=None,
                   help="the tuned arm's second channel path: when given, every ON pair's engine "
                        "range must show the prompt-tier close line with requests > 0")
    a.add_argument("--out", required=True)
    a.add_argument("--summary", required=True)

    args = ap.parse_args(argv)

    if args.mode == "derive":
        prod = json.loads(Path(args.prod_config).read_text(encoding="utf-8"))
        launcher_text = Path(args.launcher).read_text(encoding="utf-8")
        got = launcher_opts(launcher_text)
        launcher_ok = all(got.get(k) == v for k, v in CANONICAL_HELPER_OPTS.items())
        subs = {"exe": args.exe, "log": args.log, "port": args.port, "cwd": args.cwd,
                "model_name": args.model_on, "model_name_off": args.model_off, "socket": args.socket,
                "socket2": args.socket2}
        on, off = derive_arms(prod, subs)
        fails = check_arm_pair(prod, on, off, subs)
        if not launcher_ok:
            fails.append(f"launcher helper options {got} do not match the canonical set {CANONICAL_HELPER_OPTS}")
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{args.family}-config-on.json").write_text(json.dumps(on, indent=1), encoding="utf-8")
        (out_dir / f"{args.family}-config-off.json").write_text(json.dumps(off, indent=1), encoding="utf-8")
        manifest = {
            "produced_by": "tools/mixed_gpu/helper_ab.py derive",
            "rule": "the ONLY arm delta is the helper socket; every other field is production's",
            "allowed_substitutions": ALLOWED_SUBSTITUTIONS,
            "substitutions": {k: v for k, v in subs.items()},
            "helper_opts_from_launcher": got,
            "canonical_helper_opts": CANONICAL_HELPER_OPTS,
            "helper_cmd": helper_cmd(args.helper, args.socket, socket2=args.socket2,
                                     fill_mib=args.fill_mib),
            "tuned": ({"socket2": args.socket2, "fill_mib": (args.fill_mib or None),
                       "prefill_env": "STRATA_PREFILL_HELPER=1 on BOTH arms (config env)"}
                      if (args.socket2 or args.fill_mib) else None),
            "fingerprints": [fingerprint(p) for p in [args.prod_config, args.launcher, args.exe,
                                                       args.helper, *args.fingerprint]],
            "parity_failures": fails,
            "arms": {"on": {"config": str(out_dir / f"{args.family}-config-on.json"),
                            "socket": args.socket, "socket2": args.socket2, "model": args.model_on,
                            "top_level_socket": True},
                     "off": {"config": str(out_dir / f"{args.family}-config-off.json"),
                             "socket": None, "socket2": None, "model": args.model_off,
                             "top_level_socket": False}},
        }
        (out_dir / f"{args.family}-manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        print(f"derive: on args={len(on['args'])} off args={len(off['args'])} "
              f"parity_failures={len(fails)}")
        for f in fails:
            print(f"  FAIL {f}")
        if fails and args.fail_on_invalid:
            return 1
        return 0

    if args.mode == "helper-opts":
        launcher_text = Path(args.launcher).read_text(encoding="utf-8")
        got = launcher_opts(launcher_text)
        bad = {k: (v, got.get(k)) for k, v in CANONICAL_HELPER_OPTS.items() if got.get(k) != v}
        if bad:
            print(f"helper-opts: launcher says {bad} - canonical set refused", file=sys.stderr)
            return 1
        for line in helper_cmd(args.helper, args.socket, socket2=args.socket2,
                               fill_mib=args.fill_mib):
            print(line)
        return 0

    if args.mode == "request":
        build_request(args.out, args.prompt_file, args.max_tokens, args.model)
        return 0

    if args.mode == "sha":
        print(sha16(args.path))
        return 0

    if args.mode == "hashreply":
        h = record_hashes(args.label, args.reply, args.req)
        print(f"{h['label']} req={h['req']} content={h['content']} message={h['message']} "
              f"finish={h['finish']} ct={h['ct']}")
        return 0

    if args.mode == "analyze":
        records = [json.loads(l) for l in Path(args.records).read_text(encoding="utf-8").splitlines() if l.strip()]
        engine_lines = Path(args.engine_log).read_text(encoding="utf-8", errors="replace").splitlines()
        helper_lines = Path(args.helper_log).read_text(encoding="utf-8", errors="replace").splitlines()
        shas: dict[str, dict[str, str]] = {}
        for line in Path(args.shas).read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2 and any(p.startswith("req=") for p in parts):
                row = {p.split("=")[0]: p.split("=", 1)[1] for p in parts[1:]}
                shas[parts[0]] = row
        sizes = args.sizes.split(",")
        res = analyze(records, engine_lines, helper_lines, shas, sizes, args.reps, args.socket,
                      generated_min=args.generated_min, socket2=args.socket2)
        Path(args.out).write_text(json.dumps(res, indent=1), encoding="utf-8")
        on_long, off_long = res["medians"]["on"].get("long"), res["medians"]["off"].get("long")
        head = f"verdict: {res['verdict']}"
        if res.get("rel"):
            head += (f" (long-arm generated median on {on_long} / off {off_long} "
                     f"= {res['rel']:.4f})")
        lines = [f"=== AB1 analysis ({time.strftime('%Y-%m-%d %H:%M:%S')}) ===", head]
        lines.append(f"failures: {len(res['failures'])}")
        lines += [f"  FAIL {f}" for f in res["failures"]]
        lines.append("")
        lines.append("generated tok/s medians (the VERDICT metric; the prompt tok/s is recorded per sample)")
        for arm in EXPECTED_ARMS:
            row = ", ".join(f"{sz}={res['medians'][arm].get(sz)}" for sz in sizes)
            lines.append(f"  {arm}: {row}")
        lines.append("")
        lines.append("samples (prompt tokens = reused + read, generated rate, helper reqs)")
        for smp in res["samples"]:
            if "gen_rate" in smp:
                lines.append(f"  {smp['label']}: prompt={smp['prompt']} reused={smp['reused']} "
                             f"prompt_tok_s={smp['read_rate']} generated={smp['generated']} "
                             f"gen_tok_s={smp['gen_rate']} helper_reqs={smp.get('helper_reqs')} "
                             f"fallbacks={smp.get('helper_fallbacks')}")
            else:
                lines.append(f"  {smp['label']}: NO SAMPLE")
        Path(args.summary).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines))
        return 0

    return 0


def helper_cmd(helper_path: str, socket: str, socket2: str | None = None,
               fill_mib: int = 0) -> list[str]:
    """The canonical helper control-process command for BOTH arms (identical flags; OFF's
    engine simply never connects, so it never fills and never serves).

    The TUNED extras are appended ONLY when explicitly requested: fill_mib > 0 adds
    --fill-mib N (pin the uploaded expert bytes; helper_main.cpp's fill cap, stop reason
    'fill-mib'), socket2 adds --socket2 PATH (the prompt tier's own bound channel).  With
    neither, the command stays byte-identical to the canonical one."""
    o = CANONICAL_HELPER_OPTS
    cmd = [
        "env", "-i", f"PATH={os.environ.get('PATH', '/usr/bin:/bin')}",
        f"HOME={os.environ.get('HOME', '/root')}", "TERM=dumb",
        helper_path,
        "--pack", o["--pack"], "--experts", o["--experts"],
        "--device", o["--device"], "--slots", o["--slots"], "--reserve-mib", o["--reserve-mib"],
        "--timeout", o["--timeout"], "--begin-timeout", o["--begin-timeout"],
        "--socket", socket,
    ]
    if fill_mib and fill_mib > 0:
        cmd += ["--fill-mib", str(fill_mib)]
    if socket2:
        cmd += ["--socket2", socket2]
    return cmd


if __name__ == "__main__":
    sys.exit(main())
