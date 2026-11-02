#!/usr/bin/env python3
"""Opt-in restart of an owned mixed-GPU launcher after either peer is lost.

No GPU calls or reconnect protocol. A recovery interrupts the active request and
reloads both peers. Existing launchers without --recover keep their old behavior.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import threading
import time


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def process_token(pid: int) -> tuple[str, str] | None:
    """State and start time; a zombie is not a living peer, PID reuse is not ownership."""
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
            continue  # A replacement is not ours, even if it is not a socket.
        if current == token:
            path.unlink()


def descendants(root: int, known: dict[int, str]) -> dict[int, str]:
    """Remember owned descendants, including engines in their own process group."""
    table = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            table[int(entry.name)] = (int(fields[1]), fields[19])
        except (FileNotFoundError, ProcessLookupError):
            continue
    # Never forget a start token: a re-used ancestor PID must not adopt a
    # stranger's descendants after our old ancestor has exited.
    result = dict(known)
    while True:
        added = {pid: start for pid, (parent, start) in table.items()
                 if parent in result and pid not in result and parent in table
                 and table[parent][1] == result[parent]}
        if not added:
            return result
        result.update(added)


def stop_group(child: subprocess.Popen, timeout: float, known: dict[int, str]) -> None:
    """Bounded cleanup of this launcher's tree, with PID-reuse checks throughout."""
    known = descendants(child.pid, known)
    for pid, start in known.items():
        # The server owns graceful engine shutdown; do not TERM isolated engines
        # at the same time. They get the deadline below and Linux lifeline too.
        try:
            if alive(pid, start) and os.getpgid(pid) == child.pid:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        child.poll()
        known = descendants(child.pid, known)
        if not any(alive(pid, start) for pid, start in known.items()):
            break
        time.sleep(0.1)
    for pid, start in known.items():
        if alive(pid, start):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    child.wait(timeout=5)
    deadline = time.monotonic() + 5
    while any(alive(pid, start) for pid, start in known.items()):
        if time.monotonic() >= deadline:
            raise RuntimeError("Owned processes did not exit; refusing another generation")
        time.sleep(0.1)


