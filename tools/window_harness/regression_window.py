#!/usr/bin/env python3
"""REGRESSION-WINDOW controller - PARENT-only operator automation (agents never run --execute).

ONE isolated 8019 DFlash width-3 run under the frozen correctness profile, driven through the
reviewed lifecycle helpers (ops/window.py = W, ops/model_window.py = mw) and the reviewed cloner
(tools/test_inco_dflash2_integration.py). This file adds NO parallel framework: it derives ONE
config, runs ONE prototype server, runs the EXISTING integration runner against it, and reuses
mw's start/evidence/stop and W's snapshot/stop/restore paths verbatim.

Default is DRY RUN: snapshot + config currency + derivation + source-support proof + plan.
ZERO signals, ZERO writes, ZERO GPU, ZERO HTTP.

--execute (parent only), in order:
 0. --phase integration (DEFAULT) | cancel-park | rewind. The last two run exactly ONE existing
    tool subprocess (tools/cancel_park_test.py, tools/dflash_cache.py) that spawns ITS OWN engine:
    NO prototype server is ever started there (no mw.start_mode call), port 8019 must be free and
    NO process with the prototype exe may be alive (fail closed, nothing signalled); the tool runs
    in its own process group and ITS LINEAGE IS OBSERVED WHILE IT RUNS (bounded intervals through
    short wait timeouts - no watcher thread), so an engine that appears after the launch, the three
    sequential engines of the rewind tool, and any delayed child are pinned by pid + start ticks
    with their exe, full argv, cwd and device/library environment proof captured while alive. The
    timeout is recorded as exit 124 and ONLY the tool pid and OUR observed child pids are ever
    signalled (each verified by pid + start ticks); a same-exe process we never observed is a
    restore REFUSAL, never an ownership claim, and is never signalled. The child environment
    carries the live original engine's device/library flags (window.py's '_env_full'); the parent's
    os.environ is never mutated. Success REQUIRES at least one observed prototype engine whose argv
    carries the config's --layer-split value (the tools put it there with the server's own rule,
    serve/server.py:2871 - a single-card 262144 run is not an acceptable substitute) plus every
    marker in the tool's own output plus no owned survivor; an empty engines[] plus a PASS
    substring never passes. The tool's forced flags (dflash_cache forces context 4096 / spec 2 /
    block 1 / fixed caches / --spec-split, which stays explicitly narrower coverage WITH the split
    kept) are recorded verbatim, so a phase is never labeled as 262144 width-3 coverage.
 1. Fresh W.snapshot() of the ORIGINAL 8009 production proxy; an unverified snapshot REFUSES.
 2. W.stop_prod() - the SAME verified SIGTERM (proxy pid + start ticks + argv/cwd + config sha
    re-checked immediately before the signal) and the whole-family exit gate. Nothing runs while
    a verified original descendant lives. Production absence is NEVER waived: the integration
    runner's own lineage guard runs inside its process and must report isolated-8019/absent.
 3. mw.currency(MODE) proves the reviewed dflash_w3 clone is CURRENT against the live production
    config; the derived config = that clone + ONLY the ALLOW-list control diffs (below), with a
    UNIQUE per-window config name (written 0600), and a UNIQUE per-window engine log. Every other
    structured field (vision, layer_split, tokenizer, env, sampling, lib_dirs, reasoning, ...) must
    equal the clone exactly, and the derived config may not add keys the clone lacks. The original
    config, model and binaries are never edited.
 4. mw.start_mode() starts ONLY the prototype serve/server.py, from the PRODUCTION venv python,
    with the derived config on port 8019 (its own process group); startup is verified by /health,
    the exact /props model path, the engine child's exe (readlink /proc/PID/exe) and its cmdline
    carrying --serve, --max-context 262144, --native <model>, --layer-split and EVERY derived flag.
 5. The EXISTING tools/test_inco_dflash2_integration.py runs with --execute --url
    http://127.0.0.1:8019 --identity-config <derived> --confirm-execution, under a bounded
    timeout, stdout+stderr captured to a per-window log, genuine exit code (124 = timeout).
 6. Evidence. The runner's structured results (its own --results-json file, full usage + text per
    case) supply the reuse/reset proof: repeat-2 cached_tokens > 0 and the unrelated reset-new
    cached_tokens == 0 (serve/server.py:5335, the engine's reused counter), with all four requests
    COMPLETED and the cold repeat-1 never used as the reset witness. The sampled fallback is
    proven from THIS session's engine-log bytes only (mw.draft_evidence pins the byte offset +
    inode before the launch; an inode change/shrink REFUSES the evidence): EXACTLY ONE line
    `dflash: sampled request uses target-only decoding` (generate.cpp:12499, printed to stderr and
    routed to the config's log by serve/server.py:998 `stderr=self.log`) and it must sit AFTER the
    three greedy prompt lines (generate.cpp:15775, same routing). mw.evaluate's activation gate
    (geometry, collector, VRAM line, drafts offered, watchdog lines, target_only purity) is used
    UNCHANGED; only that single, correctly positioned sampled fallback is exempted from its
    fallback failure, and an extra fallback line or a greedy-phase fallback stays a failure.
    Missing proof is a FAILURE, never a pass.
 7. mw.stop_mode() stops the whole owned prototype family (SIGTERM to ITS proxy pid only, then
    port + recorded pids + no prototype exe), mw.proto_survivors() refuses any restore with a
    survivor alive, and W.restore_and_verify() restores the ORIGINAL launcher (never a duplicate)
    and verifies /health + model + config sha + engine cmdline. SIGTERM/SIGINT take the SAME
    cleanup path (mw.WindowSignal).

NOT run here (documented parent-only follow-up, see L22-REGRESSION-WINDOW-PLAN.md):
tools/cancel_park_test.py and tools/dflash_cache.py SPAWN THEIR OWN ENGINES (serve.server.
StrataEngine / dflash_sampling.Engine), so they must never run beside the 8019 server: exact
sequencing under the same original-down controller is given in the plan, phase ONLY.

Safety: no global kill anywhere; nothing OpenCode-related is ever matched; secret-looking env
values masked in every printed/persisted output; results JSON 0600; unique per-window labels so
reruns never overwrite evidence; the production config/model/binaries are never written.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time

OPS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(OPS_DIR))
import window as W                                       # reviewed snapshot/stop/restore
import model_window as mw                                # reviewed currency/start/evidence/stop

sys.path.insert(0, f"{mw.CLONE}/tools")
from test_inco_dflash2_integration import load_prod_config, clone_server_config   # reviewed cloner

MODE, WIDTH = "dflash_w3", 3
INTEGRATION = f"{mw.CLONE}/tools/test_inco_dflash2_integration.py"
INTEGRATION_TIMEOUT = 900                                # bounded; exit 124 records the genuine timeout
VALUELESS = {"--no-prefill-borrow"}                       # the source parse consumes no value (2579)

# STRICT derivation allowlist: the ONLY control diffs allowed on top of the reviewed dflash_w3
# clone. Every entry is source-cited for engine support at THIS commit (read-only scan below).
ALLOW: dict[str, tuple[str | None, str]] = {
    "--expert-cache": ("2000", "generate.cpp:2534 (o.expert_cache = next)"),
    "--prefill": ("512", "generate.cpp:2550 (o.prefill = next)"),
    "--no-prefill-borrow": (None, "generate.cpp:2579 (o.no_prefill_borrow = true)"),
    "--adapt-swaps": ("0", "generate.cpp:2712 (o.adapt_swaps = next)"),
    "--prompt-cache": ("4", "generate.cpp:2622 (o.prompt_cache = max(0, next))"),
    "--conversation-cache-mib": ("2048", "generate.cpp:2623-2637 (conversation-cache group)"),
    "--conversation-cache-slots": ("3", "generate.cpp:2623-2637 (conversation-cache group)"),
}
PROMPT_LINE = re.compile(r"prompt (\d+) tokens = (\d+) reused")      # the serve log line, server.py:441
SAMPLED_LINE = "sampled request uses target-only decoding"           # generate.cpp:12499 (stderr -> log)
CLI_SAMPLED_LINE = "sampled CLI requests use target-only decoding"   # generate.cpp:2831 (CLI only, NOT serve)
PROOFS = ["four runner cases COMPLETED (repeat-1, repeat-2, reset-new, sampled)",
          "repeat-2 cached_tokens > 0 and reset-new cached_tokens == 0 (serve/server.py:5335; the cold "
          "repeat-1 is never the witness)",
          "exactly ONE serve sampled fallback line AFTER the greedy prompt lines (generate.cpp:12499 -> "
          "stderr -> server.py:998 log) + mw.evaluate's activation gate with no extra fallback"]
NOT_RUN_HERE = ("cancel_park_test.py + dflash_cache.py spawn their OWN engines - see "
                "L22-REGRESSION-WINDOW-PLAN.md for the parent-only sequencing")


# ------------------------------------------------------------------ derivation (strict allowlist)
def derive(window: str, base: dict) -> tuple[dict, list[dict], list[str]]:
    """derived = the reviewed dflash_w3 clone + ONLY ALLOW's flags at ALLOW's values, with a
    unique per-window log. Returns (cfg, overrides, problems). Nothing outside ALLOW may move."""
    d = copy.deepcopy(base)
    args = list(d.get("args", []))
    overrides: list[dict] = []

    def apply(flag: str, to: str | None) -> None:
        blank = flag in VALUELESS
        if flag not in args:
            overrides.append({"flag": flag, "from": None, "to": None if blank else to, "action": "appended"})
            args.extend([flag] if blank else [flag, to])
            return
        i, cur = args.index(flag), "" if flag in VALUELESS else args[args.index(flag) + 1]
        action = "present" if blank else ("already" if cur == to else "replaced")
        overrides.append({"flag": flag, "from": cur, "to": cur if blank else to, "action": action})
        if not blank and cur != to:
            args[i + 1] = to

    for flag, (want, _cite) in ALLOW.items():
        apply(flag, want)
    d["args"] = args
    d["log"] = f"{W.LOGS}/engine-regwindow-{window}-{MODE}.log"      # unique: reruns never mix logs
    return d, overrides, check_derived(window, base, d)


def check_derived(window: str, base: dict, d: dict) -> list[str]:
    """Every diff between the reviewed clone and a derived config must be an ALLOW entry at the
    intended value; the exe/port/log must be the prototype's isolated, unique, absolute ones."""
    args, problems = list(d.get("args", [])), []
    b_f, d_f = mw.flags(base.get("args", [])), mw.flags(args)
    b_d, d_d = dict(b_f), dict(d_f)
    if len(b_d) != len(b_f) or len(d_d) != len(d_f):
        problems.append("a flag appears twice in the derived args")
    changed = {k: v for k, v in d_d.items() if k in b_d and v != b_d[k]}
    added = {k: v for k, v in d_d.items() if k not in b_d}
    removed = {k for k in b_d if k not in d_d}
    want_changed = {k: v for k, (v, _c) in ALLOW.items() if k in b_d and b_d[k] != v}
    want_added = {k: v for k, (v, _c) in ALLOW.items() if k not in b_d}
    if changed != want_changed:
        problems.append(f"changed {changed} != intended {want_changed}")
    if added != want_added:
        problems.append(f"added {added} != intended {want_added}")
    if removed:
        problems.append(f"flags removed from the clone: {sorted(removed)}")
    if [f for f, _ in d_f if f in b_d] != [f for f, _ in b_f]:
        problems.append("flag order differs from the clone (derivation appends, never reorders)")
    problems += [f"{flag} not at its intended value {want!r}" for flag, (want, _c) in ALLOW.items()
                 if d_d.get(flag, "" if want is None else None) not in (want, "" if want is None else None)]
    if d.get("exe") != mw.PROTO_BINARY:
        problems.append(f"derived exe {d.get('exe')!r} is not the prototype binary")
    if d.get("port") != mw.PROTO_PORT:
        problems.append(f"derived port {d.get('port')!r} is not the isolated {mw.PROTO_PORT}")
    if d.get("log") != f"{W.LOGS}/engine-regwindow-{window}-{MODE}.log":
        problems.append(f"derived log {d.get('log')!r} is not the unique window log")
    if not d["log"].startswith(f"{W.LOGS}/"):
        problems.append("derived log is not absolute under the dflash2 logs dir")
    for key in base:                                    # EVERY structured field must equal the clone
        if key in ("args", "log"):                      # args: structural check above; log: unique by design
            continue
        if d.get(key) != base[key]:
            problems.append(f"derived key {key!r} differs from the clone ({d.get(key)!r} != {base[key]!r})")
    for key in d:
        if key not in base:
            problems.append(f"derived config carries key {key!r} that the clone does not have")
    return problems


