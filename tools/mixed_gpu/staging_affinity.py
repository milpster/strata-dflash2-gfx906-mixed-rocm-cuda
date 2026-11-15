#!/usr/bin/env python3
"""Linux-only, reversible affinity trial for Strata's CPU-0 staging threads.

This does not change the host, expert workers, hostapp, or startup configuration.
Use inspect first; apply requires an explicit CPU list and a fresh snapshot path.
"""

import argparse
import collections
import datetime
import json
import os
from pathlib import Path


def start_time(path):
    return path.joinpath("stat").read_text().rsplit(") ", 1)[1].split()[19]


def threads(pid):
    for path in sorted(Path(f"/proc/{pid}/task").iterdir()):
        try:
            yield {
                "tid": int(path.name),
                "name": path.joinpath("comm").read_text().strip(),
                "start": start_time(path),
                "cpus": sorted(os.sched_getaffinity(int(path.name))),
            }
        except (FileNotFoundError, ProcessLookupError):
            continue


def engine(pid):
    """The engine renames its main thread strata-host only late in serve startup;
    its `strata --serve` command line identifies it from the first exec on."""
    try:
        if Path(f"/proc/{pid}/comm").read_text().strip() == "strata-host":
            return True
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return any(part.rsplit(b"/", 1)[-1] == b"strata" and next_arg == b"--serve"
               for part, next_arg in zip(argv, argv[1:]))


def inspect(pid):
    groups = collections.Counter((t["name"], tuple(t["cpus"])) for t in threads(pid))
    for (name, cpus), count in sorted(groups.items()):
        print(f"{count:3} {name:20} CPUs={','.join(map(str, cpus))}", flush=True)


def restore(saved):
    pid = saved["pid"]
    if start_time(Path(f"/proc/{pid}")) != saved["start"]:
        raise RuntimeError("Engine PID was reused; refusing to restore another process")
    current = {t["tid"]: t for t in threads(pid)}
    restored = 0
    for original in saved["threads"]:
        now = current.get(original["tid"])
        if now is None or (now["name"], now["start"]) != (original["name"], original["start"]):
            print(f"SKIP ended/replaced thread {original['tid']}", flush=True)
            continue
        if now["cpus"] == original["cpus"]:
            continue
        if now["cpus"] != saved["target"]:
            print(f"SKIP externally changed affinity for {original['tid']}", flush=True)
            continue
        os.sched_setaffinity(original["tid"], original["cpus"])
        if sorted(os.sched_getaffinity(original["tid"])) != original["cpus"]:
            raise RuntimeError(f"Restore readback failed for {original['tid']}")
        restored += 1
    print(f"Restored {restored} staging threads", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "apply", "restore"))
    parser.add_argument("--pid", type=int)
    parser.add_argument("--cpus", help="Explicit comma-separated logical CPUs")
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()
    if args.action == "restore":
        if args.snapshot is None:
            parser.error("restore requires --snapshot")
        restore(json.loads(args.snapshot.read_text()))
        return
    if args.pid is None:
        parser.error("inspect/apply requires --pid")
    process = Path(f"/proc/{args.pid}")
    if not engine(args.pid):
        parser.error("PID must name a live strata-host process")
    if args.action == "inspect":
        inspect(args.pid)
        return
    if args.snapshot is None or args.cpus is None:
        parser.error("apply requires --snapshot and --cpus")
    target = sorted({int(cpu) for cpu in args.cpus.split(",")})
    if not target or 0 in target or not set(target) <= os.sched_getaffinity(0):
        parser.error("CPUs must be allowed on this host and must exclude CPU 0")
    stage = [t for t in threads(args.pid) if t["name"] == "strata-stage"]
    movable = [t for t in stage if t["cpus"] == [0]]
    if not stage:
        parser.error("No CPU-0 strata-stage threads found; nothing changed")
    saved = {
        "pid": args.pid,
        "start": start_time(process),
        "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "target": target,
        "threads": movable,
    }
    # Threads already off CPU 0 (moved before, or engine-placed) leave nothing to do.
    if movable:
        # Save before the first mutation. Exclusive creation prevents overwriting a rollback.
        with args.snapshot.open("x") as output:
            json.dump(saved, output, indent=2)
            output.write("\n")
        try:
            for thread in movable:
                os.sched_setaffinity(thread["tid"], target)
                if sorted(os.sched_getaffinity(thread["tid"])) != target:
                    raise RuntimeError(f"Affinity readback failed for {thread['tid']}")
        except (OSError, RuntimeError):
            restore(saved)
            raise
    print(f"Moved {len(movable)} staging threads to CPUs {args.cpus}", flush=True)
    if movable:
        print(f"Rollback: {__file__} restore --snapshot {args.snapshot}", flush=True)
    inspect(args.pid)


if __name__ == "__main__":
    main()
