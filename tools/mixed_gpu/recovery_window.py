#!/usr/bin/env python3
"""Parent-owned, explicit-execute production helper-kill/refill evidence window.

Uses existing validated binaries. Leaves tuned production under --recover on success;
on failure stops the test supervisor and restores the plain tuned launcher.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import time
import urllib.error
import urllib.request

import helper_ab
import pair_supervisor as pair
import window_pair

WORK = Path(__file__).resolve().parents[2]
ROOT = Path(os.environ.get("STRATA_PROD_TREE", "/mnt/user/Strata") + "")
LAUNCHER = ROOT / "launch-prod-swift15-dflash-w4-q4_0-mixed.sh"
CONFIG = ROOT / "prod-config-swift15-dflash-w4-q4_0-mixed.json"
STATE = ROOT / "run/prod-mixed-supervisor.json"
SOCKETS = [ROOT / "run/prod-mixed.sock", ROOT / "run/prod-mixed2.sock"]
EV = WORK / "build-mixed-gpu/cap1-validation"


def command(pid: int) -> list[str]:
    return Path(f"/proc/{pid}/cmdline").read_bytes().decode().rstrip("\0").split("\0")


def wait_for(fn, timeout=420):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = fn()
            if result:
                return result
        except (FileNotFoundError, ConnectionError, urllib.error.URLError, json.JSONDecodeError):
            pass
        time.sleep(1)
    raise RuntimeError("Window deadline expired")


def models(base_url="http://127.0.0.1:8009"):
    with urllib.request.urlopen(base_url + "/v1/models", timeout=3) as response:
        return response.status == 200


def launch(recover: bool, log_path: Path, launcher=LAUNCHER):
    with log_path.open("a") as log:
        return subprocess.Popen(["bash", str(launcher), *(["--recover"] if recover else [])],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True, cwd=ROOT)


def launcher_dirs(launcher_pid: int) -> list[Path]:
    """Directories a relative launcher argv entry can resolve from: the CURRENT
    cwd, then the exec-time PWD from /proc/<pid>/environ (the kernel snapshot is
    immutable; a launcher script that `cd`s after start moves its live cwd away
    from the directory it was started in, e.g. into the dflash2 integration tree)."""
    dirs: list[Path] = []
    try:
        dirs.append(Path(os.readlink(f"/proc/{launcher_pid}/cwd")))
    except OSError:
        pass
    try:
        for entry in Path(f"/proc/{launcher_pid}/environ").read_bytes().split(b"\0"):
            if entry.startswith(b"PWD=") and len(entry) > 4:
                dirs.append(Path(os.fsdecode(entry[4:])))
                break
    except OSError:
        pass
    return dirs


def launcher_matches(launcher_pid: int) -> bool:
    """True if the process is the canonical launcher.

    Our own launches put the absolute LAUNCHER path in argv; a user-started
    direct launcher shows a relative form (e.g. './launch-prod-...sh') that must
    resolve from its start directory (exec-time PWD) or its current cwd. Accept
    it only when the entry resolves to the canonical file — basename coincidence
    alone is never enough.
    """
    argv = command(launcher_pid)
    if str(LAUNCHER) in argv:
        return True
    name = LAUNCHER.name
    for item in argv:
        if Path(item).name != name:
            continue
        for base in launcher_dirs(launcher_pid):
            candidate = Path(item)
            if not candidate.is_absolute():
                candidate = base / candidate
            try:
                if candidate.resolve() == LAUNCHER.resolve():
                    return True
            except OSError:
                continue
    return False


def stop_existing(server_pid: int, result: dict):
    argv = command(server_pid)
    if str(WORK / "serve/server.py") not in argv or str(CONFIG) not in argv:
        raise RuntimeError("Expected server identity does not match; refusing outage")
    fields = Path(f"/proc/{server_pid}/stat").read_text().rsplit(")", 1)[1].split()
    launcher_pid = int(fields[1])
    if not launcher_matches(launcher_pid):
        raise RuntimeError("Server parent is not the expected launcher")
    start = pair.process_token(launcher_pid)[1]
    known = pair.descendants(launcher_pid, {launcher_pid: start})
    owned = {path: token for path in SOCKETS if (token := pair.socket_token(path)) is not None}
    result["outage_started"] = True
    os.kill(server_pid, signal.SIGTERM)
    wait_for(lambda: not any(pair.alive(pid, token) for pid, token in known.items()), 90)
    pair.release_sockets(owned)
    if any(path.exists() for path in SOCKETS):
        raise RuntimeError("Residual socket after old pair exit")
    return {"server_pid": server_pid, "launcher_pid": launcher_pid, "processes": known}


def attached(generation=1, state_path=STATE, base_url="http://127.0.0.1:8009", supervisor_pid=None):
    state = json.loads(Path(state_path).read_text())
    if supervisor_pid is not None and state["supervisor_pid"] != supervisor_pid:
        return None
    if state["generation"] < generation or state["state"] != "attached":
        return None
    return state if models(base_url) else None


def check_fill(state):
    log = Path(state["helper_log"]).read_text()
    # Canonical since the slots4c adoption (2026-11-01): the launcher requests
    # 4200 slots under the 6400 MiB budget, so the fill line lands above the old
    # 4000-slot cap (measured 4093 slots / 6.25 GiB). A fill at or below 4000
    # means the old default ran, not the canonical launcher.
    filled = re.search(r"expert-helper: filled (\d+) slots \(([\d.]+) GiB\)", log)
    if not filled or int(filled[1]) <= 4000:
        actual = filled[1] if filled else "no fill line"
        raise RuntimeError(f"Recovery did not restore the accepted resident capacity ({actual} slots)")
    if "second-channel engine connected" not in log:
        raise RuntimeError("Recovery did not restore the prompt channel")


def engine_of(state, *, engine=None):
    expected = str(engine if engine is not None else WORK / "build-mixed-gpu/rpcw-hip/strata")
    known = pair.descendants(state["launcher_pid"], {state["launcher_pid"]: pair.process_token(state["launcher_pid"])[1]})
    engines = [pid for pid, start in known.items() if pair.alive(pid, start)
                and command(pid)[0] == expected]
    if len(engines) != 1:
        raise RuntimeError(f"Expected exactly one engine, found {engines}")
    return engines[0]


def probe(label, rep, state, *, config_path=CONFIG, base_url="http://127.0.0.1:8009", family="pair",
          size="long", max_tokens=None, reasoning_budget_tokens=None, enable_thinking=None,
          require_remote_draft=False):
    if size not in ("long", "xl"):
        raise ValueError("Unsupported probe size")
    tag = label if size == "long" else f"{label}-{size}"
    engine_log = Path(json.loads(Path(config_path).read_text())["log"])
    mark = engine_log.stat().st_size
    original = json.loads((EV / f"ab2-req-on-{size}-r1.json").read_text())
    text = original["messages"][0]["content"].split("\n", 1)[1]
    prefix = (f"Pair recovery check rep={rep}, matched before and after." if size == "long" else
              f"Pair recovery check size={size} rep={rep}, matched before and after.")
    original["messages"][0]["content"] = prefix + "\n" + text
    original["model"] = "swift-1.5-iq3_xxs"
    if max_tokens is not None:
        original["max_tokens"] = max_tokens
    if reasoning_budget_tokens is not None:
        original["reasoning_budget_tokens"] = reasoning_budget_tokens
    if enable_thinking is not None:
        original["chat_template_kwargs"] = dict(enable_thinking=enable_thinking)
    body = json.dumps(original).encode()
    (EV / f"{family}-{tag}-r{rep}-request.json").write_bytes(body)
    request = urllib.request.Request(base_url + "/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=600) as response:
        raw = response.read()
        status = response.status
    elapsed = time.monotonic() - started
    (EV / f"{family}-{tag}-r{rep}-reply.json").write_bytes(raw)
    with engine_log.open("rb") as log:
        log.seek(mark)
        segment = log.read().decode(errors="replace")
    (EV / f"{family}-{tag}-r{rep}-engine.log").write_text(segment)
    metrics = [m for line in segment.splitlines() if (m := helper_ab.parse_serve_line(line))]
    if len(metrics) != 1:
        raise RuntimeError(f"Expected one serve line, got {len(metrics)}; inspect saved log")
    metric = metrics[0]
    helper_less = state.get("helper_pid") == 0   # helper-off A/B arms: no helper process
    reply = json.loads(raw)
    ct = reply["usage"]["completion_tokens"]
    finish = reply["choices"][0]["finish_reason"]
    # The helper engagement gate inverts for helper-less arms (helper reqs null/0 is the
    # proof the engine really ran helper-free); None (json null in the serve line) reads
    # as 0 so a normal arm reporting null fails the gate instead of raising TypeError.
    reqs = metric.get("helper_reqs") or 0
    valid = (status == 200 and finish in ("stop", "length") and type(ct) is int
             and 0 < ct <= metric["generated"] and metric["generated"] >= 8
             and metric["reused"] == 0 and (reqs == 0 if helper_less else reqs > 0)
             and (metric.get("helper_timeouts") or 0) == 0
             and (metric.get("helper_fallbacks") or 0) == 0)
    if not valid:
        raise RuntimeError(f"Invalid recovery probe {label}/{rep}: {metric}")
    if require_remote_draft:
        failures = re.findall(r"strata draft-helper:[^\n]*(?:failed|refused|drafting stays|names)[^\n]*", segment)
        if failures or metric.get("offered", 0) <= 0:
            raise RuntimeError(f"Remote drafter did not remain active in {tag}/{rep}: {failures}")
    if not helper_less and not pair.alive(state["helper_pid"], state["helper_start"]):
        raise RuntimeError("Helper died during the probe")
    result = dict(label=label, rep=rep, http=status, elapsed_s=elapsed, finish=finish,
                  completion_tokens=ct, request_sha256=hashlib.sha256(body).hexdigest(), size=size, **metric)
    for key in ("helper_reqs", "helper_timeouts", "helper_fallbacks"):
        if result.get(key) is None:
            result[key] = 0
    print(json.dumps(result), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-server", type=int, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--family", default="rec2")
    args = parser.parse_args()
    if not args.execute:
        print("Plan: stop expected production; private18080 recovery pair; 3 matched long probes before/after exact helper SIGKILL; always stop candidate and restore tuned production (--recover on success, plain on failure).")
        return 0
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    result = {"started": time.time(), "probes": [], "success": False}
    supervisor = None
    stopped = False
    private = window_pair.prepare(args.family)
    probe_args = dict(config_path=private.config, base_url=private.base_url, family=args.family)
    try:
        # Fingerprints from the accepted window, not a source-provenance assumption.
        manifest = json.loads((EV / "ab2-manifest.json").read_text())
        for name in ("rpcw-hip/strata", "rpcw-cuda/strata-expert-helper"):
            binary = WORK / "build-mixed-gpu" / name
            expected = next(row["sha256"] for row in manifest["fingerprints"] if row["path"] == str(binary))
            if hashlib.sha256(binary.read_bytes()).hexdigest() != expected:
                raise RuntimeError(f"Validated binary changed: {binary}")
        result["old_pair"] = stop_existing(args.expected_server, result)
        stopped = True
        supervisor = launch(True, EV / f"{args.family}-console.log", private.launcher)
        before = wait_for(lambda: attached(state_path=private.state, base_url=private.base_url,
                                           supervisor_pid=supervisor.pid))
        check_fill(before)
        before["engine_pid"] = engine_of(before)
        result["before"] = before
        for rep in range(1, 4):
            result["probes"].append(probe("before", rep, before, **probe_args))
        killed_at = time.monotonic()
        if not pair.alive(before["helper_pid"], before["helper_start"]):
            raise RuntimeError("Helper identity changed before fault injection")
        os.kill(before["helper_pid"], signal.SIGKILL)
        result["killed_helper_pid"] = before["helper_pid"]
        after = wait_for(lambda: attached(before["generation"] + 1, private.state, private.base_url,
                                          supervisor.pid))
        check_fill(after)
        after["engine_pid"] = engine_of(after)
        result["recovery_seconds"] = time.monotonic() - killed_at
        if after["helper_pid"] == before["helper_pid"] or after["engine_pid"] == before["engine_pid"]:
            raise RuntimeError("Both peers must be fresh after process death")
        result["after"] = after
        for rep in range(1, 4):
            result["probes"].append(probe("after", rep, after, **probe_args))
        rates = {arm: statistics.median(p["gen_rate"] for p in result["probes"] if p["label"] == arm)
                 for arm in ("before", "after")}
        result["generated_medians"] = rates
        result["after_before_ratio"] = rates["after"] / rates["before"]
        result["performance_gate"] = "PASS" if result["after_before_ratio"] >= 0.95 else "NOT ESTABLISHED (below 95% of same-window baseline)"
        result["success"] = True
        result["production"] = "tuned under --recover; active request interrupted and live cache reset on failure"
    finally:
        if result.get("outage_started"):
            if supervisor is not None and supervisor.poll() is None:
                supervisor.terminate()
                supervisor.wait(timeout=60)
            restored = launch(result["success"], EV / f"{args.family}-restore-console.log")
            wait_for(models)
            result["fallback_launcher_pid"] = restored.pid
            result["production"] = "restored tuned --recover launcher" if result["success"] else "restored plain tuned launcher after unsuccessful test"
        result["finished"] = time.time()
        pair.write_json(EV / f"{args.family}-recovery-status.json", result)
    print(json.dumps(result, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
