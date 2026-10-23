#!/usr/bin/env python3
"""MODEL-WINDOW controller - PARENT-only operator automation (agents never run --execute).

Default is DRY RUN: fresh production snapshot + config-currency + preflight + plan.
ZERO signals, ZERO GPU, ZERO HTTP inference (the dry run issues no HTTP request at all).

--execute (parent only), in order:
 1. Fresh snapshot + full verification of the ORIGINAL 8009 production proxy exactly as
    ops/window.py does (argv, cwd, config sha, proxy start ticks, the engine child via
    readlink /proc/PID/exe, vision children pinned by pid + start ticks).  An unverified
    snapshot REFUSES the window.
 2. Verified SIGTERM stop of that proxy ONLY, through the shared stop_prod() helper of
    window.py, and the whole original family must exit.  A surviving verified descendant
    means NO GPU work runs at all.
 3. Exactly two modes, sequentially, on port 8019 ONLY: target_only first, then dflash_w3,
    with the existing configs under the selected config dir - /mnt/user/dflash2/config by default,
    opt-in STRATA_WINDOW_CONFIG_DIR for an absolute override (this script never writes a
    config).  Each starts from the PRODUCTION venv python with the PROTOTYPE serve/server.py
    of the clone tree the config's own "exe" field points into (verified against the config;
    no second tree is ever hardcoded).  Startup deadline 180 s, extended once to 360 s for a
    cold load; an early child exit is detected.  Then /health and /props must report the
    exact Swift model path, the engine child's exe must be the prototype binary, and its
    cmdline must carry the config's flags (+ --serve, --max-context 262144, --layer-split
    from the structured key); the engine cmdline and a one-shot ROCm identity proof from
    /proc (environ LD_LIBRARY_PATH + maps library paths) are captured.
 4. Per mode: two short greedy requests, max 16 new tokens each, reasoning explicitly off
    (strata_bench.chat: temperature 0, reasoning_effort "none", enable_thinking false),
    120 s request timeout.  Returned texts, usage, timings and errors are recorded VERBATIM.
    Plausible text is NEVER treated as correctness evidence.  dflash_w3 additionally needs
    draft-activation evidence in the engine log (geometry / cross-GPU tap-collection / drafter
    VRAM / shared-head lines plus draft-acceptance counters with drafts offered); a silent
    fallback to target-only in the log is a MODE FAILURE, not a pass.  target_only must show
    NO drafter lines and offer no drafts.
 5. The whole prototype family is stopped (SIGTERM to ITS proxy pid only) and verified gone -
    port free, proxy pid, engine pid, vision pids, and NO process whose exe is the prototype
    binary - before the next mode starts and before any restore.  Never two full targets
    loaded at once.
 6. Always restore the original production through the SAME restore_and_verify() path as
    window.py (original launcher backgrounded, then /health + model + config-sha + engine
    cmdline verification).  Interruptions and timeouts are recorded and still restore.

Safety: no global kill anywhere; no edit of the production config, model or binaries; nothing
OpenCode-related is ever matched; secret-looking env values are masked in every printed and
persisted output; results JSON 0600; unique per-window labels and a per-window copy of each
engine log (nothing is overwritten); nonzero exit if any mode fails.  SIGTERM/SIGINT are
converted into a WindowSignal exception (no default handler: a signal takes the SAME cleanup
path as an interruption - per-mode stop, always-restore).  The original production restore is
REFUSED while ANY prototype-family process (recorded proxy/engine/vision pids, the isolated
port listener, any process whose exe is a launched mode's binary) is alive: no duplicate model
load is ever attempted; the refusal is recorded as a clear restoration failure.

Log-copy provenance (L16 review): each mode records the SOURCE engine log's byte offset +
inode IMMEDIATELY before its launch, and the per-window evidence copy contains ONLY the
CURRENT session's bytes (from that offset to EOF, source log left untouched, append-mode
history NEVER copied).  If the log's inode changed during the mode, provenance FAILS and the
mode fails - evidence can never be read from an earlier window's lines.

Optional --parity-dir BASE (dflash_w3 ONLY): composes STRATA_DF_PARITY=<BASE>/<window> and
STRATA_DF_PARITY_CYCLE=0 into the child env (dflash_runtime.cpp:45-59 - the drafter's cycle
counter starts at 0 (line 329, ++ at 1493), so CYCLE=0 dumps the FIRST drafter cycle; the
default 1 would dump the second), creates the dir at execute time for that mode only (a dry
run never creates it), records the dir in the results JSON and lists the dump files (names +
sizes) after the run.  With parity on, dflash_w3 runs EXACTLY ONE diagnostic request (a second
request resets the drafter and rewrites the cycle-0 files with inconsistent leftover pool
state); a MISSING dir, ZERO files, or only-empty files is a MODE FAILURE, never a pass.
Without the flag the behavior is byte-identical to the L13 lane.

Pipeline-window finding (source-read, see L13-MODEL-WINDOW-PREP.md): the implicit default is
provably safe for the dflash_w3 config as generated, so NO extra flag and NO extra config is
generated.  src/program/generate.cpp:4183-4202 turns the auto default ON only with
`!o.mtp.empty() && o.spec >= 2` (line 4197); the dflash_w3 config carries --dflash and no
--mtp (--mtp/--dflash are mutually exclusive, generate.cpp:677, 5441), so pipeline_windows
ends at 0 = the serial collector.  An explicit --pipeline-windows N>0 is refused for exactly
this combination - generate.cpp:4214-4215 "not with --dflash on a layer split (the cross-GPU
tap collector is serial)" - and is set to 0 (4218-4220).  Both paths reach the same serial
state, so the generated config needs no pipeline flag.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import window as W                                      # reviewed snapshot/stop/restore helpers

# The production venv python is the proxy's own and is NEVER overridable.
VENV_PY = "/mnt/user/Strata/.venv/bin/python"          # production venv python (the proxy's own)


def _abs_override(env_name: str, default: str) -> str:
    """Opt-in ABSOLUTE path override, default-preserving (an absent/empty env yields the default
    string BYTE-FOR-BYTE). A relative value is REFUSED, never resolved against a cwd: a typo can
    never make a controller read/write configs, import tools, or launch a proxy in an unselected
    tree. Same env names and mechanism as the cloner (tools/test_inco_dflash2_integration.py), so
    the imported clone-provider and the controller always resolve the SAME clone tree, binary,
    server and config dir - a relative value fails the import of BOTH, never just one."""
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        return default
    val = raw.strip()
    if not val.startswith("/"):
        raise ValueError(f"{env_name}={raw!r}: REFUSED - the override must be an ABSOLUTE path "
                         f"(default {default!r}); a relative override is never resolved against a cwd")
    return val


# The tree the config's exe points into, the prototype binary/server it launches, and the config
# dir. Overridable so a parent gate can point the SAME reviewed machinery at another engine tree
# (e.g. the upstream-integ worktree) and a new config dir without touching the prototype:
#   STRATA_WINDOW_CLONE, STRATA_WINDOW_PROTO_BINARY, STRATA_WINDOW_PROTO_SERVER,
#   STRATA_WINDOW_CONFIG_DIR
# ALL FOUR go through _abs_override (no raw os.environ.get on a selected path): the defaults are
# byte-for-byte the literals the lane used before, PROTO_BINARY/PROTO_SERVER still derive from the
# selected CLONE, and every controller reads config paths ONLY through mw.CONFIG_DIR.
CLONE = _abs_override("STRATA_WINDOW_CLONE", "/mnt/user/dflash2/Strata-wt")
PROTO_BINARY = _abs_override("STRATA_WINDOW_PROTO_BINARY", f"{CLONE}/build-gfx906-dflash2/strata")
PROTO_SERVER = _abs_override("STRATA_WINDOW_PROTO_SERVER", f"{CLONE}/serve/server.py")
CONFIG_DIR = pathlib.Path(_abs_override("STRATA_WINDOW_CONFIG_DIR", "/mnt/user/dflash2/config"))
ARTIFACT = "/mnt/user/dflash2/artifacts/inco-dflash2-bf16.gguf"
LOGS, OPS = W.LOGS, W.OPS
DUMPS = pathlib.Path("/mnt/user/dflash2/dumps")       # STRATA_DF_PARITY dump base (parent's compare input)
PROTO_PORT = 8019
MODES = [("target_only", 1), ("dflash_w3", 3)]          # this exact order
STARTUP_S, COLD_S, REQ_TIMEOUT, MAX_TOKENS = 180, 360, 120, 16
PROMPTS = ["Explain, in two short sentences, why layer splitting moves K/V pools per stage.",
           "Name, in a few words, how many GPUs a layer split of two stages uses."]
INTENDED_KEYS = {"exe", "log", "port"}                  # the ONLY allowed non-arg diffs
# The structural diffs of the integration cloner (tools/test_inco_dflash2_integration.py:125-151),
# spelled out for EVERY mode the controllers launch - currency never KeyErrors and never waives
# a path by filtering messages.
ARG_ADDS = {"suffix_lookup": {},
            "target_only": {"--suffix-draft": "0", "--lookup-chain": "0"},
            "old_target_only": {"--suffix-draft": "0", "--lookup-chain": "0"},
            "dflash_w3": {"--dflash": ARTIFACT, "--dflash-block": "3", "--suffix-draft": "0"}}
ARG_REPLACES = {"suffix_lookup": {},
                "target_only": {"--spec": "2"},
                "old_target_only": {"--spec": "2"},          # same arg diffs as target_only
                "dflash_w3": {"--spec": "4", "--spec-min-p": "0.5"}}
EXE_WANT = {"old_target_only": W.PROD_EXE}              # the documented OLD-binary arm; default PROTO_BINARY
OLD_LOG = "engine-oldbinary-target-only.log"            # the cloner's exact old-binary log name (integration:135)


def log_want(mode: str) -> str:
    """The EXACT log path the cloner writes for this mode (no waived paths, no filtering)."""
    return f"{LOGS}/{OLD_LOG}" if mode == "old_target_only" else f"{LOGS}/engine-prototype-{mode}.log"
# Engine-log markers (source-cited strings: generate.cpp:5510-5578, dflash_runtime.cpp:604-742,
# generate.cpp:12477, 15460, 15757).
DFLASH_ON = ("dflash: ", "cross-GPU tap collection serial", "shared target head", "indexed shared head")
ACCEPT_RE = re.compile(r"drafts accepted (\d+) of (\d+)")
OFF_RE = re.compile(r"target-only decoding|is off:|--lookup-chain is not supported|"
                    r"exceeds DFlash capacity|Unrecoverable native verification failure|verify: timed out")


# --------------------------------------------------------------------- config currency (read-only)
def flags(args: list[str]) -> list[tuple[str, str | None]]:
    """(flag, value) pairs in order; valueless flags carry None (same arity walk as the plan tool)."""
    out, i = [], 0
    while i < len(args):
        tok = args[i]
        if tok.startswith("--"):
            val = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("--") else None
            out.append((tok, val))
            i += 2 if val is not None else 1
        else:
            i += 1
    return out


def currency(mode: str, width: int, prod: dict) -> tuple[dict, list[str]]:
    """Every field of the mode config except the intended exe/log/port and the drafter-flag diffs
    must equal the CURRENT production config.  Returns (summary, problems); problems fail the run."""
    path = CONFIG_DIR / f"proto-server-swift15-{mode}.json"
    # L19 generalization, STRUCTURAL AND FIRST (parent review): the intended-diff template is
    # resolved BEFORE any file is read, so an invented mode label is refused on structure even when
    # its file is missing - a controller never guesses a template from a filename. The 4 existing
    # modes keep their own templates; `dflash_w<N>` uses the cloner's DFlash template at ITS OWN
    # width (tools/test_inco_dflash2_integration.py:143-148: --spec N+1, --dflash-block N,
    # --spec-min-p 0.5, --dflash <artifact>, --suffix-draft 0); dflash_w3's verdicts are unchanged.
    tmpl = mode if mode in ARG_ADDS else ("dflash_w3" if mode.startswith("dflash_w") else None)
    if tmpl is None:
        return {"config": str(path)}, [f"{mode}: no intended-diff template - controllers never invent modes"]
    if not path.is_file():
        return {"config": str(path)}, [f"{mode}: config {path} missing - regenerate with the plan tool"]
    cfg = json.loads(path.read_text())
    want_adds, want_repl = dict(ARG_ADDS[tmpl]), dict(ARG_REPLACES[tmpl])
    if mode.startswith("dflash_w"):
        want_adds["--dflash-block"] = str(width)
        want_repl["--spec"] = str(width + 1)
    prod_f, mode_f = flags(prod["args"]), flags(cfg.get("args", []))
    prod_d, mode_d = dict(prod_f), dict(mode_f)
    problems: list[str] = []
    if len(prod_d) != len(prod_f) or len(mode_d) != len(mode_f):
        problems.append(f"{mode}: a flag appears twice in args")
    adds = {k: v for k, v in mode_d.items() if k not in prod_d}
    missing = {k: v for k, v in prod_d.items() if k not in mode_d}
    changed = {k: v for k, v in mode_d.items() if k in prod_d and v != prod_d[k]}
    if adds != want_adds:
        problems.append(f"{mode}: added flags {adds} != intended {want_adds}")
    if missing:
        problems.append(f"{mode}: production flags absent from the clone: {sorted(missing)}")
    if changed != want_repl:
        problems.append(f"{mode}: value changes {changed} != intended {want_repl}")
    if [f for f, _ in mode_f if f in prod_d] != [f for f, _ in prod_f]:
        problems.append(f"{mode}: flag order differs from production (the clone appends, never reorders)")
    for key in prod:
        if key == "args":            # args are checked structurally above (adds/missing/changed/order)
            continue
        if key not in INTENDED_KEYS and cfg.get(key) != prod[key]:
            problems.append(f"{mode}: key {key!r} differs from production ({cfg.get(key)!r} != {prod[key]!r})")
    for key in cfg:
        if key not in prod:
            problems.append(f"{mode}: key {key!r} is not in the production config")
    exe_want = EXE_WANT.get(mode, PROTO_BINARY)
    if cfg.get("exe") != exe_want:
        problems.append(f"{mode}: exe {cfg.get('exe')!r} is not the mode's binary {exe_want}")
    if cfg.get("port") != PROTO_PORT:
        problems.append(f"{mode}: port {cfg.get('port')!r} is not the isolated {PROTO_PORT}")
    if cfg.get("log") != log_want(mode):
        problems.append(f"{mode}: log {cfg.get('log')!r} is not the mode's exact log {log_want(mode)}")
    if "--pipeline-windows" in mode_d:
        problems.append(f"{mode}: --pipeline-windows set explicitly; the source default is already the "
                        "serial collector (generate.cpp:4183-4220)")
    if "--mtp" in mode_d:
        problems.append(f"{mode}: --mtp present (mutually exclusive with --dflash; it also arms the "
                        "pipeline auto-on gate)")
    summary = {"config": str(path), "port": cfg.get("port"), "exe": cfg.get("exe"), "exe_want": exe_want,
               "log": cfg.get("log"),
               "args_added": sorted(adds), "args_changed": {k: v for k, v in changed.items()},
               "layer_split_structured_key": str(cfg.get("layer_split")),
               "pipeline_windows": "absent -> 0 (serial collector): generate.cpp:4197 needs --mtp; "
                                   "4214 refuses it for dflash+split"}
    return summary, problems


def preflight(prod: dict) -> tuple[list[str], list[str]]:
    """Dry-run safe existence checks: files only, no HTTP, no process, no GPU."""
    checks, problems = [], []
    for path, label in ((VENV_PY, "production venv python"), (PROTO_SERVER, "prototype serve/server.py"),
                        (PROTO_BINARY, "prototype engine binary"), (ARTIFACT, "Inco BF16 artifact"),
                        (W.LAUNCHER, "original production launcher")):
        ok = pathlib.Path(path).exists()
        checks.append(f"{label}: {'ok' if ok else 'MISSING'} {path}")
        if not ok:
            problems.append(f"{label} missing: {path}")
    tok = pathlib.Path(prod.get("tokenizer", ""))
    for f in ("vocab.json", "merges.txt", "token_type.json"):
        if not (tok / f).is_file():
            problems.append(f"tokenizer file missing: {tok / f}")
    checks.append(f"tokenizer dir: {tok}")
    return checks, problems


# --------------------------------------------------------------------- parity dumps (--parity-dir, dflash_w3 only)
def parity_dir_for(window: str, base: str) -> pathlib.Path:
    """The per-window dump dir (unique label: reruns never mix dumps; the engine appends
    <name>.bin lazily, only when STRATA_DF_PARITY_CYCLE matches the drafter cycle)."""
    return pathlib.Path(base) / window


def parity_files(d: pathlib.Path) -> dict:
    """Names + sizes ONLY (contents stay the parent's compare input, never read here)."""
    if not d.is_dir():
        return {"dir": str(d), "exists": False, "files": []}
    files = [{"name": p.name, "bytes": p.stat().st_size}
             for p in sorted(d.iterdir()) if p.is_file() and p.suffix == ".bin"]
    return {"dir": str(d), "exists": True, "n_files": len(files), "files": files}


# --------------------------------------------------------------------- prototype identity (read-only)
def engine_of(server_pid: int, exe_want: str = PROTO_BINARY) -> int | None:
    return next((k for k in W.children(server_pid) if W.exe_of(k) == exe_want), None)


def engine_identity(engine: int, cfg: dict, width: int, exe_want: str = PROTO_BINARY) -> tuple[bool, dict]:
    """The engine child IS the mode's binary and its cmdline carries --serve, the config's own
    flags and the server-derived --layer-split.  Checked by inclusion, never by rebuilding argv."""
    cmd = W.cmdline(engine)
    exe = W.exe_of(engine)
    need = ["--serve", f"--max-context {W.MAX_CTX.split()[-1]}", f"--native {W.MODEL_PATH}",
            f"--layer-split {cfg.get('layer_split')}"] + [f"{f} {v}" if v else f for f, v in flags(cfg["args"])]
    missing = [n for n in need if n not in cmd]
    ok = exe == exe_want and not missing
    return ok, {"engine_exe": exe, "engine_exe_matches_want": exe == exe_want, "exe_want": exe_want,
                "engine_cmdline_missing": missing, "engine_cmdline": cmd,
                "dflash_flag_in_cmdline": f"--dflash {ARTIFACT}" in cmd,
                "dflash_block_in_cmdline": f"--dflash-block {width}" in cmd}


def rocm_proof(engine: int) -> dict:
    """One-shot proof from /proc that THIS engine resolves the gfx906 ROCm libraries."""
    env = W.masked(W.env_captured(engine))
    libs = []
    try:
        for line in pathlib.Path(f"/proc/{engine}/maps").read_text().splitlines():
            parts = line.split(maxsplit=5)
            if len(parts) == 6 and any(k in parts[5] for k in
                                       ("librocblas", "libhipblas", "libamdhip64", "libhsa-runtime64")):
                libs.append(parts[5])
    except OSError as e:
        libs.append(f"<maps unreadable: {type(e).__name__}>")
    return {"ld_library_path": env.get("LD_LIBRARY_PATH", ""), "maps_libs": sorted(set(libs))[:8]}


def verify_proto(server_pid: int, cfg: dict, width: int, exe_want: str = PROTO_BINARY) -> tuple[bool, dict]:
    """/health + exact /props model path + the mode engine child's exe and flags."""
    try:
        health = str(W.http_json(f"http://127.0.0.1:{PROTO_PORT}/health", 20.0))[:120]
        props = W.http_json(f"http://127.0.0.1:{PROTO_PORT}/props", 20.0)
    except (OSError, ValueError) as e:
        return False, {"detail": f"endpoints not ready: {type(e).__name__}"}
    detail = {"health": health, "model_path": props.get("model_path"), "model_alias": props.get("model_alias")}
    engine = engine_of(server_pid, exe_want)
    detail["engine"] = engine
    if engine is None:
        detail["detail"] = f"no engine child with the mode's exe {exe_want} (readlink /proc/PID/exe)"
        return False, detail
    ok_engine, idet = engine_identity(engine, cfg, width, exe_want)
    detail.update(idet)
    detail["model_path_matches_swift"] = detail["model_path"] == W.MODEL_PATH
    return (detail["model_path_matches_swift"] and ok_engine), detail


# --------------------------------------------------------------------- the window
def log_start(cfg: dict) -> dict:
    """The source engine log's size+inode IMMEDIATELY before the mode launches: the per-window
    evidence copy starts at this offset, so an earlier window's append-mode lines can never be
    read as this mode's evidence, and an inode change during the mode fails provenance."""
    try:
        st = pathlib.Path(cfg["log"]).stat()
        return {"path": cfg["log"], "bytes": st.st_size, "inode": st.st_ino}
    except OSError:
        return {"path": cfg["log"], "bytes": 0, "inode": None, "absent": True}


def start_mode(window: str, mode: str, cfg: dict, width: int, env: dict, rec: dict, note) -> bool:
    exe_want = EXE_WANT.get(mode, PROTO_BINARY)
    rec["exe_want"] = exe_want
    if W.listener_pid(PROTO_PORT) is not None:
        rec["startup"] = f"port {PROTO_PORT} already has a listener - refuse the mode (fail closed, nothing signalled)"
        return False
    cfg_path = pathlib.Path(rec["config"])
    slog = LOGS / f"{window}-server-{mode}.log"
    argv = [VENV_PY, PROTO_SERVER, "--engine", "strata", "--config", str(cfg_path), "--port", str(PROTO_PORT)]
    rec["log_start"] = log_start(cfg)          # provenance BEFORE the child appends a single byte
    t0 = time.time()
    with open(slog, "ab") as fh:      # start_new_session: its own process group, SIGTERM goes to ITS pid only
        proc = subprocess.Popen(argv, cwd=CLONE, env=env, stdout=fh, stderr=fh,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    # OWNED family tracked from the first tick: every current child of OUR proxy (the engine and
    # any vision process, including ones left behind by a startup failure) pinned by pid+ticks.
    rec.update({"server_pid": proc.pid, "server_ticks": W.start_ticks(proc.pid), "argv": argv,
                "server_log": str(slog), "cwd": CLONE,
                "family": {str(k): W.start_ticks(k) for k in W.children(proc.pid)}})
    note(f"{mode}: launched prototype proxy {proc.pid} (cwd {CLONE}, config {cfg_path.name}, port {PROTO_PORT})")
    deadline, cold = t0 + STARTUP_S, False
    while True:
        if proc.poll() is not None:                                  # early child exit: detected, recorded
            rec["startup"] = f"prototype proxy exited early (exit {proc.returncode}) after " \
                             f"{round(time.time() - t0, 1)}s - see {slog}"
            rec["family"].update({str(k): W.start_ticks(k) for k in W.children(proc.pid)})
            note(f"{mode}: {rec['startup']}")
            return False
        ok, detail = verify_proto(proc.pid, cfg, width, exe_want)
        if ok:
            eng = int(detail["engine"])
            rec["engine"] = eng                                      # top-level: the stop path reads rec["engine"]
            rec["family"].update({str(k): W.start_ticks(k) for k in W.children(proc.pid)})
            rec["env_proof"] = {k: v for k, v in W.masked(W.env_captured(eng)).items()
                                if k in ("STRATA_IQ_MT_MIN", "STRATA_ROUTE_TAIL_SKIP",
                                         "STRATA_VERIFY_COMPLEMENT")}
            rec.update({"startup_ok": True, "startup_seconds": round(time.time() - t0, 1),
                        "identity": {k: v for k, v in detail.items() if k != "engine_cmdline"},
                        "engine_cmdline": detail["engine_cmdline"],
                        "engine_ticks": W.start_ticks(eng),
                        "vision": {str(k): W.start_ticks(k) for k in W.children(proc.pid)
                                   if k != eng and "strata-vision" in W.cmdline(k)},
                        "rocm_proof": rocm_proof(eng)})
            note(f"{mode}: ready in {rec['startup_seconds']}s (health+model+{exe_want} exe+flags verified)")
            return True
        if time.time() >= deadline:
            if not cold:
                cold, deadline = True, time.time() + (COLD_S - STARTUP_S)
                rec["cold_load_extension"] = f"not ready after {STARTUP_S}s - one extension to {COLD_S}s total"
                note(f"{mode}: {rec['cold_load_extension']}")
            else:
                rec["startup"] = f"not verified within {COLD_S}s (cold extension included): {detail}"
                rec["family"].update({str(k): W.start_ticks(k) for k in W.children(proc.pid)})
                note(f"{mode}: {rec['startup']}")
                return False
        time.sleep(5)


def run_requests(mode: str, rec: dict, note, max_requests: int | None = None) -> bool:
    """Two short greedy requests, reasoning explicitly off, via the real bench client.
    max_requests=1 (the parity mode): a second request would reset the drafter and REWRITE
    the cycle-0 dump files with inconsistent leftover pool state, so the parity window
    captures exactly ONE diagnostic request."""
    sys.path.insert(0, f"{CLONE}/tools")
    try:
        from strata_bench import chat, digest        # the real chat client (temperature 0, thinking off)
    except ImportError as e:
        rec["requests_error"] = f"chat/digest helpers unavailable: {e!r}"
        return False
    base, ok = f"http://127.0.0.1:{PROTO_PORT}", True
    prompts = PROMPTS[:max_requests] if max_requests else PROMPTS
    for n, prompt in enumerate(prompts, 1):
        r = chat(base, "", [{"role": "user", "content": prompt}], MAX_TOKENS, think=False, timeout=REQ_TIMEOUT)
        d = digest(r)
        rec["requests"].append({"n": n, "prompt": prompt, "max_tokens": MAX_TOKENS, "greedy": True,
                                "reasoning": "off (reasoning_effort=none, enable_thinking=false)",
                                "ok": d.get("ok"), "err": r.get("err"), "ttft_s": d.get("ttft_s"),
                                "total_s": round(r.get("total") or 0, 3), "pieces": r.get("pieces"),
                                "usage": r.get("usage"), "timings": r.get("timings"),
                                "text": r.get("text")})
        note(f"{mode}: request {n} ok={d.get('ok')} err={str(r.get('err'))[:80]} "
             f"completion={(r.get('usage') or {}).get('completion_tokens')}")
        if not d.get("ok"):
            ok = False
    return ok


def draft_evidence(window: str, cfg: dict, mode: str, rec: dict) -> bool:
    """Engine-log evidence.  The per-window copy contains ONLY THE CURRENT SESSION's bytes:
    from the offset recorded immediately before THIS mode's launch to EOF.  The append-mode
    source log is read, never touched, and its EARLIER-WINDOW history is never copied into
    the evidence file, so evaluate() can only ever see this session's lines.  Provenance
    (byte range + inode) is recorded; an inode change mid-mode fails the evidence."""
    src, dst = pathlib.Path(cfg["log"]), LOGS / f"{window}-engine-{mode}.log"
    start = rec.get("log_start")
    if not start:
        rec["engine_log"] = {"source": str(src),
                             "provenance": "no pre-launch log offset recorded - evidence REFUSED"}
        return False
    try:
        st = src.stat()
    except OSError as e:
        rec["engine_log"] = {"source": str(src), "provenance": f"source log unreadable: {type(e).__name__}"}
        return False
    if start.get("inode") is not None and st.st_ino != start["inode"]:
        rec["engine_log"] = {"source": str(src),
                             "provenance": f"log inode changed during the mode ({start['inode']} -> "
                                           f"{st.st_ino}) - evidence REFUSED"}
        return False
    if st.st_size < start["bytes"]:
        rec["engine_log"] = {"source": str(src),
                             "provenance": f"log shrank during the mode ({start['bytes']} -> {st.st_size}) "
                                           "- evidence REFUSED"}
        return False
    try:
        with open(src, "rb") as fh:
            fh.seek(start["bytes"])
            session = fh.read()
        dst.write_bytes(session)             # the unique per-window copy: current session ONLY
    except OSError as e:
        rec["engine_log"] = {"source": str(src), "provenance": f"copy failed: {type(e).__name__}"}
        return False
    text = session.decode("utf-8", "replace")
    lines = [ln.strip() for ln in text.splitlines()]
    rec["engine_log"] = {"source": str(src), "per_window_copy": str(dst),
                         "start_bytes": start["bytes"], "end_bytes": st.st_size,
                         "session_bytes": len(session), "inode": st.st_ino,
                         "inode_was_absent_at_launch": start.get("absent", False),
                         "provenance": f"current session only: copy = source bytes [{start['bytes']}, "
                                       f"{st.st_size}) of the append-mode log (source untouched)"}
    rec["evidence"] = {"startup_lines": [ln[:300] for ln in lines if any(k in ln for k in DFLASH_ON)][:40],
                       "fallback_lines": [ln[:300] for ln in lines if OFF_RE.search(ln)][:40],
                       "acceptance": [{"accepted": int(a), "offered": int(b)}
                                      for a, b in ACCEPT_RE.findall(text)][-40:],
                       "window_counter_lines": [ln[:300] for ln in lines if "adaptive windows" in ln][:20]}
    return True


def evaluate(mode: str, rec: dict) -> list[str]:
    """Activation rules: plausible text is never evidence; a silent target-only fallback is a failure."""
    ev = rec.get("evidence") or {}
    geo = [ln for ln in ev.get("startup_lines", []) if "taps [" in ln]
    coll = [ln for ln in ev.get("startup_lines", []) if "cross-GPU tap collection serial" in ln]
    head = [ln for ln in ev.get("startup_lines", []) if "shared target head" in ln or "indexed shared head" in ln]
    vram = [ln for ln in ev.get("startup_lines", []) if "MiB of VRAM held" in ln]
    offered = sum(a["offered"] for a in ev.get("acceptance", []))
    fallback = [ln for ln in ev.get("fallback_lines", []) if "target-only decoding" in ln]
    watchdog = [ln for ln in ev.get("fallback_lines", [])
                if "timed out" in ln or "Unrecoverable native" in ln]
    bad: list[str] = []
    if str(mode).startswith("dflash_w"):        # L19: any DFlash width (dflash_w3's checks verbatim)
        if not geo:
            bad.append("no DFlash2 geometry line: the drafter never activated")
        if not coll:
            bad.append("no cross-GPU tap-collection serial line: the collector is not enabled")
        if not vram:
            bad.append("no drafter VRAM line")
        if fallback:
            bad.append("silent fallback to target-only in the engine log = MODE FAILURE")
        if offered == 0:
            bad.append("no draft was ever offered: draft activation unproven")
        rec["activation"] = {"geometry": geo[:3], "collector": coll[:3], "shared_head": head[:3],
                             "vram": vram[:3], "drafts_offered_total": offered,
                             "window_counters": ev.get("window_counter_lines", [])[:6]}
    else:
        if geo or coll:
            bad.append("target_only shows drafter lines - config/binary mismatch")
        if offered:
            bad.append(f"target_only offered {offered} drafts - not pure autoregressive")
        rec["activation"] = {"geometry": [], "collector": [], "shared_head": [], "vram": [],
                             "drafts_offered_total": offered, "window_counters": []}
    if watchdog:
        bad.append(f"{len(watchdog)} native-verification/watchdog line(s) in the engine log")
    return bad


def proto_family_gone(rec: dict) -> bool:
    """Port free, our proxy pid gone, EVERY recorded family pid (engine, vision, any child seen
    at launch - including survivors of a startup failure) gone by start ticks, and NO process
    anywhere whose exe is the prototype binary (never two full targets loaded at once)."""
    if W.listener_pid(PROTO_PORT) is not None:
        return False
    if W.same_proc(rec.get("server_pid", -1), rec.get("server_ticks", -1)):
        return False
    if W.same_proc(int(rec.get("engine", -1)), rec.get("engine_ticks", -1)):
        return False
    for pid, ticks in (rec.get("family") or {}).items():
        if W.same_proc(int(pid), ticks):
            return False
    for pid, ticks in (rec.get("vision") or {}).items():
        if W.same_proc(int(pid), ticks):
            return False
    for d in pathlib.Path("/proc").iterdir():
        if d.name.isdigit() and W.exe_of(int(d.name)) == PROTO_BINARY:
            return False
    return True


# --------------------------------------------------------------------- signal + survivors (L16 review)
class WindowSignal(BaseException):
    """SIGTERM/SIGINT arrive as this exception: the SAME cleanup path as any interruption -
    per-mode stop of OUR proxy, then the guarded restore.  BaseException on purpose: the
    per-mode handler records it and the window loop BREAKS - no further requests/modes."""


def install_signal_handlers() -> None:
    def _raise(signum, _frame):
        raise WindowSignal(f"signal {signal.Signals(signum).name} received")
    signal.signal(signal.SIGTERM, _raise)
    signal.signal(signal.SIGINT, _raise)


def proto_survivors(record: dict, extra_exes: tuple[str, ...] = ()) -> dict:
    """Live processes that belong to THIS window: any process whose exe is a launched mode's
    binary, the isolated port listener, and recorded family pids pinned by start ticks.
    Non-empty means the original production restore must NOT be attempted (no duplicate load)."""
    exes = {s.get("exe") for s in record.get("configs", []) if s.get("exe")} | set(extra_exes)
    pids = sorted(int(d.name) for d in pathlib.Path("/proc").iterdir()
                  if d.name.isdigit() and W.exe_of(int(d.name)) in exes)
    live = []
    for m in record.get("modes", []):
        for pid, ticks in (m.get("family") or {}).items():
            if W.same_proc(int(pid), ticks):
                live.append(int(pid))
    listener = W.listener_pid(PROTO_PORT)
    return {"exe_matches": pids, "recorded_family_alive": sorted(set(live)), "port_8019_listener": listener}


def proto_survivors_any(record: dict, extra_exes: tuple[str, ...] = ()) -> bool:
    s = proto_survivors(record, extra_exes)
    return bool(s["exe_matches"] or s["recorded_family_alive"] or s["port_8019_listener"])


def stop_mode(mode: str, rec: dict, note) -> bool:
    pid = rec.get("server_pid")
    if pid and W.same_proc(pid, rec.get("server_ticks", -1)):
        os.kill(pid, signal.SIGTERM)          # ITS prototype proxy pid only - never a global kill
        rec["mode_stop_sent"] = True
    deadline = time.time() + 120
    while time.time() < deadline and not proto_family_gone(rec):
        time.sleep(2)
    gone = proto_family_gone(rec)
    note(f"{mode}: prototype family gone={gone} (port {PROTO_PORT} + proxy/engine/vision pids + no prototype exe)")
    return gone


def run_model_window(snap: dict, record: dict, note, parity_base: str | None = None) -> None:
    if not W.stop_prod(snap, record, note):   # step 2: no GPU work while a verified descendant lives
        return
    env = os.environ.copy()
    env.update(snap["_env_full"])            # the live engine's OWN device/library flags (window.py's way)
    for mode, width in MODES:
        mode_env = env
        if parity_base and mode == "dflash_w3":
            # STRATA_DF_PARITY=<dir> + STRATA_DF_PARITY_CYCLE=0 (dflash_runtime.cpp:45-59: the
            # drafter's cycle counter STARTS at 0 (329, ++ at 1493), CYCLE=0 selects the FIRST
            # proposal cycle; the default 1 would select the second).  The dir is created here,
            # at execute time, for this mode only - a dry run never creates it.
            pd = parity_dir_for(record["window"], parity_base)
            pd.mkdir(parents=True, exist_ok=True)
            mode_env = dict(env, STRATA_DF_PARITY=str(pd), STRATA_DF_PARITY_CYCLE="0")
            rec_parity = {"dir": str(pd), "env": "STRATA_DF_PARITY=<dir> STRATA_DF_PARITY_CYCLE=0 (first cycle)"}
            record["parity"] = rec_parity
            note(f"dflash_w3: parity dumps -> {pd} (CYCLE=0: the first drafter cycle)")
        summary, problems = currency(mode, width, json.loads(pathlib.Path(W.PROD_CONFIG).read_text()))
        if problems:                          # config drifted mid-window: fail closed, restore
            record["modes"].append({"mode": mode, "mode_ok": False, "mode_failures": problems})
            record["break_reason"] = f"{mode}: config currency failed mid-window"
            note(record["break_reason"])
            return
        cfg = json.loads(pathlib.Path(summary["config"]).read_text())
        rec: dict = {"mode": mode, "width": width, "config": summary["config"], "requests": []}
        record["modes"].append(rec)
        try:
            if start_mode(record["window"], mode, cfg, width, mode_env, rec, note):
                parity_mode = bool(parity_base) and mode == "dflash_w3"
                requests_ok = run_requests(mode, rec, note, max_requests=1 if parity_mode else None)
                ev_ok = draft_evidence(record["window"], cfg, mode, rec)
                bad = evaluate(mode, rec) if ev_ok else ["engine log evidence unreadable"]
                if parity_mode and "parity" in record:
                    pf = parity_files(parity_dir_for(record["window"], parity_base))
                    record["parity"]["files"] = pf
                    dumps_ok = pf.get("exists") and pf.get("n_files", 0) > 0 and \
                        any(f["bytes"] > 0 for f in pf["files"])
                    if not dumps_ok:
                        bad.append(f"parity dump missing/empty at {pf['dir']} (n_files="
                                   f"{pf.get('n_files', 0)}) - mode FAILS: a successful mode with "
                                   "no dump input is a false pass")
                    note(f"dflash_w3: parity dumps {'ok' if dumps_ok else 'MISSING/EMPTY'} "
                         f"({pf.get('n_files', 0)} files in {pf['dir']})")
                if not requests_ok:
                    bad.append("a request failed (see the recorded error) - the log text is NOT correctness evidence")
                rec["mode_ok"], rec["mode_failures"] = not bad, bad
            else:
                rec["mode_ok"], rec["mode_failures"] = False, [rec.get("startup", "startup failed")]
        except BaseException as e:            # interruption is recorded, the stop+restore path still runs
            rec["mode_error"], rec["mode_ok"] = repr(e)[:300], False
            rec["mode_failures"] = [f"mode interrupted: {rec['mode_error']}"]
        note(f"{mode}: mode_ok={rec['mode_ok']} failures={rec.get('mode_failures')}")
        gone = stop_mode(mode, rec, note)     # step 5: fully stopped before the next mode / restore
        rec["family_gone"] = gone
        if not rec["mode_ok"] or not gone:
            record["break_reason"] = f"{mode}: mode_ok={rec['mode_ok']} family_gone={gone} - restoring now"
            note(record["break_reason"])
            return
    record["all_modes_ok"] = len(record["modes"]) == len(MODES) and all(m.get("mode_ok") for m in record["modes"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="PARENT-only: real model window (default DRY RUN)")
    ap.add_argument("--parity-dir", metavar="BASE", default=None,
                    help="dflash_w3 ONLY: compose STRATA_DF_PARITY=<BASE>/<window> + STRATA_DF_PARITY_CYCLE=0 "
                         "into the child env, create that dir at execute time, record it, and list the dump "
                         "files (names+sizes) after the run. Dry run never creates it. Typical BASE: "
                         f"{DUMPS}")
    a = ap.parse_args()

    window = f"model-window-{time.strftime('%Y%m%d-%H%M%S')}"   # unique: reruns never overwrite evidence
    LOGS.mkdir(parents=True, exist_ok=True)
    results, wlog = OPS / f"{window}-results.json", LOGS / f"{window}.log"
    note = lambda msg: W.note_to(wlog, msg)   # noqa: E731

    snap = W.snapshot()
    prod = json.loads(pathlib.Path(W.PROD_CONFIG).read_text())
    summaries, problems = [], []
    for mode, width in MODES:
        s, p = currency(mode, width, prod)
        summaries.append(s)
        problems += p
    checks, preflight_problems = preflight(prod)
    problems += preflight_problems
    print(json.dumps({"window": window, "production_snapshot": W.display(snap),
                      "prod_config_sha256": W.config_sha(), "configs": summaries,
                      "config_currency_problems": problems, "preflight": checks,
                      "port_8019_listener_now": W.listener_pid(PROTO_PORT),
                      "modes_in_order": [m for m, _ in MODES],
                      "parity": ({"base": a.parity_dir, "dump_dir_would_be": str(parity_dir_for(window, a.parity_dir)),
                                   "env": "STRATA_DF_PARITY=<dir> STRATA_DF_PARITY_CYCLE=0 (dflash_w3 only; "
                                           "created ONLY at execute)"}
                                  if a.parity_dir else "off (no dumps, behavior byte-identical to the L13 lane)"),
                      "startup_deadline_s": STARTUP_S, "cold_load_max_s": COLD_S,
                      "request_timeout_s": REQ_TIMEOUT, "max_new_tokens": MAX_TOKENS,
                      "greedy_reasoning_off": True,
                      "pipeline_window_finding": "generate.cpp:4183-4202 (auto needs --mtp, 4197) and "
                                                 "4214-4220 (explicit refused for dflash+split): both end at "
                                                 "pipeline_windows=0, the serial collector - NO extra flag, "
                                                 "NO extra config"}, indent=2))
    if not snap.get("verified"):
        print("REFUSE: " + snap.get("why", "snapshot failed"))
        return 2
    if problems:
        print("REFUSE: config currency / preflight problems above - regenerate configs with the plan tool "
              "(parent), never hand-edit them")
        return 2
    if not a.execute:
        print("DRY RUN only - zero signals, zero GPU, zero HTTP inference. Parent: rerun with --execute.")
        return 0

    record: dict = {"window": window, "started": time.strftime("%FT%T%z"), "snapshot": W.display(snap),
                    "prod_config_sha256": W.config_sha(), "configs": summaries,
                    "stop_sent": False, "modes": []}
    install_signal_handlers()               # SIGTERM/SIGINT -> WindowSignal -> recorded, cleanup, restore
    try:
        run_model_window(snap, record, note, a.parity_dir)
    except BaseException as e:               # SIGTERM/KeyboardInterrupt land HERE: restore honours stop_sent
        record["window_error"] = repr(e)[:300]
    finally:
        surv = proto_survivors(record)       # L16 review: never restore with a prototype survivor alive
        if surv["exe_matches"] or surv["recorded_family_alive"] or surv["port_8019_listener"]:
            record["restore"] = {"restored": False,
                                 "refused": f"prototype family survivors {json.dumps(surv)} - restore "
                                            "REFUSED (a duplicate full model load is never attempted); "
                                            "parent: confirm the recorded pids, clean them by their "
                                            "verified pids, re-check, then restore"}
        else:
            record["restore"] = W.restore_and_verify(snap, record, time.time() + 360)
        record["status"] = ("ALL OK" if record.get("all_modes_ok") and record["restore"].get("restored")
                            else "RESTORED; model runs failed - see logs" if record["restore"].get("restored")
                            else "RESTORE FAILED - PARENT INVESTIGATE NOW")
        W.note_to(wlog, f"WINDOW_FINAL {record['status']} restore={json.dumps(record['restore'])}")
        results.write_text(json.dumps(record, indent=2) + "\n")
        os.chmod(results, 0o600)            # snapshot embeds device env (masked) - owner-only
        print(json.dumps(record, indent=2))
    return 0 if record.get("all_modes_ok") and record["restore"].get("restored") else 1


if __name__ == "__main__":
    sys.exit(main())
