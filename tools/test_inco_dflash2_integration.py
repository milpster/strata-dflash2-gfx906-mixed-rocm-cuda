#!/usr/bin/env python3
"""Inco DFlash2 preflight + isolated-server validation (L9, parent executes everything).

What this IS:
  * A CONFIG CLONER: reads the ACTUAL production server config
    (/mnt/user/Strata/prod-config-swift15.json, read-only) and writes, under
    /mnt/user/dflash2/config/, (a) a PLAN MANIFEST (named manifest) and (b) VALID
    server-schema configs for the isolated prototype server on port 8019 - same schema
    fields as prod (exe/args/cwd/tokenizer/model_name/log/env/lib_dirs/vision),
    structurally merged flags (each flag exactly once), production config NEVER touched.
    Prototype logs are ABSOLUTE paths under /mnt/user/dflash2/logs/ - never relative
    paths that would create new files inside the production tree.
  * A REAL CLIENT for the isolated prototype server (--execute, parent-run): HTTP checks
    against the explicit prototype URL (port 8019; port 8009 - production - REFUSED),
    fail-closed guards (original production lineage observed RUNNING -> refuse; lineage
    UNKNOWN -> refuse; --confirm-execution required; NO override). Nothing here stops,
    restarts, signals, or probes production.
  * CPU unit fixtures (--selfcheck) for the cloner/merge/guards/RT-metadata (parent runs).

OPT-IN PATH OVERRIDES (integ-023; the parent's harness lane points the SAME reviewed cloner at
another engine tree and a NEW config dir, so nothing here hardcodes a worktree):
  STRATA_WINDOW_PROTO_BINARY  -> PROTO_BINARY (the cloned exe for every NEW-binary arm)
  STRATA_WINDOW_CONFIG_DIR    -> CONFIG_DIR  (where --plan writes manifest + clones)
  Absent/empty env = the byte-for-byte defaults below, so every earlier lane is unchanged.
  A RELATIVE override is REFUSED, never resolved against a cwd. PROD_BINARY (the explicitly OLD
  production arm) and PROD_CONFIG/LOG_DIR/ports are NEVER overridable, and the lineage/ownership
  guards keep scanning the ORIGINAL production paths - no guard is loosened by these overrides.

BASELINE FACT (parent-verified source + runtime logs, 2026-10-22; supersedes the earlier
"TARGET-ONLY" label, which was WRONG):
  * generate.cpp:778 suffix_draft DEFAULT 3; the flag --suffix-draft is only reassigned at
    2654; lookup_chain default 0 (784, flag 2656); use_mtp = !o.mtp.empty() (8344, 16456)
    with --mtp assigned only at 2599; sfx_on = suffix_draft>0 || lookup_chain>0
    (14338, 16711). Production args carry NO --mtp/--dflash, so MTP is off, but
    suffix_draft=3 is ACTIVE: production is a SUFFIX-LOOKUP baseline. Production logs
    repeat "suffix drafts: N windows, X of Y accepted". --spec 6 alone does NOT enable MTP.
  * Pure target-only (autoregressive) requires EXPLICIT --suffix-draft 0 (plus
    --lookup-chain 0) and --spec 2 - spec 1 is refused for a native (IQ) pack
    (generate.cpp:3685-3689); with no drafter the extra row is the inert oracle/0
    fill, so the output is pure AR. The prod-args-verbatim clone
    is therefore the new-binary SUFFIX-LOOKUP clone, named suffix_lookup.
  * READY MTP loader input (parent-verified, read-only, NO conversion/copy needed):
    /home/user/dev/Strata-data/mtp-bf16/rt/ (dense.txt 29 index lines, dense.bin
    ~116,099,072 B, experts.bin ~675 MiB, draft_vocab.bin ~415 KiB; dense hidden 2560,
    HC 10240 = 4 streams, 512 experts), built from mtp-bf16/mtp-q2_0.gguf (arch qwen4exp,
    31 tensors) by tools/mtp_rt.py; MtpDrafter consumes the rt dir directly. The Lama-format
    GGUF companions under ai/ai are NOT Strata-loadable. The Swift-1.5 target GGUF has no
    mtp tensors; RT geometry is compatible STATICALLY, but the ACTUAL GPU load / Swift
    acceptance is UNVALIDATED - label "READY-RT (runtime NOTRUN)", never "pack validated".
  * new_mtp uses --mtp <rt dir> --spec 6 --spec-min-p 0.7 --suffix-draft 0 (pure MTP, not
    mixed with suffix). The rt dir is READ-ONLY; no artifact copies.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import pathlib
import subprocess
import sys
import urllib.parse
import urllib.request


def _abs_override(env_name: str, default: str) -> str:
    """Opt-in ABSOLUTE path override, default-preserving: an absent/empty env returns the DEFAULT
    STRING BYTE-FOR-BYTE (every earlier lane keeps its literal paths). A relative value is REFUSED
    outright - it is never resolved against a cwd, so a typo can never write configs into a tree
    nobody selected. Same mechanism and same env names as ops/model_window.py, so the imported
    clone-provider and the controller always resolve the SAME binary and config dir."""
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        return default
    val = raw.strip()
    if not val.startswith("/"):
        raise ValueError(f"{env_name}={raw!r}: REFUSED - the override must be an ABSOLUTE path "
                         f"(default {default!r}); a relative override is never resolved against a cwd")
    return val


PROD_CONFIG = "/mnt/user/Strata/prod-config-swift15.json"
PROD_BINARY = "/mnt/user/Strata-2966/build-2966/strata"   # observed prod engine (pid cmdline evidence)
# The OLD production arm keeps PROD_BINARY always; only the NEW-binary arms follow the override.
PROTO_BINARY = _abs_override("STRATA_WINDOW_PROTO_BINARY",
                            "/mnt/user/dflash2/Strata-wt/build-gfx906-dflash2/strata")
ARTIFACT = "/mnt/user/dflash2/artifacts/inco-dflash2-bf16.gguf"
RT_DIR = "/home/user/dev/Strata-data/mtp-bf16/rt"         # ready MTP loader input (read-only)
CONFIG_DIR = pathlib.Path(_abs_override("STRATA_WINDOW_CONFIG_DIR", "/mnt/user/dflash2/config"))
LOG_DIR = "/mnt/user/dflash2/logs"                        # absolute: prototype logs NEVER land under prod CWD
PROTO_PORT = 8019
PROD_PORT = 8009
PROTO_URL_DEFAULT = f"http://127.0.0.1:{PROTO_PORT}"
WIDTHS = list(range(1, 8))

# Flags consumed ONLY by drafter paths (source-cited in the header): inert without a drafter.
DRAFTER_VERIFY_FLAGS = {"--spec": "2", "--spec-min-p": "2"}
VALUE_FLAGS = {"--suffix-draft": "2", "--lookup-chain": "2"}   # arity table for the merge walker


def load_prod_config(path: str = PROD_CONFIG) -> dict:
    cfg = json.loads(pathlib.Path(path).read_text())
    for key in ("exe", "args", "cwd", "tokenizer", "model_name", "log", "env"):
        if key not in cfg:
            raise ValueError(f"prod config: key {key} missing - schema drifted, refusing to clone blind")
    return cfg


def merge_flags(args: list[str], replace: dict[str, str], add: list[str]) -> list[str]:
    """Structural merge over the flag list: each flag appears exactly once; replacements
    update the existing position; additions append. No duplicated flag strings."""
    arity = {**DRAFTER_VERIFY_FLAGS, **VALUE_FLAGS}
    out, i = list(args), 0
    seen = set()
    while i < len(out):
        tok = out[i]
        if tok.startswith("--"):
            if tok in seen:
                raise ValueError(f"merge: flag {tok} appears twice in the source args")
            seen.add(tok)
            has_value = i + 1 < len(out) and not out[i + 1].startswith("--")
            if tok in replace:
                if not has_value:
                    raise ValueError(f"merge: cannot replace valueless flag {tok}")
                out[i + 1] = replace[tok]
            i += 2 if has_value else 1
        else:
            i += 1
    j = 0
    while j < len(add):
        flag = add[j]
        if flag in seen:
            raise ValueError(f"merge: refusing to add {flag} twice")
        seen.add(flag)
        if flag in arity:
            out.extend([flag, add[j + 1]])
            j += 2
        else:
            out.append(flag)
            j += 1
    return out


def clone_server_config(mode: str, prod: dict) -> dict:
    """A VALID server-schema config clone for the isolated prototype (parent starts it)."""
    cfg = copy.deepcopy(prod)
    cfg["exe"] = PROTO_BINARY
    cfg["log"] = f"{LOG_DIR}/engine-prototype-{mode}.log"   # ABSOLUTE - isolation from the prod tree
    # server.py derives engine flags from STRUCTURED keys (layer_split -> --layer-split,
    # server.py:2604): every such key is preserved verbatim by the deep copy; only the port
    # moves, so the isolated prototype can never collide with production's 8009.
    cfg["port"] = PROTO_PORT
    if mode == "suffix_lookup":
        pass  # prod args verbatim = SUFFIX-LOOKUP baseline on the new binary (suffix_draft
              # default 3, generate.cpp:778 - observed in production logs)
    elif mode == "target_only":
        # --spec 2, not 1: a native (IQ) pack refuses spec < 2 (generate.cpp:3685-3689).
        # With every drafter off the window's extra rows come from the oracle/0 fill
        # (generate.cpp:16772-16779) and verify commits only target-argmax rows, so the
        # OUTPUT is pure autoregressive; the second row is inert ballast, not a drafter.
        cfg["args"] = merge_flags(prod["args"], {"--spec": "2"},
                                  ["--suffix-draft", "0", "--lookup-chain", "0"])
    elif mode == "old_target_only":
        cfg["exe"] = PROD_BINARY            # old binary, pure AR - for the old-vs-new pure-AR
        cfg["log"] = f"{LOG_DIR}/engine-oldbinary-target-only.log"
        cfg["args"] = merge_flags(prod["args"], {"--spec": "2"},
                                  ["--suffix-draft", "0", "--lookup-chain", "0"])
    elif mode == "mtp":
        cfg["args"] = merge_flags(prod["args"],
                                  {"--spec": "6", "--spec-min-p": "0.7"},
                                  ["--mtp", RT_DIR, "--suffix-draft", "0"])
    elif mode.startswith("dflash_w"):
        w = int(mode.removeprefix("dflash_w"))
        if not 1 <= w <= 7:
            raise ValueError(f"mode {mode}: widths run 1..7 (--spec w+1 <= kVerifyMaxT 8)")
        cfg["args"] = merge_flags(prod["args"], {"--spec": str(w + 1), "--spec-min-p": "0.5"},
                                  ["--dflash", ARTIFACT, "--dflash-block", str(w), "--suffix-draft", "0"])
    else:
        raise ValueError(f"mode {mode}: unknown mode - not emitting configs for invented labels")
    return cfg


def validate_rt_dir(rt: str = RT_DIR) -> list[str]:
    """Cheap, header-only MTP loader-input validation (NO large reads; prod-up safe):
    required files exist; dense.txt lines parse as name kind rows cols offset bytes with
    kind in {q8_0,bf16,f32}; declared byte counts match the kind's element formula;
    spans are contiguous from 0; the last span ends within dense.bin's size. Returns the
    list of problems (empty = metadata contract holds). Runtime stays NOTRUN regardless."""
    problems: list[str] = []
    root = pathlib.Path(rt)
    for f in ("dense.txt", "dense.bin", "experts.bin", "draft_vocab.bin"):
        if not (root / f).is_file():
            problems.append(f"missing file {f}")
    if problems:
        return problems
    dense_size = (root / "dense.bin").stat().st_size
    offset = 0
    for n, line in enumerate((root / "dense.txt").read_text().splitlines(), 1):
        parts = line.split()
        if len(parts) != 6:
            problems.append(f"line {n}: expected 'name kind rows cols offset bytes', got {line!r}")
            continue
        name, kind, rows, cols, off, size = parts[0], parts[1], *map(int, parts[2:])
        if kind not in ("q8_0", "bf16", "f32"):
            problems.append(f"line {n} {name}: kind {kind!r} not in q8_0|bf16|f32")
            continue
        per = {"q8_0": rows * (cols // 32) * 34, "bf16": rows * cols * 2, "f32": rows * cols * 4}[kind]
        if kind == "q8_0" and cols % 32:
            problems.append(f"line {n} {name}: q8_0 cols {cols} not a multiple of 32")
        if per != size:
            problems.append(f"line {n} {name}: declared {size} B != {kind} formula {per} B")
        if off != offset:
            problems.append(f"line {n} {name}: offset {off} != contiguous {offset}")
        if off + size > dense_size:
            problems.append(f"line {n} {name}: span {off}+{size} exceeds dense.bin size {dense_size}")
        offset = off + size
    if offset > dense_size:
        problems.append(f"index spans total {offset} exceed dense.bin size {dense_size}")
    return problems


def plan_manifest(prod: dict) -> dict:
    modes = {
        "prod_reference": {"kind": "SUFFIX LOOKUP (observed: suffix_draft default 3, generate.cpp:778; "
                                      "runtime logs 'suffix drafts: N windows, X of Y accepted')",
                           "why": "no --mtp/--dflash in prod args, so MTP is OFF (use_mtp=!o.mtp.empty(), "
                                  "8344), but suffix_draft=3 keeps suffix lookup ACTIVE (sfx_on 14338/16711). "
                                  "It is NOT target-only and NOT MTP.",
                           "exe": prod["exe"], "config": "production config as-is"},
        "new_suffix_lookup": {"kind": "clone, prod args verbatim = new-binary SUFFIX-LOOKUP baseline",
                              "exe": PROTO_BINARY, "config": "proto-server-swift15-suffix_lookup.json"},
        "new_target_only": {"kind": "PURE autoregressive: --suffix-draft 0 --lookup-chain 0 --spec 2 "
                                     "(spec 2 because a native IQ pack refuses spec<2, generate.cpp:3685; "
                                     "no-drafter rows are the oracle/0 fill, 16772-16779 - output is AR)",
                            "exe": PROTO_BINARY, "config": "proto-server-swift15-target_only.json"},
        "old_target_only": {"kind": "PURE autoregressive on the OLD prod binary - old-vs-new pure-AR "
                                     "correctness gate", "exe": PROD_BINARY,
                            "config": "proto-server-swift15-old_target_only.json"},
        "new_mtp": {"kind": "READY-RT (config valid; runtime NOTRUN)", "why": "loader input "
                    f"{RT_DIR} exists (dense.txt 29 lines, dense.bin, experts.bin, draft_vocab.bin; "
                    "tools/mtp_rt.py contract); MtpDrafter consumes it directly, no conversion. Geometry "
                    "compatible STATICALLY; ACTUAL GPU load + Swift acceptance UNVALIDATED - never label "
                    "the pack validated. Args: --mtp <rt dir> --spec 6 --spec-min-p 0.7 --suffix-draft 0 "
                    "(pure MTP, suffix not mixed). The lama-format GGUFs under ai/ai are NOT Strata-loadable.",
                    "exe": PROTO_BINARY, "config": "proto-server-swift15-mtp.json"},
    }
    for w in WIDTHS:
        modes[f"new_dflash_w{w}"] = {
            "kind": f"clone + --dflash + --spec {w + 1} --dflash-block {w} --suffix-draft 0 (pure DFlash)",
            "why": "width=min(block,spec-1) (generate.cpp:5500); trained block 8 (5482); spec<=8 "
                   "(5445-5454, kVerifyMaxT 8); suffix off so the drafter effect is isolated",
            "exe": PROTO_BINARY, "config": f"proto-server-swift15-dflash_w{w}.json"}
    return {
        "manifest": True,
        "note": "This is a PLAN MANIFEST, not a server config. Valid cloned configs are the "
                "proto-server-*.json files written beside it. Every mode has its OWN config file; "
                "no mode borrows the width-3 file.",
        "observed_rules": [
            "baseline = SUFFIX LOOKUP (source + runtime logs); prod config and args stay EXACTLY unchanged",
            "pure target-only needs --suffix-draft 0 --lookup-chain 0 --spec 1; verbatim prod args are suffix-lookup, never label them target-only",
            "MTP mode = READY-RT: config valid, runtime NOTRUN; no pack-validated claims",
            "sampled requests: CLI target-only fallback (generate.cpp:2830); serve sampled keeps target-only decoding with an explicit line (12457) - never claim an active drafter for sampled",
            "serve --batch + dflash refused (2836); --pipeline-windows + dflash + split refused (4214)",
            "no script stops/restarts production; lineage detection failure is fail-closed",
            "isolated prototype ONLY on port 8019; port 8009 (production) is refused outright",
            "prototype logs are absolute under /mnt/user/dflash2/logs - never under the production tree",
        ],
        "cases": [
            {"case": "health+identity", "setup": "GET /health, GET /props, GET /v1/models",
             "expected": "server model_name/model_path match the cloned config", "status": "PARENT-RUN (--execute)"},
            {"case": "repeat-conversation", "setup": "same greedy prompt twice (per-mode config)",
             "expected": "second request reuses prefix (engine-reported prompt_tokens), no slot-reset errors",
             "status": "PARENT-RUN (--execute)"},
            {"case": "reset-new-conversation", "setup": "second unrelated prompt",
             "expected": "clean new-conversation start (no 'rows disagree' refusals)", "status": "PARENT-RUN (--execute)"},
            {"case": "sampled-fallback", "setup": "temperature>0 request",
             "expected": "explicit target-only fallback line in the PROTOTYPE log (read-only grep after)",
             "status": "PARENT-RUN (--execute)"},
            {"case": "cancel-restore", "setup": "tools/cancel_park_test.py + tools/dflash_cache.py against 8019",
             "expected": "park/restore/cancel on the isolated server only", "status": "PARENT-RUN (GPU window)"},
        ],
        "modes": modes,
    }


def write_artifacts(prod: dict) -> dict[str, str]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    pathlib.Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    paths = {}
    manifest = CONFIG_DIR / "inco-dflash2-plan-manifest.json"
    manifest.write_text(json.dumps(plan_manifest(prod), indent=2) + "\n")
    paths["manifest"] = str(manifest)
    modes = ["suffix_lookup", "target_only", "old_target_only", "mtp"] + [f"dflash_w{w}" for w in WIDTHS]
    for mode in modes:
        cfg = clone_server_config(mode, prod)
        p = CONFIG_DIR / f"proto-server-swift15-{mode}.json"
        p.write_text(json.dumps(cfg, indent=2) + "\n")
        paths[mode] = str(p)
    if prod["args"] == clone_server_config("suffix_lookup", prod)["args"]:
        paths["identity_note"] = ("suffix_lookup clone keeps prod args verbatim (byte-identical "
                                  "SUFFIX-LOOKUP baseline); target_only is the pure-AR variant")
    return paths


def _cmdline(pid: str) -> str | None:
    try:
        return pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return None


def prod_status() -> str:
    """Lineage-aware production detection (READ-ONLY /proc scans; nothing is signalled).
    'running'  = ORIGINAL production lineage: server.py driven by prod-config-swift15.json
                  (the 8009 proxy) and/or an engine whose parent chain is that server.
    'isolated-8019' = only parent-controlled isolated servers (config under
                  /mnt/user/dflash2/config + --port 8019) - allowed for --execute.
    'absent'   = no engine and no server lineage found.
    'unknown'  = anything unreadable/mixed -> fail-closed refusal.
    The old bare pgrep -f build-2966/strata CONFLATED an isolated old-binary run on 8019
    with production; lineage (config file + port in the server parent's cmdline) fixes that.
    OWNERSHIP GUARD, NEVER OVERRIDABLE: an isolated server is recognized ONLY by a config path
    under /mnt/user/dflash2/config/ plus --port 8019. A STRATA_WINDOW_CONFIG_DIR override stays
    inside that prefix (e.g. /mnt/user/dflash2/config/integ-023) and is therefore covered; a
    config dir outside it is 'unknown' = fail-closed refusal, never an accepted lineage."""
    try:
        pids = [p.name for p in pathlib.Path("/proc").iterdir() if p.name.isdigit()]
    except OSError:
        return "unknown"
    prod_servers, isolated_servers, engines = [], [], []
    for pid in pids:
        cmd = _cmdline(pid)
        if cmd is None:
            continue
        if "serve/server.py" in cmd:
            if "prod-config-swift15.json" in cmd:
                prod_servers.append(pid)
            elif "/mnt/user/dflash2/config/" in cmd and f"--port {PROTO_PORT}" in cmd:
                isolated_servers.append(pid)
        if "build-2966/strata" in cmd and "--serve" in cmd:
            engines.append(pid)
    if prod_servers:
        return "running"
    if not engines:
        return "absent"
    # Engines exist: their controlling server decides lineage. An engine whose parent server
    # cmdline is unreadable/unknown is 'unknown' (fail-closed) - never assume isolation.
    for pid in engines:
        try:
            fields = pathlib.Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()
            ppid = fields[1]
        except (OSError, IndexError):
            return "unknown"
        parent = _cmdline(ppid)
        if parent is None:
            return "unknown"
        if "serve/server.py" not in parent:
            return "unknown"
        if "prod-config-swift15.json" in parent:
            return "running"
        if not ("/mnt/user/dflash2/config/" in parent and f"--port {PROTO_PORT}" in parent):
            return "unknown"
    return "isolated-8019" if isolated_servers else "unknown"


def http_json(url: str, payload: dict | None = None, timeout: float = 120.0) -> dict:
    req = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="GET" if payload is None else "POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def guard_execute(url: str, confirm: bool) -> str | None:
    """Returns None when allowed, else the refusal reason. NO override of detection exists.
    The guard NEVER stops anything; 'running' (production lineage) simply refuses."""
    parsed = urllib.parse.urlparse(url)
    if parsed.port == PROD_PORT:
        return f"REFUSED: port {PROD_PORT} is production; the harness talks ONLY to the isolated prototype ({PROTO_PORT})"
    if parsed.port != PROTO_PORT:
        return f"REFUSED: --execute requires the explicit isolated prototype URL on port {PROTO_PORT}"
    if not confirm:
        return "REFUSED: --execute requires --confirm-execution (explicit acknowledgement)"
    status = prod_status()
    if status in ("running", "unknown"):
        return (f"REFUSED: production lineage detection = {status}. The harness never stops/restarts "
                "production; GPU execution needs the user-approved downtime with ORIGINAL production "
                "OBSERVED ABSENT (isolated 8019 servers are allowed; unknown lineage is fail-closed; "
                "there is no override flag)")
    return None


def execute_checks(url: str, identity_cfg: str, results_path: str | None = None) -> int:
    """Real HTTP checks against the isolated prototype (parent-run; cases are claimed ONLY
    when they executed). No GPU, no production - production was observed absent by the guard.

    L22 strengthening (narrow, guards untouched): identity is the EXACT props.model_alias and
    props.model_path from --native (clones carry --native, never --model), with no None-as-success
    when the config names the field; every short test states thinking OFF explicitly
    (reasoning_effort=none + chat_template_kwargs.enable_thinking=false, the shape strata_bench
    uses); every case emits a FULL structured record (label, ok/error, complete usage, returned
    text) to stdout and to the optional JSON result path; repeat-2 must report
    usage.prompt_tokens_details.cached_tokens > 0 and the unrelated reset-new must report
    cached_tokens == 0 (the field serve/server.py:5335 emits from the engine's reused counter);
    the cold repeat-1 is never the reset witness; all FOUR requests must COMPLETED, not merely
    print; the sampled case keeps temperature > 0 and its expected target-only fallback
    (generate.cpp:12499, stderr -> engine log via serve/server.py:998) is proven from the
    PROTOTYPE session log by the parent/controller, never claimed here."""
    fails = 0
    try:
        health = http_json(f"{url}/health")
        print(f"health: {json.dumps(health)[:200]}")
    except OSError as e:
        print(f"FAIL: /health at {url}: {e} (is the parent's prototype server up on {PROTO_PORT}?)")
        return 1
    try:
        want = json.loads(pathlib.Path(identity_cfg).read_text())
    except (OSError, ValueError) as e:
        print(f"FAIL: identity config unreadable {identity_cfg}: {e}")
        return 1
    a = want.get("args", [])
    want_pack = a[a.index("--native") + 1] if "--native" in a else (
        a[a.index("--model") + 1] if "--model" in a else None)
    model = want.get("model_name") or "swift-1.5-iq3_xxs"
    cases: list[dict] = []

    def emit(case: dict) -> None:
        cases.append(case)
        print(json.dumps(case, ensure_ascii=False)[:4000], flush=True)   # FULL usage + text

    try:
        props = http_json(f"{url}/props")
        ok_model = props.get("model_alias") == want.get("model_name")    # exact: a missing alias FAILS
        ok_pack = want_pack is None or props.get("model_path") == want_pack   # exact when named
        if ok_model and ok_pack:
            print(f"identity: model_alias/model_path match the cloned config "
                  f"({props.get('model_alias')!r}, {props.get('model_path')!r})")
        else:
            print(f"FAIL: /props {props.get('model_alias')!r}/{props.get('model_path')!r} does not match "
                  f"cloned {want.get('model_name')!r}/{want_pack!r} (exact match required, no None-as-success)")
            fails += 1
    except OSError as e:
        print(f"FAIL: /props identity check: {e}")
        fails += 1

    def request(label: str, content: str, temperature: float, max_tokens: int) -> bool:
        body = {"model": model, "messages": [{"role": "user", "content": content}],
                "max_tokens": max_tokens, "temperature": temperature, "stream": False,
                "reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False}}
        try:
            resp = http_json(f"{url}/v1/chat/completions", body)
            usage = resp.get("usage") or {}
            text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content")
            emit({"label": label, "ok": True, "err": None, "temperature": temperature,
                  "thinking": "off (reasoning_effort=none, enable_thinking=false)",
                  "usage": usage, "text": text})
            return True
        except (OSError, ValueError) as e:
            emit({"label": label, "ok": False, "err": f"{type(e).__name__}: {e}"[:300],
                  "temperature": temperature, "usage": None, "text": None})
            return False

    prompt = "Explain, in two short sentences, why layer splitting moves K/V pools per stage."
    for label, content in (("repeat-1", prompt), ("repeat-2", prompt), ("reset-new", "Count from 1 to 5.")):
        if not request(label, content, 0.0, 64):
            fails += 1
    if not request("sampled", "Say hi.", 1.0, 8):        # sampled keeps temperature > 0 on purpose
        fails += 1
    by = {c["label"]: c for c in cases}

    def cached(label: str) -> int | None:
        det = ((by.get(label) or {}).get("usage") or {}).get("prompt_tokens_details")
        v = det.get("cached_tokens") if isinstance(det, dict) else None
        return v if isinstance(v, int) else None

    four = all(l in by and by[l]["ok"] for l in ("repeat-1", "repeat-2", "reset-new", "sampled"))
    if not four:
        print(f"FAIL: not all four requests COMPLETED: {[(c['label'], c['ok']) for c in cases]} "
              "- completion is asserted, not printing")
        fails += 1
    v2, vz = cached("repeat-2"), cached("reset-new")     # repeat-1 is cold by nature: never the witness
    if v2 is None:
        print("FAIL repeat-2: no integer usage.prompt_tokens_details.cached_tokens - repeat reuse NOT proven")
        fails += 1
    elif v2 <= 0:
        print(f"FAIL repeat-2: cached_tokens {v2} is not > 0 - repeat reuse NOT proven")
        fails += 1
    if vz is None:
        print("FAIL reset-new: no integer usage.prompt_tokens_details.cached_tokens - clean reset NOT proven")
        fails += 1
    elif vz != 0:
        print(f"FAIL reset-new: cached_tokens {vz} is not 0 - not a clean new conversation")
        fails += 1
    print("sampled: temperature > 0 kept; its expected target-only fallback line (generate.cpp:12499, "
          "stderr -> engine log) is proven from the PROTOTYPE session log by the parent/controller - "
          "this tool claims nothing about it")
    summary = {"url": url, "identity_config": identity_cfg, "model": model, "pack_want": want_pack,
               "cases": cases, "completed": sum(1 for c in cases if c["ok"]),
               "repeat_cached": v2, "reset_cached": vz, "fails": fails}
    if results_path:
        pathlib.Path(results_path).write_text(json.dumps(summary, indent=2) + "\n")
        print(f"structured per-case results written: {results_path}")
    print("Claim discipline: only the cases above were attempted against the isolated server; deeper "
          "cancel/park/restore gates run via tools/cancel_park_test.py + tools/dflash_cache.py (parent).")
    return 1 if fails else 0


def selfcheck() -> int:
    fails = 0

    def check(ok: bool, what: str) -> None:
        nonlocal fails
        print(f"{'ok  ' if ok else 'FAIL'} {what}")
        if not ok:
            fails += 1
    try:
        prod = load_prod_config()
    except (OSError, ValueError) as e:
        print(f"FAIL: prod config load: {e}")
        return 1
    suffix = clone_server_config("suffix_lookup", prod)
    check(suffix["exe"] == PROTO_BINARY, "clone swaps the engine binary only")
    check(all(k in suffix for k in ("args", "cwd", "tokenizer", "model_name", "log", "env", "vision")),
          "clone preserves every server-schema field (args/cwd/tokenizer/model/env/vision)")
    check(suffix["args"] == prod["args"],
          "suffix_lookup clone keeps prod args verbatim = byte-identical SUFFIX-LOOKUP baseline "
          "(suffix_draft default 3, generate.cpp:778 - NOT target-only)")
    check(suffix.get("layer_split") == prod.get("layer_split"),
          "structured layer_split key preserved verbatim (server.py:2604 consumes it; value not hardcoded here)")
    check(suffix.get("port") == PROTO_PORT, "prototype config moved to port 8019 (never 8009)")
    check(all(clone_server_config(m, prod)["log"].startswith(LOG_DIR + "/")
              for m in ("suffix_lookup", "target_only", "old_target_only", "mtp", "dflash_w3")),
          f"prototype logs are ABSOLUTE under {LOG_DIR}/ - relative logs under prod CWD can never "
          "create files inside the production tree (baseline isolation)")
    ar = clone_server_config("target_only", prod)["args"]
    check(ar[ar.index("--suffix-draft") + 1] == "0" and ar[ar.index("--lookup-chain") + 1] == "0"
          and ar[ar.index("--spec") + 1] == "2" and "--mtp" not in ar and "--dflash" not in ar,
          "pure target-only = --suffix-draft 0 --lookup-chain 0 --spec 2 (native pack floor), no MTP/DFlash")
    old = clone_server_config("old_target_only", prod)
    check(old["exe"] == PROD_BINARY and old["args"] == clone_server_config("target_only", prod)["args"],
          "old_target_only = OLD binary + identical pure-AR flags (clean old-vs-new correctness gate)")
    mtp = clone_server_config("mtp", prod)["args"]
    check(mtp.count("--mtp") == 1 and mtp[mtp.index("--mtp") + 1] == RT_DIR
          and mtp[mtp.index("--suffix-draft") + 1] == "0" and mtp[mtp.index("--spec") + 1] == "6"
          and "--dflash" not in mtp,
          "mtp config = --mtp <ready rt dir> --spec 6 --spec-min-p 0.7 --suffix-draft 0 (pure MTP, "
          "no mixed suffix, no artifact copies)")
    for w in WIDTHS:
        a = clone_server_config(f"dflash_w{w}", prod)["args"]
        check(a.count("--spec") == 1 and a[a.index("--spec") + 1] == str(w + 1)
              and a.count("--dflash-block") == 1 and a[a.index("--dflash-block") + 1] == str(w)
              and a.count("--dflash") == 1 and a[a.index("--suffix-draft") + 1] == "0"
              and a[a.index("--spec-min-p") + 1] == "0.5",
              f"dflash_w{w}: exactly one --spec {w + 1}, one --dflash-block {w}, suffix off, minp 0.5")
    problems = validate_rt_dir()
    check(problems == [], f"ready RT dir metadata ({RT_DIR}): {problems[:2] if problems else 'dense.txt 29 spans contiguous, kinds+sizes within dense.bin'}")

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tdp = pathlib.Path(td)
        (tdp / "dense.txt").write_text("a.weight bf16 2 4 0 16\nb.weight f32 1 2 16 8\n")
        (tdp / "dense.bin").write_bytes(b"\0" * 24)
        (tdp / "experts.bin").write_bytes(b"\0")
        (tdp / "draft_vocab.bin").write_bytes(b"\0")
        check(validate_rt_dir(str(tdp)) == [], "fixture: valid small rt dir validates clean (contiguous spans, kind formulas)")
        (tdp / "dense.bin").write_bytes(b"\0" * 20)
        check(bool(validate_rt_dir(str(tdp))), "fixture: truncated dense.bin -> span exceeds file bounds")
        (tdp / "dense.bin").write_bytes(b"\0" * 24)
        (tdp / "dense.txt").write_text("a.weight bf16 2 4 0 16\nb.weight f32 1 2 20 8\n")
        check(bool(validate_rt_dir(str(tdp))), "fixture: non-contiguous index span refused")
        (tdp / "dense.txt").write_text("a.weight q4_k 2 4 0 16\nb.weight f32 1 2 16 8\n")
        check(bool(validate_rt_dir(str(tdp))), "fixture: unknown kind refused (only q8_0|bf16|f32 in the rt contract)")
        (tdp / "dense.txt").unlink()
        check(any("dense.txt" in p for p in validate_rt_dir(str(tdp))), "fixture: missing required rt file refused")
    r = guard_execute(f"http://127.0.0.1:{PROD_PORT}", True)
    check(r is not None and "production" in r, "guard: port 8009 (production) refused outright")
    r = guard_execute(PROTO_URL_DEFAULT, False)
    check(r is not None, "guard: --execute without --confirm-execution refused")
    orig = globals()["prod_status"]
    for status, allowed in (("running", False), ("unknown", False), ("isolated-8019", True), ("absent", True)):
        globals()["prod_status"] = lambda s=status: s
        r = guard_execute(PROTO_URL_DEFAULT, True)
        check((r is None) == allowed, f"guard: lineage '{status}' -> {'allow' if allowed else 'REFUSE'} (fail-closed)")
    globals()["prod_status"] = orig
    m = plan_manifest(prod)
    check(m.get("manifest") is True and m["modes"]["prod_reference"]["kind"].startswith("SUFFIX LOOKUP"),
          "manifest: prod_reference labeled SUFFIX LOOKUP (observed source+runtime), NOT target-only, NOT MTP")
    check(m["modes"]["new_mtp"]["kind"] == "READY-RT (config valid; runtime NOTRUN)",
          "new_mtp = READY-RT with real loader path; runtime NOTRUN; no placeholder, no pack-validated claim")
    check(m["modes"]["new_target_only"]["kind"].startswith("PURE autoregressive"),
          "target_only labeled PURE AR with the explicit suffix-off flags")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--plan", action="store_true", help="write manifest + valid cloned configs, print plan")
    ap.add_argument("--preflight-mtp", action="store_true", help="cheap RT metadata check (no large reads)")
    ap.add_argument("--execute", action="store_true", help="PARENT-run real checks against the isolated prototype")
    ap.add_argument("--url", default=PROTO_URL_DEFAULT)
    ap.add_argument("--identity-config", default=None)
    ap.add_argument("--confirm-execution", action="store_true")
    ap.add_argument("--results-json", default=None,
                    help="optional path for the FULL structured per-case results of --execute "
                         "(label, ok/error, complete usage, returned text); nothing is claimed from it here")
    a = ap.parse_args()
    if a.selfcheck:
        return selfcheck()
    if a.preflight_mtp:
        problems = validate_rt_dir()
        print(json.dumps({"rt_dir": RT_DIR, "problems": problems,
                          "note": "metadata contract only (stat + dense.txt spans); NO large reads; "
                                  "runtime load/Swift acceptance remains NOTRUN"}, indent=2))
        return 1 if problems else 0
    prod = load_prod_config()
    if a.plan:
        paths = write_artifacts(prod)
        print(json.dumps({"written": paths, "prod_lineage_observed": prod_status(),
                          "mtp_rt_metadata": validate_rt_dir()[:3],
                          "note": "manifest is a manifest; proto-server-*.json are valid server-schema "
                                  "clones for the parent to start on port 8019 (one file per mode); "
                                  "prod config untouched"}, indent=2))
        print("parent start line (isolated ONLY, parent executes nothing now): cd /mnt/user/Strata && "
              f".venv/bin/python serve/server.py --config {paths['dflash_w3']} --port {PROTO_PORT}")
        return 0
    if a.execute:
        reason = guard_execute(a.url, a.confirm_execution)
        if reason is not None:
            print(reason)
            return 2
        identity = a.identity_config or str(CONFIG_DIR / "proto-server-swift15-dflash_w3.json")
        return execute_checks(a.url, identity, a.results_json)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
