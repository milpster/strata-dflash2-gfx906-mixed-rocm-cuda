#!/usr/bin/env python3
"""GPU-WINDOW controller - PARENT-only operator automation (agents never run --execute).

Default is DRY RUN: fresh snapshot + plan only, ZERO signals.  --execute (parent):
 1. Fresh snapshot of the live 8009 proxy: ss + /proc verify argv
    (serve/server.py + prod-config-swift15.json + --port 8009), cwd=/mnt/user/Strata,
    proxy start ticks + PGID, and the ORIGINAL engine child via readlink
    /proc/PID/exe (+ vision children, all pinned by pid + start ticks).
    A missing engine child REFUSES the window (L10 parent review).
 2. Immediately BEFORE the signal, re-verify the proxy's exact identity: pid, start
    ticks, argv, cwd, config sha256.  SIGTERM goes to that verified proxy PID only.
 3. After the stop: wait for port 8009 AND every verified engine/vision descendant to
    exit (by pid + start ticks).  A surviving descendant = NO GPU test runs.
 4. Run the six known GPU primitives with os.environ PLUS the freshly captured engine
    device/library flags (from its live /proc/environ - the real prod library
    identity, not hardcoded flags).  Durable per-test logs under a UNIQUE window
    label, per-test timeout recorded as exit 124 with duration + log, genuine exit
    codes, break on failure.  exit 77 is SKIPPED/NOTVALIDATED, never PASS here.
 5. Restore: the ORIGINAL launch-prod-swift15.sh is started BACKGROUNDED (the launcher
    never returns - exec'd server) ONLY when this controller sent a valid stop AND the
    verified original processes are gone.  If the original proxy/engine is alive and
    healthy, verify only - NEVER launch a duplicate.  Interrupted before the stop:
    verify only.  A surviving verified proxy gets SIGTERM to its verified identity,
    then verify; never a duplicate launch while an original proxy lives.
    Verification: /health + exact /props model_path + engine exe identity + engine
    cmdline --max-context 262144 (matching the original) + config sha256.

The stop path (2 + 3) is the exported helper stop_prod(); the snapshot/verify/restore helpers
here are shared with ops/model_window.py (model runs on the isolated 8019) so both windows use
the SAME verified stop, the SAME identity verification and the SAME restore path.

Safety: no global kill anywhere; nothing OpenCode-related is ever matched; secret-
looking env values are masked in every printed/persisted output (full device flags
stay in memory for the test env only); results JSON written 0600; per-window labels
so reruns never overwrite evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import urllib.request

PROD_CWD = "/mnt/user/Strata"
LAUNCHER = "/mnt/user/Strata/launch-prod-swift15.sh"
PROD_CONFIG = "/mnt/user/Strata/prod-config-swift15.json"
PROD_EXE = "/mnt/user/Strata-2966/build-2966/strata"
MODEL_PATH = "/mnt/user/ai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf"
MAX_CTX = "--max-context 262144"
BUILD = "/mnt/user/dflash2/Strata-wt/build-gfx906-dflash2"
LOGS = pathlib.Path("/mnt/user/dflash2/logs")
OPS = pathlib.Path("/mnt/user/dflash2/ops")
PORT = 8009
TESTS = [("dflash_taps_test", 600), ("dflash_rope_parity", 600), ("dflash_head_bf16_test", 600),
         ("dflash_tap_collect_test", 900), ("dflash_owned_kv_test", 900), ("dflash2_parity", 1800)]
DEVICE_PREFIX = ("HSA_", "HIP_", "CUDA_", "STRATA_", "LD_LIBRARY_PATH")
SECRET_MARKS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def sh(args: list[str]) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=False).stdout


def listener_pid(port: int = PORT) -> int | None:
    for line in sh(["ss", "-tlnp"]).splitlines():
        if f":{port} " in line and "pid=" in line:
            try:
                return int(line.split("pid=")[1].split(",")[0])
            except (IndexError, ValueError):
                return None
    return None


def cmdline(pid: int) -> str:
    try:
        return pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _fields(pid: int) -> list[str]:
    return pathlib.Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()


def pgid_of(pid: int) -> int:
    try:
        return int(_fields(pid)[2])   # L10 parent review: field 2 is PGID (field 1 is PPID)
    except (OSError, IndexError, ValueError):
        return -1


def start_ticks(pid: int) -> int:
    try:
        return int(_fields(pid)[19])
    except (OSError, IndexError, ValueError):
        return -1


def same_proc(pid: int, ticks: int) -> bool:
    """The SAME process slot: same pid AND same start ticks (no PID-reuse false positives)."""
    return pid > 0 and ticks >= 0 and start_ticks(pid) == ticks


def exe_of(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return ""


def children(root: int) -> list[int]:
    kids = []
    for d in pathlib.Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            if int(_fields(int(d.name))[1]) == root:
                kids.append(int(d.name))
        except (OSError, IndexError, ValueError):
            continue
    return kids


def env_captured(pid: int) -> dict:
    """The live engine's OWN device/library flags - fresh, from /proc, in memory only."""
    out = {}
    try:
        raw = pathlib.Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return {}
    for item in raw:
        kv = item.decode("utf-8", "replace").split("=", 1)
        if len(kv) == 2 and kv[0].startswith(DEVICE_PREFIX):
            out[kv[0]] = kv[1]
    return out


