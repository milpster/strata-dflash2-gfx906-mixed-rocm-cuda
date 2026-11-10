#!/usr/bin/env python3
"""Capture a live Strata stall without sending prompts or controlling processes."""
from __future__ import annotations

import argparse
import datetime
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        return f"unavailable: {error}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8009")
    parser.add_argument("--state", type=Path,
                        default=ROOT / "run/prod-swift15-f16kv-mixed-supervisor.json")
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.seconds <= 0 or args.interval < 0.25:
        parser.error("--seconds must be positive and --interval must be at least 0.25")
    out = args.out or ROOT / "run" / datetime.datetime.now().strftime("stall-watch-%Y%m%d-%H%M%S-%f")
    out.mkdir(parents=True, exist_ok=False)
    (out / "manifest.json").write_text(json.dumps({
        "url": args.url, "state": str(args.state), "seconds": args.seconds,
        "interval": args.interval, "mode": "read-only GET /metrics and procfs",
        "started": datetime.datetime.now().astimezone().isoformat(),
    }, indent=2) + "\n", encoding="utf-8")
    print(f"Read-only capture: {out}", flush=True)
    until = time.monotonic() + args.seconds
    previous = None
    changed = time.monotonic()
    last_dump = 0.0
    last_report = 0.0
    with (out / "samples.jsonl").open("x", encoding="utf-8") as samples:
        while time.monotonic() < until:
            sample = {"at": datetime.datetime.now().astimezone().isoformat()}
            try:
                with urllib.request.urlopen(args.url.rstrip("/") + "/metrics", timeout=3) as response:
                    metrics = json.load(response)
                sample["live"] = metrics.get("live", {})
                sample["last_request"] = next(iter(metrics.get("requests", [])), None)
                sample["hardware"] = metrics.get("hardware", {})
                sample["cache"] = metrics.get("conversation_cache", {})
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
                sample["metrics_error"] = str(error)
            try:
                state = json.loads(args.state.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                state = {}
                sample["state_error"] = str(error)
            sample["generation"] = state.get("generation")
            processes = {}
            for role in ("engine", "helper"):
                pid = state.get(role + "_pid")
                if not isinstance(pid, int) or pid <= 0:
                    continue
                proc = Path("/proc") / str(pid)
                processes[role] = {"pid": pid, **{
                    name: read_text(proc / name) for name in ("stat", "status", "io", "wchan")
                }}
            sample["processes"] = processes
            sample["meminfo"] = read_text(Path("/proc/meminfo"))
            sample["vmstat"] = "\n".join(line for line in read_text(Path("/proc/vmstat")).splitlines()
                                          if line.startswith(("pswp", "pgmajfault ", "allocstall", "pgscan")))
            sample["diskstats"] = "\n".join(line for line in read_text(Path("/proc/diskstats")).splitlines()
                                             if "loop" not in line)
            live = sample.get("live", {})
            progress = (state.get("engine_pid"), live.get("state"), live.get("generated"),
                        live.get("prompt_read"), live.get("disk_io"))
            now = time.monotonic()
            if previous != progress:
                previous, changed = progress, now
            # A cache save can still be labelled "generating". Preserve raw disk_io: an old
            # "done" value can remain on the engine and is not proof this request saved.
            if live.get("state") in ("reading", "generating") and now - changed >= 10 and now - last_dump >= 10:
                last_dump = now
                tasks = {}
                pid = state.get("engine_pid")
                if isinstance(pid, int) and pid > 0:
                    try:
                        for task in (Path("/proc") / str(pid) / "task").iterdir():
                            tasks[task.name] = {name: read_text(task / name) for name in ("wchan", "stack")}
                    except OSError as error:
                        sample["tasks_error"] = str(error)
                sample["tasks"] = tasks
                print(f"{sample['at']} no observed progress for {now - changed:.1f}s; captured thread waits", flush=True)
            samples.write(json.dumps(sample) + "\n")
            samples.flush()
            if now - last_report >= 30:
                last_report = now
                print(f"{sample['at']} state={live.get('state')} generated={live.get('generated')} "
                      f"disk_io={live.get('disk_io')}", flush=True)
            time.sleep(min(args.interval, max(0, until - time.monotonic())))
    print(f"Capture complete: {out / 'samples.jsonl'}", flush=True)


if __name__ == "__main__":
    main()
