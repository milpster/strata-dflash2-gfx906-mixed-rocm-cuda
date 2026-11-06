#!/usr/bin/env python3
"""Recover an owned mixed-GPU pair after engine, helper, or channel failure.

No GPU calls or reconnect protocol. Recovery reloads both peers and interrupts
the failed request. Production launchers supervise by default; --single-run is
an explicit diagnostic escape hatch, not a production recovery mode.
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time


HELPER_FAILURES = (
    "the expert helper is absent",
    "helper connect failed",
    "the helper is not started",
    "the claimed rows fall back to the CPU",
    "the rows did not come back whole",
    "expert-helper: reading the prompt row reply failed",
    "expert-helper: fill refused",
)
FILLED = re.compile(r"expert-helper: filled [1-9][0-9]* slots\b|"
                    r"expert-helper: fill stopped: [^\n]*\([1-9][0-9]* slots, [0-9.]+ [GM]iB uploaded")
PROXY = re.compile(r"expert-helper proxy: [1-9][0-9]* experts\b")
REQUEST_DONE = re.compile(r"strata serve: prompt [0-9,]+ tokens")
READ_TICK = re.compile(r"reading the prompt: ([0-9,]+ of [0-9,]+) tokens")
STAGE_SCRIPT = Path(__file__).resolve().parent / "staging_affinity.py"
STAGE_ATTEMPTS = 10  # Monitor intervals; staging threads exist well before attach.


def helper_degraded(line: str) -> bool:
    return (any(mark in line for mark in HELPER_FAILURES)
            or ("expert-helper:" in line and "closing the channel" in line))


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def process_token(pid: int) -> tuple[str, str] | None:
    """State and start time; a zombie is not live and PID reuse is not ownership."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[0], fields[19]
    except (OSError, IndexError):
        return None


def alive(pid: int, start: str) -> bool:
    token = process_token(pid)
    return token is not None and token[0] != "Z" and token[1] == start


def socket_token(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"Refusing non-owned socket path: {path}")
    return info.st_dev, info.st_ino


def release_sockets(owned: dict[Path, tuple[int, int]]) -> None:
    for path, token in owned.items():
        try:
            current = socket_token(path)
        except RuntimeError:
            continue
        if current == token:
            path.unlink()


def capture_sockets(paths: list[Path], owned: dict[Path, tuple[int, int]]) -> None:
    for path in paths:
        token = socket_token(path)
        if token is not None:
            owned.setdefault(path, token)


def enable_subreaper() -> None:
    """Keep orphaned descendants owned by this private supervisor process."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def descendants(root: int, known: dict[int, str], *, adopt_orphans: bool = False) -> dict[int, str]:
    """Remember owned descendants, including engines in separate process groups."""
    table = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            table[int(entry.name)] = (int(fields[1]), fields[19])
        except (OSError, IndexError, ValueError):
            continue
    result = dict(known)
    owner = os.getpid() if adopt_orphans else None
    if owner is not None and owner in table:
        # After an ancestor exits, Linux reparents its descendants to our
        # subreaper. This dedicated process starts only one launcher at a time.
        result[owner] = table[owner][1]
    while True:
        added = {pid: start for pid, (parent, start) in table.items()
                 if parent in result and pid not in result and parent in table
                 and table[parent][1] == result[parent]}
        if not added:
            if owner is not None:
                result.pop(owner, None)  # Ownership discovery must never signal us.
            return result
        result.update(added)


def engine_process(server: int, executable: Path, known: dict[int, str]) -> tuple[int, str] | None:
    for pid, start in known.items():
        path = Path(f"/proc/{pid}")
        try:
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            if (int(fields[1]) == server and fields[0] != "Z" and fields[19] == start
                    and (path / "exe").resolve(strict=True) == executable):
                return pid, start
        except OSError:
            continue
    return None


def apply_stage_affinity(engine: tuple[int, str], cpus: str, snapshot: Path) -> tuple[bool, int | None, bool, str | None]:
    """One bounded application of the staging-thread move; the tool snapshots first."""
    result = subprocess.run(
        [sys.executable, str(STAGE_SCRIPT), "apply", "--pid", str(engine[0]),
         "--cpus", cpus, "--snapshot", str(snapshot)],
        capture_output=True, text=True, timeout=30)
    moved = re.search(r"Moved (\d+) staging threads", result.stdout)
    error = None
    if result.returncode != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        error = lines[-1] if lines else f"exit status {result.returncode}"
    return (result.returncode == 0, int(moved.group(1)) if moved else None,
            "No CPU-0 strata-stage threads found" in result.stderr, error)


class LogTail:
    """Only complete lines written since this generation started are evidence."""

    def __init__(self, path: Path):
        self.path = path
        try:
            info = path.stat()
        except FileNotFoundError:
            info = None
        self.identity = (info.st_dev, info.st_ino) if info else None
        self.position = info.st_size if info else 0
        self.pending = ""

    def read_lines(self) -> list[str]:
        try:
            log = self.path.open("rb")
        except FileNotFoundError:
            return []
        with log:
            info = os.fstat(log.fileno())
            identity = info.st_dev, info.st_ino
            if identity != self.identity or info.st_size < self.position:
                self.position = 0
                self.pending = ""
            self.identity = identity
            log.seek(self.position)
            text = self.pending + log.read().decode("utf-8", errors="replace")
            self.position = log.tell()
        complete, _, self.pending = text.rpartition("\n")
        return complete.splitlines()


def reap_orphans(known: dict[int, str], launcher: int) -> None:
    for pid, start in known.items():
        if pid != launcher and process_token(pid) == ("Z", start):
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass  # Still reaped by its live parent, not by the subreaper.


def stop_group(child: subprocess.Popen, timeout: float, known: dict[int, str]) -> None:
    """Bounded cleanup of this launcher's tree, with PID-reuse checks throughout."""
    known = descendants(child.pid, known, adopt_orphans=True)
    for pid, start in known.items():
        # Give the server its graceful engine-shutdown deadline before escalation.
        try:
            if alive(pid, start) and os.getpgid(pid) == child.pid:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        child.poll()
        known = descendants(child.pid, known, adopt_orphans=True)
        reap_orphans(known, child.pid)
        if not any(alive(pid, start) for pid, start in known.items()):
            break
        time.sleep(0.1)
    # A peer can fork while TERM is being delivered. Refresh ownership through
    # escalation too, including newly adopted children in separate sessions.
    deadline = time.monotonic() + 5
    while True:
        known = descendants(child.pid, known, adopt_orphans=True)
        for pid, start in known.items():
            if alive(pid, start):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        child.poll()
        reap_orphans(known, child.pid)
        known = descendants(child.pid, known, adopt_orphans=True)
        if child.poll() is not None and not any(alive(pid, start) for pid, start in known.items()):
            reap_orphans(known, child.pid)
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Owned processes did not exit; refusing another generation")
        time.sleep(0.1)
    child.wait(timeout=5)