def source_support() -> tuple[dict, list[str]]:
    """Read-only proof that every allowlisted flag is PARSED by the engine at this commit."""
    src = pathlib.Path(f"{mw.CLONE}/src/program/generate.cpp")
    try:
        lines = src.read_text().splitlines()
    except OSError as e:
        return {}, [f"generate.cpp unreadable: {type(e).__name__}"]
    found: dict[str, int] = {}
    for flag, (_v, cite) in ALLOW.items():
        needle = f'a == "{flag}"'
        found[flag] = next((n for n, ln in enumerate(lines, 1) if needle in ln), -1)
    problems = [f"{flag}: no source parse found (citation {cite})" for flag, n in found.items() if n < 0]
    return {"flags_line": found, "source": str(src)}, problems


# ------------------------------------------------------------------ runtime proofs (HTTP usage + session log)
CITES = {"cached_tokens": "serve/server.py:5335 (usage.prompt_tokens_details.cached_tokens = the engine's "
                          "reused counter); the cold repeat-1 has cached 0 by nature and is never the witness",
         "routing": "generate.cpp:15775 + 12499 (stderr) -> serve/server.py:998 (stderr=self.log)"}


def usage_proof(cases: list[dict]) -> dict:
    """Source-backed reuse/reset proof from the RUNNER's structured per-case results: repeat-2
    cached_tokens > 0, the unrelated reset-new == 0, all four COMPLETED; a missing field is never
    defaulted to success."""
    by, why = {c.get("label"): c for c in cases}, []

    def cached(label: str) -> int | None:
        c = by.get(label)
        det = ((c or {}).get("usage") or {}).get("prompt_tokens_details") if c and c.get("ok") else None
        v = det.get("cached_tokens") if isinstance(det, dict) else None
        return v if isinstance(v, int) else None

    four = all(l in by and by[l].get("ok") for l in ("repeat-1", "repeat-2", "reset-new", "sampled"))
    v2, vz = cached("repeat-2"), cached("reset-new")
    if not four:
        why.append(f"not all four requests COMPLETED (labels/ok: {[(c.get('label'), c.get('ok')) for c in cases]})")
    if v2 is None:
        why.append("repeat-2 has no integer cached_tokens: repeat reuse NOT proven")
    elif v2 <= 0:
        why.append(f"repeat-2 cached_tokens {v2} is not > 0: repeat reuse NOT proven")
    if vz is None:
        why.append("reset-new has no integer cached_tokens: clean reset NOT proven")
    elif vz != 0:
        why.append(f"reset-new cached_tokens {vz} is not 0: the unrelated conversation did not start clean")
    return {"four_completed": four, "repeat_cached": v2, "reset_cached": vz, "ok": not why,
            "why": why, "citations": CITES}


