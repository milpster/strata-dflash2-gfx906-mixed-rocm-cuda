#!/usr/bin/env python3
"""Storage queue-depth probe: random 2 MiB reads from a big file at several depths.

Usage: bench-storage-depth.py [file] (default: the IQ3_S GGUF pack)
Reports MiB/ms per depth. Read-only. ~25-40 s of storage work.
"""
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor

PATH = sys.argv[1] if len(sys.argv) > 1 else "/mnt/srcds/ai/Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S.gguf"
BS = 2 * 1024 * 1024
GIB = 6 * 1024 * 1024 * 1024  # per depth

fd = os.open(PATH, os.O_RDONLY)
size = os.fstat(fd).st_size
blocks = size // BS
rng = random.Random(42)
offsets_at = lambda n: [rng.randrange(blocks) * BS for _ in range(n)]
buf = bytearray(BS)

for depth in (8, 32, 64, 128):
    n_reads = GIB // BS
    offsets = offsets_at(n_reads)
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=depth) as ex:
        futs = [ex.submit(os.preadv, fd, [buf], off) for off in offsets]
        for f in futs:
            f.result()
    dt = time.monotonic() - t0
    mibs = n_reads * BS / 1048576
    print(f"depth {depth:>3}: {mibs/dt:7.1f} MiB/s ({dt:.1f} s for {mibs:.0f} MiB)", flush=True)
os.close(fd)
