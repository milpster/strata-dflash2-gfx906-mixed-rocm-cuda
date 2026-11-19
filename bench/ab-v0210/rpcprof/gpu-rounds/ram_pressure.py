#!/usr/bin/env python3
"""Bounded RAM pressure holder for the parking validation (parking-validation-plan.md T2).

Allocates 256 MiB steps (touched) until MemAvailable <= --target-mib or --cap-mib
is reached, then holds until the hold-file disappears or SIGTERM/SIGINT arrives,
then exits. Prints one progress line per step on stdout (visible in the terminal
view) and keeps the allocation list referenced so nothing is freed early.
"""
import argparse
import os
import signal
import sys
import time
from pathlib import Path


def mem_available_mib() -> int:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 0


class Holder:
    keep = []
    stop = False

    def __init__(self):
        signal.signal(signal.SIGTERM, self.quit)
        signal.signal(signal.SIGINT, self.quit)

    def quit(self, *_a):
        self.stop = True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-mib", type=int, default=4096)
    ap.add_argument("--cap-mib", type=int, default=16384)
    ap.add_argument("--step-mib", type=int, default=256)
    ap.add_argument("--hold-file", required=True)
    ap.add_argument("--sample-file")
    args = ap.parse_args()

    h = Holder()
    step = bytearray(args.step_mib * 1024 * 1024)
    samples = []
    allocated = 0
    while not h.stop and allocated < args.cap_mib and mem_available_mib() > args.target_mib:
        b = bytearray(args.step_mib * 1024 * 1024)
        for i in range(0, len(b), 4096):
            b[i] = 1  # touch one byte per page
        h.keep.append(b)
        allocated += args.step_mib
        rec = {"t": time.strftime("%H:%M:%S"), "allocated_mib": allocated,
               "mem_available_mib": mem_available_mib()}
        samples.append(rec)
        print(f"[pressure] {rec}", flush=True)
        time.sleep(1)

    print(f"[pressure] holding {allocated} MiB (MemAvailable {mem_available_mib()} MiB); "
          f"stop by removing {args.hold_file} or SIGTERM", flush=True)
    if args.sample_file:
        Path(args.sample_file).write_text("\n".join(json_dumps(s) for s in samples) + "\n")

    while not h.stop and Path(args.hold_file).exists():
        samples.append({"t": time.strftime("%H:%M:%S"), "allocated_mib": allocated,
                        "mem_available_mib": mem_available_mib()})
        time.sleep(5)
    if args.sample_file:
        with open(args.sample_file, "a") as f:
            f.write("\n".join(json_dumps(s) for s in samples[allocated // args.step_mib:]) + "\n")
    print(f"[pressure] releasing {allocated} MiB and exiting", flush=True)
    return 0


def json_dumps(o) -> str:
    import json
    return json.dumps(o)


if __name__ == "__main__":
    raise SystemExit(main())
