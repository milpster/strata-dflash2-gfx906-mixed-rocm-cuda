#!/usr/bin/env python3
"""Frozen-request identity arms for the Q8_K expert-helper wire (default OFF).

Three sequential arms over the SAME frozen ppf1 request bytes (temperature 0,
seed 1729, thinking off, max_tokens 512 - the request files are sent VERBATIM,
never rebuilt):

  stock1     the stock mixed stack: rpcw-hip/strata + rpcw-cuda helper (the
             protected production-lineage binaries, launched read-only)
  candidate  the Q8_K wire: a candidate engine build + candidate helper, with
             STRATA_EXPERT_HELPER_Q8K=1 in the engine env (config env block)
  stock2     the stock stack again (the sandwich: run-to-run determinism)

Per arm: helper up on two channels (the ppf1 launch recipe verbatim), server.py
up on 127.0.0.1:18085, three probes, and the recovery_window.probe engagement
gates (helper reqs > 0, no fallbacks, no timeouts, no reuse, exactly one serve
line per probe). Replies are saved with content_sha256.

Verdict: stock1 == stock2 content hashes is REQUIRED (determinism);
candidate == stock is REPORTED, not silently assumed. If candidate differs,
run the no-helper attribution arm before interpreting: the Q8_K wire is
CPU-faithful by design, so the candidate matches the engine's own expert
arithmetic, not necessarily the stock q8_1 wire.

Production is never touched: the driver refuses to start if any production
process is alive, signals only its own children, and restores nothing
(production is down by session permission; canonical restore is final closeout).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import helper_ab                                                     # noqa: E402

WORK = _HERE.parents[1]
ROOT = Path("/mnt/user/Strata")
EV_ROOT = WORK / "build-mixed-gpu/p2-finish"
REQ_DIR = WORK / "build-mixed-gpu/cap1-validation"
STOCK_ENGINE = WORK / "build-mixed-gpu/rpcw-hip/strata"
STOCK_HELPER = WORK / "build-mixed-gpu/rpcw-cuda/strata-expert-helper"
CAND_ENGINE = WORK / "build-mixed-gpu/repro-hip-q8k/strata"
CAND_HELPER = WORK / "build-mixed-gpu/repro-q81-cuda/strata-expert-helper"
PY = ROOT / ".venv/bin/python"
SERVER = WORK / "serve/server.py"
SERVER_CWD = Path("/mnt/user/dflash2/Strata-integ")
ROCM_LIBS = "/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib"
BASE_CONFIG = REQ_DIR / "ppf1-a-config.json"
PROBES = ("r1", "r2", "r3")
PORT = 18085
PROD_MARKERS = ("pair_supervisor.py", "launch-prod-", "serve/server.py")


def production_absent() -> tuple[bool, str]:
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            joined = b" ".join((proc / "cmdline").read_bytes().split(b"\0")).decode("utf-8", "replace")
        except OSError:
            continue
        for marker in PROD_MARKERS:
            if marker in joined:
                return False, f"live production process pid={proc.name}: {joined[:120]}"
    return True, "no production process alive"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def helper_argv(helper: Path, sock: Path, sock2: Path) -> list[str]:
    # ppf1-a-launch.sh lines 49-54 verbatim (the tested window recipe).
    return [str(helper), "--pack", "/mnt/user/ai/iq3xxs-15",
            "--experts", "../Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf",
            "--device", "0", "--slots", "4200", "--reserve-mib", "300",
            "--timeout", "7200000", "--begin-timeout", "1800000",
            "--socket", str(sock), "--fill-mib", "6400", "--socket2", str(sock2)]


def arm_config(base: dict, arm: dict, ev: Path) -> dict:
    cfg = json.loads(json.dumps(base))                 # deep clone of ppf1-a-config
    cfg["exe"] = str(arm["engine"])
    cfg["log"] = str(ev / f"{arm['name']}-engine.log")
    cfg["port"] = PORT
    if arm.get("helper") is not None:      # helper-less arms carry no socket keys at all
        cfg["expert_helper_socket"] = str(ev / f"{arm['name']}.sock")
        cfg["expert_helper_socket2"] = str(ev / f"{arm['name']}2.sock")
    else:
        cfg.pop("expert_helper_socket", None)
        cfg.pop("expert_helper_socket2", None)
    if arm.get("env"):
        cfg.setdefault("env", {}).update(arm["env"])
    return cfg


def wait_http(url: str, deadline_s: int) -> None:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(3)
    raise RuntimeError(f"server not ready at {url} within {deadline_s}s")


def run_arm(arm: dict, base: dict, ev: Path, probe_timeout: int) -> dict:
    name = arm["name"]
    engine = Path(arm["engine"])
    helper = Path(arm["helper"]) if arm.get("helper") else None
    for binary in ([engine, PY] + ([helper] if helper else [])):
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError(f"{name}: missing binary {binary}")
    if not SERVER.is_file():
        raise RuntimeError(f"{name}: missing {SERVER}")
    cfg = arm_config(base, arm, ev)
    socks = ([Path(cfg["expert_helper_socket"]), Path(cfg["expert_helper_socket2"])]
             if helper is not None else [])
    for path in socks:
        if path.exists() or path.is_symlink():
            raise RuntimeError(f"{name}: refusing existing socket {path}")
    cfg_path = ev / f"{name}-config.json"
    cfg_path.write_text(json.dumps(cfg, indent=1) + "\n", encoding="utf-8")

    helper_log = (ev / f"{name}-helper.log").open("ab")
    server_log = (ev / f"{name}-server.log").open("ab")
    helper_proc = server_proc = None
    engine_log = Path(cfg["log"])
    result = dict(arm=name, engine=str(engine), engine_sha256=sha256_file(engine),
                  helper=str(helper) if helper else "none",
                  helper_sha256=sha256_file(helper) if helper else None,
                  env_extra=arm.get("env", {}), probes=[])
    try:
        env_clean = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                     "HOME": os.environ.get("HOME", "/home/user"), "TERM": "dumb"}
        if helper is not None:
            helper_proc = subprocess.Popen(helper_argv(helper, socks[0], socks[1]), env=env_clean,
                                           stdout=helper_log, stderr=subprocess.STDOUT,
                                           start_new_session=True, cwd=str(WORK))
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline and not (socks[0].exists() and socks[1].exists()):
                if helper_proc.poll() is not None:
                    raise RuntimeError(f"{name}: helper died rc={helper_proc.returncode}")
                time.sleep(2)
            if not (socks[0].exists() and socks[1].exists()):
                raise RuntimeError(f"{name}: helper sockets missing after 900s")

        senv = dict(os.environ)
        for var in ("CUDA_VISIBLE_DEVICES", "STRATA_ARENA_MMAP", "GPU_PINNED_MIN_XFER_SIZE",
                    "HSA_USERPTR_FOR_PAGED_MEM", "LD_PRELOAD"):
            senv.pop(var, None)
        senv["LD_LIBRARY_PATH"] = ROCM_LIBS
        server_proc = subprocess.Popen([str(PY), str(SERVER), "--engine", "strata",
                                        "--config", str(cfg_path), "--port", str(PORT)],
                                       env=senv, stdout=server_log, stderr=subprocess.STDOUT,
                                       start_new_session=True, cwd=str(SERVER_CWD))
        wait_http(f"http://127.0.0.1:{PORT}/v1/models", 1800)

        for rep in PROBES:
            body = (REQ_DIR / f"ppf1-a-{rep}-request.json").read_bytes()   # VERBATIM frozen bytes
            mark = engine_log.stat().st_size if engine_log.exists() else 0
            request = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/chat/completions",
                                             data=body, headers={"Content-Type": "application/json"})
            started = time.monotonic()
            with urllib.request.urlopen(request, timeout=probe_timeout) as response:
                raw = response.read()
                status = response.status
            elapsed = time.monotonic() - started
            (ev / f"{name}-{rep}-request.json").write_bytes(body)
            (ev / f"{name}-{rep}-reply.json").write_bytes(raw)
            segment = ""
            if engine_log.exists():
                with engine_log.open("rb") as log:
                    log.seek(mark)
                    segment = log.read().decode(errors="replace")
            (ev / f"{name}-{rep}-engine.log").write_text(segment, encoding="utf-8")
            metrics = [m for line in segment.splitlines() if (m := helper_ab.parse_serve_line(line))]
            if len(metrics) != 1:
                raise RuntimeError(f"{name}/{rep}: expected one serve line, got {len(metrics)}")
            metric = metrics[0]
            reply = json.loads(raw)
            content = (reply.get("choices") or [{}])[0].get("message", {}).get("content")
            ct = reply.get("usage", {}).get("completion_tokens")
            finish = (reply.get("choices") or [{}])[0].get("finish_reason")
            reqs = metric.get("helper_reqs") or 0
            valid = (status == 200 and finish in ("stop", "length") and type(ct) is int
                     and 0 < ct <= metric["generated"] and metric["generated"] >= 8
                     and metric["reused"] == 0 and (reqs == 0 if helper is None else reqs > 0)
                     and (metric.get("helper_timeouts") or 0) == 0
                     and (metric.get("helper_fallbacks") or 0) == 0 and content)
            if not valid:
                raise RuntimeError(f"{name}/{rep}: engagement gate failed: {metric} ct={ct} finish={finish}")
            result["probes"].append(dict(
                rep=rep, http=status, elapsed_s=round(elapsed, 3), finish=finish,
                completion_tokens=ct, request_sha256=hashlib.sha256(body).hexdigest(),
                content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                content_chars=len(content), helper_reqs=reqs,
                helper_timeouts=metric.get("helper_timeouts") or 0,
                helper_fallbacks=metric.get("helper_fallbacks") or 0))
            print(json.dumps(result["probes"][-1]), flush=True)
    finally:
        helper_log.close()
        server_log.close()
        for proc in (server_proc, helper_proc):
            if proc is not None and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=15)
        for path in socks:
            if path.exists():
                path.unlink()
    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Frozen-request stock/candidate/stock identity arms for the Q8_K wire")
    ap.add_argument("--family", required=True)
    ap.add_argument("--probe-timeout", type=int, default=900)
    ap.add_argument("--arms", default="stock1,candidate,stock2",
                    help="comma-separated subset of: stock1,candidate,stock2,nohelper")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    ev = EV_ROOT / args.family
    if ev.exists():
        print(f"REFUSING: family dir exists: {ev}", file=sys.stderr)
        return 2
    absent, why = production_absent()
    if not absent:
        print(f"REFUSING: {why}", file=sys.stderr)
        return 2
    base = json.loads(BASE_CONFIG.read_text())
    catalog = {
        "stock1": dict(name="stock1", engine=STOCK_ENGINE, helper=STOCK_HELPER),
        "candidate": dict(name="candidate", engine=CAND_ENGINE, helper=CAND_HELPER,
                          env={"STRATA_EXPERT_HELPER_Q8K": "1"}),
        "stock2": dict(name="stock2", engine=STOCK_ENGINE, helper=STOCK_HELPER),
        "nohelper": dict(name="nohelper", engine=STOCK_ENGINE, helper=None),
    }
    unknown = [a for a in args.arms.split(",") if a and a not in catalog]
    if unknown:
        print(f"unknown arms: {unknown}", file=sys.stderr)
        return 2
    arms = [catalog[a] for a in args.arms.split(",") if a]
    print(f"family   : {ev.name}")
    print(f"requests : {REQ_DIR}/ppf1-a-{{r1,r2,r3}}-request.json (VERBATIM, temp0/seed1729/"
          f"thinking-off/512)")
    for arm in arms:
        extra = " +".join(arm.get("env", {}).keys())
        helper_desc = (f"{Path(arm['helper']).parent.name}/{Path(arm['helper']).name}"
                       if arm.get("helper") else "NONE (helper-less reference)")
        print(f"arm      : {arm['name']:9s} engine={Path(arm['engine']).name:26s} "
              f"helper={helper_desc} {extra}")
    print(f"production: verified absent ({why}); no outage, no restore")
    if not args.execute:
        print("plan verified; pass --execute to run")
        return 0

    ev.mkdir(parents=True)
    arm_results: list[dict] = []
    for arm in arms:
        print(f"=== arm {arm['name']} ===", flush=True)
        arm_results.append(run_arm(arm, base, ev, args.probe_timeout))
    results = {"arms": arm_results, "family": args.family,
               "requests": "ppf1-a-r1/r2/r3 verbatim (cap1-validation)"}

    ran = {a["arm"]: {p["rep"]: p["content_sha256"] for p in a["probes"]} for a in results["arms"]}

    def present(*names: str) -> bool:
        return all(n in ran for n in names)

    results["verdict"] = {
        "hashes": ran,
        "stock1_eq_stock2": (ran["stock1"] == ran["stock2"]) if present("stock1", "stock2") else None,
        "candidate_eq_stock": (ran["candidate"] == ran["stock1"]) if present("candidate", "stock1") else None,
        "candidate_eq_nohelper": (ran["candidate"] == ran["nohelper"]) if present("candidate", "nohelper") else None,
        "stock_eq_nohelper": (ran["stock1"] == ran["nohelper"]) if present("stock1", "nohelper") else None,
        "note": "stock1==stock2 is REQUIRED (determinism). candidate==nohelper means the Q8_K "
                "wire makes helper-on outputs identical to the helper-less system (CPU-faithful "
                "by design). Cross-family comparison against q8k-id1 is done manually.",
    }
    (ev / "identity-result.json").write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(results["verdict"], indent=1))
    if "stock1" in ran and "stock2" in ran:
        return 0 if ran["stock1"] == ran["stock2"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
