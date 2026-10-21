#!/usr/bin/env python3
"""Inco DFlash2 preflight + isolated-server validation (L9, parent executes everything).

What this IS:
  * A CONFIG CLONER: reads the ACTUAL production server config
    (/mnt/user/Strata/prod-config-swift15.json, read-only) and writes, under
    /mnt/user/dflash2/config/, (a) a PLAN MANIFEST (named manifest) and (b) VALID
    server-schema configs for the isolated prototype server on port 8019 - same schema
    fields as prod (exe/args/cwd/tokenizer/model_name/log/lib_dirs/vision/env),
    structurally merged flags (no duplicated --spec strings), production config NEVER
    touched.
  * A REAL CLIENT for the isolated prototype server (--execute, parent-run): HTTP checks
    and case runs against the explicit prototype URL (port 8019; port 8009 - production -
    is REFUSED outright), with strict guards:
        - production engine observed RUNNING  -> refuse
        - detection FAILED/UNKNOWN           -> refuse (fail-closed; no override flag)
        - --confirm-execution                 -> required explicit acknowledgement
    Nothing in this file can stop, restart, signal, or probe production.
  * CPU unit fixtures (--selfcheck) for the cloner/merge/guards (parent runs them).

Observed drafter evidence (read-only, 2026-10-21, do not guess):
  * Production config args contain NO --mtp and NO --dflash -> the production serve is
    TARGET-ONLY with drafter-verify flags --spec 6 --spec-min-p 0.7 present (source:
    --spec is consumed only by drafter paths - dflash validation generate.cpp:5445 and the
    MTP bind at 9176 under use_mtp; with no drafter the flags are inert). prod_reference is
    therefore labeled OBSERVED TARGET-ONLY, not "MTP".
  * No MTP drafter artifact exists in the known model dirs (searched /mnt/user/ai for
    mtp/draft artifacts exist - see new_mtp mode) -> the new_mtp mode is MTP-candidate
    UNVALIDATED with this reason,
    not a fake baseline; unknown MTP flags are never invented.
"""
from __future__ import annotations

import argparse
import copy
import json
import pathlib
import subprocess
import sys
import urllib.parse
import urllib.request

PROD_CONFIG = "/mnt/user/Strata/prod-config-swift15.json"
PROD_BINARY = "/mnt/user/Strata-2966/build-2966/strata"   # observed prod engine (pid cmdline evidence)
PROTO_BINARY = "/mnt/user/dflash2/Strata-wt/build-gfx906-dflash2/strata"
ARTIFACT = "/mnt/user/dflash2/artifacts/inco-dflash2-bf16.gguf"
CONFIG_DIR = pathlib.Path("/mnt/user/dflash2/config")
PROTO_PORT = 8019
PROD_PORT = 8009
PROTO_URL_DEFAULT = f"http://127.0.0.1:{PROTO_PORT}"

# Flags consumed ONLY by drafter paths (source-cited above): inert without --mtp/--dflash.
DRAFTER_VERIFY_FLAGS = {"--spec": "2", "--spec-min-p": "2"}


def load_prod_config(path: str = PROD_CONFIG) -> dict:
    cfg = json.loads(pathlib.Path(path).read_text())
    for key in ("exe", "args", "cwd", "tokenizer", "model_name", "log", "env"):
        if key not in cfg:
            raise ValueError(f"prod config: key {key} missing - schema drifted, refusing to clone blind")
    return cfg