def masked(env: dict) -> dict:
    return {k: ("***" if any(m in k.upper() for m in SECRET_MARKS) else v) for k, v in env.items()}


def config_sha() -> str:
    return hashlib.sha256(pathlib.Path(PROD_CONFIG).read_bytes()).hexdigest()


def proxy_verified(pid: int) -> bool:
    cmd = cmdline(pid)
    try:
        cwd_ok = pathlib.Path(f"/proc/{pid}/cwd").resolve() == pathlib.Path(PROD_CWD)
    except OSError:
        cwd_ok = False
    return ("serve/server.py" in cmd and pathlib.Path(PROD_CONFIG).name in cmd
            and f"--port {PORT}" in cmd and cwd_ok)


def snapshot() -> dict:
    proxy = listener_pid()
    if proxy is None:
        return {"verified": False, "why": "nothing listens on 8009 - the stop/restore pair is meaningless; refuse"}
    if not proxy_verified(proxy):
        return {"verified": False, "proxy": proxy, "cmdline": cmdline(proxy)[:200],
                "why": "8009 listener is NOT the verified prod proxy (argv/cwd/config) - refuse"}
    kids = children(proxy)
    engine = next((k for k in kids if exe_of(k) == PROD_EXE), None)
    if engine is None:
        return {"verified": False, "proxy": proxy,
                "why": "no engine child with the original exe (readlink /proc/PID/exe) - refuse: nothing to restore against"}
    vision = {str(k): start_ticks(k) for k in kids if k != engine and "strata-vision" in cmdline(k)}
    env = env_captured(engine)
    return {"verified": True, "proxy": proxy, "proxy_ticks": start_ticks(proxy), "proxy_pgid": pgid_of(proxy),
            "engine": engine, "engine_ticks": start_ticks(engine), "engine_exe": PROD_EXE,
            "vision": vision, "config_sha256": config_sha(),
            "maxctx_in_original": MAX_CTX in cmdline(engine),
            "engine_device_env_masked": masked(env), "_env_full": env}


def display(snap: dict) -> dict:
    return {k: v for k, v in snap.items() if k != "_env_full"}


