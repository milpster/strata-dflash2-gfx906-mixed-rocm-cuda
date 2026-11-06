#!/usr/bin/env python3
"""Bounded, owned-PID-only teardown for a wedged production tree.

Escalation: SIGTERM every listed pid (in order) -> bounded shared wait ->
SIGKILL the survivors -> bounded wait -> report. Only the explicitly listed
pids are ever signalled; nothing is discovered or inferred. A D-state pid
will not die on SIGKILL — that is reported, not hidden, because the caller
(per-GPU sysfs reset) is the tool that breaks the driver wait.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def proc_state(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return "gone"


def wchan(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/wchan").read_text().strip()
    except OSError:
        return "?"


def wait_all(pids, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(alive(pid) for pid in pids):
            return True
        time.sleep(2)
    return not any(alive(pid) for pid in pids)


def kill_quiet(pid: int, sig) -> None:
    try:
        os.kill(pid, sig)
        print(f"  sent {signal.Signals(sig).name} to {pid}", flush=True)
    except ProcessLookupError:
        print(f"  {pid} already gone", flush=True)
    except PermissionError:
        print(f"  {pid}: permission denied", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pids", nargs="+", type=int,
                        help="owned pids in escalation order (launcher, server, engine)")
    parser.add_argument("--term-wait", type=float, default=90.0)
    parser.add_argument("--kill-wait", type=float, default=40.0)
    args = parser.parse_args()

    print(time.strftime("%Y-%m-%dT%H:%M:%S%z"), flush=True)
    print("--- initial:", flush=True)
    for pid in args.pids:
        print(f"  pid {pid}: {proc_state(pid)} wchan={wchan(pid) if alive(pid) else '-'}",
              flush=True)
    if not any(alive(pid) for pid in args.pids):
        print("nothing to do: no listed pid is alive", flush=True)
        return 4

    print("--- SIGTERM pass:", flush=True)
    for pid in args.pids:
        if alive(pid):
            kill_quiet(pid, signal.SIGTERM)
    if wait_all(args.pids, args.term_wait):
        print(f"all exited within TERM wait ({args.term_wait:.0f}s)", flush=True)
    else:
        print("--- SIGKILL pass:", flush=True)
        for pid in args.pids:
            if alive(pid):
                kill_quiet(pid, signal.SIGKILL)
        if wait_all(args.pids, args.kill_wait):
            print(f"all exited within KILL wait ({args.kill_wait:.0f}s)", flush=True)

    survivors = [pid for pid in args.pids if alive(pid)]
    print("--- final:", flush=True)
    for pid in args.pids:
        if alive(pid):
            print(f"  pid {pid} SURVIVES state={proc_state(pid)} wchan={wchan(pid)}",
                  flush=True)
        else:
            print(f"  pid {pid} gone", flush=True)
    print("--- port 8009:", flush=True)
    subprocess.run(["ss", "-ltnp", "sport = :8009"], timeout=15)
    print("--- VRAM:", flush=True)
    for card in ("card1", "card3"):
        try:
            vram = Path(f"/sys/class/drm/{card}/device/mem_info_vram_used").read_text().strip()
        except OSError:
            vram = "unreadable"
        try:
            busy = Path(f"/sys/class/drm/{card}/device/gpu_busy_percent").read_text().strip()
        except OSError:
            busy = "unreadable"
        print(f"  {card}: vram_used={vram} busy={busy}", flush=True)
    return 3 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