def log_proof(text: str, n_greedy: int = 3) -> dict:
    """Session-log proof for the SAMPLED fallback ONLY (reuse/reset is the HTTP proof above).
    EXACTLY ONE fallback line (generate.cpp:12499) and it must sit AFTER the greedy requests'
    prompt lines (generate.cpp:15775, same stderr->log routing)."""
    prompts = [m.start() for m in PROMPT_LINE.finditer(text)]
    sampled_at = _offsets(text, SAMPLED_LINE)
    after = len(prompts) >= n_greedy and bool(sampled_at) and sampled_at[0] > prompts[n_greedy - 1]
    why = []
    if not sampled_at:
        why.append(f"no serve sampled fallback line ({SAMPLED_LINE}, generate.cpp:12499) in this session")
    if len(sampled_at) > 1:
        why.append(f"{len(sampled_at)} sampled fallback lines: a fallback beyond the sampled case is a failure")
    if sampled_at and not after:
        why.append("the sampled fallback line sits during the greedy requests: fallback during greedy is a failure")
    if len(prompts) < n_greedy:
        why.append(f"only {len(prompts)} greedy prompt lines in the log: the greedy cases are not all logged")
    return {"sampled_count": len(sampled_at), "sampled_after_greedy": after, "prompt_lines": len(prompts),
            "ok": len(sampled_at) == 1 and after, "why": why, "routing": CITES["routing"]}


def _offsets(text: str, needle: str) -> list[int]:
    out, i = [], text.find(needle)
    while i >= 0:
        out.append(i)
        i = text.find(needle, i + len(needle))
    return out


FALLBACK_FAIL = "silent fallback to target-only in the engine log = MODE FAILURE"   # mw.evaluate's text


def evaluate_regression(mode: str, rec: dict, proof: dict) -> list[str]:
    """mw.evaluate UNCHANGED (geometry, collector, VRAM line, drafts offered, watchdog lines,
    target_only purity), with ONE narrow exemption: the sampled case's EXPECTED fallback line is
    not a fallback failure when it is the ONLY fallback line AND sits after the greedy requests
    (log_proof). Extra fallback lines, a fallback during greedy, and every other failure stay."""
    bad = mw.evaluate(mode, rec)
    fb = [ln for ln in ((rec.get("evidence") or {}).get("fallback_lines") or []) if "target-only decoding" in ln]
    if FALLBACK_FAIL in bad and len(fb) == 1 and proof.get("ok") and proof.get("sampled_count") == 1 \
            and proof.get("sampled_after_greedy"):
        bad = [b for b in bad if b != FALLBACK_FAIL]
        rec["sampled_fallback_exempted"] = {"lines": fb, "cite": "generate.cpp:12499, the sampled case's "
                                           "explicit serve fallback; exempted ONLY as a single line "
                                           "after the greedy requests", "other_fallback_lines": 0}
    return bad


def write_derived_config(path: pathlib.Path, cfg: dict) -> None:
    """The derived config is unique per window and owner-only (it carries the engine's env block)."""
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    os.chmod(path, 0o600)


def identity_note(cfg: dict) -> dict:
    """What the runner's --identity-config check asserts for THIS config after the L22
    strengthening: the exact props.model_path from --native (the field clones carry), with no
    None-as-success, and the exact model_alias.  Source-read; no invented outcome."""
    args = cfg.get("args", [])
    return {"model_alias_want": cfg.get("model_name"),
            "pack_want": args[args.index("--native") + 1] if "--native" in args else
                         (args[args.index("--model") + 1] if "--model" in args else None),
            "pack_source_flag": "--native" if "--native" in args else ("--model" if "--model" in args else None),
            "no_none_as_success": True,
            "history": "the pre-L22 arm read '--model' only, which clones never carry, so want_pack was "
                       "None and the pack arm was inert - recorded as NOTAPPLICABLE, never as a pass"}


# ------------------------------------------------------------------ the integration run
def run_integration(cfg_path: pathlib.Path, window: str, rec: dict, note) -> bool:
    log, results = W.LOGS / f"{window}-integration.log", W.LOGS / f"{window}-integration-results.json"
    argv = [mw.VENV_PY, INTEGRATION, "--execute", "--url", f"http://127.0.0.1:{mw.PROTO_PORT}",
            "--identity-config", str(cfg_path), "--confirm-execution", "--results-json", str(results)]
    rec["integration_argv"] = argv
    t0, rc = time.time(), None
    try:
        with open(log, "wb") as fh:
            rc = subprocess.run(argv, cwd=mw.CLONE, stdout=fh, stderr=fh,
                                timeout=INTEGRATION_TIMEOUT, check=False).returncode
    except subprocess.TimeoutExpired:
        rc = 124
    try:
        cases = json.loads(results.read_text()).get("cases", [])
    except (OSError, ValueError) as e:
        cases, rec["integration_results_error"] = [], f"{type(e).__name__}: {results}"
    rec["integration"] = {"exit": rc, "seconds": round(time.time() - t0, 3), "log": str(log),
                          "results": str(results), "n_cases": len(cases), "tail": _tail(log),
                          "note": {0: "ok", 1: "checks failed", 2: "guard refused",
                                   124: "TIMEOUT"}.get(rc, "nonzero")}
    rec["cases"] = cases
    note(f"integration: exit {rc} in {rec['integration']['seconds']}s, {len(cases)} cases -> {results}")
    return rc == 0


def _tail(log: pathlib.Path, n: int = 12) -> list[str]:
    try:
        return [ln[:240] for ln in log.read_text(errors="replace").splitlines()[-n:]]
    except OSError as e:
        return [f"<log unreadable: {type(e).__name__}>"]


# ------------------------------------------------------------------ the window
def run_regression(snap: dict, record: dict, cfg: dict, cfg_path: pathlib.Path, note) -> None:
    if not W.stop_prod(snap, record, note):                       # step 2: the shared verified stop
        return
    env = os.environ.copy()
    env.update(snap["_env_full"])                                 # the live engine's OWN device flags
    rec: dict = {"mode": MODE, "width": WIDTH, "config": str(cfg_path), "requests": []}
    record["modes"].append(rec)
    try:
        if not mw.start_mode(record["window"], MODE, cfg, WIDTH, env, rec, note):
            rec["mode_ok"], rec["mode_failures"] = False, [rec.get("startup", "startup failed")]
        else:
            integration_ok = run_integration(cfg_path, record["window"], rec, note)
            ev_ok = mw.draft_evidence(record["window"], cfg, MODE, rec)
            session = _session(rec)
            lproof = log_proof(session)
            bad = evaluate_regression(MODE, rec, lproof) if ev_ok else ["engine log evidence unreadable"]
            if not integration_ok:
                bad.append(f"integration runner exit {rec['integration']['exit']} "
                           f"({rec['integration']['note']}) - not a pass")
            uproof = usage_proof(rec.get("cases") or [])
            rec["usage_proof"], rec["log_proof"] = uproof, lproof
            bad += [w for w in uproof["why"]] + [w for w in lproof["why"]]
            rec["mode_ok"], rec["mode_failures"] = not bad, bad
    except BaseException as e:                                     # interruption is recorded, cleanup runs
        rec["mode_error"], rec["mode_ok"] = repr(e)[:300], False
        rec["mode_failures"] = [f"mode interrupted: {rec['mode_error']}"]
    note(f"{MODE}: mode_ok={rec.get('mode_ok')} failures={rec.get('mode_failures')}")
    gone = mw.stop_mode(MODE, rec, note)                           # owned family stopped, no global kill
    rec["family_gone"] = gone
    if not rec.get("mode_ok") or not gone:
        record["break_reason"] = f"{MODE}: mode_ok={rec.get('mode_ok')} family_gone={gone} - restoring now"
        note(record["break_reason"])
        return
    record["all_modes_ok"] = True