def http_json(url: str, timeout: float = 20.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def verify_live(snap: dict) -> tuple[bool, dict]:
    """Live state against the ORIGINAL identity: proxy -> engine exe -> endpoints."""
    pid = listener_pid()
    if pid is None:
        return False, {"detail": "port 8009 closed"}
    if not proxy_verified(pid):
        return False, {"detail": f"proxy {pid} argv/cwd/config mismatch"}
    engine = next((k for k in children(pid) if exe_of(k) == PROD_EXE), None)
    if engine is None:
        return False, {"detail": "no engine child with the original exe"}
    try:
        props = http_json(f"http://127.0.0.1:{PORT}/props")
        health = http_json(f"http://127.0.0.1:{PORT}/health")
    except (OSError, ValueError) as e:
        return False, {"detail": f"endpoints not ready: {type(e).__name__}"}
    sha_same = config_sha() == snap["config_sha256"]
    model_ok = props.get("model_path") == MODEL_PATH
    ctx_ok = (MAX_CTX in cmdline(engine)) == snap["maxctx_in_original"]   # from the cmdline, not /slots
    ok = sha_same and model_ok and ctx_ok
    return ok, {"proxy": pid, "engine": engine, "engine_exe": PROD_EXE,
                "model_path": props.get("model_path"), "config_sha_same": sha_same,
                "maxctx_matches_original": ctx_ok, "health": str(health)[:80]}


def descendants_gone(snap: dict) -> bool:
    return (not same_proc(snap["engine"], snap["engine_ticks"])
            and all(not same_proc(int(p), t) for p, t in snap["vision"].items()))


def restore_and_verify(snap: dict, record: dict, deadline: float) -> dict:
    """NEVER launch a duplicate: launch only after a valid stop sent here + verified
    originals gone.  Healthy original = verify only.  See module docstring step 5."""
    now = listener_pid()
    if not record.get("stop_sent"):
        ok, detail = verify_live(snap)   # interrupted before the stop: verify only, never launch
        return {"restored": ok, "launched": False, "mode": "verify-only (stop was never sent)", "detail": detail}
    if now is not None and same_proc(now, snap["proxy_ticks"]):
        # The verified original proxy survived our SIGTERM: signal THAT verified identity
        # again, then verify; never launch while it lives.
        try:
            os.kill(now, signal.SIGTERM)
        except ProcessLookupError:
            pass
        t = time.time() + 90
        while time.time() < t and listener_pid() is not None:
            time.sleep(2)
    if listener_pid() is not None:
        ok, detail = verify_live(snap)
        return {"restored": ok, "launched": False, "mode": "a proxy listens; verified-identity stop then verify, no launch", "detail": detail}
    if not descendants_gone(snap):
        return {"restored": False, "launched": False, "mode": "investigate",
                "why": "verified original engine/vision descendants still alive with the port free - parent investigate; NO duplicate launch"}
    log = LOGS / f"{record['window']}-restore.log"
    with open(log, "ab") as fh:   # BACKGROUNDED: the launcher exec's the server and never returns
        subprocess.Popen(["bash", LAUNCHER], cwd=PROD_CWD, stdout=fh, stderr=fh,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    while time.time() < deadline:
        if listener_pid() is not None:
            ok, detail = verify_live(snap)
            if ok:
                return {"restored": True, "launched": True, "mode": "original launcher backgrounded + verified", "detail": detail, "log": str(log)}
        time.sleep(5)
    return {"restored": False, "launched": True, "mode": "original launcher backgrounded",
            "why": "restore verification failed before the deadline - PARENT INVESTIGATE NOW", "log": str(log)}


def note_to(wlog: pathlib.Path, msg: str) -> None:
    with open(wlog, "ab") as fh:
        fh.write(f"{msg} {time.strftime('%FT%T%z')}\n".encode())


def stop_prod(snap: dict, record: dict, note) -> bool:
    """Verified SIGTERM stop of the snapshot's proxy ONLY, then the whole-family exit gate.
    Re-verify identity (pid, start ticks, argv/cwd, config sha) IMMEDIATELY before the signal;
    refuse the signal if anything moved.  True = port 8009 closed AND every verified
    engine/vision descendant exited (by pid + start ticks) - only then may GPU work run.
    Shared by run_window (primitives) and ops/model_window.py (model runs): ONE stop path."""
    now = listener_pid()
    if (now is None or now != snap["proxy"] or start_ticks(now) != snap["proxy_ticks"]
            or not proxy_verified(now) or config_sha() != snap["config_sha256"]):
        record["stop_fail"] = "proxy identity changed since snapshot - REFUSE the signal (restore = verify only)"
        return False
    note(f"STOP: SIGTERM to verified proxy {snap['proxy']} (argv/cwd/ticks/sha re-verified) only")
    os.kill(snap["proxy"], signal.SIGTERM)
    record["stop_sent"] = True
    deadline = time.time() + 150
    while time.time() < deadline:
        if listener_pid() is None and descendants_gone(snap):
            break
        time.sleep(2)
    record["stopped_ok"] = listener_pid() is None and descendants_gone(snap)
    note(f"PROD_DOWN={record['stopped_ok']} (port 8009 + all verified engine/vision pids by start ticks)")
    if not record["stopped_ok"]:
        record["stop_fail"] = "port 8009 open OR a verified engine/vision descendant alive - NO GPU test runs"
        return False
    return True


def run_window(snap: dict, record: dict, wlog: pathlib.Path) -> None:
    note = lambda msg: note_to(wlog, msg)   # noqa: E731  the same durable note path as before
    if not stop_prod(snap, record, note):
        return
    env = os.environ.copy()
    env.update(snap["_env_full"])   # the live engine's OWN device/library flags, merged - not a replaced env
    failed = False
    for name, timeout in TESTS:
        log = LOGS / f"{record['window']}-{name}.log"
        t0 = time.time()
        rc = None
        try:
            with open(log, "wb") as tl:
                rc = subprocess.run([f"{BUILD}/{name}"], cwd=BUILD, env=env,
                                    stdout=tl, stderr=tl, timeout=timeout, check=False).returncode
        except subprocess.TimeoutExpired:
            rc = 124   # the genuine timeout status, recorded with duration + log
        record["results"].append({"test": name, "exit": rc, "seconds": round(time.time() - t0, 3),
                                  "note": {0: "ok", 77: "SKIPPED/NOTVALIDATED (NOT a pass)", 124: "timeout"}.get(rc, "nonzero"),
                                  "log": str(log)})
        note(f"{name}: exit {rc} in {round(time.time() - t0, 3)}s -> {log}")
        if rc != 0:   # 77 and 124 are NOT passes on this box: break, restore
            failed = True
            note(f"BREAK on {name} (exit {rc}); restoring production now")
            break
    record["all_tests_ok"] = not failed and len(record["results"]) == len(TESTS)
    record["skipped_77"] = [r["test"] for r in record["results"] if r["exit"] == 77]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="PARENT-only: real window (default DRY RUN)")
    a = ap.parse_args()

    window = f"window-{time.strftime('%Y%m%d-%H%M%S')}"   # unique labels: reruns never overwrite evidence
    LOGS.mkdir(parents=True, exist_ok=True)
    OPS.mkdir(parents=True, exist_ok=True)
    results = OPS / f"{window}-results.json"
    wlog = LOGS / f"{window}.log"

    snap = snapshot()
    print(json.dumps({"window": window, "snapshot": display(snap), "tests": [n for n, _ in TESTS]}, indent=2))
    if not snap.get("verified"):
        print("REFUSE: " + snap.get("why", "snapshot failed"))
        return 2
    if not a.execute:
        print("DRY RUN only - zero signals, zero GPU. Parent: rerun with --execute for the real window.")
        return 0

    record: dict = {"window": window, "started": time.strftime("%FT%T%z"),
                    "snapshot": display(snap), "stop_sent": False, "results": []}
    try:
        run_window(snap, record, wlog)
    except BaseException as e:   # SIGTERM/KeyboardInterrupt land HERE: restore respects stop_sent, so an
        record["window_error"] = repr(e)[:300]   # interrupted-before-stop window NEVER launches a duplicate
    finally:
        record["restore"] = restore_and_verify(snap, record, time.time() + 360)
        record["status"] = ("ALL OK" if record.get("all_tests_ok") and record["restore"].get("restored")
                            else "RESTORED; tests failed/unvalidated - see logs" if record["restore"].get("restored")
                            else "RESTORE FAILED - PARENT INVESTIGATE NOW")
        with open(wlog, "ab") as fh:
            fh.write(f"WINDOW_FINAL {record['status']} restore={json.dumps(record['restore'])}\n".encode())
        results.write_text(json.dumps(record, indent=2) + "\n")
        os.chmod(results, 0o600)   # snapshot embeds device env (masked values) - keep it owner-only
        print(json.dumps(record, indent=2))
    return 0 if record.get("all_tests_ok") and record["restore"].get("restored") else 1


if __name__ == "__main__":
    sys.exit(main())
