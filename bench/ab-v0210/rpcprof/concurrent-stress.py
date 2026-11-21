#!/usr/bin/env python3
"""concurrent-stress.py - a long decode with other requests in flight beside it.

The single-request and multi-turn shapes both survived on BOTH builds on 2026-11-21, so
neither is what broke the pool. The one difference a live agent lane has that those two do
not is concurrency: the lane's own requests overlap with the server's internal follow-ups
(a title, a summary, a compaction), and every concurrent request pins its own leases in the
managed pool.

This starts one long request and, after a short delay, fires `--parallel` short ones while
it is still generating. It reports every result and the server's own error text.
"""
import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request

WORDS = ("budget pool lease expert cache resident staging decode prefill window margin "
         "slot shard layer route tensor kernel stream memory region page fence event").split()


def block(target_tokens: int, seed: int) -> str:
    import random
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
    ap.add_argument("--long-max", type=int, default=1200)
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--delay", type=float, default=25.0)
    ap.add_argument("--seed", type=int, default=23)
    ap.add_argument("--timeout", type=float, default=5400.0)
    args = ap.parse_args()

    results = {}

    def helper(idx: int) -> None:
        started = time.time()
        try:
            payload = post(args.url, args.model,
                           "Reply with exactly one word: up", 32, 600)
            usage = payload.get("usage") or {}
            results[idx] = ("ok", usage.get("completion_tokens"), time.time() - started, "")
        except urllib.error.HTTPError as e:
            results[idx] = ("http", None, time.time() - started,
                            e.read().decode("utf-8", "replace")[:200])
        except Exception as e:
            results[idx] = ("conn", None, time.time() - started, repr(e))

    def long_call() -> None:
        started = time.time()
        try:
            payload = post(args.url, args.model,
                           block(args.long_tokens, args.seed)
                           + "\n\nWrite 1,500 more lines in the same format, one per line.",
                           args.long_max, args.timeout)
            usage = payload.get("usage") or {}
            results["long"] = ("ok", usage.get("completion_tokens"), time.time() - started,
                               "prompt=%s" % usage.get("prompt_tokens"))
        except urllib.error.HTTPError as e:
            results["long"] = ("http", None, time.time() - started,
                               e.read().decode("utf-8", "replace")[:300])
        except Exception as e:
            results["long"] = ("conn", None, time.time() - started, repr(e))

    print("concurrent-stress: long request (%.0f target tokens) plus %d parallel at +%.0fs"
          % (args.long_tokens, args.parallel, args.delay), flush=True)
    threads = [threading.Thread(target=long_call)]
    threads[0].start()
    time.sleep(args.delay)
    for i in range(args.parallel):
        t = threading.Thread(target=helper, args=(i,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    failed = 0
    for key in ["long"] + list(range(args.parallel)):
        state, tokens, elapsed, note = results.get(key, ("missing", None, 0.0, ""))
        print("%-5s %-6s tokens=%-5s %.1fs %s" % (key, state, tokens, elapsed, note), flush=True)
        if state != "ok":
            failed += 1
    print("concurrent-stress: %d of %d requests failed" % (failed, 1 + args.parallel), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