def _session(rec: dict) -> str:
    """The per-window copy mw.draft_evidence wrote = this session's bytes only."""
    dst = (rec.get("engine_log") or {}).get("per_window_copy")
    try:
        return pathlib.Path(dst).read_text(errors="replace") if dst else ""
    except OSError:
        return ""


# ------------------------------------------------------------------ own-engine phases (parent-only)
# Both tools spawn THEIR OWN engine: tools/cancel_park_test.py uses serve.server.StrataEngine
# (argv [exe, "--serve", *args], engine stderr -> its log) and tools/dflash_cache.py uses
# dflash_sampling.Engine (argv [exe, *args, "--serve"], stderr -> --output/<arm>-engine.txt).
# Neither binds a port and NEITHER translates the config's structured keys, so the engine runs
# WITHOUT the server-derived --layer-split (serve/server.py:2890 adds it in the server path only).
# That is recorded as the tool shape and is NEVER reported as the 262144 / width-3 split coverage
# the integration phase has. Both tools self-close their engines (QUIT / kill paths), so our job is
# ownership: pin the tool pid and its engine children by pid + start ticks, signal ONLY those, and
# refuse the original restore while any of them lives.
TOOL = {"cancel-park": f"{mw.CLONE}/tools/cancel_park_test.py", "rewind": f"{mw.CLONE}/tools/dflash_cache.py"}
REWIND_FORCED = {   # dflash_cache.py:58-66 replaces EVERY one of these values, :75 appends --spec-split
    "--expert-cache": "2000 (tool default, :40-42 + :58)", "--adapt-every": "0 (:58)",
    "--adapt-swaps": "0 (:58, adaptation stays OFF)", "--pcie-frac": "0 (:59)", "--prefill": "256 (:59)",
    "--short-read": "4096 (:59)", "--max-context": "4096 (:60)", "--kv-resident": "4096 (:60)",
    "--dflash-window": "4096 (:61)", "--spec": "2 (:62)", "--dflash-block": "1 (:62)", "--spec-min-p": "0 (:62)",
    "--suffix-draft": "0 (:63)", "--lookup-chain": "0 (:63)", "--prompt-cache": "16 (:64)",
    "--prompt-cache-every": "16 (:64)", "--conversation-cache-mib": "256 (:65)",
    "--conversation-cache-slots": "4 (:65)", "--conversation-cache-min-tokens": "1 (:66)",
    "--conversation-cache-park-gain": "0 (:66)", "--spec-split": "appended (:75)",
    "--layer-split": "NOT in the tool's forced list - the L23 fix appends it with the server's own rule "
                     "(layer_split_of/layer_split_value) so the 2-GPU split SURVIVES the 4096 narrowing",
    "child env": "STRATA_IQ_MT_MIN=1, STRATA_PREFILL_CPU_SHARE=0, STRATA_SPEC_PROB=0, "
                 "STRATA_SPEC_COUPLED=0, STRATA_SPEC_GUMBEL=0 (:76-77)"}
PHASES = {
    "cancel-park": {"tool": TOOL["cancel-park"], "timeout": 2400,
                    # the scenario lines the tool itself prints (lines 71-90); PASS alone is NOT accepted
                    "markers": ("A2 (cancelled): restored/reused at RESUME", "finish cancel", "STOP sent True",
                                "A3: prompt", "PASS"),
                    "argv": lambda cfg, out: [TOOL["cancel-park"], str(cfg)],
                    "forced": {"STRATA_EXTRA_ARGS": "NOT needed - the derived config already carries "
                                 "--prompt-cache 4 --conversation-cache-mib 2048 "
                                 "--conversation-cache-slots 3 (tool docstring lines 10-11)",
                               "phase controls": "--conversation-cache-min-tokens 1 + "
                                 "--conversation-cache-park-gain 0 written into THIS phase's own config "
                                 "(PHASE_CONTROLS): the production 2048/4096 parked nothing, measured as "
                                 "the engine's own 'skip parking (1829 tokens, below the 2048 minimum; "
                                 "parked=0)'"},
                    "shape": "engine = the derived profile verbatim (--max-context 262144, --spec 4, "
                             "--dflash-block 3, --adapt-swaps 0, caches 4/2048/3) WITH the real two-GPU "
                             "--layer-split from the config's structured key (the tool's L23 fix, the "
                             "server's own rule) - a single-GPU 262144 run is NOT an acceptable substitute",
                    "notes": "engine log = SMOKE_LOG else the config's log (append); the tool closes its own "
                             "engine at its tail (stdin.close, wait(60), kill); StrataEngine.__init__ calls "
                             "end_leftover_engines(exe) (serve/server.py:103) which signals recorded leftovers "
                             "or orphans of EXACTLY this exe - disabled here by STRATA_KEEP_LEFTOVERS=1 so "
                             "nothing outside our ownership is ever signalled; the tool's own success is "
                             "fin2 == 'cancel' and reused3 >= len(A1)+len(outA)-3, and it now STOPS with "
                             "PRECONDITION FAIL / exit 3 when the engine parked nothing or restored nothing "
                             "(its own 'parked'/'restored'/'skip parking' lines), so a cancellation of a "
                             "never-parked conversation can never be reported as coverage"},
    "rewind": {"tool": TOOL["rewind"], "timeout": 5400,
               "markers": ("DFlash live continuation, checkpoint rewind and conversation parking: PASS",),
               "argv": lambda cfg, out: [TOOL["rewind"], "--config", str(cfg), "--engine", mw.PROTO_BINARY,
                                         "--output", str(out)],
               "forced": REWIND_FORCED,
               "shape": "tool-FORCED context 4096 / spec 2 / block 1 / prompt-cache 16 / conversation-cache "
                        "256 MiB + 4 slots / min-tokens 1 / park-gain 0 / prefill 256 / + --spec-split, "
                        "explicitly NARROWER coverage than the 262144 width-3 integration lane, and WITH the "
                        "real two-GPU --layer-split kept (the tool's L23 fix) - disk sessions UNSUPPORTED "
                        "(its header)",
               "notes": "three sequential engines (cached / fresh / target), each closed in its finally "
                        "(dflash_sampling.py:182-200: QUIT then kill); engine logs open with mode 'w' under "
                        "--output; the tool writes ONLY under --output; its own commands.json records the "
                        "argv it used, which the controller compares with the observed /proc argv; the "
                        "OPTIONAL expected physical slot pair (--expected-primary-slots / "
                        "--expected-total-slots, a PARENT FIXTURE input, rewind ONLY) is appended to THIS "
                        "argv BEFORE the launch and recorded in phase_expected_slots - the tool compares it "
                        "fail-closed and records its OBSERVED counts separately in "
                        "residency-expectations.json (an expected count is never read back from the engine)"},
}
PHASE_ENV = {"STRATA_REPO": mw.CLONE, "PYTHONPATH": f"{mw.CLONE}/tools", "STRATA_KEEP_LEFTOVERS": "1"}
# Phase-ONLY diagnostic controls.  Measured on the parent's cancel-park window (the engine's own
# lines): the production-inherited --conversation-cache-min-tokens 2048 made the engine print
# "conversation cache: skip parking (1829 tokens, below the 2048 minimum; parked=0)", so cancel-park
# tested a cancellation with NOTHING parked.  Source: the size gate is conversations.wants(live.size())
# (generate.cpp:9516-9526, the flag parses at 2639) and the re-park gate is
# park_worth with park_gain (generate.cpp:12879-12887, "0 (the default) parks every turn", parses at
# 2640).  min-tokens 1 + park-gain 0 are the same two test controls dflash_cache.py forces for its own
# arms (:65-66).  These apply ONLY to a phase's own derived config, NEVER to the integration profile
# and NEVER to a production file.
PHASE_CONTROLS = {
    "cancel-park": {"--conversation-cache-min-tokens": ("1", "size gate, generate.cpp:2639 + 9516-9526"),
                    "--conversation-cache-park-gain": ("0", "re-park gate, generate.cpp:2640 + 12879-12887")},
    "rewind": {},                       # dflash_cache.py forces its own controls (its :58-66)
}
# What cancel-park must SEE in the engine's own log before its cancellation counts as coverage:
# the park event and the restore event (generate.cpp:9691 parked / 13075 restored).
PHASE_EVIDENCE = {"cancel-park": ("parked", "restored"), "rewind": ()}