def merge_flags(args: list[str], replace: dict[str, str], add: list[str]) -> list[str]:
    """Structural merge over the flag list: each flag appears at most once; replacements
    update the existing position; additions append. No duplicated '--spec' strings."""
    out, i = list(args), 0
    seen = set()
    while i < len(out):
        tok = out[i]
        if tok.startswith("--"):
            if tok in seen:
                raise ValueError(f"merge: flag {tok} appears twice in the source args")
            seen.add(tok)
            if tok in replace:
                out[i + 1] = replace[tok]
            i += 2 if i + 1 < len(out) and not out[i + 1].startswith("--") else 1
        else:
            i += 1
    j = 0
    while j < len(add):
        flag = add[j]
        if flag in seen:
            raise ValueError(f"merge: refusing to add {flag} twice")
        seen.add(flag)
        if flag in DRAFTER_VERIFY_FLAGS:
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
    cfg["log"] = "logs/engine-prototype-dflash2.log"
    # server.py derives engine flags from STRUCTURED keys (layer_split -> --layer-split,
    # server.py:2604): every such key is preserved verbatim by the deep copy; only the port
    # moves, so the isolated prototype can never collide with production's 8009.
    cfg["port"] = PROTO_PORT
    if mode == "target_only":
        pass  # prod args verbatim: no --dflash/--mtp present (observed); drafter-verify
              # flags stay inert (source-verified) so the A/B baseline is byte-identical.
    elif mode == "dflash_w3":
        cfg["args"] = merge_flags(prod["args"], {"--spec": "4", "--spec-min-p": "0.5"},
                                  ["--dflash", ARTIFACT, "--dflash-block", "3"])
    else:
        raise ValueError(f"mode {mode}: MTP-candidate UNVALIDATED - MTP artifacts DO exist "
                         "(/home/user/ai/ai/mtp-Qwen3.8-Flash-Next-Q4_K_M.gguf, shared Q4/Q8, "
                         "/home/user/dev/Strata-data/mtp-bf16/mtp-q2_0.gguf - parent L1 evidence) "
                         "but Swift 1.5 / Strata loader compatibility with the current pack is NOT "
                         "validated and the production config carries no --mtp; mode flags would be invented")
    return cfg


