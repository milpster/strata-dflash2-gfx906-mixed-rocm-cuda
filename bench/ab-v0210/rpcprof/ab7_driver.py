#!/usr/bin/env python3
"""ab7 load driver: N requests serial, then the same N concurrently.

The ratio sum(individual)/wall under concurrency shows effective parallelism
(fully serialized serving -> ~1.0). The helper's queue_us profile lines (from
STRATA_RPC_PROFILE=1 on the pair) give the per-request head-of-line truth."""
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

URL = "http://127.0.0.1:8009/v1/chat/completions"
N = 6


def one(i: int) -> float:
    body = json.dumps({
        "messages": [{"role": "user",
                      "content": f"Write {i + 3} short numbered points about cache eviction. Be terse."}],
        "max_tokens": 160,
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        json.load(r)
    return time.time() - t0


print("phase1 serial:", flush=True)
s = [one(i) for i in range(N)]
print("  walls:", [round(x, 1) for x in s], "sum", round(sum(s), 1), flush=True)

print("phase2 concurrent(6):", flush=True)
t0 = time.time()
with ThreadPoolExecutor(N) as ex:
    c = list(ex.map(one, range(N)))
wall = time.time() - t0
print("  walls:", [round(x, 1) for x in c], "wall", round(wall, 1), flush=True)
print(f"effective-parallelism: {sum(c) / wall:.2f}x (1.0 = fully serialized)", flush=True)