# The rewind tool FORCES the --expert-cache BYTE budget at its default 2000 (dflash_cache.py:40-42 +
# :58).  The engine's INFO counts are PHYSICAL slots: expert_slots over the primary, staged and remote
# sets, expert_slots_primary the primary set alone (src/program/generate.cpp:11470-11497), so a budget
# is NEVER the expected slot count.  The parent may supply the expected PHYSICAL counts as fixture
# inputs; they are forwarded to the tool's argv (rewind phase ONLY) and the tool keeps comparing them
# fail-closed (tools/dflash_sampling.py:validate_residency) - never learned from the observed engine.
REWIND_EXPERT_CACHE_BUDGET = 2000


def check_phase_expectations(phase: str, primary, total) -> tuple[tuple[int, int] | None, list[str]]:
    """Paired expected PHYSICAL slot counts, rewind phase ONLY, checked BEFORE any service action.
    Returns (pair, problems): a partial pair, a zero/negative, a reversed pair, a primary below the
    tool's requested budget, or any pair on a non-rewind phase is a problem, so the driver exits
    without a snapshot, a stop, a write, a signal or a GPU."""
    if (primary is None) != (total is None):
        return None, ["--expected-primary-slots and --expected-total-slots are required together"]
    if primary is None:
        return None, []
    problems: list[str] = []
    if phase != "rewind":
        problems.append(f"expected physical slot counts apply ONLY to the rewind phase (phase {phase!r})")
    if primary < 1 or total < 1:
        problems.append(f"expected slot counts must be strictly positive integers ({primary}/{total})")
    elif total < primary:
        problems.append(f"--expected-total-slots {total} is below --expected-primary-slots {primary}")
    elif primary < REWIND_EXPERT_CACHE_BUDGET:
        problems.append(f"expected primary slots {primary} is below the rewind tool's requested "
                        f"--expert-cache budget {REWIND_EXPERT_CACHE_BUDGET} - a clamped (shrunk) "
                        "resident set is never an accepted fixture")
    return (None if problems else (int(primary), int(total))), problems


def expected_slots_record(pair) -> dict:
    """What the plan and the window record about the expectation: budget, expectation, observation are
    kept as SEPARATE fields, and the record says plainly when no expectation was supplied."""
    return {"expected_primary_slots": (pair[0] if pair else None),
            "expected_total_slots": (pair[1] if pair else None),
            "requested_expert_cache_budget": REWIND_EXPERT_CACHE_BUDGET,
            "forwarded_to": (TOOL["rewind"] if pair else None),
            "note": ("parent fixture input, forwarded to the tool's argv and compared fail-closed there; "
                     "the tool records its observed counts separately in residency-expectations.json"
                     if pair else "no expectation - the tool keeps its legacy rule that both reported "
                                  "counts equal the requested budget")}


# Controller-surface flag extractor: the phase-controls fixtures assert against R.flag_value.
def flag_value(args: list[str], flag: str) -> str | None:
    """The value that follows a flag in an engine argv (None for a valueless flag) - a scan, so a
    valueless flag in the list can never misalign a zip."""
    i = args.index(flag)
    v = args[i + 1] if i + 1 < len(args) else ""
    return None if v.startswith("--") else v


def apply_phase_controls(cfg_path: pathlib.Path, phase: str, record: dict) -> None:
    """Write the phase's OWN diagnostic controls into the phase's own derived config file, asserting
    each flag is present (a silent no-op is refused), keeping every other arg in place, and keeping
    the file 0600.  An integration config is never touched (no controls exist for it)."""
    controls = PHASE_CONTROLS.get(phase, {})
    if not controls:
        record["phase_controls"] = {}
        return
    cfg = json.loads(cfg_path.read_text())
    args, actions = list(cfg["args"]), {}
    for flag, (value, cite) in controls.items():
        if flag not in args:
            raise SystemExit(f"REFUSE: {phase} control {flag} is not in the derived args - "
                             "a phase never invents a flag")
        i = args.index(flag)
        actions[flag] = {"was": args[i + 1], "now": value, "source": cite}
        args[i + 1] = value
    cfg["args"] = args
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
    os.chmod(cfg_path, 0o600)
    record["phase_controls"] = actions
    record["phase_controls_scope"] = (
        "phase file only - the integration profile and every production config keep the production "
        "values (min-tokens 2048, park-gain 4096)")


def phase_argv(phase: str, cfg_path: pathlib.Path, out_dir: pathlib.Path, expectations=None) -> list[str]:
    """The tool command.  WITHOUT an expectation it is byte-identical to the legacy command.  A PAIR of
    expected PHYSICAL slot counts is appended for the rewind phase ONLY - the tool compares it
    fail-closed and never derives it from its own engine."""
    argv = PHASES[phase]["argv"](cfg_path, out_dir)
    if expectations:
        if phase != "rewind":
            raise SystemExit(f"REFUSE: expected physical slot counts are rewind-only, phase {phase!r}")
        argv += ["--expected-primary-slots", str(expectations[0]),
                 "--expected-total-slots", str(expectations[1])]
    return [mw.VENV_PY, *argv]


def phase_out_path(phase: str, window: str) -> pathlib.Path:
    return W.OPS / f"{window}-{phase}"


def phase_out_dir(phase: str, window: str) -> pathlib.Path:
    d = phase_out_path(phase, window)          # created at EXECUTE only: a dry run writes nothing
    d.mkdir(parents=True, exist_ok=True)
    return d


def _term(pid: int, ticks: int) -> None:
    """SIGTERM ONLY a process verified by pid AND start ticks (no PID-reuse, no global kill)."""
    if W.same_proc(pid, ticks):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


DEVICE_KEYS = W.DEVICE_PREFIX + ("ROCM",)     # window.py's own device/library prefix list, plus ROCM*
# (HSA_* incl. HSA_ENABLE_SDMA, HIP_*, CUDA_*, LD_LIBRARY_PATH, STRATA_*, ROCM*) - no hand-picked five


