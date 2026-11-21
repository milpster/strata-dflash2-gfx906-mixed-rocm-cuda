#!/usr/bin/env python3
"""longctx-stress.py - reproduce the long-context death shape on the serving port.

The 2026-11-21 prod deaths were not prefill failures: they came during DECODE of a long
context (53,717-token prompt after 1,694 generated tokens; 64,507-token prompt after 691).
This sends one request of that shape - a large synthetic prompt, a generation long enough
to walk many layers of decode - and reports what happened, so a fix can be judged by the
same shape that broke, not by a short probe.

Usage:
    longctx-stress.py [--url http://127.0.0.1:8009] [--model NAME]
                      [--prompt-tokens 60000] [--max-tokens 1500] [--seed 7]

Exit status: 0 = a completion came back, 1 = the server returned an error or no answer,
2 = the connection dropped (the server died mid-request, which is itself the answer).
"""
import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request

WORDS = (
    "budget pool lease expert cache resident staging decode prefill window margin slot "
    "shard layer route tensor kernel stream memory region page table fence event queue "
    "worker thread lock mutex atomic counter window sample metric ledger window record "
    "token context prompt answer batch graph allocation release admission eviction pin "
    "transport socket channel helper engine server client request response latency clock"
).split()


def synthetic_prompt(target_tokens: int, seed: int) -> str:
    """A prompt with real variety, so the tokenizer does not collapse repeats.

    Roughly four characters per token in English prose; the count is normalised later from
    the server's own usage numbers, so this only has to land in the right neighbourhood.
    """
    rng = random.Random(seed)
    target_chars = target_tokens * 4
    out = []
    size = 0
    while size < target_chars:
        sentence = " ".join(rng.choice(WORDS) for _ in range(rng.randint(8, 16)))
        line = "%s %d. %s." % (sentence, rng.randint(1, 9999), sentence)
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8009")
    ap.add_argument("--model", default="swift-1.5-iq3_xxs")
    ap.add_argument("--prompt-tokens", type=int, default=60000)
    ap.add_argument("--max-tokens", type=int, default=1500)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--timeout", type=float, default=3600.0)
    args = ap.parse_args()

    prompt = synthetic_prompt(args.prompt_tokens, args.seed)
    body = json.dumps({
        "model": args.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": args.max_tokens,
        "reasoning_effort": "low",
        "stream": False,
    }).encode()
    req = urllib.request.Request(args.url + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    started = time.time()
    print("longctx-stress: prompt chars=%d target_tokens=%d max_tokens=%d url=%s"
          % (len(prompt), args.prompt_tokens, args.max_tokens, args.url), flush=True)
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:800]
        print("longctx-stress: HTTP %s after %.1fs: %s" % (e.code, time.time() - started, detail))
        return 1
    except Exception as e:                      # connection dropped / server died mid-request
        print("longctx-stress: connection failed after %.1fs: %r" % (time.time() - started, e))
        return 2

    usage = payload.get("usage") or {}
    msg = (payload.get("choices") or [{}])[0].get("message") or {}
    content = msg.get("content") or ""
    elapsed = time.time() - started
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    speed = (completion_tokens / elapsed) if completion_tokens and elapsed > 0 else None
    print("longctx-stress: prompt_tokens=%s completion_tokens=%s elapsed=%.1fs overall=%s tok/s"
          % (prompt_tokens, completion_tokens, elapsed,
             ("%.1f" % speed) if speed else "n/a"))
    print("longctx-stress: answer chars=%d head=%r" % (len(content), content[:120]))
    return 0 if content else 1


if __name__ == "__main__":
    sys.exit(main())
