#!/usr/bin/env python3
"""RETIRED 2026-11-21 by the operator: "we dont do concurrency, once and for all".

Do not run this and do not build a new one. The serving questions on this box are answered
with single-lane shapes: one long prompt, one parked multi-turn conversation, one agent
session at a time. Parallel-request load is a property of real agent traffic, not something
we manufacture in a leg. Kept in the tree only as a record of the measurement that used it.
Rename the second line if you ever resurrect it, so nobody mistakes it for policy.
"""

# --- original docstring follows ---
"""concurrent-stress.py - a long decode with other requests in flight beside it.

The single-request and multi-turn shapes both survived on BOTH builds on 2026-11-21, so
neither is what broke the card. Overlap is: a second request decoding while the first one
still is. This starts one long request, then fires `--parallel` others while it runs, and
reports every result plus the engine's own error text.

**Overlap is measured, not assumed.** A leg once "passed" only because the long request
stopped after 55 s with the helpers arriving at 40 s - fifteen seconds of overlap, not the
minutes the failing legs had. Every run now prints when each request was alive and the
overlap it actually achieved, so a pass with too little overlap cannot be mistaken for a
pass. Use `--min-overlap` to make that fatal for the run.

Exit status: 0 = every request answered and overlap was enough, 1 = a request failed or the
overlap was too small, 2 = the setup itself could not run.
"""
import argparse
import json
import random
import sys
import threading
import time
import urllib.error
import urllib.request

WORDS = ("budget pool lease expert cache resident staging decode prefill window margin "
         "slot shard layer route tensor kernel stream memory region page fence event").split()


def block(target_tokens: int, seed: int) -> str:
    rng = random.Random(seed)
    out, size = [], 0
    while size < target_tokens * 4:
        out.append(" ".join(rng.choice(WORDS) for _ in range(rng.randint(10, 18))))
        size += len(out[-1]) + 1
    return "\n".join(out)


def post(url: str, model: str, content: str, max_tokens: int, timeout: float):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": content}],
                       "max_tokens": max_tokens, "reasoning_effort": "low",
                       "stream": False}).encode()
    req = urllib.request.Request(url + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8009")
    ap.add_argument("--model", default="swift-1.5-iq3_xxs")
    ap.add_argument("--long-tokens", type=int, default=70000)
    ap.add_argument("--long-max", type=int, default=2500)
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--parallel-max", type=int, default=400,
                    help="how many tokens each concurrent request asks for; a long one keeps "
                         "the overlap real instead of ending before the helper arrives")
    ap.add_argument("--delay", type=float, default=25.0)
    ap.add_argument("--min-overlap", type=float, default=30.0,
                    help="seconds of genuine overlap required for a pass to count")
    ap.add_argument("--seed", type=int, default=23)
    ap.add_argument("--timeout", type=float, default=5400.0)
    args = ap.parse_args()

    t0 = time.time()
    results = {}

    def run_call(key, content, max_tokens) -> None:
        started = time.time() - t0
        try:
            payload = post(args.url, args.model, content, max_tokens, args.timeout)
            usage = payload.get("usage") or {}
            results[key] = ("ok", usage.get("completion_tokens"), started,
                            time.time() - t0, "prompt=%s" % usage.get("prompt_tokens"))
        except urllib.error.HTTPError as e:
            results[key] = ("http", None, started, time.time() - t0,
                            e.read().decode("utf-8", "replace")[:300])
        except Exception as e:
            results[key] = ("conn", None, started, time.time() - t0, repr(e))

    long_body = (block(args.long_tokens, args.seed)
                 + "\n\nWrite at least 1,500 more lines in exactly the same format, one per "
                   "line, numbered from 1. Do not summarise, do not stop early.")
    print("concurrent-stress: long request (%.0f target tokens, max %d) plus %d parallel "
          "(max %d each) at +%.0fs"
          % (args.long_tokens, args.long_max, args.parallel, args.parallel_max, args.delay),
          flush=True)
    threads = [threading.Thread(target=run_call, args=("long", long_body, args.long_max))]
    threads[0].start()
    time.sleep(args.delay)
    for i in range(args.parallel):
        threads.append(threading.Thread(
            target=run_call,
            args=(i, "Write about 300 words on why a fixed memory budget is easier to "
                     "operate than a growing one.", args.parallel_max)))
        threads[-1].start()
    for t in threads:
        t.join()

    failed = 0
    for key in ["long"] + list(range(args.parallel)):
        state, tokens, started, ended, note = results.get(key, ("missing", None, 0, 0, ""))
        print("%-5s %-6s tokens=%-5s alive=[%6.1fs,%6.1fs] %s"
              % (key, state, tokens, started, ended, note), flush=True)
        if state != "ok":
            failed += 1

    long_span = results.get("long", (None, None, 0, 0, ""))
    long_end = long_span[3]
    helpers = [results[i] for i in range(args.parallel) if i in results]
    overlap = 0.0
    if helpers and long_end:
        helper_start = min(h[2] for h in helpers)
        helper_end = max(h[3] for h in helpers)
        overlap = max(0.0, min(long_end, helper_end) - max(long_span[2], helper_start))
    print("concurrent-stress: %d of %d requests failed; genuine overlap %.1fs (need %.0fs)"
          % (failed, 1 + args.parallel, overlap, args.min_overlap), flush=True)
    if overlap < args.min_overlap:
        print("concurrent-stress: INCONCLUSIVE - the long request was not alive long enough "
              "for the helpers to overlap it", flush=True)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