def supervise(args: argparse.Namespace) -> int:
    args.state.parent.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _signum, _frame: stop.set())
    # One owner for the shared production port, including different model launchers.
    with args.lock.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"Another pair supervisor owns {args.lock}") from None
        with tempfile.TemporaryDirectory(prefix="mixed-pair-", dir=args.state.parent) as work:
            record = Path(work) / "peers.json"
            generation = 0
            failures = 0
            while not stop.is_set():
                generation += 1
                record.unlink(missing_ok=True)
                for path in args.socket:
                    if path.exists() or path.is_symlink():
                        raise RuntimeError(f"Refusing existing socket path: {path}")
                environment = dict(os.environ, STRATA_PAIR_RECORD=str(record))
                started = time.monotonic()
                child = subprocess.Popen(["bash", str(args.launcher)], env=environment,
                                         start_new_session=True)
                peers = None
                known = {child.pid: process_token(child.pid)[1]}
                owned: dict[Path, tuple[int, int]] = {}
                healthy = False
                reason = "launcher exited"
                publish = dict(supervisor_pid=os.getpid(), launcher_pid=child.pid,
                               generation=generation, state="starting", timestamp=time.time())
                try:
                    write_json(args.state, publish)
                    while not stop.wait(args.interval):
                        known = descendants(child.pid, known)
                        for path in args.socket:
                            token = socket_token(path)
                            if token is not None:
                                owned.setdefault(path, token)
                        if record.exists() and peers is None:
                            peers = json.loads(record.read_text())
                            # helper_pid 0 = a helper-off A/B arm: no helper process
                            # exists, so its identity/dead checks are skipped and attach
                            # keys on the server's own "ready: http://" line instead.
                            peer_names = ("server",) if peers.get("helper_pid") == 0 else ("helper", "server")
                            for name in peer_names:
                                pid = peers[f"{name}_pid"]
                                start = peers[f"{name}_start"]
                                try:
                                    if alive(pid, start) and (pid not in known or os.getpgid(pid) != child.pid):
                                        raise RuntimeError(f"{name} PID {pid} is outside the owned group")
                                except ProcessLookupError:
                                    pass  # Exit between the identity check and getpgid is a peer death.
                            publish.update(peers, state="monitoring")
                            write_json(args.state, publish)
                        if peers is not None:
                            dead = [name for name in
                                    (("server",) if peers.get("helper_pid") == 0 else ("helper", "server"))
                                    if not alive(peers[f"{name}_pid"], peers[f"{name}_start"])]
                            if dead:
                                reason = ", ".join(dead) + " exited"
                                break
                            if peers.get("helper_pid") == 0:
                                log_path = Path(peers["engine_log"])
                                attached = ("ready: http://" in log_path.read_text(errors="replace")
                                            if log_path.exists() else False)
                            else:
                                helper_log = Path(peers["helper_log"]).read_text(errors="replace")
                                # A lost secondary channel can leave the helper process alive.
                                if any(mark in helper_log for mark in (
                                    "channel:", "request failed:", "the engine said bye; exiting",
                                    "the engine idled past the timeout; exiting"
                                )):
                                    reason = "helper channel ended"
                                    break
                                attached = "expert-helper: filled " in helper_log
                                if len(args.socket) > 1:
                                    attached &= "second-channel engine connected" in helper_log
                            if attached and not healthy:
                                healthy = True
                                publish.update(state="attached", attached_at=time.time())
                                write_json(args.state, publish)
                        if child.poll() is not None:
                            break
                        if not healthy and time.monotonic() - started > args.startup_timeout:
                            reason = "helper did not attach and fill before the startup deadline"
                            break
                finally:
                    stop_group(child, args.stop_timeout, known)
                    # After SIGKILL the helper cannot unlink its sockets itself. Only
                    # remove the exact same owned inodes we observed, after peer exit.
                    release_sockets(owned)
                if stop.is_set():
                    write_json(args.state, dict(publish, state="stopped", timestamp=time.time()))
                    return 0
                if peers is None:
                    # Missing executable, foreign socket, or occupied port: no retry
                    # loop that might delete a stranger's socket or steal its port.
                    write_json(args.state, dict(publish, state="failed", reason=reason))
                    return 1
                failures = 1 if healthy and time.monotonic() - started >= 300 else failures + 1
                delay = min(args.backoff * 2 ** min(failures - 1, 5), args.max_backoff)
                print(f"mixed-pair: generation {generation}: {reason}; restart/refill in {delay:g}s",
                      flush=True)
                write_json(args.state, dict(publish, state="backoff", reason=reason,
                                            delay_s=delay, timestamp=time.time()))
                stop.wait(delay)
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
    run.add_argument("--state", type=Path, required=True)
    run.add_argument("--lock", type=Path, required=True)
    run.add_argument("--socket", type=Path, action="append", default=[])
    run.add_argument("--interval", type=float, default=1)
    run.add_argument("--stop-timeout", type=float, default=15)
    run.add_argument("--startup-timeout", type=float, default=360)
    run.add_argument("--backoff", type=float, default=2)
    run.add_argument("--max-backoff", type=float, default=30)
    args = parser.parse_args()
    if args.command == "record":
        peers = {
            key: getattr(args, key) for key in ("helper_pid", "server_pid", "engine_log", "helper_log")
        }
        # helper-less pairs (the helper-off A/B arms) record helper_pid 0: no process
        # token exists for a helper that was never started. Production always passes a
        # real pid, so this branch is unreachable on the canonical path.
        for name in ("helper", "server"):
            if name == "helper" and peers["helper_pid"] == 0:
                peers["helper_start"] = 0
                continue
            token = process_token(peers[f"{name}_pid"])
            if token is None or token[0] == "Z":
                raise RuntimeError(f"{name} exited before publication")
            peers[f"{name}_start"] = token[1]
        write_json(Path(os.environ["STRATA_PAIR_RECORD"]), peers)
        return 0
    if min(args.interval, args.stop_timeout, args.startup_timeout, args.backoff, args.max_backoff) <= 0:
        parser.error("timeouts and backoff must be positive")
    return supervise(args)


if __name__ == "__main__":
    raise SystemExit(main())