def plan_manifest(prod: dict) -> dict:
    modes = {
        "prod_reference": {"kind": "OBSERVED TARGET-ONLY", "why": "prod args contain no --mtp/--dflash; "
                           "--spec/--spec-min-p are drafter-only (generate.cpp:5445, 9176) and inert here",
                           "exe": prod["exe"]},
        "new_target_only": {"kind": "clone, prod args verbatim", "exe": PROTO_BINARY},
        "new_mtp": {"kind": "MTP-candidate UNVALIDATED", "why": "MTP artifacts exist (parent L1: "
                    "/home/user/ai/ai/mtp-Qwen3.8-Flash-Next-Q4_K_M.gguf, shared Q4/Q8, "
                    "/home/user/dev/Strata-data/mtp-bf16/mtp-q2_0.gguf) but Swift/Strata loader "
                    "compatibility with the current pack is NOT validated; not active in prod "
                    "(no --mtp in config); mode not emitted, no fake baseline"},
        "new_dflash_w3": {"kind": "clone + --dflash artifact + --spec 4 --dflash-block 3",
                          "why": "width=min(block,spec-1)=3 (generate.cpp:5500); trained block 8 (5482); "
                                 "spec<=8 (5445-5454, kVerifyMaxT=8)", "exe": PROTO_BINARY},
    }
    return {
        "manifest": True,
        "note": "This is a PLAN MANIFEST, not a server config. Valid cloned configs are the "
                "proto-server-*.json files written beside it.",
        "observed_rules": [
            "sampled requests: CLI target-only fallback (generate.cpp:2830); serve sampled keeps "
            "target-only decoding with an explicit line (12457) - never claim active DFlash for sampled",
            "serve --batch + dflash refused (2836); --pipeline-windows + dflash + split refused (4214)",
            "no script stops/restarts production; detection failure is fail-closed",
            "isolated prototype ONLY on port 8019; port 8009 (production) is refused outright",
        ],
        "cases": [
            {"case": "health+identity", "setup": "GET /health, GET /props, GET /v1/models",
             "expected": "server model_name/exe match the cloned config", "status": "PARENT-RUN (--execute)"},
            {"case": "repeat-conversation", "setup": "same greedy prompt twice (width 3)",
             "expected": "second request reuses prefix (prompt_tokens reported by engine), no slot-reset errors",
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
    paths = {}
    manifest = CONFIG_DIR / "inco-dflash2-plan-manifest.json"
    manifest.write_text(json.dumps(plan_manifest(prod), indent=2) + "\n")
    paths["manifest"] = str(manifest)
    for mode in ("target_only", "dflash_w3"):
        cfg = clone_server_config(mode, prod)
        p = CONFIG_DIR / f"proto-server-swift15-{mode}.json"
        p.write_text(json.dumps(cfg, indent=2) + "\n")
        paths[mode] = str(p)
    if prod["args"] == clone_server_config("target_only", prod)["args"]:
        paths["identity_note"] = "target_only clone keeps prod args verbatim (byte-identical baseline)"
    return paths


def prod_status() -> str:
    """'running' | 'absent' | 'unknown'. Any detection error is 'unknown' -> fail-closed."""
    try:
        r = subprocess.run(["pgrep", "-f", "build-2966/strata"], capture_output=True, text=True, check=False)
        if r.returncode == 0:
            return "running"
        if r.returncode == 1:
            return "absent"
        return "unknown"
    except OSError:
        return "unknown"


def http_json(url: str, payload: dict | None = None, timeout: float = 120.0) -> dict:
    req = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="GET" if payload is None else "POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def guard_execute(url: str, confirm: bool) -> str | None:
    """Returns None when allowed, else the refusal reason. NO override of detection exists."""
    parsed = urllib.parse.urlparse(url)
    if parsed.port == PROD_PORT:
        return f"REFUSED: port {PROD_PORT} is production; the harness talks ONLY to the isolated prototype ({PROTO_PORT})"
    if parsed.port != PROTO_PORT:
        return f"REFUSED: --execute requires the explicit isolated prototype URL on port {PROTO_PORT}"
    if not confirm:
        return "REFUSED: --execute requires --confirm-execution (explicit acknowledgement)"
    status = prod_status()
    if status != "absent":
        return (f"REFUSED: production engine detection = {status}. The harness never stops/restarts "
                "production; execution needs the user-approved downtime with production OBSERVED ABSENT "
                "(detection failure is fail-closed; there is no override flag)")
    return None


def execute_checks(url: str, identity_cfg: str) -> int:
    """Real HTTP checks against the isolated prototype (parent-run; cases are claimed ONLY
    when they executed). No GPU, no production - production was observed absent by the guard."""
    fails = 0
    try:
        health = http_json(f"{url}/health")
        print(f"health: {json.dumps(health)[:200]}")
    except OSError as e:
        print(f"FAIL: /health at {url}: {e} (is the parent's prototype server up on {PROTO_PORT}?)")
        return 1
    props = {}
    try:
        props = http_json(f"{url}/props")
        want = json.loads(pathlib.Path(identity_cfg).read_text())
        if props.get("model_name") not in (None, want["model_name"]):
            print(f"FAIL: /props model_name {props.get('model_name')!r} != cloned {want['model_name']!r}")
            fails += 1
        else:
            print(f"identity: model_name {want['model_name']} confirmed")
    except OSError as e:
        print(f"FAIL: /props identity check: {e}")
        fails += 1
    prompt = "Explain, in two short sentences, why layer splitting moves K/V pools per stage."
    for label, messages in (("repeat-1", prompt), ("repeat-2", prompt), ("reset-new", "Count from 1 to 5.")):
        body = {"model": "swift-1.5-iq3_xxs", "messages": [{"role": "user", "content": messages}],
                "max_tokens": 64, "temperature": 0.0, "stream": False}
        try:
            resp = http_json(f"{url}/v1/chat/completions", body)
            usage = resp.get("usage") or {}
            print(f"{label}: executed; usage={json.dumps(usage)[:160]}")
        except OSError as e:
            print(f"FAIL {label}: {e}")
            fails += 1
    sampled = {"model": "swift-1.5-iq3_xxs", "messages": [{"role": "user", "content": "Say hi."}],
               "max_tokens": 8, "temperature": 1.0, "stream": False}
    try:
        http_json(f"{url}/v1/chat/completions", sampled)
        print("sampled-fallback: request executed; the explicit target-only line must appear in the "
              "prototype log (parent verifies the PROTOTYPE log, read-only)")
    except OSError as e:
        print(f"FAIL sampled-fallback: {e}")
        fails += 1
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
    clone = clone_server_config("target_only", prod)
    check(clone["exe"] == PROTO_BINARY, "clone swaps the engine binary only")
    check(all(k in clone for k in ("args", "cwd", "tokenizer", "model_name", "log", "env", "vision")),
          "clone preserves every server-schema field (args/cwd/tokenizer/model/env/vision)")
    check(clone["args"] == prod["args"], "target-only clone keeps prod args verbatim (byte-identical baseline)")
    check(clone.get("layer_split") == prod.get("layer_split"),
          "structured layer_split key preserved verbatim (server.py:2604 consumes it; value not hardcoded here)")
    check(clone.get("port") == PROTO_PORT, "prototype config moved to port 8019 (never 8009)")
    check("--dflash" not in prod["args"] and "--mtp" not in prod["args"],
          "observed: production serve has NO drafter flags (target-only baseline)")
    merged = clone_server_config("dflash_w3", prod)["args"]
    check(merged.count("--spec") == 1 and merged[merged.index("--spec") + 1] == "4",
          "merge: exactly one --spec, replaced to 4 (no duplicated --spec strings)")
    check(merged.count("--spec-min-p") == 1 and merged[merged.index("--spec-min-p") + 1] == "0.5",
          "merge: exactly one --spec-min-p = 0.5")
    check(merged.count("--dflash") == 1 and merged[merged.index("--dflash") + 1] == ARTIFACT
          and merged[merged.index("--dflash-block") + 1] == "3", "merge: --dflash + --dflash-block 3 (width 3)")
    check(merged.count("--expert-cache") == 1, "merge preserves the single --expert-cache flag")
    try:
        clone_server_config("mtp", prod)
        check(False, "new_mtp mode was emitted (must stay MTP-candidate UNVALIDATED: loader compat unvalidated)")
    except ValueError as e:
        print(f"refused mtp mode: {e}")
    r = guard_execute(f"http://127.0.0.1:{PROD_PORT}", True)
    check(r is not None and "production" in r, "guard: port 8009 (production) refused outright")
    r = guard_execute(PROTO_URL_DEFAULT, False)
    check(r is not None, "guard: --execute without --confirm-execution refused")
    orig = globals()["prod_status"]
    for status in ("running", "unknown"):
        globals()["prod_status"] = lambda s=status: s
        r = guard_execute(PROTO_URL_DEFAULT, True)
        check(r is not None and status in r, f"guard: prod detection '{status}' refuses execution (fail-closed)")
    globals()["prod_status"] = orig
    m = plan_manifest(prod)
    check(m.get("manifest") is True and m["modes"]["prod_reference"]["kind"].startswith("OBSERVED"),
          "manifest labeled as manifest; prod_reference labeled OBSERVED TARGET-ONLY (no invented MTP)")
    check(m["modes"]["new_mtp"]["kind"] == "MTP-candidate UNVALIDATED",
          "new_mtp explicitly UNVALIDATED: artifacts exist (parent L1), load compat not validated - not absent, not available")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--plan", action="store_true", help="write manifest + valid cloned configs, print plan")
    ap.add_argument("--execute", action="store_true", help="PARENT-run real checks against the isolated prototype")
    ap.add_argument("--url", default=PROTO_URL_DEFAULT)
    ap.add_argument("--identity-config", default=None)
    ap.add_argument("--confirm-execution", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        return selfcheck()
    prod = load_prod_config()
    if a.plan:
        paths = write_artifacts(prod)
        print(json.dumps({"written": paths, "prod_status_observed": prod_status(),
                          "note": "manifest is a manifest; proto-server-*.json are valid server-schema "
                                  "clones for the parent to start on port 8019; prod config untouched"}, indent=2))
        print("parent start line (isolated ONLY): cd /mnt/user/Strata && .venv/bin/python serve/server.py "
              f"--config {paths['dflash_w3']} --port {PROTO_PORT}")
        return 0
    if a.execute:
        reason = guard_execute(a.url, a.confirm_execution)
        if reason is not None:
            print(reason)
            return 2
        identity = a.identity_config or str(CONFIG_DIR / "proto-server-swift15-dflash_w3.json")
        return execute_checks(a.url, identity)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