def supervise(args: argparse.Namespace) -> int:
    executable = None
    engine_log = None
    if args.config is not None:
        config = json.loads(args.config.read_text())
        cwd = Path(config.get("cwd", args.config.resolve().parent))
        executable = (cwd / config["exe"]).resolve()
        engine_log = cwd / config["log"]
    args.state.parent.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _signum, _frame: stop.set())
    with args.lock.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"Another pair supervisor owns {args.lock}") from None
        enable_subreaper()
        with tempfile.TemporaryDirectory(prefix="mixed-pair-", dir=args.state.parent) as work:
            record = Path(work) / "peers.json"
            generation = 0
            failures = 0
            publish: dict = {}
            while not stop.is_set():
                generation += 1
                record.unlink(missing_ok=True)
                for path in args.socket:
                    if path.exists() or path.is_symlink():
                        raise RuntimeError(f"Refusing existing socket path: {path}")
                environment = dict(os.environ, STRATA_PAIR_RECORD=str(record))
                tail = LogTail(engine_log) if engine_log is not None else None
                started = time.monotonic()
                child = subprocess.Popen(["nice", "-n", str(args.nice), "bash", str(args.launcher), "--pair-child"], env=environment,
                                         start_new_session=True)
                peers = None
                engine = None
                server_tail = None
                reading: tuple[str, float] | None = None
                token = process_token(child.pid)
                known = {child.pid: token[1]} if token is not None else {}
                owned: dict[Path, tuple[int, int]] = {}
                healthy = False
                engine_attached = False
                stage_attempts = 0
                stage_result: dict | None = None
                reason = "launcher exited"
                publish = dict(supervisor_pid=os.getpid(), launcher_pid=child.pid,
                               generation=generation, state="starting", timestamp=time.time())
                try:
                    write_json(args.state, publish)
                    while not stop.wait(args.interval):
                        known = descendants(child.pid, known, adopt_orphans=True)
                        capture_sockets(args.socket, owned)
                        if record.exists():
                            current = json.loads(record.read_text())
                            if current != peers:
                                # Publication can race the earlier /proc snapshot.
                                # Refresh after reading the record before checking ancestry.
                                known = descendants(child.pid, known, adopt_orphans=True)
                                for name in ("helper", "server"):
                                    pid = current[f"{name}_pid"]
                                    start = current[f"{name}_start"]
                                    try:
                                        if pid and alive(pid, start) and (
                                                known.get(pid) != start or os.getpgid(pid) != child.pid):
                                            raise RuntimeError(f"{name} PID {pid} is outside the owned group")
                                    except ProcessLookupError:
                                        pass
                                peers = current
                                publish.update(peers, state="monitoring" if peers["server_pid"] else "starting")
                                if engine_log is not None:
                                    publish["engine_stderr_log"] = str(engine_log)
                                write_json(args.state, publish)
                        if peers is not None:
                            dead = [name for name in ("helper", "server") if peers[f"{name}_pid"]
                                    and not alive(peers[f"{name}_pid"], peers[f"{name}_start"])]
                            if dead:
                                reason = ", ".join(dead) + " exited"
                                break
                            if engine is not None and not alive(*engine):
                                reason = "engine exited"
                                break
                            if executable is not None and engine is None and peers["server_pid"]:
                                engine = engine_process(peers["server_pid"], executable, known)
                                if engine is not None:
                                    publish.update(engine_pid=engine[0], engine_start=engine[1])
                                    write_json(args.state, publish)
                            if peers.get("helper_pid") == 0:
                                log_path = Path(peers["engine_log"])
                                attached = ("ready: http://" in log_path.read_text(errors="replace")
                                            if log_path.exists() else False)
                            else:
                                if tail is not None:
                                    lines = tail.read_lines()
                                    failed = next((line for line in lines if helper_degraded(line)), None)
                                    if failed is not None:
                                        reason = "engine helper degraded: " + failed.strip()
                                        break
                                    engine_attached |= any(PROXY.search(line) for line in lines)
                                    if args.stall_timeout and any(REQUEST_DONE.search(line)
                                                                 for line in lines):
                                        reading = None  # The request ended; idle is not a stall.
                                helper_path = Path(peers["helper_log"])
                                helper_log = helper_path.read_text(errors="replace") if helper_path.exists() else ""
                                if any(mark in helper_log for mark in (
                                    "channel:", "request failed:", "the engine said bye; exiting",
                                    "the engine idled past the timeout; exiting", "fill refused:",
                                    "--persist: engine session"
                                )):
                                    reason = "helper channel ended"
                                    break
                                attached = bool(FILLED.search(helper_log))
                                if len(args.socket) > 1:
                                    attached &= "second-channel engine connected" in helper_log
                                attached &= tail is None or engine_attached
                            attached &= bool(peers["server_pid"])
                            attached &= executable is None or engine is not None
                            if attached and not healthy:
                                healthy = True
                                publish.update(state="attached", attached_at=time.time())
                                write_json(args.state, publish)
                            # The engine re-pins its strata-stage threads to CPU 0 at
                            # every start, and the hostapp shares that core. Re-apply the
                            # approved move for this engine incarnation after attach.
                            if (args.stage_cpus and healthy and engine is not None
                                    and stage_result is None
                                    and stage_attempts < STAGE_ATTEMPTS):
                                stage_attempts += 1
                                suffix = "" if stage_attempts == 1 else f".retry{stage_attempts}"
                                snapshot = args.state.with_name(
                                    f"{args.state.stem}-affinity-{engine[0]}-{engine[1]}{suffix}.json")
                                try:
                                    applied, moved, retryable, error = apply_stage_affinity(
                                        engine, args.stage_cpus, snapshot)
                                except subprocess.TimeoutExpired:
                                    applied, moved, retryable, error = (False, None, False,
                                                                        "staging-affinity tool timed out")
                                if applied:
                                    stage_result = {"state": "applied", "moved": moved,
                                                    "snapshot": str(snapshot)}
                                elif not retryable or stage_attempts >= STAGE_ATTEMPTS:
                                    # Threads may spawn late; otherwise record and move on.
                                    # An affinity miss never restarts the pair by itself.
                                    stage_result = {"state": "failed", "error": error}
                                if stage_result is not None:
                                    publish["stage_affinity"] = dict(stage_result,
                                                                    cpus=args.stage_cpus)
                                    write_json(args.state, publish)
                            # A mid-request wedge keeps every process alive and
                            # every channel open; the only heartbeat is the
                            # server's read ticker, which prints every 10 s while
                            # a prompt is being read. A counter that stops
                            # changing for --stall-timeout means the read is
                            # wedged (or the ticker itself is), so rebuild the
                            # pair instead of waiting for a client to cancel.
                            if (args.stall_timeout and healthy and tail is not None
                                    and peers.get("server_pid")):
                                if server_tail is None:
                                    server_tail = LogTail(Path(peers["engine_log"]))
                                for line in server_tail.read_lines():
                                    tick = READ_TICK.search(line)
                                    if tick and (reading is None or tick.group(1) != reading[0]):
                                        reading = (tick.group(1), time.monotonic())
                                if reading is not None and time.monotonic() - reading[1] > args.stall_timeout:
                                    reason = (f"prompt read stalled at {reading[0]} tokens for "
                                              f"{args.stall_timeout:g}s without progress")
                                    break
                        if child.poll() is not None:
                            break
                        if not healthy and time.monotonic() - started > args.startup_timeout:
                            reason = "helper did not attach and fill before the startup deadline"
                            break
                    publish.update(state="stopping" if stop.is_set() else "recovering",
                                   reason=reason, timestamp=time.time())
                    write_json(args.state, publish)
                finally:
                    stop_group(child, args.stop_timeout, known)
                    # Include sockets bound during startup/teardown, even if
                    # stop arrived before the first monitoring interval.
                    capture_sockets(args.socket, owned)
                    release_sockets(owned)
                if stop.is_set():
                    write_json(args.state, dict(publish, state="stopped", timestamp=time.time()))
                    return 0
                if peers is None:
                    # Configuration/ownership refusal must not steal a foreign port or socket.
                    write_json(args.state, dict(publish, state="failed", reason=reason))
                    return 1
                failures = 1 if healthy and time.monotonic() - started >= 300 else failures + 1
                delay = min(args.backoff * 2 ** min(failures - 1, 5), args.max_backoff)
                print(f"mixed-pair: generation {generation}: {reason}; restart/refill in {delay:g}s", flush=True)
                write_json(args.state, dict(publish, state="backoff", reason=reason,
                                            delay_s=delay, timestamp=time.time()))
                stop.wait(delay)
            if generation:
                write_json(args.state, dict(publish, state="stopped", reason="stop requested", timestamp=time.time()))
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record")
    record.add_argument("helper_pid", type=int)
    record.add_argument("server_pid", type=int)
    record.add_argument("engine_log")
    record.add_argument("helper_log")
    run = commands.add_parser("run")
    run.add_argument("--launcher", type=Path, required=True)
    run.add_argument("--config", type=Path)
    run.add_argument("--state", type=Path, required=True)
    run.add_argument("--lock", type=Path, required=True)
    run.add_argument("--socket", type=Path, action="append", default=[])
    run.add_argument("--interval", type=float, default=1)
    run.add_argument("--stop-timeout", type=float, default=15)
    run.add_argument("--startup-timeout", type=float, default=360)
    run.add_argument("--backoff", type=float, default=2)
    run.add_argument("--max-backoff", type=float, default=30)
    run.add_argument("--nice", type=int, default=0, choices=range(20), metavar="0..19",
                     help="nice increment inherited by the launcher, helper, server and engine")
    run.add_argument("--stage-cpus", metavar="CPUS",
                     help="after attach, move CPU-0 strata-stage threads to these logical CPUs (opt-in)")
    run.add_argument("--stall-timeout", type=float, default=0, metavar="SECONDS",
                     help="restart the pair when an in-flight prompt read stops making progress "
                          "for this long; 0 (default) keeps the old wait-forever behavior")
    args = parser.parse_args()
    if args.command == "record":
        peers = {key: getattr(args, key) for key in ("helper_pid", "server_pid", "engine_log", "helper_log")}
        if not (peers["helper_pid"] or peers["server_pid"]):
            parser.error("record needs at least one peer")
        for name in ("helper", "server"):
            if peers[f"{name}_pid"] < 0:
                parser.error("peer PIDs must be non-negative")
            if peers[f"{name}_pid"] == 0:
                peers[f"{name}_start"] = 0
                continue
            token = process_token(peers[f"{name}_pid"])
            # A peer can die before publication. Publish the failed startup so
            # the supervisor retries; an empty token can never own a live PID.
            peers[f"{name}_start"] = token[1] if token is not None else ""
        write_json(Path(os.environ["STRATA_PAIR_RECORD"]), peers)
        return 0
    if not all(math.isfinite(value) and value > 0 for value in
               (args.interval, args.stop_timeout, args.startup_timeout, args.backoff, args.max_backoff)):
        parser.error("timeouts and backoff must be positive")
    if args.stage_cpus:
        if args.config is None:
            parser.error("--stage-cpus requires --config for engine discovery")
        try:
            target = sorted({int(value) for value in args.stage_cpus.split(",")})
        except ValueError:
            parser.error("--stage-cpus must be a comma-separated list of logical CPUs")
        if not target or 0 in target or not set(target) <= os.sched_getaffinity(0):
            parser.error("--stage-cpus must be allowed on this host and must exclude CPU 0")
        args.stage_cpus = ",".join(map(str, target))
    if args.stall_timeout and not (math.isfinite(args.stall_timeout) and args.stall_timeout > 0):
        parser.error("--stall-timeout must be positive seconds (0 disables it)")
    if args.stall_timeout and args.config is None:
        parser.error("--stall-timeout requires --config (progress lives in the config's logs)")
    return supervise(args)


if __name__ == "__main__":
    raise SystemExit(main())