def _device_key(name: str) -> bool:
    """A key the live engine's device/library environment actually carries (window.py's list)."""
    return name.startswith(DEVICE_KEYS)


def _env_proof(pid: int) -> dict:
    """The LIVE child's own device/library proof: window.py's OWN capture (`env_captured`, the same
    prefix list as the snapshot) and window.py's OWN masking (`masked`), so a secret-looking key
    (KEY/TOKEN/SECRET/PASSWORD) is persisted as `***`, never as its value."""
    proof = W.masked(W.env_captured(pid))
    return proof if proof else {"proof_empty": "no device/library key readable for this pid"}


def _cwd_of(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def _gpu_count(cfg: dict) -> int:
    g = cfg.get("gpu")
    if g in (None, ""):
        return 0
    items = g if isinstance(g, (list, tuple)) else str(g).split(",")
    return len([x for x in items if str(x).strip() != ""])


def _want_split(cfg: dict) -> str | None:
    """The engine `--layer-split` value the tools must put in their argv, from the config's structured
    key - the same rule serve/server.py:2871 (layer_split_of + layer_split_value) applies in the
    server path. None = the config names fewer than two GPUs, so a phase has no split to prove (and a
    phase REFUSES: the two-GPU split is required, a one-card 262144 run is not accepted)."""
    if _gpu_count(cfg) < 2:
        return None
    v = cfg.get("layer_split")
    if v in (None, ""):
        return "auto"
    if isinstance(v, (list, tuple)):
        return ",".join(str(x) for x in v)
    return str(v)


def _argv_split(argv: str) -> str | None:
    toks = argv.split()
    return toks[toks.index("--layer-split") + 1] if "--layer-split" in toks else None


def _observe(pid: int, seen: dict, phase: str, note, want: str | None) -> None:
    """Pin EVERY child of OUR tool the first time it is seen, while it is alive: pid + start ticks,
    exe, full argv, cwd and the device/library part of its environment. The engine appears AFTER the
    launch (dflash_cache spawns three, in sequence), so this runs at bounded intervals while the tool
    runs and its records are kept for every engine for the whole phase."""
    for child in W.children(pid):
        key = str(child)
        if key in seen:
            continue
        rec = {"pid": child, "ticks": W.start_ticks(child), "first_seen": time.strftime("%FT%T%z")}
        rec["exe"], rec["argv"] = W.exe_of(child), W.cmdline(child)
        rec["cwd"], rec["env_proof"] = _cwd_of(child), _env_proof(child)
        rec["layer_split"] = _argv_split(rec["argv"])
        seen[key] = rec
        note(f"{phase}: observed OUR child pid {child} ticks {rec['ticks']} exe {rec['exe']} "
             f"layer-split={rec['layer_split']} (want {want})")


def _proto_pids() -> list[int]:
    return sorted(int(d.name) for d in pathlib.Path("/proc").iterdir()
                  if d.name.isdigit() and W.exe_of(int(d.name)) == mw.PROTO_BINARY)


def _listing(d: pathlib.Path) -> list[dict]:
    if not d.is_dir():
        return []
    return [{"name": p.name, "bytes": p.stat().st_size} for p in sorted(d.iterdir()) if p.is_file()][:30]


def _copy_session(src: str, start: dict, dst: pathlib.Path) -> dict:
    """The current session's engine-log bytes only (offset pinned BEFORE the launch; append-mode
    history never copied) - the provenance rule of mw.draft_evidence, applied to the tool's log.
    Fail closed on an inode change AND on a SHRINK (the log got rotated/truncated below the pinned
    offset, so the session bytes are no longer there); the copied evidence is written 0600."""
    try:
        st = pathlib.Path(src).stat()
        if start.get("inode") is not None and st.st_ino != start["inode"]:
            return {"provenance": f"log inode changed during the phase ({start['inode']} -> {st.st_ino}) - REFUSED"}
        if st.st_size < int(start["bytes"]):
            return {"provenance": f"log SHRANK below the pinned offset ({st.st_size} bytes < "
                                  f"{start['bytes']}) - rotated/truncated, REFUSED"}
        with open(src, "rb") as fh:
            fh.seek(start["bytes"])
            session = fh.read()
        dst.write_bytes(session)
        os.chmod(dst, 0o600)                        # owner-only evidence (the mask is read with 0o777)
        mode = dst.stat().st_mode & 0o777
        txt = session.decode("utf-8", "replace")
        return {"per_window_copy": str(dst), "start_bytes": start["bytes"], "end_bytes": st.st_size,
                "session_bytes": len(session), "copy_mode": oct(mode), "copy_mode_is_0600": mode == 0o600,
                "markers": {m: m in txt for m in ("conversation cache: parked", "conversation cache: restored",
                                                  "skip parking", "RESUME")}}
    except OSError as e:
        return {"provenance": f"copy failed: {type(e).__name__}"}


def owned_run(phase: str, cfg_path: pathlib.Path, window: str, record: dict, note, snap: dict) -> bool:
    """ONE existing tool subprocess in OUR own process group, OBSERVED WHILE IT RUNS.

    The engine is a child that appears AFTER the launch (dflash_cache spawns three, in sequence, and
    each dies before the next), so the lineage of OUR tool pid is scanned at bounded intervals with
    short wait timeouts - no background watcher, no thread. Each pid is pinned with its start ticks
    the first time it is seen, and its exe / full argv / cwd / device-and-library environment proof
    are captured while it is alive and kept for every engine of the whole phase. Only our own pids
    are ever signalled (the tool pid first, then OUR observed children); a same-exe process we never
    observed is a RESTORE refusal, never an ownership claim, and is never signalled. A phase is NOT
    ok without at least one observed prototype engine whose argv carries the config's --layer-split
    value: an empty engines[] plus a PASS substring never passes."""
    spec, tlog = PHASES[phase], W.LOGS / f"{window}-{phase}-tool.log"
    out = phase_out_dir(phase, window)
    exp = record.get("phase_expected_slots") or {}
    pair = ((exp["expected_primary_slots"], exp["expected_total_slots"])
            if exp.get("expected_primary_slots") and exp.get("expected_total_slots") else None)
    argv = phase_argv(phase, cfg_path, out, pair)
    cfg = json.loads(cfg_path.read_text())
    want = _want_split(cfg)
    env_before = dict(os.environ)
    dev = {k: v for k, v in (snap.get("_env_full") or {}).items() if _device_key(k)}
    env = os.environ.copy()                        # the parent's env is READ, never mutated
    env.update(dev)                                # the LIVE original engine's flags go to the CHILD only
    env.update(PHASE_ENV)
    engine_log = cfg.get("log")
    start = mw.log_start({"log": engine_log}) if phase == "cancel-park" else None
    note(f"{phase}: launching {spec['tool']} (own process group, timeout {spec['timeout']}s, child device "
         f"flags {sorted(dev)}, --layer-split wanted {want}) -> {tlog}")
    t0, rc, seen, timed_out = time.time(), None, {}, False
    with open(tlog, "ab") as fh:
        proc = subprocess.Popen(argv, cwd=mw.CLONE, env=env, stdout=fh, stderr=fh,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    own_rec = {"pid": proc.pid, "ticks": W.start_ticks(proc.pid)}
    _observe(proc.pid, seen, phase, note, want)
    deadline = t0 + spec["timeout"]
    try:
        while True:                                # bounded LIVE observation of OUR lineage
            remain = deadline - time.time()
            if remain <= 0:
                rc, timed_out = 124, True
                break
            try:
                rc = proc.wait(timeout=min(2.0, max(0.2, remain)))
                break
            except subprocess.TimeoutExpired:
                _observe(proc.pid, seen, phase, note, want)
    except BaseException as e:                     # interrupted: clean OUR family, then restore
        record["phase_error"] = repr(e)[:300]
        note(f"{phase}: interrupted - SIGTERM to OUR tool pid and OUR observed child pids ONLY")
        _term(proc.pid, own_rec["ticks"])
        for rec in seen.values():
            _term(rec["pid"], rec["ticks"])
    if timed_out:
        note(f"{phase}: TOOL_TIMEOUT after {spec['timeout']}s - SIGTERM to OUR tool pid {proc.pid} only")
        _term(proc.pid, own_rec["ticks"])
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            record["tool_still_alive"] = True
    for rec in list(seen.values()):                # OUR children that outlived the tool: verified first
        if W.same_proc(rec["pid"], rec["ticks"]) and rec["exe"] == mw.PROTO_BINARY:
            note(f"{phase}: OUR engine {rec['pid']} alive - SIGTERM to that verified pid+ticks only")
            _term(rec["pid"], rec["ticks"])
    deadline = time.time() + 120
    while time.time() < deadline:                  # delayed children are observed and joined as well
        _observe(proc.pid, seen, phase, note, want)
        if not any(W.same_proc(r["pid"], r["ticks"]) for r in seen.values()):
            break
        time.sleep(2)
    survivors = {str(r["pid"]): r["ticks"] for r in seen.values() if W.same_proc(r["pid"], r["ticks"])}
    text = _tail(tlog, 90)
    markers = [m for m in spec["markers"] if any(m in ln for ln in text)]
    engines = list(seen.values())
    observed_split = [r for r in engines if want is not None and r["exe"] == mw.PROTO_BINARY
                      and r["layer_split"] == want]
    ev = {"phase": phase, "exit": rc, "seconds": round(time.time() - t0, 3), "tool_log": str(tlog),
          "output_dir": str(out), "argv": argv, "env_extra": PHASE_ENV,
          "child_device_flags": sorted(dev), "parent_env_mutated": dict(os.environ) != env_before,
          "engine_log": engine_log, "engine_log_start": start, "markers_required": list(spec["markers"]),
          "markers_found": markers, "stdout_tail": text[-12:], "engines": engines,
          "engines_observed": len(engines), "survivors_after": survivors, "outputs": _listing(out),
          "forced_overrides": spec["forced"], "shape": spec["shape"], "notes": spec["notes"],
          "expected_slots": (record.get("phase_expected_slots") or {}),
          "tool_alive_after": W.same_proc(own_rec["pid"], own_rec["ticks"]),
          "split_wanted": want,
          "split_observed": [{"pid": r["pid"], "argv_split": r["layer_split"], "exe": r["exe"],
                              "cwd": r["cwd"], "env_proof": r["env_proof"]} for r in observed_split]}
    if start:
        ev["session_copy"] = _copy_session(engine_log, start, W.LOGS / f"{window}-{phase}-engine.log")
    # The engine's OWN events, not the tool's words: cancel-park only counts as coverage when the
    # session copy shows a park AND a restore (generate.cpp:9691 / 13075).  A tool PASS with nothing
    # parked is refused - the 101959 window measured exactly that ("skip parking (1829 tokens, below
    # the 2048 minimum; parked=0)").
    evidence = list(PHASE_EVIDENCE.get(phase, ()))
    copy_markers = (ev.get("session_copy") or {}).get("markers") or {}
    evidence_seen = [m for m in evidence if copy_markers.get("conversation cache: " + m)]
    evidence_ok = not evidence or len(evidence_seen) == len(evidence)
    ev["evidence_required"] = evidence
    ev["evidence_seen"] = evidence_seen
    ev["engine_cache_markers"] = copy_markers
    ev["tool_precondition_lines"] = [ln for ln in text if "PRECONDITION FAIL" in ln][-3:]
    ev["phase_ok"] = (rc == 0 and len(markers) == len(spec["markers"]) and not survivors
                      and bool(observed_split) and evidence_ok)
    ev["phase_ok_why"] = (["exit is not 0"] if rc != 0 else []) \
        + ([f"markers missing: {[m for m in spec['markers'] if m not in markers]}"]
           if len(markers) != len(spec["markers"]) else []) \
        + ([f"owned survivors {sorted(int(p) for p in survivors)}"] if survivors else []) \
        + ([f"no observed prototype engine with --layer-split {want} in its argv ({len(engines)} children "
            f"observed, their --layer-split values {[r['layer_split'] for r in engines]})"]
           if not observed_split else []) \
        + ([f"the engine's own log shows no {' and '.join(evidence)} event, so the cancellation covered "
            f"nothing (session-copy markers {copy_markers})"] if not evidence_ok else [])
    record["phase_run"] = ev
    record["survivors"] = {"exe_matches": _proto_pids(),
                           "recorded_family_alive": sorted({int(p) for p in survivors}
                                                           | ({own_rec["pid"]} if ev["tool_alive_after"] else set())),
                           "port_8019_listener": W.listener_pid(mw.PROTO_PORT)}
    note(f"{phase}: exit {rc}, markers {markers}, observed OUR children {len(engines)}, split proven "
         f"{bool(observed_split)}, owned survivors {sorted(int(p) for p in survivors)}")
    return ev["phase_ok"]


def run_phase_window(phase: str, snap: dict, record: dict, cfg: dict, cfg_path: pathlib.Path, note) -> None:
    """A verified original-down window with NO prototype server started and ONE tool subprocess."""
    if not W.stop_prod(snap, record, note):
        return
    if W.listener_pid(mw.PROTO_PORT) is not None:
        record["break_reason"] = f"port {mw.PROTO_PORT} has a listener - {phase} REFUSED (fail closed, nothing signalled)"
        note(record["break_reason"])
        return
    if _proto_pids():
        record["break_reason"] = f"prototype-exe processes already alive - {phase} REFUSED (no double load, nothing signalled)"
        note(record["break_reason"])
        return
    record["all_modes_ok"] = owned_run(phase, cfg_path, record["window"], record, note, snap)
    if not record["all_modes_ok"]:
        record["break_reason"] = (f"{phase}: phase_ok False "
                                  f"({record['phase_run'].get('phase_ok_why')}) - restoring now")
        note(record["break_reason"])


def plan(window: str, phase: str = "integration", expectations=None) -> tuple[dict, list[str]]:
    """The dry-run plan: currency + derivation + source support + the exact parent command."""
    prod = load_prod_config()
    summary, problems = mw.currency(MODE, WIDTH, prod)
    bp = pathlib.Path(summary["config"])
    base = json.loads(bp.read_text()) if bp.is_file() else {}
    derived, overrides, dp = derive(window, base)
    support, sp = source_support()
    cfg = mw.CONFIG_DIR / f"proto-server-swift15-{MODE}-reg-{window}.json"
    out = {"window": window, "prod_config_sha256": W.config_sha(), "base_currency": summary,
           "base_config": summary["config"], "derived_config_would_be": str(cfg),
           "derived_log": derived.get("log"), "derived_overrides": overrides,
           "derived_args_tail": derived.get("args", [])[-12:],
           "allowlist": {k: v for k, (v, _c) in ALLOW.items()}, "source_support": support,
           "identity_note": identity_note(derived),
           "integration_command": (f"{mw.VENV_PY} {INTEGRATION} --execute --url http://127.0.0.1:"
                                   f"{mw.PROTO_PORT} --identity-config {cfg} --confirm-execution "
                                   f"--results-json {W.LOGS}/{window}-integration-results.json"),
           "runtime_proofs_required": PROOFS, "not_run_here": NOT_RUN_HERE, "phase": phase}
    if phase != "integration":
        spec = PHASES[phase]
        out["phase_command"] = " ".join(phase_argv(phase, cfg, phase_out_path(phase, window), expectations))
        out["phase_tool"] = spec["tool"]
        out["phase_expected_slots"] = expected_slots_record(expectations)
        out["phase_timeout_s"] = spec["timeout"]
        out["phase_markers_required"] = list(spec["markers"])
        out["phase_env_extra"] = PHASE_ENV
        out["phase_forced_overrides"] = spec["forced"]
        out["phase_shape"] = spec["shape"]
        out["phase_notes"] = spec["notes"]
        out["phase_rules"] = ["NO prototype server is started in this phase (no mw.start_mode call)",
                             "port 8019 must be free and NO prototype-exe process may be alive (fail closed)",
                             "one tool subprocess in its own process group; only its pid and its OBSERVED "
                             "child pids (pid + start ticks) are ever signalled",
                             "the tool's lineage is observed at bounded intervals WHILE it runs (short "
                             "wait timeouts, no watcher thread), so engines that appear late, run "
                             "sequentially, or orphan are pinned with their exe/argv/cwd/env proof",
                             f"the child env carries the live original engine's device/library flags "
                             f"(window.py's '_env_full'), the parent's os.environ is never mutated",
                             "the original restore is REFUSED while any owned pid lives (restore gate); a "
                             "same-exe process we never observed refuses the restore and is never signalled"]
        out["phase_split_required"] = _want_split(derived)
        out["phase_controls"] = {k: v for k, (v, _c) in PHASE_CONTROLS.get(phase, {}).items()}
        out["phase_controls_scope"] = ("the PHASE's own derived config file only - never the integration "
                                       "profile, never a production config"
                                       if PHASE_CONTROLS.get(phase) else "no controls for this phase")
        out["phase_engine_evidence_required"] = list(PHASE_EVIDENCE.get(phase, ()))
        out["phase_success_gate"] = ["exit 0", "every required marker present in the tool's OWN output",
                                     "no owned survivor (the tool pid plus every observed child)",
                                     f"at least ONE observed prototype engine whose argv carries "
                                     f"--layer-split {_want_split(derived)} (the real two-GPU split; a "
                                     f"single-card 262144 run is not an acceptable substitute)"]
    return out, problems + dp + sp


def restore_gate(snap: dict, record: dict, surv: dict, deadline: float) -> dict:
    """Never restore with a prototype survivor alive (no duplicate full model load); with the
    family verified gone, use window.py's SAME restore + verification path."""
    if not (surv["exe_matches"] or surv["recorded_family_alive"] or surv["port_8019_listener"]):
        return W.restore_and_verify(snap, record, deadline)
    return {"restored": False,
            "refused": f"prototype family survivors {json.dumps(surv)} - restore REFUSED (no duplicate "
                       "full model load); parent: clean the recorded pids, re-check, then restore"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="PARENT-only: real window (default DRY RUN)")
    ap.add_argument("--phase", choices=sorted(PHASES) + ["integration"], default="integration",
                    help="integration (default, the 8019 prototype lane) | cancel-park | rewind: the last "
                         "TWO run ONE existing tool subprocess with its OWN engine, no prototype server")
    ap.add_argument("--expected-primary-slots", type=int,
                    help="PARENT fixture: expected PHYSICAL primary expert slots (the engine's "
                         "expert_slots_primary) - NOT the tool's --expert-cache byte budget.  Required "
                         "together with --expected-total-slots, rewind phase ONLY, total >= primary, "
                         "expected primary >= the tool's requested budget; validated BEFORE any snapshot, "
                         "stop, write, signal or GPU")
    ap.add_argument("--expected-total-slots", type=int,
                    help="PARENT fixture: expected PHYSICAL TOTAL expert slots (the engine's expert_slots, "
                         "the primary, staged and remote sets together); paired with "
                         "--expected-primary-slots, rewind phase ONLY")
    a = ap.parse_args()
    expectations, exp_problems = check_phase_expectations(a.phase, a.expected_primary_slots,
                                                          a.expected_total_slots)
    if exp_problems:
        ap.error("; ".join(exp_problems))      # exit 2 - no plan, no snapshot, no stop, no write, no GPU

    window = f"reg-window-{time.strftime('%Y%m%d-%H%M%S')}-{a.phase}"    # unique per phase, never overwritten
    W.LOGS.mkdir(parents=True, exist_ok=True)
    mw.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    results, wlog = W.OPS / f"{window}-results.json", W.LOGS / f"{window}.log"
    note = lambda msg: W.note_to(wlog, msg)                        # noqa: E731

    out, problems = plan(window, a.phase, expectations)
    snap = W.snapshot()
    print(json.dumps({"window": window, "production_snapshot": W.display(snap), **out,
                      "problems": problems}, indent=2))
    for bad, msg in ((not snap.get("verified"), "REFUSE: " + snap.get("why", "snapshot failed")),
                     (bool(problems), "REFUSE: derivation / currency / source-support problems above - "
                                      "never hand-edit a config")):
        if bad:
            print(msg)
            return 2
    if not a.execute:
        print("DRY RUN only - zero signals, zero writes, zero GPU, zero HTTP. Parent: rerun with --execute.")
        return 0
    derived, _ov, dp = derive(window, json.loads(pathlib.Path(out["base_config"]).read_text()))
    if dp:                                                    # re-check at execute time, fail closed
        print("REFUSE: derivation problems at execute time: " + json.dumps(dp))
        return 2
    cfg_path = pathlib.Path(out["derived_config_would_be"])
    write_derived_config(cfg_path, derived)                   # unique name, 0600, never an existing config
    record_extra: dict = {"phase_expected_slots": expected_slots_record(expectations)}
    apply_phase_controls(cfg_path, a.phase, record_extra)     # phase file ONLY; integration has no controls

    record: dict = {"window": window, "started": time.strftime("%FT%T%z"), "phase": a.phase,
                    "snapshot": W.display(snap), "prod_config_sha256": W.config_sha(),
                    "config": str(cfg_path), "derived": out, "stop_sent": False, "modes": [],
                    **record_extra}
    mw.install_signal_handlers()                                    # SIGTERM/SIGINT -> WindowSignal path
    try:
        if a.phase == "integration":
            run_regression(snap, record, derived, cfg_path, note)
        else:
            run_phase_window(a.phase, snap, record, derived, cfg_path, note)   # NO prototype server
    except BaseException as e:                                      # restore honours stop_sent: never a duplicate
        record["window_error"] = repr(e)[:300]
    finally:
        surv = record.get("survivors") or mw.proto_survivors(record, (mw.PROTO_BINARY,))
        record["restore"] = restore_gate(snap, record, surv, time.time() + 360)
        ok = record.get("all_modes_ok") and record["restore"].get("restored")
        record["status"] = ("ALL OK" if ok else "RESTORED; regression run failed - see logs"
                            if record["restore"].get("restored") else "RESTORE FAILED - PARENT INVESTIGATE NOW")
        W.note_to(wlog, f"WINDOW_FINAL {record['status']} restore={json.dumps(record['restore'])}")
        results.write_text(json.dumps(record, indent=2) + "\n")
        os.chmod(results, 0o600)
        print(json.dumps(record, indent=2))
    return 0 if record.get("all_modes_ok") and record["restore"].get("restored") else 1


if __name__ == "__main__":
    sys.exit(main())
